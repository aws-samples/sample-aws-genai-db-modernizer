"""Tests for OpenSearch provisioner.

Mocks boto3 (opensearch) and verifies:
  - Happy path: provision creates domain and returns manifest
  - Instance type is resolved from cost_estimate.cost_components.instance_type
  - Default instance type used when schema has no cost_estimate
  - Domain name is built from job_id + run_id tags (<=28 chars, lowercase)
  - Instance count derived from index settings assumed_node_count
  - EBS size derived from shard count
  - Endpoint is read from describe_domain response
  - Idempotent provision: ResourceAlreadyExistsException is swallowed
  - Teardown deletes domain
  - Teardown is idempotent: ResourceNotFoundException is swallowed
"""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.agents.load_test.opensearch.provisioner import (
    DEFAULT_ENGINE_VERSION,
    DEFAULT_INSTANCE_TYPE,
    DOMAIN_PREFIX,
    OpenSearchProvisioner,
)
from src.contracts.load_test_models import DeployedResource, InfrastructureManifest


@pytest.fixture
def tags() -> dict[str, str]:
    return {"job_id": "job_001", "run_id": "abc123de", "database_name": "mydb"}


@pytest.fixture
def schema_output_with_instance_type() -> dict[str, Any]:
    return {
        "cost_estimate": {
            "cost_components": {
                "instance_type": "r6g.xlarge.search",
            }
        },
        "index_designs": [
            {
                "index_name": "products",
                "settings": {
                    "number_of_shards": 3,
                    "number_of_replicas": 1,
                    "assumed_node_count": 3,
                },
            }
        ],
    }


@pytest.fixture
def schema_output_minimal() -> dict[str, Any]:
    return {"index_designs": [], "data_stream_designs": []}


@pytest.fixture
def mock_opensearch_client() -> MagicMock:
    """Mock boto3 opensearch client with happy-path responses."""
    client = MagicMock()

    client.create_domain.return_value = {
        "DomainStatus": {"DomainName": "loadtest-mod-job_001-abc123", "Processing": True}
    }

    client.describe_domain.return_value = {
        "DomainStatus": {
            "DomainName": "loadtest-mod-job_001-abc123",
            "Processing": False,
            "Endpoint": "search-loadtest-mod-job001-abc123.us-east-1.es.amazonaws.com",
            "ARN": "arn:aws:es:us-east-1:123456789012:domain/loadtest-mod-job_001-abc123",
        }
    }

    client.exceptions.ResourceAlreadyExistsException = type(
        "ResourceAlreadyExistsException", (Exception,), {}
    )
    client.exceptions.ResourceNotFoundException = type(
        "ResourceNotFoundException", (Exception,), {}
    )

    return client


@pytest.fixture
def provisioner_with_mocks(mock_opensearch_client: MagicMock) -> OpenSearchProvisioner:
    """Return a provisioner whose AWS clients are fully mocked."""
    with patch("boto3.client", return_value=mock_opensearch_client):
        p = OpenSearchProvisioner(region="us-east-1")
    p.client = mock_opensearch_client
    return p


class TestResolveInstanceType:
    def test_uses_instance_type_from_cost_estimate(
        self,
        provisioner_with_mocks: OpenSearchProvisioner,
        schema_output_with_instance_type: dict[str, Any],
    ) -> None:
        result = provisioner_with_mocks._resolve_instance_type(schema_output_with_instance_type)
        assert result == "r6g.xlarge.search"

    def test_falls_back_to_default_when_no_cost_estimate(
        self,
        provisioner_with_mocks: OpenSearchProvisioner,
        schema_output_minimal: dict[str, Any],
    ) -> None:
        result = provisioner_with_mocks._resolve_instance_type(schema_output_minimal)
        assert result == DEFAULT_INSTANCE_TYPE

    def test_falls_back_to_default_when_instance_type_not_search(
        self,
        provisioner_with_mocks: OpenSearchProvisioner,
    ) -> None:
        schema = {"cost_estimate": {"cost_components": {"instance_type": "cache.r7g.large"}}}
        result = provisioner_with_mocks._resolve_instance_type(schema)
        assert result == DEFAULT_INSTANCE_TYPE


