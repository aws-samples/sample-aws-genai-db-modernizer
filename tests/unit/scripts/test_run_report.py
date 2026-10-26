"""run_report renders the deliverables into the job's deliverables folder."""

from __future__ import annotations

import json
import re
from pathlib import Path

from scripts import run_report
from src.storage.local_store import LocalArtifactStore

FIXTURE = Path(__file__).resolve().parents[1] / "report" / "fixtures" / "e2e09_report.json"
DB, JOB = "discourse", "job-x"


def test_writes_no_deliverable_copy_next_to_the_report(tmp_path: Path) -> None:
    """#987: the versioned synthesis folder keeps only the pipeline's own files;
    the deliverables live in ``deliverables/`` alone."""
    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json(f"{DB}/{JOB}/synthesis/v2/report.json", json.loads(FIXTURE.read_text()))

    result = run_report.run(JOB, DB, str(tmp_path), assignment_version=None)

    assert result["status"] == "complete", result
    assert result["errors"] == []
    base = tmp_path / DB / JOB / "synthesis" / "v2"
    assert sorted(p.name for p in base.iterdir()) == ["report.json"]
    assert result["report"] == f"{DB}/{JOB}/synthesis/v2/report.json"
    for gone in ("files", "paths", "journeys", "warnings"):
        assert gone not in result, gone


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


# ---------------------------------------------------------------------------
# The customer deliverables folder: artifacts/<db>/<job>/deliverables/
# ---------------------------------------------------------------------------

# Spelled out here on purpose, not imported: renaming a customer file must be a
# deliberate change to this test as well as to the code and the docs.
DOCUMENTED_FILES = {
    "executive-report.pdf",
    "engineering-report.md",
    "assessment-data.json",
    "engineering-report-detailed.md",
    "index.md",
}


def _seed(tmp_path: Path, version: int = 2) -> LocalArtifactStore:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json(
        f"{DB}/{JOB}/synthesis/v{version}/report.json", json.loads(FIXTURE.read_text())
    )
    return store


def test_writes_the_deliverables_folder_with_exactly_the_documented_set(tmp_path: Path) -> None:
    _seed(tmp_path)
    # Model I/O next to the report must never be copied into deliverables/.
    (tmp_path / DB / JOB / "synthesis" / "v2" / "llm_input.json").write_text("{}")
    (tmp_path / DB / JOB / "synthesis" / "v2" / "llm_response.json").write_text("{}")

    result = run_report.run(JOB, DB, str(tmp_path), assignment_version=None)

    folder = tmp_path / DB / JOB / "deliverables"
    assert {p.name for p in folder.iterdir()} == DOCUMENTED_FILES
    assert result["deliverables_dir"] == str(folder.resolve())
    assert {Path(p).name for p in result["deliverables"]} == DOCUMENTED_FILES
    assert (folder / "executive-report.pdf").read_bytes()[:5] == b"%PDF-"


def test_assessment_data_is_the_report_json(tmp_path: Path) -> None:
    """The "Assessment Data (JSON)" the reports point at is the report.json."""
    _seed(tmp_path)

    run_report.run(JOB, DB, str(tmp_path), assignment_version=None)

    data = json.loads((tmp_path / DB / JOB / "deliverables" / "assessment-data.json").read_text())
    identity = data.pop("_artifact")
    assert identity["artifact"] == "assessment-data"
    # The report the deliverables render (#1574: an older report's trade-offs read with
    # each long sentence stated once, as a new synthesis writes them).
    from src.shared.design_text import design_prose_report
    from src.shared.migration_risk_rules import backfill_report_risks

    assert data == design_prose_report(backfill_report_risks(json.loads(FIXTURE.read_text())))


def test_index_lists_each_file_its_audience_and_the_assignment_version(tmp_path: Path) -> None:
    _seed(tmp_path, version=3)

    run_report.run(JOB, DB, str(tmp_path), assignment_version=None)

    index = (tmp_path / DB / JOB / "deliverables" / "index.md").read_text()
    assert "Assignment version: v3" in index
    assert f"Job: {JOB}" in index
    for name in DOCUMENTED_FILES - {"index.md"}:
        row = next((line for line in index.splitlines() if f"`{name}`" in line), None)
        assert row is not None, name
        # | file | what it is | who it is for |
        assert row.count("|") == 4 and all(c.strip() for c in row.strip("|").split("|")), row
    assert "Assessment Data (JSON)" in index
    # Branding: the one product name, nothing else.
    assert "Database Modernizer Assessment Tool" in index
    assert not re.search(r"transform|\bATX\b", index, re.IGNORECASE)


