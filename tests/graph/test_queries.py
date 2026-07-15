"""Reference cookbook — real use-case Cypher queries against a populated graph.

Each test demonstrates a question you can answer with the graph.
Read the test names and docstrings as documentation for how to query.
"""

import pytest

from src.graph.populators import (
    populate_from_analysis,
    populate_from_assignment,
    populate_from_collector,
    populate_from_load_test,
    populate_from_reality_check,
    populate_from_synthesis,
    populate_from_triage,
)
from src.graph.schema import initialize_schema
from src.graph.store import GraphStore


@pytest.fixture
def populated_graph(
    tmp_path,
    sample_collector_output,
    sample_triage_output,
    sample_assignment,
    sample_analysis_output,
    sample_reality_check_output,
    sample_load_test_output,
    sample_synthesis_output,
):
    """Graph populated with all node/edge types for cookbook queries."""
    store = GraphStore(str(tmp_path / "cookbook.lbug"))
    initialize_schema(store)
    populate_from_collector(sample_collector_output, store)
    populate_from_triage(sample_triage_output, store)
    populate_from_analysis(sample_analysis_output, "dynamodb", store)
    populate_from_assignment(sample_assignment, store)
    populate_from_reality_check(sample_reality_check_output, store)
    populate_from_load_test(sample_load_test_output, "dynamodb", 1, store)
    populate_from_synthesis(sample_synthesis_output, store)
    yield store
    store.close()


def test_find_all_queries_for_table(populated_graph):
    """Which queries touch the orders table?"""
    results = populated_graph.query(
        "MATCH (q:Query)-[:READS_FROM]->(st:SourceTable {id: 'orders'}) "
        "RETURN q.id ORDER BY q.id"
    )
    assert len(results) == 3
    assert [r["q.id"] for r in results] == ["q1", "q2", "q3"]


def test_impact_analysis_destination_change(populated_graph):
    """If I change a destination, which queries are affected?"""
    results = populated_graph.query(
        "MATCH (q:Query)-[:MIGRATES_TO]->(d:Destination) "
        "WHERE d.engine = 'dynamodb' "
        "RETURN q.id ORDER BY q.id"
    )
    assert len(results) == 2
    assert results[0]["q.id"] == "q1"
    assert results[1]["q.id"] == "q3"


def test_tables_spanning_multiple_engines(populated_graph):
    """Which source tables have queries split across more than one engine?"""
    results = populated_graph.query(
        "MATCH (q:Query)-[:READS_FROM]->(st:SourceTable), "
        "(q)-[:MIGRATES_TO]->(d:Destination) "
        "WITH st, COUNT(DISTINCT d.engine) AS engine_count "
        "WHERE engine_count > 1 "
        "RETURN st.id, engine_count"
    )
    # orders has queries going to both dynamodb and documentdb
    assert len(results) >= 1
    assert any(r["st.id"] == "orders" for r in results)


def test_trace_decision_provenance(populated_graph):
    """What decisions were made and why?"""
    results = populated_graph.query(
        "MATCH (d:Decision) RETURN d.id, d.category, d.description ORDER BY d.id"
    )
    assert len(results) >= 1
    categories = {r["d.category"] for r in results}
    assert "consolidation" in categories


def test_high_traffic_queries_with_anti_patterns(populated_graph):
    """Which high-traffic queries also have anti-patterns?"""
    results = populated_graph.query(
        "MATCH (ap:AntiPattern)-[:OBSERVED_IN_QUERY]->(q:Query) "
        "WHERE q.calls_per_second > 10 "
        "RETURN q.id, q.calls_per_second, ap.anti_pattern_type, ap.severity_weight"
    )
    assert len(results) == 1
    assert results[0]["q.id"] == "q1"
    assert results[0]["ap.anti_pattern_type"] == "hot-partition"


def test_signal_to_engine_correlation(populated_graph):
    """Which signals led to which engine assignments?"""
    results = populated_graph.query(
        "MATCH (q:Query)-[:EMITS_SIGNAL]->(s:Signal), "
        "(q)-[:MIGRATES_TO]->(d:Destination) "
        "RETURN s.id, d.engine, COUNT(q) AS query_count "
        "ORDER BY s.id"
    )
    assert len(results) >= 1


def test_load_test_performance(populated_graph):
    """What improvement did the load test show?"""
    results = populated_graph.query(
        "MATCH (q:Query)-[:TESTED_IN]->(lt:LoadTestRun) "
        "RETURN q.id, lt.source_p50, lt.target_p50, lt.improvement_factor"
    )
    assert len(results) == 1
    assert results[0]["lt.improvement_factor"] == 15.0


def test_risk_surface_area(populated_graph):
    """Which tables carry the highest risk concentration?"""
    results = populated_graph.query(
        "MATCH (r:Risk)-[:IMPACTS]->(st:SourceTable) "
        "RETURN st.id, COUNT(r) AS risk_count ORDER BY risk_count DESC"
    )
    assert len(results) >= 1


def test_co_dependency_group_members(populated_graph):
    """Which queries must move together?"""
    results = populated_graph.query(
        "MATCH (q:Query)-[:MEMBER_OF]->(g:CoDependencyGroup) "
        "RETURN g.id, g.reason, COLLECT(q.id) AS members"
    )
    assert len(results) == 1
    assert set(results[0]["members"]) == {"q1", "q3"}
