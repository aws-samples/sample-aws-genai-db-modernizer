# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""After ``/test-routines``, the reports state the test's result (#2429).

The real runs of 2026-11-17 on #2411 found the carried-routines risk still asking for the
test after it ran, the wave 1 gate still asking to close that risk, and two labels for one
state. Here: a routine that showed no difference on the calls made leaves the LOW
carried-routines risk, so the gate no longer asks to close it for it; a routine the test
holds back keeps the risk with its reason, whatever its checks said; and the facts and the
step 1 lines share one label per state, from ``src/shared/routine_facts.py``.
"""

from __future__ import annotations

from typing import Any

from src.agents.referee.migration_risks import build_migration_risks
from src.contracts.routine_check import LEGACY_TEST_LABELS as LEGACY_TEST_STATES
from src.contracts.synthesis_output import RoutineFacts
from src.shared.migration_risk_rules import routine_risks, wave_gate_lines
from src.shared.routine_facts import (
    NOT_TESTED_STATE,
    TEST_COMMAND,
    TESTED_STATE,
    routine_facts,
    routine_test_state,
)
from tests.unit.shared.test_routine_facts import REWRITE, _artifact, _row, _unknown
from tests.unit.tools.data_movement.routine_fixtures import with_test_result

PG = "aurora_postgresql"
WAVES = [{"wave": 1, "engines": [PG], "moves_from": ["postgresql"], "tables": []}]
DIGEST = "d" * 64
ARTIFACT = _artifact(
    _row("public.plain()"),
    _row("public.unsure()", checks=[_unknown("pg-dependency")]),
    _row("public.owned()", "rewritten", rewrites=[REWRITE]),
)


def _facts(outcomes: dict[str, str] | None = None, *, tested: bool = True) -> dict[str, Any]:
    artifact = with_test_result(ARTIFACT, DIGEST, outcomes) if tested else ARTIFACT
    facts = routine_facts(artifact, 1, WAVES, sources_digest=DIGEST)
    assert facts is not None
    return facts


def _carried(facts: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for r in routine_risks(facts) if r["rule"] == "routines.carried"]


def _gate(facts: dict[str, Any]) -> str:
    risks = build_migration_risks(migration_waves=WAVES, query_assignments=[], source_queries=[],
                                  source_engine="postgresql", routines=facts)  # fmt: skip
    return wave_gate_lines(risks).get(1, "")


def test_before_a_test_the_risk_asks_for_it_and_the_gate_names_it() -> None:
    facts = _facts(tested=False)
    [risk] = _carried(facts)
    assert f"with {TEST_COMMAND} before step 1 deploys them" in risk["mitigation"]
    assert "(carried routines to check)" in _gate(facts)
    assert all(r.get("held_back") is None for r in facts["routines"])


def test_routines_with_no_difference_leave_the_risk_and_the_gate() -> None:
    facts = _facts()
    assert _carried(facts) == []
    gate = _gate(facts)
    assert "carried routines to check" not in gate and "Close" not in gate
    assert all(r.get("held_back") is None for r in facts["routines"])
    # The rewrite stays in the engineering report's notes: the facts still name it.
    assert [n["rule_id"] for n in facts["check_notes"] if n["note"] == "rewrite"] == [
        REWRITE["rule_id"]
    ]


def test_a_held_routine_keeps_the_risk_with_its_reason() -> None:
    facts = _facts({"public.owned()": "not_proved"})
    [risk] = _carried(facts)
    assert risk["description"].endswith(
        "1 of the 3 routines carried to Aurora PostgreSQL (1 function) is held back after the test on a test Aurora, so "
        "step 1 does not deploy it. Why: the test could not run or compare it, or a call failed (1)."
    )
    assert "before step 1 deploys them" not in risk["mitigation"]
    assert risk["mitigation"].startswith(
        f"Fix what {TEST_COMMAND} reported for it, test it again with "
        f"{TEST_COMMAND}, then write the step 1 routine scripts again."
    )
    assert "(carried routines to check)" in _gate(facts)
    held = {r["routine_id"]: r.get("held_back") for r in facts["routines"]}
    assert held == {"public.unsure()": None, "public.owned()": "not_proved"}


def test_a_held_routine_with_no_check_note_is_raised_too() -> None:
    """A routine carried as it is with every check passed has no row before a test; once
    the test holds it back, it is one of the carried routines to check, with its reason."""
    facts = _facts({"public.plain()": "needs_attention"})
    [risk] = _carried(facts)
    assert "Why: the test found a difference or an error (1)." in risk["description"]
    [row] = [r for r in facts["routines"] if r["routine_id"] == "public.plain()"]
    assert (row["held_back"], row["risk_classes"], row["listed"]) == (
        "needs_attention", ["carried_review"], False)  # fmt: skip
    assert facts["carried_review_count"] == 3  # with the rewritten one and the unsure one
    RoutineFacts.model_validate(facts)


def test_one_label_per_state() -> None:
    assert (routine_test_state(False), routine_test_state(True)) == (NOT_TESTED_STATE, TESTED_STATE)
    assert (NOT_TESTED_STATE, TESTED_STATE) == (
        "checked, not tested yet",
        "tested on a test Aurora",
    )
    assert _facts(tested=False)["proof"] == NOT_TESTED_STATE
    assert _facts()["proof"] == TESTED_STATE
    plain = routine_facts(_artifact(_row("public.plain()")), 1, WAVES, sources_digest=DIGEST)
    assert plain is not None and f"is {NOT_TESTED_STATE};" in plain["line"]


def test_a_report_saved_with_the_old_labels_reads_the_new_ones() -> None:
    facts = _facts()
    assert LEGACY_TEST_STATES == {
        "checked, not load-tested": NOT_TESTED_STATE,
        "load-tested on a test Aurora": TESTED_STATE,
    }
    for old, new in LEGACY_TEST_STATES.items():
        assert RoutineFacts.model_validate({**facts, "proof": old}).proof == new


def test_a_block_whose_only_carried_rows_are_held_needs_no_new_words() -> None:
    """#2429: a held routine with no rewrite and every check run has no note, so an empty
    ``check_notes`` is complete there and the words are not rebuilt on each render."""
    from src.shared.routine_facts import needs_routine_words

    artifact = _artifact(_row("public.plain()"), _row("public.other()"))
    facts = routine_facts(with_test_result(artifact, DIGEST, {"public.plain()": "needs_attention"}),
                          1, WAVES, sources_digest=DIGEST)  # fmt: skip
    assert facts is not None and facts["carried_review_count"] == 1
    assert facts["check_notes"] == [] and needs_routine_words(facts) is False
    # A block that should have a note and lost it still gets its words back.
    noted = _facts()
    assert needs_routine_words({**noted, "check_notes": []}) is True
