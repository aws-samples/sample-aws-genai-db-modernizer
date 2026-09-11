"""Strands tools that wrap the existing deterministic pipeline.

Each tool maps to a phase (or a group of phases) in the LocalOrchestrator.
The orchestrator LLM calls these — no Bedrock happens inside the pipeline
itself unless the project's own LLM-optional phases are enabled.

Y-3 (F8 fix): The Collect and Triage phases run in DEPLOYED SUBAGENTS via
the AWS Transform A2A protocol. The LLM invokes ``run_collect_via_a2a`` /
``run_triage_via_a2a`` and the tools resolve the subagent BY NAME using
``a2a.invoke_and_wait`` — no ``subagent_instance_id`` is needed at the LLM
layer.

Every pipeline phase runs in a DEPLOYED SUBAGENT over A2A; there are no
in-process phase tools. The only non-A2A tools here are ``get_job_status``
and ``get_synthesis_report``, which read progression and artifacts directly.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable

from strands.tools import tool

from src.atx_orchestrator.a2a import A2AError, invoke_and_wait
from src.atx_orchestrator.core import make_orchestrator as _make_orchestrator
from src.atx_orchestrator.core import make_store as _make_store
from src.atx_orchestrator.runtime.job_plan import (
    clear_step_registry,
    mark_step_failed,
    mark_step_pending_human_input,
    mark_step_running,
    mark_step_skipped,
    mark_step_succeeded,
    put_job_plan,
    register_steps,
)
from src.contracts.phase_models import Phase, PhaseStatus

logger = logging.getLogger(__name__)


def _platform_job_id(supplied: str) -> str:
    """Return the real AWS Transform platform job id, ignoring the LLM-supplied one.

    ``job_id`` is an argument on every pipeline ``@tool``, so the orchestrator LLM
    fills it in — and it fabricates a plausible slug (e.g. ``discourse-20250612-120000``)
    rather than the platform job UUID it never reliably echoes. Every phase then keys
    its ``{db}/{job}/`` artifact tree on that slug; a fabricated id is internally
    consistent within one run but does not match the platform job, and two phases can
    drift to different slugs. The real id lives in the agent runtime context, so use
    that as the single source of truth and fall back to ``supplied`` only outside the
    ATX runtime (local/dev/reference harness, where there is no agent context).
    """
    try:
        from agent_builder_sdk.env_var import get_agent_context_from_env

        ctx = get_agent_context_from_env()
        platform_id = getattr(ctx, "job_id", "") or ""
    except Exception as exc:  # noqa: BLE001 -- not in the ATX runtime; keep the supplied id
        logger.debug("no ATX agent context (%s); using supplied job_id=%s", exc, supplied)
        return supplied
    if platform_id and platform_id != supplied:
        logger.info(
            "overriding LLM-supplied job_id=%r with platform job id %r",
            supplied,
            platform_id,
        )
    return platform_id or supplied


# Subagent naming. Every A2A tool resolves its target as
# f"{_AGENT_PREFIX}-<phase>", so one image can drive either generation:
#   v1 (deployed):  db-modernization-<phase>          — the default
#   v2:             db-modernization-v2-<phase>       — set AGENT_NAME_PREFIX
# This matters for more than tidiness. The v1 analysis-dynamodb and
# analysis-documentdb runtimes carry LLM_MODE=bedrock, so a v2 orchestrator
# calling v1 subagents would silently reintroduce the 38-minute and ~63-minute
# Opus advisor passes that step 2b removed.
_AGENT_PREFIX = os.environ.get("AGENT_NAME_PREFIX", "db-modernization")


@tool
def declare_pipeline_plan(job_id: str, database_name: str) -> str:
    """Declare the database modernization pipeline plan for visible progress tracking.

    Call this FIRST, before invoking any pipeline phase. It registers the
    pipeline phases with the AWS Transform WebApp job-plan API, so the user
    sees a **progress panel with per-phase status** that updates in real time
    as each phase runs — independent of the chat response cycle.

    Without this call, the pipeline still works but the WebApp has no progress
    UI for the user during long-running phases (see F20 in
    ``docs-atx-poc/subagent-recipe.md``).

    The declared plan includes 9 phases:
      1. collector — ingest customer's offline collection
      2. triage — select candidate target engines
      3-8. analysis_{dynamodb,documentdb,elasticache,opensearch,aurora_postgresql,aurora_mysql}
      9. assignment — route queries to engines

    Unused analysis phases (engines not selected by triage) stay at
    ``NOT_STARTED`` — that's expected and shows the user which engines
    were skipped.

    Best-effort — if the API call fails (running outside ATX runtime, or
    endpoint unavailable) the return value indicates so but the pipeline
    is unaffected.

    Args:
        job_id: The customer's job identifier.
        database_name: The database being assessed.

    Returns:
        JSON string with status (``declared`` or ``no_plan_declared``) and
        the list of registered phases.
    """
    job_id = _platform_job_id(job_id)
    logger.info(
        "ATX declare_pipeline_plan job_id=%s db=%s",
        job_id,
        database_name,
    )

    # Reset any prior registry (new pipeline session).
    clear_step_registry()

    # dict[str, object] (not str): the "analysis" node carries a nested subSteps
    # list, so values are no longer all strings.
    steps: list[dict[str, object]] = [
        {
            "stepLabel": "collector",
            "stepName": "Collect Database Schema and Queries",
            "description": "Extract schema and query patterns from the uploaded database collection.",
        },
        {
            "stepLabel": "triage",
            "stepName": "Triage: Select Candidate Engines",
            "description": "Identify candidate AWS database engines based on the workload's patterns.",
        },
        # One consolidated analysis agent (ADR-024) runs every selected engine
        # in-process and ticks these per-engine sub-steps as it goes, so the
        # WebApp shows a nested checklist inside the Analysis box. Sub-step labels
        # match core's analysis_<target_database> phase names.
        {
            "stepLabel": "analysis",
            "stepName": "Analyze Candidate Engines",
            "description": "Score tables and queries against each selected AWS engine.",
            "subSteps": [
                {
                    "stepLabel": "analysis_dynamodb",
                    "stepName": "Analyze for DynamoDB",
                    "description": "Score tables and queries for DynamoDB suitability.",
                },
                {
                    "stepLabel": "analysis_documentdb",
                    "stepName": "Analyze for DocumentDB",
                    "description": "Score tables and queries for DocumentDB suitability.",
                },
                {
                    "stepLabel": "analysis_elasticache",
                    "stepName": "Analyze for ElastiCache",
                    "description": "Score cache-suitable workload patterns.",
                },
                {
                    "stepLabel": "analysis_opensearch",
                    "stepName": "Analyze for OpenSearch",
                    "description": "Score search-suitable workload patterns.",
                },
                {
                    "stepLabel": "analysis_aurora_postgresql",
                    "stepName": "Analyze for Aurora PostgreSQL",
                    "description": "Score relational workloads for Aurora PostgreSQL.",
                },
                {
                    "stepLabel": "analysis_aurora_mysql",
                    "stepName": "Analyze for Aurora MySQL",
                    "description": "Score relational workloads for Aurora MySQL.",
                },
            ],
        },
        {
            "stepLabel": "assignment",
            "stepName": "Route Queries to Engines",
            "description": "Route each query to the best-fit AWS engine.",
        },
        # Reality Check runs inside the assessment-core agent, right after
        # assignment (ADR-026). The agent ticks this step. It consolidates
        # redundant engines and, when it does, writes a v2 assignment that schema
        # design and synthesis pick up automatically.
        {
            "stepLabel": "reality_check",
            "stepName": "Reality Check: Consolidate Engines",
            "description": "Eliminate redundant engines a surviving engine can absorb.",
        },
        # Human gate (ADR-028): the customer reviews the query-to-engine routing and
        # approves or edits it before the long schema-design phase runs. The
        # orchestrator marks this PENDING_HUMAN_INPUT while it waits, then SUCCEEDED
        # once the customer approves. Schema design is blocked until then.
        {
            "stepLabel": "assignment_review",
            "stepName": "Review Query Routing",
            "description": "Customer reviews and approves where each query is routed before design.",
        },
        # Schema design, one per target engine. VISUAL GROUPING ONLY: the six
        # engines stay separate parallel LLM subagents (see ADR-025), but the
        # plan nests them as sub-steps under one "Design Target Schemas" box so
        # the panel shows a collapsible checklist, mirroring "Analyze Candidate
        # Engines". The sub-step labels (schema_<engine>) are unchanged, so the
        # per-engine schema tools keep ticking them. The parent "schema" step is
        # marked running by any schema tool on entry and succeeded by the
        # synthesis tool (which only runs once every schema agent has finished).
        {
            "stepLabel": "schema",
            "stepName": "Design Target Schemas",
            "description": "Design tables and access patterns for each selected AWS engine.",
            "subSteps": [
                {
                    "stepLabel": "schema_dynamodb",
                    "stepName": "DynamoDB",
                    "description": "Design tables and access patterns for the DynamoDB target.",
                },
                {
                    "stepLabel": "schema_documentdb",
                    "stepName": "DocumentDB",
                    "description": (
                        "Design collections and access patterns for the DocumentDB target."
                    ),
                },
                {
                    "stepLabel": "schema_elasticache",
                    "stepName": "ElastiCache",
                    "description": (
                        "Design key structures and access patterns for the ElastiCache target."
                    ),
                },
                {
                    "stepLabel": "schema_opensearch",
                    "stepName": "OpenSearch",
                    "description": (
                        "Design index mappings and access patterns for the OpenSearch target."
                    ),
                },
                {
                    "stepLabel": "schema_aurora_postgresql",
                    "stepName": "Aurora PostgreSQL",
                    "description": "Assess schema design coverage for the Aurora PostgreSQL target.",
                },
                {
                    "stepLabel": "schema_aurora_mysql",
                    "stepName": "Aurora MySQL",
                    "description": "Assess schema design coverage for the Aurora MySQL target.",
                },
            ],
        },
        # Synthesis was absent from this list until 2026-08-26, so its
        # mark_step_* calls resolved to an unregistered phase and were dropped
        # silently. The phase ran; only its progress was invisible.
        {
            "stepLabel": "synthesis",
            "stepName": "Produce the Assessment Report",
            "description": (
                "Rank engines, map tables, group queries, compare cost, assess risk, "
                "and recommend a target architecture."
            ),
        },
    ]

    mappings = put_job_plan(steps)
    if not mappings:
        return json.dumps(
            {
                "status": "no_plan_declared",
                "phases": 0,
                "note": (
                    "Job plan API unreachable or ATX request context missing. "
                    "Pipeline will still run correctly but WebApp progress panel "
                    "will not display step-by-step status."
                ),
            }
        )

    register_steps(mappings)
    return json.dumps(
        {
            "status": "declared",
            "phases": len(mappings),
            "phase_labels": list(mappings.keys()),
        }
    )


@tool
def get_job_status(job_id: str, database_name: str) -> str:
    """Get the current phase progression status for a job.

    Args:
        job_id: Unique job identifier.
        database_name: Source database name.

    Returns:
        JSON string with per-phase status values.
    """
    job_id = _platform_job_id(job_id)
    store = _make_store()
    orch = _make_orchestrator(store)

    progression = orch.get_progression(job_id)
    from src.contracts.phase_models import Phase

    return json.dumps(
        {
            "job_id": job_id,
            "current_phase": progression.current_phase.value,
            "phases": {p.value: progression.phases[p].status.value for p in Phase},
        }
    )


@tool
def get_synthesis_report(job_id: str, database_name: str) -> str:
    """Read and return the synthesis report for a completed assessment.

    Args:
        job_id: Unique job identifier.
        database_name: Source database name.

    Returns:
        The synthesis report as a JSON string, or an error if not available.
    """
    job_id = _platform_job_id(job_id)
    store = _make_store()
    report_key = f"{database_name}/{job_id}/synthesis/report.json"

    if not store.exists(report_key):
        return json.dumps(
            {
                "error": "Report not available yet. Run synthesis first.",
                "job_id": job_id,
                "report_artifact": report_key,
            }
        )

    report = store.read_json(report_key)
    return json.dumps(report)


@tool
def present_assignment_review(job_id: str, database_name: str) -> str:
    """Present the query-to-engine routing to the customer for review and approval.

    Call this AFTER the assessment core (which includes Reality Check) and BEFORE
    any schema-design tool. It renders the effective assignment as a review
    document (routing grouped by engine, with the rationale, plus one editable
    table), stages it on the store, publishes it to the customer's Artifacts panel
    so they can open it, and marks the review gate as awaiting the customer.

    Show the returned ``review_markdown`` to the customer in chat and ask them to
    either approve it as-is or reply with the edited routing table. Then call
    ``apply_assignment_edits`` with their reply. Schema design is blocked until the
    customer approves.

    Returns JSON with ``status`` ("awaiting_review"), ``assignment_version``,
    ``review_markdown`` (present this to the customer), and ``review_artifact``.
    """
    job_id = _platform_job_id(job_id)
    from src.agents.referee.assignment_review import render_assignment_review
    from src.contracts.assignment_models import Assignment
    from src.storage.assignment_versioning import (
        assignment_artifact_path,
        resolve_effective_assignment_version,
    )

    store = _make_store()
    version = resolve_effective_assignment_version(store, database_name, job_id)
    if version == 0:
        return json.dumps(
            {
                "error": "No assignment found to review. Run the assessment core first.",
                "job_id": job_id,
            }
        )

    assignment = Assignment.model_validate(
        store.read_json(assignment_artifact_path(database_name, job_id, version))
    )
    review_md = render_assignment_review(assignment)

    review_key = f"{database_name}/{job_id}/assignment/review/v{version}.md"
    try:
        store.write_text(review_key, review_md, "text/markdown")
    except Exception:  # noqa: BLE001 - staging is best-effort; the markdown is returned regardless
        logger.warning("ATX: could not stage review doc at %s", review_key, exc_info=True)

    # Publish so the customer can open/download the review in the Artifacts panel.
    # Use CUSTOMER_OUTPUT: it is the category the WebApp panel actually renders
    # (the synthesis deliverables use it, verified). HITL_FROM_AGENT uploads
    # succeed but do not surface in the panel, so the customer cannot find them.
    try:
        from src.atx_orchestrator.runtime import artifacts as _artifacts

        _artifacts.publish(
            [
                (
                    review_md.encode("utf-8"),
                    "MARKDOWN",
                    f"Query Routing Review — {database_name}",
                    "CUSTOMER_OUTPUT",
                    f"assignment-review-v{version}.md",
                )
            ]
        )
    except Exception:  # noqa: BLE001 - the chat copy is the primary channel
        logger.warning("ATX: could not publish review artifact", exc_info=True)

    mark_step_pending_human_input(
        "assignment_review", "Awaiting customer approval of query routing."
    )
    _mark_assignment_review(job_id, PhaseStatus.AWAITING_REVIEW)

    return json.dumps(
        {
            "status": "awaiting_review",
            "job_id": job_id,
            "assignment_version": version,
            "review_artifact": review_key,
            "review_markdown": review_md,
        }
    )


@tool
def apply_assignment_edits(job_id: str, database_name: str, edited_markdown: str = "") -> str:
    """Apply the customer's assignment-review reply and open the schema-design gate.

    Call this after ``present_assignment_review`` with the customer's response:
      - Approved as-is: pass an empty ``edited_markdown`` (or the unchanged document).
      - Edited: pass the full edited review markdown they replied with (it must
        still contain the marker-bounded routing table).

    Parses the edited table, applies only the changes as a new customer_modified
    assignment version via the shared override path, and marks the review gate
    approved so schema design may run. If the edit cannot be parsed or fails
    validation, returns an error to relay to the customer and does NOT approve
    (nothing is applied).

    Returns JSON with ``status`` ("approved" or "invalid_edit"), ``changed``,
    ``applied_overrides``, and ``assignment_version`` (effective after any edit).
    """
    job_id = _platform_job_id(job_id)
    from src.agents.referee.assignment_overrides import (
        AssignmentOverrideError,
        AssignmentValidationFailed,
        NoAssignmentFound,
        UnknownQuery,
        apply_assignment_overrides,
    )
    from src.agents.referee.assignment_review import (
        REVIEW_BEGIN_MARKER,
        ReviewParseError,
        diff_review_rows,
        parse_assignment_review,
    )
    from src.contracts.assignment_models import Assignment, AssignmentSource
    from src.storage.assignment_versioning import (
        assignment_artifact_path,
        resolve_effective_assignment_version,
    )

    store = _make_store()
    version = resolve_effective_assignment_version(store, database_name, job_id)
    if version == 0:
        return json.dumps(
            {"error": "No assignment found. Run the assessment core first.", "job_id": job_id}
        )

    changed = False
    applied = 0
    effective_version = version
    stripped = (edited_markdown or "").strip()

    # Only parse when the customer actually returned the table. An empty reply, or
    # one without the markers, is "approve as-is" (no new version).
    if stripped and REVIEW_BEGIN_MARKER in stripped:
        try:
            current = Assignment.model_validate(
                store.read_json(assignment_artifact_path(database_name, job_id, version))
            )
            overrides = diff_review_rows(current, parse_assignment_review(stripped))
        except ReviewParseError as e:
            return json.dumps(
                {
                    "status": "invalid_edit",
                    "error": str(e),
                    "job_id": job_id,
                    "message": (
                        "The edited routing table could not be read. Ask the customer to "
                        "correct it and resend; nothing was applied."
                    ),
                }
            )
        if overrides:
            try:
                result = apply_assignment_overrides(
                    store,
                    database_name,
                    job_id,
                    overrides,
                    source=AssignmentSource.CUSTOMER_GATE,
                )
            except AssignmentValidationFailed as e:
                return json.dumps(
                    {
                        "status": "invalid_edit",
                        "error": "; ".join(e.errors),
                        "job_id": job_id,
                        "message": (
                            "The requested routing is not valid (for example an engine that "
                            "did not analyze the query). Nothing was applied."
                        ),
                    }
                )
            except (UnknownQuery, NoAssignmentFound, AssignmentOverrideError) as e:
                return json.dumps({"status": "invalid_edit", "error": str(e), "job_id": job_id})
            changed = True
            applied = len(overrides)
            effective_version = result.assignment.version

    # Record approval — this opens the schema-design gate. Same .meta phase signal
    # the web path checks, so no new approval artifact is introduced (ADR-028).
    _mark_assignment_review(job_id, PhaseStatus.COMPLETED)
    detail = "Customer approved query routing."
    if changed:
        detail += f" {applied} change(s) applied (assignment v{effective_version})."
    mark_step_succeeded("assignment_review", detail)

    return json.dumps(
        {
            "status": "approved",
            "job_id": job_id,
            "changed": changed,
            "applied_overrides": applied,
            "assignment_version": effective_version,
        }
    )


# =============================================================================
# A2A-wired variants — invoke deployed subagents over the AWS Transform Agentic
# API. Under Y-3 (F8 fix), these are the ONLY variants registered in
# PIPELINE_TOOLS. The LLM invokes them by phase name (``run_collect_via_a2a`` /
# ``run_triage_via_a2a``); the tools resolve the subagent BY NAME
# (``db-modernization-collector`` / ``db-modernization-triage``) via
# ``a2a.invoke_and_wait``. Subagents must already be deployed to Bedrock
# AgentCore and registered in the AWS Transform Agent Registry with matching
# names. Payload shape matches what ``subagent_base.parse_invocation`` accepts.


def _run_phase_via_a2a(
    agent_suffix: str,
    step: str,
    label: str,
    job_id: str,
    database_name: str,
    message: str,
    timeout: float | None = None,
    on_success: Callable[[dict], None] | None = None,
) -> str:
    """Shared invoke/mark/error body for the phase A2A tools.

    Resolves the subagent as ``f"{_AGENT_PREFIX}-{agent_suffix}"``, marks the
    plan ``step`` running/succeeded/failed around ``invoke_and_wait``, and turns
    any ``A2AError`` into the standard ``{"error": ...}`` JSON dict rather than
    raising. ``label`` is the human phase word used in the log and error text.
    ``timeout`` is passed through only when set (the two LLM-heavy analyses need
    a longer ceiling). ``on_success`` runs with the completion payload before the
    JSON is returned — synthesis uses it to publish its rendered deliverables.

    ``step`` may be empty. The assessment-core agent spans several plan steps
    and ticks them itself from inside the subagent (it holds the per-phase
    progress), so its tool passes ``step=""`` and this wrapper marks nothing —
    otherwise a single tool-level step would fight the agent's per-phase ticks.
    """
    agent_id = f"{_AGENT_PREFIX}-{agent_suffix}"
    logger.info(
        "ATX: %s via A2A agent=%s job_id=%s db=%s",
        label,
        agent_id,
        job_id,
        database_name,
    )
    if step:
        mark_step_running(step)
    try:
        if timeout is None:
            payload = invoke_and_wait(agent_id, message)
        else:
            payload = invoke_and_wait(agent_id, message, timeout=timeout)
    except A2AError as e:
        logger.error("ATX %s FAILED: %s: %s", label, type(e).__name__, e)
        if step:
            mark_step_failed(step, str(e))
        return json.dumps(
            {
                "error": f"A2A {label} failed: {e}",
                "error_type": type(e).__name__,
                "job_id": job_id,
                "agent_id": agent_id,
            }
        )
    if step:
        mark_step_succeeded(step)
    if on_success is not None:
        on_success(payload)
    return json.dumps(payload)


@tool
def run_assessment_core_via_a2a(
    job_id: str,
    database_name: str,
) -> str:
    """Run the whole assessment front-half in ONE consolidated subagent over A2A.

    Invokes the ``assessment-core`` subagent (ADR-025, ADR-026), which runs
    Collect -> Triage -> Analyze (every triage-selected engine) -> Assign ->
    Reality Check in a single process, in order. It replaced the four separate
    collect / triage / analysis / assignment tools and now also runs the
    CTO-level Reality Check consolidation: call this ONCE, after
    ``declare_pipeline_plan``. Collect/Triage/Analyze/Assign are deterministic;
    Reality Check adds one Bedrock pass. Each phase writes the same artifacts as
    before:

      - ``<db>/<job>/collector/output.json``
      - ``<db>/<job>/referee-triage/triage.json``
      - ``<db>/<job>/analysis-<engine>/analysis.json`` (+ trace, + ER diagram)
      - ``<db>/<job>/assignment/v1/assignment.json``
      - ``<db>/<job>/reality-check/output.json`` (+ ``assignment/v2/`` when it consolidates)

    The customer's uploaded offline collection is located AUTOMATICALLY: this tool
    discovers the file the customer uploaded through the WebApp (it lands under the
    job's ``User Uploads/`` prefix) and hands the agent its key. You do NOT pass,
    construct, or ask for a storage path.

    The subagent ticks its own plan steps (collector, triage, analysis with nested
    per-engine sub-steps, assignment, reality_check) as it progresses, so the
    WebApp panel shows live per-phase and per-engine status; this tool therefore
    does not mark a single step itself. Subagent NAME: ``<prefix>-assessment-core``.

    Args:
        job_id: Unique job identifier.
        database_name: Source database name used to namespace artifacts.

    Returns:
        JSON string with the merged completion payload (collector, triage,
        analysis, assignment, reality_check summaries, ``effective_assignment_version``,
        plus a ``summary_for_chat`` block with the detected signals, selected
        engines, query distribution, and consolidation to narrate), or an error
        dict if the A2A round-trip failed.
    """
    # Resolve the customer's uploaded offline collection here, in the orchestrator:
    # it reliably holds the Transform job context (workspace_id + platform job UUID
    # + agent instance), so _discover_uploaded_input can find the WebApp upload via
    # the ATX Artifact API (ListArtifacts CUSTOMER_INPUT) — which is account/bucket
    # agnostic, unlike listing our own S3_BUCKET — download it, and stage it at the
    # seed key. It returns that key. Discovery is the single source of truth for the
    # path; the LLM never supplies one. Outside the ATX runtime (dev/reference
    # harness) discovery returns None, input_key stays "", and the collector step
    # falls back to a pre-staged seed key. An ambiguous upload (more than one
    # CUSTOMER_INPUT JSON) raises with a clear message rather than picking one.
    from src.atx_orchestrator.core import _discover_uploaded_input

    job_id = _platform_job_id(job_id)
    input_key = _discover_uploaded_input(_make_store(), job_id, database_name) or ""
    if input_key:
        logger.info("ATX assessment-core: using customer upload staged at %s", input_key)
    else:
        logger.warning(
            "ATX assessment-core: no customer upload discovered for job_id=%s; "
            "passing empty input_key. The collect step will fall back to the seed "
            "key and fail if none is staged. See upload-discovery log above for what "
            "the Artifact API returned.",
            job_id,
        )
    message = json.dumps(
        {
            "job_id": job_id,
            "database_name": database_name,
            "input_key": input_key,
        }
    )
    # step="" — the agent ticks collector/triage/analysis/assignment/reality_check
    # itself; a single tool-level step would collide with the per-phase progress.
    return _run_phase_via_a2a(
        agent_suffix="assessment-core",
        step="",
        label="assessment-core",
        job_id=job_id,
        database_name=database_name,
        message=message,
    )


@tool
def run_synthesis_via_a2a(
    job_id: str,
    database_name: str,
) -> str:
    """Run Referee-Synthesis by invoking a deployed subagent over A2A.

    Requires the assessment core (Collector + Triage + Analysis + Assignment +
    Reality Check) to have run first. Schema-design is an optional enrichment,
    not a prerequisite — synthesis proceeds without it, though ``query_groups``
    will be empty until schema-design has run, since those are built from the
    schema output's access patterns.

    Produces the consolidated report a customer actually reads: engine ranking,
    table mappings, TCO comparison, risk assessment, and a recommended
    architecture. Deterministic-first, then one Bedrock call for the executive
    summary — matching core-modernizer, which invokes ``run_synthesis`` without
    an ``llm_mode`` argument and so takes its ``"bedrock"`` default.

    The assignment version is resolved automatically (the latest one on the
    store: the consolidated v2 when Reality Check ran, else v1); you do not pass
    it. Writes ``<db>/<job>/synthesis/v<N>/report.json``.

    Args:
        job_id: Unique job identifier.
        database_name: Source database name.

    Returns:
        JSON string with the subagent's completion payload (engines_ranked,
        top_engine, architecture_type, recommended_databases, table_mappings,
        query_groups, overall_risk_level, has_executive_summary,
        report_artifact).
    """
    job_id = _platform_job_id(job_id)
    # Resolve the version in Python, not via the LLM (ADR-026): the latest
    # assignment (v2 when Reality Check consolidated, else v1).
    assignment_version = _effective_assignment_version(job_id, database_name)
    message = json.dumps(
        {
            "job_id": job_id,
            "database_name": database_name,
            "assignment_version": assignment_version,
        }
    )
    # Synthesis only runs once every schema-design agent has finished, so this is
    # the deterministic point to close out the parent "Design Target Schemas" box
    # (the six engines run as separate parallel agents with no single owner to
    # mark the parent, unlike the in-process analysis phase). Before closing the
    # parent, mark the sub-steps for engines that were never selected as skipped,
    # so they show the "not run" state instead of a perpetual pending clock.
    _mark_unselected_schema_steps_skipped(job_id, database_name)
    mark_step_succeeded("schema")
    result = _run_phase_via_a2a(
        agent_suffix="synthesis",
        step="synthesis",
        label="synthesis",
        job_id=job_id,
        database_name=database_name,
        message=message,
        on_success=lambda payload: _publish_synthesis_deliverables(job_id, database_name, payload),
    )
    # Synthesis is the last step of the assessment pipeline. When it succeeds the
    # whole job is done, so mark the platform JOB terminal — the platform does not
    # roll the job up when only plan steps and subagent instances finish, so
    # without this the job stays EXECUTING forever. Only the orchestrator owns
    # this transition; it is idempotent and fail-open. A synthesis error is left
    # non-terminal on purpose so the LLM can retry.
    if not _is_error_result(result):
        _complete_job_success(job_id)
    return result


def _publish_synthesis_deliverables(job_id: str, database_name: str, payload: dict) -> None:
    """Render the audience-shaped deliverables from the synthesis report.json and
    publish them as CUSTOMER_OUTPUT.

    The orchestrator owns this, not the subagent, because it owns the synthesis
    plan step. Five artifacts reach the WebApp Artifacts panel: Decision Report
    (executive HTML), Engineering Report (build-team Markdown), Assessment Data
    (the raw report JSON), Interactive Analysis Report (HTML) and Executive
    Summary Report (PDF). Each rendered deliverable is also written to our own
    S3 bucket, which is the system of record and survives the customer stopping
    the job; report.json is already there (the subagent wrote it). The executive
    summary's editable ``.pptx`` is written to S3 too but deliberately not
    registered -- the PDF is the delivery.

    Every item carries an explicit download filename (the 5th tuple element) so
    the artifact publishes EXTERNAL and saves under a human-readable name rather
    than its UUID. Dropping it regresses cross-account visibility silently: the
    publishing account still sees INTERNAL artifacts, so nothing looks wrong.

    Every deliverable is rendered deterministically from artifacts already on the
    store -- no LLM call happens here, so two runs over the same report.json
    produce byte-comparable content.

    Entirely non-fatal: a synthesis whose report is durable in S3 must not fail
    over a rendering or registration call. ``artifacts.publish`` never raises on
    its own; this guard covers the render and the S3 writes.
    """
    # The subagent wraps its return dict under a "response" key before serialising
    # it to agentOutput.serializedPayload (subagent_base ~L274), so report_artifact
    # is nested there, not at the top level of what invoke_and_wait returns. Fall
    # back to a flat shape defensively.
    resp = payload.get("response")
    inner = resp if isinstance(resp, dict) else payload
    report_key = inner.get("report_artifact")
    if not report_key:
        logger.warning("ATX synthesis: payload has no report_artifact; skipping deliverables")
        return
    try:
        from src.atx_orchestrator.runtime import artifacts as _artifacts

        store = _make_store()
        report = store.read_json(report_key)
        base = report_key.rsplit("/", 1)[0]
        trust = any(r.get("schema_design_available") for r in (report.get("ranking") or []))

        def _prov(artifact: str, ext: str) -> dict:
            return _artifacts.provenance(
                report, artifact, ext, job_id=job_id, source_artifact=report_key
            )

        decision_prov = _prov("decision-report", "html")
        engineering_prov = _prov("engineering-report", "md")
        data_prov = _prov("assessment-data", "json")

        decision_html = _artifacts.render_decision_report_html(
            report, trust_generated_summary=trust, prov=decision_prov
        )
        engineering_md = _artifacts.render_engineering_report_md(report, prov=engineering_prov)
        # The published JSON is wrapped with an identity envelope; the object at
        # report_key is NOT touched. That one is the system of record and is
        # validated against the synthesis contract on re-read, so injecting a key
        # into it would risk failing validation for the sake of a filename.
        data_json = json.dumps({"_artifact": data_prov, **report}, indent=2)

        # S3-first: our bucket is the system of record (survives job stop).
        store.write_text(f"{base}/{decision_prov['filename']}", decision_html, "text/html")
        store.write_text(f"{base}/{engineering_prov['filename']}", engineering_md, "text/markdown")

        items: list = [
            (
                decision_html.encode("utf-8"),
                "HTML",
                f"Decision Report — {database_name}",
                "CUSTOMER_OUTPUT",
                decision_prov["filename"],
            ),
            (
                engineering_md.encode("utf-8"),
                "MARKDOWN",
                f"Engineering Report — {database_name}",
                "CUSTOMER_OUTPUT",
                engineering_prov["filename"],
            ),
            (
                data_json.encode("utf-8"),
                "JSON",
                f"Assessment Data (raw) — {database_name}",
                "CUSTOMER_OUTPUT",
                data_prov["filename"],
            ),
        ]

        # Fourth deliverable: the interactive report the WebApp's "Export to HTML"
        # produces. Assembled straight off the ArtifactStore because the API path is
        # unreachable for an ATX job -- every route resolves database_name through
        # Step Functions, and an A2A-orchestrated job has no execution. Isolated in
        # its own try: it reads six more artifacts than the other three, and none of
        # them failing is a reason to withhold reports that already rendered.
        try:
            from src.atx_orchestrator.runtime import analysis_report as _ar

            assignment_version = int(inner.get("assignment_version") or 1)
            export_data = _ar.build_export_data(
                store, job_id, database_name, assignment_version=assignment_version
            )
            analysis_prov = _prov("analysis-report", "html")
            analysis_html = _ar.render_analysis_report_html(
                export_data, filename=analysis_prov["filename"]
            )
            store.write_text(f"{base}/{analysis_prov['filename']}", analysis_html, "text/html")
            items.append(
                (
                    analysis_html.encode("utf-8"),
                    "HTML",
                    f"Interactive Analysis Report — {database_name}",
                    "CUSTOMER_OUTPUT",
                    analysis_prov["filename"],
                )
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("ATX interactive analysis report skipped: %s: %s", type(e).__name__, e)
            export_data = None

        # Fifth deliverable: the executive summary, rendered from the same report
        # as the Decision Report HTML plus the export data above (slide 3 needs
        # the collector query patterns; it degrades to a stated gap without them,
        # which is why export_data is passed even when it failed to build).
        #
        # The PDF is what the customer gets in the Artifacts panel -- it opens
        # anywhere and carries the deck's fonts with it. The .pptx is still
        # written to S3 as the editable source for whoever presents it, just not
        # registered. Both come from one render, so they cannot disagree.
        #
        # Its own try: this is the only part of the function with a binary
        # dependency (python-pptx, reportlab, the bundled template and fonts),
        # and a problem there must not withhold the four reports already rendered.
        try:
            from src.atx_orchestrator.runtime import pdf_report as _pdf
            from src.atx_orchestrator.runtime import pptx_report as _pptx

            deck, deck_pdf = _pdf.render_executive_summary_pdf(report, export_data)
            # Fixed names, unlike the other four: this is the reusable executive
            # deliverable and is called the same thing in every engagement. The
            # job it belongs to is already in the key prefix (and in the deck's
            # own core properties), so no date-stamped stem is needed.
            store.write_bytes(f"{base}/{_pptx.FILENAME}", deck)
            store.write_bytes(f"{base}/{_pdf.FILENAME}", deck_pdf)
            items.append(
                (
                    deck_pdf,
                    "PDF",
                    f"Executive Summary Report — {database_name}",
                    "CUSTOMER_OUTPUT",
                    _pdf.FILENAME,
                )
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("ATX executive summary skipped: %s: %s", type(e).__name__, e)

        # Register in the WebApp panel. CUSTOMER_OUTPUT is accepted from the agent
        # side (constraint C2, verified 2026-08-24).
        _artifacts.publish(items)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "ATX synthesis deliverables skipped (report is durable in S3): %s: %s",
            type(e).__name__,
            e,
        )


def _is_error_result(result: str) -> bool:
    """True if a phase tool's JSON string represents an ``{"error": ...}`` result.

    ``_run_phase_via_a2a`` returns either the completion payload or an error dict,
    both JSON-serialised. Treat unparseable output as an error too, so a
    completion transition never fires on a malformed synthesis result.
    """
    try:
        parsed = json.loads(result)
    except (TypeError, json.JSONDecodeError):
        return True
    return isinstance(parsed, dict) and "error" in parsed


def _complete_job_success(job_id: str) -> None:
    """Mark the platform job COMPLETED after the pipeline's final step succeeds.

    Best-effort and idempotent (see ``runtime.job_status.complete_job``). Kept as
    a thin wrapper so the import stays local and the synthesis tool reads cleanly.
    """
    try:
        from src.atx_orchestrator.runtime.job_status import complete_job

        complete_job(success=True, job_id=job_id)
    except Exception:  # noqa: BLE001
        # Fail-open: never let job completion crash the final synthesis turn.
        logger.warning("ATX: marking job COMPLETED failed (best-effort)", exc_info=True)


# Target engine per schema-design tool suffix, used in the plan step label and,
# since ADR-027, sent as target_type in the payload to the one consolidated
# `schema` agent. The suffix and engine differ because tool suffixes use hyphens
# (aurora-pg) while artifact keys and upstream's dispatch use the engine's own
# identifier (aurora_postgresql). Must stay within schema.VALID_TARGET_TYPES.
_SCHEMA_ENGINES: dict[str, str] = {
    "dynamodb": "dynamodb",
    "documentdb": "documentdb",
    "elasticache": "elasticache",
    "opensearch": "opensearch",
    "aurora-pg": "aurora_postgresql",
    "aurora-mysql": "aurora_mysql",
}


def _effective_assignment_version(job_id: str, database_name: str) -> int:
    """Resolve the latest assignment version from the store; default 1 (ADR-026).

    Reality Check writes ``assignment/v2/`` when it consolidates engines, and
    schema design and synthesis must operate on that latest version. Resolving it
    here in Python — mirroring the REST API's ``_latest_assignment_version`` —
    keeps the version out of the LLM's hands entirely. Falls back to 1 (the
    version the assessment core always writes) if nothing is found or the store
    is unavailable, so downstream never receives a bogus 0.
    """
    try:
        from src.atx_orchestrator.core import _resolve_assignment_version

        version = _resolve_assignment_version(_make_store(), job_id, database_name)
        return version if version > 0 else 1
    except Exception:  # noqa: BLE001 - best-effort; fall back to the always-written v1
        return 1


def _engines_with_in_scope_queries(
    job_id: str, database_name: str, assignment_version: int
) -> set[str]:
    """Return the engines that have at least one in-scope query routed to them.

    Reads ``<db>/<job>/assignment/v<N>/assignment.json`` and unions
    ``assigned_engine`` over in-scope query assignments. Mirrors
    ``local_orchestrator._get_engines_with_in_scope_queries``. Because the caller
    passes the *effective* version (v2 when Reality Check consolidated, else v1),
    this already reflects any engine consolidation.

    Fail-open: returns an empty set if the artifact is missing or unreadable, so
    callers can leave the plan untouched rather than mismark it.
    """
    try:
        store = _make_store()
        key = f"{database_name}/{job_id}/assignment/v{assignment_version}/assignment.json"
        if not store.exists(key):
            return set()
        assignment = store.read_json(key)
        return {
            qa["assigned_engine"]
            for qa in assignment.get("query_assignments", [])
            if qa.get("in_scope", True) and qa.get("assigned_engine")
        }
    except Exception:  # noqa: BLE001 - best-effort; leave the plan untouched on any error
        return set()


def _mark_unselected_schema_steps_skipped(job_id: str, database_name: str) -> None:
    """Mark the schema sub-steps for engines with no routed queries as STOPPED.

    Without this, an engine triage did not pick (or Reality Check consolidated
    away) keeps its ``schema_<engine>`` sub-step at NOT_STARTED, which the WebApp
    renders as a perpetual pending/clock icon. STOPPED renders as the "considered
    but not run" state, matching how the analysis phase reports skipped engines.

    Determines the selected set from the effective-version assignment (the engines
    schema-design actually ran for). Best-effort: if the selected set can't be
    resolved, nothing is marked (leaving the prior behaviour) rather than wrongly
    skipping every engine.
    """
    version = _effective_assignment_version(job_id, database_name)
    selected = _engines_with_in_scope_queries(job_id, database_name, version)
    if not selected:
        # Could not resolve which engines ran — don't risk marking all six
        # skipped. Leave the plan as-is.
        logger.info(
            "ATX: no selected schema engines resolved (job_id=%s) — not marking skips", job_id
        )
        return

    # The engine part of each schema label (schema_<engine>) matches the
    # assignment's assigned_engine vocabulary 1:1, so no translation is needed.
    all_engines = set(_SCHEMA_ENGINES.values())
    for engine in sorted(all_engines - selected):
        mark_step_skipped(
            f"schema_{engine}",
            "Not selected — no queries routed to this engine.",
        )
        logger.info("ATX: marked schema_%s skipped (not selected)", engine)


# =============================================================================
# Assignment-review gate (ADR-028)
#
# The gate reuses the existing ASSIGNMENT_REVIEW phase in the .meta/{job}.json
# progression as the approval signal — the same one the web path checks
# (_is_phase_completed(job_id, "assignment_review")) — so no new approval
# artifact is introduced. present_assignment_review marks it AWAITING_REVIEW;
# apply_assignment_edits marks it COMPLETED; the schema-design tools refuse to
# dispatch until it is COMPLETED (a hard interrupt between Reality Check and
# Schema Design).


def _mark_assignment_review(job_id: str, status: PhaseStatus) -> None:
    """Set the ASSIGNMENT_REVIEW phase status in the .meta progression (best-effort).

    Reuses LocalOrchestrator's progression persistence (store-backed .meta), which
    is S3-safe: get_progression returns a fresh all-NOT_STARTED progression when
    .meta is absent, so nothing is inferred by scanning a local dir.
    """
    try:
        store = _make_store()
        orch = _make_orchestrator(store)
        progression = orch.get_progression(job_id)
        orch._set_phase_status(progression, Phase.ASSIGNMENT_REVIEW, status)
        orch._save_progression(progression)
    except Exception:  # noqa: BLE001 - progress signal must not break the tool
        logger.warning(
            "ATX: could not set assignment_review=%s (job_id=%s)", status, job_id, exc_info=True
        )


def _assignment_review_approved(job_id: str) -> bool:
    """True when the customer has approved the assignment review (phase COMPLETED).

    Fail-closed: any error reading the progression is treated as NOT approved, so
    the gate holds rather than letting schema design run on an unreviewed
    assignment.
    """
    try:
        store = _make_store()
        orch = _make_orchestrator(store)
        progression = orch.get_progression(job_id)
        return bool(progression.phases[Phase.ASSIGNMENT_REVIEW].status == PhaseStatus.COMPLETED)
    except Exception:  # noqa: BLE001 - fail closed
        logger.warning(
            "ATX: could not read assignment_review phase (job_id=%s); treating as NOT approved",
            job_id,
            exc_info=True,
        )
        return False


def _run_schema_design_via_a2a(
    suffix: str,
    job_id: str,
    database_name: str,
) -> str:
    """Shared body for the six schema-design A2A tools."""
    job_id = _platform_job_id(job_id)
    # Hard interrupt (ADR-028): schema design must not run until the customer has
    # approved the routing at the assignment-review gate. Refuse before any state
    # change or dispatch; the orchestrator calls present_assignment_review then
    # apply_assignment_edits (which records approval) first.
    if not _assignment_review_approved(job_id):
        logger.info(
            "ATX: schema-design %s blocked — assignment review not approved (job_id=%s)",
            suffix,
            job_id,
        )
        return json.dumps(
            {
                "status": "blocked",
                "reason": "awaiting_assignment_review_approval",
                "job_id": job_id,
                "message": (
                    "Schema design is gated on the customer approving the query-to-engine "
                    "routing. Call present_assignment_review, share it with the customer, "
                    "and call apply_assignment_edits with their reply before designing schemas."
                ),
            }
        )
    # One consolidated `schema` agent serves every engine (ADR-027); the target
    # engine travels in the invocation payload as target_type, not in the agent
    # id. The orchestrator still invokes once per engine, concurrently.
    agent_id = f"{_AGENT_PREFIX}-schema"
    # Plan step labels use the engine's own identifier with underscores
    # (schema_aurora_postgresql), while agent ids use hyphens
    # (schema-aurora-pg). A mismatch here is silent: mark_step_* ignores an
    # unregistered phase name, so progress would simply never appear.
    engine = _SCHEMA_ENGINES[suffix]
    step = f"schema_{engine}"
    # Resolve the version in Python, not via the LLM (ADR-026): picks up the v2
    # assignment when Reality Check consolidated, else v1.
    assignment_version = _effective_assignment_version(job_id, database_name)

    from src.atx_orchestrator.core import (
        IMPLEMENTED_SCHEMA_DESIGNERS,
        _source_engine,
        schema_no_design_notes,
    )

    # Implemented-designer skip: guards engines with no real designer in
    # handler._dispatch_schema_agent (which would write a placeholder), so we
    # avoid paying an AgentCore cold-start for a guaranteed non-design. All six
    # current targets are implemented, so this branch is currently unreachable —
    # it stays as defensive coverage for any future target added before its
    # designer. When it does fire it emits the SAME note the post-dispatch path
    # would have, before the routing skip, so a same-family target reports "no
    # redesign" rather than "no queries routed".
    if engine not in IMPLEMENTED_SCHEMA_DESIGNERS:
        src = _source_engine(_make_store(), job_id, database_name)
        nd_notes, nd_warnings = schema_no_design_notes(engine, src, status="not_implemented")
        detail = (nd_notes or nd_warnings or ["No schema designer for this engine."])[0]
        logger.info(
            "ATX: schema-design %s skipped pre-dispatch — no implemented designer for "
            "%s (source=%s)",
            suffix,
            engine,
            src or "unknown",
        )
        mark_step_skipped(step, detail)
        no_designer: dict[str, object] = {
            "status": "skipped",
            "reason": "No implemented schema designer for this engine",
            "target_type": engine,
            "assignment_version": assignment_version,
            "job_id": job_id,
            "skipped_pre_dispatch": True,
        }
        if nd_notes:
            no_designer["notes"] = nd_notes
        if nd_warnings:
            no_designer["warnings"] = nd_warnings
        return json.dumps(no_designer)

    # Pre-dispatch skip: if the effective assignment routes no in-scope query to
    # this engine, there is nothing to design. Short-circuit before the A2A call so
    # we don't pay an AgentCore runtime cold-start just for the subagent to write a
    # "skipped" placeholder (mirrors handler.run_schema_design). This gate keys off
    # query routing (assigned_engine), independent of the table-qualifier match the
    # subagent-side skip uses.
    #
    # Fail-open: _engines_with_in_scope_queries returns an empty set when the
    # assignment can't be read, so we only skip when the routed set was positively
    # resolved AND this engine is absent from it; an empty set means "unknown" and
    # we still dispatch.
    routed = _engines_with_in_scope_queries(job_id, database_name, assignment_version)
    if routed and engine not in routed:
        logger.info(
            "ATX: schema-design %s skipped pre-dispatch — no in-scope queries routed to "
            "%s (assignment v%s)",
            suffix,
            engine,
            assignment_version,
        )
        mark_step_skipped(step, "No queries routed to this engine.")
        return json.dumps(
            {
                "status": "skipped",
                "reason": "No queries or tables assigned to this engine",
                "target_type": engine,
                "assignment_version": assignment_version,
                "job_id": job_id,
                "skipped_pre_dispatch": True,
                "notes": [f"{engine}: no queries routed to this engine; schema design skipped."],
            }
        )

    logger.info(
        "ATX: schema-design via A2A agent=%s job_id=%s db=%s assignment_version=%s",
        agent_id,
        job_id,
        database_name,
        assignment_version,
    )
    message = json.dumps(
        {
            "job_id": job_id,
            "database_name": database_name,
            "assignment_version": assignment_version,
            # target_type selects the engine for the one consolidated schema agent
            # (ADR-027). The subagent validates it against VALID_TARGET_TYPES.
            "target_type": engine,
        }
    )
    # Flip the parent "schema" box to in-progress as soon as any engine's design
    # starts. Idempotent across the parallel schema tools; the synthesis tool
    # marks the parent succeeded once every schema agent has finished.
    mark_step_running("schema")
    mark_step_running(step)
    try:
        payload = invoke_and_wait(agent_id, message)
    except A2AError as e:
        logger.error("ATX schema-design %s FAILED: %s: %s", suffix, type(e).__name__, e)
        mark_step_failed(step, str(e))
        return json.dumps(
            {
                "error": f"A2A schema-design failed: {e}",
                "error_type": type(e).__name__,
                "job_id": job_id,
                "agent_id": agent_id,
            }
        )
    mark_step_succeeded(step)
    return json.dumps(payload)


_SCHEMA_DOC = """Design the {label} target schema by invoking a deployed subagent over A2A.

    Requires Collector, Triage, the matching Analysis, and Assignment to have run
    first. Produces the table definitions and access patterns that synthesis turns
    into ``table_mappings``, ``query_groups`` and
    ``recommended_architecture.databases`` — three fields that stay empty in the
    report until this has run for at least one engine.

    Call this for every engine triage selected, in parallel with the other
    schema-design tools, after assignment and before synthesis. A substantive
    design takes roughly ten to fifteen minutes, so running them sequentially
    would exceed the response window.

    Writes 1 artifact: ``<db>/<job>/schema-{engine}/v<N>/schema_output.json``.

    The assignment version is resolved automatically (the latest one on the
    store, which is the consolidated v2 when Reality Check ran, else v1); you do
    not pass it.

    Args:
        job_id: Unique job identifier.
        database_name: Source database name.

    Returns:
        JSON string with status, the counts of table_definitions, access_patterns
        and unsupported_patterns, the artifact key, and either ``notes`` or
        ``warnings`` when no design was produced. Relay those strings verbatim.
    """


@tool
def run_schema_design_dynamodb_via_a2a(job_id: str, database_name: str) -> str:
    return _run_schema_design_via_a2a("dynamodb", job_id, database_name)


@tool
def run_schema_design_documentdb_via_a2a(job_id: str, database_name: str) -> str:
    return _run_schema_design_via_a2a("documentdb", job_id, database_name)


@tool
def run_schema_design_elasticache_via_a2a(job_id: str, database_name: str) -> str:
    return _run_schema_design_via_a2a("elasticache", job_id, database_name)


@tool
def run_schema_design_opensearch_via_a2a(job_id: str, database_name: str) -> str:
    return _run_schema_design_via_a2a("opensearch", job_id, database_name)


@tool
def run_schema_design_aurora_pg_via_a2a(job_id: str, database_name: str) -> str:
    return _run_schema_design_via_a2a("aurora-pg", job_id, database_name)


@tool
def run_schema_design_aurora_mysql_via_a2a(job_id: str, database_name: str) -> str:
    return _run_schema_design_via_a2a("aurora-mysql", job_id, database_name)


# Docstrings are assigned rather than written inline so the six tools cannot drift
# apart. The LLM reads these as the tool descriptions, so a divergence between
# them would be a behavioural difference, not a cosmetic one.
for _fn, _label, _engine in (
    (run_schema_design_dynamodb_via_a2a, "DynamoDB", "dynamodb"),
    (run_schema_design_documentdb_via_a2a, "DocumentDB", "documentdb"),
    (run_schema_design_elasticache_via_a2a, "ElastiCache", "elasticache"),
    (run_schema_design_opensearch_via_a2a, "OpenSearch", "opensearch"),
    (run_schema_design_aurora_pg_via_a2a, "Aurora PostgreSQL", "aurora_postgresql"),
    (run_schema_design_aurora_mysql_via_a2a, "Aurora MySQL", "aurora_mysql"),
):
    _target = getattr(_fn, "__wrapped__", _fn)
    _target.__doc__ = _SCHEMA_DOC.format(label=_label, engine=_engine)
