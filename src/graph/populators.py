"""Graph populators — read pipeline artifacts and write nodes/edges to the graph."""

from __future__ import annotations

import json
import time

from src.graph.schema import initialize_schema
from src.graph.store import GraphStore
from src.storage.artifact_store import ArtifactStore


def _stamp_agent(
    store: GraphStore,
    agent_id: str,
    agent_name: str,
    phase: str,
    from_label: str,
    node_ids: list[str],
) -> None:
    """MERGE the producing Agent and link each node to it via PRODUCED_BY.

    from_label is the source node label (Decision, Signal, AntiPattern,
    AccessPattern, LoadTestRun, Risk) — a module constant, never user input.
    node_ids are the ids of the just-created nodes of that label. PRODUCED_BY
    is a single multi-pair rel table.
    """
    if not node_ids:
        return
    store.execute(
        "MERGE (a:Agent {id: $id}) SET a.name = $name, a.phase = $phase",
        {"id": agent_id, "name": agent_name, "phase": phase},
    )
    store.execute(  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query
        f"UNWIND $rows AS r MATCH (n:{from_label} {{id: r.nid}}), (a:Agent {{id: $aid}}) "
        "MERGE (n)-[:PRODUCED_BY]->(a)",
        {"rows": [{"nid": nid} for nid in node_ids], "aid": agent_id},
    )


def populate_from_collector(collector_output: dict, store: GraphStore) -> None:
    """Create Query and SourceTable nodes, READS_FROM edges from collector output."""
    patterns = collector_output["queries"]["query_patterns"]

    table_rows = [{"id": t} for t in {t for p in patterns for t in p["tables_accessed"]}]
    if table_rows:
        store.execute(
            "UNWIND $rows AS r MERGE (st:SourceTable {id: r.id}) "
            "SET st.database = '', st.row_estimate = 0",
            {"rows": table_rows},
        )

    query_rows = [
        {
            "id": p["query_id"],
            "sql": p["query_text"],
            "cps": p["calls_per_second"],
            "op": p["query_type"],
        }
        for p in patterns
    ]
    if query_rows:
        store.execute(
            "UNWIND $rows AS r MERGE (q:Query {id: r.id}) "
            "SET q.sql_text = r.sql, q.calls_per_second = r.cps, "
            "q.operation_type = r.op, q.in_scope = true",
            {"rows": query_rows},
        )

    edge_rows = [{"qid": p["query_id"], "tid": t} for p in patterns for t in p["tables_accessed"]]
    if edge_rows:
        store.execute(
            "UNWIND $rows AS r MATCH (q:Query {id: r.qid}), (st:SourceTable {id: r.tid}) "
            "MERGE (q)-[:READS_FROM]->(st)",
            {"rows": edge_rows},
        )


def populate_from_triage(triage_output: dict, store: GraphStore) -> None:
    """Create Signal nodes and EMITS_SIGNAL edges from triage output."""
    signals = triage_output.get("signals", [])
    total_queries = sum(s.get("query_count", 0) for s in signals) or 1

    schema_signals = {
        "json_columns",
        "junction_tables",
        "high_fk_density",
        "self_referential_fk",
        "eav_pattern",
    }

    signal_rows = [
        {
            "id": s["signal"],
            "cat": "schema_signal" if s["signal"] in schema_signals else "query_signal",
            "description": s["evidence"],
        }
        for s in signals
    ]
    if signal_rows:
        store.execute(
            "UNWIND $rows AS r MERGE (s:Signal {id: r.id}) "
            "SET s.category = r.cat, s.description = r.description",
            {"rows": signal_rows},
        )
        _stamp_agent(
            store,
            "triage",
            "Triage",
            "TRIAGE",
            "Signal",
            [r["id"] for r in signal_rows],
        )

    edge_rows = [
        {"qid": qid, "sid": s["signal"], "strength": s.get("query_count", 1) / total_queries}
        for s in signals
        for qid in s.get("query_ids", [])
    ]
    if edge_rows:
        store.execute(
            "UNWIND $rows AS r MATCH (q:Query {id: r.qid}), (s:Signal {id: r.sid}) "
            "MERGE (q)-[:EMITS_SIGNAL {strength: r.strength}]->(s)",
            {"rows": edge_rows},
        )


