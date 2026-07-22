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
