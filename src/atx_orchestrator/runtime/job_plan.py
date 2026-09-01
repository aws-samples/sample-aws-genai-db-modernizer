"""Job plan API wrappers for AWS Transform WebApp progress reporting.

Thin boto3 wrappers around the ``transformagenticservice`` client's
``PutJobPlan`` and ``UpdateJobPlanStep`` operations. These give the WebApp
a visible progress panel that updates independent of the chat response cycle
(see F20 in ``docs-atx-poc/subagent-recipe.md``).

Design principles:
    * Additive only — no changes to A2A flow. This module can be imported
      or ignored without affecting existing subagent invocation.
    * Same request-context resolution pattern as ``a2a.py`` — reads
      ``WORKSPACE_ID``, ``JOB_ID``, ``AGENT_INSTANCE_ID``, ``AUTHORIZATION_TOKEN``
      via ``agent_builder_sdk.env_var.get_agent_context_from_env`` at runtime.
    * Graceful degradation — outside ATX runtime (local Mac, dry runs), all
      operations become no-ops with a debug log. Callers don't have to guard.
    * PlanStepStatus enum (from V3 discovery): ``NOT_STARTED``, ``IN_PROGRESS``,
      ``SUCCEEDED`` (not "COMPLETED"), ``PENDING_HUMAN_INPUT``, ``FAILED``,
      ``STOPPED``.

Endpoint: ``transformagenticservice.us-east-1.amazonaws.com`` — internal AWS
Transform API, only reachable from inside AgentCore runtime containers. Local
calls fail with ``EndpointConnectionError``; the wrapper catches and returns.
"""

from __future__ import annotations

import logging
from typing import Any

# Reuse a2a's request-context resolver — same env-var flow, same fallback.
from src.atx_orchestrator.a2a import _resolve_request_context

logger = logging.getLogger(__name__)

# PlanStepStatus enum values (verified via botocore model V3 discovery).
STATUS_NOT_STARTED = "NOT_STARTED"
STATUS_IN_PROGRESS = "IN_PROGRESS"
STATUS_SUCCEEDED = "SUCCEEDED"  # NOTE: not "COMPLETED" — Transform uses "SUCCEEDED"
STATUS_PENDING_HUMAN_INPUT = "PENDING_HUMAN_INPUT"
STATUS_FAILED = "FAILED"
STATUS_STOPPED = "STOPPED"


# Lazy-cached boto3 client. Created once per Python process.
_client: Any | None = None


def _get_client() -> Any | None:
    """Lazily create the transformagenticservice client. Returns None if unavailable.

    Uses the SDK's ``get_agentic_api_client()`` — this reads the internal endpoint
    from ``QT_AGENTIC_API_ENDPOINT`` env var (or constructs it from stage+region),
    which is the ONLY reachable route from inside AgentCore runtimes. A raw
    ``boto3.client('transformagenticservice')`` would use the public endpoint URL
    which is not reachable from the runtime containers (see F20/F21 debugging
    session — Phase 1 test-19 discovered this on the retest after subSteps fix).
    """
    global _client
    if _client is not None:
        return _client
    try:
        from agent_builder_sdk.agentic_framework.client_factory import (  # noqa: PLC0415
            get_agentic_api_client,
        )

        _client = get_agentic_api_client()
        return _client
    except Exception as exc:
        logger.debug("Failed to resolve transformagenticservice client via SDK: %s", exc)
        return None


def _has_context(request_context: dict[str, Any]) -> bool:
    """Return True if request_context has the minimum required fields.

    Used to decide whether to attempt the API call at all. When we're running
    outside ATX runtime (local, tests, or missing env vars), we skip.
    """
    if not request_context:
        return False
    job_meta = request_context.get("jobMetadata") or {}
    return bool(
        job_meta.get("jobId")
        and job_meta.get("workspaceId")
        and request_context.get("agentInstanceId")
        and request_context.get("authorizationToken")
    )


