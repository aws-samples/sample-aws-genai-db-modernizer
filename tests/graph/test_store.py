"""Tests for GraphStore — embedded graph database wrapper."""

from src.graph.store import GraphStore


def test_graph_store_creates_database(tmp_path):
    """Opening a GraphStore at a path creates the database directory."""
    db_path = str(tmp_path / "test.lbug")
    store = GraphStore(db_path)
    assert store is not None
    store.close()


def test_graph_store_execute_and_query(tmp_path):
    """Can create a node table, insert a node, and query it back."""
    db_path = str(tmp_path / "test.lbug")
    store = GraphStore(db_path)
    store.execute("CREATE NODE TABLE Foo(id STRING PRIMARY KEY, val INT64)")
    store.execute("CREATE (f:Foo {id: 'a', val: 42})")
    results = store.query("MATCH (f:Foo) RETURN f.id, f.val")
    assert len(results) == 1
    assert results[0]["f.id"] == "a"
    assert results[0]["f.val"] == 42
    store.close()


def test_graph_store_is_populated_empty(tmp_path):
    """A fresh graph with no node tables reports not populated."""
    db_path = str(tmp_path / "test.lbug")
    store = GraphStore(db_path)
    assert store.is_populated() is False
    store.close()


def test_graph_store_is_populated_with_data(tmp_path):
    """A graph with nodes reports populated."""
    db_path = str(tmp_path / "test.lbug")
    store = GraphStore(db_path)
    store.execute("CREATE NODE TABLE Foo(id STRING PRIMARY KEY)")
    store.execute("CREATE (f:Foo {id: 'x'})")
    assert store.is_populated() is True
    store.close()


def test_graph_store_clear(tmp_path):
    """Clear drops all data and schema."""
    db_path = str(tmp_path / "test.lbug")
    store = GraphStore(db_path)
    store.execute("CREATE NODE TABLE Foo(id STRING PRIMARY KEY)")
    store.execute("CREATE (f:Foo {id: 'x'})")
    store.clear()
    assert store.is_populated() is False
    store.close()
