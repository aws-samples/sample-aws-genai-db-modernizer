"""run_report writes the deliverables next to the local synthesis report."""

from __future__ import annotations

import json
from pathlib import Path

from scripts import run_report
from src.storage.local_store import LocalArtifactStore

FIXTURE = (
    Path(__file__).resolve().parents[1] / "atx_orchestrator" / "fixtures" / "e2e09_report.json"
)
DB, JOB = "discourse", "job-x"


def test_writes_staged_deliverables_next_to_the_report(tmp_path: Path, monkeypatch) -> None:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json(f"{DB}/{JOB}/synthesis/v2/report.json", json.loads(FIXTURE.read_text()))
    monkeypatch.setattr("src.report.analysis_report.default_graph_fetcher", lambda *_: False)

    result = run_report.run(JOB, DB, str(tmp_path), assignment_version=None)

    # No graph and no legacy query-journeys artifacts -> 0 journeys embedded, which
    # is a warning, not a silent "complete".
    assert result["status"] == "partial", result
    assert result["journeys"] == 0
    assert any("0 query journeys" in w for w in result["warnings"])
    base = tmp_path / DB / JOB / "synthesis" / "v2"
    names = sorted(p.name for p in base.iterdir())
    assert "summary-executive-report.pdf" in names
    assert "summary-executive-report.pptx" in names
    assert any(n.startswith("discourse_decision-report_") and n.endswith(".html") for n in names)
    assert any(n.startswith("discourse_analysis-report_") and n.endswith(".html") for n in names)
    assert any(n.startswith("discourse_engineering-report_") and n.endswith(".md") for n in names)
    assert set(result["files"]) == {
        f"{DB}/{JOB}/synthesis/v2/{n}" for n in names if n != "report.json"
    }
    assert set(result["paths"]) == {str((tmp_path / f).resolve()) for f in result["files"]}


def test_renders_complete_with_a_real_graph_rebuild(tmp_path: Path) -> None:
    """No monkeypatching: the default fetcher rebuilds the graph from a minimal
    collector artifact, so the analysis report embeds a real journey and the
    overall status is "complete", not "partial"."""
    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json(f"{DB}/{JOB}/synthesis/v1/report.json", json.loads(FIXTURE.read_text()))
    store.write_json(
        f"{DB}/{JOB}/collector/output.json",
        {
            "queries": {
                "query_patterns": [
                    {
                        "query_id": "q1",
                        "query_text": "SELECT * FROM posts WHERE id = ?",
                        "query_type": "SELECT",
                        "tables_accessed": ["posts"],
                        "calls_per_second": 12.0,
                    }
                ]
            }
        },
    )

    result = run_report.run(JOB, DB, str(tmp_path), assignment_version=None)

    assert result["status"] == "complete", result
    assert result["journeys"] >= 1
    assert result["warnings"] == []


def test_missing_report_is_an_error_status(tmp_path: Path) -> None:
    result = run_report.run(JOB, DB, str(tmp_path), assignment_version=None)
    assert result["status"] == "error"
    assert "run /synthesize first" in result["message"]


def test_render_failure_still_prints_json_error_status(tmp_path: Path, monkeypatch) -> None:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json(f"{DB}/{JOB}/synthesis/v1/report.json", json.loads(FIXTURE.read_text()))

    def boom(*_a, **_k):
        raise ValueError("boom")

    monkeypatch.setattr("src.report.deliverables.render_deliverables", boom)

    result = run_report.run(JOB, DB, str(tmp_path), assignment_version=None)

    assert result["status"] == "error"
    assert "ValueError" in result["message"]
