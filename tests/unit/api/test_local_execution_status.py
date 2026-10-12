"""LocalExecutionService reports a terminal/waiting status instead of RUNNING
forever, and cancel actually records that the job stopped (#383).

Before this fix, ``_infer_status`` only ever returned ``SUCCEEDED`` (a
synthesis report exists) or ``RUNNING`` (anything else), and
``stop_execution`` was a no-op -- a job that stopped before Synthesis, or
that is waiting on an external LLM response, showed as running forever and
the cancel call did nothing.
"""

from __future__ import annotations

from pathlib import Path

from src.api.services.local_execution import LocalExecutionService
from src.storage.local_store import LocalArtifactStore

DB, JOB = "wordpress", "job-001"


def _service(tmp_path: Path) -> tuple[LocalExecutionService, LocalArtifactStore]:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    return LocalExecutionService(store), store


def _write_collector(store: LocalArtifactStore) -> None:
    store.write_json(
        f"{DB}/{JOB}/collector/output.json",
        {"metadata": {"source_database": {"engine": "mysql", "database_name": DB}}},
    )


class TestStopExecution:
    def test_returns_false_for_unknown_job(self, tmp_path: Path) -> None:
        service, _ = _service(tmp_path)
        assert service.stop_execution("nope") is False

    def test_writes_a_status_marker(self, tmp_path: Path) -> None:
        service, store = _service(tmp_path)
        _write_collector(store)

        assert service.stop_execution(JOB) is True
        marker = store.read_json(f"{DB}/{JOB}/_status.json")
        assert marker["status"] == "CANCELLED"
        assert marker["stopped_at"]

    def test_records_the_last_completed_phase(self, tmp_path: Path) -> None:
        service, store = _service(tmp_path)
        _write_collector(store)
        store.write_json(f"{DB}/{JOB}/referee-triage/triage.json", {"selected_agents": []})

        service.stop_execution(JOB)
        marker = store.read_json(f"{DB}/{JOB}/_status.json")
        assert marker["last_phase"] == "RunRefereeTriage"


class TestInferStatusAfterCancel:
    def test_cancelled_job_reports_cancelled_not_running(self, tmp_path: Path) -> None:
        service, store = _service(tmp_path)
        _write_collector(store)

        service.stop_execution(JOB)

        execution = service.describe_execution(JOB)
        assert execution is not None
        assert execution["status"] == "CANCELLED"

    def test_cancelled_job_no_longer_lists_as_running(self, tmp_path: Path) -> None:
        service, store = _service(tmp_path)
        _write_collector(store)

        service.stop_execution(JOB)

        executions = service.list_executions()
        assert len(executions) == 1
        assert executions[0]["status"] == "CANCELLED"

    def test_cancel_marker_wins_over_a_later_synthesis_report(self, tmp_path: Path) -> None:
        """A cancelled job must never flip back to SUCCEEDED just because some
        artifact later appears on disk -- the marker is authoritative (#383
        suggested fix: never fabricate a synthesis report to make a job look
        finished, and the inverse holds too: a recorded cancellation stays)."""
        service, store = _service(tmp_path)
        _write_collector(store)
        service.stop_execution(JOB)

        store.write_json(f"{DB}/{JOB}/referee-synthesis/report.json", {"ranking": []})

        execution = service.describe_execution(JOB)
        assert execution is not None
        assert execution["status"] == "CANCELLED"

    def test_unknown_marker_value_falls_through_to_artifact_status(self, tmp_path: Path) -> None:
        """_infer_status only trusts a known marker status (CANCELLED) -- a
        corrupted or unexpected value must not be reported outright, and
        instead falls through to whatever the artifacts on disk say (#518
        round 2)."""
        service, store = _service(tmp_path)
        _write_collector(store)
        store.write_json(f"{DB}/{JOB}/_status.json", {"status": "BOGUS"})

        execution = service.describe_execution(JOB)
        assert execution is not None
        assert execution["status"] == "RUNNING"

        store.write_json(f"{DB}/{JOB}/referee-synthesis/report.json", {"ranking": []})
        execution = service.describe_execution(JOB)
        assert execution is not None
        assert execution["status"] == "SUCCEEDED"