class TestResolveInstanceCount:
    def test_derives_from_assumed_node_count(
        self,
        provisioner_with_mocks: OpenSearchProvisioner,
        schema_output_with_instance_type: dict[str, Any],
    ) -> None:
        result = provisioner_with_mocks._resolve_instance_count(schema_output_with_instance_type)
        assert result == 3

    def test_minimum_is_two(
        self,
        provisioner_with_mocks: OpenSearchProvisioner,
    ) -> None:
        schema = {
            "index_designs": [{"settings": {"assumed_node_count": 1}}],
        }
        result = provisioner_with_mocks._resolve_instance_count(schema)
        assert result == 2

    def test_maximum_is_three(
        self,
        provisioner_with_mocks: OpenSearchProvisioner,
    ) -> None:
        schema = {
            "index_designs": [{"settings": {"assumed_node_count": 10}}],
        }
        result = provisioner_with_mocks._resolve_instance_count(schema)
        assert result == 3

    def test_defaults_to_two_when_no_index_designs(
        self,
        provisioner_with_mocks: OpenSearchProvisioner,
        schema_output_minimal: dict[str, Any],
    ) -> None:
        result = provisioner_with_mocks._resolve_instance_count(schema_output_minimal)
        assert result == 2


class TestResolveEbsSize:
    def test_computes_from_shard_count(
        self,
        provisioner_with_mocks: OpenSearchProvisioner,
    ) -> None:
        schema = {
            "index_designs": [
                {"settings": {"number_of_shards": 3}},
                {"settings": {"number_of_shards": 2}},
            ],
            "data_stream_designs": [],
        }
        # 5 shards * 20GB = 100GB
        result = provisioner_with_mocks._resolve_ebs_size(schema)
        assert result == 100

    def test_minimum_is_20(
        self,
        provisioner_with_mocks: OpenSearchProvisioner,
    ) -> None:
        schema: dict[str, Any] = {"index_designs": [], "data_stream_designs": []}
        result = provisioner_with_mocks._resolve_ebs_size(schema)
        assert result == 20

    def test_caps_at_100(
        self,
        provisioner_with_mocks: OpenSearchProvisioner,
    ) -> None:
        schema = {
            "index_designs": [{"settings": {"number_of_shards": 10}}],
            "data_stream_designs": [{"index_template": {"settings": {"number_of_shards": 5}}}],
        }
        # 15 shards * 20 = 300 -> capped at 100
        result = provisioner_with_mocks._resolve_ebs_size(schema)
        assert result == 100

    def test_includes_data_stream_shards(
        self,
        provisioner_with_mocks: OpenSearchProvisioner,
    ) -> None:
        schema = {
            "index_designs": [],
            "data_stream_designs": [{"index_template": {"settings": {"number_of_shards": 2}}}],
        }
        result = provisioner_with_mocks._resolve_ebs_size(schema)
        assert result == 40  # 2 * 20


class TestBuildDomainName:
    def test_starts_with_prefix(
        self,
        provisioner_with_mocks: OpenSearchProvisioner,
        tags: dict[str, str],
    ) -> None:
        name = provisioner_with_mocks._build_domain_name(tags)
        assert name.startswith(DOMAIN_PREFIX)

    def test_does_not_exceed_28_chars(
        self,
        provisioner_with_mocks: OpenSearchProvisioner,
    ) -> None:
        long_tags = {"job_id": "x" * 64, "run_id": "y" * 64}
        name = provisioner_with_mocks._build_domain_name(long_tags)
        assert len(name) <= 28

    def test_is_lowercase(
        self,
        provisioner_with_mocks: OpenSearchProvisioner,
        tags: dict[str, str],
    ) -> None:
        name = provisioner_with_mocks._build_domain_name(tags)
        assert name == name.lower()


