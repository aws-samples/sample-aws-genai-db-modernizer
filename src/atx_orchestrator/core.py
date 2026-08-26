"""Shared pipeline logic for the AWS Transform integration.

This module holds the storage-agnostic functions that BOTH the orchestrator
tools (tools.py) and the subagent wrappers call. Single source of truth — no
duplication between the in-process tool path and the A2A subagent path.

Everything here operates on the ArtifactStore abstraction. Storage type
(local dir vs S3) is controlled by env vars via create_artifact_store().
"""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)


def make_store():
    """Create a text-capable ArtifactStore using the project's own factory.

    core-modernizer's ``create_artifact_store()`` decides S3-vs-local from env;
    ``upgrade_store`` re-homes that choice onto the Transform subclass that adds
    ``write_text``. See src/atx_orchestrator/store.py for why that capability is
    not on the shared ABC.
    """
    from src.atx_orchestrator.store import upgrade_store
    from src.storage import create_artifact_store

    return upgrade_store(create_artifact_store())


def make_orchestrator(store=None):
    """Create a LocalOrchestrator bound to the given store.

    llm_mode="none" keeps all phases fully deterministic. Set LLM_MODE=bedrock
    to enable the optional LLM schema-design / synthesis passes.
    """
    from src.orchestrator.local_orchestrator import LocalOrchestrator

    s = store or make_store()
    llm_mode = os.environ.get("LLM_MODE", "none")
    return LocalOrchestrator(store=s, llm_mode=llm_mode)


def default_input_key(job_id: str, database_name: str) -> str:
    """Default ArtifactStore key for the raw offline collection JSON."""
    return f"{database_name}/{job_id}/uploads/collector-output.json"


def ingest_offline_collection(store, job_id: str, database_name: str, raw_input: dict) -> dict:
    """Build a collector output contract from a raw offline collection dict.

    Mirrors scripts/run_assessment.py::phase_collect exactly, but reads the raw
    input from a dict (sourced via ArtifactStore) instead of a local file, and
    writes the result back through the ArtifactStore. Reuses the existing
    builder functions — no new parsing logic.

    Returns the collector output dict that was written.
    """
    import time

    from src.agents.collector.mysql_collector import (
        _build_metrics,
        _build_output,
        _build_procedures,
        _build_queries,
        _build_tables,
        _build_triggers,
        _build_views,
    )
    from src.contracts.collector_input import CollectorInput
    from src.tools.database.offline_parser import parse_offline_collection

    contract_version = raw_input.get("contract_version")
    is_contract = isinstance(contract_version, str) and contract_version.startswith("3")

    if is_contract:
        collector_data = raw_input
    else:
        parsed = parse_offline_collection(raw_input)
        start = time.monotonic()

        tables_built = _build_tables(parsed["tables"], database_name)
        queries_built = _build_queries(parsed.get("queries", []))
        views = _build_views(parsed.get("views", []))
        procedures = _build_procedures(parsed.get("procedures", []))
        triggers = _build_triggers(parsed.get("triggers", []))
        metrics = _build_metrics(queries_built, None)

        version_str = (parsed.get("metadata", {}).get("version") or "").lower()
        if "postgresql" in version_str or "postgres" in version_str:
            detected_engine = "postgresql"
            detected_port = 5432
        else:
            detected_engine = "mysql"
            detected_port = 3306

        inp = CollectorInput.model_validate(
            {
                "job_id": job_id,
                "engine": detected_engine,
                "cluster_endpoint": "offline",
                "port": detected_port,
                "database_name": database_name,
                "mode": "offline",
                "offline_config": {"s3_bucket": "local", "s3_key": "in-memory"},
            }
        )
        offline_meta = parsed.get("metadata", {})
        collector_data = json.loads(
            _build_output(
                inp,
                start,
                version=offline_meta.get("version", "unknown"),
                db_size=offline_meta.get("database_size_gb"),
                tables=tables_built,
                queries=queries_built,
                metrics=metrics,
                rds_meta=None,
                views=views,
                procedures=procedures,
                triggers=triggers,
            ).model_dump_json()
        )

    collector_key = f"{database_name}/{job_id}/collector/output.json"
    store.write_json(collector_key, collector_data)

    from src.agents.query_journey_materializer import materialize_source

    materialize_source(collector_data, database_name, job_id, store)
    return collector_data


def run_collect_core(job_id: str, database_name: str, input_key: str = "", store=None) -> dict:
    """Ingest offline collection only (no triage). Returns a summary dict.

    Wraps the collector step exactly as the existing collector agent does, reading
    the raw offline collection through the ArtifactStore and writing the collector
    output contract. This maps one-to-one to AGENT_TYPE='collector'.

    Raises FileNotFoundError if the offline input is missing.
    """
    store = store or make_store()
    key = input_key or default_input_key(job_id, database_name)

    if not store.exists(key):
        raise FileNotFoundError(
            f"Offline collection input not found at '{key}'. "
            "Upload the collection JSON to the artifact store first."
        )

    raw_input = store.read_json(key)
    collector_data = ingest_offline_collection(store, job_id, database_name, raw_input)

    return {
        "job_id": job_id,
        "database_name": database_name,
        "tables": len(collector_data.get("database_schema", {}).get("tables", [])),
        "queries": len(collector_data.get("queries", {}).get("query_patterns", [])),
        "collector_artifact": f"{database_name}/{job_id}/collector/output.json",
    }


def run_triage_core(job_id: str, database_name: str, store=None) -> dict:
    """Run triage only (assumes collector output already exists). Returns a summary dict.

    Maps one-to-one to AGENT_TYPE='referee-triage'. Reads the collector output from
    the ArtifactStore and writes the triage decision.

    Raises FileNotFoundError if the collector output is missing.
    """
    store = store or make_store()
    collector_key = f"{database_name}/{job_id}/collector/output.json"
    if not store.exists(collector_key):
        raise FileNotFoundError(
            f"Collector output not found at '{collector_key}'. Run the collector agent first."
        )

    from src.agents.referee.triage_handler import run_triage

    run_triage(job_id, database_name, store)

    triage_key = f"{database_name}/{job_id}/referee-triage/triage.json"
    selected_engines: list[str] = []
    signal_count = 0
    if store.exists(triage_key):
        triage = store.read_json(triage_key)
        selected_engines = [
            a["agent_type"] if isinstance(a, dict) else a for a in triage.get("selected_agents", [])
        ]
        signal_count = len(triage.get("signals", []))

    return {
        "job_id": job_id,
        "database_name": database_name,
        "selected_engines": selected_engines,
        "signal_count": signal_count,
        "triage_artifact": triage_key,
    }


