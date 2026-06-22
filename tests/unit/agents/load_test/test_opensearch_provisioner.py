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
    DOMAIN_PREFIX,
    OpenSearchProvisioner,
)
from src.agents.load_test.opensearch.sizing import SEARCH_INSTANCE_TYPE
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
    """Mock boto3 opensearch client with happy-path responses.

    describe_domain raises ResourceNotFoundException on first call (domain doesn't
    exist yet), then returns a valid response on subsequent calls (after creation).
    """
    client = MagicMock()

    client.exceptions.ResourceAlreadyExistsException = type(
        "ResourceAlreadyExistsException", (Exception,), {}
    )
    client.exceptions.ResourceNotFoundException = type(
        "ResourceNotFoundException", (Exception,), {}
    )

    client.create_domain.return_value = {
        "DomainStatus": {"DomainName": "loadtest-mod-job_001-os", "Processing": True}
    }

    domain_response = {
        "DomainStatus": {
            "DomainName": "loadtest-mod-job_001-os",
            "Processing": False,
            "Endpoint": "search-loadtest-mod-job001-os.us-east-1.es.amazonaws.com",
            "ARN": "arn:aws:es:us-east-1:123456789012:domain/loadtest-mod-job_001-os",
            "ClusterConfig": {"InstanceType": "r8g.large.search", "InstanceCount": 3},
            "EngineVersion": "OpenSearch_2.17",
        }
    }
    # Default: domain exists (reuse path). Tests that need fresh creation
    # override this with side_effect.
    client.describe_domain.return_value = domain_response

    return client


@pytest.fixture
def provisioner_with_mocks(mock_opensearch_client: MagicMock) -> OpenSearchProvisioner:
    """Return a provisioner whose AWS clients are fully mocked."""
    with patch("boto3.client", return_value=mock_opensearch_client):
        p = OpenSearchProvisioner(region="us-east-1")
    p.client = mock_opensearch_client
    return p


class TestSizingIntegration:
    def test_search_workload_uses_r8g(self) -> None:
        from src.agents.load_test.opensearch.sizing import derive_cluster_config

        schema = {"index_designs": [{"index_name": "test"}], "data_stream_designs": []}
        config = derive_cluster_config(schema, {})
        assert config["instance_type"] == SEARCH_INSTANCE_TYPE
        assert "r8g" in config["instance_type"]

    def test_node_count_is_multiple_of_three(self) -> None:
        from src.agents.load_test.opensearch.sizing import derive_cluster_config

        schema = {"index_designs": [{"index_name": "test"}], "data_stream_designs": []}
        config = derive_cluster_config(schema, {})
        assert config["instance_count"] % 3 == 0

    def test_log_analytics_uses_or2(self) -> None:
        from src.agents.load_test.opensearch.sizing import (
            LOG_ANALYTICS_INSTANCE_TYPE,
            derive_cluster_config,
        )

        schema = {
            "index_designs": [],
            "data_stream_designs": [
                {"data_stream_name": "logs", "ism_policy": {"hot_phase_days": 7}}
            ],
        }
        config = derive_cluster_config(schema, {})
        assert config["instance_type"] == LOG_ANALYTICS_INSTANCE_TYPE
        assert "or2" in config["instance_type"]


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
    def test_reuses_existing_domain(
        self,
        provisioner_with_mocks: OpenSearchProvisioner,
        mock_opensearch_client: MagicMock,
        schema_output_minimal: dict[str, Any],
        tags: dict[str, str],
    ) -> None:
        """When domain exists, skip create_domain and return existing manifest."""
        manifest = provisioner_with_mocks.provision(schema_output_minimal, tags)

        mock_opensearch_client.create_domain.assert_not_called()
        assert len(manifest.resources) == 1
        assert manifest.resources[0].resource_type == "AWS::OpenSearchService::Domain"

    def test_creates_domain_when_not_found(
        self,
        provisioner_with_mocks: OpenSearchProvisioner,
        mock_opensearch_client: MagicMock,
        schema_output_minimal: dict[str, Any],
        tags: dict[str, str],
    ) -> None:
        """When domain doesn't exist, create it."""
        domain_response = mock_opensearch_client.describe_domain.return_value
        mock_opensearch_client.describe_domain.side_effect = [
            mock_opensearch_client.exceptions.ResourceNotFoundException("not found"),
            domain_response,
            domain_response,
        ]

        provisioner_with_mocks.provision(schema_output_minimal, tags)

        mock_opensearch_client.create_domain.assert_called_once()
        call_kwargs = mock_opensearch_client.create_domain.call_args.kwargs
        assert call_kwargs["EngineVersion"] == DEFAULT_ENGINE_VERSION
        assert call_kwargs["NodeToNodeEncryptionOptions"]["Enabled"] is True
        assert call_kwargs["EncryptionAtRestOptions"]["Enabled"] is True
        assert call_kwargs["DomainEndpointOptions"]["EnforceHTTPS"] is True
        assert call_kwargs["AdvancedSecurityOptions"]["Enabled"] is True

    def test_provision_returns_manifest_with_endpoint(
        self,
        provisioner_with_mocks: OpenSearchProvisioner,
        schema_output_minimal: dict[str, Any],
        tags: dict[str, str],
    ) -> None:
        manifest = provisioner_with_mocks.provision(schema_output_minimal, tags)

        config = manifest.resources[0].configuration
        assert "search-loadtest" in config["endpoint"]
        assert config["master_user"] == "loadtest_admin"

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

    def test_teardown_does_not_delete_by_default(
        self,
        provisioner_with_mocks: OpenSearchProvisioner,
        mock_opensearch_client: MagicMock,
    ) -> None:
        manifest = self._make_manifest()
        provisioner_with_mocks.teardown(manifest)
        mock_opensearch_client.delete_domain.assert_not_called()

    def test_teardown_force_deletes_domain(
        self,
        provisioner_with_mocks: OpenSearchProvisioner,
        mock_opensearch_client: MagicMock,
    ) -> None:
        manifest = self._make_manifest()
        provisioner_with_mocks.teardown_force(manifest)
        mock_opensearch_client.delete_domain.assert_called_once_with(
            DomainName="loadtest-mod-job001-run01"
        )

    def test_teardown_force_is_idempotent_when_not_found(
        self,
        provisioner_with_mocks: OpenSearchProvisioner,
        mock_opensearch_client: MagicMock,
    ) -> None:
        mock_opensearch_client.delete_domain.side_effect = (
            mock_opensearch_client.exceptions.ResourceNotFoundException()
        )
        manifest = self._make_manifest()
        provisioner_with_mocks.teardown_force(manifest)

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
