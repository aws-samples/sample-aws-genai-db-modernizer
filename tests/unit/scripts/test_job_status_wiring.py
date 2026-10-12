"""Each scripts/run_*.py entry point records a durable failure marker on
_error() or an unhandled exception, and schema design / synthesis record the
assignment-review gate as passed (#517) -- including the round-1 review
follow-ups:

- C1: the marker never carries str(exc) for an unhandled exception, only a
  fixed, content-free message.
- I2: nothing is written for a job that was never actually collected (a
  mistyped --job-id/--db).

scripts/_job_status.py and its _infer_status consumer have their own
dedicated unit tests (tests/unit/scripts/test_job_status.py,
tests/unit/shared/test_job_status.py, tests/unit/api/test_local_execution_status.py);
this file only exercises the wiring in each script's main() -- that
_error()/an unhandled exception at a point where job/db are known actually
reaches write_failure_marker, and that read-only invocations
(run_schema_design.py --status) never write anything.
"""

from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path

import pytest

from scripts import (
    run_analysis,
    run_assessment,
    run_assignment,
    run_reality_check,
    run_schema_design,
    run_triage,
)
from src.storage.local_store import LocalArtifactStore

DB, JOB = "wordpress", "job-001"

REPO_ROOT = Path(__file__).resolve().parents[3]
RUN_COLLECT_SCRIPT = REPO_ROOT / "scripts" / "run_collect.py"
RUN_SYNTHESIS_SCRIPT = REPO_ROOT / "scripts" / "run_synthesis.py"

# A raw (non-contract) collection: run_collect.py takes the offline-parser
# branch for this shape, which is where the unhandled-exception test below
# injects a failure after job/db are known.
RAW_COLLECTION = {
    "collection_version": 1,
    "metadata": {"database_name": DB, "version": "8.0.0"},
    "tables": [{"table_name": "t1", "row_count": 1}],
    "columns": [
        {
            "table_name": "t1",
            "column_name": "id",
            "data_type": "int",
            "is_nullable": False,
            "ordinal_position": 1,
        }
    ],
    "indexes": [],
    "foreign_keys": [],
    "primary_keys": [],
    "queries": [{"query_text": "SELECT 1", "digest": "d1"}],
    "global_stats": {},
}


def _store(tmp_path: Path) -> LocalArtifactStore:
    return LocalArtifactStore(base_dir=str(tmp_path / "artifacts"))


def _make_real(store: LocalArtifactStore) -> None:
    """collector/output.json existing is what "the job is real" means
    (#517 round-1 review, I2) -- every test below that expects a marker to
    actually get written sets this up first."""
    store.write_json(f"{DB}/{JOB}/collector/output.json", {"metadata": {}})


def _marker(store: LocalArtifactStore) -> dict:
    return store.read_json(f"{DB}/{JOB}/_status.json")


def test_mistyped_job_id_writes_no_marker_and_no_job_directory(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """#517 round-1 review, I2: run_triage.py's own "Collector output not
    found" error IS the job-reality check failing -- a typo in --job-id or
    --db must not litter the artifact tree with a phantom job directory
    just because _error() happened to run."""
    store = _store(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_triage.py",
            "--job-id",
            JOB,
            "--db",
            DB,
            "--artifact-root",
            str(store.base_dir),
        ],
    )

    with pytest.raises(SystemExit):
        run_triage.main()

    assert not store.exists(f"{DB}/{JOB}/_status.json")
    assert not (store.base_dir / DB).exists()


def test_assignment_error_writes_a_failed_marker(tmp_path: Path, monkeypatch, capsys) -> None:
    store = _store(tmp_path)
    _make_real(store)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_assignment.py",
            "--job-id",
            JOB,
            "--db",
            DB,
            "--artifact-root",
            str(store.base_dir),
        ],
    )

    with pytest.raises(SystemExit):
        run_assignment.main()

    marker = _marker(store)
    assert marker["status"] == "FAILED"
    assert marker["phase"] == "assignment"
    assert "Triage output not found" in marker["message"]


def test_reality_check_finalize_error_writes_a_failed_marker(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    store = _store(tmp_path)
    _make_real(store)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_reality_check.py",
            "--job-id",
            JOB,
            "--db",
            DB,
            "--finalize",
            "--artifact-root",
            str(store.base_dir),
        ],
    )

    with pytest.raises(SystemExit):
        run_reality_check.main()

    marker = _marker(store)
    assert marker["status"] == "FAILED"
    assert marker["phase"] == "reality_check"
    assert "LLM response not found" in marker["message"]


