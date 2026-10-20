"""Reality check Phase 1's extra-engine burden is engine-aware (#494 slice 2).

Before, an engine's burden was its standing cost plus the flat extra-engine burden,
whatever its queries cost to run there. Now it also carries the net variable cost of
its own queries: what they cost on the engine, at the engine file's per-request
prices, less what they would cost on the engine each would move to. A request-priced
engine serving a hot query is a heavier burden than its standing cost says; an engine
that serves its queries more cheaply than where they would go is a lighter one.
"""

from __future__ import annotations

import pytest

import src.agents.referee.reality_check as reality_check
from src.agents.referee.query_cost import SECONDS_PER_MONTH, QueryCostModel
from src.agents.referee.reality_check_rules import engine_base_cost, extra_engine_burden
from src.agents.referee.reality_check_settle import (
    _evaluate_engine_removal,
    _settle_committed_engines,
)
from src.shared import catalog


def _query(qid, cps):
    return {
        "query_id": qid,
        "query_text": f"SELECT * FROM t WHERE id = {qid!r}",
        "query_type": "SELECT",
        "tables_accessed": ["d.t"],
        "calls_per_second": cps,
        "rows_returned_avg": 1.0,
    }


def _read_cost(cps: float) -> float:
    """``cps`` one-unit reads a second for a month, at DynamoDB's read unit price."""
    return cps * SECONDS_PER_MONTH * catalog.pricing("dynamodb").read_unit_price_per_million / 1e6


def _evaluate(engine, alt, cps, *, cost_model, engine_score=90, alt_score=70):
    q = _query("q1", cps)
    removable, info, rank_key = _evaluate_engine_removal(
        engine,
        [{"query_id": "q1"}],
        {alt, "x"},
        set(),
        {"q1": {"engines": {engine: engine_score, alt: alt_score}}},
        {"q1": engine},
        {},
        {"q1": q},
        {},
        3,
        cost_model=cost_model,
    )
    return removable, info, rank_key


MODEL = QueryCostModel(source_engine="")  # no table sizes: one unit a call


class TestTheBurdenCarriesTheNetVariableCost:
    def test_a_hot_query_on_a_request_priced_engine_adds_its_running_cost(self):
        removable, info, rank_key = _evaluate(
            "dynamodb", "aurora_postgresql", 1000.0, cost_model=MODEL
        )
        assert info["unique_queries"] == ["q1"]
        flat = engine_base_cost("dynamodb") + extra_engine_burden("dynamodb")
        assert rank_key == pytest.approx(flat + _read_cost(1000.0))
        assert removable  # $624 for one unique query is past the $100 cut-off

    def test_with_no_traffic_the_burden_is_the_flat_one(self):
        _, _, rank_key = _evaluate("dynamodb", "aurora_postgresql", 0.0, cost_model=MODEL)
        assert rank_key == engine_base_cost("dynamodb") + extra_engine_burden("dynamodb")

    def test_with_no_cost_model_the_burden_is_the_flat_one(self):
        _, _, rank_key = _evaluate("dynamodb", "aurora_postgresql", 1000.0, cost_model=None)
        assert rank_key == engine_base_cost("dynamodb") + extra_engine_burden("dynamodb")

    def test_an_engine_that_serves_its_query_more_cheaply_is_a_lighter_burden(self):
        """Aurora PostgreSQL serves the hot read inside its capacity; on DynamoDB the
        same read would cost $324 a month, so keeping Aurora PostgreSQL saves that."""
        _, _, rank_key = _evaluate("aurora_postgresql", "dynamodb", 1000.0, cost_model=MODEL)
        flat = engine_base_cost("aurora_postgresql") + extra_engine_burden("aurora_postgresql")
        assert rank_key == pytest.approx(max(0.0, flat - _read_cost(1000.0)))

    def test_an_engine_that_saves_more_than_it_costs_ranks_last_for_removal(self):
        """A redundant engine (5 points ahead, under the 15-point margin) is removable;
        when its query would cost more on the alternative than its whole burden, its
        rank is below 0, behind every engine whose burden is a cost."""
        removable, info, rank_key = _evaluate(
            "aurora_postgresql", "dynamodb", 2000.0, cost_model=MODEL, alt_score=85
        )
        assert removable and info["unique_queries"] == []
        flat = engine_base_cost("aurora_postgresql") + extra_engine_burden("aurora_postgresql")
        assert flat - _read_cost(2000.0) < 0
        assert rank_key == pytest.approx(flat - _read_cost(2000.0))


class TestTheEngineAwareBurdenDecidesWhichEngineGoes:
    """Three engines over the target of two, two of them removable with one unique
    query each. On the flat burden DocumentDB ($200 + $300) outranks DynamoDB ($0 +
    $300) and is removed. DynamoDB's one query is a hot read that costs it $324 a
    month more than on Aurora PostgreSQL, so DynamoDB is the heavier burden and goes."""

    def _settle(self, cost_model):
        queries = {
            "pg1": _query("pg1", 0.01),
            "pg2": _query("pg2", 0.01),
            "ddb": _query("ddb", 1000.0),
            "doc": _query("doc", 0.01),
        }
        engines = {"pg1": "aurora_postgresql", "pg2": "aurora_postgresql"}
        engines |= {"ddb": "dynamodb", "doc": "documentdb"}
        scores = {
            "pg1": {"aurora_postgresql": 90, "dynamodb": 60, "documentdb": 60},
            "pg2": {"aurora_postgresql": 90, "dynamodb": 60, "documentdb": 60},
            "ddb": {"aurora_postgresql": 70, "dynamodb": 90, "documentdb": 70},
            "doc": {"aurora_postgresql": 70, "dynamodb": 70, "documentdb": 90},
        }
        engine_queries: dict[str, list[dict]] = {}
        for qid, engine in engines.items():
            engine_queries.setdefault(engine, []).append({"query_id": qid})
        committed, _ = _settle_committed_engines(
            engine_queries,
            set(),
            set(),
            {qid: {"engines": s} for qid, s in scores.items()},
            engines,
            {},
            queries,
            {},
            5,
            None,
            "aurora_postgresql",
            {},
            cost_model=cost_model,
        )
        return committed

    def test_the_flat_burden_removes_documentdb(self):
        assert self._settle(None) == {"aurora_postgresql", "dynamodb"}

    def test_the_engine_aware_burden_removes_dynamodb(self):
        assert self._settle(MODEL) == {"aurora_postgresql", "documentdb"}


def test_the_reality_check_hands_phase_1_the_scoring_contexts_cost_model(monkeypatch):
    seen = {}
    real = reality_check._settle_committed_engines

    def spy(*args, **kwargs):
        seen["cost_model"] = kwargs.get("cost_model")
        return real(*args, **kwargs)

    monkeypatch.setattr(reality_check, "_settle_committed_engines", spy)
    collector = {
        "metadata": {"source_database": {"engine": "postgresql"}},
        "database_schema": {"tables": []},
        "queries": {"query_patterns": [_query("q1", 1.0)]},
    }
    assignment = {
        "query_assignments": [
            {"query_id": "q1", "assigned_engine": "aurora_postgresql", "assignment_reason": "t"}
        ]
    }
    analysis = {"aurora_postgresql": {"table_recommendations": []}}
    reality_check.run_reality_check(assignment, {}, analysis, collector)
    assert isinstance(seen["cost_model"], QueryCostModel)
    assert seen["cost_model"].workload_ids() == ("q1",)
