"""Run the load-test handler against a committed synthetic DynamoDB design.

The design (docs/examples/wordpress/load-test-results/schema-dynamodb/v2) is a
hand-written fixture, and the collector output is synthesized from its query ids
inside a pytest tmp_path, so the test needs no AWS, no k6 and no local artifacts.
DynamoDB provisioning, seeding and k6 execution are mocked; script generation is real.

Validates the pipeline: read -> adapt -> generate scripts -> (mock run) -> results.
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parents[4]
DB = "wordpress"
JOB_ID = "b70de5dc"
SCHEMA_VERSION = 2
SCHEMA_FIXTURE = (
    REPO / "docs/examples/wordpress/load-test-results/schema-dynamodb/v2/schema_output.json"
)


def _design_with_edge_cases(schema: dict) -> dict:
    """Add one pattern reusing an existing query id (exercises the dedupe) and one
    out-of-scope pattern with its own query id (exercises the in-scope filter)."""
    aps = schema["access_patterns"]
    dup = {**aps[0], "pattern_id": "SYNTH-DUP"}
    out_of_scope = {
        **aps[0],
        "pattern_id": "SYNTH-OUT",
        "query_ids": ["q-out-of-scope"],
        "in_scope": False,
    }
    return {**schema, "access_patterns": [*aps, dup, out_of_scope]}


def _expected_query_ids(schema: dict) -> set[str]:
    """Unique query ids of the patterns the handler must test (in scope, design_rps > 0)."""
    return {
        q
        for ap in schema["access_patterns"]
        if ap.get("in_scope", True) and ap.get("design_rps", 0) > 0
        for q in ap["query_ids"]
    }


def _synthetic_collector_output(schema: dict) -> dict:
    """One obviously synthetic query pattern per query id the design references.

    The committed design is a hand-written fixture (ids q1..qN), not derived from a
    collector run, so the collector side is synthesized from it: only the fields the
    DynamoDB path of ``run_load_test`` reads, with fixed values.
    """
    qids = sorted({q for ap in schema["access_patterns"] for q in ap["query_ids"]})
    return {
        "queries": {
            "query_patterns": [
                {
                    "query_id": qid,
                    "query_text": f"-- synthetic fixture query {qid}",
                    "execution_time_ms_p50": 2.0,
                    "execution_time_ms_p90": 4.0,
                    "execution_time_ms_p95": 6.0,
                    "execution_time_ms_p99": 12.0,
                    "execution_time_ms_p999": 30.0,
                    "execution_time_ms_min": 0.5,
                    "execution_time_ms_max": 40.0,
                }
                for qid in qids
            ]
        }
    }


@pytest.fixture
def artifact_root(tmp_path, monkeypatch):
    """Build wordpress/b70de5dc in a temporary ARTIFACT_DIR (no repo artifacts/ needed)."""
    root = tmp_path / "artifacts"
    job_dir = root / DB / JOB_ID
    schema_dir = job_dir / "schema-dynamodb" / f"v{SCHEMA_VERSION}"
    schema_dir.mkdir(parents=True)
    schema = _design_with_edge_cases(json.loads(SCHEMA_FIXTURE.read_text()))
    (schema_dir / "schema_output.json").write_text(json.dumps(schema))
    (job_dir / "collector").mkdir()
    (job_dir / "collector" / "output.json").write_text(
        json.dumps(_synthetic_collector_output(schema))
    )

    monkeypatch.setenv("RUNTIME_MODE", "local")
    monkeypatch.setenv("ARTIFACT_DIR", str(root))
    # The k6 script generator uses tempfile.mkdtemp(); keep its output inside tmp_path.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    return root


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


@patch("src.agents.load_test.handler._resolve_aws_credentials")
def test_handler_with_synthetic_design(mock_creds, artifact_root, mock_engine_components):
    """Handler tests every in-scope pattern query of the committed synthetic design."""
    schema_path = artifact_root / DB / JOB_ID / "schema-dynamodb" / f"v{SCHEMA_VERSION}"
    expected = _expected_query_ids(json.loads((schema_path / "schema_output.json").read_text()))
    assert "q-out-of-scope" not in expected
    assert expected, "fixture has no testable patterns"
    from src.agents.load_test.handler import run_load_test
    from src.storage import create_artifact_store

    store = create_artifact_store()

    output = run_load_test(
        job_id=JOB_ID,
        database_name=DB,
        target_engine="dynamodb",
        store=store,
        schema_version=SCHEMA_VERSION,
    )

    assert output is not None
    assert output.total_patterns_tested == len(expected)
    assert output.run_id is not None

    # The design reuses one query id across two patterns: results must list it once (Bug 3)
    result_qids = [pr.query_id for pr in output.pattern_results]
    assert len(result_qids) == len(set(result_qids)), "Duplicate query_ids in pattern_results"

    print(f"\nPatterns tested: {output.total_patterns_tested}")
    print(f"Patterns passed: {output.patterns_passed}")
    print(f"Patterns failed: {output.patterns_failed}")
    for pr in output.pattern_results[:5]:
        print(f"  {pr.query_id[:16]}: {pr.operation_type} | {pr.access_pattern_description[:40]}")
