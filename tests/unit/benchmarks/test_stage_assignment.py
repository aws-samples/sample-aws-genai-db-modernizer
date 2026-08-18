import pytest

from benchmarks.runner.loader import Case
from benchmarks.runner.stage_assignment import run_assignment_case


@pytest.fixture
def demo_case(sample_collector_output, sample_triage_output, sample_analysis_output, tmp_path):
    # Reuse the graph conftest fixtures as known-good agent inputs.
    return Case(
        case_id="demo",
        path=tmp_path,
        intent="reuse fixtures",
        tags=[],
        reviewed=True,
        collection=sample_collector_output,
        triage=sample_triage_output,
        analysis={"dynamodb": sample_analysis_output},
        expected={
            "q1": {
                "acceptable": [
                    "dynamodb",
                    "documentdb",
                    "aurora_mysql",
                    "aurora_postgresql",
                    "opensearch",
                    "elasticache",
                ]
            }
        },
    )


def test_run_assignment_case_returns_engine_map(demo_case, tmp_path):
    result = run_assignment_case(demo_case, tmp_path)
    assert isinstance(result, dict)
    # every query in the collector fixture gets an assigned engine string
    assert "q1" in result
    assert isinstance(result["q1"], str)


def test_deterministic(demo_case, tmp_path):
    r1 = run_assignment_case(demo_case, tmp_path / "a")
    r2 = run_assignment_case(demo_case, tmp_path / "b")
    assert r1 == r2  # rule-based: identical inputs -> identical output
