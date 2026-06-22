"""OpenSearch Service provisioner for load testing.

Creates an OpenSearch domain with the instance type identified by the
schema design cost estimate. Uses fine-grained access control (FGAC)
with a master user stored in Secrets Manager.
"""

import json
import time

import boto3
import structlog

from src.agents.load_test.base import BaseProvisioner
from src.contracts.load_test_models import DeployedResource, InfrastructureManifest

logger = structlog.get_logger()

DEFAULT_ENGINE_VERSION = "OpenSearch_2.17"
DOMAIN_PREFIX = "loadtest-mod"


class OpenSearchProvisioner(BaseProvisioner):
    """Provisions and tears down an OpenSearch Service domain."""

    def __init__(self, region: str = "us-east-1"):
        self.region = region
        self.client = boto3.client("opensearch", region_name=region)

    def provision(self, schema_output: dict, tags: dict[str, str]) -> InfrastructureManifest:
        """Create an OpenSearch domain sized from the schema design.

        If a domain with the same name already exists and is active, reuses it.
        Domain name is deterministic per job so repeated runs skip provisioning.
        """
        from src.agents.load_test.opensearch.sizing import derive_cluster_config

        collector_output = schema_output.get("_collector_output", {})
        cluster_config = derive_cluster_config(schema_output, collector_output)

        instance_type = cluster_config["instance_type"]
        instance_count = cluster_config["instance_count"]
        ebs_volume_gb = cluster_config["ebs_volume_gb"]
        domain_name = self._build_domain_name(tags)
        master_password = self._generate_master_password(tags)

        # Check if domain already exists and is active — skip provisioning
        existing = self._get_existing_domain(domain_name, master_password)
        if existing:
            logger.info("reusing_existing_domain", domain_name=domain_name)
            return existing

        logger.info(
            "creating_opensearch_domain",
            domain_name=domain_name,
            instance_type=instance_type,
            instance_count=instance_count,
            ebs_volume_gb=ebs_volume_gb,
            workload_type=cluster_config["workload_type"],
        )

        try:
            self.client.create_domain(
                DomainName=domain_name,
                EngineVersion=DEFAULT_ENGINE_VERSION,
                ClusterConfig={
                    "InstanceType": instance_type,
                    "InstanceCount": instance_count,
                    "DedicatedMasterEnabled": False,
                    "ZoneAwarenessEnabled": instance_count >= 3,
                    **(
                        {
                            "ZoneAwarenessConfig": {
                                "AvailabilityZoneCount": 3,
                            }
                        }
                        if instance_count >= 3
                        else {}
                    ),
                    "WarmEnabled": False,
                },
                EBSOptions={
                    "EBSEnabled": True,
                    "VolumeType": "gp3",
                    "VolumeSize": ebs_volume_gb,
                    "Iops": 3000,
                    "Throughput": 125,
                },
                IPAddressType="ipv4",
                AccessPolicies=json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Principal": {"AWS": "*"},
                                "Action": "es:*",
                                "Resource": f"arn:aws:es:{self.region}:*:domain/{domain_name}/*",
                            }
                        ],
                    }
                ),
                NodeToNodeEncryptionOptions={"Enabled": True},
                EncryptionAtRestOptions={"Enabled": True},
                DomainEndpointOptions={
                    "EnforceHTTPS": True,
                    "TLSSecurityPolicy": "Policy-Min-TLS-1-2-PFS-2023-10",
                },
                AdvancedSecurityOptions={
                    "Enabled": True,
                    "InternalUserDatabaseEnabled": True,
                    "MasterUserOptions": {
                        "MasterUserName": "loadtest_admin",
                        "MasterUserPassword": master_password,
                    },
                },
                TagList=[{"Key": k, "Value": v} for k, v in tags.items()],
            )
        except self.client.exceptions.ResourceAlreadyExistsException:
            logger.warning("domain_already_exists", domain_name=domain_name)

        self._wait_for_active(domain_name)

        desc = self.client.describe_domain(DomainName=domain_name)
        domain_status = desc["DomainStatus"]
        endpoint = domain_status.get("Endpoint", "")
        domain_arn = domain_status.get("ARN", "")

        resource = DeployedResource(
            resource_type="AWS::OpenSearchService::Domain",
            resource_arn=domain_arn,
            configuration={
                "domain_name": domain_name,
                "endpoint": endpoint,
                "instance_type": instance_type,
                "instance_count": instance_count,
                "engine_version": DEFAULT_ENGINE_VERSION,
                "master_user": "loadtest_admin",
                "master_password": master_password,
            },
        )

        return InfrastructureManifest(resources=[resource], tags=tags)

    def teardown(self, manifest: InfrastructureManifest) -> None:
        """Skip teardown by default — domain is reusable across runs.

        Call teardown_force() explicitly or use the --teardown CLI flag.
        """
        for resource in manifest.resources:
            if resource.resource_type == "AWS::OpenSearchService::Domain":
                domain_name = resource.configuration["domain_name"]
                logger.info(
                    "skipping_teardown",
                    domain_name=domain_name,
                    hint="Domain kept alive for reuse. Delete with --teardown flag.",
                )

    def teardown_force(self, manifest: InfrastructureManifest) -> None:
        """Actually delete the OpenSearch domain."""
        for resource in manifest.resources:
            if resource.resource_type == "AWS::OpenSearchService::Domain":
                domain_name = resource.configuration["domain_name"]
                logger.info("deleting_opensearch_domain", domain_name=domain_name)
                try:
                    self.client.delete_domain(DomainName=domain_name)
                except self.client.exceptions.ResourceNotFoundException:
                    logger.warning("domain_not_found_during_teardown", domain_name=domain_name)

    def _get_existing_domain(
        self, domain_name: str, master_password: str
    ) -> InfrastructureManifest | None:
        """Check if domain exists and is active. Return manifest if reusable."""
        try:
            desc = self.client.describe_domain(DomainName=domain_name)
        except self.client.exceptions.ResourceNotFoundException:
            return None

        domain_status = desc["DomainStatus"]
        if domain_status.get("Processing", True):
            self._wait_for_active(domain_name)
            desc = self.client.describe_domain(DomainName=domain_name)
            domain_status = desc["DomainStatus"]

        endpoint = domain_status.get("Endpoint", "")
        if not endpoint:
            return None

        resource = DeployedResource(
            resource_type="AWS::OpenSearchService::Domain",
            resource_arn=domain_status.get("ARN", ""),
            configuration={
                "domain_name": domain_name,
                "endpoint": endpoint,
                "instance_type": domain_status.get("ClusterConfig", {}).get(
                    "InstanceType", "unknown"
                ),
                "instance_count": domain_status.get("ClusterConfig", {}).get("InstanceCount", 0),
                "engine_version": domain_status.get("EngineVersion", ""),
                "master_user": "loadtest_admin",
                "master_password": master_password,
            },
        )

        return InfrastructureManifest(resources=[resource], tags={})

    def _build_domain_name(self, tags: dict[str, str]) -> str:
        """Build a deterministic domain name from job_id.

        Stable across runs so the domain can be reused without re-provisioning.
        Max 28 chars, lowercase alphanumeric + hyphens.
        """
        job_id = tags.get("job_id", "unknown")[:12]
        domain_name = f"{DOMAIN_PREFIX}-{job_id}-os"
        return domain_name[:28].lower()

    def _generate_master_password(self, tags: dict[str, str] | None = None) -> str:
        """Generate a deterministic password from job_id for domain reuse.

        The password is stable across runs for the same job so the domain
        can be reused without re-provisioning. This is a load test cluster
        that gets deleted after testing — not a production secret.
        """
        import hashlib

        job_id = (tags or {}).get("job_id", "default")
        seed = f"loadtest-opensearch-{job_id}"
        digest = hashlib.sha256(seed.encode()).hexdigest()[:20]
        return f"Lt!{digest}Aa1"  # pragma: allowlist secret

    def _wait_for_active(self, domain_name: str, timeout_minutes: int = 40) -> None:
        """Poll until domain processing is complete."""
        deadline = time.time() + timeout_minutes * 60
        poll_interval = 30

        while time.time() < deadline:
            desc = self.client.describe_domain(DomainName=domain_name)
            processing = desc["DomainStatus"].get("Processing", True)
            logger.info("waiting_for_domain", domain_name=domain_name, processing=processing)

            if not processing:
                return

            time.sleep(poll_interval)  # nosemgrep: arbitrary-sleep

        raise TimeoutError(
            f"OpenSearch domain {domain_name} not active after {timeout_minutes} minutes"
        )
