"""render_deliverables turns one synthesis report into every customer deliverable."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.report import deliverables as dl
from src.storage.local_store import LocalArtifactStore

FIXTURE = (
    Path(__file__).resolve().parents[1] / "atx_orchestrator" / "fixtures" / "e2e09_report.json"
)
DB, JOB = "discourse", "job-x"
KEY = f"{DB}/{JOB}/synthesis/v1/report.json"


@pytest.fixture
def store(tmp_path: Path) -> LocalArtifactStore:
    s = LocalArtifactStore(base_dir=str(tmp_path))
    s.write_json(KEY, json.loads(FIXTURE.read_text()))
    return s


def _no_graph(*_):
    return False


def test_renders_all_six_items_in_publish_order(store) -> None:
    out = dl.render_deliverables(store, JOB, DB, KEY, assignment_version=1, graph_fetcher=_no_graph)

    assert out.errors == []
    assert [d.name for d in out.items] == [
        "decision-report",
        "engineering-report",
        "assessment-data",
        "analysis-report",
        "executive-summary-pptx",
        "executive-summary-pdf",
    ]


def test_staged_and_published_sets_match_the_atx_contract(store) -> None:
    out = dl.render_deliverables(store, JOB, DB, KEY, assignment_version=1, graph_fetcher=_no_graph)

    staged = {d.name for d in out.items if d.stage}
    published = [d.name for d in out.items if d.publish]
    # report.json is the system of record, so the raw-data copy is published, not staged.
    assert staged == {
        "decision-report",
        "engineering-report",
        "analysis-report",
        "executive-summary-pptx",
        "executive-summary-pdf",
    }
    # The editable deck is staged but not published; the PDF is the delivery.
    assert published == [
        "decision-report",
        "engineering-report",
        "assessment-data",
        "analysis-report",
        "executive-summary-pdf",
    ]


def test_content_types_and_filenames(store) -> None:
    out = {
        d.name: d
        for d in dl.render_deliverables(store, JOB, DB, KEY, graph_fetcher=_no_graph).items
    }

    content = out["decision-report"].content
    assert content.startswith(b"<!DOCTYPE html") or b"<html" in content[:200]
    assert out["executive-summary-pdf"].content.startswith(b"%PDF")
    assert out["executive-summary-pptx"].content[:2] == b"PK"  # zip container
    assert out["executive-summary-pdf"].filename == "summary-executive-report.pdf"
    assert out["decision-report"].filename.startswith("discourse_decision-report_job-x_")
    assert json.loads(out["assessment-data"].content)["_artifact"]["artifact"] == "assessment-data"


def test_a_failing_optional_deliverable_is_reported_not_raised(store, monkeypatch) -> None:
    def broken(*_a, **_k):
        raise RuntimeError("template missing")

    monkeypatch.setattr("src.report.pdf_report.render_executive_summary_pdf", broken)
    out = dl.render_deliverables(store, JOB, DB, KEY, graph_fetcher=_no_graph)

    names = {d.name for d in out.items}
    assert "executive-summary-pdf" not in names
    assert "executive-summary-pptx" not in names
    assert any(
        e.startswith("executive-summary: RuntimeError: template missing") for e in out.errors
    )
    assert "decision-report" in names


def test_analysis_report_failure_still_renders_the_rest(store, monkeypatch) -> None:
    def broken(*_a, **_k):
        raise RuntimeError("no collector")

    monkeypatch.setattr("src.report.analysis_report.build_export_data", broken)
    out = dl.render_deliverables(store, JOB, DB, KEY, graph_fetcher=_no_graph)

    names = {d.name for d in out.items}
    assert "analysis-report" not in names
    assert any(e.startswith("analysis-report: RuntimeError: no collector") for e in out.errors)
    # The PDF renders with export_data=None; everything else still ships too.
    assert "decision-report" in names
    assert "engineering-report" in names
    assert "assessment-data" in names
    assert "executive-summary-pdf" in names


def test_missing_report_raises(tmp_path: Path) -> None:
    empty = LocalArtifactStore(base_dir=str(tmp_path))
    with pytest.raises(FileNotFoundError):
        dl.render_deliverables(empty, JOB, DB, KEY, graph_fetcher=_no_graph)
