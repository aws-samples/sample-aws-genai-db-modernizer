import json
from pathlib import Path

from benchmarks.runner.loader import load_cases


def _write_case(root: Path):
    case = root / "assignment" / "demo"
    (case / "analysis").mkdir(parents=True)
    (case / "collection.json").write_text(json.dumps({"queries": {"query_patterns": []}}))
    (case / "triage.json").write_text(json.dumps({"selected_agents": [{"agent_type": "dynamodb"}]}))
    (case / "analysis" / "dynamodb.json").write_text(json.dumps({"workload_analysis": {}}))
    (case / "expected.json").write_text(
        json.dumps(
            {
                "case_id": "demo",
                "stage": "assignment",
                "reviewed": True,
                "expected": {"q1": {"acceptable": ["dynamodb"]}},
            }
        )
    )
    (root / "assignment" / "index.json").write_text(
        json.dumps({"cases": [{"id": "demo", "path": "demo", "intent": "x", "tags": ["kv"]}]})
    )


def test_load_cases_reads_manifest_and_artifacts(tmp_path):
    _write_case(tmp_path)
    cases = load_cases(tmp_path / "assignment")
    assert len(cases) == 1
    c = cases[0]
    assert c.case_id == "demo"
    assert c.reviewed is True
    assert c.triage["selected_agents"][0]["agent_type"] == "dynamodb"
    assert "dynamodb" in c.analysis
    assert c.expected["q1"]["acceptable"] == ["dynamodb"]


def test_tag_filter(tmp_path):
    _write_case(tmp_path)
    assert len(load_cases(tmp_path / "assignment", tags=["kv"])) == 1
    assert len(load_cases(tmp_path / "assignment", tags=["nope"])) == 0
