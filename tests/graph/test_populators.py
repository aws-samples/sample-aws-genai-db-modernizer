"""Tests for graph populators — validates nodes/edges created from artifacts."""

from src.graph.populators import populate_from_collector, populate_from_triage


def test_populate_from_collector_creates_queries(graph_store, sample_collector_output):
    """Collector output produces Query nodes."""
    populate_from_collector(sample_collector_output, graph_store)
    results = graph_store.query("MATCH (q:Query) RETURN q.id ORDER BY q.id")
    assert len(results) == 3
    assert results[0]["q.id"] == "q1"
    assert results[1]["q.id"] == "q2"
    assert results[2]["q.id"] == "q3"


def test_populate_from_collector_creates_source_tables(graph_store, sample_collector_output):
    """Collector output produces SourceTable nodes (deduplicated)."""
    populate_from_collector(sample_collector_output, graph_store)
    results = graph_store.query("MATCH (st:SourceTable) RETURN st.id ORDER BY st.id")
    assert len(results) == 2
    assert results[0]["st.id"] == "customers"
    assert results[1]["st.id"] == "orders"


def test_populate_from_collector_creates_reads_from_edges(graph_store, sample_collector_output):
    """Collector output produces READS_FROM edges."""
    populate_from_collector(sample_collector_output, graph_store)
    results = graph_store.query(
        "MATCH (q:Query)-[:READS_FROM]->(st:SourceTable) " "RETURN q.id, st.id ORDER BY q.id, st.id"
    )
    # q1 -> orders, q2 -> customers, q2 -> orders, q3 -> orders
    assert len(results) == 4


def test_populate_from_triage_creates_signals(
    graph_store, sample_collector_output, sample_triage_output
):
    """Triage output produces Signal nodes."""
    populate_from_collector(sample_collector_output, graph_store)
    populate_from_triage(sample_triage_output, graph_store)
    results = graph_store.query("MATCH (s:Signal) RETURN s.id ORDER BY s.id")
    assert len(results) == 2
    assert results[0]["s.id"] == "complex_joins"
    assert results[1]["s.id"] == "key_value_lookups"


def test_populate_from_triage_creates_emits_signal_edges(
    graph_store, sample_collector_output, sample_triage_output
):
    """Triage output produces EMITS_SIGNAL edges linking queries to signals."""
    populate_from_collector(sample_collector_output, graph_store)
    populate_from_triage(sample_triage_output, graph_store)
    results = graph_store.query(
        "MATCH (q:Query)-[:EMITS_SIGNAL]->(s:Signal) " "RETURN q.id, s.id ORDER BY q.id"
    )
    assert len(results) == 2
    assert results[0]["q.id"] == "q1"
    assert results[0]["s.id"] == "key_value_lookups"
    assert results[1]["q.id"] == "q2"
    assert results[1]["s.id"] == "complex_joins"
