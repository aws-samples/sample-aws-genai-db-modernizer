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

from strands.tools import tool

from src.atx_orchestrator.a2a import A2AError, invoke_and_wait
from src.atx_orchestrator.core import ingest_offline_collection as _ingest_offline_collection
from src.atx_orchestrator.core import make_orchestrator as _make_orchestrator
from src.atx_orchestrator.core import make_store as _make_store
from src.atx_orchestrator.core import run_collect_core, run_collect_triage_core, run_triage_core

logger = logging.getLogger(__name__)


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
    agent_id = "db-modernization-collector"
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
    try:
        payload = invoke_and_wait(agent_id, message)
    except A2AError as e:
        logger.error("ATX collect FAILED: %s: %s", type(e).__name__, e)
        return json.dumps(
            {
                "error": f"A2A collect failed: {e}",
                "error_type": type(e).__name__,
                "job_id": job_id,
                "agent_id": agent_id,
            }
        )
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
    agent_id = "db-modernization-triage"
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
    try:
        payload = invoke_and_wait(agent_id, message)
    except A2AError as e:
        logger.error("ATX triage FAILED: %s: %s", type(e).__name__, e)
        return json.dumps(
            {
                "error": f"A2A triage failed: {e}",
                "error_type": type(e).__name__,
                "job_id": job_id,
                "agent_id": agent_id,
            }
        )
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
    agent_id = "db-modernization-analysis-dynamodb"
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
    try:
        # 90-minute timeout: DynamoDB LLM Advisor runs Bedrock Opus 4.8 across
        # groups of ~30 queries (~60-90 sec/group). For a 1600-query workload
        # like Discourse that's ~55 groups → ~55-80 min end-to-end. The default
        # 300s timeout is fine for collector/triage but far too short for the
        # LLM-heavy analysis path.
        payload = invoke_and_wait(agent_id, message, timeout=5400.0)
    except A2AError as e:
        logger.error("ATX analysis-dynamodb FAILED: %s: %s", type(e).__name__, e)
        return json.dumps(
            {
                "error": f"A2A analysis-dynamodb failed: {e}",
                "error_type": type(e).__name__,
                "job_id": job_id,
                "agent_id": agent_id,
            }
        )
    return json.dumps(payload)
