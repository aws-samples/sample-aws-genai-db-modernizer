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
    mark_step_running,
    mark_step_succeeded,
    put_job_plan,
    register_steps,
)

logger = logging.getLogger(__name__)

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
                    "stepName": "Design DynamoDB Schema",
                    "description": "Design tables and access patterns for the DynamoDB target.",
                },
                {
                    "stepLabel": "schema_documentdb",
                    "stepName": "Design DocumentDB Schema",
                    "description": (
                        "Design collections and access patterns for the DocumentDB target."
                    ),
                },
                {
                    "stepLabel": "schema_elasticache",
                    "stepName": "Design ElastiCache Schema",
                    "description": (
                        "Design key structures and access patterns for the ElastiCache target."
                    ),
                },
                {
                    "stepLabel": "schema_opensearch",
                    "stepName": "Design OpenSearch Schema",
                    "description": (
                        "Design index mappings and access patterns for the OpenSearch target."
                    ),
                },
                {
                    "stepLabel": "schema_aurora_postgresql",
                    "stepName": "Design Aurora PostgreSQL Schema",
                    "description": "Assess schema design coverage for the Aurora PostgreSQL target.",
                },
                {
                    "stepLabel": "schema_aurora_mysql",
                    "stepName": "Design Aurora MySQL Schema",
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
    # mark the parent, unlike the in-process analysis phase).
    mark_step_succeeded("schema")
    return _run_phase_via_a2a(
        agent_suffix="synthesis",
        step="synthesis",
        label="synthesis",
        job_id=job_id,
        database_name=database_name,
        message=message,
        on_success=lambda payload: _publish_synthesis_deliverables(job_id, database_name, payload),
    )


def _publish_synthesis_deliverables(job_id: str, database_name: str, payload: dict) -> None:
    """Render the two audience-shaped reports from the synthesis report.json and
    publish all three deliverables as CUSTOMER_OUTPUT.

    The orchestrator owns this, not the subagent, because it owns the synthesis
    plan step. Three artifacts reach the WebApp Artifacts panel: Decision Report
    (executive HTML), Engineering Report (build-team Markdown) and Assessment
    Data (the raw report JSON). Each rendered report is also written to our own
    S3 bucket, which is the system of record and survives the customer stopping
    the job; report.json is already there (the subagent wrote it).

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

        decision_html = _artifacts.render_decision_report_html(
            report, trust_generated_summary=trust
        )
        engineering_md = _artifacts.render_engineering_report_md(report)

        # S3-first: our bucket is the system of record (survives job stop).
        store.write_text(f"{base}/decision-report-{database_name}.html", decision_html, "text/html")
        store.write_text(
            f"{base}/engineering-report-{database_name}.md", engineering_md, "text/markdown"
        )

        # Register all three in the WebApp panel. CUSTOMER_OUTPUT is accepted from
        # the agent side (constraint C2, verified 2026-08-24).
        _artifacts.publish(
            [
                (decision_html.encode("utf-8"), "HTML", "Decision Report", "CUSTOMER_OUTPUT"),
                (
                    engineering_md.encode("utf-8"),
                    "MARKDOWN",
                    "Engineering Report",
                    "CUSTOMER_OUTPUT",
                ),
                (
                    json.dumps(report, indent=2).encode("utf-8"),
                    "JSON",
                    "Assessment Data",
                    "CUSTOMER_OUTPUT",
                ),
            ]
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "ATX synthesis deliverables skipped (report is durable in S3): %s: %s",
            type(e).__name__,
            e,
        )


# Target engine per schema-design agent, keyed by the suffix used in both the
# agent id and the plan step. Mirrors SCHEMA_TARGETS in schema_subagent.py; the
# two differ because agent ids use hyphens while artifact keys and upstream's
# dispatch use the engine's own identifier.
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


def _run_schema_design_via_a2a(
    suffix: str,
    job_id: str,
    database_name: str,
) -> str:
    """Shared body for the six schema-design A2A tools."""
    agent_id = f"{_AGENT_PREFIX}-schema-{suffix}"
    # Plan step labels use the engine's own identifier with underscores
    # (schema_aurora_postgresql), while agent ids use hyphens
    # (schema-aurora-pg). A mismatch here is silent: mark_step_* ignores an
    # unregistered phase name, so progress would simply never appear.
    step = f"schema_{_SCHEMA_ENGINES[suffix]}"
    # Resolve the version in Python, not via the LLM (ADR-026): picks up the v2
    # assignment when Reality Check consolidated, else v1.
    assignment_version = _effective_assignment_version(job_id, database_name)
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
