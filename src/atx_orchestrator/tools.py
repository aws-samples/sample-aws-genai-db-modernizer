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
    get_step_id,
    mark_step_failed,
    mark_step_not_started,
    mark_step_pending_human_input,
    mark_step_running,
    mark_step_skipped,
    mark_step_succeeded,
    put_job_plan,
    register_steps,
    register_steps_from_server,
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

    # The synthesis writer produces a VERSIONED key, synthesis/v{N}/report.json
    # (keyed on the effective assignment version), or the legacy
    # referee-synthesis/report.json when no versioned assignment exists. It never
    # writes an unversioned synthesis/report.json, so resolve the effective
    # version and read the versioned key first, then fall back. (ADR-029: the
    # previous unversioned-only read meant this tool always reported "not
    # available" for a normally-run synthesis.)
    from src.storage.assignment_versioning import resolve_effective_assignment_version

    version = resolve_effective_assignment_version(store, database_name, job_id)
    candidate_keys: list[str] = []
    if version > 0:
        candidate_keys.append(f"{database_name}/{job_id}/synthesis/v{version}/report.json")
    candidate_keys.append(f"{database_name}/{job_id}/referee-synthesis/report.json")
    # Legacy/defensive: an older unversioned artifact, if one exists.
    candidate_keys.append(f"{database_name}/{job_id}/synthesis/report.json")

    for report_key in candidate_keys:
        if store.exists(report_key):
            return json.dumps(store.read_json(report_key))

    return json.dumps(
        {
            "error": "Report not available yet. Run synthesis first.",
            "job_id": job_id,
            "report_artifact": candidate_keys[0],
            "searched": candidate_keys,
        }
    )


@tool
def present_assignment_review(job_id: str, database_name: str) -> str:
    """Present the routing RECOMMENDATION and ask the customer how to proceed.

    Call this AFTER the assessment core (which includes Reality Check) and BEFORE
    any schema-design tool. It renders an engine-level summary of the effective
    assignment (per engine: query count and the main rationale) for the chat, and
    marks the review gate as awaiting the customer. The summary is shown in chat,
    NOT published as a downloadable artifact, and it does NOT generate the large
    per-query table — that is only built if the customer asks to review in detail
    (``open_detailed_routing_review``).

    Show the returned ``summary_markdown`` to the customer and ask them to choose:
      - continue with the recommendation as-is, or
      - review the full per-query routing in detail.

    If they choose continue, call ``finalize_assignment_review`` (no edits) to
    approve. If they choose detail, call ``open_detailed_routing_review``. Schema
    design is blocked until ``finalize_assignment_review`` records approval.

    Returns JSON with ``status`` ("awaiting_choice"), ``assignment_version``, and
    ``summary_markdown`` (present this to the customer).
    """
    job_id = _platform_job_id(job_id)
    from src.agents.referee.assignment_review import render_assignment_summary
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
    summary_md = render_assignment_summary(assignment)

    # The recommendation is a chat message the customer reads to decide "continue"
    # vs "review in detail" — it is intentionally NOT published as a CUSTOMER_OUTPUT
    # artifact, because that renders as a download in the WebApp and misleads the
    # customer into thinking the review happens in a downloaded file. It is staged
    # on the store only for provenance; the editable experience is the HITL table
    # from ``open_detailed_routing_review``.
    summary_key = f"{database_name}/{job_id}/assignment/review/summary-v{version}.md"
    try:
        store.write_text(summary_key, summary_md, "text/markdown")
    except Exception:  # noqa: BLE001 - staging is best-effort; the markdown is returned regardless
        logger.warning("ATX: could not stage routing summary at %s", summary_key, exc_info=True)

    mark_step_pending_human_input(
        "assignment_review", "Awaiting customer review of the routing recommendation."
    )
    _mark_assignment_review(job_id, PhaseStatus.AWAITING_REVIEW)

    return json.dumps(
        {
            "status": "awaiting_choice",
            "job_id": job_id,
            "assignment_version": version,
            "summary_artifact": summary_key,
            "summary_markdown": summary_md,
        }
    )


