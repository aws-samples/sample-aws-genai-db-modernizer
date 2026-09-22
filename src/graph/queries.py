"""Curated graph query views: pure functions over a GraphStore.

Each function runs a fixed Cypher traversal and returns a typed response model.
No FastAPI/HTTP here — handlers in src/api/routes/graph.py call these.
"""

from __future__ import annotations

from src.api.models.graph_responses import (
    AffectedQuery,
    EngineDestination,
    EngineDetailResponse,
    LatencyPercentilesModel,
    LoadTestPattern,
    LoadTestQuery,
    LoadTestResultsResponse,
    ProvenanceDecision,
    QueryProvenanceResponse,
    RiskHotspot,
    RiskHotspotsResponse,
    TableImpactResponse,
)
from src.graph.store import GraphStore

_LATENCY_PERCENTILES = ("p50", "p90", "p95", "p99", "p999", "min", "max")


def _nest_latency(row: dict, prefix: str) -> LatencyPercentilesModel:
    """Collect flattened {prefix}_{p} columns back into a percentile model."""
    return LatencyPercentilesModel(
        **{p: row.get(f"{prefix}_{p}", 0.0) or 0.0 for p in _LATENCY_PERCENTILES}
    )


def table_impact(store: GraphStore, table_id: str) -> TableImpactResponse:
    """Queries that read the table, with destinations, patterns, anti-patterns."""
    rows = store.query(
        "MATCH (st:SourceTable {id: $tid})<-[:READS_FROM]-(q:Query) "
        "OPTIONAL MATCH (q)-[:MIGRATES_TO]->(d:Destination) "
        "OPTIONAL MATCH (q)-[:PART_OF]->(ap:AccessPattern) "
        "OPTIONAL MATCH (a:AntiPattern)-[:OBSERVED_IN_QUERY]->(q) "
        "RETURN q.id AS query_id, q.calls_per_second AS cps, "
        "  COLLECT(DISTINCT d.id) AS destinations, "
        "  COLLECT(DISTINCT ap.id) AS access_patterns, "
        "  COLLECT(DISTINCT a.anti_pattern_type) AS anti_patterns "
        "ORDER BY cps DESC",
        {"tid": table_id},
    )
    affected = [
        AffectedQuery(
            query_id=r["query_id"],
            calls_per_second=r.get("cps") or 0.0,
            destinations=[x for x in (r.get("destinations") or []) if x is not None],
            access_patterns=[x for x in (r.get("access_patterns") or []) if x is not None],
            anti_patterns=[x for x in (r.get("anti_patterns") or []) if x is not None],
        )
        for r in rows
    ]
    return TableImpactResponse(table_id=table_id, affected_queries=affected)


def query_provenance(store: GraphStore, query_id: str) -> QueryProvenanceResponse:
    """Where a query migrated, the signals it emitted, and the decisions (with agent) about it."""
    head = store.query(
        "MATCH (q:Query {id: $qid}) "
        "OPTIONAL MATCH (q)-[m:MIGRATES_TO]->(d:Destination) "
        "RETURN d.id AS destination, m.confidence AS confidence, "
        "  m.assignment_reason AS reason",
        {"qid": query_id},
    )
    signals = [
        r["sig"]
        for r in store.query(
            "MATCH (q:Query {id: $qid})-[:EMITS_SIGNAL]->(s:Signal) RETURN s.id AS sig",
            {"qid": query_id},
        )
    ]
    dec_rows = store.query(
        "MATCH (dec:Decision)-[:INFORMED_BY]->(q:Query {id: $qid}) "
        "OPTIONAL MATCH (dec)-[:PRODUCED_BY]->(a:Agent) "
        "RETURN dec.id AS decision_id, dec.category AS category, "
        "  dec.description AS description, a.id AS agent_id, a.phase AS phase",
        {"qid": query_id},
    )
    decisions = [
        ProvenanceDecision(
            decision_id=r["decision_id"],
            category=r.get("category") or "",
            description=r.get("description") or "",
            agent_id=r.get("agent_id"),
            phase=r.get("phase"),
        )
        for r in dec_rows
    ]
    h = head[0] if head else {}
    return QueryProvenanceResponse(
        query_id=query_id,
        destination=h.get("destination"),
        confidence=h.get("confidence"),
        assignment_reason=h.get("reason"),
        signals=signals,
        decisions=decisions,
    )


