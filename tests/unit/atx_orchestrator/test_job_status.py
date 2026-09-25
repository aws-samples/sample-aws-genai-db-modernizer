"""Tests for job-level terminal-status completion.

Two behaviours are pinned here:

* ``runtime.job_status.complete_job`` issues ``JobStatus.COMPLETED`` on success
  and ``JobStatus.FAILED`` on failure via an injected JobManager, is idempotent
  per job_id, fails open on a JobManager error, and no-ops when no JobManager is
  available (outside the ATX runtime).
* ``set_awaiting_human_input`` / ``resume_executing`` issue the corresponding
  non-terminal ``JobStatus``, are not idempotency-guarded, clear the completion
  latch, and fail open.
* ``tools.run_synthesis_via_a2a`` RESTS the job at AWAITING_HUMAN_INPUT when
  synthesis succeeds (so the customer can re-route — ADR-029 re-entry) and does
  NOT complete it; a synthesis error rests nothing. ``tools.complete_assessment``
  is the single terminal completion, called only on the customer's explicit done.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from src.atx_orchestrator import tools
from src.atx_orchestrator.a2a import A2AFailedError
from src.atx_orchestrator.runtime import job_status


class _FakeJobManager:
    """Records update_job_status calls; optionally raises to test fail-open."""

    def __init__(self, raise_exc: Exception | None = None) -> None:
        self.calls: list[tuple] = []
        self._raise_exc = raise_exc

    def update_job_status(self, status, error_message=None, **kwargs):
        self.calls.append((status, error_message, kwargs))
        if self._raise_exc is not None:
            raise self._raise_exc
        return {"ok": True}


def _status_name(status) -> str:
    """JobStatus is a str-Enum in the SDK; tests inject a SimpleNamespace-free
    stand-in, so accept either an enum-like (.value/.name) or a bare string."""
    return getattr(status, "value", None) or getattr(status, "name", None) or str(status)


# =============================================================================
# complete_job primitive


class TestCompleteJob:
    def setup_method(self) -> None:
        # Isolate the module-level idempotency guard between tests.
        job_status._COMPLETED_JOB_IDS.clear()

    def test_success_marks_completed(self) -> None:
        mgr = _FakeJobManager()
        # Patch JobStatus so we don't require the SDK; complete_job imports it lazily.
        with _patch_jobstatus():
            ok = job_status.complete_job(success=True, job_id="job-1", job_manager=mgr)

        assert ok is True
        assert len(mgr.calls) == 1
        assert _status_name(mgr.calls[0][0]) == "COMPLETED"

    def test_failure_marks_failed_with_message(self) -> None:
        mgr = _FakeJobManager()
        with _patch_jobstatus():
            ok = job_status.complete_job(
                success=False,
                error_message="boom",
                job_id="job-2",
                job_manager=mgr,
            )

        assert ok is True
        status, error_message, _kwargs = mgr.calls[0]
        assert _status_name(status) == "FAILED"
        assert error_message == "boom"

    def test_failure_forwards_category_when_given(self) -> None:
        mgr = _FakeJobManager()
        with _patch_jobstatus():
            job_status.complete_job(
                success=False,
                error_message="boom",
                failure_category="SERVICE_ERROR",
                job_id="job-3",
                job_manager=mgr,
            )

        _status, _msg, kwargs = mgr.calls[0]
        assert kwargs.get("failure_category") == "SERVICE_ERROR"

    def test_idempotent_per_job_id(self) -> None:
        mgr = _FakeJobManager()
        with _patch_jobstatus():
            first = job_status.complete_job(success=True, job_id="job-dup", job_manager=mgr)
            second = job_status.complete_job(success=True, job_id="job-dup", job_manager=mgr)

        assert first is True
        assert second is False  # guarded — no second transition
        assert len(mgr.calls) == 1

    def test_fail_open_when_update_raises(self) -> None:
        mgr = _FakeJobManager(raise_exc=RuntimeError("api down"))
        with _patch_jobstatus():
            ok = job_status.complete_job(success=True, job_id="job-4", job_manager=mgr)

        assert ok is False  # swallowed, not raised
        # Not recorded as completed, so a later retry can still fire.
        assert "job-4" not in job_status._COMPLETED_JOB_IDS

    def test_noop_when_no_job_manager(self) -> None:
        # _resolve_job_manager returns None outside the ATX runtime.
        with patch.object(job_status, "_resolve_job_manager", return_value=None):
            ok = job_status.complete_job(success=True, job_id="job-5")

        assert ok is False


def _patch_jobstatus():
    """Provide a stand-in agent_builder_sdk.agentic_framework.job_manager.JobStatus
    so complete_job's lazy ``from ... import JobStatus`` resolves without the SDK."""
    import sys
    import types
    from unittest.mock import patch as _patch

    class _JobStatus:
        COMPLETED = "COMPLETED"
        FAILED = "FAILED"
        EXECUTING = "EXECUTING"
        AWAITING_HUMAN_INPUT = "AWAITING_HUMAN_INPUT"

    pkg = types.ModuleType("agent_builder_sdk")
    fw = types.ModuleType("agent_builder_sdk.agentic_framework")
    jm = types.ModuleType("agent_builder_sdk.agentic_framework.job_manager")
    jm.JobStatus = _JobStatus  # type: ignore[attr-defined]

    return _patch.dict(
        sys.modules,
        {
            "agent_builder_sdk": pkg,
            "agent_builder_sdk.agentic_framework": fw,
            "agent_builder_sdk.agentic_framework.job_manager": jm,
        },
    )


