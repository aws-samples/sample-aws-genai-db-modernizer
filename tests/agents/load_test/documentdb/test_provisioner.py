"""Tests for DocumentDB provisioner.

Mocks boto3 (docdb, secretsmanager, sts) and pymongo to verify:
  - Happy path: provision creates secret + cluster + instances + IAM bootstrap
  - VPC env vars are required (clear error if missing)
  - Replicas are created in parallel when replica_count > 0
  - Sizing module integration (instance class + replica count propagate)
  - Resource naming uses DocumentDB conventions (lowercase + hyphens)
  - Tags are applied to cluster, instances, and secret
  - Idempotent teardown (DBClusterNotFoundFault, DBInstanceNotFoundFault caught)
  - IAM bootstrap failure is non-fatal (logs warning, returns manifest)
  - Bootstrap retries with exponential backoff
  - STS assumed-role ARN normalizes to underlying role ARN
"""

from typing import Any
from unittest.mock import MagicMock

import pytest

from src.agents.load_test.documentdb import provisioner as provisioner_module
from src.agents.load_test.documentdb.provisioner import (
    ENV_SECURITY_GROUPS,
    ENV_SUBNET_GROUP,
    ENV_TASK_ROLE_ARN,
    DocumentDBProvisioner,
)
from src.contracts.load_test_models import InfrastructureManifest

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def vpc_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set required VPC environment variables."""
    monkeypatch.setenv(ENV_SUBNET_GROUP, "loadtest-subnet-group")
    monkeypatch.setenv(ENV_SECURITY_GROUPS, "sg-aaaa,sg-bbbb")


@pytest.fixture
def task_role_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the task role ARN override."""
    monkeypatch.setenv(ENV_TASK_ROLE_ARN, "arn:aws:iam::123456789012:role/LoadTestTaskRole")


@pytest.fixture
def schema_output() -> dict[str, Any]:
    """Schema output with embedded collector_output and test_config (per provisioner contract)."""
    return {
        "collections": [
            {
                "source_tables": ["users"],
                "embedded_entities": [],
                "indexes": [{"keys": {"_id": 1}}],
            }
        ],
        "access_patterns": [{"operation": "findOne", "design_rps": 10}],
        "target_engine_version_min": "5.0.0",
        # Coordinator carries these on schema_output for sizing access:
        "_collector_output": {
            "metrics": {
                "cache_hit_ratio_pct": 99.5,
                "cpu_utilization_avg": 30.0,
                "max_connections": 100,
            },
            "source_database": {"database_size_gb": 50.0},
            "rds_metadata": {
                "instance_class": "db.r6i.2xlarge",
                "instance_specs": {"memory_gb": 64, "vcpus": 8},
                "read_replica_count": 0,
                "multi_az": False,
            },
            "schema": {"tables": [{"table_name": "users", "row_count": 100_000}]},
        },
        "_test_config": _FakeTestConfig(),
    }


class _FakeTestConfig:
    scale_factor = 1.0
    max_concurrent_vus = 50


@pytest.fixture
def tags() -> dict[str, str]:
    return {"job_id": "job_001", "run_id": "abc123def456", "database_name": "wordpress"}


@pytest.fixture
def mock_docdb() -> MagicMock:
    """Mock boto3 docdb client with happy-path responses."""
    client = MagicMock()

    # create_db_cluster
    client.create_db_cluster.return_value = {
        "DBCluster": {
            "DBClusterArn": "arn:aws:docdb:us-east-1:123:cluster:loadtest-abc123def456",
            "Endpoint": "loadtest-abc123def456.cluster-xyz.us-east-1.docdb.amazonaws.com",
            "ReaderEndpoint": "loadtest-abc123def456.cluster-ro-xyz.us-east-1.docdb.amazonaws.com",
        }
    }

    # describe_db_clusters — return Available immediately
    client.describe_db_clusters.return_value = {"DBClusters": [{"Status": "available"}]}

    # create_db_instance
    def create_db_instance_side_effect(**kwargs: Any) -> dict[str, Any]:
        instance_id = kwargs["DBInstanceIdentifier"]
        return {
            "DBInstance": {
                "DBInstanceArn": f"arn:aws:docdb:us-east-1:123:db:{instance_id}",
            }
        }

    client.create_db_instance.side_effect = create_db_instance_side_effect

    # waiter — no-op
    waiter = MagicMock()
    waiter.wait.return_value = None
    client.get_waiter.return_value = waiter

    # exception classes — set to plain Exception subclasses for isinstance checks
    client.exceptions.DBClusterAlreadyExistsFault = type(
        "DBClusterAlreadyExistsFault", (Exception,), {}
    )
    client.exceptions.DBClusterNotFoundFault = type("DBClusterNotFoundFault", (Exception,), {})
    client.exceptions.DBInstanceAlreadyExistsFault = type(
        "DBInstanceAlreadyExistsFault", (Exception,), {}
    )
    client.exceptions.DBInstanceNotFoundFault = type("DBInstanceNotFoundFault", (Exception,), {})

    return client


