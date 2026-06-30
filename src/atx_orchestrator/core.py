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
    """Create an ArtifactStore using env vars (same factory as the rest of the project)."""
    from src.storage import create_artifact_store

    return create_artifact_store()


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
