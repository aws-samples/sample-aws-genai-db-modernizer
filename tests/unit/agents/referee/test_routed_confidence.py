"""Routed confidence (#152): the fit of the work an engine was actually given.

#490: the fit this module reports is now the resolver's own score
(``query_scoring.score_query_for_engine``) -- base table/pattern confidence
minus the resolver's anti-pattern penalty -- not the reality check's own,
separately drifting ``_engine_fit_score`` (deleted, along with its
``BASIC_CRUD_SCORE`` flat baseline and ``SIGNAL_MATCH_BONUS``). None of the
fixtures below give any engine a ``workload_analysis`` with
anti-patterns/patterns detected, so
the resolver's own penalty is always 0 here and every number below is a
plain base confidence: a table-recommendation average for most engines, the
query's own matched pattern (or 0 with none) for ``PATTERN_ONLY_ENGINES``
(OpenSearch), and a last-resort average across every table recommendation
when a query's own tables rate none (e.g. the collector's ``"unknown"``
placeholder).
"""

from __future__ import annotations

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
        # OpenSearch never uses the table-level average as its own fit (#475:
        # that number reflects whether ANY query against the table needs
        # search, not whether this one does) -- it uses its own matched
        # pattern instead, and ANALYSIS below gives it none, so its fit is 0
        # (#490: no flat basic-CRUD baseline/signal bonus to fall back to
        # any more -- the resolver's own scorer has neither).
        assert fits["opensearch"].confidence == 0
        assert fits["opensearch"].basis == BASIS_OWNED
        assert fits["opensearch"].queries == 2
        assert fits["opensearch"].tables == 1
        # k1: users 90; k2: mean(90, 70) = 80 -- no signal bonus any more.
        assert fits["dynamodb"].confidence == round((90 + 80) / 2)
        assert fits["dynamodb"].tables == 2

    def test_stored_per_query_confidence_is_ignored(self):
        """Reality Check moves a query without updating its stored confidence."""
        a = _assignment(_qa("k1", "dynamodb", confidence=5), _qa("k2", "dynamodb", confidence=5))
        assert routed_fits(a, TRIAGE, QUERIES, ANALYSIS)["dynamodb"].confidence > 50

    def test_out_of_scope_queries_do_not_count(self):
        a = _assignment(_qa("s1", "opensearch"), _qa("k1", "opensearch", in_scope=False))
        fit = routed_fits(a, TRIAGE, QUERIES, ANALYSIS)["opensearch"]
        assert fit.queries == 1
        assert fit.confidence == 0

    def test_engine_with_no_routed_query_has_none(self):
        a = _assignment(_qa("s1", "opensearch"))
        fit = routed_fits(a, TRIAGE, QUERIES, ANALYSIS)["dynamodb"]
        assert fit.confidence is None
        assert fit.queries == 0

    def test_no_assignment_means_no_routed_confidence(self):
        assert routed_fits(None, TRIAGE, QUERIES, ANALYSIS) == {}


class TestCustomerOverride:
    def test_an_overridden_query_gets_the_new_owners_fit(self):
        """A customer moves k1 to OpenSearch: OpenSearch's fit for it counts, not DynamoDB's."""
        a = _assignment(
            _qa("k1", "opensearch", confidence=95, assignment_reason="customer override"),
            _qa("k2", "dynamodb"),
        )
        fits = routed_fits(a, TRIAGE, QUERIES, ANALYSIS)
        # OpenSearch ignores the "users" table-level average (#475) and uses
        # its own matched pattern instead -- ANALYSIS gives it none, so its
        # fit is 0 (#490: no basic-CRUD/signal-bonus fallback any more).
        # The stored confidence (95) and DynamoDB's fit (90+) are not used.
        assert fits["opensearch"].queries == 1
        assert fits["opensearch"].confidence == 0
        assert fits["dynamodb"].queries == 1


