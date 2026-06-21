"""Tests for engine-agnostic load test handler."""

from unittest.mock import MagicMock, patch

import pytest

from src.agents.load_test.handler import (
    _get_testable_patterns,
    create_engine_components,
    run_load_test,
)


class TestCreateEngineComponents:
    def test_dynamodb_returns_correct_types(self):
        provisioner, seeder, generator, runner = create_engine_components("dynamodb", "us-east-1")

        from src.agents.load_test.dynamodb import (
            DynamoDBProvisioner,
            DynamoDBScriptGenerator,
            DynamoDBSeeder,
            K6Runner,
        )

        assert isinstance(provisioner, DynamoDBProvisioner)
        assert isinstance(seeder, DynamoDBSeeder)
        assert isinstance(generator, DynamoDBScriptGenerator)
        assert isinstance(runner, K6Runner)

    def test_unsupported_engine_raises(self):
        with pytest.raises(ValueError, match="Unsupported engine"):
            create_engine_components("redis", "us-east-1")

    def test_documentdb_returns_correct_types(self):
        provisioner, seeder, generator, runner = create_engine_components("documentdb", "us-east-1")

        from src.agents.load_test.documentdb import (
            DocumentDBProvisioner,
            DocumentDBScriptGenerator,
            DocumentDBSeeder,
        )
        from src.agents.load_test.dynamodb.runner import K6Runner

        assert isinstance(provisioner, DocumentDBProvisioner)
        assert isinstance(seeder, DocumentDBSeeder)
        assert isinstance(generator, DocumentDBScriptGenerator)
        # K6Runner is reused across engines (xk6-mongo is just a custom k6 build)
        assert isinstance(runner, K6Runner)


class TestGetTestablePatterns:
    def test_filters_in_scope_with_traffic(self):
        schema = {
            "access_patterns": [
                {"pattern_id": "AP-1", "in_scope": True, "design_rps": 100},
                {"pattern_id": "AP-2", "in_scope": True, "design_rps": 0},
                {"pattern_id": "AP-3", "in_scope": False, "design_rps": 50},
            ]
        }
        result = _get_testable_patterns(schema)
        assert len(result) == 1
        assert result[0]["pattern_id"] == "AP-1"


class TestRunLoadTest:
    @patch("src.agents.load_test.handler.create_engine_components")
    def test_skips_unsupported_engines(self, mock_factory):
        store = MagicMock()
        result = run_load_test(
            job_id="j1",
            database_name="test_db",
            target_engine="neptune",
            store=store,
            schema_version=1,
        )
        assert result is None
        store.write_json.assert_called_once()

    @patch("src.agents.load_test.handler.materialize_load_test")
    @patch("src.agents.load_test.handler._resolve_aws_credentials")
    @patch("src.agents.load_test.handler.create_engine_components")
    def test_orchestrates_full_lifecycle(self, mock_factory, mock_creds, mock_materialize):
        mock_provisioner = MagicMock()
        mock_seeder = MagicMock()
        mock_generator = MagicMock()
        mock_runner = MagicMock()
        mock_factory.return_value = (mock_provisioner, mock_seeder, mock_generator, mock_runner)

        from src.agents.load_test.models import RunResult, SeedManifest
        from src.contracts.load_test_models import InfrastructureManifest

        mock_provisioner.provision.return_value = InfrastructureManifest(
            resources=[], tags={"job_id": "j1"}
        )
        mock_seeder.seed.return_value = SeedManifest(
            resources={}, total_items=100, duration_seconds=1.0
        )
        mock_generator.generate_all.return_value = "/tmp/scripts"  # nosec B108
        mock_runner.dry_run.return_value = True
        mock_runner.run.return_value = RunResult(
            returncode=0, stdout="", stderr="", summary={"metrics": {}}
        )
        mock_runner.extract_scenario_latency.return_value = MagicMock(p50=5.0)
        mock_runner.extract_scenario_iterations.return_value = 1000
        mock_creds.return_value = {"AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test"}

        store = MagicMock()
        store.read_json.side_effect = [
            # schema_output
            {"table_definitions": [], "access_patterns": []},
            # collector_output
            {"queries": {"query_patterns": []}},
        ]

        result = run_load_test(
            job_id="j1",
            database_name="test_db",
            target_engine="dynamodb",
            store=store,
            schema_version=1,
        )

        mock_provisioner.provision.assert_called_once()
        mock_seeder.seed.assert_called_once()
        mock_generator.generate_all.assert_called_once()
        mock_runner.dry_run.assert_called_once()
        mock_runner.run.assert_called_once()
        mock_provisioner.teardown.assert_called_once()
        assert result is not None

    @patch("src.agents.load_test.handler._resolve_aws_credentials")
    @patch("src.agents.load_test.handler.create_engine_components")
    def test_teardown_called_on_failure(self, mock_factory, mock_creds):
        mock_provisioner = MagicMock()
        mock_seeder = MagicMock()
        mock_generator = MagicMock()
        mock_runner = MagicMock()
        mock_factory.return_value = (mock_provisioner, mock_seeder, mock_generator, mock_runner)

        from src.contracts.load_test_models import InfrastructureManifest

        mock_provisioner.provision.return_value = InfrastructureManifest(
            resources=[], tags={"job_id": "j1"}
        )
        mock_seeder.seed.side_effect = RuntimeError("seed failed")
        mock_creds.return_value = {}

        store = MagicMock()
        store.read_json.side_effect = [
            {
                "table_definitions": [],
                "access_patterns": [
                    {"in_scope": True, "design_rps": 10, "table_name": "T1", "query_ids": ["q1"]}
                ],
            },
            {"queries": {"query_patterns": []}},
        ]

        with pytest.raises(RuntimeError, match="seed failed"):
            run_load_test(
                job_id="j1",
                database_name="test_db",
                target_engine="dynamodb",
                store=store,
                schema_version=1,
            )

        # Teardown still called
        mock_provisioner.teardown.assert_called_once()