class TestProvision:
    def test_provision_calls_create_domain(
        self,
        provisioner_with_mocks: OpenSearchProvisioner,
        mock_opensearch_client: MagicMock,
        schema_output_minimal: dict[str, Any],
        tags: dict[str, str],
    ) -> None:
        provisioner_with_mocks.provision(schema_output_minimal, tags)

        mock_opensearch_client.create_domain.assert_called_once()
        call_kwargs = mock_opensearch_client.create_domain.call_args.kwargs
        assert call_kwargs["EngineVersion"] == DEFAULT_ENGINE_VERSION
        assert call_kwargs["NodeToNodeEncryptionOptions"]["Enabled"] is True
        assert call_kwargs["EncryptionAtRestOptions"]["Enabled"] is True
        assert call_kwargs["DomainEndpointOptions"]["EnforceHTTPS"] is True
        assert call_kwargs["AdvancedSecurityOptions"]["Enabled"] is True

    def test_provision_returns_manifest_with_domain_resource(
        self,
        provisioner_with_mocks: OpenSearchProvisioner,
        schema_output_minimal: dict[str, Any],
        tags: dict[str, str],
    ) -> None:
        manifest = provisioner_with_mocks.provision(schema_output_minimal, tags)

        assert len(manifest.resources) == 1
        resource = manifest.resources[0]
        assert resource.resource_type == "AWS::OpenSearchService::Domain"

    def test_provision_carries_endpoint_in_configuration(
        self,
        provisioner_with_mocks: OpenSearchProvisioner,
        schema_output_minimal: dict[str, Any],
        tags: dict[str, str],
    ) -> None:
        manifest = provisioner_with_mocks.provision(schema_output_minimal, tags)

        config = manifest.resources[0].configuration
        assert "search-loadtest" in config["endpoint"]
        assert config["master_user"] == "loadtest_admin"
        assert len(config["master_password"]) >= 24

    def test_provision_already_exists_is_idempotent(
        self,
        provisioner_with_mocks: OpenSearchProvisioner,
        mock_opensearch_client: MagicMock,
        schema_output_minimal: dict[str, Any],
        tags: dict[str, str],
    ) -> None:
        mock_opensearch_client.create_domain.side_effect = (
            mock_opensearch_client.exceptions.ResourceAlreadyExistsException()
        )

        manifest = provisioner_with_mocks.provision(schema_output_minimal, tags)
        assert len(manifest.resources) == 1

    def test_provision_uses_custom_instance_type(
        self,
        provisioner_with_mocks: OpenSearchProvisioner,
        mock_opensearch_client: MagicMock,
        schema_output_with_instance_type: dict[str, Any],
        tags: dict[str, str],
    ) -> None:
        provisioner_with_mocks.provision(schema_output_with_instance_type, tags)

        call_kwargs = mock_opensearch_client.create_domain.call_args.kwargs
        assert call_kwargs["ClusterConfig"]["InstanceType"] == "r6g.xlarge.search"

    def test_provision_forwards_tags(
        self,
        provisioner_with_mocks: OpenSearchProvisioner,
        mock_opensearch_client: MagicMock,
        schema_output_minimal: dict[str, Any],
        tags: dict[str, str],
    ) -> None:
        provisioner_with_mocks.provision(schema_output_minimal, tags)

        call_kwargs = mock_opensearch_client.create_domain.call_args.kwargs
        tag_keys = {t["Key"] for t in call_kwargs["TagList"]}
        assert "job_id" in tag_keys
        assert "run_id" in tag_keys

    def test_provision_carries_arn(
        self,
        provisioner_with_mocks: OpenSearchProvisioner,
        schema_output_minimal: dict[str, Any],
        tags: dict[str, str],
    ) -> None:
        manifest = provisioner_with_mocks.provision(schema_output_minimal, tags)

        arn = manifest.resources[0].resource_arn
        assert "us-east-1" in arn
        assert "123456789012" in arn


class TestTeardown:
    def _make_manifest(
        self, domain_name: str = "loadtest-mod-job001-run01"
    ) -> InfrastructureManifest:
        return InfrastructureManifest(
            resources=[
                DeployedResource(
                    resource_type="AWS::OpenSearchService::Domain",
                    resource_arn=f"arn:aws:es:us-east-1:123:domain/{domain_name}",
                    configuration={
                        "domain_name": domain_name,
                        "endpoint": "search-example.us-east-1.es.amazonaws.com",
                        "master_user": "loadtest_admin",
                        "master_password": "test123",  # pragma: allowlist secret
                    },
                )
            ],
            tags={"job_id": "job001"},
        )

    def test_teardown_calls_delete_domain(
        self,
        provisioner_with_mocks: OpenSearchProvisioner,
        mock_opensearch_client: MagicMock,
    ) -> None:
        manifest = self._make_manifest()
        provisioner_with_mocks.teardown(manifest)
        mock_opensearch_client.delete_domain.assert_called_once_with(
            DomainName="loadtest-mod-job001-run01"
        )

    def test_teardown_is_idempotent_when_not_found(
        self,
        provisioner_with_mocks: OpenSearchProvisioner,
        mock_opensearch_client: MagicMock,
    ) -> None:
        mock_opensearch_client.delete_domain.side_effect = (
            mock_opensearch_client.exceptions.ResourceNotFoundException()
        )
        manifest = self._make_manifest()
        provisioner_with_mocks.teardown(manifest)

    def test_teardown_skips_non_opensearch_resources(
        self,
        provisioner_with_mocks: OpenSearchProvisioner,
        mock_opensearch_client: MagicMock,
    ) -> None:
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
        mock_opensearch_client.delete_domain.assert_not_called()
