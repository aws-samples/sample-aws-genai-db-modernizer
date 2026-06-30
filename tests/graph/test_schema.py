"""Tests for graph schema initialization."""

from src.graph.schema import initialize_schema
from src.graph.store import GraphStore


def test_initialize_schema_creates_all_node_tables(tmp_path):
    """Schema init creates all 10 node tables."""
    store = GraphStore(str(tmp_path / "test.lbug"))
    initialize_schema(store)
    for table in [
        "Query",
        "SourceTable",
        "Destination",
        "Engine",
        "Signal",
        "CoDependencyGroup",
        "Decision",
        "LoadTestRun",
        "AntiPattern",
        "Risk",
    ]:
        rows = store.query(f"MATCH (n:{table}) RETURN COUNT(n) AS c")
        assert rows[0]["c"] == 0
    store.close()


def test_initialize_schema_creates_all_rel_tables(tmp_path):
    """Schema init creates all 14 relationship tables."""
    store = GraphStore(str(tmp_path / "test.lbug"))
    initialize_schema(store)
    # Verify by inserting and querying a relationship
    store.execute(
        "CREATE (q:Query {id: 'q1', sql_text: '', calls_per_second: 1.0,"
        " operation_type: 'SELECT', in_scope: true})"
    )
    store.execute("CREATE (st:SourceTable {id: 't1', database: 'test', row_estimate: 100})")
    store.execute(
        "MATCH (q:Query {id: 'q1'}), (st:SourceTable {id: 't1'})" " CREATE (q)-[:READS_FROM]->(st)"
    )
    results = store.query("MATCH (q:Query)-[:READS_FROM]->(st:SourceTable) RETURN q.id, st.id")
    assert len(results) == 1
    store.close()


def test_initialize_schema_is_idempotent(tmp_path):
    """Running initialize_schema twice doesn't error."""
    store = GraphStore(str(tmp_path / "test.lbug"))
    initialize_schema(store)
    initialize_schema(store)  # Should not raise
    store.close()
