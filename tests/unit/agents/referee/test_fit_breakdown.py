"""The routing scorer's own account of a score (#1603, ``explain=True``).

``score_query_for_engine(..., explain=True)`` returns the arithmetic it ran, as a fit
breakdown, and never another number: the score and gate are the plain call's, on every
statement and engine of the samples. ``apply_gates=False`` is the score past the gates
(the resolver's guarantee for a homogeneous source's own Aurora engine). The penalty the
breakdown lists is the one the anti-pattern map applies (its largest points), and the
pattern class is the one whose score the pattern dimension reads.
"""

from __future__ import annotations

from functools import cache
from typing import Any

import pytest

from src.agents.referee.query_scoring import (
    BREAKDOWN_FIELDS,
    EngineScore,
    ScoringContext,
    compute_query_confidence,
    score_query_for_engine,
)
from src.shared.engine_names import without_placeholder_engines
from src.tools.analysis.scoring import (
    DEFAULT_WEIGHTS,
    compute_confidence,
    effective_weights,
    weighted_confidence,
)
from src.tools.sql.query_facts import remember_collected_facts
from tests.unit.assessment.conftest import SAMPLES, placed_run


@cache
def _context(sample: str) -> tuple[ScoringContext, list[dict[str, Any]], list[str]]:
    _, state, _ = placed_run(sample)
    analyses = without_placeholder_engines(state.analyses)
    remember_collected_facts(state.collector)
    ctx = ScoringContext.from_pipeline(analyses, state.triage, state.collector)
    return ctx, state.query_patterns, list(analyses)


@pytest.mark.parametrize("sample", SAMPLES)
def test_explaining_a_score_never_changes_it(sample: str) -> None:
    ctx, queries, engines = _context(sample)
    for query in queries:
        for engine in engines:
            plain = score_query_for_engine(query, engine, ctx)
            explained = score_query_for_engine(query, engine, ctx, explain=True)
            assert explained == plain, (query["query_id"], engine)
            assert plain.breakdown is None
            breakdown = explained.breakdown
            assert breakdown is not None and tuple(breakdown) == BREAKDOWN_FIELDS
            if plain.score is None:
                assert breakdown["gate"] == {
                    "kind": plain.gate_kind,
                    "reason": plain.gate_reason,
                }
                assert breakdown["fit"] is None and breakdown["tables"] == []
            else:
                assert breakdown["gate"] is None
                assert breakdown["fit"] == int(plain.score)


@pytest.mark.parametrize("sample", SAMPLES)
def test_the_listed_penalty_is_the_one_applied(sample: str) -> None:
    """The largest points among a pair's findings is the anti-pattern map's penalty for
    it: the breakdown lists exactly what the score lost."""
    ctx, queries, engines = _context(sample)
    seen = 0
    for query in queries:
        for engine in engines:
            result = score_query_for_engine(query, engine, ctx, explain=True, apply_gates=False)
            assert result.breakdown is not None
            rows = result.breakdown["penalties"]
            applied = ctx.anti_pattern_map.get((query["query_id"], engine), 0)
            assert max((r["points"] for r in rows), default=0) == applied, (
                query["query_id"],
                engine,
            )
            seen += bool(rows)
    assert seen


@pytest.mark.parametrize("sample", SAMPLES)
def test_past_the_gates_the_score_is_base_less_penalty(sample: str) -> None:
    """``apply_gates=False`` is the resolver's own arithmetic for an engine whose gate it
    lifts (``compute_query_confidence`` less the penalty, at least 0)."""
    ctx, queries, engines = _context(sample)
    gated = 0
    for query in queries:
        for engine in engines:
            gated += score_query_for_engine(query, engine, ctx).score is None
            result = score_query_for_engine(query, engine, ctx, apply_gates=False)
            base = compute_query_confidence(
                query["query_id"],
                query,
                engine,
                ctx.analysis_outputs[engine],
                pattern_model=ctx.pattern_model,
                cost_score=ctx.cost_score(query, engine),
            )
            penalty = ctx.anti_pattern_map.get((query["query_id"], engine), 0)
            assert result.score == float(max(0, base - penalty))
    assert gated


@pytest.mark.parametrize("sample", SAMPLES)
def test_the_trace_leaves_the_base_score_as_it_is(sample: str) -> None:
    ctx, queries, engines = _context(sample)
    for query in queries:
        for engine in engines:
            for exact in (False, True):
                kwargs: dict[str, Any] = {
                    "pattern_model": ctx.pattern_model,
                    "cost_score": ctx.cost_score(query, engine),
                    "exact": exact,
                }
                args = (query["query_id"], query, engine, ctx.analysis_outputs[engine])
                trace: dict[str, Any] = {}
                assert compute_query_confidence(*args, **kwargs) == compute_query_confidence(
                    *args, **kwargs, trace=trace
                )
                assert trace["base"] == compute_query_confidence(*args, **kwargs)


@pytest.mark.parametrize("sample", SAMPLES)
def test_the_pattern_class_is_the_one_scored(sample: str) -> None:
    """The class a breakdown names is the redesigned one exactly where routing relies on
    the redesign, and its score is then the redesigned class's."""
    ctx, queries, engines = _context(sample)
    model = ctx.pattern_model
    for query in queries:
        if not model.has_class_score(query):
            continue
        for engine in engines:
            name = model.pattern_class(query, engine)
            if model.relies_on_redesign(query, engine):
                found = model.redesign(query)
                assert found is not None and name == found.redesigned.name
                assert model.pattern_score(query, engine) == model.redesigned_pattern_score(
                    query, engine
                )
            else:
                assert name == model.classification(query).name


def test_effective_weights_are_the_ones_weighted_confidence_uses() -> None:
    from src.contracts.analysis_output import ScoreBreakdown

    assert effective_weights(None) == effective_weights({}) == dict(DEFAULT_WEIGHTS)
    assert effective_weights({"pattern_match": 0.7}) == {
        "pattern_match": 0.7,
        "complexity": 0.3,
        "performance": 0.2,
        "cost": 0.1,
    }
    score = ScoreBreakdown(
        pattern_match_score=77, complexity_score=33, performance_score=91, cost_score=12
    )
    for weights in ({"pattern_match": 0.64, "complexity": 0.21, "performance": 0.05}, None):
        w = effective_weights(weights)
        by_hand = 77 * w["pattern_match"] + 33 * w["complexity"] + 91 * w["performance"]
        assert weighted_confidence(score, weights) == by_hand + 12 * w["cost"]
        assert compute_confidence(score, weights) == int(by_hand + 12 * w["cost"])


def test_a_breakdown_is_never_compared() -> None:
    assert EngineScore(score=1.0, breakdown={"fit": 1}) == EngineScore(score=1.0)
