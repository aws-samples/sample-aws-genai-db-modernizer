"""The per-query, per-engine cost dimension (ADR-031 Decision 5, #494 slice 2).

A classified query's cost dimension on an engine is its own estimated monthly cost
there, from the engine file's ``[pricing]`` inputs (#923): the engine's standing cost
shared over the queries it could serve, plus the variable cost the query's own call
rate and size drive. It is no longer its tables' engine-blind cost band
(``scoring._cost``). These tests read only the routing path production uses
(``ScoringContext.from_pipeline``, ``score_query_for_engine`` and the resolver).
"""

from __future__ import annotations

import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src.agents.referee.assignment_resolver import AssignmentResolver
from src.agents.referee.query_cost import SECONDS_PER_MONTH, QueryCostModel
from src.agents.referee.query_scoring import ScoringContext, score_query_for_engine
from src.agents.referee.routing_trace import RoutingTrace
from src.contracts.analysis_output import ScoreBreakdown, WorkloadAnalysis
from src.shared import catalog
from src.tools.analysis.score_weights import weight_profiles
from src.tools.analysis.scoring import (
    build_table_profiles,
    compute_base_scores,
    compute_confidence,
)

SOR_ENGINES = ("aurora_postgresql", "aurora_mysql", "dynamodb", "documentdb")


def _rec(engine, table_id, pattern=80, complexity=80, performance=60, cost=70):
    breakdown = {
        "pattern_match_score": pattern,
        "complexity_score": complexity,
        "performance_score": performance,
        "cost_score": cost,
    }
    weights = dict(weight_profiles(engine)[0])
    return {
        "table_id": table_id,
        "confidence_score": compute_confidence(ScoreBreakdown(**breakdown), weights),
        "score_breakdown": breakdown,
    }


def _analysis(engine, *tables, **dims):
    return {
        "table_recommendations": [_rec(engine, t, **dims) for t in tables],
        "workload_analysis": {"patterns_detected": [], "anti_patterns_detected": []},
    }


def _table(name, pk, columns, row_count=100_000, size_mb=10.0):
    return {
        "table_id": f"d.{name}",
        "table_name": name,
        "schema_name": "d",
        "row_count": row_count,
        "size_mb": size_mb,
        "primary_key": pk,
        "columns": [{"column_name": c, "data_type": "int"} for c in columns],
        "indexes": (
            [{"index_name": "ix_category_id", "columns": ["category_id"]}]
            if "category_id" in columns
            else []
        ),
        "foreign_keys": None,
    }


# 10 MiB over 10,240 rows: 1 KiB a row.
TABLES = [
    _table("topics", ["id"], ["id", "category_id", "title", "views"], row_count=10_240),
    _table("categories", ["id"], ["id", "name", "position"], row_count=10_240),
]


def _query(qid, text, tables=("topics",), qtype="SELECT", cps=0.01, rows=1.0, **extra):
    return {
        "query_id": qid,
        "query_text": text,
        "query_type": qtype,
        "tables_accessed": [f"d.{t}" for t in tables],
        "calls_per_second": cps,
        "rows_returned_avg": rows if qtype == "SELECT" else 0.0,
        "rows_affected_avg": rows if qtype != "SELECT" else 0.0,
        "has_joins": len(tables) > 1,
        **extra,
    }


def _collector(*queries, source="postgresql"):
    return {
        "metadata": {"source_database": {"engine": source}},
        "database_schema": {"tables": TABLES},
        "queries": {"query_patterns": list(queries)},
    }


def _ctx(engines, *queries, triage=None):
    analysis = {e: _analysis(e, "d.topics", "d.categories") for e in engines}
    return ScoringContext.from_pipeline(analysis, triage, _collector(*queries))


LOOKUP = _query("lookup", "SELECT * FROM topics WHERE id = $1")


def _expected_score(total_monthly: float, engine: str) -> int:
    ref = catalog.pricing(engine).max_burden_per_unique_query
    return int(round(100 * ref / (ref + total_monthly)))


# ---------------------------------------------------------------------------
# The issue's acceptance test: a low-traffic engine with high standing cost loses rank
# ---------------------------------------------------------------------------


