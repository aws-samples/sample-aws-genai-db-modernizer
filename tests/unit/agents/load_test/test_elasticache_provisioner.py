"""Tests for ElastiCache (Valkey) provisioner.

Mocks boto3 (elasticache, sts) and verifies:
  - Happy path: provision creates replication group and returns manifest
  - Node type is resolved from cost_estimate.cost_components.instance_type
  - Default node type used when schema has no cost_estimate
  - Replication group ID is built from job_id + run_id tags (≤40 chars, lowercase)
  - Always provisions 1 shard (num_node_groups=1)
  - ConfigurationEndpoint is preferred over primary NodeGroup endpoint
  - Falls back to NodeGroup PrimaryEndpoint when ConfigurationEndpoint is absent
  - ARN is constructed from region + account_id + replication_group_id
  - Idempotent provision: ReplicationGroupAlreadyExistsFault is swallowed
  - Teardown deletes replication group
  - Teardown is idempotent: ReplicationGroupNotFoundFault is swallowed
  - Tags are forwarded to the AWS API call
"""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.agents.load_test.elasticache.provisioner import (
    DEFAULT_ENGINE_VERSION,
    DEFAULT_NODE_TYPE,
    REPLICATION_GROUP_PREFIX,
    ElastiCacheProvisioner,
)
from src.contracts.load_test_models import DeployedResource, InfrastructureManifest

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def tags() -> dict[str, str]:
    return {"job_id": "job_001", "run_id": "abc123de", "database_name": "mydb"}


@pytest.fixture
def schema_output_with_node_type() -> dict[str, Any]:
    return {
        "cost_estimate": {
            "cost_components": {
                "instance_type": "cache.r7g.xlarge",
            }
        }
    }


@pytest.fixture
def schema_output_no_cost_estimate() -> dict[str, Any]:
    return {"key_designs": []}


@pytest.fixture
def mock_elasticache() -> MagicMock:
    """Mock boto3 elasticache client with happy-path responses."""
    client = MagicMock()

    client.create_replication_group.return_value = {
        "ReplicationGroup": {
            "ReplicationGroupId": "loadtest-modernizer-job_001-abc123de",
            "Status": "creating",
        }
    }

    # describe_replication_groups: first call during _wait_for_available, second for endpoint
    available_rg = {
        "ReplicationGroups": [
            {
                "Status": "available",
                "ConfigurationEndpoint": {
                    "Address": "loadtest-modernizer-job_001-abc123de.cfg.use1.cache.amazonaws.com",
                    "Port": 6379,
                },
                "NodeGroups": [],
            }
        ]
    }
    client.describe_replication_groups.return_value = available_rg

    client.exceptions.ReplicationGroupAlreadyExistsFault = type(
        "ReplicationGroupAlreadyExistsFault", (Exception,), {}
    )
    client.exceptions.ReplicationGroupNotFoundFault = type(
        "ReplicationGroupNotFoundFault", (Exception,), {}
    )

    waiter = MagicMock()
    waiter.wait.return_value = None
    client.get_waiter.return_value = waiter

    return client


@pytest.fixture
def mock_sts() -> MagicMock:
    client = MagicMock()
    client.get_caller_identity.return_value = {"Account": "123456789012"}
    return client


@pytest.fixture
def provisioner_with_mocks(
    mock_elasticache: MagicMock, mock_sts: MagicMock
) -> ElastiCacheProvisioner:
    """Return a provisioner whose AWS clients are fully mocked."""
    with patch("boto3.client") as mock_boto_client:
        mock_boto_client.side_effect = lambda service, **_: (
            mock_elasticache if service == "elasticache" else mock_sts
        )
        p = ElastiCacheProvisioner(region="us-east-1")
    p.client = mock_elasticache
    return p


# =============================================================================
# Node type resolution
# =============================================================================


