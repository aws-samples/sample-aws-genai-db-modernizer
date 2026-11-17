# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A model-repaired routine is held back by step 1's deploy gate until step 1 deploys
repaired routines (#2385, #2384): ``routine_holds`` lists it as ``repaired_not_deployed``
whatever the test on a test Aurora found, and a routine that calls it is held too."""

from __future__ import annotations

from typing import Any

from src.shared.routine_facts import (
    DEPENDS_ON_HELD,
    HELD_REASONS,
    REPAIRED_NOT_DEPLOYED,
    routine_holds,
)
from src.tools.data_movement.routine_test_gate import HELD_WHY

PROVED = {"outcome": "proved"}


def _row(rid: str, status: str, *, calls: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "kind": "function",
        "routine_id": rid,
        "status": status,
        "proof": PROVED,
        "dependencies": [
            {"kind": "function", "routines": [{"kind": "function", "routine_id": c}]} for c in calls
        ],
    }


def test_a_repaired_routine_is_held_even_when_the_current_test_proved_it():
    rows = [
        _row("a", "as_is"),
        _row("fixed", "model_repaired"),
        _row("caller", "rewritten", calls=("fixed",)),
    ]
    assert routine_holds(rows, current=True) == {
        ("function", "fixed"): REPAIRED_NOT_DEPLOYED,
        ("function", "caller"): DEPENDS_ON_HELD,
    }


def test_before_any_test_a_repaired_routine_keeps_its_own_reason():
    rows = [_row("a", "as_is"), _row("fixed", "model_repaired")]
    assert routine_holds(rows, current=False) == {
        ("function", "a"): "not_tested",
        ("function", "fixed"): REPAIRED_NOT_DEPLOYED,
    }


def test_a_routine_that_is_not_carried_is_never_listed():
    assert routine_holds([_row("x", "needs_attention")], current=True) == {}


def test_every_held_reason_has_its_words_for_the_customer():
    assert REPAIRED_NOT_DEPLOYED in HELD_REASONS
    assert set(HELD_WHY) == set(HELD_REASONS)
    assert "does not deploy a repaired routine yet" in HELD_WHY[REPAIRED_NOT_DEPLOYED]
