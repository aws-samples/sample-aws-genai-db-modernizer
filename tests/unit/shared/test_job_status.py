"""src.shared.job_status: the durable side effects scripts/run_*.py (and
LocalOrchestrator.resume, for I3) use to keep LocalExecutionService's
_infer_status from reporting a script-driven job as RUNNING or WAITING
forever (#517), including the review follow-ups:

- I1 (round 1): every function here is best-effort (a write failure never
  raises).
- I2 (round 1): nothing is written for a job that doesn't actually exist
  yet (``collector/output.json`` absent), except when a caller opts out
  via ``require_existing_job=False`` (run_collect.py's own phase).
- I3/I4 (round 1 and round 2): clearing is scoped to the exact (phase,
  engine) about to run, or ``ANY_ENGINE`` when the whole fan-out phase is
  about to rerun.
- N1 (round 2, critical): mark_assignment_reviewed never creates
  .meta/<job_id>.json -- it records the approval in
  decisions/assignment_review.json instead when .meta doesn't already
  exist.
"""

from __future__ import annotations

from pathlib import Path

from src.shared.job_status import (
    ANY_ENGINE,
    clear_failure_marker,
    is_assignment_reviewed,
    mark_assignment_reviewed,
    write_failure_marker,
)
from src.storage.local_store import LocalArtifactStore

DB, JOB = "wordpress", "job-001"


def _store(tmp_path: Path) -> LocalArtifactStore:
    return LocalArtifactStore(base_dir=str(tmp_path))


def _make_real(store: LocalArtifactStore) -> None:
    store.write_json(f"{DB}/{JOB}/collector/output.json", {"metadata": {}})