# =============================================================================
# Non-terminal transitions (ADR-029 re-entry: rest at AWAITING_HUMAN_INPUT, resume EXECUTING)


class TestNonTerminalTransitions:
    def setup_method(self) -> None:
        job_status._COMPLETED_JOB_IDS.clear()

    def test_awaiting_human_input_sets_that_status(self) -> None:
        mgr = _FakeJobManager()
        with _patch_jobstatus():
            ok = job_status.set_awaiting_human_input(job_id="job-1", job_manager=mgr)
        assert ok is True
        assert _status_name(mgr.calls[0][0]) == "AWAITING_HUMAN_INPUT"

    def test_resume_executing_sets_that_status(self) -> None:
        mgr = _FakeJobManager()
        with _patch_jobstatus():
            ok = job_status.resume_executing(job_id="job-1", job_manager=mgr)
        assert ok is True
        assert _status_name(mgr.calls[0][0]) == "EXECUTING"

    def test_transition_clears_completion_latch(self) -> None:
        # A live (non-terminal) status means the job is no longer completed, so a
        # later round is allowed to complete it again.
        job_status._COMPLETED_JOB_IDS.add("job-x")
        mgr = _FakeJobManager()
        with _patch_jobstatus():
            job_status.resume_executing(job_id="job-x", job_manager=mgr)
        assert "job-x" not in job_status._COMPLETED_JOB_IDS

    def test_not_guarded_can_be_called_repeatedly(self) -> None:
        mgr = _FakeJobManager()
        with _patch_jobstatus():
            job_status.set_awaiting_human_input(job_id="job-1", job_manager=mgr)
            job_status.set_awaiting_human_input(job_id="job-1", job_manager=mgr)
        assert len(mgr.calls) == 2  # no idempotency latch on non-terminal transitions

    def test_fail_open_when_update_raises(self) -> None:
        # A terminal job rejects this (TerminalResourceException) — must not raise.
        mgr = _FakeJobManager(raise_exc=RuntimeError("terminal state"))
        with _patch_jobstatus():
            ok = job_status.set_awaiting_human_input(job_id="job-1", job_manager=mgr)
        assert ok is False

    def test_noop_when_no_job_manager(self) -> None:
        with patch.object(job_status, "_resolve_job_manager", return_value=None):
            assert job_status.resume_executing(job_id="job-1") is False


# =============================================================================
# run_synthesis_via_a2a -> rests the job (does NOT complete it), + complete_assessment


class TestSynthesisRestsJobAwaitingInput:
    def test_synthesis_success_rests_awaiting_input_not_completed(self) -> None:
        payload = {"response": {"top_engine": "dynamodb", "report_artifact": "k.json"}}
        with (
            patch("src.atx_orchestrator.tools.invoke_and_wait", return_value=payload),
            patch("src.atx_orchestrator.tools._publish_synthesis_deliverables"),
            patch("src.atx_orchestrator.tools._effective_assignment_version", return_value=1),
            patch("src.atx_orchestrator.runtime.job_status.set_awaiting_human_input") as mock_rest,
            patch("src.atx_orchestrator.runtime.job_status.complete_job") as mock_complete,
        ):
            result = tools.run_synthesis_via_a2a(job_id="job-1", database_name="discourse")

        assert "error" not in json.loads(result)
        mock_rest.assert_called_once()  # rested at AWAITING_HUMAN_INPUT
        mock_complete.assert_not_called()  # NOT terminally completed

    def test_synthesis_error_rests_nothing(self) -> None:
        err = A2AFailedError("synthesis blew up")
        with (
            patch("src.atx_orchestrator.tools.invoke_and_wait", side_effect=err),
            patch("src.atx_orchestrator.tools._effective_assignment_version", return_value=1),
            patch("src.atx_orchestrator.runtime.job_status.set_awaiting_human_input") as mock_rest,
            patch("src.atx_orchestrator.runtime.job_status.complete_job") as mock_complete,
        ):
            result = tools.run_synthesis_via_a2a(job_id="job-1", database_name="discourse")

        assert "error" in json.loads(result)
        mock_rest.assert_not_called()
        mock_complete.assert_not_called()


class TestCompleteAssessmentTool:
    def test_complete_assessment_marks_job_completed(self) -> None:
        with patch("src.atx_orchestrator.runtime.job_status.complete_job") as mock_complete:
            result = tools.complete_assessment(job_id="job-1", database_name="discourse")

        out = json.loads(result)
        assert out["status"] == "completed"
        mock_complete.assert_called_once()
        _, kwargs = mock_complete.call_args
        assert kwargs.get("success") is True
