"""Tests for curated graph query view functions."""

from src.graph.populators import (
    populate_from_assignment,
    populate_from_collector,
    populate_from_reality_check,
    populate_from_schema_design,
)
from src.graph.queries import query_provenance, table_impact


def test_table_impact_lists_affected_queries(
    graph_store, sample_collector_output, sample_assignment, sample_schema_design_output
):
    populate_from_collector(sample_collector_output, graph_store)
    populate_from_assignment(sample_assignment, graph_store)
    populate_from_schema_design(sample_schema_design_output, "dynamodb", 1, graph_store)

    result = table_impact(graph_store, "orders")
    assert result.table_id == "orders"
    assert isinstance(result.affected_queries, list)
    # q1, q2, q3 all read 'orders'
    assert {q.query_id for q in result.affected_queries} == {"q1", "q2", "q3"}
    # highest-traffic query first (q1 @ 423 cps)
    assert result.affected_queries[0].query_id == "q1"


def test_table_impact_unknown_table_is_empty(graph_store, sample_collector_output):
    populate_from_collector(sample_collector_output, graph_store)
    result = table_impact(graph_store, "does-not-exist")
    assert result.table_id == "does-not-exist"
    assert result.affected_queries == []


def test_query_provenance_includes_agent(
    graph_store, sample_collector_output, sample_reality_check_output
):
    populate_from_collector(sample_collector_output, graph_store)
    populate_from_reality_check(sample_reality_check_output, graph_store)
    qid = sample_collector_output["queries"]["query_patterns"][0]["query_id"]
    result = query_provenance(graph_store, qid)
    assert result.query_id == qid
    assert isinstance(result.signals, list)
    assert isinstance(result.decisions, list)