def run_collect_triage_core(
    job_id: str, database_name: str, input_key: str = "", store=None
) -> dict:
    """Ingest offline collection + run triage. Returns a summary dict.

    This is the shared core used by BOTH the orchestrator tool and the subagent.
    Raises FileNotFoundError if the offline input is missing.
    """
    store = store or make_store()
    key = input_key or default_input_key(job_id, database_name)

    if not store.exists(key):
        raise FileNotFoundError(
            f"Offline collection input not found at '{key}'. "
            "Upload the collection JSON to the artifact store first."
        )

    raw_input = store.read_json(key)

    # Phase 1a: ingest collection (mirrors run_assessment.phase_collect)
    collector_data = ingest_offline_collection(store, job_id, database_name, raw_input)
    n_tables = len(collector_data.get("database_schema", {}).get("tables", []))
    n_queries = len(collector_data.get("queries", {}).get("query_patterns", []))

    # Phase 1b: triage (existing deterministic handler)
    from src.agents.referee.triage_handler import run_triage

    run_triage(job_id, database_name, store)

    triage_key = f"{database_name}/{job_id}/referee-triage/triage.json"
    selected_engines: list[str] = []
    signal_count = 0
    if store.exists(triage_key):
        triage = store.read_json(triage_key)
        selected_engines = [
            a["agent_type"] if isinstance(a, dict) else a for a in triage.get("selected_agents", [])
        ]
        signal_count = len(triage.get("signals", []))

    return {
        "job_id": job_id,
        "database_name": database_name,
        "tables": n_tables,
        "queries": n_queries,
        "selected_engines": selected_engines,
        "signal_count": signal_count,
    }


def run_analysis_dynamodb_core(
    job_id: str,
    database_name: str,
    store=None,
) -> dict:
    """Run DynamoDB analysis for a completed collector run. Returns a summary dict.

    Maps one-to-one to AGENT_TYPE='analysis-dynamodb'. Reads the collector
    output from the ArtifactStore, constructs AnalysisInput, invokes
    analyze_for_dynamodb, and writes 3 S3 artifacts:
      - analysis.json         (AnalysisOutputContract, JSON)
      - decision-trace.json   (per-query decision trace, JSON)
      - er-diagram.mmd        (Mermaid ER diagram, plain text)

    LLM mode is controlled by the LLM_MODE env var:
      - "bedrock"  (default): calls LlmAdvisor when ENABLE_LLM_ADVISOR=true
      - "none":               deterministic only, no Bedrock calls
      - "external":           deterministic only, marks output as awaiting

    Raises FileNotFoundError if the collector output is missing.
    """
    store = store or make_store()
    collector_key = f"{database_name}/{job_id}/collector/output.json"
    if not store.exists(collector_key):
        raise FileNotFoundError(
            f"Collector output not found at '{collector_key}'. " "Run the collector agent first."
        )

    collector_output = store.read_json(collector_key)

    # Deferred imports — avoid loading heavy analysis modules at core.py import time.
    from src.agents.analysis.dynamodb_analysis_agent import analyze_for_dynamodb
    from src.contracts.analysis_input import AnalysisInput, TargetDatabase

    analysis_input = AnalysisInput(
        job_id=job_id,
        collector_output=collector_output,
        target_database=TargetDatabase.dynamodb,
    )

    # Matches core-modernizer: run_analysis() passes llm_mode="none" explicitly,
    # which overrides analyze_for_dynamodb's own "bedrock" default. Hardcoded
    # rather than env-driven so container configuration cannot re-enable the LLM
    # advisor pass — measured at 2293.88s (38.2 min) on the test-24 trace. Same
    # discipline the aurora_pg / aurora_mysql siblings already apply.
    llm_mode = "none"
    logger.info(
        "ATX analysis-dynamodb starting: job_id=%s db=%s llm_mode=%s",
        job_id,
        database_name,
        llm_mode,
    )
    contract, decision_trace, mermaid_diagram = analyze_for_dynamodb(
        analysis_input, llm_mode=llm_mode
    )

    prefix = f"{database_name}/{job_id}/analysis-dynamodb"
    analysis_key = f"{prefix}/analysis.json"
    trace_key = f"{prefix}/decision-trace.json"
    mermaid_key = f"{prefix}/er-diagram.mmd"

    store.write_json(analysis_key, contract.model_dump(mode="json"))
    store.write_json(trace_key, decision_trace)
    store.write_text(mermaid_key, mermaid_diagram, content_type="text/x-mermaid")

    # Aggregate table-recommendation counts by confidence band.
    # Thresholds per shared scoring layer (src/tools/analysis/scoring.py):
    #   >=80 HIGHLY_SUITABLE, >=60 SUITABLE, >=40 MARGINAL, <40 NOT_SUITABLE
    level_counts: dict[str, int] = {
        "HIGHLY_SUITABLE": 0,
        "SUITABLE": 0,
        "MARGINAL": 0,
        "NOT_SUITABLE": 0,
    }
    for tr in contract.table_recommendations:
        score = tr.confidence_score
        if score >= 80:
            level_counts["HIGHLY_SUITABLE"] += 1
        elif score >= 60:
            level_counts["SUITABLE"] += 1
        elif score >= 40:
            level_counts["MARGINAL"] += 1
        else:
            level_counts["NOT_SUITABLE"] += 1

    llm_status = "unknown"
    if isinstance(decision_trace, dict):
        llm_status = decision_trace.get("llm_advisor", {}).get("status", "unknown")

    return {
        "job_id": job_id,
        "database_name": database_name,
        "target_database": "dynamodb",
        "tables_analyzed": len(contract.table_recommendations),
        "highly_suitable_count": level_counts["HIGHLY_SUITABLE"],
        "suitable_count": level_counts["SUITABLE"],
        "marginal_count": level_counts["MARGINAL"],
        "not_suitable_count": level_counts["NOT_SUITABLE"],
        "estimated_monthly_cost_usd": contract.cost_estimate.monthly_cost_usd,
        "llm_advisor_status": llm_status,
        "analysis_artifact": analysis_key,
        "decision_trace_artifact": trace_key,
        "er_diagram_artifact": mermaid_key,
    }