class TestResolveNodeType:
    def test_uses_instance_type_from_cost_estimate(
        self,
        provisioner_with_mocks: ElastiCacheProvisioner,
        schema_output_with_node_type: dict[str, Any],
    ) -> None:
        result = provisioner_with_mocks._resolve_node_type(schema_output_with_node_type)
        assert result == "cache.r7g.xlarge"

    def test_falls_back_to_default_when_no_cost_estimate(
        self,
        provisioner_with_mocks: ElastiCacheProvisioner,
        schema_output_no_cost_estimate: dict[str, Any],
    ) -> None:
        result = provisioner_with_mocks._resolve_node_type(schema_output_no_cost_estimate)
        assert result == DEFAULT_NODE_TYPE

    def test_falls_back_to_default_when_instance_type_not_string(
        self,
        provisioner_with_mocks: ElastiCacheProvisioner,
    ) -> None:
        schema = {"cost_estimate": {"cost_components": {"instance_type": 42}}}
        result = provisioner_with_mocks._resolve_node_type(schema)
        assert result == DEFAULT_NODE_TYPE


# =============================================================================
# Shard count
# =============================================================================


class TestResolveNumNodeGroups:
    def test_always_returns_one(
        self,
        provisioner_with_mocks: ElastiCacheProvisioner,
    ) -> None:
        """Always 1 shard to avoid MOVED redirects with xk6-redis."""
        assert provisioner_with_mocks._resolve_num_node_groups({}) == 1
        assert provisioner_with_mocks._resolve_num_node_groups({"shards": 5}) == 1


# =============================================================================
# Replication group ID building
# =============================================================================


class TestBuildReplicationGroupId:
    def test_id_starts_with_prefix(
        self,
        provisioner_with_mocks: ElastiCacheProvisioner,
        tags: dict[str, str],
    ) -> None:
        rg_id = provisioner_with_mocks._build_replication_group_id(tags)
        assert rg_id.startswith(REPLICATION_GROUP_PREFIX)

    def test_id_includes_job_and_run(
        self,
        provisioner_with_mocks: ElastiCacheProvisioner,
        tags: dict[str, str],
    ) -> None:
        rg_id = provisioner_with_mocks._build_replication_group_id(tags)
        # job_id[:8] and run_id[:8] are embedded
        assert "job_001" in rg_id
        assert "abc123de" in rg_id

    def test_id_does_not_exceed_40_chars(
        self,
        provisioner_with_mocks: ElastiCacheProvisioner,
    ) -> None:
        long_tags = {"job_id": "x" * 64, "run_id": "y" * 64}
        rg_id = provisioner_with_mocks._build_replication_group_id(long_tags)
        assert len(rg_id) <= 40

    def test_id_uses_only_first_8_chars_of_job_and_run(
        self,
        provisioner_with_mocks: ElastiCacheProvisioner,
    ) -> None:
        tags = {"job_id": "abcdefgh1234", "run_id": "runXXXXY999"}
        rg_id = provisioner_with_mocks._build_replication_group_id(tags)
        assert "abcdefgh" in rg_id
        assert "1234" not in rg_id
        assert "runXXXXY" in rg_id
        assert "999" not in rg_id


# =============================================================================
# Provision happy path
# =============================================================================