def populate_from_assignment(assignment: dict, store: GraphStore) -> None:
    """Create Destination, Engine, CoDependencyGroup nodes and edges from assignment."""
    qas = assignment["query_assignments"]

    def _dest_id(qa: dict) -> str:
        tables = qa.get("source_tables", [])
        engine = qa["assigned_engine"]
        return f"{tables[0]}-{engine}" if tables else f"unknown-{engine}"

    engine_rows = [
        {"id": e, "name": f"Amazon {e.replace('_', ' ').title()}"}
        for e in {qa["assigned_engine"] for qa in qas}
    ]
    if engine_rows:
        store.execute(
            "UNWIND $rows AS r MERGE (e:Engine {id: r.id}) SET e.display_name = r.name",
            {"rows": engine_rows},
        )

    dest_rows = [{"id": _dest_id(qa), "engine": qa["assigned_engine"]} for qa in qas]
    if dest_rows:
        store.execute(
            "UNWIND $rows AS r MERGE (d:Destination {id: r.id}) "
            "SET d.engine = r.engine, d.artifact_type = 'table', d.artifact_name = r.id",
            {"rows": dest_rows},
        )
        store.execute(
            "UNWIND $rows AS r MATCH (d:Destination {id: r.id}), (e:Engine {id: r.engine}) "
            "MERGE (d)-[:HOSTED_ON]->(e)",
            {"rows": dest_rows},
        )

    migrate_rows = [
        {
            "qid": qa["query_id"],
            "did": _dest_id(qa),
            "conf": qa["confidence"],
            "reason": qa["assignment_reason"],
        }
        for qa in qas
    ]
    if migrate_rows:
        store.execute(
            "UNWIND $rows AS r MATCH (q:Query {id: r.qid}), (d:Destination {id: r.did}) "
            "MERGE (q)-[:MIGRATES_TO {confidence: r.conf, assignment_reason: r.reason}]->(d)",
            {"rows": migrate_rows},
        )

    group_rows: list[dict] = []
    member_rows: list[dict] = []
    for i, group in enumerate(assignment.get("co_dependency_groups", [])):
        # A group is either a dict ({group_id, query_ids, reason}) or a bare
        # list of query ids. Normalize both to an id, reason, and query ids.
        if isinstance(group, dict):
            gid = group.get("group_id", f"group-{i}")
            reason = group.get("reason", "")
            qids = group.get("query_ids", [])
        else:
            gid, reason, qids = f"group-{i}", "", group
        group_rows.append({"id": gid, "reason": reason})
        member_rows.extend({"qid": qid, "gid": gid} for qid in qids)

    if group_rows:
        store.execute(
            "UNWIND $rows AS r MERGE (g:CoDependencyGroup {id: r.id}) SET g.reason = r.reason",
            {"rows": group_rows},
        )
    if member_rows:
        store.execute(
            "UNWIND $rows AS r MATCH (q:Query {id: r.qid}), (g:CoDependencyGroup {id: r.gid}) "
            "MERGE (q)-[:MEMBER_OF]->(g)",
            {"rows": member_rows},
        )