class TestStandingCostIsSharedOverTheQueriesAnEngineCouldServe:
    TRIAGE = {
        "selected_agents": [{"agent_type": "aurora_postgresql"}, {"agent_type": "dynamodb"}],
        "query_capabilities": {},
        "signals": [],
    }

    def _trace(self, *queries):
        # The other dimensions put Aurora PostgreSQL ahead: on its own tables'
        # complexity and performance it scores 75 on the key lookup, DynamoDB 72
        # (the key-lookup class rates them 80 and 100).
        analysis = {
            "aurora_postgresql": _analysis("aurora_postgresql", "d.topics", "d.categories"),
            "dynamodb": _analysis(
                "dynamodb", "d.topics", "d.categories", complexity=10, performance=0
            ),
        }
        trace = RoutingTrace()
        assignment = AssignmentResolver().resolve(
            self.TRIAGE, analysis, _collector(*queries), routing_trace=trace
        )
        return trace, {qa.query_id: qa.assigned_engine for qa in assignment.query_assignments}

    def test_a_low_traffic_engine_with_high_standing_cost_loses_rank(self):
        """One low-traffic key lookup is the whole workload. Aurora PostgreSQL's $250
        standing cost falls on that one query (cost score 29), DynamoDB's on-demand
        reads cost it cents (100): Aurora PostgreSQL loses rank in the decision trace
        and the query goes to DynamoDB. With the old engine-blind cost band (70 on
        both) Aurora PostgreSQL ranked first, 75 to 72."""
        trace, engines = self._trace(LOOKUP)
        ladder = trace.by_query["lookup"]["engines"]
        assert ladder["dynamodb"] > ladder["aurora_postgresql"], ladder
        assert engines["lookup"] == "dynamodb"

    def test_the_same_query_keeps_its_rank_when_the_engine_serves_many(self):
        """The same lookup among 200 low-traffic queries both engines could serve:
        Aurora PostgreSQL's standing cost is shared 200 ways ($1.25 a query, cost
        score 99), so it keeps its rank and the query."""
        others = [
            _query(f"o{i}", f"SELECT * FROM categories WHERE id = {i}", ("categories",))
            for i in range(199)
        ]
        trace, engines = self._trace(LOOKUP, *others)
        ladder = trace.by_query["lookup"]["engines"]
        assert ladder["aurora_postgresql"] >= ladder["dynamodb"], ladder
        assert engines["lookup"] == "aurora_postgresql"

    def test_the_cost_dimension_is_the_standing_share_on_a_capacity_priced_engine(self):
        ctx = _ctx(("aurora_postgresql",), LOOKUP)
        base = catalog.pricing("aurora_postgresql").base_monthly
        assert ctx.cost_score(LOOKUP, "aurora_postgresql") == _expected_score(
            base, "aurora_postgresql"
        )
        two = _query("two", "SELECT * FROM topics WHERE id = $2")
        ctx = _ctx(("aurora_postgresql",), LOOKUP, two)
        assert ctx.cost_score(LOOKUP, "aurora_postgresql") == _expected_score(
            base / 2, "aurora_postgresql"
        )

    def test_a_query_an_engine_cannot_own_does_not_share_its_standing_cost(self):
        """OpenSearch is not a system of record: it could not serve the write, so the
        read alone carries its standing cost (and the write still scores, the
        resolver's write gate is what keeps it off OpenSearch)."""
        write = _query("w", "UPDATE topics SET views = views + 1 WHERE id = $1", qtype="UPDATE")
        ctx = _ctx(("opensearch", "aurora_postgresql"), LOOKUP, write)
        assert ctx.cost_score(LOOKUP, "opensearch") == _expected_score(
            catalog.pricing("opensearch").base_monthly, "opensearch"
        )
        assert ctx.cost_score(LOOKUP, "aurora_postgresql") == _expected_score(
            catalog.pricing("aurora_postgresql").base_monthly / 2, "aurora_postgresql"
        )

    def test_a_cache_shares_its_standing_cost_over_the_reads_it_could_front(self):
        """A cache owns no query, but it serves the reads it fronts: they share its
        standing cost, its writes do not."""
        two = _query("two", "SELECT * FROM topics WHERE id = $2")
        write = _query("w", "UPDATE topics SET views = 1 WHERE id = $1", qtype="UPDATE")
        ctx = _ctx(("elasticache",), LOOKUP, two, write)
        assert ctx.servable_queries("elasticache") == frozenset({"lookup", "two"})

    def test_a_query_an_engine_is_gated_from_does_not_share_its_standing_cost(self):
        """A required capability the engine lacks gates it (Decision 2 step 2): the
        gated query is not one of the queries the engine could serve."""
        needs = _query("needs", "SELECT * FROM topics WHERE id = $3")
        triage = {"query_capabilities": {"needs": ["complex_joins"]}, "signals": []}
        gated = _ctx(("documentdb",), LOOKUP, needs, triage=triage)
        assert score_query_for_engine(needs, "documentdb", gated).score is None
        assert gated.servable_queries("documentdb") == frozenset({"lookup"})
        base = catalog.pricing("documentdb").base_monthly
        assert gated.cost_score(LOOKUP, "documentdb") == _expected_score(base, "documentdb")
        served = _ctx(("documentdb",), LOOKUP, needs, triage={"query_capabilities": {}})
        assert served.servable_queries("documentdb") == frozenset({"lookup", "needs"})
        assert served.cost_score(LOOKUP, "documentdb") == _expected_score(base / 2, "documentdb")

    def test_a_model_flag_does_not_change_how_the_standing_cost_is_shared(self):
        """A model's flag gates one query on one engine (Decision 11 Amendment 2), but
        a run without a model must give the same scores to every other query."""
        two = _query("two", "SELECT * FROM topics WHERE id = $2")
        ctx = _ctx(("aurora_postgresql", "dynamodb"), LOOKUP, two)
        # A context of its own, so it counts the servable queries itself.
        flagged = _ctx(("aurora_postgresql", "dynamodb"), LOOKUP, two).with_flag_gaps(
            {"two": ["aurora_postgresql"]}
        )
        assert flagged.servable_queries("aurora_postgresql") == frozenset({"lookup", "two"})
        assert flagged.cost_score(LOOKUP, "aurora_postgresql") == ctx.cost_score(
            LOOKUP, "aurora_postgresql"
        )
        assert score_query_for_engine(LOOKUP, "aurora_postgresql", flagged).score == (
            score_query_for_engine(LOOKUP, "aurora_postgresql", ctx).score
        )