def run_analysis_documentdb_core(
    job_id: str,
    database_name: str,
    store=None,
) -> dict:
    """Run DocumentDB analysis for a completed collector run. Returns a summary dict.

    Maps one-to-one to AGENT_TYPE='analysis-documentdb'. Reads the collector
    output from the ArtifactStore, constructs AnalysisInput, invokes
    analyze_for_documentdb, and writes 3 S3 artifacts:
      - analysis.json         (AnalysisOutputContract, JSON)
      - decision-trace.json   (per-query decision trace, JSON)
      - er-diagram.mmd        (Mermaid ER diagram, plain text)

    LLM mode is controlled by the LLM_MODE env var:
      - "bedrock"  (default): calls LlmDocumentDBAdvisor when ENABLE_LLM_ADVISOR=true
      - "none":               deterministic only, no Bedrock calls
      - "external":           deterministic only, marks output as awaiting

    Raises FileNotFoundError if the collector output is missing.
    """
    store = store or make_store()
    collector_key = f"{database_name}/{job_id}/collector/output.json"
    if not store.exists(collector_key):
        raise FileNotFoundError(
            f"Collector output not found at '{collector_key}'. " "Run the collector agent first."
        )

    collector_output = store.read_json(collector_key)

    # Deferred imports — avoid loading heavy analysis modules at core.py import time.
    from src.agents.analysis.documentdb_analysis_agent import analyze_for_documentdb
    from src.contracts.analysis_input import AnalysisInput, TargetDatabase

    analysis_input = AnalysisInput(
        job_id=job_id,
        collector_output=collector_output,
        target_database=TargetDatabase.documentdb,
    )

    # Matches core-modernizer: run_analysis() passes llm_mode="none" explicitly,
    # which overrides analyze_for_documentdb's own "bedrock" default. Hardcoded
    # rather than env-driven so container configuration cannot re-enable the LLM
    # advisor pass — measured at ~63 min on the test-24 trace. Same discipline
    # the aurora_pg / aurora_mysql siblings already apply.
    llm_mode = "none"
    logger.info(
        "ATX analysis-documentdb starting: job_id=%s db=%s llm_mode=%s",
        job_id,
        database_name,
        llm_mode,
    )
    contract, decision_trace, mermaid_diagram = analyze_for_documentdb(
        analysis_input, llm_mode=llm_mode
    )

    prefix = f"{database_name}/{job_id}/analysis-documentdb"
    analysis_key = f"{prefix}/analysis.json"
    trace_key = f"{prefix}/decision-trace.json"
    mermaid_key = f"{prefix}/er-diagram.mmd"

    store.write_json(analysis_key, contract.model_dump(mode="json"))
    store.write_json(trace_key, decision_trace)
    store.write_text(mermaid_key, mermaid_diagram, content_type="text/x-mermaid")

    # Aggregate table-recommendation counts by confidence band.
    # Thresholds per shared scoring layer (src/tools/analysis/scoring.py):
    #   >=80 HIGHLY_SUITABLE, >=60 SUITABLE, >=40 MARGINAL, <40 NOT_SUITABLE
    level_counts: dict[str, int] = {
        "HIGHLY_SUITABLE": 0,
        "SUITABLE": 0,
        "MARGINAL": 0,
        "NOT_SUITABLE": 0,
    }
    for tr in contract.table_recommendations:
        score = tr.confidence_score
        if score >= 80:
            level_counts["HIGHLY_SUITABLE"] += 1
        elif score >= 60:
            level_counts["SUITABLE"] += 1
        elif score >= 40:
            level_counts["MARGINAL"] += 1
        else:
            level_counts["NOT_SUITABLE"] += 1

    llm_status = "unknown"
    if isinstance(decision_trace, dict):
        llm_status = decision_trace.get("llm_advisor", {}).get("status", "unknown")

    return {
        "job_id": job_id,
        "database_name": database_name,
        "target_database": "documentdb",
        "tables_analyzed": len(contract.table_recommendations),
        "highly_suitable_count": level_counts["HIGHLY_SUITABLE"],
        "suitable_count": level_counts["SUITABLE"],
        "marginal_count": level_counts["MARGINAL"],
        "not_suitable_count": level_counts["NOT_SUITABLE"],
        "estimated_monthly_cost_usd": contract.cost_estimate.monthly_cost_usd,
        "llm_advisor_status": llm_status,
        "analysis_artifact": analysis_key,
        "decision_trace_artifact": trace_key,
        "er_diagram_artifact": mermaid_key,
    }