def test_analysis_unsupported_finalize_writes_a_failed_marker(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    store = _store(tmp_path)
    _make_real(store)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_analysis.py",
            "--job-id",
            JOB,
            "--db",
            DB,
            "--engine",
            "opensearch",
            "--finalize",
            "--artifact-root",
            str(store.base_dir),
        ],
    )

    with pytest.raises(SystemExit):
        run_analysis.main()

    marker = _marker(store)
    assert marker["status"] == "FAILED"
    assert marker["phase"] == "analysis"
    assert marker["engine"] == "opensearch"


def test_schema_design_split_on_aurora_writes_a_failed_marker(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    store = _store(tmp_path)
    _make_real(store)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_schema_design.py",
            "--job-id",
            JOB,
            "--db",
            DB,
            "--engine",
            "aurora_mysql",
            "--split",
            "--assignment-version",
            "1",
            "--artifact-root",
            str(store.base_dir),
        ],
    )

    with pytest.raises(SystemExit):
        run_schema_design.main()

    marker = _marker(store)
    assert marker["status"] == "FAILED"
    assert marker["phase"] == "schema_design"
    assert marker["engine"] == "aurora_mysql"
    assert "single-pass" in marker["message"]


def test_schema_design_status_is_read_only_and_writes_no_marker(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """--status is documented read-only -- it must never affect the job's
    own status, even when the engine has no drafts yet."""
    store = _store(tmp_path)
    _make_real(store)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_schema_design.py",
            "--job-id",
            JOB,
            "--db",
            DB,
            "--engine",
            "dynamodb",
            "--status",
            "--assignment-version",
            "1",
            "--artifact-root",
            str(store.base_dir),
        ],
    )

    run_schema_design.main()

    assert not store.exists(f"{DB}/{JOB}/_status.json")
    assert not store.exists(f".meta/{JOB}.json")


def test_schema_design_records_assignment_reviewed(tmp_path: Path, monkeypatch, capsys) -> None:
    """Schema design can only ever run after the assignment review gate --
    even this failing --split call records it, since the check that fails
    happens after the gate is recorded (#517). No .meta/<job>.json exists
    for this job, so the approval goes to decisions/assignment_review.json
    instead (#517 round-2 review, N1): writing a partial .meta here would
    make LocalOrchestrator load it as the job's entire progression later."""
    store = _store(tmp_path)
    _make_real(store)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_schema_design.py",
            "--job-id",
            JOB,
            "--db",
            DB,
            "--engine",
            "aurora_mysql",
            "--split",
            "--assignment-version",
            "1",
            "--artifact-root",
            str(store.base_dir),
        ],
    )

    with pytest.raises(SystemExit):
        run_schema_design.main()

    assert not store.exists(f".meta/{JOB}.json")
    decision = store.read_json(f"{DB}/{JOB}/decisions/assignment_review.json")
    assert decision["status"] == "completed"


def test_synthesis_unhandled_exception_writes_a_failed_marker_and_records_review(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """run_finalize loads the full pipeline's artifacts before it ever checks
    for the llm_response file it is actually missing here, so this is an
    unhandled RuntimeError (synthesis_data._read_artifact's "Required
    artifact missing"), not an _error() call -- exercised through the real
    ``if __name__ == "__main__":`` catch-all (#517), the same as
    test_collect_unhandled_exception_writes_a_failed_marker. The
    assignment-review gate is still recorded, since that happens before the
    failing call. #517 round-1 review, C1: the marker's message is the
    fixed, content-free string, never the exception's own text."""
    store = _store(tmp_path)
    _make_real(store)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_synthesis.py",
            "--job-id",
            JOB,
            "--db",
            DB,
            "--finalize",
            "--assignment-version",
            "1",
            "--artifact-root",
            str(store.base_dir),
        ],
    )

    with pytest.raises(RuntimeError, match="Required artifact missing"):
        runpy.run_path(str(RUN_SYNTHESIS_SCRIPT), run_name="__main__")

    marker = _marker(store)
    assert marker["status"] == "FAILED"
    assert marker["phase"] == "synthesis"
    assert marker["error_type"] == "RuntimeError"
    assert marker["message"] == "unhandled error; see _logs/"

    # No .meta/<job>.json exists for this job -- the approval goes to
    # decisions/assignment_review.json instead (#517 round-2 review, N1).
    assert not store.exists(f".meta/{JOB}.json")
    decision = store.read_json(f"{DB}/{JOB}/decisions/assignment_review.json")
    assert decision["status"] == "completed"


