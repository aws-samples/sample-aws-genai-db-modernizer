"""Tests for graph schema initialization."""

from src.graph.schema import initialize_schema
from src.graph.store import GraphStore


def test_initialize_schema_creates_all_node_tables(tmp_path):
    """Schema init creates all 12 node tables."""
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
        "AccessPattern",
        "Agent",
    ]:
        rows = store.query(f"MATCH (n:{table}) RETURN COUNT(n) AS c")
        assert rows[0]["c"] == 0
    store.close()


def test_initialize_schema_creates_all_rel_tables(tmp_path):
    """Schema init creates all 16 relationship tables."""
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


def test_initialize_schema_creates_part_of_edge(tmp_path):
    """PART_OF links a Query to an AccessPattern node."""
    store = GraphStore(str(tmp_path / "test.lbug"))
    initialize_schema(store)
    store.execute(
        "CREATE (q:Query {id: 'q1', sql_text: '', calls_per_second: 1.0,"
        " operation_type: 'SELECT', in_scope: true})"
    )
    store.execute(
        "CREATE (ap:AccessPattern {id: 'DDB-AP-1', engine: 'dynamodb', schema_version: 1,"
        " description: '', pattern_group: '', operation: 'GetItem', design_rps: 10.0,"
        " in_scope: true})"
    )
    store.execute(
        "MATCH (q:Query {id: 'q1'}), (ap:AccessPattern {id: 'DDB-AP-1'})"
        " CREATE (q)-[:PART_OF]->(ap)"
    )
    results = store.query(
        "MATCH (q:Query)-[:PART_OF]->(ap:AccessPattern) RETURN q.id, ap.id, ap.engine"
    )
    assert len(results) == 1
    assert results[0]["ap.id"] == "DDB-AP-1"
    assert results[0]["ap.engine"] == "dynamodb"
    store.close()


def test_initialize_schema_creates_agent_and_produced_by(tmp_path):
    """Agent is a NODE table and PRODUCED_BY links a Decision to an Agent."""
    store = GraphStore(str(tmp_path / "test.lbug"))
    initialize_schema(store)
    store.execute(
        "CREATE (d:Decision {id: 'dec-1', category: 'trade_off', description: '',"
        " rationale: '', phase: 'SCHEMA_DESIGN', metadata: ''})"
    )
    store.execute(
        "CREATE (a:Agent {id: 'schema-dynamodb', name: 'Schema dynamodb',"
        " phase: 'SCHEMA_DESIGN'})"
    )
    store.execute(
        "MATCH (d:Decision {id: 'dec-1'}), (a:Agent {id: 'schema-dynamodb'})"
        " CREATE (d)-[:PRODUCED_BY]->(a)"
    )
    rows = store.query(
        "MATCH (d:Decision)-[:PRODUCED_BY]->(a:Agent) RETURN a.id AS aid, a.phase AS phase"
    )
    assert len(rows) == 1
    assert rows[0]["aid"] == "schema-dynamodb"
    assert rows[0]["phase"] == "SCHEMA_DESIGN"
    store.close()


def test_initialize_schema_is_idempotent(tmp_path):
    """Running initialize_schema twice doesn't error."""
    store = GraphStore(str(tmp_path / "test.lbug"))
    initialize_schema(store)
    initialize_schema(store)  # Should not raise
    store.close()