class TestWaitingStatus:
    def test_awaiting_llm_reports_waiting_not_running(self, tmp_path: Path) -> None:
        service, store = _service(tmp_path)
        _write_collector(store)
        store.write_json(
            f"{DB}/{JOB}/reality-check/awaiting_llm.json",
            {"status": "awaiting_llm", "input_key": "irrelevant"},
        )

        execution = service.describe_execution(JOB)
        assert execution is not None
        assert execution["status"] == "WAITING"

    def test_finalized_awaiting_marker_does_not_report_waiting(self, tmp_path: Path) -> None:
        """Once Reality Check's finalize step runs, awaiting_llm.json flips to
        ``status: finalized`` -- that must not still read as WAITING."""
        service, store = _service(tmp_path)
        _write_collector(store)
        store.write_json(f"{DB}/{JOB}/reality-check/awaiting_llm.json", {"status": "finalized"})

        execution = service.describe_execution(JOB)
        assert execution is not None
        assert execution["status"] == "RUNNING"

    def test_normal_running_job_is_unaffected(self, tmp_path: Path) -> None:
        service, store = _service(tmp_path)
        _write_collector(store)

        execution = service.describe_execution(JOB)
        assert execution is not None
        assert execution["status"] == "RUNNING"

    def test_succeeded_job_is_unaffected(self, tmp_path: Path) -> None:
        service, store = _service(tmp_path)
        _write_collector(store)
        store.write_json(f"{DB}/{JOB}/referee-synthesis/report.json", {"ranking": []})

        execution = service.describe_execution(JOB)
        assert execution is not None
        assert execution["status"] == "SUCCEEDED"

    def test_assignment_approval_gate_reports_waiting(self, tmp_path: Path) -> None:
        """Reality Check is done, nothing has approved the assignment yet, and
        schema design hasn't started -- the same WaitForAssignmentApproval
        human gate get_execution_history already reports (#518 round 1)."""
        service, store = _service(tmp_path)
        _write_collector(store)
        store.write_json(f"{DB}/{JOB}/assignment/v1/assignment.json", {"query_assignments": []})
        store.write_json(f"{DB}/{JOB}/reality-check/output.json", {"recommendations": []})

        execution = service.describe_execution(JOB)
        assert execution is not None
        assert execution["status"] == "WAITING"

    def test_schema_design_awaiting_llm_reports_waiting(self, tmp_path: Path) -> None:
        """--llm-mode external schema design writes a request and waits for the
        response to be pasted back in; no schema_output.json exists yet."""
        service, store = _service(tmp_path)
        _write_collector(store)
        store.write_json(
            f"{DB}/{JOB}/llm_requests/schema_design_dynamodb.json", {"engine": "dynamodb"}
        )

        execution = service.describe_execution(JOB)
        assert execution is not None
        assert execution["status"] == "WAITING"

    def test_synthesis_awaiting_llm_reports_waiting(self, tmp_path: Path) -> None:
        """--llm-mode external synthesis writes llm_input.json and waits for the
        response; no report.json exists yet under any candidate location."""
        service, store = _service(tmp_path)
        _write_collector(store)
        store.write_json(f"{DB}/{JOB}/synthesis/llm_input.json", {"ranking": []})

        execution = service.describe_execution(JOB)
        assert execution is not None
        assert execution["status"] == "WAITING"

    def test_awaiting_review_phase_reports_waiting(self, tmp_path: Path) -> None:
        """A phase the orchestrator parked at AWAITING_REVIEW (e.g. after fan-out
        Analysis) means the job is waiting on a human, not actively running."""
        service, store = _service(tmp_path)
        _write_collector(store)
        store.write_json(
            f".meta/{JOB}.json",
            {
                "job_id": JOB,
                "current_phase": "analysis",
                "phases": {"analysis": {"phase": "analysis", "status": "awaiting_review"}},
            },
        )

        execution = service.describe_execution(JOB)
        assert execution is not None
        assert execution["status"] == "WAITING"


class TestFailedStatus:
    """#518 round 1 (I4): a phase the orchestrator recorded FAILED in
    ``.meta/<job>.json`` is read directly with ``store.read_json`` -- never
    through a helper (like LocalOrchestrator.get_progression) that would
    write an inferred progression file as a side effect of just checking."""

    def test_failed_phase_reports_failed_with_its_error(self, tmp_path: Path) -> None:
        service, store = _service(tmp_path)
        _write_collector(store)
        store.write_json(
            f".meta/{JOB}.json",
            {
                "job_id": JOB,
                "current_phase": "analysis",
                "phases": {
                    "analysis": {
                        "phase": "analysis",
                        "status": "failed",
                        "error_message": "boom: analysis agent crashed",
                    }
                },
            },
        )

        execution = service.describe_execution(JOB)
        assert execution is not None
        assert execution["status"] == "FAILED"
        assert execution.get("cause") == "boom: analysis agent crashed"

    def test_checking_status_never_writes_meta_as_a_side_effect(self, tmp_path: Path) -> None:
        service, store = _service(tmp_path)
        _write_collector(store)

        service.describe_execution(JOB)
        service.list_executions()

        assert not store.exists(f".meta/{JOB}.json")


