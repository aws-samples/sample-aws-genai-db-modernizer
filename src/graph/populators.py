"""Graph populators — read pipeline artifacts and write nodes/edges to the graph."""

from __future__ import annotations

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
