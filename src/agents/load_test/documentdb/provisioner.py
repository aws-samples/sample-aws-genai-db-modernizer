"""DocumentDB cluster provisioner for load testing.

Implements :class:`BaseProvisioner` for DocumentDB. Uses the sizing module
(ADR-022) to derive cluster configuration from the source workload, then:

1. Generates a random master password and stores it in Secrets Manager
2. Creates the DocumentDB cluster
3. Creates the writer instance and (in parallel) any read replicas
4. Waits for all instances to reach the ``available`` state
5. Bootstraps an IAM user in the ``$external`` database for the load test
   ECS task role (per AWS documentation on MONGODB-AWS authentication)

Teardown deletes instances → cluster → Secrets Manager secret, idempotently.

VPC infrastructure (subnet group, security groups) is **not** provisioned here.
It is expected to exist already and is read from environment variables:

  - ``DOCDB_DB_SUBNET_GROUP_NAME`` — pre-existing DocumentDB subnet group
  - ``DOCDB_VPC_SECURITY_GROUP_IDS`` — comma-separated security group IDs

Environment variables for IAM bootstrap (resolved via STS GetCallerIdentity):

  - ``LOAD_TEST_TASK_ROLE_ARN`` — optional override; otherwise auto-resolved
"""

import concurrent.futures
import json
import os
import secrets
import time
from typing import Any

import boto3
import structlog

from src.agents.load_test.base import BaseProvisioner
from src.agents.load_test.documentdb.sizing import ClusterConfig, derive_cluster_config
from src.contracts.load_test_models import DeployedResource, InfrastructureManifest

logger = structlog.get_logger()

# DocumentDB resource naming (lowercase + hyphens, DocumentDB requirement)
RESOURCE_NAME_PREFIX = "loadtest"
MASTER_USERNAME = "loadtestadmin"

# Bootstrap retry settings — cluster endpoint accepts connections ~30s after Available
BOOTSTRAP_MAX_ATTEMPTS = 5
BOOTSTRAP_INITIAL_BACKOFF_SECONDS = 10
BOOTSTRAP_BACKOFF_MULTIPLIER = 1.5

# Secrets Manager naming
SECRET_NAME_TEMPLATE = (
    "LoadTest_DocumentDB_{run_id}_primary"  # nosec B105 — template for secret name, not a password
)

# Required env vars
ENV_SUBNET_GROUP = "DOCDB_DB_SUBNET_GROUP_NAME"
ENV_SECURITY_GROUPS = "DOCDB_VPC_SECURITY_GROUP_IDS"
ENV_TASK_ROLE_ARN = "LOAD_TEST_TASK_ROLE_ARN"

# DocumentDB CA bundle — bundled into the load test Docker image (commit 3)
DOCDB_CA_BUNDLE_PATH = "/etc/ssl/certs/docdb-global-bundle.pem"