def populate_from_analysis(analysis_output: dict, engine: str, store: GraphStore) -> None:
    """Create AntiPattern nodes and OBSERVED_IN_QUERY/TABLE edges from analysis output."""
    workload = analysis_output.get("workload_analysis", {})
    anti_patterns = workload.get("anti_patterns_detected") or []

    node_rows = [
        {
            "id": ap["anti_pattern_id"],
            "type": ap["anti_pattern_type"],
            "sev": ap.get("severity_weight", 0.5),
            "description": ap.get("description", ""),
            "rec": ap.get("recommendation", ""),
        }
        for ap in anti_patterns
    ]
    if node_rows:
        store.execute(
            "UNWIND $rows AS r MERGE (ap:AntiPattern {id: r.id}) "
            "SET ap.anti_pattern_type = r.type, ap.severity_weight = r.sev, "
            "ap.description = r.description, ap.recommendation = r.rec",
            {"rows": node_rows},
        )
        _stamp_agent(
            store,
            f"analysis-{engine}",
            f"Analysis {engine}",
            "ANALYSIS",
            "AntiPattern",
            [r["id"] for r in node_rows],
        )

    q_edges = [
        {"apid": ap["anti_pattern_id"], "qid": qid}
        for ap in anti_patterns
        for qid in (ap.get("query_ids") or [])
    ]
    if q_edges:
        store.execute(
            "UNWIND $rows AS r MATCH (ap:AntiPattern {id: r.apid}), (q:Query {id: r.qid}) "
            "MERGE (ap)-[:OBSERVED_IN_QUERY]->(q)",
            {"rows": q_edges},
        )

    t_edges = [
        {"apid": ap["anti_pattern_id"], "tid": tid}
        for ap in anti_patterns
        for tid in (ap.get("table_ids") or [])
    ]
    if t_edges:
        store.execute(
            "UNWIND $rows AS r MATCH (ap:AntiPattern {id: r.apid}), (st:SourceTable {id: r.tid}) "
            "MERGE (ap)-[:OBSERVED_IN_TABLE]->(st)",
            {"rows": t_edges},
        )


def populate_from_reality_check(reality_check_output: dict, store: GraphStore) -> None:
    """Create Decision nodes (consolidation) from reality check output."""
    consolidations = reality_check_output.get("consolidations", [])

    rows: list[dict] = []
    for i, cons in enumerate(consolidations):
        metadata = {
            "from_engine": cons["from_engine"],
            "to_engine": cons["to_engine"],
            "saved_cost_estimate": cons.get("saved_cost_estimate", 0),
            "action": cons.get("action", "full"),
        }
        rows.append(
            {
                "id": f"consolidation-{i}",
                "description": (
                    f"Consolidated {cons['query_count']} queries "
                    f"from {cons['from_engine']} to {cons['to_engine']}"
                ),
                "rationale": cons["reason"],
                "meta": json.dumps(metadata),
            }
        )
    if rows:
        store.execute(
            "UNWIND $rows AS r MERGE (d:Decision {id: r.id}) "
            "SET d.category = 'consolidation', d.description = r.description, "
            "d.rationale = r.rationale, d.phase = 'REALITY_CHECK', d.metadata = r.meta",
            {"rows": rows},
        )
        _stamp_agent(
            store,
            "reality-check",
            "Reality Check",
            "REALITY_CHECK",
            "Decision",
            [r["id"] for r in rows],
        )