def test_an_older_version_is_refused_and_leaves_the_deliverables_folder_alone(
    tmp_path: Path,
) -> None:
    """deliverables/ always holds the latest assignment version, and no copy is
    written next to an older report any more (#987), so rendering an older
    version has nowhere to go: it is refused, with the folder left as it was."""
    _seed(tmp_path, version=1)
    _seed(tmp_path, version=2)
    run_report.run(JOB, DB, str(tmp_path), assignment_version=None)
    index = tmp_path / DB / JOB / "deliverables" / "index.md"
    before = index.read_text()

    result = run_report.run(JOB, DB, str(tmp_path), assignment_version=1)

    assert result["status"] == "error", result
    assert "v2" in result["message"] and "v1" in result["message"]
    assert index.read_text() == before
    assert "Assignment version: v2" in before
    assert sorted(p.name for p in (tmp_path / DB / JOB / "synthesis" / "v1").iterdir()) == [
        "report.json"
    ]


def test_the_latest_version_named_explicitly_renders_the_folder(tmp_path: Path) -> None:
    _seed(tmp_path, version=1)
    _seed(tmp_path, version=2)

    result = run_report.run(JOB, DB, str(tmp_path), assignment_version=2)

    assert result["status"] == "complete", result
    index = (tmp_path / DB / JOB / "deliverables" / "index.md").read_text()
    assert "Assignment version: v2" in index


def test_a_rerender_removes_the_retired_html_reports_an_older_round_wrote(
    tmp_path: Path,
) -> None:
    """#987: a job rendered before the HTML reports were retired has them in its
    folder; the next render removes them, so the folder matches its index."""
    from src.report.deliverables import RETIRED_FOLDER_FILENAMES

    _seed(tmp_path)
    folder = tmp_path / DB / JOB / "deliverables"
    folder.mkdir(parents=True)
    for name in RETIRED_FOLDER_FILENAMES:
        (folder / name).write_text("<html>old</html>")
    (folder / "my-notes.txt").write_text("customer notes")

    run_report.run(JOB, DB, str(tmp_path), assignment_version=None)

    assert {p.name for p in folder.iterdir()} == DOCUMENTED_FILES | {"my-notes.txt"}


def test_a_rerender_drops_stale_documented_files_and_keeps_others(
    tmp_path: Path, monkeypatch
) -> None:
    """A file this round did not render must not survive from an older round,
    where the index would pass it off as current; files we never write stay."""
    _seed(tmp_path)
    run_report.run(JOB, DB, str(tmp_path), assignment_version=None)
    folder = tmp_path / DB / JOB / "deliverables"
    (folder / "my-notes.txt").write_text("customer notes")

    def boom(*_a, **_k):
        raise ValueError("boom")

    monkeypatch.setattr("src.report.executive_report.render_executive_report_pdf", boom)
    result = run_report.run(JOB, DB, str(tmp_path), assignment_version=None)

    assert result["status"] == "partial"
    names = {p.name for p in folder.iterdir()}
    assert "executive-report.pdf" not in names
    assert "my-notes.txt" in names
    index = (folder / "index.md").read_text()
    assert "`executive-report.pdf`" not in index.split("Not rendered")[0]
    assert "executive-report.pdf" in index.split("Not rendered")[1]


def test_the_index_of_a_scoped_job_states_its_engine_scope(tmp_path: Path) -> None:
    # #1574: the index states the customer's engine scope from the report's facts.
    from src.report.facts import report_facts

    report = json.loads(FIXTURE.read_text())
    report["facts"] = {**report_facts(report), "engine_scope": ["aurora_postgresql", "dynamodb"]}
    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json(f"{DB}/{JOB}/synthesis/v2/report.json", report)

    run_report.run(JOB, DB, str(tmp_path), assignment_version=None)

    index = (tmp_path / DB / JOB / "deliverables" / "index.md").read_text()
    assert "Engines in scope: Aurora PostgreSQL, DynamoDB (customer's choice)." in index
