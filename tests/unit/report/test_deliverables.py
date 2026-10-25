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


def test_renders_the_deliverables_in_publish_order(store) -> None:
    out = dl.render_deliverables(store, JOB, DB, KEY, assignment_version=1)

    assert out.errors == []
    assert [d.name for d in out.items] == [
        "engineering-report",
        "assessment-data",
        "engineering-report-detailed",
        "executive-report",
    ]
    # #1364: the detailed report stays in the folder; the delivery channel gets the rest.
    assert [d.name for d in out.items if not d.publish] == ["engineering-report-detailed"]


def test_the_folder_files_are_exactly_the_folder_entries(store) -> None:
    """#987: every rendered item is one file of the deliverables folder, and every
    folder entry is rendered: no deliverable outside the folder, none missing."""
    out = dl.render_deliverables(store, JOB, DB, KEY, assignment_version=1)

    assert [(e.filename, d.name) for e, d in out.folder_files()] == [
        ("executive-report.pdf", "executive-report"),
        ("engineering-report.md", "engineering-report"),
        ("assessment-data.json", "assessment-data"),
        ("engineering-report-detailed.md", "engineering-report-detailed"),
    ]
    assert {d.name for d in out.items} == {e.name for e in dl.FOLDER_ENTRIES}
    assert dl.FOLDER_FILENAMES == (
        "executive-report.pdf",
        "engineering-report.md",
        "assessment-data.json",
        "engineering-report-detailed.md",
        "index.md",
    )


def test_a_failed_render_is_absent_from_the_folder_files(store, monkeypatch) -> None:
    def broken(*_a, **_k):
        raise RuntimeError("font missing")

    monkeypatch.setattr("src.report.executive_report.render_executive_report_pdf", broken)
    out = dl.render_deliverables(store, JOB, DB, KEY)

    assert [e.filename for e, _ in out.folder_files()] == [
        "engineering-report.md",
        "assessment-data.json",
        "engineering-report-detailed.md",
    ]


def test_folder_key_is_the_jobs_deliverables_folder() -> None:
    assert (
        dl.folder_key(DB, JOB, "executive-report.pdf")
        == f"{DB}/{JOB}/deliverables/executive-report.pdf"
    )


@pytest.mark.passes_on_main  # content types and the engineering report's name stem
def test_content_types_and_filenames(store) -> None:
    out = {d.name: d for d in dl.render_deliverables(store, JOB, DB, KEY).items}

    assert out["executive-report"].content.startswith(b"%PDF")
    assert out["engineering-report"].content.startswith(b"---")
    assert out["engineering-report"].filename.startswith("discourse_engineering-report_job-x_")
    assert json.loads(out["assessment-data"].content)["_artifact"]["artifact"] == "assessment-data"


def test_published_names_and_labels_carry_the_assignment_version(store) -> None:
    """#147: the panel is a flat list, so each round's download name and label
    state the assignment version, the executive report's name included."""
    by_round = {
        v: {
            d.name: d
            for d in dl.render_deliverables(store, JOB, DB, KEY, assignment_version=v).items
        }
        for v in (1, 3)
    }
    for v, out in by_round.items():
        assert out["executive-report"].filename == f"executive-report_v{v}.pdf"
        assert f"_v{v}_" in out["engineering-report"].filename
        assert f"_v{v}_" in out["assessment-data"].filename
        for name in ("executive-report", "engineering-report", "assessment-data"):
            assert out[name].label.endswith(f"(v{v})"), name
    # The version is also in the identity block inside the JSON file.
    artifact = json.loads(by_round[3]["assessment-data"].content)["_artifact"]
    assert "_v3_" in artifact["filename"]
    # Two rounds never share a published name.
    for name in ("executive-report", "engineering-report", "assessment-data"):
        assert by_round[1][name].filename != by_round[3][name].filename
    # The folder keeps its stable names.
    rendered = dl.render_deliverables(store, JOB, DB, KEY, assignment_version=3)
    assert [e.filename for e, _ in rendered.folder_files()][0] == "executive-report.pdf"


