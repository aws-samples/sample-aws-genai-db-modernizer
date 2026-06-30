"""Graph populators — read pipeline artifacts and write nodes/edges to the graph."""

from __future__ import annotations

import json

from src.graph.store import GraphStore


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

    for group in assignment.get("co_dependency_groups", []):
        store.execute(
            "MERGE (g:CoDependencyGroup {id: $id}) SET g.reason = $reason",
            {"id": group["group_id"], "reason": group["reason"]},
        )
        for query_id in group["query_ids"]:
            store.execute(
                "MATCH (q:Query {id: $qid}), (g:CoDependencyGroup {id: $gid}) "
                "MERGE (q)-[:MEMBER_OF]->(g)",
                {"qid": query_id, "gid": group["group_id"]},
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
