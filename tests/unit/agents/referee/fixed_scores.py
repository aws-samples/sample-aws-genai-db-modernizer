"""Test helper: give a query a fixed score on an engine through the per-query scorer (#728).

Since the access-pattern switch (ADR-031 Decision 14 Amendment 5) a classified query's
pattern dimension is its own class score, so a table row's ``confidence_score`` no
longer decides a query's score. A test about some other rule (a model flag, a tie, a
gate, the reality check) that wants "query ``q`` scores ``S`` on engine ``e``" builds
the rows and the model here: each row carries ``S`` in all four dimensions, and
:class:`FixedPatternModel` gives the query the pattern score ``S`` too, so the shared
scorer's arithmetic returns exactly ``S`` with any weights. Production code never
builds one (``test_per_query_pattern_score.py``'s guard covers ``src/`` and
``scripts/``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import pytest

from src.agents.referee.query_cost import QueryCostModel
from src.agents.referee.query_pattern import QueryPatternModel


@dataclass(frozen=True)
class FixedPatternModel(QueryPatternModel):
    """``scores[(query_id, engine)]`` is that query's pattern score on that engine; a
    pair it does not list keeps the real class model's answer."""

    scores: Mapping[tuple[str, str], int] = field(default_factory=dict)

    def has_class_score(self, query: Mapping) -> bool:
        qid = str(query.get("query_id") or "")
        return any(q == qid for q, _ in self.scores) or super().has_class_score(query)

    def pattern_score(self, query: Mapping, engine: str) -> int:
        key = (str(query.get("query_id") or ""), engine)
        if key in self.scores:
            return int(self.scores[key])
        return super().pattern_score(query, engine)


@dataclass(frozen=True)
class FixedCostModel(QueryCostModel):
    """``scores[(query_id, engine)]`` is that query's cost score on that engine; a pair
    it does not list keeps the real cost model's answer."""

    scores: Mapping[tuple[str, str], int] = field(default_factory=dict)

    def score(self, query: Mapping, engine: str, servable: frozenset[str]) -> int:
        key = (str(query.get("query_id") or ""), engine)
        if key in self.scores:
            return int(self.scores[key])
        return super().score(query, engine, servable)


def flat_row(table_id: str, score: float) -> dict:
    """A table row whose four dimensions (and so its confidence) are all ``score``."""
    value = int(score)
    return {
        "table_id": table_id,
        "confidence_score": value,
        "score_breakdown": {
            "pattern_match_score": value,
            "complexity_score": value,
            "performance_score": value,
            "cost_score": value,
        },
    }


def fixed_model(scores: Mapping[str, Mapping[str, float]]) -> FixedPatternModel:
    """``scores[query_id][engine]`` as each query's pattern score."""
    return FixedPatternModel(
        scores={(qid, engine): int(s) for qid, per in scores.items() for engine, s in per.items()}
    )


def fixed_cost_model(scores: Mapping[str, Mapping[str, float]]) -> FixedCostModel:
    """``scores[query_id][engine]`` as each query's cost score."""
    return FixedCostModel(
        scores={(qid, engine): int(s) for qid, per in scores.items() for engine, s in per.items()},
        source_engine="",
    )


@pytest.fixture
def table_confidence_scores(monkeypatch: pytest.MonkeyPatch) -> None:
    """Score every query by its tables' ``confidence_score``, the pre-switch rule.

    For a test of another rule (a tie-break, the cache owner, a reality-check floor)
    that runs the pipeline's own entry points, which build their scoring context
    themselves, and engineers its scores per table: every query is scored as a
    statement with no access-pattern class is (``query_scoring._unclassified_confidence``),
    so the rule under test sees the scores the test wrote. The switch itself is tested
    in ``test_per_query_pattern_score.py`` and ``test_per_query_pattern_acceptance.py``.
    """
    monkeypatch.setattr(QueryPatternModel, "has_class_score", lambda self, query: False)