class DocumentDBProvisioner(BaseProvisioner):
    """Provisions and tears down DocumentDB clusters from schema design + sizing."""

    def __init__(self, region: str = "us-east-1") -> None:
        self.region = region
        self.docdb = boto3.client("docdb", region_name=region)
        self.secrets_manager = boto3.client("secretsmanager", region_name=region)
        self.sts = boto3.client("sts", region_name=region)

    # =========================================================================
    # Public API
    # =========================================================================

    def provision(self, schema_output: Any, tags: dict[str, str]) -> InfrastructureManifest:
        """Create DocumentDB cluster, instances, and bootstrap IAM auth."""
        # Sizing requires collector_output and test_config which are not in the
        # provision() signature. They are looked up from schema_output (carried
        # there by the coordinator before calling provision). If absent, sizing
        # falls back to fallback strategy.
        collector_output = schema_output.get("_collector_output", {})
        test_config = schema_output.get("_test_config")

        cluster_config = derive_cluster_config(schema_output, collector_output, test_config)
        logger.info(
            "documentdb_sizing_complete",
            instance_class=cluster_config.instance_class,
            replica_count=cluster_config.replica_count,
            strategy=cluster_config.sizing_strategy,
        )

        run_id = tags.get("run_id", secrets.token_hex(6))
        cluster_id = self._cluster_identifier(run_id)

        # Bundle resources progressively so partial failures still report what
        # was created — the coordinator's finally-block teardown can clean up.
        resources: list[DeployedResource] = []

        # 1. Generate password and store in Secrets Manager
        master_password = self._generate_password()
        secret_arn = self._create_secret(run_id, master_password, tags)
        resources.append(
            DeployedResource(
                resource_type="AWS::SecretsManager::Secret",
                resource_arn=secret_arn,
                configuration={"secret_name": SECRET_NAME_TEMPLATE.format(run_id=run_id)},
            )
        )

        # 2. Create cluster
        cluster_arn, cluster_endpoint, reader_endpoint = self._create_cluster(
            cluster_id, cluster_config, master_password, tags
        )
        resources.append(
            DeployedResource(
                resource_type="AWS::DocDB::DBCluster",
                resource_arn=cluster_arn,
                configuration={
                    "cluster_identifier": cluster_id,
                    "cluster_endpoint": cluster_endpoint,
                    "reader_endpoint": reader_endpoint,
                    "port": 27017,
                    "engine_version": cluster_config.engine_version,
                    "master_username": MASTER_USERNAME,
                    "secret_arn": secret_arn,
                    "instance_class": cluster_config.instance_class,
                    "replica_count": cluster_config.replica_count,
                    "sizing_strategy": cluster_config.sizing_strategy,
                },
            )
        )
        self._wait_cluster_available(cluster_id)

        # 3. Create writer instance
        writer_id = self._instance_identifier(run_id, 0)
        writer_arn = self._create_instance(writer_id, cluster_id, cluster_config, tags)
        resources.append(
            DeployedResource(
                resource_type="AWS::DocDB::DBInstance",
                resource_arn=writer_arn,
                configuration={
                    "instance_identifier": writer_id,
                    "instance_class": cluster_config.instance_class,
                    "instance_role": "writer",
                },
            )
        )

        # 4. Create replica instances in parallel
        if cluster_config.replica_count > 0:
            replica_resources = self._create_replicas_parallel(
                run_id, cluster_id, cluster_config, tags
            )
            resources.extend(replica_resources)

        # 5. Wait for all instances Available
        self._wait_instances_available(
            [r for r in resources if r.resource_type == "AWS::DocDB::DBInstance"]
        )

        # 6. Bootstrap IAM user for the load test task role
        # Failure is non-fatal: log warning and continue. The operator can fix
        # via the master password (still in Secrets Manager).
        try:
            self._bootstrap_iam_user(cluster_endpoint, master_password)
            logger.info("documentdb_iam_bootstrap_complete", cluster_id=cluster_id)
        except Exception as exc:
            logger.warning(
                "documentdb_iam_bootstrap_failed",
                cluster_id=cluster_id,
                error=str(exc),
                hint="primary password is in Secrets Manager; manual bootstrap possible",
            )

        return InfrastructureManifest(resources=resources, tags=tags)

    def teardown(self, manifest: InfrastructureManifest) -> None:
        """Delete instances → cluster → secret. Idempotent."""
        instances = [r for r in manifest.resources if r.resource_type == "AWS::DocDB::DBInstance"]
        clusters = [r for r in manifest.resources if r.resource_type == "AWS::DocDB::DBCluster"]
        secrets_resources = [
            r for r in manifest.resources if r.resource_type == "AWS::SecretsManager::Secret"
        ]

        # 1. Delete instances first (DocumentDB requirement)
        for instance in instances:
            instance_id = instance.configuration["instance_identifier"]
            logger.info("deleting_documentdb_instance", instance_id=instance_id)
            try:
                self.docdb.delete_db_instance(DBInstanceIdentifier=instance_id)
            except self.docdb.exceptions.DBInstanceNotFoundFault:
                logger.warning(
                    "documentdb_instance_not_found_during_teardown", instance_id=instance_id
                )

        # 2. Wait for instances to actually disappear before deleting cluster
        for instance in instances:
            try:
                waiter = self.docdb.get_waiter("db_instance_deleted")
                waiter.wait(
                    DBInstanceIdentifier=instance.configuration["instance_identifier"],
                    WaiterConfig={"Delay": 30, "MaxAttempts": 40},
                )
            except Exception as exc:
                logger.warning(
                    "documentdb_instance_delete_wait_failed",
                    instance_id=instance.configuration["instance_identifier"],
                    error=str(exc),
                )

        # 3. Delete clusters
        for cluster in clusters:
            cluster_id = cluster.configuration["cluster_identifier"]
            logger.info("deleting_documentdb_cluster", cluster_id=cluster_id)
            try:
                self.docdb.delete_db_cluster(
                    DBClusterIdentifier=cluster_id,
                    SkipFinalSnapshot=True,
                )
            except self.docdb.exceptions.DBClusterNotFoundFault:
                logger.warning(
                    "documentdb_cluster_not_found_during_teardown", cluster_id=cluster_id
                )

        # 4. Delete secrets (force immediate so we don't pay for the recovery window)
        for secret in secrets_resources:
            secret_name = secret.configuration["secret_name"]
            logger.info("deleting_secret", secret_name=secret_name)
            try:
                self.secrets_manager.delete_secret(
                    SecretId=secret_name, ForceDeleteWithoutRecovery=True
                )
            except self.secrets_manager.exceptions.ResourceNotFoundException:
                logger.warning("secret_not_found_during_teardown", secret_name=secret_name)

    # =========================================================================
    # Internal helpers
    # =========================================================================

    def _cluster_identifier(self, run_id: str) -> str:
        # DocumentDB requires lowercase letters, digits, hyphens; must start with
        # a letter and not end with hyphen. token_hex output meets this.
        return f"{RESOURCE_NAME_PREFIX}-{run_id[:12]}"

    def _instance_identifier(self, run_id: str, index: int) -> str:
        return f"{RESOURCE_NAME_PREFIX}-{run_id[:12]}-{index}"

    def _generate_password(self) -> str:
        """Generate a 32-character URL-safe random password.

        DocumentDB master passwords accept printable ASCII excluding /, ", and @.
        ``token_urlsafe`` returns base64-url chars (a-z, A-Z, 0-9, -, _) which all
        satisfy DocumentDB's constraints.
        """
        return secrets.token_urlsafe(32)

    def _create_secret(self, run_id: str, password: str, tags: dict[str, str]) -> str:
        secret_name = SECRET_NAME_TEMPLATE.format(run_id=run_id)
        response = self.secrets_manager.create_secret(
            Name=secret_name,
            Description=f"DocumentDB primary password for load test run {run_id}",
            SecretString=json.dumps(
                {
                    "username": MASTER_USERNAME,
                    "password": password,
                    "engine": "docdb",
                }
            ),
            Tags=[{"Key": k, "Value": v} for k, v in tags.items()],
        )
        return str(response["ARN"])

    def _create_cluster(
        self,
        cluster_id: str,
        config: ClusterConfig,
        master_password: str,
        tags: dict[str, str],
    ) -> tuple[str, str, str]:
        """Create the DocumentDB cluster. Returns (arn, endpoint, reader_endpoint)."""
        subnet_group = self._require_env(ENV_SUBNET_GROUP)
        sg_ids = [s.strip() for s in self._require_env(ENV_SECURITY_GROUPS).split(",") if s.strip()]

        try:
            response = self.docdb.create_db_cluster(
                DBClusterIdentifier=cluster_id,
                Engine="docdb",
                EngineVersion=config.engine_version,
                MasterUsername=MASTER_USERNAME,
                MasterUserPassword=master_password,
                DBSubnetGroupName=subnet_group,
                VpcSecurityGroupIds=sg_ids,
                StorageEncrypted=True,
                BackupRetentionPeriod=1,  # minimum, ephemeral test
                DeletionProtection=False,
                Tags=[{"Key": k, "Value": v} for k, v in tags.items()],
                Port=27017,
            )
            cluster = response["DBCluster"]
        except self.docdb.exceptions.DBClusterAlreadyExistsFault:
            logger.warning("documentdb_cluster_already_exists", cluster_id=cluster_id)
            cluster = self.docdb.describe_db_clusters(DBClusterIdentifier=cluster_id)["DBClusters"][
                0
            ]

        return (
            str(cluster["DBClusterArn"]),
            str(cluster["Endpoint"]),
            str(cluster["ReaderEndpoint"]),
        )

    def _wait_cluster_available(self, cluster_id: str) -> None:
        """Block until cluster status is 'available'.

        DocumentDB doesn't ship a ``db_cluster_available`` waiter in older boto3
        versions, so we poll describe_db_clusters manually.
        """
        logger.info("waiting_documentdb_cluster_available", cluster_id=cluster_id)
        deadline = time.time() + 1200  # 20 min hard cap
        while time.time() < deadline:
            response = self.docdb.describe_db_clusters(DBClusterIdentifier=cluster_id)
            status = response["DBClusters"][0]["Status"]
            if status == "available":
                return
            if status in {"failed", "deleted", "deleting"}:
                raise RuntimeError(
                    f"DocumentDB cluster {cluster_id} entered terminal state: {status}"
                )
            time.sleep(15)
        raise RuntimeError(f"DocumentDB cluster {cluster_id} did not become available in 20 min")

    def _create_instance(
        self,
        instance_id: str,
        cluster_id: str,
        config: ClusterConfig,
        tags: dict[str, str],
    ) -> str:
        """Create one instance attached to the cluster. Returns instance ARN."""
        try:
            response = self.docdb.create_db_instance(
                DBInstanceIdentifier=instance_id,
                DBInstanceClass=config.instance_class,
                Engine="docdb",
                DBClusterIdentifier=cluster_id,
                Tags=[{"Key": k, "Value": v} for k, v in tags.items()],
                AutoMinorVersionUpgrade=False,
            )
            return str(response["DBInstance"]["DBInstanceArn"])
        except self.docdb.exceptions.DBInstanceAlreadyExistsFault:
            logger.warning("documentdb_instance_already_exists", instance_id=instance_id)
            existing = self.docdb.describe_db_instances(DBInstanceIdentifier=instance_id)
            return str(existing["DBInstances"][0]["DBInstanceArn"])

    def _create_replicas_parallel(
        self,
        run_id: str,
        cluster_id: str,
        config: ClusterConfig,
        tags: dict[str, str],
    ) -> list[DeployedResource]:
        """Create read replicas in parallel via ThreadPoolExecutor."""
        replica_resources: list[DeployedResource] = []
        max_workers = max(1, min(config.replica_count, 5))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    self._create_instance,
                    self._instance_identifier(run_id, i),
                    cluster_id,
                    config,
                    tags,
                ): i
                for i in range(1, config.replica_count + 1)
            }
            for future in concurrent.futures.as_completed(futures):
                index = futures[future]
                instance_id = self._instance_identifier(run_id, index)
                instance_arn = future.result()
                replica_resources.append(
                    DeployedResource(
                        resource_type="AWS::DocDB::DBInstance",
                        resource_arn=instance_arn,
                        configuration={
                            "instance_identifier": instance_id,
                            "instance_class": config.instance_class,
                            "instance_role": "replica",
                        },
                    )
                )
        return replica_resources

    def _wait_instances_available(self, instances: list[DeployedResource]) -> None:
        """Wait for all instances to reach available state."""
        for instance in instances:
            instance_id = instance.configuration["instance_identifier"]
            logger.info("waiting_documentdb_instance_available", instance_id=instance_id)
            try:
                waiter = self.docdb.get_waiter("db_instance_available")
                waiter.wait(
                    DBInstanceIdentifier=instance_id,
                    WaiterConfig={"Delay": 30, "MaxAttempts": 30},
                )
            except Exception as exc:
                raise RuntimeError(
                    f"DocumentDB instance {instance_id} did not become available"
                ) from exc

    def _get_task_role_arn(self) -> str:
        """Resolve the IAM role ARN this provisioner is running under.

        Checks ``LOAD_TEST_TASK_ROLE_ARN`` env var first (override), then falls
        back to STS GetCallerIdentity. The returned ARN is registered as a
        DocumentDB user via MONGODB-AWS auth so the seeder + runner can connect
        without a password.
        """
        override = os.environ.get(ENV_TASK_ROLE_ARN)
        if override:
            return override

        identity = self.sts.get_caller_identity()
        # ECS task role ARNs come back as "assumed-role/<role-name>/<session-name>".
        # Normalize to the underlying IAM role ARN: arn:aws:iam::<acct>:role/<role-name>.
        caller_arn = identity["Arn"]
        if ":assumed-role/" in caller_arn:
            account_id = identity["Account"]
            role_name = caller_arn.split("/")[1]
            return f"arn:aws:iam::{account_id}:role/{role_name}"
        return str(caller_arn)

    def _bootstrap_iam_user(self, cluster_endpoint: str, master_password: str) -> None:
        """Connect as primary and register the task role ARN as a DocumentDB user.

        Per AWS docs (https://docs.aws.amazon.com/documentdb/latest/devguide/iam-identity-auth.html),
        IAM users/roles are registered in the ``$external`` database with the
        MONGODB-AWS mechanism. After this, the seeder and runner connect using
        the task role's auto-resolved credentials — no password in env vars.
        """
        # Lazy import: pymongo not always available in test environments.
        # Imported here so unit tests can mock it without conditional skips.
        from pymongo import MongoClient  # noqa: PLC0415

        role_arn = self._get_task_role_arn()
        logger.info("bootstrapping_documentdb_iam_user", role_arn=role_arn)

        backoff = BOOTSTRAP_INITIAL_BACKOFF_SECONDS
        last_error: Exception | None = None
        for attempt in range(1, BOOTSTRAP_MAX_ATTEMPTS + 1):
            try:
                client: MongoClient[Any] = MongoClient(
                    host=cluster_endpoint,
                    port=27017,
                    username=MASTER_USERNAME,
                    password=master_password,
                    tls=True,
                    tlsCAFile=DOCDB_CA_BUNDLE_PATH,
                    retryWrites=False,
                    serverSelectionTimeoutMS=10_000,
                )
                external_db = client["$external"]
                external_db.command(
                    "createUser",
                    role_arn,
                    mechanisms=["MONGODB-AWS"],
                    roles=[{"role": "readWrite", "db": "loadtest"}],
                )
                client.close()
                return
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "documentdb_iam_bootstrap_attempt_failed",
                    attempt=attempt,
                    max_attempts=BOOTSTRAP_MAX_ATTEMPTS,
                    error=str(exc),
                )
                if attempt < BOOTSTRAP_MAX_ATTEMPTS:
                    time.sleep(backoff)
                    backoff = int(backoff * BOOTSTRAP_BACKOFF_MULTIPLIER)

        raise RuntimeError(
            f"IAM bootstrap failed after {BOOTSTRAP_MAX_ATTEMPTS} attempts: {last_error}"
        )

    def _require_env(self, name: str) -> str:
        value = os.environ.get(name)
        if not value:
            raise RuntimeError(
                f"Required environment variable {name} is not set. "
                f"DocumentDB provisioner expects pre-existing VPC infrastructure."
            )
        return value