def run_analysis_elasticache_core(
    job_id: str,
    database_name: str,
    store=None,
) -> dict:
    """Run ElastiCache (Redis) analysis for a completed collector run. Returns a summary dict.

    Maps one-to-one to AGENT_TYPE='analysis-elasticache'. Deterministic — no LLM
    invocation. Reads the collector output from the ArtifactStore, constructs
    AnalysisInput, invokes analyze_for_elasticache, and writes 2-3 S3 artifacts:
      - analysis.json         (AnalysisOutputContract, JSON)
      - decision-trace.json   (per-query decision trace, JSON)
      - er-diagram.mmd        (Mermaid ER diagram, optional — only if produced)

    ElastiCache/Redis analysis is purely pattern-based (caching, session store,
    leaderboards, time-series). No LLM advisor, no llm_mode parameter.
    Mermaid diagram is optional — the analyze_for_elasticache function may
    return None for it.

    Raises FileNotFoundError if the collector output is missing.
    """
    store = store or make_store()
    collector_key = f"{database_name}/{job_id}/collector/output.json"
    if not store.exists(collector_key):
        raise FileNotFoundError(
            f"Collector output not found at '{collector_key}'. " "Run the collector agent first."
        )

    collector_output = store.read_json(collector_key)

    from src.agents.analysis.elasticache_analysis_agent import analyze_for_elasticache
    from src.contracts.analysis_input import AnalysisInput, TargetDatabase

    analysis_input = AnalysisInput(
        job_id=job_id,
        collector_output=collector_output,
        target_database=TargetDatabase.elasticache,
    )

    logger.info(
        "ATX analysis-elasticache starting: job_id=%s db=%s",
        job_id,
        database_name,
    )
    contract, decision_trace, mermaid_diagram = analyze_for_elasticache(analysis_input)

    prefix = f"{database_name}/{job_id}/analysis-elasticache"
    analysis_key = f"{prefix}/analysis.json"
    trace_key = f"{prefix}/decision-trace.json"

    store.write_json(analysis_key, contract.model_dump(mode="json"))
    store.write_json(trace_key, decision_trace)

    # Mermaid diagram is optional for ElastiCache — analyze_for_elasticache may
    # return None. Only write the artifact if we actually got content.
    mermaid_key: str | None = None
    if mermaid_diagram:
        mermaid_key = f"{prefix}/er-diagram.mmd"
        store.write_text(mermaid_key, mermaid_diagram, content_type="text/x-mermaid")

    # Aggregate table-recommendation counts by confidence band.
    level_counts: dict[str, int] = {
        "HIGHLY_SUITABLE": 0,
        "SUITABLE": 0,
        "MARGINAL": 0,
        "NOT_SUITABLE": 0,
    }
    for tr in contract.table_recommendations:
        score = tr.confidence_score
        if score >= 80:
            level_counts["HIGHLY_SUITABLE"] += 1
        elif score >= 60:
            level_counts["SUITABLE"] += 1
        elif score >= 40:
            level_counts["MARGINAL"] += 1
        else:
            level_counts["NOT_SUITABLE"] += 1

    return {
        "job_id": job_id,
        "database_name": database_name,
        "target_database": "elasticache",
        "tables_analyzed": len(contract.table_recommendations),
        "highly_suitable_count": level_counts["HIGHLY_SUITABLE"],
        "suitable_count": level_counts["SUITABLE"],
        "marginal_count": level_counts["MARGINAL"],
        "not_suitable_count": level_counts["NOT_SUITABLE"],
        "estimated_monthly_cost_usd": contract.cost_estimate.monthly_cost_usd,
        "llm_advisor_status": "not_applicable",
        "analysis_artifact": analysis_key,
        "decision_trace_artifact": trace_key,
        "er_diagram_artifact": mermaid_key,
    }


def run_analysis_opensearch_core(
    job_id: str,
    database_name: str,
    store=None,
) -> dict:
    """Run OpenSearch analysis for a completed collector run. Returns a summary dict.

    Maps one-to-one to AGENT_TYPE='analysis-opensearch'. Deterministic — no LLM
    invocation. Reads the collector output from the ArtifactStore, constructs
    AnalysisInput, invokes analyze_for_opensearch, and writes 2 S3 artifacts
    (OpenSearch doesn't produce an ER diagram — indexes are independent):
      - analysis.json         (AnalysisOutputContract, JSON)
      - decision-trace.json   (per-query decision trace, JSON)

    OpenSearch analysis detects full-text, wildcard, time-series, and log
    analytics patterns. No LLM advisor, no llm_mode parameter. The function
    returns "" (empty string) for mermaid_diagram; we skip writing that
    artifact.

    Raises FileNotFoundError if the collector output is missing.
    """
    store = store or make_store()
    collector_key = f"{database_name}/{job_id}/collector/output.json"
    if not store.exists(collector_key):
        raise FileNotFoundError(
            f"Collector output not found at '{collector_key}'. " "Run the collector agent first."
        )

    collector_output = store.read_json(collector_key)

    from src.agents.analysis.opensearch_analysis_agent import analyze_for_opensearch
    from src.contracts.analysis_input import AnalysisInput, TargetDatabase

    analysis_input = AnalysisInput(
        job_id=job_id,
        collector_output=collector_output,
        target_database=TargetDatabase.opensearch,
    )

    logger.info(
        "ATX analysis-opensearch starting: job_id=%s db=%s",
        job_id,
        database_name,
    )
    contract, decision_trace, mermaid_diagram = analyze_for_opensearch(analysis_input)

    prefix = f"{database_name}/{job_id}/analysis-opensearch"
    analysis_key = f"{prefix}/analysis.json"
    trace_key = f"{prefix}/decision-trace.json"

    store.write_json(analysis_key, contract.model_dump(mode="json"))
    store.write_json(trace_key, decision_trace)

    # OpenSearch returns "" (empty string) for mermaid — no ER diagram artifact.
    # (Guard the None case too, for defensive symmetry with ElastiCache.)
    mermaid_key: str | None = None
    if mermaid_diagram:
        mermaid_key = f"{prefix}/er-diagram.mmd"
        store.write_text(mermaid_key, mermaid_diagram, content_type="text/x-mermaid")

    # Aggregate table-recommendation counts by confidence band.
    level_counts: dict[str, int] = {
        "HIGHLY_SUITABLE": 0,
        "SUITABLE": 0,
        "MARGINAL": 0,
        "NOT_SUITABLE": 0,
    }
    for tr in contract.table_recommendations:
        score = tr.confidence_score
        if score >= 80:
            level_counts["HIGHLY_SUITABLE"] += 1
        elif score >= 60:
            level_counts["SUITABLE"] += 1
        elif score >= 40:
            level_counts["MARGINAL"] += 1
        else:
            level_counts["NOT_SUITABLE"] += 1

    return {
        "job_id": job_id,
        "database_name": database_name,
        "target_database": "opensearch",
        "tables_analyzed": len(contract.table_recommendations),
        "highly_suitable_count": level_counts["HIGHLY_SUITABLE"],
        "suitable_count": level_counts["SUITABLE"],
        "marginal_count": level_counts["MARGINAL"],
        "not_suitable_count": level_counts["NOT_SUITABLE"],
        "estimated_monthly_cost_usd": contract.cost_estimate.monthly_cost_usd,
        "llm_advisor_status": "not_applicable",
        "analysis_artifact": analysis_key,
        "decision_trace_artifact": trace_key,
        "er_diagram_artifact": mermaid_key,
    }