def engine_detail(store: GraphStore, engine: str) -> EngineDetailResponse:
    """Destinations for an engine, with their source tables and access patterns."""
    rows = store.query(
        "MATCH (q:Query)-[:MIGRATES_TO]->(d:Destination {engine: $engine}) "
        "OPTIONAL MATCH (q)-[:READS_FROM]->(st:SourceTable) "
        "OPTIONAL MATCH (q)-[:PART_OF]->(ap:AccessPattern) "
        "RETURN d.id AS destination_id, "
        "  COLLECT(DISTINCT st.id) AS source_tables, "
        "  COLLECT(DISTINCT ap.id) AS access_patterns, "
        "  COUNT(DISTINCT q) AS query_count "
        "ORDER BY query_count DESC",
        {"engine": engine},
    )
    dests = [
        EngineDestination(
            destination_id=r["destination_id"],
            source_tables=[x for x in (r.get("source_tables") or []) if x is not None],
            access_patterns=[x for x in (r.get("access_patterns") or []) if x is not None],
            query_count=r.get("query_count") or 0,
        )
        for r in rows
    ]
    return EngineDetailResponse(engine=engine, destinations=dests)


def risk_hotspots(store: GraphStore) -> RiskHotspotsResponse:
    """Tables carrying risk and/or anti-patterns, weighted by query traffic."""
    rows = store.query(
        "MATCH (st:SourceTable)<-[:READS_FROM]-(q:Query) "
        "OPTIONAL MATCH (r:Risk)-[:IMPACTS]->(st) "
        "OPTIONAL MATCH (a:AntiPattern)-[:OBSERVED_IN_TABLE]->(st) "
        "WITH st, SUM(q.calls_per_second) AS total_cps, "
        "  COUNT(DISTINCT r) AS risks, COUNT(DISTINCT a) AS anti_patterns "
        "WHERE risks > 0 OR anti_patterns > 0 "
        "RETURN st.id AS table_id, total_cps, risks, anti_patterns "
        "ORDER BY total_cps DESC",
    )
    hotspots = [
        RiskHotspot(
            table_id=r["table_id"],
            total_calls_per_second=r.get("total_cps") or 0.0,
            risk_count=r.get("risks") or 0,
            anti_pattern_count=r.get("anti_patterns") or 0,
        )
        for r in rows
    ]
    return RiskHotspotsResponse(hotspots=hotspots)


def load_test_results(
    store: GraphStore,
    job_id: str,
    engine: str | None = None,
    version: int | None = None,
    prefix: str | None = None,
) -> LoadTestResultsResponse:
    """Load test results grouped by the solution-generated access-pattern id."""
    rows = store.query(
        "MATCH (ap:AccessPattern)<-[:PART_OF]-(q:Query)-[:TESTED_IN]-(lt:LoadTestRun) "
        "WHERE ($engine IS NULL OR ap.engine = $engine) "
        "  AND ($version IS NULL OR ap.schema_version = $version) "
        "  AND ($prefix IS NULL OR starts_with(ap.id, $prefix)) "
        "RETURN ap.id AS pattern_id, ap.engine AS engine, "
        "  ap.schema_version AS schema_version, ap.description AS description, "
        "  ap.pattern_group AS pattern_group, ap.design_rps AS design_rps, "
        "  COLLECT({"
        "    query_id: q.id, improvement_factor: lt.improvement_factor, "
        "    throughput_rps: lt.throughput_rps, error_rate_pct: lt.error_rate_pct, "
        "    source_p50: lt.source_p50, source_p90: lt.source_p90, "
        "    source_p95: lt.source_p95, source_p99: lt.source_p99, "
        "    source_p999: lt.source_p999, source_min: lt.source_min, "
        "    source_max: lt.source_max, "
        "    target_p50: lt.target_p50, target_p90: lt.target_p90, "
        "    target_p95: lt.target_p95, target_p99: lt.target_p99, "
        "    target_p999: lt.target_p999, target_min: lt.target_min, "
        "    target_max: lt.target_max"
        "  }) AS queries "
        "ORDER BY pattern_id",
        {"engine": engine, "version": version, "prefix": prefix},
    )

    results = [
        LoadTestPattern(
            pattern_id=row["pattern_id"],
            engine=row["engine"],
            schema_version=row["schema_version"],
            description=row["description"],
            pattern_group=row["pattern_group"],
            design_rps=row["design_rps"],
            queries=[
                LoadTestQuery(
                    query_id=q["query_id"],
                    source_latency=_nest_latency(q, "source"),
                    target_latency=_nest_latency(q, "target"),
                    improvement_factor=q.get("improvement_factor"),
                    throughput_rps=q.get("throughput_rps"),
                    error_rate_pct=q.get("error_rate_pct"),
                )
                for q in (row.get("queries") or [])
            ],
        )
        for row in rows
    ]
    return LoadTestResultsResponse(job_id=job_id, results=results)