@tool
def open_detailed_routing_review(job_id: str, database_name: str) -> str:
    """Open the full per-query routing table for detailed customer review/editing.

    Call this ONLY when the customer, after seeing the recommendation from
    ``present_assignment_review``, asks to review the routing in detail. It builds
    the per-query table (query_id, access pattern, current engine, editable new
    engine, editable in-scope, and rationale) and raises it as a BLOCKING
    human-in-the-loop task the customer edits inline in the WebApp.

    The task is BLOCKING: after it returns, STOP and end your turn. Do NOT call any
    other tool. Tell the customer their editable routing table is open in the
    WebApp and to submit it when done. When they submit, the platform re-invokes
    you; then call ``finalize_assignment_review`` to read their edits and proceed.

    If the human-in-the-loop transport is unavailable (e.g. running outside the
    WebApp), this falls back to publishing the full editable table as markdown to
    the Artifacts panel and returns it as ``review_markdown``; in that case present
    it in chat, wait for the customer's edited rows, and pass them to
    ``finalize_assignment_review(edited_markdown=...)``.

    Returns JSON with ``status`` ("awaiting_review"), ``transport`` ("hitl" or
    "chat"), ``assignment_version``, and — for the chat fallback —
    ``review_markdown``.
    """
    job_id = _platform_job_id(job_id)
    from src.agents.referee.assignment_review import build_review_table, render_assignment_review
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

    # Try the platform HITL editable-table transport first.
    from src.atx_orchestrator.runtime import hitl as _hitl

    # Resolve the assignment_review plan step id so the HITL task renders UNDER
    # that step in the WebApp. A task created without a stepId attaches to no step
    # and never surfaces in the tasks panel (the customer sees nothing to open).
    # The in-process registry is populated by declare_pipeline_plan; under
    # raise-and-resume this may run in a fresh process, so fall back to reading the
    # plan back from the server.
    step_id = get_step_id("assignment_review")
    if not step_id:
        register_steps_from_server()
        step_id = get_step_id("assignment_review")

    column_definitions, items = build_review_table(assignment)
    hitl_task_id = _hitl.raise_assignment_table(
        column_definitions=column_definitions,
        items=items,
        header=f"Query-to-engine routing — {database_name} (v{version})",
        title="Review query-to-engine routing",
        description=(
            "Edit the 'new engine' and 'in scope' cells to change routing, then submit. "
            "Leave a row unchanged to keep its current routing."
        ),
        step_id=step_id,
        tag=f"assignment-review-v{version}",
    )

    if hitl_task_id:
        _record_pending_hitl(store, database_name, job_id, hitl_task_id, version)
        mark_step_pending_human_input(
            "assignment_review", "Awaiting the customer's edited routing table."
        )
        _mark_assignment_review(job_id, PhaseStatus.AWAITING_REVIEW)
        return json.dumps(
            {
                "status": "awaiting_review",
                "transport": "hitl",
                "job_id": job_id,
                "assignment_version": version,
                "hitl_task_id": hitl_task_id,
            }
        )

    # Fallback: HITL unavailable. Publish the full editable markdown table to the
    # Artifacts panel and hand it to chat, preserving the original transport.
    review_md = render_assignment_review(assignment)
    review_key = f"{database_name}/{job_id}/assignment/review/v{version}.md"
    try:
        store.write_text(review_key, review_md, "text/markdown")
    except Exception:  # noqa: BLE001 - staging is best-effort
        logger.warning("ATX: could not stage review doc at %s", review_key, exc_info=True)
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
        "assignment_review", "Awaiting the customer's edited routing table (chat)."
    )
    _mark_assignment_review(job_id, PhaseStatus.AWAITING_REVIEW)
    return json.dumps(
        {
            "status": "awaiting_review",
            "transport": "chat",
            "job_id": job_id,
            "assignment_version": version,
            "review_artifact": review_key,
            "review_markdown": review_md,
        }
    )