class TestWriteFailureMarker:
    def test_writes_phase_type_message_and_timestamp(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _make_real(store)

        write_failure_marker(store, DB, JOB, "reality_check", "ValueError", "boom")

        marker = store.read_json(f"{DB}/{JOB}/_status.json")
        assert marker["status"] == "FAILED"
        assert marker["phase"] == "reality_check"
        assert marker["engine"] is None
        assert marker["error_type"] == "ValueError"
        assert marker["message"] == "boom"
        assert marker["failed_at"]

    def test_records_the_engine_for_a_fan_out_phase(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _make_real(store)

        write_failure_marker(
            store, DB, JOB, "schema_design", "ValueError", "boom", engine="dynamodb"
        )

        marker = store.read_json(f"{DB}/{JOB}/_status.json")
        assert marker["engine"] == "dynamodb"

    def test_truncates_an_overlong_message(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _make_real(store)

        write_failure_marker(store, DB, JOB, "collect", "ValueError", "x" * 5000)

        marker = store.read_json(f"{DB}/{JOB}/_status.json")
        assert len(marker["message"]) == 1000

    def test_never_overwrites_a_cancelled_marker(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _make_real(store)
        store.write_json(f"{DB}/{JOB}/_status.json", {"status": "CANCELLED", "stopped_at": "t"})

        write_failure_marker(store, DB, JOB, "synthesis", "RuntimeError", "boom")

        marker = store.read_json(f"{DB}/{JOB}/_status.json")
        assert marker["status"] == "CANCELLED"

    def test_overwrites_an_earlier_failure_from_a_different_phase(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _make_real(store)
        write_failure_marker(store, DB, JOB, "assignment", "RuntimeError", "first")

        write_failure_marker(store, DB, JOB, "reality_check", "ValueError", "second")

        marker = store.read_json(f"{DB}/{JOB}/_status.json")
        assert marker["phase"] == "reality_check"
        assert marker["message"] == "second"

    def test_mistyped_job_id_writes_nothing_and_creates_no_directory(self, tmp_path: Path) -> None:
        """I2: no collector/output.json means this "job" was never actually
        collected -- a typo in --job-id/--db must not create a phantom job
        directory just because an _error() call happened to name it."""
        store = _store(tmp_path)

        write_failure_marker(store, DB, "typo-id", "triage", "RuntimeError", "boom")

        assert not (tmp_path / DB).exists()

    def test_require_existing_job_false_writes_for_a_job_that_does_not_exist_yet(
        self, tmp_path: Path
    ) -> None:
        """run_collect.py's own phase: the job it is creating has no
        collector/output.json yet by definition (#517 round-1 review, I2)."""
        store = _store(tmp_path)

        write_failure_marker(
            store,
            DB,
            JOB,
            "collect",
            "RuntimeError",
            "boom",
            require_existing_job=False,
        )

        marker = store.read_json(f"{DB}/{JOB}/_status.json")
        assert marker["status"] == "FAILED"

    def test_is_best_effort_against_a_broken_store(self, tmp_path: Path) -> None:
        """I1: a write failure must never raise out of here."""
        store = _store(tmp_path)
        _make_real(store)

        class _BrokenStore:
            def exists(self, path):
                return store.exists(path)

            def read_json(self, path):
                return store.read_json(path)

            def write_json(self, path, data):
                raise OSError("disk full")

        write_failure_marker(_BrokenStore(), DB, JOB, "collect", "RuntimeError", "boom")  # no raise


class TestClearFailureMarker:
    def test_supersedes_a_matching_failed_marker(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _make_real(store)
        write_failure_marker(store, DB, JOB, "assignment", "RuntimeError", "boom")

        clear_failure_marker(store, DB, JOB, "assignment")

        marker = store.read_json(f"{DB}/{JOB}/_status.json")
        assert marker["status"] == "SUPERSEDED"

    def test_leaves_a_different_phases_marker_alone(self, tmp_path: Path) -> None:
        """I4: a schema-design (or any other) retry must never clear a
        sibling phase's still-relevant FAILED record."""
        store = _store(tmp_path)
        _make_real(store)
        write_failure_marker(store, DB, JOB, "assignment", "RuntimeError", "boom")

        clear_failure_marker(store, DB, JOB, "reality_check")

        marker = store.read_json(f"{DB}/{JOB}/_status.json")
        assert marker["status"] == "FAILED"
        assert marker["phase"] == "assignment"

    def test_parallel_schema_design_engines_do_not_clear_each_other(self, tmp_path: Path) -> None:
        """I4: dynamodb fails; documentdb starting (and clearing for itself)
        must not erase dynamodb's still-unresolved failure."""
        store = _store(tmp_path)
        _make_real(store)
        write_failure_marker(
            store, DB, JOB, "schema_design", "RuntimeError", "ddb boom", engine="dynamodb"
        )

        clear_failure_marker(store, DB, JOB, "schema_design", engine="documentdb")

        marker = store.read_json(f"{DB}/{JOB}/_status.json")
        assert marker["status"] == "FAILED"
        assert marker["engine"] == "dynamodb"

        # dynamodb's own retry, however, clears its own prior failure.
        clear_failure_marker(store, DB, JOB, "schema_design", engine="dynamodb")
        marker = store.read_json(f"{DB}/{JOB}/_status.json")
        assert marker["status"] == "SUPERSEDED"

    def test_leaves_a_cancelled_marker_alone(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _make_real(store)
        store.write_json(f"{DB}/{JOB}/_status.json", {"status": "CANCELLED", "stopped_at": "t"})

        clear_failure_marker(store, DB, JOB, "assignment")

        marker = store.read_json(f"{DB}/{JOB}/_status.json")
        assert marker["status"] == "CANCELLED"

    def test_is_a_noop_when_there_is_no_marker(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _make_real(store)

        clear_failure_marker(store, DB, JOB, "assignment")  # must not raise

        assert not store.exists(f"{DB}/{JOB}/_status.json")

    def test_mistyped_job_id_is_a_noop(self, tmp_path: Path) -> None:
        store = _store(tmp_path)

        clear_failure_marker(store, DB, "typo-id", "triage")

        assert not (tmp_path / DB).exists()

    def test_any_engine_clears_regardless_of_which_engine_failed(self, tmp_path: Path) -> None:
        """#517 round-2 review, I3: when the whole fan-out phase reruns
        (every engine at once -- LocalOrchestrator.resume, or
        run_assessment.py's own _start_phase), the caller doesn't know (and
        shouldn't need to know) which specific engine previously failed."""
        store = _store(tmp_path)
        _make_real(store)
        write_failure_marker(
            store, DB, JOB, "schema_design", "RuntimeError", "ddb boom", engine="dynamodb"
        )

        clear_failure_marker(store, DB, JOB, "schema_design", engine=ANY_ENGINE)

        marker = store.read_json(f"{DB}/{JOB}/_status.json")
        assert marker["status"] == "SUPERSEDED"

    def test_any_engine_still_respects_the_phase(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _make_real(store)
        write_failure_marker(store, DB, JOB, "analysis", "RuntimeError", "boom", engine="dynamodb")

        clear_failure_marker(store, DB, JOB, "schema_design", engine=ANY_ENGINE)

        marker = store.read_json(f"{DB}/{JOB}/_status.json")
        assert marker["status"] == "FAILED"
        assert marker["phase"] == "analysis"


class TestMarkAssignmentReviewed:
    """#517 round-2 review, N1 (critical): when .meta/<job_id>.json doesn't
    exist yet, this must *never* create one -- a hand-written, partial
    progression still validates as JSON, but LocalOrchestrator then loads
    it as the job's entire progression (instead of inferring one properly
    from real artifacts, the way it would for a .meta-less job otherwise),
    and a later resume/get_progression call KeyErrors or 500s. The
    approval goes to decisions/assignment_review.json instead -- this
    job's existing audit-trail folder for human approvals -- and is read
    back by is_assignment_reviewed."""

    def test_records_the_decision_when_no_meta_exists(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _make_real(store)

        mark_assignment_reviewed(store, DB, JOB, "synthesis")

        assert not store.exists(f".meta/{JOB}.json")
        decision = store.read_json(f"{DB}/{JOB}/decisions/assignment_review.json")
        assert decision["status"] == "completed"
        assert decision["source"] == "inferred"
        assert decision["inferred_from"] == "synthesis"
        assert is_assignment_reviewed(store, DB, JOB)

    def test_records_which_phase_inferred_it(self, tmp_path: Path) -> None:
        """#517 round-3 review: schema_design and synthesis both call this,
        so the decision records which one actually inferred the gate."""
        store = _store(tmp_path)
        _make_real(store)

        mark_assignment_reviewed(store, DB, JOB, "schema_design")

        decision = store.read_json(f"{DB}/{JOB}/decisions/assignment_review.json")
        assert decision["inferred_from"] == "schema_design"

    def test_is_idempotent_without_meta(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _make_real(store)
        mark_assignment_reviewed(store, DB, JOB, "synthesis")
        first = store.read_json(f"{DB}/{JOB}/decisions/assignment_review.json")

        mark_assignment_reviewed(store, DB, JOB, "synthesis")

        second = store.read_json(f"{DB}/{JOB}/decisions/assignment_review.json")
        assert second == first
        assert not store.exists(f".meta/{JOB}.json")

    def test_merges_into_an_existing_meta_instead(self, tmp_path: Path) -> None:
        """.meta already existing means the orchestrator already owns a
        complete, valid progression for this job -- safe to merge the one
        field in-place, and no decisions/ file is needed."""
        store = _store(tmp_path)
        _make_real(store)
        store.write_json(
            f".meta/{JOB}.json",
            {
                "job_id": JOB,
                "current_phase": "reality_check",
                "phases": {"reality_check": {"phase": "reality_check", "status": "completed"}},
            },
        )

        mark_assignment_reviewed(store, DB, JOB, "synthesis")

        meta = store.read_json(f".meta/{JOB}.json")
        assert meta["phases"]["reality_check"]["status"] == "completed"
        assert meta["phases"]["assignment_review"]["status"] == "completed"
        assert not store.exists(f"{DB}/{JOB}/decisions/assignment_review.json")

    def test_is_idempotent_when_meta_exists(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _make_real(store)
        store.write_json(
            f".meta/{JOB}.json",
            {
                "job_id": JOB,
                "current_phase": "reality_check",
                "phases": {"reality_check": {"phase": "reality_check", "status": "completed"}},
            },
        )
        mark_assignment_reviewed(store, DB, JOB, "synthesis")
        first = store.read_json(f".meta/{JOB}.json")

        mark_assignment_reviewed(store, DB, JOB, "synthesis")

        second = store.read_json(f".meta/{JOB}.json")
        assert second == first

    def test_never_fabricates_a_meta_file(self, tmp_path: Path) -> None:
        """The critical regression this guards against (#517 round-2
        review, N1): no .meta/<job>.json must come into existence at all
        as a result of calling this with no .meta already there."""
        store = _store(tmp_path)
        _make_real(store)

        mark_assignment_reviewed(store, DB, JOB, "synthesis")

        assert not store.exists(f".meta/{JOB}.json")

    def test_mistyped_job_id_writes_nothing(self, tmp_path: Path) -> None:
        store = _store(tmp_path)

        mark_assignment_reviewed(store, DB, "typo-id", "synthesis")

        assert not store.exists(".meta/typo-id.json")
        assert not store.exists(f"{DB}/typo-id/decisions/assignment_review.json")

    def test_is_best_effort_against_a_broken_store(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _make_real(store)

        class _BrokenStore:
            def exists(self, path):
                return store.exists(path)

            def read_json(self, path):
                return store.read_json(path)

            def write_json(self, path, data):
                raise OSError("disk full")

        mark_assignment_reviewed(_BrokenStore(), DB, JOB, "synthesis")  # must not raise


class TestIsAssignmentReviewed:
    def test_false_before_anything_is_recorded(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _make_real(store)

        assert is_assignment_reviewed(store, DB, JOB) is False

    def test_true_after_mark_assignment_reviewed_without_meta(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _make_real(store)
        mark_assignment_reviewed(store, DB, JOB, "synthesis")

        assert is_assignment_reviewed(store, DB, JOB) is True

    def test_false_when_only_meta_has_it(self, tmp_path: Path) -> None:
        """is_assignment_reviewed only ever reads decisions/assignment_review.json
        -- LocalExecutionService._is_awaiting_assignment_approval checks
        .meta separately and ORs the two together."""
        store = _store(tmp_path)
        _make_real(store)
        store.write_json(
            f".meta/{JOB}.json",
            {
                "phases": {
                    "assignment_review": {"phase": "assignment_review", "status": "completed"}
                }
            },
        )

        assert is_assignment_reviewed(store, DB, JOB) is False

    def test_mistyped_job_id_is_false(self, tmp_path: Path) -> None:
        store = _store(tmp_path)

        assert is_assignment_reviewed(store, DB, "typo-id") is False

    def test_true_from_a_genuine_human_approval_decision(self, tmp_path: Path) -> None:
        """#517 round-3 review: decisions/assignment_approval.json is a
        genuine human approval (scripts/check_decision.py's "--decision
        assignment_approval" convention), written independently of this
        module -- at least as strong a signal as this module's own
        inferred record."""
        store = _store(tmp_path)
        _make_real(store)
        store.write_json(
            f"{DB}/{JOB}/decisions/assignment_approval.json",
            {"status": "approved"},
        )

        assert is_assignment_reviewed(store, DB, JOB) is True
