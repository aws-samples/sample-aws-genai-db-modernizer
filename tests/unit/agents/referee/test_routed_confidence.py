"""Routed confidence (#152): the fit of the work an engine was actually given."""

from __future__ import annotations

from src.agents.referee.reality_check import BASIC_CRUD_SCORE, SIGNAL_MATCH_BONUS
from src.agents.referee.routed_confidence import BASIS_CACHED, BASIS_OWNED, routed_fits


def _q(qid: str, *tables: str) -> dict:
    return {"query_id": qid, "query_type": "SELECT", "tables_accessed": list(tables)}


def _analysis(**scores: int) -> dict:
    return {
        "table_recommendations": [{"table_id": t, "confidence_score": s} for t, s in scores.items()]
    }


QUERIES = [_q("s1", "posts"), _q("s2", "posts"), _q("k1", "users"), _q("k2", "users", "meta")]

TRIAGE = {
    "signals": [
        {"signal": "text_search", "query_ids": ["s1", "s2"]},
        {"signal": "low_frequency_reads", "query_ids": ["s1", "s2", "k1", "k2"]},
        {"signal": "key_value_lookups", "query_ids": ["k1", "k2"]},
    ]
}

# OpenSearch rates 1 of 3 tables well: its all-tables average is 33%.
ANALYSIS = {
    "opensearch": _analysis(posts=80, users=10, meta=10),
    "dynamodb": _analysis(posts=40, users=90, meta=70),
    "elasticache": _analysis(posts=30, users=60, meta=80),
}


def _assignment(*rows: dict) -> dict:
    return {"version": 2, "query_assignments": list(rows)}


def _qa(qid: str, engine: str, **extra) -> dict:
    return {"query_id": qid, "assigned_engine": engine, **extra}


class TestOwnerFit:
    def test_fit_is_the_mean_of_the_routed_queries(self):
        a = _assignment(
            _qa("s1", "opensearch"),
            _qa("s2", "opensearch"),
            _qa("k1", "dynamodb"),
            _qa("k2", "dynamodb"),
        )
        fits = routed_fits(a, TRIAGE, QUERIES, ANALYSIS)
        # text_search matches OpenSearch's capability: 80 + bonus, capped at 100
        assert fits["opensearch"].confidence == min(100, 80 + SIGNAL_MATCH_BONUS)
        assert fits["opensearch"].basis == BASIS_OWNED
        assert fits["opensearch"].queries == 2
        assert fits["opensearch"].tables == 1
        # k1: users 90 (+bonus, capped); k2: mean(90, 70) = 80 (+bonus)
        expected = round(
            (min(100, 90 + SIGNAL_MATCH_BONUS) + min(100, 80 + SIGNAL_MATCH_BONUS)) / 2
        )
        assert fits["dynamodb"].confidence == expected
        assert fits["dynamodb"].tables == 2

    def test_stored_per_query_confidence_is_ignored(self):
        """Reality Check moves a query without updating its stored confidence."""
        a = _assignment(
            _qa("s1", "opensearch", confidence=5), _qa("s2", "opensearch", confidence=5)
        )
        assert routed_fits(a, TRIAGE, QUERIES, ANALYSIS)["opensearch"].confidence > 50

    def test_out_of_scope_queries_do_not_count(self):
        a = _assignment(_qa("s1", "opensearch"), _qa("k1", "opensearch", in_scope=False))
        fit = routed_fits(a, TRIAGE, QUERIES, ANALYSIS)["opensearch"]
        assert fit.queries == 1
        assert fit.confidence == min(100, 80 + SIGNAL_MATCH_BONUS)

    def test_engine_with_no_routed_query_has_none(self):
        a = _assignment(_qa("s1", "opensearch"))
        fit = routed_fits(a, TRIAGE, QUERIES, ANALYSIS)["dynamodb"]
        assert fit.confidence is None
        assert fit.queries == 0

    def test_no_assignment_means_no_routed_confidence(self):
        assert routed_fits(None, TRIAGE, QUERIES, ANALYSIS) == {}


class TestLeadSignal:
    def test_workload_characteristics_never_lead(self):
        a = _assignment(_qa("s1", "opensearch"), _qa("s2", "opensearch"))
        fit = routed_fits(a, TRIAGE, QUERIES, ANALYSIS)["opensearch"]
        assert (fit.lead_signal, fit.lead_count) == ("text_search", 2)

    def test_a_signal_the_engine_serves_leads(self):
        triage = {
            "signals": [
                {"signal": "status_filters", "query_ids": ["k1", "k2"]},
                {"signal": "key_value_lookups", "query_ids": ["k1"]},
            ]
        }
        a = _assignment(_qa("k1", "dynamodb"), _qa("k2", "dynamodb"))
        fit = routed_fits(a, triage, QUERIES, ANALYSIS)["dynamodb"]
        assert (fit.lead_signal, fit.lead_count) == ("key_value_lookups", 1)


class TestCacheLayer:
    def test_cache_is_measured_on_the_reads_it_fronts(self):
        a = _assignment(
            _qa("k1", "dynamodb", cache_engine="elasticache", cache_pattern="point_lookup"),
            _qa("k2", "dynamodb", cache_engine="elasticache", cache_pattern="point_lookup"),
            _qa("s1", "opensearch"),
        )
        fit = routed_fits(a, TRIAGE, QUERIES, ANALYSIS)["elasticache"]
        assert fit.basis == BASIS_CACHED
        assert fit.queries == 2
        # The owner's key_value_lookups signal is not a cache penalty: k1 users=60,
        # k2 mean(60, 80)=70, so 65 with no bonus and no penalty
        assert fit.confidence == 65
        assert (fit.lead_signal, fit.lead_count) == ("point_lookup", 2)

    def test_cache_hint_signal_earns_the_bonus(self):
        triage = {"signals": [{"signal": "leaderboard_pattern", "query_ids": ["k1"]}]}
        a = _assignment(_qa("k1", "dynamodb", cache_engine="elasticache", cache_pattern="top_n"))
        fit = routed_fits(a, triage, QUERIES, ANALYSIS)["elasticache"]
        assert fit.confidence == 60 + SIGNAL_MATCH_BONUS

    def test_cache_without_cached_reads_has_none(self):
        a = _assignment(_qa("k1", "dynamodb"))
        assert routed_fits(a, TRIAGE, QUERIES, ANALYSIS)["elasticache"].confidence is None

    def test_tables_without_analysis_use_the_basic_crud_baseline(self):
        a = _assignment(_qa("x", "dynamodb"))
        fit = routed_fits(a, {"signals": []}, [_q("x", "unknown")], ANALYSIS)["dynamodb"]
        assert fit.confidence == BASIC_CRUD_SCORE
