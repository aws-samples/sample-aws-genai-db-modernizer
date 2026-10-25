"""The approval-gate figures are computed once, in the facts layer (#1307, ADR-032)."""

from __future__ import annotations

from src.shared.gate_facts import gate_block_facts

GATE = {
    "before_distribution": {"aurora_mysql": 59, "dynamodb": 38, "documentdb": 9, "opensearch": 1},
    "after_distribution": {"aurora_mysql": 67, "dynamodb": 40, "documentdb": 0, "opensearch": 0},
    "eliminated_engines": ["documentdb", "opensearch"],
    "consolidations": [{"from": "documentdb", "to": "aurora_mysql", "queries": 7}],
}


def test_rows_run_from_the_largest_final_count_with_shares() -> None:
    facts = gate_block_facts(GATE)
    assert [r["engine"] for r in facts["rows"]] == [
        "aurora_mysql",
        "dynamodb",
        "documentdb",
        "opensearch",
    ]
    assert facts["rows"][0] == {
        "engine": "aurora_mysql",
        "before": 59,
        "after": 67,
        "share_percent": 62.6,
        "dropped": False,
    }
    assert facts["total_before"] == 107
    assert facts["total_after"] == 107
    assert facts["unchanged"] is False


def test_dropped_comes_from_eliminated_engines_not_from_a_zero_count() -> None:
    # An eliminated engine can keep a count for an out-of-scope query, and a kept one can be 0.
    facts = gate_block_facts(
        {
            "before_distribution": {"a": 5, "b": 5},
            "after_distribution": {"a": 8, "b": 2},
            "eliminated_engines": ["b"],
        }
    )
    assert {r["engine"]: r["dropped"] for r in facts["rows"]} == {"a": False, "b": True}


def test_nothing_changed_is_stated_by_the_facts() -> None:
    same = {"a": 3, "b": 1}
    facts = gate_block_facts(
        {"before_distribution": same, "after_distribution": same, "consolidations": []}
    )
    assert facts["unchanged"] is True


def test_a_consolidation_or_elimination_is_a_change_even_with_equal_counts() -> None:
    same = {"a": 3, "b": 1}
    assert not gate_block_facts(
        {"before_distribution": same, "after_distribution": same, "eliminated_engines": ["b"]}
    )["unchanged"]
    assert not gate_block_facts(
        {
            "before_distribution": same,
            "after_distribution": same,
            "consolidations": [{"from": "b", "to": "a", "queries": 0}],
        }
    )["unchanged"]


def test_ties_sort_by_engine_id_and_an_empty_gate_has_no_rows_and_zero_shares() -> None:
    tie = gate_block_facts(
        {"before_distribution": {"b": 2, "a": 2}, "after_distribution": {"b": 2, "a": 2}}
    )
    assert [r["engine"] for r in tie["rows"]] == ["a", "b"]
    assert [r["share_percent"] for r in tie["rows"]] == [50.0, 50.0]
    empty = gate_block_facts({})
    assert empty == {
        "rows": [],
        "surviving": [],
        "cache_engine": None,
        "cache_candidates": None,
        "total_before": 0,
        "total_after": 0,
        "unchanged": True,
        # #1574: no engine scope, so no query is left with no engine in it.
        "engine_scope": None,
        "unservable_query_count": 0,
        "query_total_count": 0,
    }


def test_an_engine_only_in_the_final_distribution_still_gets_a_row() -> None:
    facts = gate_block_facts(
        {"before_distribution": {"a": 1}, "after_distribution": {"a": 1, "c": 1}}
    )
    assert {r["engine"]: r["before"] for r in facts["rows"]} == {"a": 1, "c": 0}


def test_the_surviving_engines_lead_with_the_largest_and_end_with_the_cache_layer() -> None:
    facts = gate_block_facts(
        {**GATE, "schema_design_engines": ["aurora_mysql", "dynamodb", "elasticache"]}
    )
    assert facts["surviving"] == ["aurora_mysql", "dynamodb", "elasticache"]
    assert facts["cache_engine"] == "elasticache"


def test_without_a_cache_layer_there_is_no_cache_engine() -> None:
    facts = gate_block_facts({**GATE, "schema_design_engines": ["aurora_mysql", "dynamodb"]})
    assert facts["surviving"] == ["aurora_mysql", "dynamodb"]
    assert facts["cache_engine"] is None


def _qa(
    qid, owner="aurora_mysql", cached=True, tables=("t",), in_scope=True, pattern="point_lookup"
):
    return {
        "query_id": qid,
        "assigned_engine": owner,
        "source_tables": list(tables),
        "in_scope": in_scope,
        "cache_engine": "elasticache" if cached else None,
        "cache_pattern": pattern if cached else None,
    }


def test_cache_candidate_reads_list_the_hottest_first_with_their_share_of_calls() -> None:
    from src.shared.gate_facts import cache_candidate_reads

    qas = [
        _qa("a"),
        _qa("b", owner="dynamodb", tables=("x", "y")),
        _qa("c"),
        _qa("d", cached=False),
    ]
    queries = [
        {"query_id": "a", "calls_per_second": 10},
        {"query_id": "b", "calls_per_second": 30},
        {"query_id": "c", "calls_per_second": 10},
        {"query_id": "d", "calls_per_second": 50},
    ]
    out = cache_candidate_reads(qas, queries, limit=2)
    assert [r["query_id"] for r in out["reads"]] == ["b", "a"]  # hottest, then id
    assert out["reads"][0] == {
        "query_id": "b",
        "tables": ["x", "y"],
        "owner": "dynamodb",
        "pattern": "point_lookup",
        "calls_per_second": 30.0,
        "call_share_percent": 30.0,  # of all 100 in-scope calls/s, as the overlay's share is
    }
    assert out["more"] == 1


def test_cache_candidate_reads_skip_out_of_scope_queries_and_empty_overlays() -> None:
    from src.shared.gate_facts import cache_candidate_reads

    assert cache_candidate_reads([_qa("a", cached=False)], [{"query_id": "a"}]) is None
    out = cache_candidate_reads(
        [_qa("a"), _qa("b", in_scope=False)],
        [{"query_id": "a", "calls_per_second": 4}, {"query_id": "b", "calls_per_second": 99}],
    )
    assert [r["query_id"] for r in out["reads"]] == ["a"]
    assert out["reads"][0]["call_share_percent"] == 100.0
    assert out["more"] == 0


def test_cache_candidate_reads_without_call_rates_show_zero_shares() -> None:
    from src.shared.gate_facts import cache_candidate_reads

    out = cache_candidate_reads([_qa("a")], [{"query_id": "a"}])
    assert out["reads"][0]["call_share_percent"] == 0.0


def test_the_gate_facts_carry_the_cache_candidate_reads() -> None:
    reads = {"reads": [{"query_id": "a"}], "more": 3}
    facts = gate_block_facts(
        {
            **GATE,
            "cache_overlay": {"query_count": 4, "call_share_percent": 50.0},
            "cache_reads": reads,
        }
    )
    assert facts["cache_candidates"]["reads"] == reads["reads"]
    assert facts["cache_candidates"]["more"] == 3
    bare = gate_block_facts({**GATE, "cache_overlay": {"query_count": 4}})
    assert bare["cache_candidates"]["reads"] == []
    assert bare["cache_candidates"]["more"] == 0