def run_analysis_aurora_pg_core(
    job_id: str,
    database_name: str,
    store=None,
) -> dict:
    """Run Aurora PostgreSQL analysis for a completed collector run. Returns a summary dict.

    Maps one-to-one to AGENT_TYPE='analysis-aurora-pg'. Deterministic — no LLM
    invocation (Aurora agent's "bedrock" LLM mode is not yet implemented).
    Reads the collector output, invokes analyze_for_aurora_pg with LLM_MODE=none,
    and writes 2 S3 artifacts (Aurora doesn't produce an ER diagram):
      - analysis.json         (AnalysisOutputContract, JSON)
      - decision-trace.json   (per-query decision trace, JSON)

    Aurora PG analysis is the relational baseline — used when other engines
    don't fit or when the workload wants to stay relational. Only invoke this
    subagent if the source engine is PostgreSQL.

    Raises FileNotFoundError if the collector output is missing.
    """
    store = store or make_store()
    collector_key = f"{database_name}/{job_id}/collector/output.json"
    if not store.exists(collector_key):
        raise FileNotFoundError(
            f"Collector output not found at '{collector_key}'. " "Run the collector agent first."
        )

    collector_output = store.read_json(collector_key)

    from src.agents.analysis.aurora_pg_analysis_agent import analyze_for_aurora_pg
    from src.contracts.analysis_input import AnalysisInput, TargetDatabase

    analysis_input = AnalysisInput(
        job_id=job_id,
        collector_output=collector_output,
        target_database=TargetDatabase.aurora_postgresql,
    )

    logger.info(
        "ATX analysis-aurora-pg starting: job_id=%s db=%s",
        job_id,
        database_name,
    )
    contract, decision_trace, mermaid_diagram = analyze_for_aurora_pg(
        analysis_input, llm_mode="none"
    )

    prefix = f"{database_name}/{job_id}/analysis-aurora_postgresql"
    analysis_key = f"{prefix}/analysis.json"
    trace_key = f"{prefix}/decision-trace.json"

    store.write_json(analysis_key, contract.model_dump(mode="json"))
    store.write_json(trace_key, decision_trace)

    # Aurora returns "" for mermaid — no ER diagram artifact written.
    mermaid_key: str | None = None
    if mermaid_diagram:
        mermaid_key = f"{prefix}/er-diagram.mmd"
        store.write_text(mermaid_key, mermaid_diagram, content_type="text/x-mermaid")

    level_counts: dict[str, int] = {
        "HIGHLY_SUITABLE": 0,
        "SUITABLE": 0,
        "MARGINAL": 0,
        "NOT_SUITABLE": 0,
    }
    for tr in contract.table_recommendations:
        score = tr.confidence_score
        if score >= 80:
            level_counts["HIGHLY_SUITABLE"] += 1
        elif score >= 60:
            level_counts["SUITABLE"] += 1
        elif score >= 40:
            level_counts["MARGINAL"] += 1
        else:
            level_counts["NOT_SUITABLE"] += 1

    return {
        "job_id": job_id,
        "database_name": database_name,
        "target_database": "aurora_postgresql",
        "tables_analyzed": len(contract.table_recommendations),
        "highly_suitable_count": level_counts["HIGHLY_SUITABLE"],
        "suitable_count": level_counts["SUITABLE"],
        "marginal_count": level_counts["MARGINAL"],
        "not_suitable_count": level_counts["NOT_SUITABLE"],
        "estimated_monthly_cost_usd": contract.cost_estimate.monthly_cost_usd,
        "llm_advisor_status": "not_applicable",
        "analysis_artifact": analysis_key,
        "decision_trace_artifact": trace_key,
        "er_diagram_artifact": mermaid_key,
    }


def run_analysis_aurora_mysql_core(
    job_id: str,
    database_name: str,
    store=None,
) -> dict:
    """Run Aurora MySQL analysis for a completed collector run. Returns a summary dict.

    Maps one-to-one to AGENT_TYPE='analysis-aurora-mysql'. Deterministic — no LLM
    invocation. Same shape as Aurora PG; only invoke when source engine is MySQL
    or MariaDB.

    Raises FileNotFoundError if the collector output is missing.
    """
    store = store or make_store()
    collector_key = f"{database_name}/{job_id}/collector/output.json"
    if not store.exists(collector_key):
        raise FileNotFoundError(
            f"Collector output not found at '{collector_key}'. " "Run the collector agent first."
        )

    collector_output = store.read_json(collector_key)

    from src.agents.analysis.aurora_mysql_analysis_agent import analyze_for_aurora_mysql
    from src.contracts.analysis_input import AnalysisInput, TargetDatabase

    analysis_input = AnalysisInput(
        job_id=job_id,
        collector_output=collector_output,
        target_database=TargetDatabase.aurora_mysql,
    )

    logger.info(
        "ATX analysis-aurora-mysql starting: job_id=%s db=%s",
        job_id,
        database_name,
    )
    contract, decision_trace, mermaid_diagram = analyze_for_aurora_mysql(
        analysis_input, llm_mode="none"
    )

    prefix = f"{database_name}/{job_id}/analysis-aurora_mysql"
    analysis_key = f"{prefix}/analysis.json"
    trace_key = f"{prefix}/decision-trace.json"

    store.write_json(analysis_key, contract.model_dump(mode="json"))
    store.write_json(trace_key, decision_trace)

    mermaid_key: str | None = None
    if mermaid_diagram:
        mermaid_key = f"{prefix}/er-diagram.mmd"
        store.write_text(mermaid_key, mermaid_diagram, content_type="text/x-mermaid")

    level_counts: dict[str, int] = {
        "HIGHLY_SUITABLE": 0,
        "SUITABLE": 0,
        "MARGINAL": 0,
        "NOT_SUITABLE": 0,
    }
    for tr in contract.table_recommendations:
        score = tr.confidence_score
        if score >= 80:
            level_counts["HIGHLY_SUITABLE"] += 1
        elif score >= 60:
            level_counts["SUITABLE"] += 1
        elif score >= 40:
            level_counts["MARGINAL"] += 1
        else:
            level_counts["NOT_SUITABLE"] += 1

    return {
        "job_id": job_id,
        "database_name": database_name,
        "target_database": "aurora_mysql",
        "tables_analyzed": len(contract.table_recommendations),
        "highly_suitable_count": level_counts["HIGHLY_SUITABLE"],
        "suitable_count": level_counts["SUITABLE"],
        "marginal_count": level_counts["MARGINAL"],
        "not_suitable_count": level_counts["NOT_SUITABLE"],
        "estimated_monthly_cost_usd": contract.cost_estimate.monthly_cost_usd,
        "llm_advisor_status": "not_applicable",
        "analysis_artifact": analysis_key,
        "decision_trace_artifact": trace_key,
        "er_diagram_artifact": mermaid_key,
    }