# ---------------------------------------------------------------------------
# The variable cost: the query's own call rate and size, at the engine file's prices
# ---------------------------------------------------------------------------


class TestTheQuerysOwnTrafficDrivesItsVariableCost:
    def test_a_request_priced_read_costs_its_read_units(self):
        """1,000 calls a second, each returning 10 rows of 1 KiB: 10 KiB, 3 units of
        4 KiB a call on DynamoDB, at its read unit price per million."""
        hot = _query("hot", "SELECT * FROM topics WHERE category_id = $1", cps=1000, rows=10)
        model = QueryCostModel.from_collector(_collector(hot))
        prices = catalog.pricing("dynamodb")
        units = math.ceil(10 * 1024 / prices.read_unit_bytes)
        assert units == 3
        expected = 1000 * SECONDS_PER_MONTH * units * prices.read_unit_price_per_million / 1e6
        assert model.variable_monthly(hot, "dynamodb") == pytest.approx(expected)

    def test_a_request_priced_write_costs_its_write_units(self):
        write = _query(
            "w", "UPDATE topics SET views = $1 WHERE id = $2", qtype="UPDATE", cps=10, rows=1
        )
        model = QueryCostModel.from_collector(_collector(write))
        prices = catalog.pricing("dynamodb")
        units = math.ceil(1024 / prices.write_unit_bytes)
        expected = 10 * SECONDS_PER_MONTH * units * prices.write_unit_price_per_million / 1e6
        assert model.variable_monthly(write, "dynamodb") == pytest.approx(expected)

    def test_a_call_that_returns_nothing_still_costs_one_unit(self):
        empty = _query("e", "SELECT * FROM topics WHERE id = $1", cps=1, rows=0)
        model = QueryCostModel.from_collector(_collector(empty))
        prices = catalog.pricing("dynamodb")
        assert model.variable_monthly(empty, "dynamodb") == pytest.approx(
            SECONDS_PER_MONTH * prices.read_unit_price_per_million / 1e6
        )

    def test_reads_billed_on_a_cache_miss_only_pay_for_the_misses(self):
        """DocumentDB bills a read I/O only when the page is not in its buffer cache;
        the source's own cache hit ratio is the estimate of that share."""
        hit = _query("h", "SELECT * FROM topics WHERE id = $1", cps=100, cache_hit_ratio_pct=90.0)
        cold = _query("c", "SELECT * FROM topics WHERE id = $1", cps=100)
        model = QueryCostModel.from_collector(_collector(hit, cold))
        assert catalog.pricing("documentdb").reads_billed_on_cache_miss_only
        assert model.variable_monthly(hit, "documentdb") == pytest.approx(
            0.1 * model.variable_monthly(cold, "documentdb")
        )
        assert not catalog.pricing("dynamodb").reads_billed_on_cache_miss_only
        assert model.variable_monthly(hit, "dynamodb") == pytest.approx(
            model.variable_monthly(cold, "dynamodb")
        )

    @pytest.mark.parametrize("engine", ["aurora_postgresql", "aurora_mysql", "opensearch"])
    def test_a_capacity_priced_engine_has_no_per_request_cost(self, engine):
        """Aurora I/O-Optimized and an OpenSearch domain bill capacity, not requests:
        their standing cost is the capacity, so a query's traffic adds nothing here."""
        hot = _query("hot", "SELECT * FROM topics WHERE id = $1", cps=1000, rows=10)
        model = QueryCostModel.from_collector(_collector(hot))
        assert model.variable_monthly(hot, engine) == 0.0

    def test_a_hot_query_costs_a_request_priced_engine_more_than_a_cluster(self):
        """At 1,000 calls a second a request-priced engine pays far more for the query
        than its share of a cluster that already has the capacity."""
        hot = _query("hot", "SELECT * FROM topics WHERE id = $1", cps=1000, rows=1)
        ctx = _ctx(("aurora_postgresql", "dynamodb"), hot)
        assert ctx.cost_score(hot, "dynamodb") < ctx.cost_score(hot, "aurora_postgresql")
        cold = _query("hot", "SELECT * FROM topics WHERE id = $1", cps=0.001, rows=1)
        ctx = _ctx(("aurora_postgresql", "dynamodb"), cold)
        assert ctx.cost_score(cold, "dynamodb") > ctx.cost_score(cold, "aurora_postgresql")

    def test_the_cost_score_falls_as_the_monthly_cost_rises(self):
        model = QueryCostModel(source_engine="")
        scores = [model.score_for_monthly(c, "dynamodb") for c in (0, 1, 10, 100, 300, 1000)]
        assert scores == sorted(scores, reverse=True)
        assert scores[0] == 100
        assert model.score_for_monthly(100, "dynamodb") == 50  # the per-query cut-off


