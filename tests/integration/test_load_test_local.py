"""Integration test: run load test handler against real schema design artifacts.

Uses real collector + schema outputs from the wordpress fixture but mocks:
  - DynamoDB provisioning/seeding (no real AWS)
  - k6 execution (no real k6 binary needed)

Validates the full pipeline: read → adapt → generate scripts → (mock run) → results.
"""
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Use local artifact store
os.environ.setdefault("RUNTIME_MODE", "local")
os.environ.setdefault("ARTIFACT_DIR", "./artifacts")


FIXTURES_DIR = Path(__file__).parents[2] / "artifacts" / "wordpress" / "b70de5dc"


@pytest.fixture
def mock_engine_components():
    """Mock create_engine_components to return mocked provisioner, seeder, generator, runner."""
    from src.agents.load_test.models import RunResult, SeedManifest
    from src.contracts.load_test_models import (
        DeployedResource,
        InfrastructureManifest,
        LatencyPercentiles,
    )

    provisioner = MagicMock()
    provisioner.provision.return_value = InfrastructureManifest(
        resources=[
            DeployedResource(
                resource_type="AWS::DynamoDB::Table",
                resource_arn="arn:aws:dynamodb:us-east-1:123:table/LoadTest",
                configuration={"BillingMode": "PAY_PER_REQUEST"},
            )
        ],
        tags={"job_id": "b70de5dc", "run_id": "test123"},
    )
    provisioner.teardown.return_value = None

    seeder = MagicMock()
    seeder.seed.return_value = SeedManifest(
        resources={"wp_posts": {"table_name": "wp_posts", "pk_count": 100, "pk_pad_width": 4}},
        total_items=5000,
        duration_seconds=2.5,
    )

    runner = MagicMock()
    runner.dry_run.return_value = True
    runner.run.return_value = RunResult(
        returncode=0,
        stdout="",
        stderr="",
        summary={
            "metrics": {
                "iteration_duration": {
                    "values": {
                        "p(50)": 3.5,
                        "p(90)": 6.0,
                        "p(95)": 8.0,
                        "p(99)": 15.0,
                        "p(99.9)": 40.0,
                        "min": 1.0,
                        "max": 50.0,
                        "count": 10000,
                    }
                }
            }
        },
    )
    runner.extract_scenario_latency.return_value = LatencyPercentiles(
        p50=3.5,
        p90=6.0,
        p95=8.0,
        p99=15.0,
        p999=40.0,
        min=1.0,
        max=50.0,
    )
    runner.extract_scenario_iterations.return_value = 5000

    with patch("src.agents.load_test.handler.create_engine_components") as mock_factory:
        # generator is real — we want to test actual script generation
        from src.agents.load_test.dynamodb import DynamoDBScriptGenerator

        generator = DynamoDBScriptGenerator(region="us-east-1")
        mock_factory.return_value = (provisioner, seeder, generator, runner)
        yield {"provisioner": provisioner, "seeder": seeder, "runner": runner}


@pytest.fixture
def mock_materialize():
    with patch("src.agents.load_test.handler.materialize_load_test") as mock:
        yield mock


@pytest.mark.skipif(
    not FIXTURES_DIR.exists(),
    reason="Real artifacts not available (run test_local_phased.py first)",
)
def test_handler_with_real_schema_output(mock_engine_components, mock_materialize):
    """Handler should successfully process real wordpress schema design output."""
    from src.agents.load_test.handler import run_load_test
    from src.storage import create_artifact_store

    store = create_artifact_store()

    output = run_load_test(
        job_id="b70de5dc",
        database_name="wordpress",
        target_engine="dynamodb",
        store=store,
        schema_version=2,
    )

    assert output is not None
    assert output.total_patterns_tested > 0
    assert output.run_id is not None

    # Verify no duplicate query_ids in results (Bug 3 regression check)
    result_qids = [pr.query_id for pr in output.pattern_results]
    assert len(result_qids) == len(set(result_qids)), "Duplicate query_ids in pattern_results"

    print(f"\nPatterns tested: {output.total_patterns_tested}")
    print(f"Patterns passed: {output.patterns_passed}")
    print(f"Patterns failed: {output.patterns_failed}")
    for pr in output.pattern_results[:5]:
        print(f"  {pr.query_id[:16]}: {pr.operation_type} | {pr.access_pattern_description[:40]}")