# ---------------------------------------------------------------------------
# Query journeys — the per-query read-model the WebApp report + API serve.
#
# This replaces the ~1,654 per-query journey JSON artifacts: the same per-query
# facts now live in the graph (Query node + READS_FROM + MIGRATES_TO + PART_OF),
# so these return the SAME projected shape ``analysis_report._project_journey``
# produced, sourced from the graph instead of one artifact read per query.
# ---------------------------------------------------------------------------

import json as _json  # noqa: E402 - local alias, kept out of the module import block


def _journey_from_row(row: dict) -> dict:
    """Project one graph row into the journey read-model dict.

    Mirrors ``analysis_report._project_journey``: ``source`` (with the nested
    performance/characteristics decoded from their JSON columns), ``assignment``
    (or None), ``design`` (engine + status, or None).
    """

    def _decode(blob: object) -> dict:
        if not blob:
            return {}
        try:
            out = _json.loads(blob)  # type: ignore[arg-type]
            return out if isinstance(out, dict) else {}
        except (ValueError, TypeError):
            return {}

    source = {
        "query_text": row.get("query_text"),
        "query_type": row.get("query_type"),
        "tables_accessed": [t for t in (row.get("tables_accessed") or []) if t is not None],
        "frequency_per_hour": row.get("frequency_per_hour"),
        "calls_per_second": row.get("calls_per_second"),
        "performance": _decode(row.get("performance_json")),
        "characteristics": _decode(row.get("characteristics_json")),
    }

    engine = row.get("assigned_engine")
    assignment = (
        {
            "assigned_engine": engine,
            "confidence": row.get("confidence"),
            "in_scope": row.get("in_scope"),
        }
        if engine
        else None
    )

    # A query that was designed has a PART_OF edge to an AccessPattern carrying the
    # target engine. Presence == the design completed for that engine (the journey
    # JSON's design section was {engine, status}; status was "completed" whenever a
    # design existed for the query).
    design_engine = row.get("design_engine")
    design = {"engine": design_engine, "status": "completed"} if design_engine else None

    return {
        "query_id": row.get("query_id"),
        "source": source,
        "assignment": assignment,
        "design": design,
    }


_JOURNEY_MATCH = (
    "MATCH (q:Query) "
    "OPTIONAL MATCH (q)-[:READS_FROM]->(st:SourceTable) "
    "OPTIONAL MATCH (q)-[m:MIGRATES_TO]->(d:Destination) "
    "OPTIONAL MATCH (q)-[:PART_OF]->(ap:AccessPattern) "
    "RETURN q.id AS query_id, q.sql_text AS query_text, "
    "  q.operation_type AS query_type, q.calls_per_second AS calls_per_second, "
    "  q.frequency_per_hour AS frequency_per_hour, q.in_scope AS in_scope, "
    "  q.performance_json AS performance_json, "
    "  q.characteristics_json AS characteristics_json, "
    "  COLLECT(DISTINCT st.id) AS tables_accessed, "
    "  d.engine AS assigned_engine, m.confidence AS confidence, "
    "  COLLECT(DISTINCT ap.engine)[1] AS design_engine"
)


def query_journeys(store: GraphStore) -> list[dict]:
    """Return every query's journey read-model, sourced from the graph.

    Same shape as ``analysis_report._read_journeys`` returned from the per-query
    JSON artifacts, so the report's flow aggregate and per-query drill-down render
    identically. One graph query instead of N artifact reads.
    """
    rows = store.query(_JOURNEY_MATCH + " ORDER BY query_id")
    return [_journey_from_row(r) for r in rows]


def query_journey(store: GraphStore, query_id: str) -> dict | None:
    """Return one query's journey read-model, or None if the query is unknown."""
    rows = store.query(
        _JOURNEY_MATCH.replace("MATCH (q:Query) ", "MATCH (q:Query {id: $qid}) ") + " LIMIT 1",
        {"qid": query_id},
    )
    return _journey_from_row(rows[0]) if rows else None
