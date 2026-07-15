"""Graph populators — read pipeline artifacts and write nodes/edges to the graph."""

from __future__ import annotations

import json
import time

from src.graph.schema import initialize_schema
from src.graph.store import GraphStore
from src.storage.artifact_store import ArtifactStore


def populate_from_collector(collector_output: dict, store: GraphStore) -> None:
    """Create Query and SourceTable nodes, READS_FROM edges from collector output."""
    patterns = collector_output["queries"]["query_patterns"]

    # Collect all unique tables first
    all_tables: set[str] = set()
    for pattern in patterns:
        for table in pattern["tables_accessed"]:
            all_tables.add(table)

    # Create SourceTable nodes
    for table_id in all_tables:
        store.execute(
            "MERGE (st:SourceTable {id: $id}) SET st.database = '', st.row_estimate = 0",
            {"id": table_id},
        )

    # Create Query nodes and READS_FROM edges
    for pattern in patterns:
        store.execute(
            "MERGE (q:Query {id: $id}) "
            "SET q.sql_text = $sql, q.calls_per_second = $cps, "
            "q.operation_type = $op, q.in_scope = true",
            {
                "id": pattern["query_id"],
                "sql": pattern["query_text"],
                "cps": pattern["calls_per_second"],
                "op": pattern["query_type"],
            },
        )
        for table_id in pattern["tables_accessed"]:
            store.execute(
                "MATCH (q:Query {id: $qid}), (st:SourceTable {id: $tid}) "
                "MERGE (q)-[:READS_FROM]->(st)",
                {"qid": pattern["query_id"], "tid": table_id},
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

    for signal_record in signals:
        signal_id = signal_record["signal"]
        category = "schema_signal" if signal_id in schema_signals else "query_signal"

        store.execute(
            "MERGE (s:Signal {id: $id}) " "SET s.category = $cat, s.description = $description",
            {"id": signal_id, "cat": category, "description": signal_record["evidence"]},
        )

        strength = signal_record.get("query_count", 1) / total_queries
        for query_id in signal_record.get("query_ids", []):
            store.execute(
                "MATCH (q:Query {id: $qid}), (s:Signal {id: $sid}) "
                "MERGE (q)-[:EMITS_SIGNAL {strength: $str}]->(s)",
                {"qid": query_id, "sid": signal_id, "str": strength},
            )


def populate_from_assignment(assignment: dict, store: GraphStore) -> None:
    """Create Destination, Engine, CoDependencyGroup nodes and edges from assignment."""
    engines_seen: set[str] = set()
    for qa in assignment["query_assignments"]:
        engine = qa["assigned_engine"]
        if engine not in engines_seen:
            engines_seen.add(engine)
            display = engine.replace("_", " ").title()
            store.execute(
                "MERGE (e:Engine {id: $id}) SET e.display_name = $name",
                {"id": engine, "name": f"Amazon {display}"},
            )

    for qa in assignment["query_assignments"]:
        engine = qa["assigned_engine"]
        source_tables = qa.get("source_tables", [])
        dest_id = f"{source_tables[0]}-{engine}" if source_tables else f"unknown-{engine}"
        store.execute(
            "MERGE (d:Destination {id: $id}) "
            "SET d.engine = $engine, d.artifact_type = 'table', d.artifact_name = $id",
            {"id": dest_id, "engine": engine},
        )
        store.execute(
            "MATCH (d:Destination {id: $did}), (e:Engine {id: $eid}) "
            "MERGE (d)-[:HOSTED_ON]->(e)",
            {"did": dest_id, "eid": engine},
        )
        store.execute(
            "MATCH (q:Query {id: $qid}), (d:Destination {id: $did}) "
            "MERGE (q)-[:MIGRATES_TO {confidence: $conf, assignment_reason: $reason}]->(d)",
            {
                "qid": qa["query_id"],
                "did": dest_id,
                "conf": qa["confidence"],
                "reason": qa["assignment_reason"],
            },
        )

    for i, group in enumerate(assignment.get("co_dependency_groups", [])):
        # A group is either a dict ({group_id, query_ids, reason}) or a bare
        # list of query ids. Normalize both to an id, reason, and query ids.
        if isinstance(group, dict):
            group_id = group.get("group_id", f"group-{i}")
            reason = group.get("reason", "")
            query_ids = group.get("query_ids", [])
        else:
            group_id = f"group-{i}"
            reason = ""
            query_ids = group

        store.execute(
            "MERGE (g:CoDependencyGroup {id: $id}) SET g.reason = $reason",
            {"id": group_id, "reason": reason},
        )
        for query_id in query_ids:
            store.execute(
                "MATCH (q:Query {id: $qid}), (g:CoDependencyGroup {id: $gid}) "
                "MERGE (q)-[:MEMBER_OF]->(g)",
                {"qid": query_id, "gid": group_id},
            )


def populate_from_analysis(analysis_output: dict, engine: str, store: GraphStore) -> None:
    """Create AntiPattern nodes and OBSERVED_IN_QUERY/TABLE edges from analysis output."""
    workload = analysis_output.get("workload_analysis", {})
    anti_patterns = workload.get("anti_patterns_detected") or []

    for ap in anti_patterns:
        store.execute(
            "MERGE (ap:AntiPattern {id: $id}) "
            "SET ap.anti_pattern_type = $type, ap.severity_weight = $sev, "
            "ap.description = $description, ap.recommendation = $rec",
            {
                "id": ap["anti_pattern_id"],
                "type": ap["anti_pattern_type"],
                "sev": ap.get("severity_weight", 0.5),
                "description": ap.get("description", ""),
                "rec": ap.get("recommendation", ""),
            },
        )
        for query_id in ap.get("query_ids") or []:
            store.execute(
                "MATCH (ap:AntiPattern {id: $apid}), (q:Query {id: $qid}) "
                "MERGE (ap)-[:OBSERVED_IN_QUERY]->(q)",
                {"apid": ap["anti_pattern_id"], "qid": query_id},
            )
        for table_id in ap.get("table_ids") or []:
            store.execute(
                "MATCH (ap:AntiPattern {id: $apid}), (st:SourceTable {id: $tid}) "
                "MERGE (ap)-[:OBSERVED_IN_TABLE]->(st)",
                {"apid": ap["anti_pattern_id"], "tid": table_id},
            )


def populate_from_reality_check(reality_check_output: dict, store: GraphStore) -> None:
    """Create Decision nodes (consolidation) from reality check output."""
    consolidations = reality_check_output.get("consolidations", [])

    for i, cons in enumerate(consolidations):
        decision_id = f"consolidation-{i}"
        metadata = {
            "from_engine": cons["from_engine"],
            "to_engine": cons["to_engine"],
            "saved_cost_estimate": cons.get("saved_cost_estimate", 0),
            "action": cons.get("action", "full"),
        }
        description_text = (
            f"Consolidated {cons['query_count']} queries "
            f"from {cons['from_engine']} to {cons['to_engine']}"
        )
        store.execute(
            "MERGE (d:Decision {id: $id}) "
            "SET d.category = 'consolidation', "
            "d.description = $description, d.rationale = $rationale, "
            "d.phase = 'REALITY_CHECK', d.metadata = $meta",
            {
                "id": decision_id,
                "description": description_text,
                "rationale": cons["reason"],
                "meta": json.dumps(metadata),
            },
        )


def populate_from_schema_design(
    schema_output: dict, engine: str, schema_version: int, store: GraphStore
) -> None:
    """Create Decision (trade_off) and AccessPattern nodes from schema design output."""
    trade_offs = schema_output.get("trade_offs", [])

    for i, trade_off in enumerate(trade_offs):
        if not isinstance(trade_off, dict):
            continue
        decision_id = f"trade_off-{engine}-{i}"
        store.execute(
            "MERGE (d:Decision {id: $id}) "
            "SET d.category = 'trade_off', d.description = $description, "
            "d.rationale = $impact, d.phase = 'SCHEMA_DESIGN', "
            "d.metadata = $meta",
            {
                "id": decision_id,
                "description": trade_off.get("description", ""),
                "impact": trade_off.get("impact", ""),
                "meta": json.dumps({"engine": engine}),
            },
        )
        for query_id in trade_off.get("query_ids", []):
            store.execute(
                "MATCH (d:Decision {id: $did}), (q:Query {id: $qid}) "
                "MERGE (d)-[:INFORMED_BY]->(q)",
                {"did": decision_id, "qid": query_id},
            )

    for ap in schema_output.get("access_patterns", []):
        pattern_id = ap.get("pattern_id")
        if not pattern_id:
            continue
        store.execute(
            "MERGE (ap:AccessPattern {id: $id}) "
            "SET ap.engine = $engine, ap.schema_version = $ver, "
            "ap.description = $description, ap.pattern_group = $pattern_group, "
            "ap.operation = $op, ap.design_rps = $rps, ap.in_scope = $in_scope",
            {
                "id": pattern_id,
                "engine": engine,
                "ver": schema_version,
                "description": ap.get("description", ""),
                "pattern_group": ap.get("pattern_group", ""),
                "op": ap.get("operation", ""),
                "rps": float(ap.get("design_rps", 0) or 0),
                "in_scope": ap.get("in_scope", True),
            },
        )
        # DynamoDB/OpenSearch use query_ids; DocumentDB/ElastiCache use source_query_ids.
        query_ids = ap.get("query_ids") or ap.get("source_query_ids") or []
        for qid in query_ids:
            store.execute(
                "MATCH (q:Query {id: $qid}), (ap:AccessPattern {id: $pid}) "
                "MERGE (q)-[:PART_OF]->(ap)",
                {"qid": qid, "pid": pattern_id},
            )


def populate_from_post_schema_router(router_output: dict, store: GraphStore) -> None:
    """Create Decision nodes (reroute) from post-schema router output."""
    routings = router_output.get("routings", [])

    for i, routing in enumerate(routings):
        decision_id = f"reroute-{i}"
        metadata = {
            "from_engine": routing["from_engine"],
            "to_engine": routing.get("to_engine"),
            "cascade_depth": routing.get("cascade_depth", 0),
        }
        description_text = (
            f"Rerouted {routing['query_id']} from {routing['from_engine']} "
            f"to {routing.get('to_engine', 'application-layer')}"
        )
        store.execute(
            "MERGE (d:Decision {id: $id}) "
            "SET d.category = 'reroute', "
            "d.description = $description, d.rationale = $rationale, "
            "d.phase = 'POST_SCHEMA_ROUTER', d.metadata = $meta",
            {
                "id": decision_id,
                "description": description_text,
                "rationale": routing["reason"],
                "meta": json.dumps(metadata),
            },
        )
        store.execute(
            "MATCH (d:Decision {id: $did}), (q:Query {id: $qid}) " "MERGE (d)-[:INFORMED_BY]->(q)",
            {"did": decision_id, "qid": routing["query_id"]},
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

    for result in pattern_results:
        run_id = f"lt-{engine}-v{schema_version}-{result['query_id']}"
        params = {
            "id": run_id,
            "qid": result["query_id"],
            "engine": engine,
            "ver": schema_version,
            "imp": result.get("improvement_factor", 0),
            "thr": result.get("throughput_rps", 0),
            "err": result.get("error_rate_pct", 0),
            "cost": result.get("cost_per_operation_usd", 0),
        }
        params.update(_flatten_latency(result.get("source_latency_ms", {}), "source"))
        params.update(_flatten_latency(result.get("target_latency_ms", {}), "target"))
        store.execute(
            "MERGE (lt:LoadTestRun {id: $id}) "
            "SET lt.timestamp = '', lt.query_id = $qid, "
            "lt.engine = $engine, lt.schema_version = $ver, "
            "lt.source_p50 = $source_p50, lt.source_p90 = $source_p90, "
            "lt.source_p95 = $source_p95, lt.source_p99 = $source_p99, "
            "lt.source_p999 = $source_p999, lt.source_min = $source_min, "
            "lt.source_max = $source_max, "
            "lt.target_p50 = $target_p50, lt.target_p90 = $target_p90, "
            "lt.target_p95 = $target_p95, lt.target_p99 = $target_p99, "
            "lt.target_p999 = $target_p999, lt.target_min = $target_min, "
            "lt.target_max = $target_max, "
            "lt.improvement_factor = $imp, lt.throughput_rps = $thr, "
            "lt.error_rate_pct = $err, lt.cost_per_operation_usd = $cost",
            params,
        )
        store.execute(
            "MATCH (q:Query {id: $qid}), (lt:LoadTestRun {id: $ltid}) "
            "MERGE (q)-[:TESTED_IN]->(lt)",
            {"qid": result["query_id"], "ltid": run_id},
        )
        # VALIDATES: the run validates every destination the query migrates to.
        store.execute(
            "MATCH (lt:LoadTestRun {id: $ltid}), "
            "(q:Query {id: $qid})-[:MIGRATES_TO]->(d:Destination) "
            "MERGE (lt)-[:VALIDATES]->(d)",
            {"ltid": run_id, "qid": result["query_id"]},
        )


def populate_from_synthesis(synthesis_output: dict, store: GraphStore) -> None:
    """Create Risk nodes and IMPACTS/EVIDENCED_BY edges from synthesis output."""
    risk_assessment = synthesis_output.get("risk_assessment", {})
    risks = risk_assessment.get("risks", [])

    for risk in risks:
        risk_id = risk.get("risk_id", f"risk-{risks.index(risk)}")
        store.execute(
            "MERGE (r:Risk {id: $id}) "
            "SET r.risk_type = $type, r.severity = $sev, "
            "r.description = $description, r.mitigation = $mitigation",
            {
                "id": risk_id,
                "type": risk.get("risk_type", ""),
                "sev": risk.get("severity", ""),
                "description": risk.get("description", ""),
                "mitigation": risk.get("mitigation", ""),
            },
        )
        for table_id in risk.get("affected_tables") or []:
            store.execute(
                "MATCH (r:Risk {id: $rid}), (st:SourceTable {id: $tid}) "
                "MERGE (r)-[:IMPACTS]->(st)",
                {"rid": risk_id, "tid": table_id},
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