class TestLeadSignal:
    def test_workload_characteristics_never_lead(self):
        a = _assignment(_qa("s1", "opensearch"), _qa("s2", "opensearch"))
        fit = routed_fits(a, TRIAGE, QUERIES, ANALYSIS)["opensearch"]
        assert (fit.lead_signal, fit.lead_count) == ("text_search", 2)

    def test_a_tie_names_no_lead(self):
        triage = {
            "signals": [
                {"signal": "status_filters", "query_ids": ["k1"]},
                {"signal": "key_value_lookups", "query_ids": ["k2"]},
            ]
        }
        a = _assignment(_qa("k1", "dynamodb"), _qa("k2", "dynamodb"))
        fit = routed_fits(a, triage, QUERIES, ANALYSIS)["dynamodb"]
        assert (fit.lead_signal, fit.lead_count) == (None, 0)

    def test_a_plurality_under_the_minimum_share_names_no_lead(self):
        queries = [_q(f"q{i}", "users") for i in range(10)]
        triage = {"signals": [{"signal": "key_value_lookups", "query_ids": ["q0", "q1"]}]}
        a = _assignment(*(_qa(f"q{i}", "dynamodb") for i in range(10)))
        assert routed_fits(a, triage, queries, ANALYSIS)["dynamodb"].lead_signal is None
        triage["signals"][0]["query_ids"].append("q2")  # 3 of 10 >= 25%
        assert routed_fits(a, triage, queries, ANALYSIS)["dynamodb"].lead_signal == (
            "key_value_lookups"
        )


class TestTableEvidence:
    def test_unknown_tables_are_not_counted_and_flag_signal_only(self):
        """discourse: the 3 OpenSearch queries have tables_accessed ["unknown"], so
        OpenSearch (a ``PATTERN_ONLY_ENGINES`` engine) has no matched pattern for
        any of them and its fit is a constant 0 (#490: no CRUD baseline/signal
        bonus to fall back to any more)."""
        queries = [_q(f"t{i}", "unknown") for i in range(3)]
        triage = {"signals": [{"signal": "text_search", "query_ids": ["t0", "t1", "t2"]}]}
        a = _assignment(*(_qa(f"t{i}", "opensearch") for i in range(3)))
        fit = routed_fits(a, triage, queries, ANALYSIS, source_tables=["posts", "users"])[
            "opensearch"
        ]
        assert fit.confidence == 0
        assert fit.tables == 0
        assert fit.evidence == "signal_only"
        assert fit.unbacked_queries == 3

    def test_names_that_are_not_source_tables_do_not_count(self):
        """Table-backed evidence, for an engine OUTSIDE ``PATTERN_ONLY_ENGINES``
        (dynamodb, not opensearch -- #475 review, finding 4: opensearch's own
        fit never reads a table recommendation, so it has no table-backed
        evidence to test here; see ``TestPatternEvidence`` below instead).
        """
        queries = [_q("a", "posts", "pg_type"), _q("b", "w")]
        a = _assignment(_qa("a", "dynamodb"), _qa("b", "dynamodb"))
        fit = routed_fits(a, TRIAGE, queries, ANALYSIS, source_tables=["posts", "users", "meta"])[
            "dynamodb"
        ]
        assert fit.tables == 1
        assert fit.evidence == "partial"
        assert fit.unbacked_queries == 1

    def test_fully_backed(self):
        """Same note as above: dynamodb, not opensearch (#475 review, finding 4)."""
        a = _assignment(_qa("s1", "dynamodb"), _qa("s2", "dynamodb"))
        fit = routed_fits(a, TRIAGE, QUERIES, ANALYSIS, source_tables=["posts"])["dynamodb"]
        assert (fit.evidence, fit.unbacked_queries, fit.tables) == ("table", 0, 1)