def run_assignment_core(
    job_id: str,
    database_name: str,
    store=None,
) -> dict:
    """Run assignment for a completed collector + triage + analysis pipeline.

    Maps one-to-one to AGENT_TYPE='assignment'. Deterministic — no LLM invocation.
    Scores every query against selected candidate engines (from triage output)
    using each engine's analysis output, producing a per-query assignment
    mapping. Writes to S3:

      - <db>/<job>/assignment/v1/assignment.json (query -> engine mapping)

    Prerequisites: collector/output.json + referee-triage/triage.json + at least
    one analysis-<engine>/analysis.json must exist. If those are missing,
    assignment_resolver will raise.
    """
    store = store or make_store()

    # Verify collector + triage prerequisites (analysis outputs are checked by
    # the resolver itself).
    collector_key = f"{database_name}/{job_id}/collector/output.json"
    triage_key = f"{database_name}/{job_id}/referee-triage/triage.json"
    for key in (collector_key, triage_key):
        if not store.exists(key):
            raise FileNotFoundError(
                f"Prerequisite artifact missing: '{key}'. Run collector + triage first."
            )

    from src.agents.referee.assignment_handler import run_assignment_resolver

    logger.info(
        "ATX assignment starting: job_id=%s db=%s",
        job_id,
        database_name,
    )
    run_assignment_resolver(job_id, database_name, store)

    # Summarize the produced assignment
    assignment_key = f"{database_name}/{job_id}/assignment/v1/assignment.json"
    engine_counts: dict[str, int] = {}
    total_queries = 0
    if store.exists(assignment_key):
        assignment = store.read_json(assignment_key)
        for qa in assignment.get("query_assignments", []):
            e = qa.get("assigned_engine", "unknown")
            engine_counts[e] = engine_counts.get(e, 0) + 1
            total_queries += 1

    return {
        "job_id": job_id,
        "database_name": database_name,
        "assignment_version": 1,
        "total_queries": total_queries,
        "queries_per_engine": engine_counts,
        "assignment_artifact": assignment_key,
    }