class TestScriptFailureMarker:
    """#517: a scripts/run_*.py entry point that errors out mid-run writes
    the same _status.json marker stop_execution writes for a cancel, but
    with status FAILED (scripts/_job_status.write_failure_marker). This is
    distinct from the orchestrator's own FAILED record in .meta/<job>.json
    (#518 round 1, exercised by TestFailedStatus above)."""

    def test_failed_marker_reports_failed_with_phase_and_reason(self, tmp_path: Path) -> None:
        service, store = _service(tmp_path)
        _write_collector(store)
        store.write_json(
            f"{DB}/{JOB}/_status.json",
            {
                "status": "FAILED",
                "phase": "reality_check",
                "error_type": "ValueError",
                "message": "LLM response not found",
                "failed_at": "2026-10-12T00:00:00+00:00",
            },
        )

        execution = service.describe_execution(JOB)
        assert execution is not None
        assert execution["status"] == "FAILED"
        assert execution["stopped_at"] == "2026-10-12T00:00:00+00:00"
        assert execution["cause"] == "reality_check: ValueError: LLM response not found"

    def test_failed_marker_labels_the_engine_for_a_fan_out_phase(self, tmp_path: Path) -> None:
        service, store = _service(tmp_path)
        _write_collector(store)
        store.write_json(
            f"{DB}/{JOB}/_status.json",
            {
                "status": "FAILED",
                "phase": "schema_design",
                "engine": "dynamodb",
                "error_type": "RuntimeError",
                "message": "boom",
            },
        )

        execution = service.describe_execution(JOB)
        assert execution is not None
        assert execution["cause"] == "schema_design[dynamodb]: RuntimeError: boom"

    def test_failed_marker_does_not_list_as_running(self, tmp_path: Path) -> None:
        service, store = _service(tmp_path)
        _write_collector(store)
        store.write_json(
            f"{DB}/{JOB}/_status.json",
            {"status": "FAILED", "phase": "collect", "error_type": "OSError", "message": "boom"},
        )

        executions = service.list_executions()
        assert len(executions) == 1
        assert executions[0]["status"] == "FAILED"

    def test_a_later_synthesis_report_wins_over_a_stale_failed_marker(self, tmp_path: Path) -> None:
        """Mirrors the .meta-based precedent in _infer_status's docstring: a
        synthesis report means the job is genuinely done, even if a FAILED
        marker from an earlier, since-retried run is still sitting there."""
        service, store = _service(tmp_path)
        _write_collector(store)
        store.write_json(
            f"{DB}/{JOB}/_status.json",
            {
                "status": "FAILED",
                "phase": "assignment",
                "error_type": "RuntimeError",
                "message": "x",
            },
        )
        store.write_json(f"{DB}/{JOB}/referee-synthesis/report.json", {"ranking": []})

        execution = service.describe_execution(JOB)
        assert execution is not None
        assert execution["status"] == "SUCCEEDED"

    def test_cancelled_marker_is_not_read_as_failed(self, tmp_path: Path) -> None:
        """A CANCELLED marker is handled entirely by the earlier, higher
        priority check -- _script_failure must never also fire for it."""
        service, store = _service(tmp_path)
        _write_collector(store)
        service.stop_execution(JOB)

        execution = service.describe_execution(JOB)
        assert execution is not None
        assert execution["status"] == "CANCELLED"

    def test_superseded_marker_falls_through_to_artifact_status(self, tmp_path: Path) -> None:
        """clear_failure_marker writes status SUPERSEDED rather than deleting
        the file (ArtifactStore has no delete) -- _infer_status must treat
        that the same as no marker at all."""
        service, store = _service(tmp_path)
        _write_collector(store)
        store.write_json(f"{DB}/{JOB}/_status.json", {"status": "SUPERSEDED"})

        execution = service.describe_execution(JOB)
        assert execution is not None
        assert execution["status"] == "RUNNING"


class TestAssignmentReviewedMarker:
    """#517: a job approved in chat, or run with --llm-mode none straight
    through to Synthesis, never updates .meta/<job_id>.json through the
    API -- scripts/run_schema_design.py and scripts/run_synthesis.py record
    assignment_review themselves (scripts/_job_status.mark_assignment_reviewed)
    as soon as either actually starts, since both can only run after that
    gate anyway."""

    def test_recorded_review_clears_the_waiting_gate(self, tmp_path: Path) -> None:
        service, store = _service(tmp_path)
        _write_collector(store)
        store.write_json(f"{DB}/{JOB}/assignment/v1/assignment.json", {"query_assignments": []})
        store.write_json(f"{DB}/{JOB}/reality-check/output.json", {"recommendations": []})
        store.write_json(
            f".meta/{JOB}.json",
            {
                "job_id": JOB,
                "current_phase": "assignment_review",
                "phases": {
                    "assignment_review": {"phase": "assignment_review", "status": "completed"}
                },
            },
        )

        execution = service.describe_execution(JOB)
        assert execution is not None
        assert execution["status"] == "RUNNING"

    def test_recorded_review_via_decisions_file_clears_the_waiting_gate(
        self, tmp_path: Path
    ) -> None:
        """#517 round-2 review, N1: when .meta doesn't exist for this job,
        mark_assignment_reviewed records the gate in
        decisions/assignment_review.json instead -- this must clear the
        WAITING gate exactly like the .meta-based record does, with no
        .meta file involved at all."""
        service, store = _service(tmp_path)
        _write_collector(store)
        store.write_json(f"{DB}/{JOB}/assignment/v1/assignment.json", {"query_assignments": []})
        store.write_json(f"{DB}/{JOB}/reality-check/output.json", {"recommendations": []})
        store.write_json(
            f"{DB}/{JOB}/decisions/assignment_review.json",
            {"status": "completed", "recorded_at": "2026-10-12T00:00:00+00:00"},
        )

        assert not store.exists(f".meta/{JOB}.json")
        execution = service.describe_execution(JOB)
        assert execution is not None
        assert execution["status"] == "RUNNING"
