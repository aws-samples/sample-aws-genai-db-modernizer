"""Query Journey Materializer — writes per-query journey JSON files progressively.

Each pipeline stage (collector, assignment, schema design, …) calls the
appropriate ``materialize_*`` function to persist the stage's contribution to
the query's journey file in the ArtifactStore.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor

from src.storage.artifact_store import ArtifactStore

logger = logging.getLogger(__name__)

# Journey materialization is a per-query read-modify-write against the
# ArtifactStore. Under the ATX backend each read_json/write_json is a network
# round trip (create download/upload URL + S3 transfer), so a workload with
# hundreds of queries (the reference discourse run has 1,654) is minutes of pure
# serial latency. The reads/writes are independent per query, so fan them out —
# mirrors the ThreadPoolExecutor(32) reader in
# src/atx_orchestrator/runtime/analysis_report.py (same rationale documented
# there). Local/S3 backends are unaffected; the pool just runs cheap calls.
_MATERIALIZE_WORKERS = 32


def _journey_path(db_name: str, job_id: str, query_id: str) -> str:
    """Return the S3/store path for a query journey file."""
    return f"{db_name}/{job_id}/query-journeys/{query_id}.json"


def _materialize_parallel(
    store: ArtifactStore,
    db_name: str,
    job_id: str,
    query_ids: Iterable[str],
    update_one: Callable[[str], None],
) -> None:
    """Run ``update_one(query_id)`` for every id across a thread pool.

    ``update_one`` performs the per-query read-modify-write against ``store``.
    Each query's journey is an independent artifact, so the work parallelizes
    cleanly. The pool size is capped at the number of ids so small workloads
    don't spin up idle threads.

    The store's path->id index (ATX backend) is warmed once here, before the
    pool starts, so worker threads all hit the already-built cache instead of
    racing to rebuild it. ``exists`` on the ATX store is an index-only lookup,
    which triggers that one-time build; on other backends it is a cheap no-op.
    """
    ids = list(query_ids)
    if not ids:
        return

    # Warm the store index once on this thread (best-effort; a backend without an
    # index simply returns quickly). Prevents N threads each doing a full listing.
    try:
        store.exists(_journey_path(db_name, job_id, ids[0]))
    except Exception:  # noqa: BLE001 - warming is an optimization, not correctness
        logger.debug("journey index warm-up skipped", exc_info=True)

    workers = min(_MATERIALIZE_WORKERS, len(ids))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        # list() forces evaluation so exceptions in a worker surface here rather
        # than being silently dropped by a lazy map.
        list(pool.map(update_one, ids))


def materialize_source(
    collector_output: dict,
    db_name: str,
    job_id: str,
    store: ArtifactStore,
) -> None:
    """Write one journey file per query in *collector_output*.

    Reads ``collector_output["queries"]["query_patterns"]`` and creates (or
    overwrites) a journey file for each query at::

        {db_name}/{job_id}/query-journeys/{query_id}.json

    The file is initialised with the ``source`` section populated from the
    collector pattern data; all downstream sections (``assignment``,
    ``design``, ``load_test``, ``sdk_code``) are set to ``null``.

    Args:
        collector_output: Deserialised CollectorOutputContract dict.
        db_name: Database name used as the top-level path segment.
        job_id: Unique job identifier used as the second path segment.
        store: ArtifactStore instance for persistence.
    """
    query_patterns: list[dict] = collector_output["queries"]["query_patterns"]
    by_query: dict[str, dict] = {p["query_id"]: p for p in query_patterns}

    def _write(query_id: str) -> None:
        pattern = by_query[query_id]
        journey = {
            "query_id": query_id,
            "source": {
                "query_text": pattern["query_text"],
                "query_type": pattern["query_type"],
                "tables_accessed": pattern["tables_accessed"],
                "frequency_per_hour": pattern["frequency_per_hour"],
                "calls_per_second": pattern["calls_per_second"],
                "performance": {
                    "execution_time_ms_avg": pattern["execution_time_ms_avg"],
                    "execution_time_ms_p50": pattern["execution_time_ms_p50"],
                    "execution_time_ms_p95": pattern["execution_time_ms_p95"],
                    "execution_time_ms_p99": pattern["execution_time_ms_p99"],
                    "rows_returned_avg": pattern["rows_returned_avg"],
                    "rows_examined_avg": pattern["rows_examined_avg"],
                    "scan_efficiency_pct": pattern["scan_efficiency_pct"],
                    "full_table_scans": pattern["full_table_scans"],
                    "db_load_contribution_percent": pattern["db_load_contribution_percent"],
                    "lock_time_ms": pattern["lock_time_ms"],
                    "total_time_ms": pattern["total_time_ms"],
                },
                "characteristics": {
                    "has_joins": pattern["has_joins"],
                    "join_count": pattern["join_count"],
                    "has_aggregations": pattern["has_aggregations"],
                    "has_subqueries": pattern["has_subqueries"],
                    "has_text_search": pattern["has_text_search"],
                    "has_time_range_filter": pattern["has_time_range_filter"],
                    "filter_columns": pattern["filter_columns"],
                    "sort_columns": pattern["sort_columns"],
                },
            },
            "assignment": None,
            "design": None,
            "load_test": None,
            "sdk_code": None,
        }
        store.write_json(_journey_path(db_name, job_id, query_id), journey)

    # Each journey is an independent write; fan them out so a workload with
    # hundreds/thousands of queries isn't minutes of serial ATX upload latency
    # (the reference discourse run writes 1,654 journeys here). Mirrors the three
    # downstream materializers below. _write is create-only (no prior read), so
    # the index warm-up in _materialize_parallel is a cheap no-op here.
    _materialize_parallel(store, db_name, job_id, by_query.keys(), _write)


def _project_assignment(entry: dict) -> dict:
    """Extract the assignment section fields from a query assignment entry.

    Drops ``query_id`` and returns only the fields that belong in the
    journey's ``assignment`` section.
    """
    return {
        "assigned_engine": entry["assigned_engine"],
        "confidence": entry["confidence"],
        "assignment_reason": entry["assignment_reason"],
        "in_scope": entry["in_scope"],
        "customer_override": entry["customer_override"],
        "warnings": entry["warnings"],
    }


def materialize_assignment(
    assignment: dict,
    db_name: str,
    job_id: str,
    store: ArtifactStore,
) -> None:
    """Update the ``assignment`` section of each query's journey file.

    Reads ``assignment["query_assignments"]`` and, for each entry, reads the
    existing journey file, updates its ``assignment`` section, and writes it
    back.  If the journey file does not exist (i.e. ``read_json`` raises),
    the query is silently skipped.

    Args:
        assignment: Deserialised AssignmentOutputContract dict.
        db_name: Database name used as the top-level path segment.
        job_id: Unique job identifier used as the second path segment.
        store: ArtifactStore instance for persistence.
    """
    by_query: dict[str, dict] = {e["query_id"]: e for e in assignment["query_assignments"]}

    def _update(query_id: str) -> None:
        path = _journey_path(db_name, job_id, query_id)
        try:
            journey = store.read_json(path)
        except Exception:  # nosec B112 - a missing journey is skipped, not fatal
            return
        journey["assignment"] = _project_assignment(by_query[query_id])
        store.write_json(path, journey)

    _materialize_parallel(store, db_name, job_id, by_query.keys(), _update)


def materialize_load_test(
    load_test_results: list[dict],
    database_name: str,
    job_id: str,
    store: ArtifactStore,
) -> None:
    """Enrich query journey files with load test results.

    Called by the load test handler after computing per-pattern results.
    """
    by_query: dict[str, dict] = {r["query_id"]: r for r in load_test_results}

    def _update(query_id: str) -> None:
        path = _journey_path(database_name, job_id, query_id)
        try:
            journey = store.read_json(path)
        except Exception:  # nosec B112 - a missing journey is skipped, not fatal
            return
        result = by_query[query_id]
        journey["load_test"] = {k: v for k, v in result.items() if k != "query_id"}
        store.write_json(path, journey)

    _materialize_parallel(store, database_name, job_id, by_query.keys(), _update)


# ---------------------------------------------------------------------------
# Design stage helpers
# ---------------------------------------------------------------------------


def _get_query_ids(pattern: dict) -> list[str]:
    """Return the query ID list from a pattern, checking both field name variants.

    DynamoDB/OpenSearch use ``query_ids``; DocumentDB/ElastiCache use
    ``source_query_ids``.  Returns an empty list if neither is present.
    """
    return pattern.get("query_ids") or pattern.get("source_query_ids") or []


def _filter_trade_offs(trade_offs: list, query_id: str) -> list[dict]:
    """Return trade-offs that reference *query_id*, projected without query_ids key.

    Plain-string trade-offs (ElastiCache style) are skipped entirely.
    """
    result: list[dict] = []
    for trade_off in trade_offs:
        if not isinstance(trade_off, dict):
            continue
        if query_id not in trade_off.get("query_ids", []):
            continue
        projected = {k: v for k, v in trade_off.items() if k != "query_ids"}
        result.append(projected)
    return result


def _project_unsupported(pattern: dict) -> dict:
    """Extract ``reason`` and ``recommendation`` from an unsupported pattern."""
    reason = pattern.get("reason") or pattern.get("pattern_type")
    recommendation = pattern.get("recommendation") or pattern.get("workaround", "")
    return {"reason": reason, "recommendation": recommendation}


def _project_access_pattern(pattern: dict) -> dict:
    """Return pattern fields with query ID fields stripped out."""
    return {k: v for k, v in pattern.items() if k not in ("query_ids", "source_query_ids")}


def materialize_design(
    schema_output: dict,
    engine: str,
    schema_version: int,
    db_name: str,
    job_id: str,
    store: ArtifactStore,
) -> None:
    """Update the ``design`` section of each query's journey file from schema output.

    Scans ``schema_output["access_patterns"]`` and
    ``schema_output["unsupported_patterns"]`` to build per-query design data,
    then reads, updates, and writes each journey file.  Queries whose journey
    file does not exist are silently skipped.

    Args:
        schema_output: Deserialised schema design output contract dict.
        engine: Target engine name (e.g. ``"dynamodb"``).
        schema_version: Schema iteration number (1-based).
        db_name: Database name used as the top-level path segment.
        job_id: Unique job identifier used as the second path segment.
        store: ArtifactStore instance for persistence.
    """
    trade_offs: list = schema_output.get("trade_offs", [])

    # Build query_id → access_pattern map
    designed_map: dict[str, dict] = {}
    for pattern in schema_output.get("access_patterns", []):
        for qid in _get_query_ids(pattern):
            designed_map[qid] = pattern

    # Build query_id → unsupported_pattern map
    unsupported_map: dict[str, dict] = {}
    for pattern in schema_output.get("unsupported_patterns", []):
        for qid in _get_query_ids(pattern):
            unsupported_map[qid] = pattern

    all_query_ids = set(designed_map) | set(unsupported_map)

    def _update(query_id: str) -> None:
        path = _journey_path(db_name, job_id, query_id)
        try:
            journey = store.read_json(path)
        except Exception:  # nosec B112 - a missing journey is skipped, not fatal
            return

        if query_id in designed_map:
            journey["design"] = {
                "engine": engine,
                "schema_version": schema_version,
                "status": "designed",
                "access_pattern": _project_access_pattern(designed_map[query_id]),
                "unsupported": None,
                "trade_offs": _filter_trade_offs(trade_offs, query_id),
            }
        else:
            journey["design"] = {
                "engine": engine,
                "schema_version": schema_version,
                "status": "unsupported",
                "access_pattern": None,
                "unsupported": _project_unsupported(unsupported_map[query_id]),
                "trade_offs": [],
            }

        store.write_json(path, journey)

    _materialize_parallel(store, db_name, job_id, all_query_ids, _update)