class TestPatternEvidence:
    """#475 review, finding 4: a ``PATTERN_ONLY_ENGINES`` engine's evidence is
    whether the query matches one of its OWN detected patterns, never
    whether a table recommendation merely exists for it.
    """

    def test_pattern_matched_query_is_fully_backed(self):
        queries = [_q("s1", "posts")]
        triage = {"signals": [{"signal": "text_search", "query_ids": ["s1"]}]}
        analysis = {
            "opensearch": {
                "table_recommendations": [{"table_id": "posts", "confidence_score": 80}],
                "workload_analysis": {
                    "patterns_detected": [
                        {
                            "pattern_type": "full-text-search",
                            "query_ids": ["s1"],
                            "table_ids": ["posts"],
                        }
                    ]
                },
            }
        }
        a = _assignment(_qa("s1", "opensearch"))
        fit = routed_fits(a, triage, queries, analysis, source_tables=["posts"])["opensearch"]
        assert fit.evidence == "pattern"
        assert fit.unbacked_queries == 0

    def test_table_recommendation_alone_is_not_evidence(self):
        """A table recommendation exists for ``posts``, but s1 is not in any
        OpenSearch pattern's query_ids -- not evidence, unlike every other
        engine, where the same table recommendation would be.
        """
        queries = [_q("s1", "posts")]
        triage = {"signals": [{"signal": "text_search", "query_ids": ["s1"]}]}
        analysis = {
            "opensearch": {
                "table_recommendations": [{"table_id": "posts", "confidence_score": 80}],
                "workload_analysis": {"patterns_detected": []},
            }
        }
        a = _assignment(_qa("s1", "opensearch"))
        fit = routed_fits(a, triage, queries, analysis, source_tables=["posts"])["opensearch"]
        assert fit.evidence == "signal_only"
        assert fit.unbacked_queries == 1


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

    def test_the_overlays_cache_pattern_earns_no_separate_bonus(self):
        """#490: the overlay's ``cache_pattern`` used to earn ElastiCache a flat
        signal-match bonus here, synthesized specifically for this module's own,
        now-deleted call into ``_engine_fit_score``. ElastiCache's fit is now the
        same resolver score every other engine gets from its own analysis
        (``k1``'s ``users`` table confidence, 60) -- ``cache_pattern`` still
        decides WHO the cache fronts (``cache_overlay.py``), just not a scoring
        bonus here.
        """
        a = _assignment(_qa("k1", "dynamodb", cache_engine="elasticache", cache_pattern="top_n"))
        fit = routed_fits(a, {"signals": []}, QUERIES, ANALYSIS)["elasticache"]
        assert fit.confidence == 60

    def test_a_triage_hint_on_a_point_lookup_earns_nothing(self):
        """leaderboard_pattern fires on reads the overlay classified as point lookups."""
        triage = {"signals": [{"signal": "leaderboard_pattern", "query_ids": ["k1"]}]}
        a = _assignment(
            _qa("k1", "dynamodb", cache_engine="elasticache", cache_pattern="point_lookup")
        )
        assert routed_fits(a, triage, QUERIES, ANALYSIS)["elasticache"].confidence == 60

    def test_cache_without_cached_reads_has_none(self):
        a = _assignment(_qa("k1", "dynamodb"))
        assert routed_fits(a, TRIAGE, QUERIES, ANALYSIS)["elasticache"].confidence is None

    def test_tables_without_analysis_fall_back_to_the_resolvers_own_last_resort(self):
        """#490: no table recommendation matches ``x``'s own ``tables_accessed``
        (the collector's ``"unknown"`` placeholder) -- the resolver's own
        last-resort branch (``AssignmentResolver._compute_query_confidence``)
        averages across every one of DynamoDB's table recommendations instead
        of a flat basic-CRUD baseline: mean(40, 90, 70) = 66.
        """
        a = _assignment(_qa("x", "dynamodb"))
        fit = routed_fits(a, {"signals": []}, [_q("x", "unknown")], ANALYSIS)["dynamodb"]
        assert fit.confidence == 66
        assert fit.evidence == "signal_only"
