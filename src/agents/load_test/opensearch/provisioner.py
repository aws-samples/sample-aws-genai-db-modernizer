"""OpenSearch Service provisioner for load testing.

Creates an OpenSearch domain with the instance type identified by the
schema design cost estimate. Uses fine-grained access control (FGAC)
with a master user stored in Secrets Manager.
"""

import time

import boto3
import structlog

from src.agents.load_test.base import BaseProvisioner
from src.contracts.load_test_models import DeployedResource, InfrastructureManifest

logger = structlog.get_logger()

DEFAULT_INSTANCE_TYPE = "r6g.large.search"
DEFAULT_ENGINE_VERSION = "OpenSearch_2.17"
DOMAIN_PREFIX = "loadtest-mod"


class OpenSearchProvisioner(BaseProvisioner):
    """Provisions and tears down an OpenSearch Service domain."""

    def __init__(self, region: str = "us-east-1"):
        self.region = region
        self.client = boto3.client("opensearch", region_name=region)

    def provision(self, schema_output: dict, tags: dict[str, str]) -> InfrastructureManifest:
        """Create an OpenSearch domain sized from the schema design."""
        instance_type = self._resolve_instance_type(schema_output)
        instance_count = self._resolve_instance_count(schema_output)
        domain_name = self._build_domain_name(tags)
        master_password = self._generate_master_password()

        logger.info(
            "creating_opensearch_domain",
            domain_name=domain_name,
            instance_type=instance_type,
            instance_count=instance_count,
        )

        try:
            self.client.create_domain(
                DomainName=domain_name,
                EngineVersion=DEFAULT_ENGINE_VERSION,
                ClusterConfig={
                    "InstanceType": instance_type,
                    "InstanceCount": instance_count,
                    "DedicatedMasterEnabled": False,
                    "ZoneAwarenessEnabled": instance_count > 1,
                    "WarmEnabled": False,
                },
                EBSOptions={
                    "EBSEnabled": True,
                    "VolumeType": "gp3",
                    "VolumeSize": self._resolve_ebs_size(schema_output),
                    "Iops": 3000,
                    "Throughput": 125,
                },
                AccessPolicies="",
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
        """Delete the OpenSearch domain."""
        for resource in manifest.resources:
            if resource.resource_type == "AWS::OpenSearchService::Domain":
                domain_name = resource.configuration["domain_name"]
                logger.info("deleting_opensearch_domain", domain_name=domain_name)
                try:
                    self.client.delete_domain(DomainName=domain_name)
                except self.client.exceptions.ResourceNotFoundException:
                    logger.warning("domain_not_found_during_teardown", domain_name=domain_name)

    def _resolve_instance_type(self, schema_output: dict) -> str:
        """Extract instance type from the cost estimate, or use default."""
        cost_components = schema_output.get("cost_estimate", {}).get("cost_components", {})
        instance_type = cost_components.get("instance_type")
        if isinstance(instance_type, str) and ".search" in instance_type:
            return instance_type
        return DEFAULT_INSTANCE_TYPE

    def _resolve_instance_count(self, schema_output: dict) -> int:
        """Determine data node count from index settings or default to 2."""
        index_designs = schema_output.get("index_designs", [])
        if index_designs:
            settings = index_designs[0].get("settings", {})
            node_count = int(settings.get("assumed_node_count", 2))
            return max(2, min(node_count, 3))
        return 2

    def _resolve_ebs_size(self, schema_output: dict) -> int:
        """EBS volume size in GB. Estimate from shard count * 50GB target shard size."""
        index_designs = schema_output.get("index_designs", [])
        data_streams = schema_output.get("data_stream_designs", [])
        total_shards = 0
        for idx in index_designs:
            settings = idx.get("settings", {})
            total_shards += settings.get("number_of_shards", 1)
        for ds in data_streams:
            template = ds.get("index_template", {})
            settings = template.get("settings", {})
            total_shards += settings.get("number_of_shards", 1)
        # 20GB per shard minimum, cap at 100GB for load test
        size = max(20, total_shards * 20)
        return min(size, 100)

    def _build_domain_name(self, tags: dict[str, str]) -> str:
        """Build a unique domain name from tags. Max 28 chars, lowercase alphanumeric + hyphens."""
        job_id = tags.get("job_id", "unknown")[:8]
        run_id = tags.get("run_id", "unknown")[:6]
        domain_name = f"{DOMAIN_PREFIX}-{job_id}-{run_id}"
        return domain_name[:28].lower()

    def _generate_master_password(self) -> str:
        """Generate a secure password for the master user."""
        import secrets
        import string

        alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
        password = "".join(secrets.choice(alphabet) for _ in range(24))
        # Ensure at least one of each required type
        password = "A" + "a" + "1" + "!" + password[4:]
        return password

    def _wait_for_active(self, domain_name: str, timeout_minutes: int = 25) -> None:
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