def test_a_failing_optional_deliverable_is_reported_not_raised(store, monkeypatch) -> None:
    def broken(*_a, **_k):
        raise RuntimeError("template missing")

    monkeypatch.setattr("src.report.executive_report.render_executive_report_pdf", broken)
    out = dl.render_deliverables(store, JOB, DB, KEY)

    names = {d.name for d in out.items}
    assert "executive-report" not in names
    assert any(e.startswith("executive-report: RuntimeError: template missing") for e in out.errors)
    assert names == {"engineering-report", "assessment-data", "engineering-report-detailed"}


def test_synthesis_report_key_prefers_the_highest_version(tmp_path: Path) -> None:
    s = LocalArtifactStore(base_dir=str(tmp_path))
    s.write_json(f"{DB}/{JOB}/synthesis/v1/report.json", {})
    s.write_json(f"{DB}/{JOB}/synthesis/v2/report.json", {})
    assert dl.synthesis_report_key(s, DB, JOB, 0) == f"{DB}/{JOB}/synthesis/v2/report.json"
    assert dl.synthesis_report_key(s, DB, JOB, 1) == f"{DB}/{JOB}/synthesis/v1/report.json"


def test_synthesis_report_key_falls_back_to_the_unversioned_report(tmp_path: Path) -> None:
    s = LocalArtifactStore(base_dir=str(tmp_path))
    with pytest.raises(FileNotFoundError):
        dl.synthesis_report_key(s, DB, JOB, 0)
    s.write_json(f"{DB}/{JOB}/referee-synthesis/report.json", {})
    assert dl.synthesis_report_key(s, DB, JOB, 0) == f"{DB}/{JOB}/referee-synthesis/report.json"


