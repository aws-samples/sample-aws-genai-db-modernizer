"""ADR-031 Decision 5: cost is one of four weighted score dimensions (#638).

The decision states that a score has exactly four dimensions (pattern match,
complexity, performance, cost) at a 40/30/20/10 default split, that some
engines weight cost differently, and that the per-table confidence built from
them is what a query's score rests on. These tests assert that behaviour, not
the shape of any one function: change the dimension set or the default weights
and they fail until the ADR is amended too.
"""

from __future__ import annotations

import pytest

from src.agents.analysis.aurora_mysql_analysis_agent import analyze_for_aurora_mysql
from src.agents.analysis.aurora_pg_analysis_agent import analyze_for_aurora_pg
from src.agents.analysis.documentdb_analysis_agent import analyze_for_documentdb
from src.agents.analysis.dynamodb_analysis_agent import analyze_for_dynamodb
from src.agents.analysis.elasticache_analysis_agent import analyze_for_elasticache
from src.agents.analysis.opensearch_analysis_agent import analyze_for_opensearch
from src.agents.referee.query_scoring import ScoringContext, score_query_for_engine
from src.contracts.analysis_input import AnalysisInput, TargetDatabase
from src.contracts.analysis_output import ScoreBreakdown
from src.tools.analysis import (
    aurora_mysql_analysis_tools,
    aurora_pg_analysis_tools,
    documentdb_analysis_tools,
    dynamodb_analysis_tools,
    opensearch_analysis_tools,
    redis_analysis_tools,
)
from src.tools.analysis.aurora_common_pattern_catalog import (
    AURORA_MYSQL_SCORE_WEIGHTS,
    AURORA_PG_SCORE_WEIGHTS,
)
from src.tools.analysis.documentdb_pattern_catalog import DOCUMENTDB_SCORE_WEIGHTS
from src.tools.analysis.dynamodb_pattern_catalog import DYNAMODB_SCORE_WEIGHTS
from src.tools.analysis.opensearch_pattern_catalog import (
    OPENSEARCH_SEARCH_WEIGHTS,
    OPENSEARCH_TIMESERIES_WEIGHTS,
)
from src.tools.analysis.scoring import DEFAULT_WEIGHTS, compute_confidence
from tests.fixtures.ecommerce_collector_output import get_ecommerce_collector_output
from tests.fixtures.opensearch_pattern_fixtures import get_fulltext_search_fixture

DIMENSIONS = {"pattern_match", "complexity", "performance", "cost"}
DEFAULT_SPLIT = {"pattern_match": 40, "complexity": 30, "performance": 20, "cost": 10}

ENGINE_WEIGHTS = {
    "aurora_postgresql": AURORA_PG_SCORE_WEIGHTS,
    "aurora_mysql": AURORA_MYSQL_SCORE_WEIGHTS,
    "dynamodb": DYNAMODB_SCORE_WEIGHTS,
    "documentdb": DOCUMENTDB_SCORE_WEIGHTS,
    "opensearch-search": OPENSEARCH_SEARCH_WEIGHTS,
    "opensearch-timeseries": OPENSEARCH_TIMESERIES_WEIGHTS,
}


def _only(dimension: str, value: int = 100) -> ScoreBreakdown:
    fields = {f"{d}_score": 0 for d in DIMENSIONS}
    fields[f"{dimension}_score"] = value
    return ScoreBreakdown(**fields)


def test_a_score_has_exactly_the_four_decision_dimensions() -> None:
    assert set(ScoreBreakdown.model_fields) == {f"{d}_score" for d in DIMENSIONS}
    assert set(DEFAULT_WEIGHTS) == DIMENSIONS


@pytest.mark.parametrize("dimension", sorted(DIMENSIONS))
def test_each_dimension_contributes_its_default_share(dimension: str) -> None:
    """A perfect score on one dimension alone is worth exactly its share of 100."""
    assert compute_confidence(_only(dimension)) == DEFAULT_SPLIT[dimension]


def test_default_split_is_40_30_20_10_and_adds_up_to_a_whole_score() -> None:
    perfect = ScoreBreakdown(
        pattern_match_score=100, complexity_score=100, performance_score=100, cost_score=100
    )
    assert compute_confidence(perfect) == 100


