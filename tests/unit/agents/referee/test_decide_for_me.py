# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``--auto``'s "decide for me" rule for a pending engine choice (#1003).

Maintainer decision (2026-10-17): per table, in order, the higher unrounded
score; then the engine the plan already uses more (``settled_counts``); then the
existing tie_break order (source-compatible, then stability). No engine name is
an input.
"""

from __future__ import annotations

import pytest

from src.agents.referee.tie_break import (
    DECIDED_BY_SETTLED_WORKLOAD,
    DECIDED_BY_TIE_BREAK,
    DECIDED_BY_UNROUNDED_SCORE,
    TIE_KIND_SOURCE_COMPATIBLE,
    TIE_KIND_STABILITY,
    TIE_KIND_UNRESOLVED,
    decide_for_me,
)

TIED = ["documentdb", "dynamodb"]


def test_the_higher_unrounded_score_wins_whatever_the_workload() -> None:
    engine, rule, kind = decide_for_me(
        TIED,
        {"documentdb": 78.20, "dynamodb": 78.15},
        {"dynamodb"},
        None,
        {"dynamodb": 641, "documentdb": 182},
        "discourse.incoming_emails",
    )
    assert (engine, rule, kind) == ("documentdb", DECIDED_BY_UNROUNDED_SCORE, None)


def test_level_before_rounding_the_engine_the_plan_uses_more_wins() -> None:
    engine, rule, kind = decide_for_me(
        TIED,
        {"documentdb": 69.8, "dynamodb": 69.8},
        set(),
        None,
        {"dynamodb": 641, "documentdb": 182},
        "t",
    )
    assert (engine, rule, kind) == ("dynamodb", DECIDED_BY_SETTLED_WORKLOAD, None)


def test_settled_workload_compares_only_the_engines_still_level() -> None:
    # A third engine with more settled work but a lower unrounded score never wins.
    engine, rule, _ = decide_for_me(
        ["aurora_postgresql", "documentdb", "dynamodb"],
        {"aurora_postgresql": 70.0, "documentdb": 71.0, "dynamodb": 71.0},
        set(),
        None,
        {"aurora_postgresql": 900, "documentdb": 10, "dynamodb": 5},
        "t",
    )
    assert (engine, rule) == ("documentdb", DECIDED_BY_SETTLED_WORKLOAD)


@pytest.mark.parametrize(
    ("current", "source_compatible", "expected", "kind"),
    [
        (set(), "dynamodb", "dynamodb", TIE_KIND_SOURCE_COMPATIBLE),
        ({"documentdb"}, None, "documentdb", TIE_KIND_STABILITY),
    ],
)
def test_level_on_score_and_workload_the_tie_break_order_decides(
    current: set[str], source_compatible: str | None, expected: str, kind: str
) -> None:
    engine, rule, got_kind = decide_for_me(
        TIED, {"documentdb": 75.0, "dynamodb": 75.0}, current, source_compatible, {}, "t"
    )
    assert (engine, rule, got_kind) == (expected, DECIDED_BY_TIE_BREAK, kind)


def test_with_nothing_to_prefer_the_stable_hash_decides_and_repeats() -> None:
    picks = {
        decide_for_me(TIED, {"documentdb": 1.0, "dynamodb": 1.0}, set(), None, {}, key)
        for key in ("a.users", "a.users")
    }
    assert len(picks) == 1
    ((engine, rule, kind),) = picks
    assert engine in TIED and rule == DECIDED_BY_TIE_BREAK and kind == TIE_KIND_UNRESOLVED


def test_no_engine_name_bias_swapping_the_names_swaps_the_answer() -> None:
    """The same facts under swapped names give the swapped engine (#1003: engine-neutral)."""
    scores = {"documentdb": 80.6, "dynamodb": 80.15}
    swapped = {"documentdb": 80.15, "dynamodb": 80.6}
    assert decide_for_me(TIED, scores, set(), None, {}, "t")[0] == "documentdb"
    assert decide_for_me(TIED, swapped, set(), None, {}, "t")[0] == "dynamodb"
    counts = {"documentdb": 1, "dynamodb": 2}
    flipped = {"documentdb": 2, "dynamodb": 1}
    level = {"documentdb": 5.0, "dynamodb": 5.0}
    assert decide_for_me(TIED, level, set(), None, counts, "t")[0] == "dynamodb"
    assert decide_for_me(TIED, level, set(), None, flipped, "t")[0] == "documentdb"


def test_an_engine_with_no_unrounded_score_never_wins_on_score() -> None:
    engine, rule, _ = decide_for_me(TIED, {"dynamodb": 10.0}, set(), None, {}, "t")
    assert (engine, rule) == ("dynamodb", DECIDED_BY_UNROUNDED_SCORE)


def test_no_engine_is_an_error() -> None:
    with pytest.raises(ValueError):
        decide_for_me([], {}, set(), None, {}, "t")


def test_the_unrounded_score_is_the_routing_score_before_its_fraction_is_dropped() -> None:
    from src.contracts.analysis_output import ScoreBreakdown
    from src.tools.analysis.scoring import compute_confidence, weighted_confidence

    scores = ScoreBreakdown(
        pattern_match_score=77, complexity_score=81, performance_score=73, cost_score=65
    )
    exact = weighted_confidence(scores)
    assert exact == pytest.approx(77 * 0.4 + 81 * 0.3 + 73 * 0.2 + 65 * 0.1)
    assert compute_confidence(scores) == int(exact) < exact