def run_synthesis_core(
    job_id: str,
    database_name: str,
    assignment_version: int = 0,
    store=None,
) -> dict:
    """Run referee-synthesis for a completed analysis + assignment pipeline.

    Maps one-to-one to AGENT_TYPE='referee-synthesis'. Deterministic-first: all
    builders run without an LLM, then a single Bedrock call generates the
    executive summary. This matches core-modernizer, whose ``entrypoint.py``
    calls ``run_synthesis(...)`` without an ``llm_mode`` argument and therefore
    takes its ``"bedrock"`` default. Unlike the analysis phases — where
    core-modernizer passes ``llm_mode="none"`` via ``run_analysis`` — synthesis
    is genuinely LLM-assisted upstream, so parity means keeping it.

    Writes to S3, at a key that depends on ``assignment_version``:
      - assignment_version > 0:  <db>/<job>/synthesis/v<N>/report.json
      - assignment_version == 0: <db>/<job>/referee-synthesis/report.json

    ``assignment_version`` controls three separate behaviours, and passing it
    correctly is the single most important input to this phase:
      1. whether the assignment is read at all (at 0 it is skipped entirely);
      2. which schema-design path is loaded (versioned vs unversioned);
      3. where this report is written (above).

    Prerequisites: collector/output.json, referee-triage/triage.json, and at
    least one analysis-<engine>/analysis.json. When assignment_version > 0 the
    matching assignment/v<N>/assignment.json must also exist.
    """
    store = store or make_store()

    collector_key = f"{database_name}/{job_id}/collector/output.json"
    triage_key = f"{database_name}/{job_id}/referee-triage/triage.json"
    for key in (collector_key, triage_key):
        if not store.exists(key):
            raise FileNotFoundError(
                f"Prerequisite artifact missing: '{key}'. Run collector + triage first."
            )

    if assignment_version > 0:
        assignment_key = (
            f"{database_name}/{job_id}/assignment/v{assignment_version}/assignment.json"
        )
        if not store.exists(assignment_key):
            raise FileNotFoundError(
                f"Prerequisite artifact missing: '{assignment_key}'. "
                f"Either run the assignment agent to produce version "
                f"{assignment_version}, or pass the version that exists — "
                f"synthesis silently produces an empty report when the "
                f"assignment cannot be found."
            )

    from src.agents.referee.synthesis_handler import run_synthesis

    logger.info(
        "ATX referee-synthesis starting: job_id=%s db=%s assignment_version=%s",
        job_id,
        database_name,
        assignment_version,
    )
    run_synthesis(job_id, database_name, store, assignment_version=assignment_version)

    if assignment_version > 0:
        report_key = f"{database_name}/{job_id}/synthesis/v{assignment_version}/report.json"
    else:
        report_key = f"{database_name}/{job_id}/referee-synthesis/report.json"

    if not store.exists(report_key):
        raise FileNotFoundError(f"Synthesis completed but no report was written at '{report_key}'.")

    report = store.read_json(report_key)
    ranking = report.get("ranking") or []
    architecture = report.get("recommended_architecture") or {}
    databases = architecture.get("databases") or []
    risk = (report.get("risk_assessment") or {}).get("overall_risk_level")

    # An empty recommended_architecture has TWO distinct causes, and they warrant
    # different treatment. Measured against webapp-test-24 and v2-e2e-01 on
    # 2026-08-21.
    #
    #   a) wrong assignment_version -> the assignment is never read at all, so
    #      assignment_summary is absent too. The report is genuinely worthless:
    #      no ranking input, no workload distribution, and a rationale reading
    #      "Insufficient data to recommend a specific architecture." This is the
    #      defect that went unnoticed for a month in core-modernizer, where the
    #      deployed Step Functions does not pass ASSIGNMENT_VERSION to
    #      referee-synthesis. RAISE — publishing it would misinform.
    #
    #   b) schema-design never ran -> build_table_mappings derives mappings from
    #      schema_design output rather than from the assignment, so table_mappings
    #      and query_groups are empty and build_architecture_recommendation skips
    #      every engine via `if not tables and not schema_design_available`. The
    #      assignment IS read and everything else populates: ranking, workload
    #      split, architecture_type, risk assessment, executive summary.
    #      WARN — the report is a real answer with two documented gaps.
    #
    # (b) was originally also a raise. That was wrong, and the way it was wrong is
    # worth remembering: the exception fired AFTER _write_synthesis_report had
    # already persisted a valid 48 KB report, so it converted a partial success
    # into a total phase failure and left the orchestrator telling the customer
    # "no report was produced" while the report sat in S3 with a 1,483-character
    # executive summary. A guard that runs after the artifact is durable must not
    # raise on a state the pipeline is expected to reach.
    warnings: list[str] = []
    # Hoisted out of the guard below because the deliverable renderer also needs it:
    # when no schema design exists, the generated executive summary cannot be
    # trusted to describe it. See artifacts.render_synthesis_markdown.
    any_schema_design = any(r.get("schema_design_available") for r in ranking)
    if ranking and not databases:
        assignment_was_read = bool(report.get("assignment_summary"))
        if not assignment_was_read:
            raise ValueError(
                f"Synthesis ranked {len(ranking)} engine(s) but never read the "
                f"assignment, so recommended_architecture is empty. This is the "
                f"signature of a wrong assignment_version — it was "
                f"{assignment_version}. At version 0 the assignment is skipped "
                f"entirely and the schema design is looked up at an unversioned path "
                f"that does not exist. Pass the version the assignment agent actually "
                f"produced."
            )
        if not any_schema_design:
            warnings.append(
                f"No engine has schema-design output, so table_mappings, query_groups "
                f"and recommended_architecture.databases are empty. The assignment "
                f"(version {assignment_version}) was read correctly and every other "
                f"section is populated: engine ranking, workload distribution, "
                f"architecture type, risk assessment and executive summary. "
                f"table_mappings is derived from schema-design output, not from the "
                f"assignment, so running schema-design is what fills these three "
                f"fields. This is a known pipeline gap, not a failure."
            )

    # Register the report with the platform so it appears in the WebApp Artifacts
    # panel. The S3 copy above is the system of record and is already durable; this
    # is the download surface (constraint C2). Publishing never raises — see
    # artifacts.publish — because a phase whose real work succeeded must not fail
    # over a registration call.
    from src.atx_orchestrator import artifacts as _artifacts

    report_md = _artifacts.render_synthesis_markdown(
        report, warnings, trust_generated_summary=any_schema_design
    )
    published = _artifacts.publish(
        [
            # The human deliverable. CUSTOMER_OUTPUT is what C2 calls for — the
            # customer downloads this from the WebApp. The working reference used
            # AGENT_OUTPUT, so if CUSTOMER_OUTPUT is refused from the agent side the
            # per-item warning will name it and the JSON below still publishes.
            (
                report_md.encode("utf-8"),
                "MARKDOWN",
                f"Database Modernization Assessment: {database_name}",
                "CUSTOMER_OUTPUT",
            ),
            # The machine artifact, for downstream tooling rather than reading.
            (
                json.dumps(report, indent=2).encode("utf-8"),
                "JSON",
                f"Assessment report data ({database_name})",
                "AGENT_OUTPUT",
            ),
        ]
    )

    return {
        "job_id": job_id,
        "database_name": database_name,
        "assignment_version": assignment_version,
        "engines_ranked": len(ranking),
        "top_engine": ranking[0].get("target") if ranking else None,
        "architecture_type": architecture.get("architecture_type"),
        "recommended_databases": [d.get("service") for d in databases],
        "table_mappings": len(report.get("table_mappings") or []),
        "query_groups": len(report.get("query_groups") or []),
        "overall_risk_level": risk,
        # The report key is "summary" (LLM) with "summary_deterministic" alongside
        # it; there is no "executive_summary" key. Measured on webapp-test-24.
        "has_executive_summary": bool(report.get("summary")),
        "warnings": warnings,
        "report_artifact": report_key,
        # Platform artifact ids, keyed by the label the customer sees. Empty when
        # running outside the ATX runtime or when publishing failed; the report is
        # still at report_artifact either way.
        "published_artifacts": published,
    }


# Target engines that share a family with a relational source, keyed by the
# source engine reported in the collector's metadata. Used to tell "no schema
# redesign is required" apart from "this report does not cover that conversion".
_SAME_FAMILY: dict[str, set[str]] = {
    "postgresql": {"aurora_postgresql"},
    "mysql": {"aurora_mysql"},
}


# Each schema designer names its output after the target's own vocabulary, so
# there is no single field that means "a design exists". Values are
# (artifact field, human-readable unit).
#
# Upstream's ``schema_design_available`` normalises ``collections`` and
# ``index_designs`` into a common count but not ``key_designs``, so an
# ElastiCache design reads as absent in the synthesis report. That is
# core-modernizer's to fix and is on the list for their team; we report
# accurately on our side regardless.
_DESIGN_SHAPE: dict[str, tuple[str, str]] = {
    "dynamodb": ("table_definitions", "target tables"),
    "documentdb": ("collections", "collections"),
    "elasticache": ("key_designs", "key designs"),
    "opensearch": ("index_designs", "index designs"),
}

# Aurora targets have no designer upstream, so they have no design field either.
# Fall back to the DynamoDB name rather than guessing: a relational target that
# ever gains a designer will most plausibly emit table definitions, and the
# fallback only has to be empty-or-present, not exhaustive.
_DESIGN_SHAPE_DEFAULT: tuple[str, str] = ("table_definitions", "target tables")


