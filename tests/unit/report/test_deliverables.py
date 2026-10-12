"""render_deliverables turns one synthesis report into every customer deliverable."""

from __future__ import annotations

import json
import logging
import traceback
from pathlib import Path

import pytest

from src.report import deliverables as dl
from src.storage.local_store import LocalArtifactStore

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "e2e09_report.json"
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
    # The label still carries the round (#146); #147 tracks the download name too.
    assert "(v1)" in out["executive-summary-pdf"].label
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


def test_select_frame_prefers_a_src_frame_over_a_deeper_third_party_one() -> None:
    """The actual deepest frame can be inside a third-party library (jinja2,
    python-pptx, ...), which isn't actionable -- _select_frame should pick our
    own nearest frame instead. Built from plain FrameSummary values (outermost
    first, as traceback.extract_tb returns them), no real exception needed."""
    middle = traceback.FrameSummary("/repo/src/report/pdf_report.py", 42, "middle")
    inner = traceback.FrameSummary("/vendor/thirdparty/lib.py", 7, "inner")

    frame = dl._select_frame([middle, inner])

    assert frame is middle


def test_select_frame_falls_back_to_the_deepest_frame_when_none_is_under_src() -> None:
    """No frame is under src/ -- fall back to the actual deepest frame rather
    than returning nothing useful."""
    outer = traceback.FrameSummary("/vendor/thirdparty/caller.py", 1, "outer")
    inner = traceback.FrameSummary("/vendor/thirdparty/lib.py", 7, "inner")

    frame = dl._select_frame([outer, inner])

    assert frame is inner


def test_select_frame_returns_none_for_an_empty_traceback() -> None:
    assert dl._select_frame([]) is None


_FORMATTER = logging.Formatter()


def _format(record: logging.LogRecord) -> str:
    """Full record text, including a traceback -- ``exc_info=True`` attaches it
    here, not in ``record.getMessage()`` (see ``test_log_hygiene.py``)."""
    return _FORMATTER.format(record)


def _text_at_or_above(caplog: pytest.LogCaptureFixture, level: int) -> str:
    return "\n".join(_format(r) for r in caplog.records if r.levelno >= level)


@pytest.mark.parametrize(
    "artifact,target",
    [
        ("executive-summary", "src.report.pdf_report.render_executive_summary_pdf"),
        ("analysis-report", "src.report.analysis_report.build_export_data"),
    ],
)
def test_render_failure_logged_without_echoing_raw_content(
    store, monkeypatch, caplog: pytest.LogCaptureFixture, artifact: str, target: str
) -> None:
    """A render failure (#146) must stay diagnosable without repeating the #143
    logging-content leak: the raised message can echo the report's own content
    (a customer-derived table/query name baked into a template error), so only
    the artifact name, the exception type and the raising frame belong at
    WARNING+ -- never the message. The full traceback (``exc_info=True``) stays
    available at DEBUG for troubleshooting."""
    sentinel = "SENTINEL_CUSTOMER_PAYLOAD_146"

    def broken(*_a, **_k):
        raise RuntimeError(f"template missing: {sentinel}")

    monkeypatch.setattr(target, broken)
    with caplog.at_level(logging.DEBUG, logger="src.report.deliverables"):
        out = dl.render_deliverables(store, JOB, DB, KEY, graph_fetcher=_no_graph)

    assert any(sentinel in e for e in out.errors)  # callers still get full detail

    warning_text = _text_at_or_above(caplog, logging.WARNING)
    debug_text = _text_at_or_above(caplog, logging.DEBUG)
    assert sentinel not in warning_text
    assert artifact in warning_text
    assert "RuntimeError" in warning_text
    assert "deliverables.py" in warning_text  # the raising frame
    assert sentinel in debug_text


def test_missing_report_raises(tmp_path: Path) -> None:
    empty = LocalArtifactStore(base_dir=str(tmp_path))
    with pytest.raises(FileNotFoundError):
        dl.render_deliverables(empty, JOB, DB, KEY, graph_fetcher=_no_graph)


def test_zero_journeys_is_a_warning_not_a_silent_success(store) -> None:
    """A graph that cannot be fetched (and no legacy query-journeys artifacts)
    renders an analysis report with 0 journeys embedded. That must surface as a
    warning -- not look identical to a job that genuinely has none."""
    out = dl.render_deliverables(store, JOB, DB, KEY, graph_fetcher=_no_graph)

    assert out.errors == []
    assert out.journeys == 0
    assert any("0 query journeys" in w for w in out.warnings)
    assert "analysis-report" in {d.name for d in out.items}