class TestRunLoadTestDocumentDB:
    """DocumentDB-specific orchestration: schema_output stuffing pre/post provision."""

    def _build_mocks(
        self,
        cluster_endpoint: str = "loadtest-abc.cluster-xyz.us-east-1.docdb.amazonaws.com",
        replica_count: int = 1,
    ):
        from src.agents.load_test.models import RunResult, SeedManifest
        from src.contracts.load_test_models import DeployedResource, InfrastructureManifest

        provisioner = MagicMock()
        seeder = MagicMock()
        generator = MagicMock()
        runner = MagicMock()

        provisioner.provision.return_value = InfrastructureManifest(
            resources=[
                DeployedResource(
                    resource_type="AWS::DocDB::DBCluster",
                    resource_arn="arn:aws:docdb:us-east-1:123:cluster:loadtest-abc",
                    configuration={
                        "cluster_identifier": "loadtest-abc",
                        "cluster_endpoint": cluster_endpoint,
                        "replica_count": replica_count,
                    },
                ),
            ],
            tags={"job_id": "j1"},
        )
        seeder.seed.return_value = SeedManifest(resources={}, total_items=100, duration_seconds=1.0)
        generator.generate_all.return_value = "/tmp/scripts"  # nosec B108
        runner.dry_run.return_value = True
        runner.run.return_value = RunResult(
            returncode=0, stdout="", stderr="", summary={"metrics": {}}
        )
        runner.extract_scenario_latency.return_value = MagicMock(p50=5.0)
        runner.extract_scenario_iterations.return_value = 1000
        return provisioner, seeder, generator, runner

    @patch("src.agents.load_test.handler.materialize_load_test")
    @patch("src.agents.load_test.handler._resolve_aws_credentials")
    @patch("src.agents.load_test.handler.create_engine_components")
    def test_documentdb_does_not_skip(self, mock_factory, mock_creds, mock_materialize):
        mock_factory.return_value = self._build_mocks()
        mock_creds.return_value = {}

        store = MagicMock()
        store.read_json.side_effect = [
            {"collections": [], "access_patterns": []},
            {"queries": {"query_patterns": []}},
        ]

        result = run_load_test(
            job_id="j1",
            database_name="test_db",
            target_engine="documentdb",
            store=store,
            schema_version=1,
        )

        # documentdb should NOT be skipped
        assert result is not None
        provisioner, _, _, _ = mock_factory.return_value
        provisioner.provision.assert_called_once()

    @patch("src.agents.load_test.handler.materialize_load_test")
    @patch("src.agents.load_test.handler._resolve_aws_credentials")
    @patch("src.agents.load_test.handler.create_engine_components")
    def test_documentdb_stuffs_collector_and_test_config_before_provision(
        self, mock_factory, mock_creds, mock_materialize
    ):
        provisioner, seeder, generator, runner = self._build_mocks()
        mock_factory.return_value = (provisioner, seeder, generator, runner)
        mock_creds.return_value = {}

        store = MagicMock()
        schema_output = {"collections": [], "access_patterns": []}
        collector_output = {"queries": {"query_patterns": []}}
        store.read_json.side_effect = [schema_output, collector_output]

        run_load_test(
            job_id="j1",
            database_name="test_db",
            target_engine="documentdb",
            store=store,
            schema_version=1,
        )

        # Provision was called with schema_output that has _collector_output and _test_config
        provision_args = provisioner.provision.call_args.args
        passed_schema = provision_args[0]
        assert "_collector_output" in passed_schema
        assert passed_schema["_collector_output"] == collector_output
        assert "_test_config" in passed_schema

    @patch("src.agents.load_test.handler.materialize_load_test")
    @patch("src.agents.load_test.handler._resolve_aws_credentials")
    @patch("src.agents.load_test.handler.create_engine_components")
    def test_documentdb_stuffs_endpoint_and_replicas_after_provision(
        self, mock_factory, mock_creds, mock_materialize
    ):
        provisioner, seeder, generator, runner = self._build_mocks(
            cluster_endpoint="loadtest-foo.cluster-bar.us-east-1.docdb.amazonaws.com",
            replica_count=2,
        )
        mock_factory.return_value = (provisioner, seeder, generator, runner)
        mock_creds.return_value = {}

        store = MagicMock()
        store.read_json.side_effect = [
            {"collections": [], "access_patterns": []},
            {"queries": {"query_patterns": []}},
        ]

        run_load_test(
            job_id="j1",
            database_name="test_db",
            target_engine="documentdb",
            store=store,
            schema_version=1,
        )

        # By the time seeder.seed() was called, schema_output had the cluster info
        seed_args = seeder.seed.call_args.args
        passed_schema = seed_args[0]
        assert (
            passed_schema["_documentdb_endpoint"]
            == "loadtest-foo.cluster-bar.us-east-1.docdb.amazonaws.com"
        )
        assert passed_schema["_documentdb_replica_count"] == 2

    @patch("src.agents.load_test.handler.materialize_load_test")
    @patch("src.agents.load_test.handler._resolve_aws_credentials")
    @patch("src.agents.load_test.handler.create_engine_components")
    def test_dynamodb_does_not_get_documentdb_keys_added(
        self, mock_factory, mock_creds, mock_materialize
    ):
        from src.agents.load_test.models import RunResult, SeedManifest
        from src.contracts.load_test_models import InfrastructureManifest

        provisioner = MagicMock()
        seeder = MagicMock()
        generator = MagicMock()
        runner = MagicMock()
        provisioner.provision.return_value = InfrastructureManifest(
            resources=[], tags={"job_id": "j1"}
        )
        seeder.seed.return_value = SeedManifest(resources={}, total_items=0, duration_seconds=0.1)
        generator.generate_all.return_value = "/tmp/scripts"  # nosec B108
        runner.dry_run.return_value = True
        runner.run.return_value = RunResult(
            returncode=0, stdout="", stderr="", summary={"metrics": {}}
        )
        runner.extract_scenario_latency.return_value = MagicMock(p50=5.0)
        runner.extract_scenario_iterations.return_value = 0
        mock_factory.return_value = (provisioner, seeder, generator, runner)
        mock_creds.return_value = {}

        store = MagicMock()
        store.read_json.side_effect = [
            {"table_definitions": [], "access_patterns": []},
            {"queries": {"query_patterns": []}},
        ]

        run_load_test(
            job_id="j1",
            database_name="test_db",
            target_engine="dynamodb",
            store=store,
            schema_version=1,
        )

        # DynamoDB path doesn't enrich schema_output with documentdb-specific keys
        passed_schema = seeder.seed.call_args.args[0]
        assert "_documentdb_endpoint" not in passed_schema
        assert "_collector_output" not in passed_schema
        assert "_test_config" not in passed_schema

    @patch("src.agents.load_test.handler.materialize_load_test")
    @patch("src.agents.load_test.handler._resolve_aws_credentials")
    @patch("src.agents.load_test.handler.create_engine_components")
    def test_documentdb_handles_missing_cluster_resource_gracefully(
        self, mock_factory, mock_creds, mock_materialize
    ):
        """If provisioner returns no DBCluster resource, post-provision stuffing skips."""
        from src.agents.load_test.models import RunResult, SeedManifest
        from src.contracts.load_test_models import InfrastructureManifest

        provisioner = MagicMock()
        seeder = MagicMock()
        generator = MagicMock()
        runner = MagicMock()
        # No DBCluster in resources — degenerate case
        provisioner.provision.return_value = InfrastructureManifest(
            resources=[], tags={"job_id": "j1"}
        )
        seeder.seed.return_value = SeedManifest(resources={}, total_items=0, duration_seconds=0.1)
        generator.generate_all.return_value = "/tmp/scripts"  # nosec B108
        runner.dry_run.return_value = True
        runner.run.return_value = RunResult(
            returncode=0, stdout="", stderr="", summary={"metrics": {}}
        )
        runner.extract_scenario_latency.return_value = MagicMock(p50=5.0)
        runner.extract_scenario_iterations.return_value = 0
        mock_factory.return_value = (provisioner, seeder, generator, runner)
        mock_creds.return_value = {}

        store = MagicMock()
        store.read_json.side_effect = [
            {"collections": [], "access_patterns": []},
            {"queries": {"query_patterns": []}},
        ]

        # Should not raise
        result = run_load_test(
            job_id="j1",
            database_name="test_db",
            target_engine="documentdb",
            store=store,
            schema_version=1,
        )
        assert result is not None
        # Endpoint key was NOT added (no cluster found)
        passed_schema = seeder.seed.call_args.args[0]
        assert "_documentdb_endpoint" not in passed_schema
