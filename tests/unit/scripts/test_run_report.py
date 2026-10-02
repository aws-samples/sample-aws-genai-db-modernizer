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

    assert result["status"] == "complete", result
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


def test_missing_report_is_an_error_status(tmp_path: Path) -> None:
    result = run_report.run(JOB, DB, str(tmp_path), assignment_version=None)
    assert result["status"] == "error"
    assert "run /synthesize first" in result["message"]