def test_select_frame_prefers_a_src_frame_over_a_deeper_third_party_one() -> None:
    """The actual deepest frame can be inside a third-party library (jinja2,
    reportlab, ...), which isn't actionable -- _select_frame should pick our
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


def test_render_failure_logged_without_echoing_raw_content(
    store, monkeypatch, caplog: pytest.LogCaptureFixture
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

    monkeypatch.setattr("src.report.executive_report.render_executive_report_pdf", broken)
    with caplog.at_level(logging.DEBUG, logger="src.report.deliverables"):
        out = dl.render_deliverables(store, JOB, DB, KEY)

    assert any(sentinel in e for e in out.errors)  # callers still get full detail

    warning_text = _text_at_or_above(caplog, logging.WARNING)
    debug_text = _text_at_or_above(caplog, logging.DEBUG)
    assert sentinel not in warning_text
    assert "executive-report" in warning_text
    assert "RuntimeError" in warning_text
    assert "deliverables.py" in warning_text  # the raising frame
    assert sentinel in debug_text


def test_missing_report_raises(tmp_path: Path) -> None:
    empty = LocalArtifactStore(base_dir=str(tmp_path))
    with pytest.raises(FileNotFoundError):
        dl.render_deliverables(empty, JOB, DB, KEY)


def test_an_older_report_renders_the_flagged_query_risk_it_never_stored(tmp_path: Path) -> None:
    """#1148: the deliverables read a stored report through ``backfill_report_risks``,
    so a report synthesized before ``routing.flagged`` shows the risk its
    ``flagged_unservable`` rows raise, in the engineering report and the data file."""
    s = LocalArtifactStore(base_dir=str(tmp_path))
    report = json.loads(FIXTURE.read_text())
    report["flagged_unservable"] = [
        {
            "query_id": "q-flag",
            "engine": "dynamodb",
            "cause": "no_kept_engine",
            "reason_category": "full_scan",
        }
    ]
    s.write_json(KEY, report)
    out = {d.name: d for d in dl.render_deliverables(s, JOB, DB, KEY).items}
    data = json.loads(out["assessment-data"].content)
    [risk] = [r for r in data["risk_assessment"]["risks"] if r.get("rule") == "routing.flagged"]
    assert risk["risk_id"] == "RISK-029" and risk["severity"] == "HIGH"
    md = out["engineering-report"].content.decode("utf-8")
    assert "**RISK-029**" in md and "although a review found DynamoDB cannot serve it" in md
    # The stored report is left as it was.
    assert "routing.flagged" not in json.dumps(s.read_json(KEY))


def test_a_real_old_report_states_one_risk_count_everywhere(tmp_path: Path) -> None:
    """Review of #1203: a report main's code wrote before ``routing.flagged`` (1.41)
    renders with the backfilled risk, and its summary states the backfilled count
    and level, not the stored ones."""
    s = LocalArtifactStore(base_dir=str(tmp_path))
    old = json.loads((FIXTURE.parent / "wordpress_1_41_flagged_report.json").read_text())
    key = "wordpress/probe-flag/synthesis/v2/report.json"
    s.write_json(key, old)
    out = {d.name: d for d in dl.render_deliverables(s, "probe-flag", "wordpress", key).items}
    md = out["engineering-report"].content.decode("utf-8")
    assert "4 open migration risks \\(overall: HIGH\\)" in md
    assert "3 open migration risks" not in md and "overall: MEDIUM" not in md
    assert "**RISK-004**" in md
    data = json.loads(out["assessment-data"].content)
    assert len(data["risk_assessment"]["risks"]) == 4


def test_an_older_report_renders_the_relational_gap_risks_it_never_stored(tmp_path: Path) -> None:
    """#1149: the gap risks a stored ``facts.relational_gaps`` raises are rendered for a
    report synthesized before ``relational.gaps``, with each gap's reason in full in
    the engineering report."""
    from src.shared.report_facts import compute_report_facts

    s = LocalArtifactStore(base_dir=str(tmp_path))
    report = json.loads(FIXTURE.read_text())
    report["migration_waves"] = [
        {"wave": 1, "engines": ["aurora_postgresql"], "tables": ["db.t"], "table_count": 1}
    ]
    gaps = {
        "source_engine": "sqlserver",
        "target_engine": "aurora_postgresql",
        "gaps": [
            {
                "rule_id": "top",
                "feature": "TOP row limits",
                "impact": "needs_rewrite",
                "reason": "TOP becomes LIMIT on the target.",
                "docs": "Playbook",
                "tables": ["db.t"],
                "queries": ["q1"],
            }
        ],
    }
    report["facts"] = compute_report_facts(report, relational_gaps=gaps)
    s.write_json(KEY, report)
    out = {d.name: d for d in dl.render_deliverables(s, JOB, DB, KEY).items}
    data = json.loads(out["assessment-data"].content)
    [risk] = [r for r in data["risk_assessment"]["risks"] if r.get("rule") == "relational.gaps"]
    assert risk["risk_id"] == "RISK-029" and risk["severity"] == "MEDIUM" and risk["wave"] == 1
    md = out["engineering-report"].content.decode("utf-8")
    assert "**RISK-029**" in md and "TOP becomes LIMIT on the target." in md


def test_a_real_old_trailgear_report_states_its_gap_risks_everywhere(tmp_path: Path) -> None:
    """Review of #1204: trailgear as main's code wrote it before ``relational.gaps``
    (1.41, 9 open risks) renders with its two Wave 1 gap risks, and the summary,
    the risk posture and the data file all state the backfilled 11 (overall HIGH)."""
    s = LocalArtifactStore(base_dir=str(tmp_path))
    old = json.loads((FIXTURE.parent / "trailgear_1_41_report.json").read_text())
    assert "9 open migration risks (overall: HIGH)" in old["summary"]
    key = "trailgear/probe-trai/synthesis/v2/report.json"
    s.write_json(key, old)
    out = {d.name: d for d in dl.render_deliverables(s, "probe-trai", "trailgear", key).items}
    data = json.loads(out["assessment-data"].content)
    gaps = [r for r in data["risk_assessment"]["risks"] if r.get("rule") == "relational.gaps"]
    assert [(r["risk_id"], r["severity"], r["wave"], len(r["details"])) for r in gaps] == [
        ("RISK-010", "MEDIUM", 1, 5),
        ("RISK-011", "MEDIUM", 1, 4),
    ]
    assert data["facts"]["open_risk_count"] == 11
    md = out["engineering-report"].content.decode("utf-8")
    assert "11 open migration risks \\(overall: HIGH\\)" in md
    assert "9 open migration risks" not in md
    assert "**RISK-010**" in md and "**RISK-011**" in md
    # The pointer to the details is the executive report's alone.
    assert "lists each one in full" not in md
