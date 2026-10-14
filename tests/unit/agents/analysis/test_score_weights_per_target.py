"""ADR-017 Decision 4: score weights are per-target configurable (#703).

The rule, not the table of numbers (the numbers in the ADR are stale, #701): each target's
analysis scores its tables with the weights its own catalog defines, ``compute_confidence``
accepts such a dict, and without one it falls back to the 40/30/20/10 default.
"""

from __future__ import annotations

import pytest

from src.agents.analysis.aurora_mysql_analysis_agent import analyze_for_aurora_mysql
from src.agents.analysis.aurora_pg_analysis_agent import analyze_for_aurora_pg
from src.agents.analysis.documentdb_analysis_agent import analyze_for_documentdb
from src.agents.analysis.dynamodb_analysis_agent import analyze_for_dynamodb
from src.contracts.analysis_input import AnalysisInput, TargetDatabase
from src.contracts.analysis_output import ScoreBreakdown
from src.tools.analysis import (
    aurora_mysql_analysis_tools,
    aurora_pg_analysis_tools,
    documentdb_analysis_tools,
    dynamodb_analysis_tools,
)
from src.tools.analysis.aurora_common_pattern_catalog import (
    AURORA_MYSQL_SCORE_WEIGHTS,
    AURORA_PG_SCORE_WEIGHTS,
)
from src.tools.analysis.documentdb_pattern_catalog import DOCUMENTDB_SCORE_WEIGHTS
from src.tools.analysis.dynamodb_pattern_catalog import DYNAMODB_SCORE_WEIGHTS
from src.tools.analysis.scoring import DEFAULT_WEIGHTS, compute_confidence
from tests.fixtures.ecommerce_collector_output import get_ecommerce_collector_output

SCORES = ScoreBreakdown(
    pattern_match_score=100, complexity_score=0, performance_score=0, cost_score=0
)

TARGETS = {
    "dynamodb": (analyze_for_dynamodb, TargetDatabase.dynamodb, DYNAMODB_SCORE_WEIGHTS),
    "documentdb": (analyze_for_documentdb, TargetDatabase.documentdb, DOCUMENTDB_SCORE_WEIGHTS),
    "aurora_postgresql": (
        analyze_for_aurora_pg,
        TargetDatabase.aurora_postgresql,
        AURORA_PG_SCORE_WEIGHTS,
    ),
    "aurora_mysql": (
        analyze_for_aurora_mysql,
        TargetDatabase.aurora_mysql,
        AURORA_MYSQL_SCORE_WEIGHTS,
    ),
}


def test_the_default_when_no_weights_are_given_is_40_30_20_10() -> None:
    assert DEFAULT_WEIGHTS == {
        "pattern_match": 0.4,
        "complexity": 0.3,
        "performance": 0.2,
        "cost": 0.1,
    }
    assert compute_confidence(SCORES) == compute_confidence(SCORES, DEFAULT_WEIGHTS) == 40


def test_a_weights_dict_replaces_the_default() -> None:
    pattern_heavy = {"pattern_match": 0.6, "complexity": 0.2, "performance": 0.05, "cost": 0.15}
    assert compute_confidence(SCORES, weights=pattern_heavy) == 60


TOOLS = {
    "dynamodb": dynamodb_analysis_tools,
    "documentdb": documentdb_analysis_tools,
    "aurora_postgresql": aurora_pg_analysis_tools,
    "aurora_mysql": aurora_mysql_analysis_tools,
}


@pytest.mark.parametrize("target", sorted(TARGETS))
def test_a_target_scores_its_tables_with_its_own_catalogs_weights(
    target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spy on what each analysis hands the scorer: it must be that catalog's own object.

    Aurora's numbers equal the default, so equality could not tell "its own weights" from
    "no weights"; identity can (a call with ``None`` or ``DEFAULT_WEIGHTS`` fails).
    """
    analyze, target_database, catalog_weights = TARGETS[target]
    passed: list[dict[str, float] | None] = []

    def spy(scores: ScoreBreakdown, weights: dict[str, float] | None = None) -> int:
        passed.append(weights)
        return compute_confidence(scores, weights)

    monkeypatch.setattr(TOOLS[target], "compute_confidence", spy)
    _contract, trace, _diagram = analyze(
        AnalysisInput(
            job_id="t",
            collector_output=get_ecommerce_collector_output(),
            target_database=target_database,
        ),
        llm_mode="none",
    )

    assert passed, f"{target}: no table was scored"
    assert all(w is catalog_weights for w in passed), target
    assert catalog_weights is not DEFAULT_WEIGHTS
    # The trace reports the weights that were really used, and its confidence follows them.
    for d in trace["recommendation_derivations"]:
        assert d["weights_used"] is catalog_weights or d["weights_used"] == catalog_weights
        breakdown = ScoreBreakdown(
            pattern_match_score=d["score_breakdown"]["pattern_match"],
            complexity_score=d["score_breakdown"]["complexity"],
            performance_score=d["score_breakdown"]["performance"],
            cost_score=d["score_breakdown"]["cost"],
        )
        assert d["weighted_confidence"] == compute_confidence(breakdown, passed[0])


def test_targets_are_free_to_weight_differently_from_the_default() -> None:
    """Per-target weights are real configuration: at least one target departs from the default."""
    assert any(w != DEFAULT_WEIGHTS for _, _, w in TARGETS.values())
    assert DYNAMODB_SCORE_WEIGHTS != DEFAULT_WEIGHTS