def test_cost_moves_the_default_score_by_ten() -> None:
    base = ScoreBreakdown(
        pattern_match_score=50, complexity_score=50, performance_score=50, cost_score=0
    )
    with_cost = base.model_copy(update={"cost_score": 100})
    assert compute_confidence(with_cost) - compute_confidence(base) == 10


def test_an_engine_may_weight_cost_differently_but_keeps_the_same_four_dimensions() -> None:
    """The decision allows per-engine weights; each still covers the four and sums to 1."""
    for name, weights in ENGINE_WEIGHTS.items():
        assert set(weights) == DIMENSIONS, name
        assert sum(weights.values()) == pytest.approx(1.0), name
    assert any(w["cost"] != DEFAULT_WEIGHTS["cost"] for w in ENGINE_WEIGHTS.values())


def test_explicit_weights_change_what_cost_is_worth() -> None:
    cost_heavy = {"pattern_match": 0.1, "complexity": 0.1, "performance": 0.1, "cost": 0.7}
    assert compute_confidence(_only("cost"), weights=cost_heavy) == 70
    assert compute_confidence(_only("cost")) == 10


# The weights each engine's analysis actually passes when it scores a table.
ENGINE_ANALYSES = {
    "aurora_postgresql": (
        aurora_pg_analysis_tools,
        analyze_for_aurora_pg,
        TargetDatabase.AURORA_POSTGRESQL,
    ),
    "aurora_mysql": (
        aurora_mysql_analysis_tools,
        analyze_for_aurora_mysql,
        TargetDatabase.AURORA_MYSQL,
    ),
    "dynamodb": (dynamodb_analysis_tools, analyze_for_dynamodb, TargetDatabase.DYNAMODB),
    "documentdb": (documentdb_analysis_tools, analyze_for_documentdb, TargetDatabase.DOCUMENTDB),
    "opensearch": (opensearch_analysis_tools, analyze_for_opensearch, TargetDatabase.OPENSEARCH),
    "elasticache": (
        redis_analysis_tools,
        analyze_for_elasticache,
        TargetDatabase.ELASTICACHE,
    ),
}


def _collector_for(engine: str) -> dict:
    return (
        get_fulltext_search_fixture()
        if engine == "opensearch"
        else get_ecommerce_collector_output()
    )


@pytest.mark.parametrize("engine", sorted(ENGINE_ANALYSES))
def test_every_engines_table_scoring_weights_all_four_dimensions_including_cost(
    engine: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run the engine's real analysis and look at the weights it hands to the scorer."""
    module, analyze, target = ENGINE_ANALYSES[engine]
    seen: list[tuple[ScoreBreakdown, dict[str, float]]] = []

    def spy(scores: ScoreBreakdown, weights: dict[str, float] | None = None) -> int:
        seen.append((scores, weights or DEFAULT_WEIGHTS))
        return compute_confidence(scores, weights)

    monkeypatch.setattr(module, "compute_confidence", spy)
    collector = _collector_for(engine)
    analyze(AnalysisInput(job_id="t", collector_output=collector, target_database=target))

    assert seen, f"{engine}: no table was scored through compute_confidence"
    for _scores, weights in seen:
        assert set(weights) == DIMENSIONS, (engine, weights)
        assert weights["cost"] > 0, (engine, weights)
        assert sum(weights.values()) == pytest.approx(1.0), (engine, weights)
        assert any(weights[d] for d in DIMENSIONS if d != "cost")


def test_a_querys_score_is_the_confidence_the_engines_analysis_gave_its_tables() -> None:
    """Cost reaches a query through the table confidence; the query score adds nothing of its own."""
    collector = get_ecommerce_collector_output()
    analysis = analyze_for_aurora_pg(
        AnalysisInput(
            job_id="t", collector_output=collector, target_database=TargetDatabase.AURORA_POSTGRESQL
        )
    )[0].model_dump()
    recs = {r["table_id"]: r["confidence_score"] for r in analysis["table_recommendations"]}
    tables = sorted(recs)[:2]
    assert len(tables) == 2
    ctx = ScoringContext(analysis_outputs={"aurora_postgresql": analysis})
    query = {"query_id": "q1", "query_text": "SELECT 1", "tables_accessed": tables}
    got = score_query_for_engine(query, "aurora_postgresql", ctx).score
    assert got == int(sum(recs[t] for t in tables) / len(tables))
