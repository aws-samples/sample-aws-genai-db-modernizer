"""Curated graph query views: pure functions over a GraphStore.

Each function runs a fixed Cypher traversal and returns a typed response model.
No FastAPI/HTTP here — handlers in src/api/routes/graph.py call these.
"""

from __future__ import annotations

from src.api.models.graph_responses import (
    AffectedQuery,
    ProvenanceDecision,
    QueryProvenanceResponse,
    TableImpactResponse,
)
from src.graph.store import GraphStore


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