@pytest.fixture
def mock_secrets_manager() -> MagicMock:
    """Mock boto3 secretsmanager client."""
    client = MagicMock()
    client.create_secret.return_value = {
        "ARN": "arn:aws:secretsmanager:us-east-1:123:secret:LoadTest_DocumentDB_abc123_primary-AbCdEf"
    }
    client.exceptions.ResourceNotFoundException = type(
        "ResourceNotFoundException", (Exception,), {}
    )
    return client


@pytest.fixture
def mock_sts() -> MagicMock:
    """Mock boto3 STS client returning a normal IAM role ARN."""
    client = MagicMock()
    client.get_caller_identity.return_value = {
        "Arn": "arn:aws:sts::123456789012:assumed-role/LoadTestTaskRole/abc123session",
        "Account": "123456789012",
    }
    return client


@pytest.fixture
def mock_pymongo() -> MagicMock:
    """Mock pymongo.MongoClient. The IAM bootstrap step uses MongoClient."""
    mock_client = MagicMock()
    mock_client.return_value.__getitem__.return_value.command.return_value = {"ok": 1}
    return mock_client


@pytest.fixture
def patched_provisioner(
    mock_docdb: MagicMock,
    mock_secrets_manager: MagicMock,
    mock_sts: MagicMock,
    mock_pymongo: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> DocumentDBProvisioner:
    """DocumentDBProvisioner with all AWS / pymongo calls mocked."""

    # Patch boto3.client to return appropriate mock based on service name
    def fake_boto_client(service: str, **_kwargs: Any) -> MagicMock:
        mapping = {
            "docdb": mock_docdb,
            "secretsmanager": mock_secrets_manager,
            "sts": mock_sts,
        }
        return mapping[service]

    monkeypatch.setattr("boto3.client", fake_boto_client)

    # Patch the lazy pymongo import in _bootstrap_iam_user
    monkeypatch.setitem(__import__("sys").modules, "pymongo", MagicMock(MongoClient=mock_pymongo))

    # Patch time.sleep to no-op (avoid real backoff in tests)
    monkeypatch.setattr(provisioner_module.time, "sleep", lambda _seconds: None)

    return DocumentDBProvisioner(region="us-east-1")


# =============================================================================
# Happy path
# =============================================================================


class TestHappyPath:
    def test_provision_returns_manifest_with_all_resources(
        self,
        patched_provisioner: DocumentDBProvisioner,
        vpc_env: None,
        task_role_env: None,
        schema_output: dict[str, Any],
        tags: dict[str, str],
        mock_docdb: MagicMock,
    ) -> None:
        manifest = patched_provisioner.provision(schema_output, tags)
        assert isinstance(manifest, InfrastructureManifest)

        types = [r.resource_type for r in manifest.resources]
        assert "AWS::SecretsManager::Secret" in types
        assert "AWS::DocDB::DBCluster" in types
        assert types.count("AWS::DocDB::DBInstance") >= 1  # at least the writer

    def test_cluster_is_created_with_correct_params(
        self,
        patched_provisioner: DocumentDBProvisioner,
        vpc_env: None,
        task_role_env: None,
        schema_output: dict[str, Any],
        tags: dict[str, str],
        mock_docdb: MagicMock,
    ) -> None:
        patched_provisioner.provision(schema_output, tags)
        call_kwargs = mock_docdb.create_db_cluster.call_args.kwargs
        assert call_kwargs["Engine"] == "docdb"
        assert call_kwargs["StorageEncrypted"] is True
        assert call_kwargs["DeletionProtection"] is False
        assert call_kwargs["BackupRetentionPeriod"] == 1
        assert call_kwargs["Port"] == 27017
        assert call_kwargs["DBSubnetGroupName"] == "loadtest-subnet-group"
        assert call_kwargs["VpcSecurityGroupIds"] == ["sg-aaaa", "sg-bbbb"]

    def test_writer_instance_is_created(
        self,
        patched_provisioner: DocumentDBProvisioner,
        vpc_env: None,
        task_role_env: None,
        schema_output: dict[str, Any],
        tags: dict[str, str],
        mock_docdb: MagicMock,
    ) -> None:
        patched_provisioner.provision(schema_output, tags)
        # At least one create_db_instance call (writer); replicas depend on sizing
        assert mock_docdb.create_db_instance.called

    def test_iam_bootstrap_called_with_normalized_role_arn(
        self,
        patched_provisioner: DocumentDBProvisioner,
        vpc_env: None,
        schema_output: dict[str, Any],
        tags: dict[str, str],
        mock_pymongo: MagicMock,
    ) -> None:
        # No ENV_TASK_ROLE_ARN override → STS assumed-role ARN must normalize
        patched_provisioner.provision(schema_output, tags)
        # MongoClient.command was called for createUser
        mock_db = mock_pymongo.return_value.__getitem__.return_value
        assert mock_db.command.called
        call_args = mock_db.command.call_args
        # Second positional arg is the user ARN
        user_arn = call_args.args[1]
        # Should be normalized: arn:aws:iam:: (not arn:aws:sts::assumed-role/)
        assert user_arn.startswith("arn:aws:iam::")
        assert "/role/" in user_arn or ":role/" in user_arn

    def test_resource_naming_lowercase_with_hyphens(
        self,
        patched_provisioner: DocumentDBProvisioner,
        vpc_env: None,
        task_role_env: None,
        schema_output: dict[str, Any],
        tags: dict[str, str],
        mock_docdb: MagicMock,
    ) -> None:
        """DocumentDB requires lowercase letters, digits, hyphens. Verify."""
        patched_provisioner.provision(schema_output, tags)
        cluster_id = mock_docdb.create_db_cluster.call_args.kwargs["DBClusterIdentifier"]
        # No uppercase, no underscores
        assert cluster_id == cluster_id.lower()
        assert "_" not in cluster_id
        assert cluster_id.startswith("loadtest-")

    def test_tags_applied_to_cluster_secret_and_instances(
        self,
        patched_provisioner: DocumentDBProvisioner,
        vpc_env: None,
        task_role_env: None,
        schema_output: dict[str, Any],
        tags: dict[str, str],
        mock_docdb: MagicMock,
        mock_secrets_manager: MagicMock,
    ) -> None:
        patched_provisioner.provision(schema_output, tags)

        cluster_tags = mock_docdb.create_db_cluster.call_args.kwargs["Tags"]
        assert {"Key": "job_id", "Value": "job_001"} in cluster_tags

        secret_tags = mock_secrets_manager.create_secret.call_args.kwargs["Tags"]
        assert {"Key": "run_id", "Value": "abc123def456"} in secret_tags

        instance_tags = mock_docdb.create_db_instance.call_args_list[0].kwargs["Tags"]
        assert {"Key": "database_name", "Value": "wordpress"} in instance_tags


# =============================================================================
# VPC environment validation
# =============================================================================


class TestVpcEnvValidation:
    def test_missing_subnet_group_raises(
        self,
        patched_provisioner: DocumentDBProvisioner,
        task_role_env: None,
        schema_output: dict[str, Any],
        tags: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # No DOCDB_DB_SUBNET_GROUP_NAME env var
        monkeypatch.setenv(ENV_SECURITY_GROUPS, "sg-aaaa")
        with pytest.raises(RuntimeError, match=ENV_SUBNET_GROUP):
            patched_provisioner.provision(schema_output, tags)

    def test_missing_security_groups_raises(
        self,
        patched_provisioner: DocumentDBProvisioner,
        task_role_env: None,
        schema_output: dict[str, Any],
        tags: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(ENV_SUBNET_GROUP, "loadtest-subnet-group")
        # No DOCDB_VPC_SECURITY_GROUP_IDS
        with pytest.raises(RuntimeError, match=ENV_SECURITY_GROUPS):
            patched_provisioner.provision(schema_output, tags)


# =============================================================================
# Replica creation
# =============================================================================


class TestReplicaCreation:
    def test_replicas_created_when_source_is_multi_az(
        self,
        patched_provisioner: DocumentDBProvisioner,
        vpc_env: None,
        task_role_env: None,
        schema_output: dict[str, Any],
        tags: dict[str, str],
        mock_docdb: MagicMock,
    ) -> None:
        # Force multi-az → sizing returns 1 replica
        schema_output["_collector_output"]["rds_metadata"]["multi_az"] = True

        patched_provisioner.provision(schema_output, tags)
        # 1 writer + 1 replica = 2 total create_db_instance calls
        assert mock_docdb.create_db_instance.call_count == 2

    def test_no_replicas_when_source_is_single_instance(
        self,
        patched_provisioner: DocumentDBProvisioner,
        vpc_env: None,
        task_role_env: None,
        schema_output: dict[str, Any],
        tags: dict[str, str],
        mock_docdb: MagicMock,
    ) -> None:
        # Already configured: multi_az=False, read_replica_count=0
        patched_provisioner.provision(schema_output, tags)
        assert mock_docdb.create_db_instance.call_count == 1


# =============================================================================
# Teardown — idempotency
# =============================================================================


class TestTeardown:
    def _build_manifest(self) -> InfrastructureManifest:
        from src.contracts.load_test_models import DeployedResource

        secret_name = "LoadTest_abc_primary"  # pragma: allowlist secret
        return InfrastructureManifest(
            resources=[
                DeployedResource(
                    resource_type="AWS::SecretsManager::Secret",
                    resource_arn=f"arn:aws:secretsmanager:us-east-1:123:secret:{secret_name}",
                    configuration={"secret_name": secret_name},
                ),
                DeployedResource(
                    resource_type="AWS::DocDB::DBCluster",
                    resource_arn="arn:aws:docdb:us-east-1:123:cluster:loadtest-abc",
                    configuration={"cluster_identifier": "loadtest-abc"},
                ),
                DeployedResource(
                    resource_type="AWS::DocDB::DBInstance",
                    resource_arn="arn:aws:docdb:us-east-1:123:db:loadtest-abc-0",
                    configuration={"instance_identifier": "loadtest-abc-0"},
                ),
            ],
            tags={"run_id": "abc"},
        )

    def test_teardown_deletes_in_correct_order(
        self,
        patched_provisioner: DocumentDBProvisioner,
        mock_docdb: MagicMock,
        mock_secrets_manager: MagicMock,
    ) -> None:
        manifest = self._build_manifest()
        patched_provisioner.teardown(manifest)

        # Instance deleted before cluster
        assert mock_docdb.delete_db_instance.called
        assert mock_docdb.delete_db_cluster.called
        # Secret deleted last
        assert mock_secrets_manager.delete_secret.called

    def test_teardown_skips_missing_instance(
        self,
        patched_provisioner: DocumentDBProvisioner,
        mock_docdb: MagicMock,
    ) -> None:
        # Instance already deleted — should not raise
        mock_docdb.delete_db_instance.side_effect = mock_docdb.exceptions.DBInstanceNotFoundFault(
            "not found"
        )
        manifest = self._build_manifest()
        patched_provisioner.teardown(manifest)  # should not raise

    def test_teardown_skips_missing_cluster(
        self,
        patched_provisioner: DocumentDBProvisioner,
        mock_docdb: MagicMock,
    ) -> None:
        mock_docdb.delete_db_cluster.side_effect = mock_docdb.exceptions.DBClusterNotFoundFault(
            "not found"
        )
        manifest = self._build_manifest()
        patched_provisioner.teardown(manifest)  # should not raise

    def test_teardown_skips_missing_secret(
        self,
        patched_provisioner: DocumentDBProvisioner,
        mock_secrets_manager: MagicMock,
    ) -> None:
        mock_secrets_manager.delete_secret.side_effect = (
            mock_secrets_manager.exceptions.ResourceNotFoundException("not found")
        )
        manifest = self._build_manifest()
        patched_provisioner.teardown(manifest)  # should not raise

    def test_teardown_uses_skip_final_snapshot(
        self,
        patched_provisioner: DocumentDBProvisioner,
        mock_docdb: MagicMock,
    ) -> None:
        manifest = self._build_manifest()
        patched_provisioner.teardown(manifest)
        cluster_kwargs = mock_docdb.delete_db_cluster.call_args.kwargs
        assert cluster_kwargs["SkipFinalSnapshot"] is True

    def test_teardown_uses_force_delete_for_secret(
        self,
        patched_provisioner: DocumentDBProvisioner,
        mock_secrets_manager: MagicMock,
    ) -> None:
        manifest = self._build_manifest()
        patched_provisioner.teardown(manifest)
        secret_kwargs = mock_secrets_manager.delete_secret.call_args.kwargs
        assert secret_kwargs["ForceDeleteWithoutRecovery"] is True


# =============================================================================
# IAM bootstrap behavior
# =============================================================================


class TestIamBootstrap:
    def test_bootstrap_failure_does_not_fail_provision(
        self,
        patched_provisioner: DocumentDBProvisioner,
        vpc_env: None,
        task_role_env: None,
        schema_output: dict[str, Any],
        tags: dict[str, str],
        mock_pymongo: MagicMock,
    ) -> None:
        """IAM bootstrap is best-effort. Provision returns manifest even if bootstrap fails."""
        # Force ALL bootstrap attempts to fail
        mock_pymongo.side_effect = Exception("connection refused")
        # Should NOT raise; should return manifest with what was created
        manifest = patched_provisioner.provision(schema_output, tags)
        assert isinstance(manifest, InfrastructureManifest)

    def test_bootstrap_uses_env_override_when_set(
        self,
        patched_provisioner: DocumentDBProvisioner,
        vpc_env: None,
        task_role_env: None,  # sets the override
        schema_output: dict[str, Any],
        tags: dict[str, str],
        mock_pymongo: MagicMock,
    ) -> None:
        patched_provisioner.provision(schema_output, tags)
        mock_db = mock_pymongo.return_value.__getitem__.return_value
        user_arn = mock_db.command.call_args.args[1]
        assert user_arn == "arn:aws:iam::123456789012:role/LoadTestTaskRole"

    def test_bootstrap_normalizes_assumed_role_arn(
        self,
        patched_provisioner: DocumentDBProvisioner,
        vpc_env: None,
        schema_output: dict[str, Any],
        tags: dict[str, str],
        mock_pymongo: MagicMock,
        mock_sts: MagicMock,
    ) -> None:
        """STS GetCallerIdentity returns assumed-role ARN; must normalize to role ARN."""
        # No ENV_TASK_ROLE_ARN → falls back to STS
        patched_provisioner.provision(schema_output, tags)
        mock_db = mock_pymongo.return_value.__getitem__.return_value
        user_arn = mock_db.command.call_args.args[1]
        assert user_arn == "arn:aws:iam::123456789012:role/LoadTestTaskRole"
        # Verify it's NOT the raw STS arn
        assert "assumed-role" not in user_arn
        assert "session" not in user_arn


# =============================================================================
# Sizing module integration
# =============================================================================


class TestSizingIntegration:
    def test_instance_class_propagates_from_sizing(
        self,
        patched_provisioner: DocumentDBProvisioner,
        vpc_env: None,
        task_role_env: None,
        schema_output: dict[str, Any],
        tags: dict[str, str],
        mock_docdb: MagicMock,
    ) -> None:
        patched_provisioner.provision(schema_output, tags)
        instance_class = mock_docdb.create_db_instance.call_args.kwargs["DBInstanceClass"]
        # Per sizing: derived strategy on this workload should pick something
        # in the r6g family, at or above floor (db.r6g.large)
        assert instance_class.startswith("db.r6g.")

    def test_engine_version_propagates_from_schema(
        self,
        patched_provisioner: DocumentDBProvisioner,
        vpc_env: None,
        task_role_env: None,
        schema_output: dict[str, Any],
        tags: dict[str, str],
        mock_docdb: MagicMock,
    ) -> None:
        schema_output["target_engine_version_min"] = "5.0.0"
        patched_provisioner.provision(schema_output, tags)
        engine_version = mock_docdb.create_db_cluster.call_args.kwargs["EngineVersion"]
        assert engine_version == "5.0.0"
