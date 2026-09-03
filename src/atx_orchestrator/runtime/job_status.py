"""Job-level terminal-status transition for the AWS Transform orchestrator.

The problem this solves: when the assessment pipeline finishes, the platform job
stays in ``EXECUTING`` forever. The ATX platform auto-transitions a job *into*
``EXECUTING`` at agent start (SDK ``JobManager.transition_to_executing_if_assessing``),
but it does NOT roll the job up to a terminal state when only the plan steps and
subagent instances finish. The orchestrator that owns the job must explicitly
mark it ``COMPLETED`` (or ``FAILED``) — this is the same pattern the reference
run-to-completion agents use (ATXDotNetStrandsCLI ``BaseRunner.complete_job`` ->
``JobManager.update_job_status``).

Only the orchestrator calls this. Subagents report their own *instance* status
(``subagents/base.py`` ``_report_completed``) and must never complete the job —
the orchestrator owns the job lifecycle.

Design mirrors ``job_plan.py``:
    * Lazy SDK imports so this module imports fine outside the ATX runtime.
    * Graceful degradation — outside the runtime (local Mac, unit tests) it is a
      no-op that returns ``False`` instead of raising.
    * Fail-open — any error is logged and swallowed; a completion failure must
      not crash the final pipeline turn.
    * Idempotent — a job is completed at most once per process, guarded by
      ``_COMPLETED_JOB_IDS`` (mirrors the reference runner's ``_job_completed``
      flag). Repeated pipeline turns or retries do not re-fire the transition.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Job ids already driven to a terminal state in this process. Guards against a
# second completion call (e.g. the LLM re-running synthesis, or a retry) issuing
# a redundant status transition. Process-scoped, matching the container's
# one-job-per-instance lifecycle.
_COMPLETED_JOB_IDS: set[str] = set()


def _resolve_job_manager(job_manager: Any) -> Any:
    """Resolve a JobManager — injected in tests, else built from the ATX env.

    Returns ``None`` (not raising) when the SDK or agent context is unavailable,
    i.e. outside the ATX runtime. Callers treat ``None`` as "nothing to do".
    """
    if job_manager is not None:
        return job_manager
    try:
        from agent_builder_sdk.agentic_framework.client_factory import (  # noqa: PLC0415
            get_agentic_api_client,
        )
        from agent_builder_sdk.agentic_framework.job_manager import JobManager  # noqa: PLC0415
        from agent_builder_sdk.env_var import get_agent_context_from_env  # noqa: PLC0415

        ctx = get_agent_context_from_env()
        return JobManager(
            workspace_id=ctx.workspace_id,
            job_id=ctx.job_id,
            agent_instance_id=ctx.agent_instance_id,
            client=get_agentic_api_client(),
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("complete_job: no JobManager available (outside ATX runtime?): %s", exc)
        return None


def complete_job(
    *,
    success: bool = True,
    error_message: str | None = None,
    failure_category: str | None = None,
    job_id: str | None = None,
    job_manager: Any = None,
) -> bool:
    """Drive the platform job to a terminal status (COMPLETED or FAILED).

    Best-effort and idempotent. Call this once, from the orchestrator, when the
    assessment pipeline reaches its deterministic end (after synthesis).

    Args:
        success: True -> ``JobStatus.COMPLETED``; False -> ``JobStatus.FAILED``.
        error_message: Failure detail, forwarded on the failure path.
        failure_category: Optional category (e.g. ``CUSTOMER_ERROR``,
            ``SERVICE_ERROR``); forwarded only when the SDK accepts it.
        job_id: Platform job id, used only for the idempotency guard. When None
            it is read from the agent context; if that is unavailable the guard
            is skipped (the call still runs once per invocation).
        job_manager: Injectable JobManager (tests). When None, one is built from
            the ATX env; a no-op outside the runtime.

    Returns:
        True if a terminal transition was issued, False if it was skipped
        (already completed, outside runtime) or failed.
    """
    guard_key = job_id or _job_id_from_env()
    if guard_key and guard_key in _COMPLETED_JOB_IDS:
        logger.info("complete_job: job %s already completed — skipping", guard_key)
        return False

    manager = _resolve_job_manager(job_manager)
    if manager is None:
        logger.info("complete_job: skipped (no JobManager; success=%s)", success)
        return False

    try:
        from agent_builder_sdk.agentic_framework.job_manager import JobStatus  # noqa: PLC0415

        if success:
            manager.update_job_status(JobStatus.COMPLETED)
            logger.info("Job marked COMPLETED")
        else:
            # failure_category is optional on the SDK signature; pass it only when
            # given so we don't depend on a kwarg that may not exist on older SDKs.
            if failure_category is not None:
                manager.update_job_status(
                    JobStatus.FAILED, error_message, failure_category=failure_category
                )
            else:
                manager.update_job_status(JobStatus.FAILED, error_message)
            logger.info("Job marked FAILED: %s", error_message)

        if guard_key:
            _COMPLETED_JOB_IDS.add(guard_key)
        return True
    except Exception:  # noqa: BLE001
        # Fail-open: a completion failure must not crash the final turn.
        logger.warning("complete_job: failed to update job status", exc_info=True)
        return False


def _job_id_from_env() -> str | None:
    """Best-effort platform job id from the agent context, for the guard key."""
    try:
        from agent_builder_sdk.env_var import get_agent_context_from_env  # noqa: PLC0415

        return str(get_agent_context_from_env().job_id)
    except Exception:  # noqa: BLE001
        return None