# ---------------------------------------------------------------------------
# The scorer: the cost dimension is the query's own; the weights are unchanged
# ---------------------------------------------------------------------------


class TestAnEqualCostEarnsEqualCostPoints:
    """ADR-031 Decision 5 (#1002): every engine weights cost the same, so two engines
    whose cost score for a query is equal get the same points for it from the scorer."""

    @pytest.mark.parametrize("cost", [0, 50, 96, 100])
    def test_two_engines_with_the_same_cost_get_the_same_cost_points(self, cost, monkeypatch):
        from src.agents.referee import query_scoring

        ctx = _ctx(SOR_ENGINES, LOOKUP)
        monkeypatch.setattr(ScoringContext, "cost_score", lambda self, query, engine: cost)
        points: dict[str, set[float]] = {}
        real = query_scoring.compute_confidence

        def spy(scores, weights=None):
            points.setdefault(current, set()).add(scores.cost_score * weights["cost"])
            return real(scores, weights)

        monkeypatch.setattr(query_scoring, "compute_confidence", spy)
        for current in SOR_ENGINES:
            score_query_for_engine(LOOKUP, current, ctx)
        assert set(points) == set(SOR_ENGINES)
        assert len(set().union(*points.values())) == 1, points

    def test_the_engines_weight_sets_still_differ_on_the_other_dimensions(self):
        """The guard above is not vacuous: the engines' own weight sets still differ on the
        other three dimensions."""
        assert len({tuple(sorted(weight_profiles(e)[0].items())) for e in SOR_ENGINES}) > 1


# DynamoDB's and DocumentDB's weights before #1002, as a saved job's analysis used them.
PRE_1002_WEIGHTS = {
    "dynamodb": {"pattern_match": 0.60, "complexity": 0.20, "performance": 0.05, "cost": 0.15},
    "documentdb": {"pattern_match": 0.45, "complexity": 0.25, "performance": 0.15, "cost": 0.15},
}


