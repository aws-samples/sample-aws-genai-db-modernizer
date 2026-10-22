"""Curated graph query views: pure functions over a GraphStore.

Each function runs a fixed Cypher traversal and returns a typed response model.
No FastAPI/HTTP here — handlers in src/api/routes/graph.py call these.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

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
    QueryTraceResponse,
    RiskHotspot,
    RiskHotspotsResponse,
    TableImpactResponse,
)
from src.contracts.move_ledger import EngineScore, Move, MoveEvidence
from src.graph.store import GraphStore
from src.shared.query_trace import build_query_trace

_LATENCY_PERCENTILES = ("p50", "p90", "p95", "p99", "p999", "min", "max")


def _nest_latency(row: dict, prefix: str) -> LatencyPercentilesModel:
    """Collect flattened {prefix}_{p} columns back into a percentile model."""
    return LatencyPercentilesModel(
        **{p: row.get(f"{prefix}_{p}", 0.0) or 0.0 for p in _LATENCY_PERCENTILES}
    )


def _sorted_ids(values: list | None) -> list:
    """Drop nulls and sort: COLLECT(DISTINCT ...) has no defined order (#669)."""
    return sorted(x for x in (values or []) if x is not None)


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
        "ORDER BY cps DESC, query_id",
        {"tid": table_id},
    )
    affected = [
        AffectedQuery(
            query_id=r["query_id"],
            calls_per_second=r.get("cps") or 0.0,
            destinations=_sorted_ids(r.get("destinations")),
            access_patterns=_sorted_ids(r.get("access_patterns")),
            anti_patterns=_sorted_ids(r.get("anti_patterns")),
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
            "MATCH (q:Query {id: $qid})-[:EMITS_SIGNAL]->(s:Signal) RETURN s.id AS sig ORDER BY sig",
            {"qid": query_id},
        )
    ]
    dec_rows = store.query(
        "MATCH (dec:Decision)-[:INFORMED_BY]->(q:Query {id: $qid}) "
        "OPTIONAL MATCH (dec)-[:PRODUCED_BY]->(a:Agent) "
        "RETURN dec.id AS decision_id, dec.category AS category, "
        "  dec.description AS description, a.id AS agent_id, a.phase AS phase "
        "ORDER BY decision_id, agent_id",
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
        "ORDER BY query_count DESC, destination_id",
        {"engine": engine},
    )
    dests = [
        EngineDestination(
            destination_id=r["destination_id"],
            source_tables=_sorted_ids(r.get("source_tables")),
            access_patterns=_sorted_ids(r.get("access_patterns")),
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
        "ORDER BY total_cps DESC, table_id",
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
    # Build the filter predicates only for the values actually supplied, rather than
    # the ``($param IS NULL OR col = $param)`` guard. Under the newer LadybugDB binder
    # a parameter that appears in ``$param IS NULL`` is type-inferred as BOOL, which
    # then clashes with the real comparison ("Cannot compare types INT64 and BOOL" for
    # the INT64 schema_version, "change parameter expression data type from BOOL to
    # STRING" for the STRING engine/prefix). Only binding a param in its true-typed
    # comparison removes that ambiguity, and an omitted filter contributes no parameter
    # at all.
    # A run only belongs to the access pattern of its own engine and schema version;
    # without this a query designed on several engines would attach every engine's runs
    # to each engine's pattern.
    conditions: list[str] = [
        "lt.engine = ap.engine",
        "lt.schema_version = ap.schema_version",
    ]
    params: dict[str, Any] = {}
    if engine is not None:
        conditions.append("ap.engine = $engine")
        params["engine"] = engine
    if version is not None:
        conditions.append("ap.schema_version = $version")
        params["version"] = version
    if prefix is not None:
        conditions.append("starts_with(ap.id, $prefix)")
        params["prefix"] = prefix
    where_clause = "WHERE " + " AND ".join(conditions) + " "

    rows = store.query(
        "MATCH (ap:AccessPattern)<-[:PART_OF]-(q:Query)-[:TESTED_IN]->(lt:LoadTestRun) "
        f"{where_clause}"
        "RETURN ap.id AS pattern_id, ap.engine AS engine, "
        "  ap.schema_version AS schema_version, ap.description AS description, "
        "  ap.pattern_group AS pattern_group, ap.design_rps AS design_rps, "
        "  COLLECT({"
        "    query_id: q.id, run_id: lt.id, improvement_factor: lt.improvement_factor, "
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
        "ORDER BY pattern_id, schema_version, engine",
        params,
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
                for q in sorted(
                    row.get("queries") or [], key=lambda x: (x["query_id"], x["run_id"])
                )
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
# so these return one projected journey shape, sourced from the graph instead of
# one artifact read per query.
# ---------------------------------------------------------------------------

import json as _json  # noqa: E402 - local alias, kept out of the module import block


def _journey_from_row(row: dict) -> dict:
    """Project one graph row into the journey read-model dict.

    The journey shape: ``source`` (with the nested
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
        # COLLECT(DISTINCT ...) has no defined order; sort by table name (#664).
        # This is display order only: do not read tables_accessed[0] as the primary
        # table (the collector's order drives primary-table grouping elsewhere).
        "tables_accessed": sorted(t for t in (row.get("tables_accessed") or []) if t is not None),
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
    # A query can be designed on several engines. Report the engine that owns the
    # query in the assignment when it has a design there; otherwise the first design
    # engine by name, so the result never depends on insertion order (#669).
    design_engines = sorted(e for e in (row.get("design_engines") or []) if e)
    design_engine = engine if engine in design_engines else (design_engines or [None])[0]
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
    "  COLLECT(DISTINCT ap.engine) AS design_engines"
)


# Queries read per statement when listing every journey (query_journeys).
JOURNEY_BATCH_SIZE = 200


def query_count(store: GraphStore) -> int:
    """The number of Query nodes: the journeys the list pages over."""
    rows = store.query("MATCH (q:Query) RETURN count(q) AS c")
    return int(rows[0]["c"]) if rows else 0


def query_journeys_page(store: GraphStore, skip: int, limit: int) -> list[dict]:
    """The journeys of ``limit`` queries in id order, after the first ``skip``.

    The page is cut in the graph query itself, before the optional matches and
    the aggregation, so the engine's work and memory are bounded by the page,
    not by the job's size (#998). Rows come in the same order as the full
    list: by query id, then assigned engine.
    """
    skip, limit = int(skip), int(limit)
    if skip < 0 or limit < 1:
        raise ValueError(f"invalid journey page: skip={skip}, limit={limit}")
    # SKIP/LIMIT are integer literals, not parameters: the engine binds a
    # SKIP/LIMIT parameter once per connection, so a reused connection (the
    # read-write handle) would return the first page for every later one.
    page_clause = f"MATCH (q:Query) WITH q ORDER BY q.id SKIP {skip} LIMIT {limit} "
    rows = store.query(
        _JOURNEY_MATCH.replace("MATCH (q:Query) ", page_clause)
        + " ORDER BY query_id, assigned_engine"
    )
    return [_journey_from_row(r) for r in rows]


def iter_query_journeys(store: GraphStore, batch_size: int | None = None) -> Iterator[dict]:
    """Every query's journey, read from the graph ``batch_size`` queries at a time."""
    size = batch_size or JOURNEY_BATCH_SIZE
    skip = 0
    while True:
        page = query_journeys_page(store, skip, size)
        yield from page
        # A short batch is the last one (a query can span several rows).
        if len({j["query_id"] for j in page}) < size:
            return
        skip += size


def query_journeys(store: GraphStore) -> list[dict]:
    """Return every query's journey read-model, sourced from the graph.

    The same shape the per-query JSON artifacts held, so the results page's
    per-query drill-down reads either. Read in batches (``iter_query_journeys``)
    so no single statement aggregates the whole job.
    """
    return list(iter_query_journeys(store))


def query_journey(store: GraphStore, query_id: str) -> dict | None:
    """Return one query's journey read-model, or None if the query is unknown.

    The single-query journey also carries ``trace``: the query's decision
    trace (:func:`query_trace`, #339). The all-queries list
    (:func:`query_journeys`) does not.
    """
    rows = store.query(
        _JOURNEY_MATCH.replace("MATCH (q:Query) ", "MATCH (q:Query {id: $qid}) ")
        + " ORDER BY assigned_engine LIMIT 1",
        {"qid": query_id},
    )
    if not rows:
        return None
    journey = _journey_from_row(rows[0])
    trace = query_trace(store, query_id)
    journey["trace"] = trace.model_dump(mode="json", by_alias=True) if trace else None
    return journey


# ---------------------------------------------------------------------------
# Query decision trace (#339): why engine X and not Y, from the MOVED edges
# (one per Move of the move ledger, every version of the effective lineage).
# ---------------------------------------------------------------------------


def _move_from_edge(row: dict) -> tuple[int, Move]:
    """Rebuild one ledger Move (and its assignment version) from a MOVED edge row."""
    try:
        evidence = MoveEvidence.model_validate_json(row.get("evidence_json") or "{}")
    except ValueError:
        evidence = MoveEvidence()
    try:
        tied = _json.loads(row.get("tied_engines_json") or "[]")
    except ValueError:
        tied = []
    runner_engine = row.get("runner_up_engine") or None
    move = Move(
        seq=row["seq"],
        query_id=row["query_id"],
        pass_=row["pass"],
        from_engine=row.get("from_engine") or None,
        to_engine=row["to_engine"],
        score_before=row.get("score_before"),
        score_after=row.get("score_after"),
        runner_up=(
            EngineScore(engine=runner_engine, score=row.get("runner_up_score"))
            if runner_engine
            else None
        ),
        reason_code=row["reason_code"],
        evidence=evidence,
        group_key=row.get("group_key") or None,
        tied_engines=tied if isinstance(tied, list) else [],
        actor=row["actor"],
    )
    return int(row["version"]), move


def _plan_engines(store: GraphStore) -> set[str]:
    """The engines the effective assignment uses (its destinations)."""
    return {
        r["engine"]
        for r in store.query("MATCH (d:Destination) RETURN DISTINCT d.engine AS engine")
        if r.get("engine")
    }


def query_trace(store: GraphStore, query_id: str) -> QueryTraceResponse | None:
    """The query's decision trace (#339), or None when the query is unknown.

    The first assignment (with every engine's score or gate), every later
    Move across the lineage's assignment versions with its reason, the final
    engine, the runner-up (an engine of the final plan) with its score gap or
    disqualifying gate, and the engines that scored higher with the step that
    decided against each. A graph with no MOVED table (saved by an earlier
    release) or a job written before the move ledger has no steps:
    ``has_ledger`` is False and only the final engine is set.
    """
    head = store.query(
        "MATCH (q:Query {id: $qid}) "
        "OPTIONAL MATCH (q)-[:MIGRATES_TO]->(d:Destination) "
        "RETURN q.id AS query_id, d.engine AS engine ORDER BY engine LIMIT 1",
        {"qid": query_id},
    )
    if not head:
        return None
    meta = store.graph_meta()
    effective = meta["effective_version"] or None
    rows = (
        store.query(
            "MATCH (q:Query {id: $qid})-[m:MOVED]->(e:Engine) "
            "RETURN q.id AS query_id, e.id AS to_engine, m.version AS version, m.seq AS seq, "
            "  m.pass AS pass, m.reason_code AS reason_code, m.actor AS actor, "
            "  m.from_engine AS from_engine, m.score_before AS score_before, "
            "  m.score_after AS score_after, m.runner_up_engine AS runner_up_engine, "
            "  m.runner_up_score AS runner_up_score, m.group_key AS group_key, "
            "  m.tied_engines_json AS tied_engines_json, m.evidence_json AS evidence_json "
            "ORDER BY version, seq",
            {"qid": query_id},
        )
        if store.has_table("MOVED")
        else []
    )
    moves = [_move_from_edge(r) for r in rows]
    assigned = head[0].get("engine")
    plan = _plan_engines(store) if moves else set()
    return build_query_trace(
        query_id, moves, assigned, plan, effective, _key_candidates(store, query_id, meta)
    )


def _key_candidates(store: GraphStore, query_id: str, meta: dict) -> list:
    """The query's ``key_candidates_json`` (#1361); none on a graph of an older schema
    revision (no such column) or an unreadable value."""
    if int(meta.get("schema_revision") or 0) < 2:
        return []
    rows = store.query(
        "MATCH (q:Query {id: $qid}) RETURN q.key_candidates_json AS keys", {"qid": query_id}
    )
    try:
        keys = _json.loads((rows[0].get("keys") if rows else None) or "[]")
    except ValueError:
        return []
    return keys if isinstance(keys, list) else []
