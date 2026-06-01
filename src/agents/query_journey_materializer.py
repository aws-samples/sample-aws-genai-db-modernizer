"""Query Journey Materializer — writes per-query journey JSON files progressively.

Each pipeline stage (collector, assignment, schema design, …) calls the
appropriate ``materialize_*`` function to persist the stage's contribution to
the query's journey file in the ArtifactStore.
"""

from __future__ import annotations

from src.storage.artifact_store import ArtifactStore


def _journey_path(db_name: str, job_id: str, query_id: str) -> str:
    """Return the S3/store path for a query journey file."""
    return f"{db_name}/{job_id}/query-journeys/{query_id}.json"


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

    for pattern in query_patterns:
        query_id: str = pattern["query_id"]

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
    for entry in assignment["query_assignments"]:
        query_id: str = entry["query_id"]
        path = _journey_path(db_name, job_id, query_id)

        try:
            journey = store.read_json(path)
        except Exception:  # nosec B112
            continue

        journey["assignment"] = _project_assignment(entry)
        store.write_json(path, journey)


def materialize_load_test(
    load_test_results: list[dict],
    database_name: str,
    job_id: str,
    store: ArtifactStore,
) -> None:
    """Enrich query journey files with load test results.

    Called by the load test handler after computing per-pattern results.
    """
    for result in load_test_results:
        query_id = result["query_id"]
        path = _journey_path(database_name, job_id, query_id)

        try:
            journey = store.read_json(path)
        except Exception:  # nosec B112
            continue

        journey["load_test"] = {k: v for k, v in result.items() if k != "query_id"}
        store.write_json(path, journey)


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

    for query_id in all_query_ids:
        path = _journey_path(db_name, job_id, query_id)

        try:
            journey = store.read_json(path)
        except Exception:  # nosec B112
            continue

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