class TestProvision:
    def test_provision_calls_create_replication_group(
        self,
        provisioner_with_mocks: ElastiCacheProvisioner,
        mock_elasticache: MagicMock,
        mock_sts: MagicMock,
        schema_output_no_cost_estimate: dict[str, Any],
        tags: dict[str, str],
    ) -> None:
        with patch("boto3.client", return_value=mock_sts):
            provisioner_with_mocks.provision(schema_output_no_cost_estimate, tags)

        mock_elasticache.create_replication_group.assert_called_once()
        call_kwargs = mock_elasticache.create_replication_group.call_args.kwargs
        assert call_kwargs["Engine"] == "valkey"
        assert call_kwargs["NumNodeGroups"] == 1
        assert call_kwargs["ReplicasPerNodeGroup"] == 1
        assert call_kwargs["MultiAZEnabled"] is True
        assert call_kwargs["TransitEncryptionEnabled"] is True

    def test_provision_returns_single_replication_group_resource(
        self,
        provisioner_with_mocks: ElastiCacheProvisioner,
        mock_sts: MagicMock,
        schema_output_no_cost_estimate: dict[str, Any],
        tags: dict[str, str],
    ) -> None:
        with patch("boto3.client", return_value=mock_sts):
            manifest = provisioner_with_mocks.provision(schema_output_no_cost_estimate, tags)

        assert len(manifest.resources) == 1
        resource = manifest.resources[0]
        assert resource.resource_type == "AWS::ElastiCache::ReplicationGroup"

    def test_provision_manifest_carries_endpoint_from_configuration_endpoint(
        self,
        provisioner_with_mocks: ElastiCacheProvisioner,
        mock_sts: MagicMock,
        schema_output_no_cost_estimate: dict[str, Any],
        tags: dict[str, str],
    ) -> None:
        with patch("boto3.client", return_value=mock_sts):
            manifest = provisioner_with_mocks.provision(schema_output_no_cost_estimate, tags)

        config = manifest.resources[0].configuration
        assert "loadtest-modernizer" in config["endpoint_address"]
        assert config["endpoint_port"] == 6379

    def test_provision_falls_back_to_node_group_primary_endpoint(
        self,
        provisioner_with_mocks: ElastiCacheProvisioner,
        mock_elasticache: MagicMock,
        mock_sts: MagicMock,
        schema_output_no_cost_estimate: dict[str, Any],
        tags: dict[str, str],
    ) -> None:
        """When ConfigurationEndpoint is absent, use NodeGroups[0].PrimaryEndpoint."""
        mock_elasticache.describe_replication_groups.return_value = {
            "ReplicationGroups": [
                {
                    "Status": "available",
                    "ConfigurationEndpoint": {},
                    "NodeGroups": [
                        {
                            "PrimaryEndpoint": {
                                "Address": "primary.cache.example.com",
                                "Port": 6380,
                            }
                        }
                    ],
                }
            ]
        }

        with patch("boto3.client", return_value=mock_sts):
            manifest = provisioner_with_mocks.provision(schema_output_no_cost_estimate, tags)

        config = manifest.resources[0].configuration
        assert config["endpoint_address"] == "primary.cache.example.com"
        assert config["endpoint_port"] == 6380

    def test_provision_uses_custom_node_type_from_schema(
        self,
        provisioner_with_mocks: ElastiCacheProvisioner,
        mock_elasticache: MagicMock,
        mock_sts: MagicMock,
        schema_output_with_node_type: dict[str, Any],
        tags: dict[str, str],
    ) -> None:
        with patch("boto3.client", return_value=mock_sts):
            provisioner_with_mocks.provision(schema_output_with_node_type, tags)

        call_kwargs = mock_elasticache.create_replication_group.call_args.kwargs
        assert call_kwargs["CacheNodeType"] == "cache.r7g.xlarge"

    def test_provision_forwards_tags(
        self,
        provisioner_with_mocks: ElastiCacheProvisioner,
        mock_elasticache: MagicMock,
        mock_sts: MagicMock,
        schema_output_no_cost_estimate: dict[str, Any],
        tags: dict[str, str],
    ) -> None:
        with patch("boto3.client", return_value=mock_sts):
            provisioner_with_mocks.provision(schema_output_no_cost_estimate, tags)

        call_kwargs = mock_elasticache.create_replication_group.call_args.kwargs
        tag_keys = {t["Key"] for t in call_kwargs["Tags"]}
        assert "job_id" in tag_keys
        assert "run_id" in tag_keys

    def test_provision_arn_contains_region_and_account(
        self,
        provisioner_with_mocks: ElastiCacheProvisioner,
        mock_sts: MagicMock,
        schema_output_no_cost_estimate: dict[str, Any],
        tags: dict[str, str],
    ) -> None:
        with patch("boto3.client", return_value=mock_sts):
            manifest = provisioner_with_mocks.provision(schema_output_no_cost_estimate, tags)

        arn = manifest.resources[0].resource_arn
        assert "us-east-1" in arn
        assert "123456789012" in arn
        assert "replicationgroup" in arn

    def test_provision_already_exists_is_idempotent(
        self,
        provisioner_with_mocks: ElastiCacheProvisioner,
        mock_elasticache: MagicMock,
        mock_sts: MagicMock,
        schema_output_no_cost_estimate: dict[str, Any],
        tags: dict[str, str],
    ) -> None:
        """ReplicationGroupAlreadyExistsFault must not propagate."""
        mock_elasticache.create_replication_group.side_effect = (
            mock_elasticache.exceptions.ReplicationGroupAlreadyExistsFault()
        )

        with patch("boto3.client", return_value=mock_sts):
            # Should not raise
            manifest = provisioner_with_mocks.provision(schema_output_no_cost_estimate, tags)

        assert len(manifest.resources) == 1

    def test_provision_configuration_carries_engine_version(
        self,
        provisioner_with_mocks: ElastiCacheProvisioner,
        mock_sts: MagicMock,
        schema_output_no_cost_estimate: dict[str, Any],
        tags: dict[str, str],
    ) -> None:
        with patch("boto3.client", return_value=mock_sts):
            manifest = provisioner_with_mocks.provision(schema_output_no_cost_estimate, tags)

        config = manifest.resources[0].configuration
        assert config["engine"] == "valkey"
        assert config["engine_version"] == DEFAULT_ENGINE_VERSION