def test_collect_unhandled_exception_writes_a_failed_marker(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """An unhandled exception after job/db are known -- not an _error() call
    -- is also recorded, via the real ``if __name__ == "__main__":`` catch-all
    (#517). Runs the actual script file (not just ``main()``) with ``runpy``
    so that catch-all is exercised, with the parser patched to fail.
    collector/output.json does not exist at any point this script can still
    fail -- require_existing_job=False is what lets this marker get written
    at all (#517 round-1 review, I2). #517 round-1 review, C1: the message
    is the fixed, content-free string, never the raw exception text."""
    store = _store(tmp_path)
    collector_file = tmp_path / "collection.json"
    collector_file.write_text(json.dumps(RAW_COLLECTION))

    def _boom(*args, **kwargs):
        raise RuntimeError("disk is on fire")

    monkeypatch.setattr("src.tools.database.offline_parser.parse_offline_collection", _boom)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_collect.py",
            "--file",
            str(collector_file),
            "--db",
            DB,
            "--job-id",
            JOB,
            "--artifact-root",
            str(store.base_dir),
        ],
    )

    with pytest.raises(RuntimeError, match="disk is on fire"):
        runpy.run_path(str(RUN_COLLECT_SCRIPT), run_name="__main__")

    marker = _marker(store)
    assert marker["status"] == "FAILED"
    assert marker["phase"] == "collect"
    assert marker["error_type"] == "RuntimeError"
    assert marker["message"] == "unhandled error; see _logs/"


def test_run_assessment_unhandled_exception_in_a_later_phase_writes_a_failed_marker(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """run_assessment.py drives collect through reality-check in one
    invocation (#517): collect succeeds (job/db become known), then triage
    raises -- an unhandled exception caught by main()'s own compact-mode
    handler, not run_collect.py's. The marker names the phase that was
    actually running, not "collect". #517 round-1 review, C1: the marker's
    message is the fixed string, never the raw exception text."""
    store = _store(tmp_path)
    collector_file = tmp_path / "collection.json"
    collector_file.write_text(json.dumps(RAW_COLLECTION))

    def _boom(*args, **kwargs):
        raise RuntimeError("triage blew up")

    monkeypatch.setattr("src.agents.referee.triage_handler.run_triage", _boom)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_assessment.py",
            "--file",
            str(collector_file),
            "--db",
            DB,
            "--artifact-root",
            str(store.base_dir),
        ],
    )

    with pytest.raises(RuntimeError, match="triage blew up"):
        run_assessment.main()

    [job_dir] = (store.base_dir / DB).iterdir()
    job_id = job_dir.name
    marker = store.read_json(f"{DB}/{job_id}/_status.json")
    assert marker["status"] == "FAILED"
    assert marker["phase"] == "triage"
    assert marker["error_type"] == "RuntimeError"
    assert marker["message"] == "unhandled error; see _logs/"


def test_run_assessment_start_phase_clears_any_engine_for_fan_out_phases(
    tmp_path: Path,
) -> None:
    """#517 round-2 review, I3: run_assessment.py's own _start_phase("analysis")
    (and "schema_design") reruns every selected engine at once, not one
    specific engine -- it must clear a FAILED marker regardless of which
    engine it names."""
    store = _store(tmp_path)
    _make_real(store)
    store.write_json(
        f"{DB}/{JOB}/_status.json",
        {
            "status": "FAILED",
            "phase": "analysis",
            "engine": "dynamodb",
            "error_type": "RuntimeError",
        },
    )
    run_assessment._JOB.clear()
    run_assessment._JOB.update(store=store, db=DB, job_id=JOB)
    try:
        run_assessment._start_phase("analysis")
    finally:
        run_assessment._JOB.clear()

    marker = _marker(store)
    assert marker["status"] == "SUPERSEDED"