def populate_from_schema_design(
    schema_output: dict, engine: str, schema_version: int, store: GraphStore
) -> None:
    """Create Decision (trade_off) and AccessPattern nodes from schema design output."""
    trade_offs = schema_output.get("trade_offs", [])

    decision_rows: list[dict] = []
    informed_rows: list[dict] = []
    for i, trade_off in enumerate(trade_offs):
        if not isinstance(trade_off, dict):
            continue
        did = f"trade_off-{engine}-{i}"
        decision_rows.append(
            {
                "id": did,
                "description": trade_off.get("description", ""),
                "impact": trade_off.get("impact", ""),
                "meta": json.dumps({"engine": engine}),
            }
        )
        informed_rows.extend({"did": did, "qid": qid} for qid in trade_off.get("query_ids", []))

    if decision_rows:
        store.execute(
            "UNWIND $rows AS r MERGE (d:Decision {id: r.id}) "
            "SET d.category = 'trade_off', d.description = r.description, "
            "d.rationale = r.impact, d.phase = 'SCHEMA_DESIGN', d.metadata = r.meta",
            {"rows": decision_rows},
        )
        _stamp_agent(
            store,
            f"schema-{engine}",
            f"Schema {engine}",
            "SCHEMA_DESIGN",
            "Decision",
            [r["id"] for r in decision_rows],
        )
    if informed_rows:
        store.execute(
            "UNWIND $rows AS r MATCH (d:Decision {id: r.did}), (q:Query {id: r.qid}) "
            "MERGE (d)-[:INFORMED_BY]->(q)",
            {"rows": informed_rows},
        )

    ap_rows: list[dict] = []
    part_of_rows: list[dict] = []
    for ap in schema_output.get("access_patterns", []):
        pattern_id = ap.get("pattern_id")
        if not pattern_id:
            continue
        ap_rows.append(
            {
                "id": pattern_id,
                "engine": engine,
                "ver": schema_version,
                "description": ap.get("description", ""),
                "pattern_group": ap.get("pattern_group", ""),
                "op": ap.get("operation", ""),
                "rps": float(ap.get("design_rps", 0) or 0),
                "in_scope": ap.get("in_scope", True),
            }
        )
        # DynamoDB/OpenSearch use query_ids; DocumentDB/ElastiCache use source_query_ids.
        qids = ap.get("query_ids") or ap.get("source_query_ids") or []
        part_of_rows.extend({"qid": qid, "pid": pattern_id} for qid in qids)

    if ap_rows:
        store.execute(
            "UNWIND $rows AS r MERGE (ap:AccessPattern {id: r.id}) "
            "SET ap.engine = r.engine, ap.schema_version = r.ver, "
            "ap.description = r.description, ap.pattern_group = r.pattern_group, "
            "ap.operation = r.op, ap.design_rps = r.rps, ap.in_scope = r.in_scope",
            {"rows": ap_rows},
        )
        _stamp_agent(
            store,
            f"schema-{engine}",
            f"Schema {engine}",
            "SCHEMA_DESIGN",
            "AccessPattern",
            [r["id"] for r in ap_rows],
        )
    if part_of_rows:
        store.execute(
            "UNWIND $rows AS r MATCH (q:Query {id: r.qid}), (ap:AccessPattern {id: r.pid}) "
            "MERGE (q)-[:PART_OF]->(ap)",
            {"rows": part_of_rows},
        )


def populate_from_post_schema_router(router_output: dict, store: GraphStore) -> None:
    """Create Decision nodes (reroute) from post-schema router output."""
    routings = router_output.get("routings", [])

    decision_rows: list[dict] = []
    informed_rows: list[dict] = []
    for i, routing in enumerate(routings):
        did = f"reroute-{i}"
        metadata = {
            "from_engine": routing["from_engine"],
            "to_engine": routing.get("to_engine"),
            "cascade_depth": routing.get("cascade_depth", 0),
        }
        decision_rows.append(
            {
                "id": did,
                "description": (
                    f"Rerouted {routing['query_id']} from {routing['from_engine']} "
                    f"to {routing.get('to_engine', 'application-layer')}"
                ),
                "rationale": routing["reason"],
                "meta": json.dumps(metadata),
            }
        )
        informed_rows.append({"did": did, "qid": routing["query_id"]})

    if decision_rows:
        store.execute(
            "UNWIND $rows AS r MERGE (d:Decision {id: r.id}) "
            "SET d.category = 'reroute', d.description = r.description, "
            "d.rationale = r.rationale, d.phase = 'POST_SCHEMA_ROUTER', d.metadata = r.meta",
            {"rows": decision_rows},
        )
        _stamp_agent(
            store,
            "post-schema-router",
            "Post-Schema Router",
            "POST_SCHEMA_ROUTER",
            "Decision",
            [r["id"] for r in decision_rows],
        )
    if informed_rows:
        store.execute(
            "UNWIND $rows AS r MATCH (d:Decision {id: r.did}), (q:Query {id: r.qid}) "
            "MERGE (d)-[:INFORMED_BY]->(q)",
            {"rows": informed_rows},
        )


_LATENCY_PERCENTILES = ("p50", "p90", "p95", "p99", "p999", "min", "max")


def _flatten_latency(latency: dict, prefix: str) -> dict[str, float]:
    """Expand a percentile dict into {prefix}_{p50..max} params.

    LadybugDB reformats strings that look like maps, so latency objects can't be
    stored as JSON. Flattening to DOUBLE columns keeps the values native and
    queryable. Missing percentiles default to 0.0.
    """
    latency = latency or {}
    return {f"{prefix}_{p}": float(latency.get(p, 0) or 0) for p in _LATENCY_PERCENTILES}