def _design_shape(target_type: str) -> tuple[str, str]:
    """Return (artifact field, unit label) holding the design for one target."""
    return _DESIGN_SHAPE.get(target_type, _DESIGN_SHAPE_DEFAULT)


def _source_engine(store, job_id: str, database_name: str) -> str:
    """Read the source engine from the collector output's metadata.

    Returns "" when absent rather than raising: the classification this feeds is
    advisory, and a missing engine should not fail a phase whose real work has
    already completed.
    """
    try:
        collector = store.read_json(f"{database_name}/{job_id}/collector/output.json")
        engine = collector.get("metadata", {}).get("source_database", {}).get("engine")
        return str(engine or "").strip().lower()
    except Exception:  # noqa: BLE001
        logger.warning("Could not read source engine from collector metadata", exc_info=True)
        return ""


def run_schema_design_core(
    job_id: str,
    database_name: str,
    target_type: str,
    assignment_version: int = 1,
    store=None,
) -> dict:
    """Design the target schema for one engine. Returns a summary dict.

    Maps to AGENT_TYPE='schema-<engine>'. One call per engine, because
    core-modernizer's ``run_schema_design`` is parameterised by ``target_type``
    rather than iterating engines itself.

    Writes ``<db>/<job>/schema-<target_type>/v<N>/schema_output.json``, which is
    the exact key ``synthesis_data.py`` reads at the same version. Synthesis
    derives ``table_mappings``, ``query_groups`` and
    ``recommended_architecture.databases`` from that file, so this phase is what
    populates three fields that are otherwise empty in every report.

    ``assignment_version`` defaults to 1 and must match the version the
    assignment agent produced. At 0 the handler passes every query to every
    engine instead of the ones assigned to it, and writes to a different key than
    synthesis reads.

    This phase is LLM-driven and there is no deterministic mode: upstream's
    ``llm_mode`` only branches on ``"external"``, which writes the prepared model
    input and designs nothing. That is the opposite of the analysis phases, where
    ``llm_mode="none"`` is a full deterministic path. Model selection is left to
    ``SCHEMA_AGENT_MODEL_ID`` in the runtime environment so it is a deployment
    decision rather than an inherited library default.

    Prerequisites: collector/output.json and assignment/v<N>/assignment.json.
    """
    store = store or make_store()
    prefix = f"{database_name}/{job_id}"

    collector_key = f"{prefix}/collector/output.json"
    if not store.exists(collector_key):
        raise FileNotFoundError(f"Collector output not found at {collector_key}")

    assignment_key = f"{prefix}/assignment/v{assignment_version}/assignment.json"
    if assignment_version > 0 and not store.exists(assignment_key):
        raise FileNotFoundError(
            f"Assignment not found at {assignment_key}. Schema design filters queries "
            f"by assignment; run the assignment phase first."
        )

    logger.info(
        "ATX schema-design starting: job_id=%s db=%s target=%s assignment_version=%s",
        job_id,
        database_name,
        target_type,
        assignment_version,
    )

    from src.agents.schema_design.handler import run_schema_design

    run_schema_design(
        job_id=job_id,
        database_name=database_name,
        target_type=target_type,
        store=store,
        assignment_version=assignment_version,
    )

    output_key = f"{prefix}/schema-{target_type}/v{assignment_version}/schema_output.json"
    if not store.exists(output_key):
        raise FileNotFoundError(
            f"Schema design reported success but no output exists at {output_key}"
        )
    output = store.read_json(output_key)

    status = str(output.get("status") or "completed")
    design_field, design_units = _design_shape(target_type)
    designs = output.get(design_field) or []
    access_patterns = output.get("access_patterns") or []

    # Upstream dispatches on target_type alone and has designers for dynamodb,
    # documentdb, opensearch and elasticache; other targets take a default branch
    # that writes a placeholder. The source engine — which upstream does not
    # consult — is what distinguishes a target needing no redesign from one this
    # report simply does not cover. Reported as informational in the first case
    # and as a warning in the second, so that a warning always means the reader
    # needs to act.
    #
    # "Did a design happen" is asked of the engine's own field, not of
    # ``table_definitions``. Only DynamoDB uses that name; DocumentDB produces
    # ``collections``, ElastiCache ``key_designs``, OpenSearch ``index_designs``.
    # Testing ``table_definitions`` universally made three engines that had
    # designed 20 collections, 10 key structures and 5 index mappings look like
    # they had produced nothing, and emitted a warning telling the reader a
    # separate schema conversion assessment was needed for designs that were
    # already in the artifact. See §13 Step 19.
    notes: list[str] = []
    warnings: list[str] = []
    if not designs:
        src = _source_engine(store, job_id, database_name)
        if target_type in _SAME_FAMILY.get(src, set()):
            notes.append(
                f"{target_type}: no schema design required. Source and target are both "
                f"{src}, so the existing schema carries over unchanged."
            )
        elif status == "skipped":
            notes.append(
                f"{target_type}: {output.get('reason') or 'no queries or tables assigned'}."
            )
        else:
            warnings.append(
                f"{target_type}: schema design not included in this report."
                + (
                    f" The source engine is {src}, so type and object mappings for this "
                    f"target would need a separate schema conversion assessment."
                    if src
                    else " Type and object mappings for this target are not covered here."
                )
            )

    summary = {
        "job_id": job_id,
        "database_name": database_name,
        "target_type": target_type,
        "assignment_version": assignment_version,
        "status": status,
        # Reported under a neutral name with the engine's own unit alongside,
        # because "table_definitions: 0" for DocumentDB read as a failed design
        # when 20 collections had in fact been produced. The orchestrator relays
        # this into the chat, so the label is customer-visible.
        "designs": len(designs),
        "design_unit": design_units,
        "access_patterns": len(access_patterns),
        "unsupported_patterns": len(output.get("unsupported_patterns") or []),
        "schema_artifact": output_key,
    }
    if notes:
        summary["notes"] = notes
    if warnings:
        summary["warnings"] = warnings

    logger.info(
        "[schema-design/%s] status=%s %s=%d access_patterns=%d",
        target_type,
        status,
        design_field,
        len(designs),
        len(access_patterns),
    )
    return summary