@tool
def finalize_assignment_review(
    job_id: str,
    database_name: str,
    edited_markdown: str = "",
    accept_feasibility_risks: bool = False,
) -> str:
    """Apply the customer's routing decision and open the schema-design gate.

    This is the single approval point of the review gate. It handles all three
    ways the customer can respond:

      - Continue with the recommendation (from ``present_assignment_review``):
        call with an empty ``edited_markdown`` and no detailed review open. Nothing
        is changed; the routing is approved as-is.
      - Detailed review via the WebApp table (``open_detailed_routing_review``
        returned transport "hitl"): call with an empty ``edited_markdown``. Their
        submitted edits are read back from the human-in-the-loop task and applied.
      - Detailed review via the chat fallback (transport "chat"): pass the
        customer's edited marker-bounded markdown table as ``edited_markdown``.

    Applies only the changed rows as a new customer_modified assignment version via
    the shared override path, then records approval so schema design may run. On a
    parse/validation failure nothing is applied and the gate stays closed.

    After applying edits (or approving as-is), a feasibility review runs on the
    effective routing (ADR-029 Layer C). If it finds a blocking problem — a table
    whose reads are routed away from its writes, or a co-dependent JOIN group split
    onto an engine that cannot serve joins — the gate stays closed and this returns
    ``status`` "infeasible" with ``feasibility_findings``; present them to the
    customer, who must fix the routing and resubmit, or re-run with
    ``accept_feasibility_risks=True`` to proceed with the risk recorded on the
    artifact.

    Returns JSON with ``status`` ("approved", "invalid_edit", or "infeasible"),
    ``changed``, ``applied_overrides``, ``assignment_version`` (effective after any
    edit), ``validation_warnings``, and ``feasibility_findings``.
    """
    job_id = _platform_job_id(job_id)
    from src.agents.referee.assignment_overrides import (
        AssignmentOverrideError,
        AssignmentValidationFailed,
        NoAssignmentFound,
        UnknownQuery,
        apply_assignment_overrides,
        mark_assignment_customer_approved,
    )
    from src.agents.referee.assignment_review import (
        REVIEW_BEGIN_MARKER,
        ReviewParseError,
        diff_review_items,
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

    current = Assignment.model_validate(
        store.read_json(assignment_artifact_path(database_name, job_id, version))
    )

    # Resolve overrides from whichever transport the customer used. Precedence:
    #   1. explicit edited_markdown (chat fallback)
    #   2. a pending HITL submission (WebApp editable table)
    #   3. nothing -> approve as-is (customer continued with the recommendation)
    overrides: list = []
    no_changes_submission = False
    stripped = (edited_markdown or "").strip()
    pending = _read_pending_hitl(store, database_name, job_id)

    try:
        if stripped and REVIEW_BEGIN_MARKER in stripped:
            overrides = diff_review_rows(current, parse_assignment_review(stripped))
        elif pending and pending.get("hitl_task_id"):
            from src.atx_orchestrator.runtime import hitl as _hitl

            status, edited_items = _hitl.read_assignment_submission(pending["hitl_task_id"])
            if status == "awaiting_submission":
                return json.dumps(
                    {
                        "status": "awaiting_review",
                        "job_id": job_id,
                        "message": (
                            "The customer has not submitted the routing table yet. Wait for "
                            "their submission before finalizing."
                        ),
                    }
                )
            if status == "submitted":
                overrides = diff_review_items(current, edited_items or [])
            elif status == "submitted_empty":
                # The customer opened the routing table, changed nothing, and
                # submitted — a valid "keep the routing as-is" action. Treat it as
                # approve-as-is (no overrides) and proceed; the approved response
                # notes that no changes were detected so a lost edit is noticeable.
                overrides = []
                no_changes_submission = True
            else:
                # status in ("unreadable", "unavailable"): the customer submitted
                # content we could not read, or the task could not be fetched. Do
                # NOT approve-as-is — that would silently drop real edits. Fail
                # loudly so the gate stays open and nothing is lost.
                return json.dumps(
                    {
                        "status": "error",
                        "job_id": job_id,
                        "message": (
                            "The customer's submitted routing table could not be read back, "
                            "so nothing was applied and the routing was NOT approved. Ask them "
                            "to submit again; if it keeps failing, this is a bug to report."
                        ),
                    }
                )
    except ReviewParseError as e:
        return json.dumps(
            {
                "status": "invalid_edit",
                "error": str(e),
                "job_id": job_id,
                "message": (
                    "The edited routing could not be read. Ask the customer to correct it "
                    "and resend; nothing was applied."
                ),
            }
        )

    changed = False
    applied = 0
    warnings: list[str] = []
    propagated: list[str] = []
    effective_version = version
    effective_assignment: Assignment = current
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
        effective_assignment = result.assignment
        # Surface the co-dependency-split (and scope) warnings the validator
        # computed for this version so the orchestrator can show them to the
        # customer, instead of leaving them buried on the artifact (ADR-029 B).
        warnings = result.assignment.validation_warnings
        # Queries moved automatically to stay co-located with a co-dependent query
        # the customer re-routed (ADR-029 Amendment 3), so the orchestrator can
        # tell the customer the group moved together.
        propagated = result.propagated_query_ids
    # Approve-as-is (no edits) is stamped CUSTOMER_APPROVED on the artifact only
    # after the feasibility gate below passes, so a routing the reviewer blocks is
    # never recorded as approved.

    # Post-gate feasibility review (ADR-029 Layer C). Runs on the effective
    # routing (first-pass approval or an edited version). Blocking findings loop
    # the gate: the customer must fix the routing or re-run with
    # accept_feasibility_risks=true. Advisory findings are surfaced but do not
    # block. The reviewer pushes back rather than shipping a routing that fails
    # in production.
    from src.agents.referee.feasibility_review import review_assignment_feasibility
    from src.contracts.feasibility_models import FindingSeverity

    collector_key = f"{database_name}/{job_id}/collector/output.json"
    collector_output = store.read_json(collector_key) if store.exists(collector_key) else {}
    findings = review_assignment_feasibility(effective_assignment, collector_output)
    blocking = [f for f in findings if f.severity is FindingSeverity.BLOCKING]
    findings_json = [f.model_dump(mode="json") for f in findings]

    if blocking and not accept_feasibility_risks:
        # Keep the gate awaiting — do NOT open schema design. The edited version
        # (if any) is already written; the customer either re-edits or accepts.
        _mark_assignment_review(job_id, PhaseStatus.AWAITING_REVIEW)
        mark_step_pending_human_input(
            "assignment_review",
            "Routing is not feasible as submitted; awaiting customer fix or acceptance.",
        )
        return json.dumps(
            {
                "status": "infeasible",
                "job_id": job_id,
                "changed": changed,
                "applied_overrides": applied,
                "co_dependency_propagated": propagated,
                "assignment_version": effective_version,
                "validation_warnings": warnings,
                "feasibility_findings": findings_json,
                "message": (
                    "The routing has blocking feasibility problems (see "
                    "feasibility_findings). Present them to the customer plainly: this "
                    "will not work as routed. They must either change the routing and "
                    "resubmit, or explicitly accept the risks by re-running "
                    "finalize_assignment_review with accept_feasibility_risks=true."
                ),
            }
        )

    if blocking and accept_feasibility_risks:
        # Record the accepted blocking findings on the artifact for audit, then
        # proceed. No new version — this stamps the effective version in place.
        effective_assignment.accepted_feasibility_findings = blocking
        store.write_json(
            assignment_artifact_path(database_name, job_id, effective_version),
            effective_assignment.model_dump(mode="json"),
        )

    if not changed:
        # Approved as-is and feasible: stamp the effective assignment
        # CUSTOMER_APPROVED in place (no new version) so the artifact is
        # self-describing. Best-effort — the .meta phase is the authoritative gate.
        try:
            mark_assignment_customer_approved(store, database_name, job_id)
        except Exception:  # noqa: BLE001 - artifact stamp must not fail the gate
            logger.warning(
                "ATX: could not stamp assignment CUSTOMER_APPROVED (job_id=%s)",
                job_id,
                exc_info=True,
            )

    # Record approval — this opens the schema-design gate. Same .meta phase signal
    # the web path checks, so no new approval artifact is introduced (ADR-028).
    _mark_assignment_review(job_id, PhaseStatus.COMPLETED)
    detail = "Customer approved query routing."
    if changed:
        detail += f" {applied} change(s) applied (assignment v{effective_version})."
    mark_step_succeeded("assignment_review", detail)

    approved: dict = {
        "status": "approved",
        "job_id": job_id,
        "changed": changed,
        "applied_overrides": applied,
        "co_dependency_propagated": propagated,
        "assignment_version": effective_version,
        "validation_warnings": warnings,
        "feasibility_findings": findings_json,
    }
    if propagated:
        # Be transparent that co-dependent group-mates moved with the customer's
        # explicit pick, so they are not surprised by engine changes they did not
        # click (ADR-029 Amendment 3).
        approved["message"] = (
            f"Note: {len(propagated)} co-dependent quer{'y' if len(propagated) == 1 else 'ies'} "
            f"({', '.join(propagated)}) moved to the same engine as your edit to keep the shared "
            f"JOIN group together."
        )
    if no_changes_submission:
        # Be transparent: the customer submitted without changes, so tell them the
        # routing was kept as-is. If they actually intended a change, this makes a
        # dropped edit visible so they can re-open the table and edit again.
        approved["message"] = (
            "You submitted the routing table without changing any cells, so the current "
            "routing is kept as-is and the assessment continues. If you meant to change a "
            "routing, re-open the table and edit the 'new engine' / 'in scope' cells before "
            "submitting."
        )
    return json.dumps(approved)


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
    # Synthesis produced the report, but the assessment is NOT necessarily done:
    # the customer may want to re-route queries and re-run (ADR-029 re-entry). So
    # rest the job at the non-terminal AWAITING_HUMAN_INPUT rather than completing
    # it. A terminal COMPLETED job cannot be revived (the platform rejects updates
    # on a terminal job), which is exactly what broke re-entry. The terminal
    # COMPLETED is set once, explicitly, by complete_assessment when the customer
    # confirms they are done. The platform still does not roll the job up on its
    # own, so we own this transition; it is best-effort and fail-open. A synthesis
    # error is left as-is so the LLM can retry.
    if not _is_error_result(result):
        _rest_job_awaiting_input(job_id)
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


def _rest_job_awaiting_input(job_id: str) -> None:
    """Rest the platform job at AWAITING_HUMAN_INPUT after a synthesis round.

    Replaces the old auto-COMPLETE-at-synthesis: a terminal job cannot be revived,
    which broke ADR-029 re-entry (the reopened gate's HITL table was not
    submittable on a terminal job). Resting at the non-terminal
    AWAITING_HUMAN_INPUT keeps the job re-enterable; the terminal COMPLETED is set
    once, explicitly, by ``complete_assessment`` when the customer is done.
    """
    try:
        from src.atx_orchestrator.runtime.job_status import set_awaiting_human_input

        set_awaiting_human_input(job_id=job_id)
    except Exception:  # noqa: BLE001
        logger.warning("ATX: resting job AWAITING_HUMAN_INPUT failed (best-effort)", exc_info=True)


def _resume_job_executing(job_id: str) -> None:
    """Move the platform job back to EXECUTING for a re-entry round.

    Called by ``reopen_assignment_review`` so the reopened gate + HITL run on a
    live (non-terminal) job. Best-effort and fail-open.
    """
    try:
        from src.atx_orchestrator.runtime.job_status import resume_executing

        resume_executing(job_id=job_id)
    except Exception:  # noqa: BLE001
        logger.warning("ATX: resuming job EXECUTING failed (best-effort)", exc_info=True)


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


def _reset_schema_view_for_reentry(job_id: str, database_name: str) -> None:
    """Reset the schema-design and synthesis plan steps to NOT_STARTED (ADR-029).

    Called when the customer reopens the routing gate to change their selection.
    After a completed round the "Design Target Schemas" box and its per-engine
    sub-steps hold that round's terminal states (SUCCEEDED / FAILED / STOPPED),
    and synthesis shows SUCCEEDED. On re-entry only the *affected* engines
    re-run; a stale FAILED or STOPPED left on an engine this round copies forward
    (or does not touch) reads to the customer as an error even though the round is
    healthy. Resetting the parent, all six per-engine sub-steps, and synthesis to
    NOT_STARTED gives a clean slate, so the panel clearly shows the schema work is
    queued to run again for the new routing. redispatch_after_reroute then
    re-marks each engine accurately (reused vs re-designing), and synthesis marks
    the unselected engines skipped again.

    Best-effort and fail-open: each mark is a no-op when the step is unregistered
    or the plan API is unreachable (e.g. running outside the ATX runtime).
    """
    mark_step_not_started(
        "schema", "Reopened routing — schema design will re-run for the updated selection."
    )
    for engine in sorted(set(_SCHEMA_ENGINES.values())):
        mark_step_not_started(f"schema_{engine}")
    mark_step_not_started("synthesis", "Will rebuild the report after schema design re-runs.")
    logger.info(
        "ATX: reset schema + synthesis plan steps for re-entry (job_id=%s)",
        job_id,
    )


# =============================================================================
# Assignment-review gate (ADR-028)
#
# The gate reuses the existing ASSIGNMENT_REVIEW phase in the .meta/{job}.json
# progression as the approval signal — the same one the web path checks
# (_is_phase_completed(job_id, "assignment_review")) — so no new approval
# artifact is introduced. present_assignment_review marks it AWAITING_REVIEW;
# finalize_assignment_review marks it COMPLETED; the schema-design tools refuse to
# dispatch until it is COMPLETED (a hard interrupt between Reality Check and
# Schema Design).


def _pending_hitl_key(database_name: str, job_id: str) -> str:
    """Store key for the review gate's pending HITL pointer (transport state)."""
    return f"{database_name}/{job_id}/assignment/review/pending_hitl.json"


def _record_pending_hitl(
    store: object, database_name: str, job_id: str, hitl_task_id: str, version: int
) -> None:
    """Persist the HITL task the review gate is blocked on (best-effort).

    Under the raise-and-resume model the task id is otherwise known only to the
    turn that raised it; recording it lets a later turn (after the customer
    submits and the platform re-invokes the orchestrator) read the submission
    back. This is transport state, not an approval signal — approval remains the
    ``.meta`` ASSIGNMENT_REVIEW phase (ADR-028), so no approval artifact is added.
    """
    try:
        store.write_text(  # type: ignore[attr-defined]
            _pending_hitl_key(database_name, job_id),
            json.dumps({"hitl_task_id": hitl_task_id, "assignment_version": version}),
            "application/json",
        )
    except Exception:  # noqa: BLE001 - pointer loss only degrades a later resume
        logger.warning(
            "ATX: could not record pending HITL task %s (job_id=%s)",
            hitl_task_id,
            job_id,
            exc_info=True,
        )


def _read_pending_hitl(store: object, database_name: str, job_id: str) -> dict | None:
    """Return the recorded pending HITL pointer, or None when absent/unreadable."""
    key = _pending_hitl_key(database_name, job_id)
    try:
        if store.exists(key):  # type: ignore[attr-defined]
            data = store.read_json(key)  # type: ignore[attr-defined]
            return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 - absence is normal (chat fallback / approve-as-is)
        logger.debug("ATX: no pending HITL pointer for job_id=%s", job_id, exc_info=True)
    return None


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
    # finalize_assignment_review (which records approval) first.
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
                    "routing. Call present_assignment_review, share the recommendation with "
                    "the customer, and call finalize_assignment_review once they approve "
                    "(directly, or after open_detailed_routing_review) before designing schemas."
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

    # Idempotent skip (restore-safe reuse): if this engine's schema output already
    # exists for the effective assignment version, return it instead of designing
    # again. A substantive design runs ~10-35 min, and the schema subagent
    # completes and writes its output INDEPENDENTLY of the orchestrator. So if the
    # orchestrator is recycled mid-wait and restored (idle/hibernation) — or a
    # dispatch is duplicated — re-running would redo a finished design and can
    # loop. Reusing the existing output makes a recycle harmless and lets restore
    # make forward progress. This also composes with redispatch_after_reroute,
    # which copies unaffected engines' output forward to the new version: those are
    # skipped here, and only the truly-affected engines (no vN output yet) run.
    existing_key = (
        f"{database_name}/{job_id}/schema-{engine}/v{assignment_version}/schema_output.json"
    )
    _store = _make_store()
    if _store.exists(existing_key):
        logger.info(
            "ATX: schema-design %s reused — output already exists at %s (restore-safe skip)",
            suffix,
            existing_key,
        )
        mark_step_succeeded(step, "Schema already designed for this version (reused).")
        existing = _store.read_json(existing_key)
        if isinstance(existing, dict):
            existing["reused_existing"] = True
            existing.setdefault("status", "already_designed")
            return json.dumps(existing)
        return json.dumps(
            {
                "status": "already_designed",
                "reused_existing": True,
                "target_type": engine,
                "assignment_version": assignment_version,
                "job_id": job_id,
            }
        )

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


# =============================================================================
# Staleness-driven re-entry (ADR-029 Layer A)
#
# After schema design and synthesis, a customer may change routing. Re-entry
# reuses the review gate: reopen_assignment_review flips the gate back to
# AWAITING_REVIEW so present/open/finalize run again; finalize appends a new
# assignment version. redispatch_after_reroute then re-designs only the engines
# whose in-scope query set changed and copies the unchanged engines' schema
# forward to the new version, so the expensive fleet is not re-run wholesale.


# engine identifier (aurora_postgresql) -> tool suffix (aurora-pg).
_ENGINE_TO_SUFFIX: dict[str, str] = {engine: suffix for suffix, engine in _SCHEMA_ENGINES.items()}


def _latest_schema_version_below(
    store: object, database_name: str, job_id: str, below: int
) -> int | None:
    """Highest assignment version < ``below`` that has any schema output written.

    This is the version schema was last built at, so an unaffected engine's
    ``schema-<engine>/v<prev>/schema_output.json`` can be copied forward.
    """
    prefix = f"{database_name}/{job_id}/"
    best: int | None = None
    for key in store.list_prefix(prefix):  # type: ignore[attr-defined]
        parts = str(key).replace(prefix, "").split("/")
        if (
            len(parts) == 3
            and parts[0].startswith("schema-")
            and parts[1].startswith("v")
            and parts[2] == "schema_output.json"
        ):
            try:
                version = int(parts[1][1:])
            except ValueError:
                continue
            if version < below and (best is None or version > best):
                best = version
    return best


def _copy_schema_forward(
    store: object, database_name: str, job_id: str, engine: str, prev_version: int, new_version: int
) -> bool:
    """Copy an unaffected engine's schema output (+ design trace) to ``new_version``.

    Restamps the embedded ``assignment_version`` so a reader sees the version it
    now belongs to. Returns True when a schema output was copied. An engine whose
    in-scope query set is unchanged produces a byte-identical design, so this
    avoids re-running the LLM-heavy designer for it.
    """
    base_prev = f"{database_name}/{job_id}/schema-{engine}/v{prev_version}"
    base_new = f"{database_name}/{job_id}/schema-{engine}/v{new_version}"
    src = f"{base_prev}/schema_output.json"
    if not store.exists(src):  # type: ignore[attr-defined]
        return False
    output = store.read_json(src)  # type: ignore[attr-defined]
    if isinstance(output, dict):
        output["assignment_version"] = new_version
    store.write_json(f"{base_new}/schema_output.json", output)  # type: ignore[attr-defined]
    trace_src = f"{base_prev}/design_trace.json"
    if store.exists(trace_src):  # type: ignore[attr-defined]
        store.write_json(  # type: ignore[attr-defined]
            f"{base_new}/design_trace.json", store.read_json(trace_src)  # type: ignore[attr-defined]
        )
    return True


@tool
def reopen_assignment_review(job_id: str, database_name: str) -> str:
    """Reopen the assignment-review gate so the customer can change routing.

    Use this for staleness-driven re-entry: after schema design / synthesis, when
    the customer wants to adjust the query-to-engine routing. It flips the
    ASSIGNMENT_REVIEW phase back to awaiting review; then drive the normal gate
    (``present_assignment_review`` -> optionally ``open_detailed_routing_review``
    -> ``finalize_assignment_review``). After ``finalize`` applies the edit, call
    ``redispatch_after_reroute`` to re-run only the affected engines.

    Returns JSON with ``status`` ("reopened") and the current ``assignment_version``.
    """
    job_id = _platform_job_id(job_id)
    from src.storage.assignment_versioning import resolve_effective_assignment_version

    store = _make_store()
    version = resolve_effective_assignment_version(store, database_name, job_id)
    if version == 0:
        return json.dumps(
            {"error": "No assignment to reopen. Run the assessment core first.", "job_id": job_id}
        )
    # Move the job back to EXECUTING FIRST. After a synthesis round the job rests
    # at the non-terminal AWAITING_HUMAN_INPUT; resuming it to EXECUTING is what
    # makes the subsequent job-plan updates and the re-raised HITL routing table
    # submittable. Without this the reopened table renders but cannot be submitted.
    _resume_job_executing(job_id)
    _mark_assignment_review(job_id, PhaseStatus.AWAITING_REVIEW)
    mark_step_pending_human_input(
        "assignment_review", "Reopened for customer routing changes (re-entry)."
    )
    # Clear the previous round's schema-design and synthesis states so the panel
    # shows a clean slate for the re-run instead of stale SUCCEEDED/FAILED/STOPPED
    # icons that read as errors (ADR-029). redispatch_after_reroute re-marks each
    # engine accurately once the customer applies their edit.
    _reset_schema_view_for_reentry(job_id, database_name)
    return json.dumps(
        {
            "status": "reopened",
            "job_id": job_id,
            "assignment_version": version,
            "message": (
                "Gate reopened. Present the routing, apply the customer's edit with "
                "finalize_assignment_review, then call redispatch_after_reroute."
            ),
        }
    )


@tool
def redispatch_after_reroute(job_id: str, database_name: str) -> str:
    """Re-design only the engines a re-entry edit changed; copy the rest forward.

    Call this after ``finalize_assignment_review`` applies a re-entry edit (a new
    assignment version). It computes, deterministically from the assignment diff,
    which engines' in-scope query set changed since schema was last built, copies
    the unchanged engines' schema output forward to the new version, and returns
    the affected engines to re-design.

    Next steps (the tool does not run them, so the per-engine designs stay
    parallel and within the response window): call the listed ``dispatch_tools``
    for the ``affected_engines`` (in parallel, as in the first pass), then
    ``run_synthesis_via_a2a`` to rebuild the report at the new version.

    Returns JSON with ``affected_engines``, ``copied_forward_engines``,
    ``dispatch_tools``, ``assignment_version`` and ``previous_schema_version``.
    """
    job_id = _platform_job_id(job_id)
    from src.storage.assignment_versioning import (
        assignment_engine_diff,
        resolve_effective_assignment_version,
    )

    store = _make_store()
    new_version = resolve_effective_assignment_version(store, database_name, job_id)
    if new_version == 0:
        return json.dumps({"error": "No assignment found.", "job_id": job_id})

    prev_version = _latest_schema_version_below(store, database_name, job_id, new_version)
    copied: list[str] = []
    if prev_version is None:
        # No prior schema to reuse (not a re-entry, or schema never ran): every
        # engine with in-scope queries must be designed.
        affected = sorted(_engines_with_in_scope_queries(job_id, database_name, new_version))
    else:
        diff = assignment_engine_diff(store, database_name, job_id, prev_version, new_version)
        affected = list(diff["affected"])
        for engine in diff["unaffected"]:
            if _copy_schema_forward(
                store, database_name, job_id, engine, prev_version, new_version
            ):
                copied.append(engine)
            else:
                affected.append(engine)  # nothing to copy forward -> must re-run
        affected = sorted(set(affected))

    # Fill in the schema view that reopen reset to NOT_STARTED: the parent box is
    # running again, and each reused engine shows SUCCEEDED with a "reused" note so
    # it does not sit at a pending clock while only the affected engines re-run.
    # The affected engines' own dispatch tools mark them running -> succeeded, and
    # synthesis marks the unselected engines skipped.
    if affected:
        mark_step_running("schema", "Re-designing the engines affected by the routing change.")
    for engine in sorted(set(copied)):
        mark_step_succeeded(f"schema_{engine}", "Reused — routing for this engine did not change.")

    dispatch_tools = [
        f"run_schema_design_{_ENGINE_TO_SUFFIX.get(e, e).replace('-', '_')}_via_a2a"
        for e in affected
    ]
    logger.info(
        "ATX redispatch: job_id=%s prev_schema_v=%s new_v=%s affected=%s copied=%s",
        job_id,
        prev_version,
        new_version,
        affected,
        copied,
    )
    return json.dumps(
        {
            "status": "redispatch_ready",
            "job_id": job_id,
            "assignment_version": new_version,
            "previous_schema_version": prev_version,
            "affected_engines": affected,
            "copied_forward_engines": copied,
            "dispatch_tools": dispatch_tools,
            "message": (
                "Copied unchanged engines' schema forward. Now dispatch schema design for the "
                "affected_engines (call the listed dispatch_tools in parallel), then call "
                "run_synthesis_via_a2a to rebuild the report at this version."
            ),
        }
    )


@tool
def complete_assessment(job_id: str, database_name: str) -> str:
    """Mark the assessment DONE — the single terminal completion of the job.

    Call this ONLY when the customer, after seeing the report, confirms they have
    no further routing changes (they are done). It marks the platform job
    COMPLETED, which is terminal and cannot be undone: no further re-entry,
    routing edits, or schema/synthesis runs are possible on this job afterward.

    Do NOT call it right after synthesis by default. Between rounds the job rests
    at AWAITING_HUMAN_INPUT so the customer can re-route (reopen_assignment_review
    -> ... -> redispatch_after_reroute). Only their explicit "I'm done" (or
    equivalent) should trigger this.

    Returns JSON with ``status`` ("completed").
    """
    job_id = _platform_job_id(job_id)
    _complete_job_success(job_id)
    logger.info("ATX: assessment marked COMPLETED (terminal) for job_id=%s", job_id)
    return json.dumps(
        {
            "status": "completed",
            "job_id": job_id,
            "message": (
                "Assessment marked complete. The job is now closed; no further routing "
                "changes or re-runs are possible on it."
            ),
        }
    )