def populate_from_load_test(
    load_test_output: dict, engine: str, schema_version: int, store: GraphStore
) -> None:
    """Create LoadTestRun nodes and TESTED_IN/VALIDATES edges from load test output."""
    pattern_results = load_test_output.get("pattern_results", [])

    run_rows: list[dict] = []
    edge_rows: list[dict] = []
    for result in pattern_results:
        run_id = f"lt-{engine}-v{schema_version}-{result['query_id']}"
        row = {
            "id": run_id,
            "qid": result["query_id"],
            "engine": engine,
            "ver": schema_version,
            "imp": result.get("improvement_factor", 0),
            "thr": result.get("throughput_rps", 0),
            "err": result.get("error_rate_pct", 0),
            "cost": result.get("cost_per_operation_usd", 0),
        }
        row.update(_flatten_latency(result.get("source_latency_ms", {}), "source"))
        row.update(_flatten_latency(result.get("target_latency_ms", {}), "target"))
        run_rows.append(row)
        edge_rows.append({"qid": result["query_id"], "ltid": run_id})

    if run_rows:
        store.execute(
            "UNWIND $rows AS r MERGE (lt:LoadTestRun {id: r.id}) "
            "SET lt.timestamp = '', lt.query_id = r.qid, "
            "lt.engine = r.engine, lt.schema_version = r.ver, "
            "lt.source_p50 = r.source_p50, lt.source_p90 = r.source_p90, "
            "lt.source_p95 = r.source_p95, lt.source_p99 = r.source_p99, "
            "lt.source_p999 = r.source_p999, lt.source_min = r.source_min, "
            "lt.source_max = r.source_max, "
            "lt.target_p50 = r.target_p50, lt.target_p90 = r.target_p90, "
            "lt.target_p95 = r.target_p95, lt.target_p99 = r.target_p99, "
            "lt.target_p999 = r.target_p999, lt.target_min = r.target_min, "
            "lt.target_max = r.target_max, "
            "lt.improvement_factor = r.imp, lt.throughput_rps = r.thr, "
            "lt.error_rate_pct = r.err, lt.cost_per_operation_usd = r.cost",
            {"rows": run_rows},
        )
        _stamp_agent(
            store,
            f"load-test-{engine}",
            f"Load Test {engine}",
            "LOAD_TEST",
            "LoadTestRun",
            [r["id"] for r in run_rows],
        )
    if edge_rows:
        store.execute(
            "UNWIND $rows AS r MATCH (q:Query {id: r.qid}), (lt:LoadTestRun {id: r.ltid}) "
            "MERGE (q)-[:TESTED_IN]->(lt)",
            {"rows": edge_rows},
        )
        # VALIDATES: the run validates every destination the query migrates to.
        store.execute(
            "UNWIND $rows AS r MATCH (lt:LoadTestRun {id: r.ltid}), "
            "(q:Query {id: r.qid})-[:MIGRATES_TO]->(d:Destination) "
            "MERGE (lt)-[:VALIDATES]->(d)",
            {"rows": edge_rows},
        )


def populate_from_synthesis(synthesis_output: dict, store: GraphStore) -> None:
    """Create Risk nodes and IMPACTS/EVIDENCED_BY edges from synthesis output."""
    risk_assessment = synthesis_output.get("risk_assessment", {})
    risks = risk_assessment.get("risks", [])

    node_rows: list[dict] = []
    impact_rows: list[dict] = []
    for i, risk in enumerate(risks):
        risk_id = risk.get("risk_id", f"risk-{i}")
        node_rows.append(
            {
                "id": risk_id,
                "type": risk.get("risk_type", ""),
                "sev": risk.get("severity", ""),
                "description": risk.get("description", ""),
                "mitigation": risk.get("mitigation", ""),
            }
        )
        impact_rows.extend(
            {"rid": risk_id, "tid": tid} for tid in (risk.get("affected_tables") or [])
        )

    if node_rows:
        store.execute(
            "UNWIND $rows AS r MERGE (rk:Risk {id: r.id}) "
            "SET rk.risk_type = r.type, rk.severity = r.sev, "
            "rk.description = r.description, rk.mitigation = r.mitigation",
            {"rows": node_rows},
        )
        _stamp_agent(
            store,
            "referee-synthesis",
            "Referee Synthesis",
            "SYNTHESIS",
            "Risk",
            [r["id"] for r in node_rows],
        )
    if impact_rows:
        store.execute(
            "UNWIND $rows AS r MATCH (rk:Risk {id: r.rid}), (st:SourceTable {id: r.tid}) "
            "MERGE (rk)-[:IMPACTS]->(st)",
            {"rows": impact_rows},
        )


