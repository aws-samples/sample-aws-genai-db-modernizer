"""Strands tools that wrap the existing deterministic pipeline.

Each tool maps to a phase (or a group of phases) in the LocalOrchestrator.
The orchestrator LLM calls these — no Bedrock happens inside the pipeline
itself unless the project's own LLM-optional phases are enabled.

Y-3 (F8 fix): The Collect and Triage phases run in DEPLOYED SUBAGENTS via
the AWS Transform A2A protocol. The LLM invokes ``run_collect_via_a2a`` /
``run_triage_via_a2a`` and the tools resolve the subagent BY NAME using
``a2a.invoke_and_wait`` — no ``subagent_instance_id`` is needed at the LLM
layer.

The in-process helpers ``run_collect`` / ``run_triage`` /
``run_collect_and_triage`` remain defined for direct programmatic use by
``scripts/atx_*_test.py`` local tests, but they are NOT registered in
``PIPELINE_TOOLS`` and are therefore invisible to the LLM. This prevents
silent fallback to the monolithic path when a deployed subagent misbehaves.
"""

from __future__ import annotations

import json
import logging
import os

from strands.tools import tool

from src.atx_orchestrator.a2a import A2AError, invoke_and_wait
from src.atx_orchestrator.core import ingest_offline_collection as _ingest_offline_collection
from src.atx_orchestrator.core import make_orchestrator as _make_orchestrator
from src.atx_orchestrator.core import make_store as _make_store
from src.atx_orchestrator.core import run_collect_core, run_collect_triage_core, run_triage_core
from src.atx_orchestrator.job_plan import (
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

    steps = [
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
        {
            "stepLabel": "assignment",
            "stepName": "Route Queries to Engines",
            "description": "Route each query to the best-fit AWS engine.",
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
def run_collect(job_id: str, database_name: str, input_key: str = "") -> str:
    """Run the Collector phase: ingest the offline collection into the artifact store.

    Reads the raw offline collection JSON through the ArtifactStore (local dir or
    S3, controlled by env vars) and writes the collector output contract. Does not
    run triage. Storage-agnostic.

    Args:
        job_id: Unique job identifier.
        database_name: Source database name used to namespace artifacts.
        input_key: ArtifactStore key of the raw offline collection JSON.
            Defaults to "{database_name}/{job_id}/uploads/collector-output.json".

    Returns:
        JSON string with table/query counts and the collector artifact path.
    """
    logger.info("ATX: collect job_id=%s db=%s", job_id, database_name)
    try:
        return json.dumps(run_collect_core(job_id, database_name, input_key))
    except FileNotFoundError as e:
        return json.dumps({"error": str(e), "job_id": job_id})


@tool
def run_triage(job_id: str, database_name: str) -> str:
    """Run the Triage phase: select candidate engines from existing collector output.

    Requires the Collector phase to have run first (reads collector/output.json
    from the ArtifactStore). Pure deterministic pattern matching.

    Args:
        job_id: Unique job identifier.
        database_name: Source database name.

    Returns:
        JSON string with selected engines and signal count.
    """
    logger.info("ATX: triage job_id=%s db=%s", job_id, database_name)
    try:
        return json.dumps(run_triage_core(job_id, database_name))
    except FileNotFoundError as e:
        return json.dumps({"error": str(e), "job_id": job_id})


@tool
def run_collect_and_triage(job_id: str, database_name: str, input_key: str = "") -> str:
    """Convenience: run Collector then Triage in sequence.

    Equivalent to run_collect followed by run_triage. Useful for a single-shot
    Phase 1 when you don't need a review gate between collection and triage.

    Args:
        job_id: Unique job identifier.
        database_name: Source database name.
        input_key: Optional ArtifactStore key of the raw offline collection JSON.

    Returns:
        JSON string with triage results summary (selected engines + signal count).
    """
    logger.info("ATX: collect+triage job_id=%s db=%s", job_id, database_name)
    try:
        summary = run_collect_triage_core(job_id, database_name, input_key)
    except FileNotFoundError as e:
        return json.dumps({"error": str(e), "job_id": job_id})
    return json.dumps(summary)


@tool
def run_assignment(job_id: str, database_name: str) -> str:
    """Run Phase 3: score all queries against selected engines and produce an assignment.

    Requires collect+triage and analysis to be complete first.

    Args:
        job_id: Unique job identifier.
        database_name: Source database name.

    Returns:
        JSON string with assignment summary (query count per engine).
    """
    store = _make_store()
    orch = _make_orchestrator(store)

    logger.info("ATX: running assignment job_id=%s db=%s", job_id, database_name)
    from src.contracts.phase_models import Phase

    orch.resume_lenient(job_id, Phase.ASSIGNMENT)

    # Summarise assignment
    assignment_key = f"{database_name}/{job_id}/assignment/v1/assignment.json"
    engine_counts: dict[str, int] = {}
    total_queries = 0
    if store.exists(assignment_key):
        assignment = store.read_json(assignment_key)
        for qa in assignment.get("query_assignments", []):
            e = qa.get("assigned_engine", "unknown")
            engine_counts[e] = engine_counts.get(e, 0) + 1
            total_queries += 1

    return json.dumps(
        {
            "job_id": job_id,
            "assignment_version": 1,
            "total_queries": total_queries,
            "queries_per_engine": engine_counts,
        }
    )


@tool
def run_reality_check(job_id: str, database_name: str) -> str:
    """Run Phase 4: CTO-level engine consolidation and architectural pattern detection.

    Eliminates redundant engines, identifies CQRS / Materialized View / Event Sourcing patterns.
    Requires assignment to be complete first.

    Args:
        job_id: Unique job identifier.
        database_name: Source database name.

    Returns:
        JSON string with consolidation decisions and recommended architectural patterns.
    """
    store = _make_store()
    orch = _make_orchestrator(store)

    logger.info("ATX: running reality-check job_id=%s db=%s", job_id, database_name)
    from src.contracts.phase_models import Phase

    orch.resume_lenient(job_id, Phase.REALITY_CHECK)

    reality_key = f"{database_name}/{job_id}/reality-check/output.json"
    consolidations: list[str] = []
    patterns: list[str] = []
    if store.exists(reality_key):
        rc = store.read_json(reality_key)
        consolidations = [
            f"{c.get('from_engine')} → {c.get('to_engine')}: {c.get('reason', '')}"
            for c in rc.get("consolidations", [])
        ]
        patterns = [p.get("name", "") for p in rc.get("architectural_patterns", [])]

    return json.dumps(
        {
            "job_id": job_id,
            "consolidations": consolidations,
            "architectural_patterns": patterns,
        }
    )


@tool
def run_schema_design(job_id: str, database_name: str) -> str:
    """Run Phase 5: design target schemas for each assigned engine.

    Produces DynamoDB table definitions, DocumentDB collections, OpenSearch index
    mappings, ElastiCache data structures, and Aurora DDL as appropriate.

    Args:
        job_id: Unique job identifier.
        database_name: Source database name.

    Returns:
        JSON string listing engines for which schema output was produced.
    """
    store = _make_store()
    orch = _make_orchestrator(store)

    logger.info("ATX: running schema-design job_id=%s db=%s", job_id, database_name)
    from src.contracts.phase_models import Phase

    orch.resume_lenient(job_id, Phase.SCHEMA_DESIGN)
    orch.confirm_schema_design(job_id)

    # Discover which engines have schema output
    engines_designed: list[str] = []
    for engine in [
        "dynamodb",
        "documentdb",
        "opensearch",
        "elasticache",
        "aurora_postgresql",
        "aurora_mysql",
    ]:
        key = f"{database_name}/{job_id}/schema-{engine}/v1/schema_output.json"
        if store.exists(key):
            engines_designed.append(engine)

    return json.dumps(
        {
            "job_id": job_id,
            "engines_with_schema": engines_designed,
        }
    )


@tool
def run_synthesis(job_id: str, database_name: str) -> str:
    """Run Phase 6: synthesise a final migration report with TCO, risk, and recommendations.

    Args:
        job_id: Unique job identifier.
        database_name: Source database name.

    Returns:
        JSON string with the artifact path for the synthesis report.
    """
    store = _make_store()
    orch = _make_orchestrator(store)

    logger.info("ATX: running synthesis job_id=%s db=%s", job_id, database_name)
    from src.contracts.phase_models import Phase

    orch.resume_lenient(job_id, Phase.SYNTHESIS)

    report_key = f"{database_name}/{job_id}/synthesis/report.json"
    return json.dumps(
        {
            "job_id": job_id,
            "report_artifact": report_key,
            "available": store.exists(report_key),
        }
    )


@tool
def run_full_assessment(job_id: str, database_name: str) -> str:
    """Run the complete DB modernization assessment pipeline end-to-end.

    Executes all phases in order:
      Collect+Triage → Analysis (fan-out) → Assignment → Reality Check
      → Schema Design (fan-out) → Synthesis

    Use this for a single-shot assessment. Use the individual phase tools
    if you need to pause between phases for human review.

    Args:
        job_id: Unique job identifier.
        database_name: Source database name.

    Returns:
        JSON string with per-phase status and the synthesis report artifact path.
    """
    store = _make_store()
    orch = _make_orchestrator(store)

    logger.info("ATX: running full assessment job_id=%s db=%s", job_id, database_name)
    from src.contracts.phase_models import Phase

    # Phase 1: ingest collection + triage via the storage-agnostic path
    # (do NOT use start_job — that calls the boto3 ECS collector handler).
    key = f"{database_name}/{job_id}/uploads/collector-output.json"
    if not store.exists(key):
        return json.dumps(
            {
                "error": f"Offline collection input not found at '{key}'.",
                "job_id": job_id,
            }
        )
    raw_input = store.read_json(key)
    _ingest_offline_collection(store, job_id, database_name, raw_input)

    from src.agents.referee.triage_handler import run_triage as _run_triage_handler

    _run_triage_handler(job_id, database_name, store)

    # Mark COLLECT_TRIAGE complete in progression so resume() prerequisites pass.
    from src.contracts.phase_models import PhaseStatus

    progression = orch.get_progression(job_id)
    orch._set_phase_status(progression, Phase.COLLECT_TRIAGE, PhaseStatus.COMPLETED)
    orch._save_progression(progression)

    # Phase 2–6: resume each remaining phase (analysis through synthesis)
    for phase in [
        Phase.ANALYSIS,
        Phase.ASSIGNMENT,
        Phase.REALITY_CHECK,
        Phase.SCHEMA_DESIGN,
        Phase.SYNTHESIS,
    ]:
        orch.resume_lenient(job_id, phase)
        if phase == Phase.SCHEMA_DESIGN:
            orch.confirm_schema_design(job_id)

    progression = orch.get_progression(job_id)
    phase_statuses = {p.value: progression.phases[p].status.value for p in Phase}

    report_key = f"{database_name}/{job_id}/synthesis/report.json"
    return json.dumps(
        {
            "job_id": job_id,
            "database_name": database_name,
            "phase_statuses": phase_statuses,
            "report_artifact": report_key,
            "report_available": store.exists(report_key),
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


@tool
def run_collect_via_a2a(
    job_id: str,
    database_name: str,
    input_key: str = "",
) -> str:
    """Run the Collector phase by invoking a deployed collector subagent over A2A.

    Uses the AWS Transform Agentic API's ``invoke_agent`` primitive to spawn
    a fresh collector subagent instance BY NAME and deliver the initial
    message atomically. Polls until the subagent reports COMPLETED (or
    FAILED). The subagent's ``agent_output`` payload is returned as a JSON
    string.

    This is the ONLY collector tool available to the orchestrator — the
    in-process variant was removed to prevent silent fallback. The subagent
    must be deployed and its registered NAME must be
    ``db-modernization-collector``.

    Args:
        job_id: Unique job identifier.
        database_name: Source database name used to namespace artifacts.
        input_key: Optional ArtifactStore key of the raw offline collection JSON.

    Returns:
        JSON string with the subagent's completion payload, or an error dict
        if the A2A round-trip failed (timeout, FAILED status, network, etc.).
    """
    agent_id = f"{_AGENT_PREFIX}-collector"
    logger.info(
        "ATX: collect via A2A agent=%s job_id=%s db=%s",
        agent_id,
        job_id,
        database_name,
    )
    message = json.dumps(
        {
            "job_id": job_id,
            "database_name": database_name,
            "input_key": input_key,
        }
    )
    mark_step_running("collector")
    try:
        payload = invoke_and_wait(agent_id, message)
    except A2AError as e:
        logger.error("ATX collect FAILED: %s: %s", type(e).__name__, e)
        mark_step_failed("collector", str(e))
        return json.dumps(
            {
                "error": f"A2A collect failed: {e}",
                "error_type": type(e).__name__,
                "job_id": job_id,
                "agent_id": agent_id,
            }
        )
    mark_step_succeeded("collector")
    return json.dumps(payload)


@tool
def run_triage_via_a2a(
    job_id: str,
    database_name: str,
) -> str:
    """Run the Triage phase by invoking a deployed triage subagent over A2A.

    Requires the Collector phase to have run first (so the subagent can read
    ``collector/output.json`` from shared artifact storage — S3 in the deployed
    case). Uses ``invoke_agent`` to spawn a fresh triage subagent instance
    BY NAME and deliver the initial message atomically. Polls until terminal
    status.

    This is the ONLY triage tool available to the orchestrator — the in-process
    variant was removed to prevent silent fallback. The subagent must be
    deployed and its registered NAME must be ``db-modernization-triage``.

    Args:
        job_id: Unique job identifier.
        database_name: Source database name.

    Returns:
        JSON string with the subagent's completion payload, or an error dict
        if the A2A round-trip failed.
    """
    agent_id = f"{_AGENT_PREFIX}-triage"
    logger.info(
        "ATX: triage via A2A agent=%s job_id=%s db=%s",
        agent_id,
        job_id,
        database_name,
    )
    message = json.dumps(
        {
            "job_id": job_id,
            "database_name": database_name,
        }
    )
    mark_step_running("triage")
    try:
        payload = invoke_and_wait(agent_id, message)
    except A2AError as e:
        logger.error("ATX triage FAILED: %s: %s", type(e).__name__, e)
        mark_step_failed("triage", str(e))
        return json.dumps(
            {
                "error": f"A2A triage failed: {e}",
                "error_type": type(e).__name__,
                "job_id": job_id,
                "agent_id": agent_id,
            }
        )
    mark_step_succeeded("triage")
    return json.dumps(payload)


@tool
def run_analysis_dynamodb_via_a2a(
    job_id: str,
    database_name: str,
) -> str:
    """Run DynamoDB analysis by invoking a deployed analysis subagent over A2A.

    Requires the Collector phase to have run first (so the subagent can read
    ``collector/output.json`` from shared artifact storage — S3 in the deployed
    case). Triage is NOT a hard prerequisite — analysis reads from collector
    output directly and produces recommendations independently.

    Uses ``invoke_agent`` to spawn a fresh analysis-dynamodb subagent instance
    BY NAME and deliver the initial message atomically. Polls until terminal
    status. The subagent writes 3 artifacts to S3:

      - ``<db>/<job>/analysis-dynamodb/analysis.json``      — recommendations
      - ``<db>/<job>/analysis-dynamodb/decision-trace.json`` — per-query trace
      - ``<db>/<job>/analysis-dynamodb/er-diagram.mmd``      — Mermaid ER diagram

    This is the ONLY DynamoDB analysis tool available to the orchestrator —
    no in-process variant exists (matches the pattern from A14). The subagent
    must be deployed and its registered NAME must be
    ``db-modernization-analysis-dynamodb``.

    Args:
        job_id: Unique job identifier.
        database_name: Source database name.

    Returns:
        JSON string with the subagent's completion payload (tables_analyzed,
        confidence-band counts, estimated cost, artifact keys), or an error
        dict if the A2A round-trip failed.
    """
    agent_id = f"{_AGENT_PREFIX}-analysis-dynamodb"
    logger.info(
        "ATX: analysis-dynamodb via A2A agent=%s job_id=%s db=%s",
        agent_id,
        job_id,
        database_name,
    )
    message = json.dumps(
        {
            "job_id": job_id,
            "database_name": database_name,
        }
    )
    mark_step_running("analysis_dynamodb")
    try:
        # 90-minute timeout: DynamoDB LLM Advisor runs Bedrock Opus 4.8 across
        # groups of ~30 queries (~60-90 sec/group). For a 1600-query workload
        # like Discourse that's ~55 groups → ~55-80 min end-to-end. The default
        # 300s timeout is fine for collector/triage but far too short for the
        # LLM-heavy analysis path.
        payload = invoke_and_wait(agent_id, message, timeout=5400.0)
    except A2AError as e:
        logger.error("ATX analysis-dynamodb FAILED: %s: %s", type(e).__name__, e)
        mark_step_failed("analysis_dynamodb", str(e))
        return json.dumps(
            {
                "error": f"A2A analysis-dynamodb failed: {e}",
                "error_type": type(e).__name__,
                "job_id": job_id,
                "agent_id": agent_id,
            }
        )
    mark_step_succeeded("analysis_dynamodb")
    return json.dumps(payload)


@tool
def run_analysis_documentdb_via_a2a(
    job_id: str,
    database_name: str,
) -> str:
    """Run DocumentDB analysis by invoking a deployed analysis subagent over A2A.

    Requires the Collector phase to have run first (so the subagent can read
    ``collector/output.json`` from shared artifact storage — S3 in the deployed
    case). Triage is NOT a hard prerequisite — analysis reads from collector
    output directly and produces recommendations independently.

    Uses ``invoke_agent`` to spawn a fresh analysis-documentdb subagent instance
    BY NAME and deliver the initial message atomically. Polls until terminal
    status. The subagent writes 3 artifacts to S3:

      - ``<db>/<job>/analysis-documentdb/analysis.json``      — recommendations
      - ``<db>/<job>/analysis-documentdb/decision-trace.json`` — per-query trace
      - ``<db>/<job>/analysis-documentdb/er-diagram.mmd``      — Mermaid ER diagram

    This is the ONLY DocumentDB analysis tool available to the orchestrator —
    no in-process variant exists (matches the pattern from A14 / Phase A Half 2).
    The subagent must be deployed and its registered NAME must be
    ``db-modernization-analysis-documentdb``.

    Args:
        job_id: Unique job identifier.
        database_name: Source database name.

    Returns:
        JSON string with the subagent's completion payload (tables_analyzed,
        confidence-band counts, estimated cost, artifact keys), or an error
        dict if the A2A round-trip failed.
    """
    agent_id = f"{_AGENT_PREFIX}-analysis-documentdb"
    logger.info(
        "ATX: analysis-documentdb via A2A agent=%s job_id=%s db=%s",
        agent_id,
        job_id,
        database_name,
    )
    message = json.dumps(
        {
            "job_id": job_id,
            "database_name": database_name,
        }
    )
    mark_step_running("analysis_documentdb")
    try:
        # 90-minute timeout: DocumentDB LlmAdvisor runs Bedrock Opus 4.8 for
        # embedding-vs-reference trade-off analysis. Uses same 5400s ceiling
        # as DynamoDB — Discourse-scale workloads can hit similar tail latency
        # across the LlmAdvisor's chunked query groups.
        payload = invoke_and_wait(agent_id, message, timeout=5400.0)
    except A2AError as e:
        logger.error("ATX analysis-documentdb FAILED: %s: %s", type(e).__name__, e)
        mark_step_failed("analysis_documentdb", str(e))
        return json.dumps(
            {
                "error": f"A2A analysis-documentdb failed: {e}",
                "error_type": type(e).__name__,
                "job_id": job_id,
                "agent_id": agent_id,
            }
        )
    mark_step_succeeded("analysis_documentdb")
    return json.dumps(payload)


@tool
def run_analysis_elasticache_via_a2a(
    job_id: str,
    database_name: str,
) -> str:
    """Run ElastiCache/Redis analysis by invoking a deployed analysis subagent over A2A.

    Requires the Collector phase to have run first. Deterministic — no LLM
    invocation. Uses ``invoke_agent`` to spawn a fresh analysis-elasticache
    subagent instance BY NAME. Polls until terminal status. The subagent
    writes 2-3 artifacts to S3:

      - ``<db>/<job>/analysis-elasticache/analysis.json``      — recommendations
      - ``<db>/<job>/analysis-elasticache/decision-trace.json`` — per-query trace
      - ``<db>/<job>/analysis-elasticache/er-diagram.mmd``      — optional (only if produced)

    The subagent must be deployed and its registered NAME must be
    ``db-modernization-analysis-elasticache``.

    Args:
        job_id: Unique job identifier.
        database_name: Source database name.

    Returns:
        JSON string with the subagent's completion payload, or an error dict.
    """
    agent_id = f"{_AGENT_PREFIX}-analysis-elasticache"
    logger.info(
        "ATX: analysis-elasticache via A2A agent=%s job_id=%s db=%s",
        agent_id,
        job_id,
        database_name,
    )
    message = json.dumps(
        {
            "job_id": job_id,
            "database_name": database_name,
        }
    )
    mark_step_running("analysis_elasticache")
    try:
        # Deterministic path — 30-min default timeout is plenty. No Bedrock calls.
        payload = invoke_and_wait(agent_id, message)
    except A2AError as e:
        logger.error("ATX analysis-elasticache FAILED: %s: %s", type(e).__name__, e)
        mark_step_failed("analysis_elasticache", str(e))
        return json.dumps(
            {
                "error": f"A2A analysis-elasticache failed: {e}",
                "error_type": type(e).__name__,
                "job_id": job_id,
                "agent_id": agent_id,
            }
        )
    mark_step_succeeded("analysis_elasticache")
    return json.dumps(payload)


@tool
def run_analysis_opensearch_via_a2a(
    job_id: str,
    database_name: str,
) -> str:
    """Run OpenSearch analysis by invoking a deployed analysis subagent over A2A.

    Requires the Collector phase to have run first. Deterministic — no LLM
    invocation. Uses ``invoke_agent`` to spawn a fresh analysis-opensearch
    subagent instance BY NAME. Polls until terminal status. The subagent
    writes 2 artifacts to S3 (no ER diagram for OpenSearch):

      - ``<db>/<job>/analysis-opensearch/analysis.json``      — recommendations
      - ``<db>/<job>/analysis-opensearch/decision-trace.json`` — per-query trace

    The subagent must be deployed and its registered NAME must be
    ``db-modernization-analysis-opensearch``.

    Args:
        job_id: Unique job identifier.
        database_name: Source database name.

    Returns:
        JSON string with the subagent's completion payload, or an error dict.
    """
    agent_id = f"{_AGENT_PREFIX}-analysis-opensearch"
    logger.info(
        "ATX: analysis-opensearch via A2A agent=%s job_id=%s db=%s",
        agent_id,
        job_id,
        database_name,
    )
    message = json.dumps(
        {
            "job_id": job_id,
            "database_name": database_name,
        }
    )
    mark_step_running("analysis_opensearch")
    try:
        # Deterministic path — 30-min default timeout is plenty.
        payload = invoke_and_wait(agent_id, message)
    except A2AError as e:
        logger.error("ATX analysis-opensearch FAILED: %s: %s", type(e).__name__, e)
        mark_step_failed("analysis_opensearch", str(e))
        return json.dumps(
            {
                "error": f"A2A analysis-opensearch failed: {e}",
                "error_type": type(e).__name__,
                "job_id": job_id,
                "agent_id": agent_id,
            }
        )
    mark_step_succeeded("analysis_opensearch")
    return json.dumps(payload)


@tool
def run_analysis_aurora_pg_via_a2a(
    job_id: str,
    database_name: str,
) -> str:
    """Run Aurora PostgreSQL analysis by invoking a deployed subagent over A2A.

    Requires the Collector phase to have run first. Deterministic — no LLM
    invocation. Only meaningful for PostgreSQL source engines. Subagent name:
    ``db-modernization-analysis-aurora-pg``.

    Args:
        job_id: Unique job identifier.
        database_name: Source database name.

    Returns:
        JSON string with the subagent's completion payload, or an error dict.
    """
    agent_id = f"{_AGENT_PREFIX}-analysis-aurora-pg"
    logger.info(
        "ATX: analysis-aurora-pg via A2A agent=%s job_id=%s db=%s",
        agent_id,
        job_id,
        database_name,
    )
    message = json.dumps(
        {
            "job_id": job_id,
            "database_name": database_name,
        }
    )
    mark_step_running("analysis_aurora_postgresql")
    try:
        payload = invoke_and_wait(agent_id, message)
    except A2AError as e:
        logger.error("ATX analysis-aurora-pg FAILED: %s: %s", type(e).__name__, e)
        mark_step_failed("analysis_aurora_postgresql", str(e))
        return json.dumps(
            {
                "error": f"A2A analysis-aurora-pg failed: {e}",
                "error_type": type(e).__name__,
                "job_id": job_id,
                "agent_id": agent_id,
            }
        )
    mark_step_succeeded("analysis_aurora_postgresql")
    return json.dumps(payload)


@tool
def run_analysis_aurora_mysql_via_a2a(
    job_id: str,
    database_name: str,
) -> str:
    """Run Aurora MySQL analysis by invoking a deployed subagent over A2A.

    Requires the Collector phase to have run first. Deterministic — no LLM
    invocation. Only meaningful for MySQL/MariaDB source engines. Subagent
    name: ``db-modernization-analysis-aurora-mysql``.

    Args:
        job_id: Unique job identifier.
        database_name: Source database name.

    Returns:
        JSON string with the subagent's completion payload, or an error dict.
    """
    agent_id = f"{_AGENT_PREFIX}-analysis-aurora-mysql"
    logger.info(
        "ATX: analysis-aurora-mysql via A2A agent=%s job_id=%s db=%s",
        agent_id,
        job_id,
        database_name,
    )
    message = json.dumps(
        {
            "job_id": job_id,
            "database_name": database_name,
        }
    )
    mark_step_running("analysis_aurora_mysql")
    try:
        payload = invoke_and_wait(agent_id, message)
    except A2AError as e:
        logger.error("ATX analysis-aurora-mysql FAILED: %s: %s", type(e).__name__, e)
        mark_step_failed("analysis_aurora_mysql", str(e))
        return json.dumps(
            {
                "error": f"A2A analysis-aurora-mysql failed: {e}",
                "error_type": type(e).__name__,
                "job_id": job_id,
                "agent_id": agent_id,
            }
        )
    mark_step_succeeded("analysis_aurora_mysql")
    return json.dumps(payload)


@tool
def run_assignment_via_a2a(
    job_id: str,
    database_name: str,
) -> str:
    """Run Assignment by invoking a deployed subagent over A2A.

    Requires the Collector + Triage + Analysis phases to have run first.
    Deterministic — no LLM invocation. The assignment subagent scores every
    query against each candidate engine's analysis output and produces a
    per-query assignment mapping.

    Subagent name: ``db-modernization-assignment``. Writes 1 artifact:

      - ``<db>/<job>/assignment/v1/assignment.json`` — query -> engine mapping

    Args:
        job_id: Unique job identifier.
        database_name: Source database name.

    Returns:
        JSON string with the subagent's completion payload
        (total_queries, queries_per_engine, assignment_artifact).
    """
    agent_id = f"{_AGENT_PREFIX}-assignment"
    logger.info(
        "ATX: assignment via A2A agent=%s job_id=%s db=%s",
        agent_id,
        job_id,
        database_name,
    )
    message = json.dumps(
        {
            "job_id": job_id,
            "database_name": database_name,
        }
    )
    mark_step_running("assignment")
    try:
        # Deterministic path — 30-min default timeout is plenty.
        payload = invoke_and_wait(agent_id, message)
    except A2AError as e:
        logger.error("ATX assignment FAILED: %s: %s", type(e).__name__, e)
        mark_step_failed("assignment", str(e))
        return json.dumps(
            {
                "error": f"A2A assignment failed: {e}",
                "error_type": type(e).__name__,
                "job_id": job_id,
                "agent_id": agent_id,
            }
        )
    mark_step_succeeded("assignment")
    return json.dumps(payload)


@tool
def run_synthesis_via_a2a(
    job_id: str,
    database_name: str,
    assignment_version: int = 1,
) -> str:
    """Run Referee-Synthesis by invoking a deployed subagent over A2A.

    Requires Collector + Triage + at least one Analysis + Assignment to have run
    first. Reality-check and schema-design are optional enrichments, not
    prerequisites — synthesis proceeds without them, though ``query_groups`` will
    be empty until schema-design has run, since those are built from the schema
    output's access patterns.

    Produces the consolidated report a customer actually reads: engine ranking,
    table mappings, TCO comparison, risk assessment, and a recommended
    architecture. Deterministic-first, then one Bedrock call for the executive
    summary — matching core-modernizer, which invokes ``run_synthesis`` without
    an ``llm_mode`` argument and so takes its ``"bedrock"`` default.

    Writes 1 artifact, at a key that depends on the version:

      - ``<db>/<job>/synthesis/v<N>/report.json``    when assignment_version > 0
      - ``<db>/<job>/referee-synthesis/report.json`` when assignment_version = 0

    Args:
        job_id: Unique job identifier.
        database_name: Source database name.
        assignment_version: Version the assignment agent produced. Defaults to 1
            because ``run_assignment_core`` writes ``assignment/v1/``. Do not
            pass 0 — at version 0 synthesis never reads the assignment at all and
            emits a report whose recommended architecture, table mappings and
            query groups are all empty.

    Returns:
        JSON string with the subagent's completion payload (engines_ranked,
        top_engine, architecture_type, recommended_databases, table_mappings,
        query_groups, overall_risk_level, has_executive_summary,
        report_artifact).
    """
    agent_id = f"{_AGENT_PREFIX}-synthesis"
    logger.info(
        "ATX: synthesis via A2A agent=%s job_id=%s db=%s assignment_version=%s",
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
    mark_step_running("synthesis")
    try:
        payload = invoke_and_wait(agent_id, message)
    except A2AError as e:
        logger.error("ATX synthesis FAILED: %s: %s", type(e).__name__, e)
        mark_step_failed("synthesis", str(e))
        return json.dumps(
            {
                "error": f"A2A synthesis failed: {e}",
                "error_type": type(e).__name__,
                "job_id": job_id,
                "agent_id": agent_id,
            }
        )
    mark_step_succeeded("synthesis")
    return json.dumps(payload)
