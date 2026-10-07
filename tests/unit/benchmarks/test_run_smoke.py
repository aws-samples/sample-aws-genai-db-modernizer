import json
from pathlib import Path

from benchmarks.runner.run import run_benchmark


def _make_case(stage_dir: Path, collector, triage, analysis):
    case = stage_dir / "demo"
    (case / "analysis").mkdir(parents=True)
    (case / "collection.json").write_text(json.dumps(collector))
    (case / "triage.json").write_text(json.dumps(triage))
    (case / "analysis" / "dynamodb.json").write_text(json.dumps(analysis))
    (case / "expected.json").write_text(
        json.dumps(
            {
                "case_id": "demo",
                "stage": "assignment",
                "reviewed": True,
                "expected": {
                    "q1": {
                        "acceptable": [
                            "dynamodb",
                            "documentdb",
                            "opensearch",
                            "elasticache",
                            "aurora_mysql",
                            "aurora_postgresql",
                        ]
                    }
                },
            }
        )
    )
    (stage_dir / "index.json").write_text(
        json.dumps({"cases": [{"id": "demo", "path": "demo", "intent": "x", "tags": []}]})
    )


def test_run_benchmark_end_to_end(
    tmp_path, sample_collector_output, sample_triage_output, sample_analysis_output
):
    stage_dir = tmp_path / "assignment"
    stage_dir.mkdir()
    _make_case(stage_dir, sample_collector_output, sample_triage_output, sample_analysis_output)
    report = run_benchmark(stage_dir, work_root=tmp_path / "work")
    assert report["complete"] is True
    assert report["scored_cases"] == 1
    assert report["aggregate_acceptable_accuracy"] == 1.0  # acceptable set is all engines
