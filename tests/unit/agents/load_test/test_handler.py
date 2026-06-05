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
    def test_skips_non_dynamodb_engines(self, mock_factory):
        store = MagicMock()
        result = run_load_test(
            job_id="j1",
            database_name="test_db",
            target_engine="opensearch",
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