def rebuild_graph(
    db_name: str,
    job_id: str,
    artifact_store: ArtifactStore,
    graph_store: GraphStore,
) -> dict:
    """Full rebuild: clear graph, initialize schema, read all artifacts, populate in order.

    Skips any artifact that doesn't exist yet (partial pipeline runs).
    Returns stats: {"nodes_created": int, "edges_created": int, "duration_ms": int}
    """
    start = time.time()
    graph_store.clear()
    initialize_schema(graph_store)

    prefix = f"{db_name}/{job_id}"

    def _read_safe(path: str) -> dict | None:
        try:
            return artifact_store.read_json(path)
        except Exception:  # noqa: BLE001
            return None

    # 1. Collector
    collector = _read_safe(f"{prefix}/collector/output.json")
    if collector:
        populate_from_collector(collector, graph_store)

    # 2. Triage
    triage = _read_safe(f"{prefix}/referee-triage/triage.json")
    if triage:
        populate_from_triage(triage, graph_store)

    # 3. Analysis (multiple engines)
    for engine in [
        "dynamodb",
        "documentdb",
        "opensearch",
        "elasticache",
        "aurora_postgresql",
        "aurora_mysql",
    ]:
        analysis = _read_safe(f"{prefix}/analysis-{engine}/analysis.json")
        if analysis:
            populate_from_analysis(analysis, engine, graph_store)

    # 4. Assignment (latest version)
    assignment = None
    for v in range(10, 0, -1):
        assignment = _read_safe(f"{prefix}/assignment/v{v}/assignment.json")
        if assignment:
            break
    if assignment:
        populate_from_assignment(assignment, graph_store)

    # 5. Reality check
    reality = _read_safe(f"{prefix}/reality-check/output.json")
    if reality:
        populate_from_reality_check(reality, graph_store)

    # 6. Schema design (multiple engines, latest version)
    for engine in ["dynamodb", "documentdb", "opensearch", "elasticache"]:
        for v in range(10, 0, -1):
            schema = _read_safe(f"{prefix}/schema-{engine}/v{v}/schema_output.json")
            if schema:
                populate_from_schema_design(schema, engine, v, graph_store)
                break

    # 7. Post-schema router
    router = _read_safe(f"{prefix}/post-schema-router/router_output.json")
    if router:
        populate_from_post_schema_router(router, graph_store)

    # 8. Load test (multiple engines, latest version)
    for engine in ["dynamodb", "documentdb", "opensearch", "elasticache"]:
        for v in range(10, 0, -1):
            lt = _read_safe(f"{prefix}/load-test-{engine}/v{v}/results/summary.json")
            if lt:
                populate_from_load_test(lt, engine, v, graph_store)
                break

    # 9. Synthesis
    synthesis = _read_safe(f"{prefix}/referee-synthesis/report.json")
    if synthesis:
        populate_from_synthesis(synthesis, graph_store)

    duration_ms = int((time.time() - start) * 1000)
    node_count = graph_store.query("MATCH (n) RETURN COUNT(n) AS c")[0]["c"]
    edge_count = graph_store.query("MATCH ()-[r]->() RETURN COUNT(r) AS c")[0]["c"]

    return {
        "nodes_created": node_count,
        "edges_created": edge_count,
        "duration_ms": duration_ms,
    }
