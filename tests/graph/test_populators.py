"""Tests for graph populators — validates nodes/edges created from artifacts."""

from src.graph.populators import (
    populate_from_analysis,
    populate_from_assignment,
    populate_from_collector,
    populate_from_reality_check,
    populate_from_triage,
)


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


def test_populate_from_assignment_creates_destinations(
    graph_store, sample_collector_output, sample_assignment
):
    """Assignment produces Destination and Engine nodes, MIGRATES_TO edges."""
    populate_from_collector(sample_collector_output, graph_store)
    populate_from_assignment(sample_assignment, graph_store)
    destinations = graph_store.query("MATCH (d:Destination) RETURN d.id ORDER BY d.id")
    assert len(destinations) >= 1
    engines = graph_store.query("MATCH (e:Engine) RETURN e.id ORDER BY e.id")
    assert len(engines) == 2  # dynamodb, documentdb
    migrates = graph_store.query(
        "MATCH (q:Query)-[:MIGRATES_TO]->(d:Destination) RETURN q.id, d.engine"
    )
    assert len(migrates) == 3


def test_populate_from_assignment_creates_co_dependency_groups(
    graph_store, sample_collector_output, sample_assignment
):
    """Assignment produces CoDependencyGroup nodes and MEMBER_OF edges."""
    populate_from_collector(sample_collector_output, graph_store)
    populate_from_assignment(sample_assignment, graph_store)
    groups = graph_store.query("MATCH (g:CoDependencyGroup) RETURN g.id")
    assert len(groups) == 1
    members = graph_store.query(
        "MATCH (q:Query)-[:MEMBER_OF]->(g:CoDependencyGroup) RETURN q.id ORDER BY q.id"
    )
    assert len(members) == 2
    assert members[0]["q.id"] == "q1"
    assert members[1]["q.id"] == "q3"


def test_populate_from_analysis_creates_anti_patterns(
    graph_store, sample_collector_output, sample_analysis_output
):
    """Analysis output produces AntiPattern nodes linked to queries."""
    populate_from_collector(sample_collector_output, graph_store)
    populate_from_analysis(sample_analysis_output, "dynamodb", graph_store)
    anti_patterns = graph_store.query("MATCH (ap:AntiPattern) RETURN ap.id, ap.anti_pattern_type")
    assert len(anti_patterns) == 1
    assert anti_patterns[0]["ap.anti_pattern_type"] == "hot-partition"
    edges = graph_store.query("MATCH (ap:AntiPattern)-[:OBSERVED_IN_QUERY]->(q:Query) RETURN q.id")
    assert edges[0]["q.id"] == "q1"


def test_populate_from_reality_check_creates_consolidation_decisions(
    graph_store, sample_reality_check_output
):
    """Reality check produces Decision nodes (consolidation category)."""
    populate_from_reality_check(sample_reality_check_output, graph_store)
    decisions = graph_store.query("MATCH (d:Decision) RETURN d.category, d.description")
    assert len(decisions) == 1
    assert decisions[0]["d.category"] == "consolidation"