# =============================================================================
# Teardown
# =============================================================================


class TestTeardown:
    def _make_manifest(self, rg_id: str = "loadtest-modernizer-job001-run001") -> InfrastructureManifest:
        return InfrastructureManifest(
            resources=[
                DeployedResource(
                    resource_type="AWS::ElastiCache::ReplicationGroup",
                    resource_arn=f"arn:aws:elasticache:us-east-1:123:replicationgroup:{rg_id}",
                    configuration={
                        "replication_group_id": rg_id,
                        "endpoint_address": "example.cache.amazonaws.com",
                        "endpoint_port": 6379,
                    },
                )
            ],
            tags={"job_id": "job001"},
        )

    def test_teardown_calls_delete_replication_group(
        self,
        provisioner_with_mocks: ElastiCacheProvisioner,
        mock_elasticache: MagicMock,
    ) -> None:
        manifest = self._make_manifest()
        provisioner_with_mocks.teardown(manifest)
        mock_elasticache.delete_replication_group.assert_called_once_with(
            ReplicationGroupId="loadtest-modernizer-job001-run001",
            RetainPrimaryCluster=False,
        )

    def test_teardown_is_idempotent_when_not_found(
        self,
        provisioner_with_mocks: ElastiCacheProvisioner,
        mock_elasticache: MagicMock,
    ) -> None:
        """ReplicationGroupNotFoundFault must not propagate."""
        mock_elasticache.delete_replication_group.side_effect = (
            mock_elasticache.exceptions.ReplicationGroupNotFoundFault()
        )

        manifest = self._make_manifest()
        # Should not raise
        provisioner_with_mocks.teardown(manifest)

    def test_teardown_skips_non_replication_group_resources(
        self,
        provisioner_with_mocks: ElastiCacheProvisioner,
        mock_elasticache: MagicMock,
    ) -> None:
        """Resources with other types should be silently ignored."""
        manifest = InfrastructureManifest(
            resources=[
                DeployedResource(
                    resource_type="AWS::SomeOtherService::Resource",
                    resource_arn="arn:aws:other:us-east-1:123:resource/r1",
                    configuration={},
                )
            ],
            tags={},
        )
        provisioner_with_mocks.teardown(manifest)
        mock_elasticache.delete_replication_group.assert_not_called()

    def test_teardown_deletes_all_replication_groups(
        self,
        provisioner_with_mocks: ElastiCacheProvisioner,
        mock_elasticache: MagicMock,
    ) -> None:
        """If manifest has multiple groups (unusual but possible), all are deleted."""
        manifest = InfrastructureManifest(
            resources=[
                DeployedResource(
                    resource_type="AWS::ElastiCache::ReplicationGroup",
                    resource_arn="arn1",
                    configuration={"replication_group_id": "rg-1"},
                ),
                DeployedResource(
                    resource_type="AWS::ElastiCache::ReplicationGroup",
                    resource_arn="arn2",
                    configuration={"replication_group_id": "rg-2"},
                ),
            ],
            tags={},
        )
        provisioner_with_mocks.teardown(manifest)
        assert mock_elasticache.delete_replication_group.call_count == 2
