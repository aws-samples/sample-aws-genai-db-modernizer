"""Tests for curated graph query view functions."""

from src.graph.populators import (
    populate_from_assignment,
    populate_from_collector,
    populate_from_reality_check,
    populate_from_schema_design,
    populate_from_triage,
)
from src.graph.queries import engine_detail, query_provenance, risk_hotspots, table_impact


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


def test_engine_detail_lists_destinations(graph_store, sample_collector_output, sample_assignment):
    populate_from_collector(sample_collector_output, graph_store)
    populate_from_assignment(sample_assignment, graph_store)
    result = engine_detail(graph_store, "dynamodb")
    assert result.engine == "dynamodb"
    assert isinstance(result.destinations, list)
    assert result.destinations, "expected at least one dynamodb destination"


def test_risk_hotspots_returns_list(graph_store, sample_collector_output):
    populate_from_collector(sample_collector_output, graph_store)
    result = risk_hotspots(graph_store)
    # no synthesis/analysis run → no risk or anti-pattern hotspots
    assert result.hotspots == []


def test_table_impact_lists_are_sorted_and_ties_break_on_query_id(graph_store):
    populate_from_collector(
        {
            "queries": {
                "query_patterns": [
                    {
                        "query_id": qid,
                        "query_text": "SELECT 1",
                        "query_type": "SELECT",
                        "tables_accessed": ["orders"],
                        "calls_per_second": 5.0,
                    }
                    for qid in ("qc", "qa", "qb")
                ]
            }
        },
        graph_store,
    )
    result = table_impact(graph_store, "orders")
    # Equal traffic: the secondary key makes the order stable.
    assert [q.query_id for q in result.affected_queries] == ["qa", "qb", "qc"]


def _collector(rows: dict[str, list[str]], cps: float = 1.0) -> dict:
    return {
        "queries": {
            "query_patterns": [
                {
                    "query_id": qid,
                    "query_text": "SELECT 1",
                    "query_type": "SELECT",
                    "tables_accessed": tables,
                    "calls_per_second": cps,
                }
                for qid, tables in rows.items()
            ]
        }
    }


def _qa(qid: str, engine: str, table: str) -> dict:
    return {
        "query_id": qid,
        "assigned_engine": engine,
        "confidence": 0.9,
        "source_tables": [table],
        "assignment_reason": "r",
        "in_scope": True,
    }


def _ap(pattern_id: str, qids: list[str]) -> dict:
    return {
        "pattern_id": pattern_id,
        "pattern_group": "g",
        "query_ids": qids,
        "description": "d",
        "operation": "op",
        "table_name": "t",
        "design_rps": 1.0,
        "in_scope": True,
    }


def test_engine_detail_lists_and_ties_are_ordered(graph_store):
    # Every list is inserted in reverse sorted order, and the two destinations tie on
    # query_count with the larger id inserted first.
    populate_from_collector(
        _collector({"q1": ["zeta", "mid"], "q2": ["alpha"], "q3": ["x"], "q4": ["y"]}),
        graph_store,
    )
    populate_from_assignment(
        {
            "query_assignments": [
                _qa("q1", "dynamodb", "zz"),
                _qa("q2", "dynamodb", "zz"),
                _qa("q3", "dynamodb", "aa"),
                _qa("q4", "dynamodb", "aa"),
            ],
            "table_assignments": [],
            "co_dependency_groups": [],
        },
        graph_store,
    )
    populate_from_schema_design(
        {
            "trade_offs": [],
            "access_patterns": [
                _ap("DDB-AP-3", ["q1"]),
                _ap("DDB-AP-1", ["q1", "q2"]),
                _ap("DDB-AP-2", ["q2"]),
            ],
        },
        "dynamodb",
        1,
        graph_store,
    )
    dests = engine_detail(graph_store, "dynamodb").destinations
    assert [d.destination_id for d in dests] == ["aa-dynamodb", "zz-dynamodb"]
    zz = dests[1]
    assert zz.source_tables == ["alpha", "mid", "zeta"]
    assert zz.access_patterns == ["DDB-AP-1", "DDB-AP-2", "DDB-AP-3"]


def test_query_provenance_signals_and_decisions_are_ordered(graph_store):
    populate_from_collector(_collector({"q1": ["orders"]}), graph_store)
    populate_from_triage(
        {
            "signals": [
                {
                    "signal": sig,
                    "targets": ["dynamodb"],
                    "evidence": "e",
                    "query_ids": ["q1"],
                    "table_ids": ["orders"],
                    "query_count": 1,
                }
                for sig in ("sig_z", "sig_m", "sig_a")
            ]
        },
        graph_store,
    )
    for engine in ("opensearch", "dynamodb", "documentdb"):
        populate_from_schema_design(
            {
                "trade_offs": [
                    {
                        "description": f"t {engine}",
                        "impact": "i",
                        "source_tables": ["orders"],
                        "target_tables": ["t"],
                        "query_ids": ["q1"],
                        "engine": engine,
                    }
                ],
                "access_patterns": [],
            },
            engine,
            1,
            graph_store,
        )
    r = query_provenance(graph_store, "q1")
    assert r.signals == ["sig_a", "sig_m", "sig_z"]
    assert [d.decision_id for d in r.decisions] == [
        "trade_off-documentdb-0",
        "trade_off-dynamodb-0",
        "trade_off-opensearch-0",
    ]