class TestASavedPre1002AnalysisStillRoutes:
    """An ``analysis.json`` written before #1002 stored confidences computed with the old
    weights. It still loads and routes; routing applies the catalog's current weights to
    its stored dimensions, not the weights its stored confidence used."""

    @staticmethod
    def _saved(engine):
        breakdown = {
            "pattern_match_score": 80,
            "complexity_score": 80,
            "performance_score": 60,
            "cost_score": 70,
        }
        old = compute_confidence(ScoreBreakdown(**breakdown), PRE_1002_WEIGHTS[engine])
        recs = [
            {"table_id": t, "confidence_score": old, "score_breakdown": dict(breakdown)}
            for t in ("d.topics", "d.categories")
        ]
        return {
            "table_recommendations": recs,
            "workload_analysis": {"patterns_detected": [], "anti_patterns_detected": []},
        }

    @pytest.mark.parametrize("engine", sorted(PRE_1002_WEIGHTS))
    def test_it_is_scored_with_the_catalogs_current_weights(self, engine):
        ctx = ScoringContext.from_pipeline({engine: self._saved(engine)}, None, _collector(LOOKUP))
        assert ctx.pattern_model.weights[(engine, "d.topics")] == catalog.score_weights(engine)
        dims = {
            "pattern_match_score": ctx.pattern_model.pattern_score(LOOKUP, engine),
            "complexity_score": 80,
            "performance_score": 60,
            "cost_score": ctx.cost_score(LOOKUP, engine),
        }
        now = compute_confidence(ScoreBreakdown(**dims), catalog.score_weights(engine))
        then = compute_confidence(ScoreBreakdown(**dims), PRE_1002_WEIGHTS[engine])
        assert now != then, "the fixture must tell the two weight sets apart"
        assert score_query_for_engine(LOOKUP, engine, ctx).score == now

    def test_the_resolver_routes_it(self):
        analysis = {e: self._saved(e) for e in sorted(PRE_1002_WEIGHTS)}
        ctx = ScoringContext.from_pipeline(analysis, None, _collector(LOOKUP))
        best = max(
            sorted(PRE_1002_WEIGHTS), key=lambda e: score_query_for_engine(LOOKUP, e, ctx).score
        )
        triage = {
            "selected_agents": [{"agent_type": e} for e in sorted(PRE_1002_WEIGHTS)],
            "signals": [],
        }
        result = AssignmentResolver().resolve(triage, analysis, _collector(LOOKUP))
        (qa,) = result.query_assignments
        assert qa.assigned_engine == best


class TestTheScorerUsesTheQuerysCost:
    @pytest.mark.parametrize("engine", SOR_ENGINES)
    def test_the_cost_dimension_replaces_the_tables_cost_and_keeps_the_weights(self, engine):
        ctx = _ctx((engine,), LOOKUP)
        weights = dict(weight_profiles(engine)[0])
        expected = compute_confidence(
            ScoreBreakdown(
                pattern_match_score=ctx.pattern_model.pattern_score(LOOKUP, engine),
                complexity_score=80,
                performance_score=60,
                cost_score=ctx.cost_score(LOOKUP, engine),
            ),
            weights,
        )
        assert score_query_for_engine(LOOKUP, engine, ctx).score == expected

    @pytest.mark.parametrize("cost", [0, 35, 70, 100])
    def test_the_tables_cost_band_no_longer_reaches_a_classified_query(self, cost):
        collector = _collector(LOOKUP)
        scores = set()
        for table_cost in (0, cost):
            analysis = {"dynamodb": _analysis("dynamodb", "d.topics", cost=table_cost)}
            ctx = ScoringContext.from_pipeline(analysis, None, collector)
            scores.add(score_query_for_engine(LOOKUP, "dynamodb", ctx).score)
        assert len(scores) == 1

    def test_a_statement_with_no_class_keeps_its_tables_score(self):
        """``BEGIN`` has no access-pattern class: it keeps the pre-switch rule, its
        tables' confidence, cost band included (ADR-031 Decision 14 Amendment 5)."""
        begin = _query("b", "BEGIN", tables=("topics",), qtype="OTHER")
        engine = "aurora_postgresql"
        analysis = {engine: _analysis(engine, "d.topics", cost=0)}
        ctx = ScoringContext.from_pipeline(analysis, None, _collector(begin))
        assert score_query_for_engine(begin, engine, ctx).score == (
            analysis[engine]["table_recommendations"][0]["confidence_score"]
        )

    def test_the_cost_model_names_no_engine(self):
        from pathlib import Path

        import src.agents.referee.query_cost as module

        text = Path(module.__file__).read_text(encoding="utf-8").lower()
        for engine in catalog.engine_ids():
            assert engine not in text, engine
        for word in ("aurora", "dynamo", "documentdb", "opensearch", "elasticache", "valkey"):
            assert word not in text, word


