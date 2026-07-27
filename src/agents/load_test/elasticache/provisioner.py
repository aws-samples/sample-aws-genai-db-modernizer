"""ElastiCache (Valkey) provisioner for load testing.

Creates a Valkey replication group with cluster mode enabled,
using the instance type identified by the ElastiCache analysis.
"""

import time

import boto3
import structlog

from src.agents.load_test.base import BaseProvisioner
from src.contracts.load_test_models import DeployedResource, InfrastructureManifest

logger = structlog.get_logger()

DEFAULT_NODE_TYPE = "cache.r7g.large"
DEFAULT_ENGINE_VERSION = "9.0"
REPLICATION_GROUP_PREFIX = "loadtest-modernizer"

class ElastiCacheProvisioner(BaseProvisioner):
    """Provisions and tears down an ElastiCache Valkey replication group."""

    def __init__(self, region: str = "us-east-1"):
        self.region = region
        self.client = boto3.client("elasticache", region_name=region)

    def provision(self, schema_output: dict, tags: dict[str, str]) -> InfrastructureManifest:
        """Create a Valkey replication group sized from the analysis cost estimate."""
        node_type = self._resolve_node_type(schema_output)
        num_node_groups = self._resolve_num_node_groups(schema_output)
        replication_group_id = self._build_replication_group_id(tags)

        logger.info(
            "creating_replication_group",
            replication_group_id=replication_group_id,
            node_type=node_type,
            num_node_groups=num_node_groups,
        )

        try:
            response = self.client.create_replication_group(
                ReplicationGroupId=replication_group_id,
                ReplicationGroupDescription="Database Modernizer load test Valkey cluster",
                Engine="valkey",
                EngineVersion=DEFAULT_ENGINE_VERSION,
                CacheNodeType=node_type,
                NumNodeGroups=num_node_groups,
                ReplicasPerNodeGroup=1,
                MultiAZEnabled=True,
                AutomaticFailoverEnabled=True,
                TransitEncryptionEnabled=True,
                Tags=[{"Key": k, "Value": v} for k, v in tags.items()],
            )
            status = response["ReplicationGroup"]["Status"]
            logger.info("replication_group_creating", status=status)
        except self.client.exceptions.ReplicationGroupAlreadyExistsFault:
            logger.warning(
                "replication_group_already_exists",
                replication_group_id=replication_group_id,
            )

        # Wait for the replication group to become available
        self._wait_for_available(replication_group_id)

        # Get the endpoint — ConfigurationEndpoint for cluster mode,
        # or PrimaryEndpoint from first NodeGroup for single-shard mode
        desc = self.client.describe_replication_groups(ReplicationGroupId=replication_group_id)
        rg = desc["ReplicationGroups"][0]
        config_endpoint = rg.get("ConfigurationEndpoint") or {}
        endpoint_address = config_endpoint.get("Address", "")
        endpoint_port = config_endpoint.get("Port", 6379)

        # Fallback: single-shard mode has no ConfigurationEndpoint
        if not endpoint_address:
            node_groups = rg.get("NodeGroups", [])
            if node_groups:
                primary_ep = node_groups[0].get("PrimaryEndpoint", {})
                endpoint_address = primary_ep.get("Address", "")
                endpoint_port = primary_ep.get("Port", 6379)

        # Build ARN
        sts = boto3.client("sts", region_name=self.region)
        account_id = sts.get_caller_identity()["Account"]
        arn = f"arn:aws:elasticache:{self.region}:{account_id}:replicationgroup:{replication_group_id}"

        resource = DeployedResource(
            resource_type="AWS::ElastiCache::ReplicationGroup",
            resource_arn=arn,
            configuration={
                "replication_group_id": replication_group_id,
                "endpoint_address": endpoint_address,
                "endpoint_port": endpoint_port,
                "node_type": node_type,
                "num_node_groups": num_node_groups,
                "engine": "valkey",
                "engine_version": DEFAULT_ENGINE_VERSION,
            },
        )

        return InfrastructureManifest(resources=[resource], tags=tags)

    def teardown(self, manifest: InfrastructureManifest) -> None:
        """Delete the replication group."""
        for resource in manifest.resources:
            if resource.resource_type == "AWS::ElastiCache::ReplicationGroup":
                rg_id = resource.configuration["replication_group_id"]
                logger.info("deleting_replication_group", replication_group_id=rg_id)
                try:
                    self.client.delete_replication_group(
                        ReplicationGroupId=rg_id,
                        RetainPrimaryCluster=False,
                    )
                except self.client.exceptions.ReplicationGroupNotFoundFault:
                    logger.warning(
                        "replication_group_not_found_during_teardown",
                        replication_group_id=rg_id,
                    )

    def _resolve_node_type(self, schema_output: dict) -> str:
        """Extract node type from the analysis cost estimate, or use default."""
        cost_components = schema_output.get("cost_estimate", {}).get("cost_components", {})
        instance_type = cost_components.get("instance_type")
        if isinstance(instance_type, str):
            return instance_type
        return DEFAULT_NODE_TYPE

    def _resolve_num_node_groups(self, schema_output: dict) -> int:
        """Number of shards for load testing.

        Use 1 shard (no cluster mode) to avoid MOVED redirects with xk6-redis.
        The load test measures latency characteristics, not horizontal scaling.
        Production sizing uses multiple shards based on memory/throughput needs.
        """
        return 1

    def _build_replication_group_id(self, tags: dict[str, str]) -> str:
        """Build a unique replication group ID from tags."""
        job_id = tags.get("job_id", "unknown")[:8]
        run_id = tags.get("run_id", "unknown")[:8]
        # ElastiCache IDs: 1-40 chars, lowercase alphanumeric + hyphens
        rg_id = f"{REPLICATION_GROUP_PREFIX}-{job_id}-{run_id}"
        return str(rg_id[:40])

    def _wait_for_available(self, replication_group_id: str, timeout_minutes: int = 15) -> None:
        """Poll until replication group is available."""
        deadline = time.time() + timeout_minutes * 60
        poll_interval = 30

        while time.time() < deadline:
            desc = self.client.describe_replication_groups(ReplicationGroupId=replication_group_id)
            status = desc["ReplicationGroups"][0]["Status"]
            logger.info("waiting_for_replication_group", status=status)

            if status == "available":
                return
            if status in ("create-failed", "deleting"):
                raise RuntimeError(
                    f"Replication group {replication_group_id} entered status: {status}"
                )

            time.sleep(poll_interval)  # nosemgrep: arbitrary-sleep

        raise TimeoutError(
            f"Replication group {replication_group_id} not available after {timeout_minutes} minutes"
        )