def put_job_plan(
    steps: list[dict[str, object]],
    request_context: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Declare the job plan; returns dict mapping ``stepLabel`` -> ``stepId``.

    Each step is a dict with keys ``stepLabel`` (our internal key), ``stepName``
    (displayed name), and ``description``. Submitted flat (no nested subSteps
    in this initial version).

    Uses ``override`` mode — replaces any existing plan for this job. Idempotent
    across replays of the same session; the last call wins.

    Args:
        steps: List of dicts, each with keys stepLabel, stepName, description.
        request_context: Optional overrideRequestContext. Defaults to
            env-resolved context from the ATX runtime.

    Returns:
        Dict mapping stepLabel -> server-assigned stepId. Empty dict on
        failure (any exception — call is best-effort progress reporting).
    """
    ctx = _resolve_request_context(request_context)
    if not _has_context(ctx):
        logger.debug("put_job_plan: no ATX context — skipping (running outside runtime?)")
        return {}

    client = _get_client()
    if client is None:
        logger.debug("put_job_plan: no client — skipping")
        return {}

    nodes = []
    for s in steps:
        node = {
            "stepLabel": s["stepLabel"],
            "stepName": s["stepName"],
            "description": s["description"],
        }
        # BUG FIX (Phase 1 test-19): subSteps has valid min length=1 per botocore
        # validation. Omit the field entirely when there are no sub-steps —
        # sending an empty list raises ParamValidationError before the request
        # is even attempted.
        sub_steps = s.get("subSteps")
        if sub_steps:
            node["subSteps"] = sub_steps
        nodes.append(node)

    try:
        response = client.put_job_plan(
            requestContext=ctx,
            plan={"nodes": nodes},
            mode={"override": {}},
        )
        mappings = response.get("mappings") or []
        result = {
            m["stepLabel"]: m["stepId"] for m in mappings if "stepLabel" in m and "stepId" in m
        }
        logger.info(
            "ATX put_job_plan OK: status=%s steps=%d mappings=%d",
            response.get("status"),
            len(nodes),
            len(result),
        )
        return result
    except Exception as exc:
        logger.warning(
            "ATX put_job_plan FAILED (best-effort, continuing): %s: %s",
            type(exc).__name__,
            str(exc)[:400],
        )
        return {}


def update_job_plan_step(
    step_id: str,
    status: str,
    description: str | None = None,
    request_context: dict[str, Any] | None = None,
) -> bool:
    """Update a single step's status. Returns True on success, False otherwise.

    Best-effort — never raises. Callers can ignore the return value.

    Args:
        step_id: The stepId returned by :func:`put_job_plan`.
        status: One of the ``STATUS_*`` constants above.
        description: Optional free-text status detail.
        request_context: Optional override.
    """
    if not step_id:
        logger.debug("update_job_plan_step: empty step_id — skipping")
        return False

    ctx = _resolve_request_context(request_context)
    if not _has_context(ctx):
        logger.debug("update_job_plan_step: no ATX context — skipping")
        return False

    client = _get_client()
    if client is None:
        logger.debug("update_job_plan_step: no client — skipping")
        return False

    plan_step: dict[str, Any] = {"stepId": step_id, "status": status}
    if description:
        plan_step["description"] = description

    try:
        client.update_job_plan_step(
            requestContext=ctx,
            planStep=plan_step,
        )
        logger.info(
            "ATX update_job_plan_step OK: stepId=%s status=%s",
            step_id[:16] + "..." if len(step_id) > 16 else step_id,
            status,
        )
        return True
    except Exception as exc:
        logger.warning(
            "ATX update_job_plan_step FAILED (best-effort): %s: %s (stepId=%s status=%s)",
            type(exc).__name__,
            str(exc)[:200],
            step_id[:16] + "..." if len(step_id) > 16 else step_id,
            status,
        )
        return False


def list_job_plan_steps(
    parent_step_id: str | None = None,
    request_context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """List the job plan's steps (best-effort). Returns [] outside the runtime.

    Each item carries at least ``stepId``, ``stepLabel``, ``status`` and
    ``parentStepId``. Pass ``parent_step_id`` to list only the children of a
    step (e.g. the analysis parent's per-engine sub-steps). Paginates.

    Used by consolidated subagents to resolve their own sub-step ids across
    process boundaries: the orchestrator declares the plan (and holds the
    in-process registry), but a subagent runs in a different runtime, so it
    reads the step ids back from the server instead.
    """
    ctx = _resolve_request_context(request_context)
    if not _has_context(ctx):
        logger.debug("list_job_plan_steps: no ATX context — skipping")
        return []

    client = _get_client()
    if client is None:
        logger.debug("list_job_plan_steps: no client — skipping")
        return []

    steps: list[dict[str, Any]] = []
    next_token: str | None = None
    try:
        while True:
            kwargs: dict[str, Any] = {"requestContext": ctx}
            if parent_step_id:
                kwargs["parentStepId"] = parent_step_id
            if next_token:
                kwargs["nextToken"] = next_token
            response = client.list_job_plan_steps(**kwargs)
            steps.extend(response.get("steps") or [])
            next_token = response.get("nextToken")
            if not next_token:
                break
        logger.info("ATX list_job_plan_steps OK: %d steps", len(steps))
        return steps
    except Exception as exc:
        logger.warning(
            "ATX list_job_plan_steps FAILED (best-effort): %s: %s",
            type(exc).__name__,
            str(exc)[:200],
        )
        return []


# ═════════════════════════════════════════════════════════════════════════════
# Module-level step registry
# ═════════════════════════════════════════════════════════════════════════════
# Maps our internal phase names (e.g. "analysis_dynamodb") to the stepIds
# returned by PutJobPlan. Populated by declare_pipeline_plan (an orchestrator
# tool); read by run_*_via_a2a tools to update status before/after each phase.
#
# Lives at module scope because tools in tools.py can't share instance state
# with the orchestrator. Reset via clear_step_registry() when a new pipeline
# starts (called from declare_pipeline_plan).
#
# Failure mode is graceful: if a run_*_via_a2a tool looks up a phase and gets
# None, update_job_plan_step handles the empty step_id gracefully (returns False,
# logs debug). So an accidentally-empty registry doesn't break the A2A flow.

_step_registry: dict[str, str] = {}


def register_steps(mappings: dict[str, str]) -> None:
    """Register stepLabel -> stepId mappings after PutJobPlan.

    Overwrites the previous registry — a new PutJobPlan replaces the previous plan.
    """
    global _step_registry
    _step_registry = dict(mappings)
    logger.info("ATX job_plan registry updated with %d phases", len(_step_registry))


def get_step_id(phase_name: str) -> str:
    """Look up the stepId for a phase. Returns empty string if not registered."""
    return _step_registry.get(phase_name, "")


def clear_step_registry() -> None:
    """Reset the registry — used when starting a new pipeline."""
    global _step_registry
    _step_registry = {}


def register_steps_from_server(parent_step_id: str | None = None) -> dict[str, str]:
    """Populate the registry from the server's plan (for out-of-process agents).

    Lists the plan via :func:`list_job_plan_steps`, builds a
    ``stepLabel -> stepId`` map, registers it, and returns it. Empty outside the
    ATX runtime. Lets a consolidated subagent resolve the sub-step ids the
    orchestrator declared in a different process, so its ``mark_step_*`` calls
    land on the right steps.
    """
    mappings = {
        s["stepLabel"]: s["stepId"]
        for s in list_job_plan_steps(parent_step_id)
        if s.get("stepLabel") and s.get("stepId")
    }
    if mappings:
        register_steps(mappings)
    return mappings


# ═════════════════════════════════════════════════════════════════════════════
# Convenience helpers — keeps A2A tools clean (2 lines added per phase)
# ═════════════════════════════════════════════════════════════════════════════
# Each helper looks up the stepId by phase_name and calls update_job_plan_step.
# Empty step_id (registry miss or unregistered phase) is handled silently by
# update_job_plan_step, so calls are always safe.


def mark_step_running(phase_name: str, detail: str = "") -> None:
    """Mark a phase IN_PROGRESS by phase_name lookup. Safe if unregistered.

    ``detail``, when given, is attached as the step description (truncated) so the
    WebApp panel can show a live note for the running phase.
    """
    step_id = get_step_id(phase_name)
    if step_id:
        desc = detail[:200] if detail else None
        update_job_plan_step(step_id, STATUS_IN_PROGRESS, description=desc)


def mark_step_succeeded(phase_name: str, detail: str = "") -> None:
    """Mark a phase SUCCEEDED by phase_name lookup. Safe if unregistered.

    ``detail``, when given, is attached as the step description (truncated) so the
    WebApp panel can show what the phase found (e.g. triage's detected signals).
    """
    step_id = get_step_id(phase_name)
    if step_id:
        desc = detail[:200] if detail else None
        update_job_plan_step(step_id, STATUS_SUCCEEDED, description=desc)


def mark_step_failed(phase_name: str, reason: str = "") -> None:
    """Mark a phase FAILED by phase_name lookup. Safe if unregistered."""
    step_id = get_step_id(phase_name)
    if step_id:
        # Truncate reason to keep the API payload small.
        desc = reason[:200] if reason else None
        update_job_plan_step(step_id, STATUS_FAILED, description=desc)


def mark_step_skipped(phase_name: str, reason: str = "") -> None:
    """Mark a phase STOPPED (not analyzed) by phase_name lookup, carrying the reason.

    Used for candidate engines triage deliberately did not analyze (e.g. the
    non-matching Aurora variant for a given source engine). STOPPED is the closest
    terminal status to "considered but not run"; NOT_STARTED renders as still
    pending. The reason rides along as the step description so the WebApp can show
    why the engine was skipped. Safe if unregistered.
    """
    step_id = get_step_id(phase_name)
    if step_id:
        desc = reason[:200] if reason else None
        update_job_plan_step(step_id, STATUS_STOPPED, description=desc)