# ---------------------------------------------------------------------------
# Property: one query's cost never depends on another query's pattern
# ---------------------------------------------------------------------------

# Shapes no system-of-record engine is gated from: every class but text search.
OTHER_SHAPES = [
    ("SELECT * FROM topics WHERE id = $1", ("topics",), "SELECT"),
    ("SELECT * FROM topics WHERE category_id = $1", ("topics",), "SELECT"),
    ("SELECT * FROM topics WHERE category_id = $1 AND id > $2", ("topics",), "SELECT"),
    ("SELECT * FROM topics ORDER BY id LIMIT $1", ("topics",), "SELECT"),
    ("SELECT * FROM topics WHERE title = $1 AND views > $2", ("topics",), "SELECT"),
    ("SELECT category_id, count(*) FROM topics GROUP BY category_id", ("topics",), "SELECT"),
    (
        "SELECT t.id FROM topics t JOIN categories c ON c.position = t.views",
        ("topics", "categories"),
        "SELECT",
    ),
    ("INSERT INTO topics (id, title) VALUES ($1, $2)", ("topics",), "INSERT"),
    ("UPDATE topics SET views = views + 1 WHERE id = $1", ("topics",), "UPDATE"),
    ("DELETE FROM topics WHERE views < $1", ("topics",), "DELETE"),
]

_other = st.tuples(
    st.integers(min_value=0, max_value=len(OTHER_SHAPES) - 1),
    st.floats(min_value=0, max_value=5000, allow_nan=False),
    st.floats(min_value=0, max_value=1000, allow_nan=False),
)


@settings(max_examples=60, deadline=None)
@given(
    st.lists(_other, min_size=1, max_size=4).flatmap(
        lambda first: st.tuples(
            st.just(first), st.lists(_other, min_size=len(first), max_size=len(first))
        )
    ),
    st.floats(min_value=0, max_value=2000, allow_nan=False),
)
def test_a_querys_cost_never_depends_on_another_querys_pattern(others, own_cps):
    """Two workloads with the same number of other queries, whose patterns, call
    rates and sizes all differ: the query's cost dimension, and its score, are the
    same on every system-of-record engine.

    Each table's cost band is computed from the workload, as the analyses compute it
    (``build_table_profiles`` and ``compute_base_scores``: it sums the traffic of every
    query on the table), with complexity and performance held fixed. Before #494 that
    band was the query's cost dimension, so another query's traffic moved this one's
    score."""
    first, second = others
    query = _query("me", "SELECT * FROM topics WHERE id = $1", cps=own_cps)

    def workload(spec):
        return [
            _query(f"o{n}", OTHER_SHAPES[i][0], OTHER_SHAPES[i][1], OTHER_SHAPES[i][2], cps, rows)
            for n, (i, cps, rows) in enumerate(spec)
        ]

    def ctx(queries):
        collector = _collector(query, *queries)
        profiles = build_table_profiles(collector, WorkloadAnalysis(patterns_detected=[]))
        analysis = {
            e: {
                "table_recommendations": [
                    _rec(e, t, cost=compute_base_scores(profile).cost_score)
                    for t, profile in profiles.items()
                ],
                "workload_analysis": {"patterns_detected": [], "anti_patterns_detected": []},
            }
            for e in SOR_ENGINES
        }
        return ScoringContext.from_pipeline(analysis, None, collector)

    a = ctx(workload(first))
    b = ctx(workload(second))
    for engine in SOR_ENGINES:
        assert a.cost_score(query, engine) == b.cost_score(query, engine), engine
        assert (
            score_query_for_engine(query, engine, a).score
            == score_query_for_engine(query, engine, b).score
        ), engine
