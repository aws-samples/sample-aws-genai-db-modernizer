# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The executive summary's Decision comes from the facts only (#1992).

The Decision names the first suggested wave and every wave the plan does not suggest
(held, not designed or not reviewed). A model's summary never states a decision: finalize
takes out its Decision paragraph and every sentence with approval or go-ahead wording,
whatever wave it names, and adds the Decision rendered from the facts after its text.
"""

from __future__ import annotations

import copy

import pytest

from src.shared.executive_brief import (
    SUMMARY_WORD_BUDGET,
    check_summary_brief,
    compose_summary,
    decision_paragraph,
    executive_brief_text,
    strip_decision_wording,
    word_count,
)
from src.shared.skill_sections import skill_path
from tests.unit.shared.test_executive_brief import FACTS

HELD_HOLD = {
    "check": "open_high_risk",
    "engine": "dynamodb",
    "risk_id": "RISK-001",
    "severity": "HIGH",
    "flagged_by": None,
    "line": "an open high risk on DynamoDB (RISK-001, performance degradation)",
}
EARLIER = {
    "check": "earlier_wave",
    "line": "wave 1 is not suggested yet, and this wave builds on it",
}


def _facts(states: dict[int, str], decision: int | None = 1) -> dict:
    facts = copy.deepcopy(FACTS)
    rows = []
    for n in (1, 2, 3):
        state = states.get(n, "suggested")
        holds, steps = [], []
        if state == "held":
            holds = [EARLIER] if n > 1 and states.get(1) == "held" else [HELD_HOLD]
            steps = (
                [{"step": "earlier_wave", "line": "wait until wave 1 is suggested"}]
                if holds == [EARLIER]
                else [{"step": "design", "line": "run the DynamoDB schema design"}]
            )
        rows.append(
            {
                "wave": n,
                "lists_relational_gaps": False,
                "readiness": state,
                "suggested": {"suggested": True, "held": False}.get(state),
                "holds": holds,
                "next_steps": steps,
                "readiness_line": "x",
            }
        )
    facts["waves"] = rows
    brief = facts["executive_brief"]
    for w in brief["waves"]:
        w["readiness"] = states.get(w["wave"], "suggested")
    brief["held_waves"] = [n for n, s in states.items() if s == "held"]
    brief["decision_wave"] = decision
    return facts


def _decision(facts: dict) -> str:
    text = decision_paragraph(facts)
    assert text
    return text


def test_the_decision_names_a_held_wave_with_its_reason_and_next_step() -> None:
    decision = _decision(_facts({3: "held"}))
    assert decision.startswith("Decision: approve wave 1. It is safe to approve:")
    assert decision.endswith(
        " Wave 3 is not suggested yet: an open high risk on DynamoDB (RISK-001). Next, run "
        "its schema design."
    )


def test_a_hold_raised_by_another_engines_analysis_says_so() -> None:
    facts = _facts({3: "held"})
    facts["waves"][2]["holds"] = [{**HELD_HOLD, "flagged_by": "documentdb"}]
    assert (
        "Wave 3 is not suggested yet: an open high risk on DynamoDB, flagged by the "
        "DocumentDB analysis (RISK-001)." in _decision(facts)
    )


@pytest.mark.passes_on_main  # a guard: no readiness, the decision as it was
def test_no_readiness_leaves_the_decision_as_it_was() -> None:
    decision = _decision(copy.deepcopy(FACTS))
    assert decision.startswith("Decision: approve wave 1.")
    assert "not suggested" not in decision


def test_waves_not_designed_or_not_reviewed_are_named_and_never_approved() -> None:
    decision = _decision(_facts({2: "not_designed", 3: "not_reviewed"}))
    assert decision.startswith("Decision: approve wave 1.")
    assert "Wave 2 is not designed yet: run its schema design." in decision
    assert "Wave 3 is not reviewed (the review failed): review its design again." in decision
    # No model: every wave is not designed, so none is put up for approval.
    decision = _decision(
        _facts({1: "not_designed", 2: "not_designed", 3: "not_designed"}, decision=None)
    )
    assert decision == (
        "Decision: no wave is suggested yet. Waves 1, 2 and 3 are not designed yet: run "
        "their schema design."
    )


def test_the_decision_wave_says_why_it_is_safe_from_its_own_facts() -> None:
    """With wave 1 not suggested, the decision is a later wave, and the reason is that
    wave's own (a cache moves no data), never wave 1's."""
    decision = _decision(_facts({1: "not_designed"}, decision=2))
    assert decision.startswith("Decision: approve wave 2. It is safe to approve: ElastiCache")
    assert "1:1" not in decision


def test_every_wave_held_asks_to_approve_none() -> None:
    decision = _decision(_facts({1: "held", 2: "held", 3: "held"}, decision=None))
    assert decision.startswith("Decision: no wave is suggested yet.")
    assert "approve" not in decision
    assert "Waves 2 and 3 are not suggested yet: they build on wave 1." in decision


def test_the_deterministic_summary_with_a_held_wave_fits_and_passes() -> None:
    facts = _facts({3: "held"})
    text = executive_brief_text(facts, "wordpress")
    assert text and "Wave 3 is not suggested yet" in text
    assert check_summary_brief(text, facts) == []
    assert word_count(text) <= SUMMARY_WORD_BUDGET


# The review's probes (2051): each states an approval, a go-ahead or a next wave in its
# own words. Every one is taken out, whatever wave it names.
PROBES_ONE_HELD = [
    "Wave 3 is not suggested yet, but approve wave 3 as well.",
    "Proceed with wave 3 too.",
    "Approve all three waves.",
    "Approve the full plan now.",
    "Wave 3 is ready to start.",
    "Approve waves 1 through 3.",
    "Approve the DynamoDB wave.",
    "Approve the final wave.",
    "Approve wave 1 and wave 3; wave 2 is not suggested yet elsewhere.",
]
PROBES_ALL_HELD = [
    "Approve the plan's first step.",
    "Approve the full modernization.",
    "Approve the first wave.",
]
MORE = [
    "Decision: approve wave 3.",
    "We recommend you sign off on wave 1 today.",
    "Give the green light to the migration.",
    "Start the migration with wave 3 next quarter.",
    "Move on to the next wave once the cache is live.",
    "The team can go ahead with the DynamoDB move.",
]

MODEL = (
    "Recommendation: move the wordpress MySQL database to three AWS services in three waves: "
    "Aurora MySQL first, then ElastiCache, then DynamoDB. The projected cost of the full "
    "modernization is $505 per month at AWS Price List on-demand prices.\n\n"
    "Risk: overall risk is medium. Confirm the close calls before wave 3 starts."
)


@pytest.mark.parametrize("probe", PROBES_ONE_HELD + MORE)
def test_a_models_approval_in_any_words_is_taken_out(probe: str) -> None:
    facts = _facts({3: "held"})
    shown = compose_summary(f"{MODEL} {probe}", facts)
    assert probe not in shown
    body, stripped = strip_decision_wording(f"{MODEL} {probe}")
    assert probe in stripped or any(probe.startswith(s) for s in stripped)
    # What the customer sees is the model's own text, then the facts' Decision only.
    assert shown == f"{body}\n\n{decision_paragraph(facts)}"


@pytest.mark.parametrize("probe", PROBES_ALL_HELD)
def test_with_every_wave_held_no_approval_is_shown(probe: str) -> None:
    facts = _facts({1: "held", 2: "held", 3: "held"}, decision=None)
    shown = compose_summary(f"{MODEL} {probe}", facts)
    assert probe not in shown
    assert "approve" not in shown.lower().replace("not approve", "")


def test_the_models_own_text_is_kept_and_composing_twice_changes_nothing() -> None:
    facts = _facts({3: "held"})
    shown = compose_summary(f"{MODEL}\n\nDecision: approve wave 1 and then wave 3.", facts)
    assert shown.startswith(MODEL)
    assert "before wave 3 starts" in shown  # a risk's own timing is not a decision
    assert shown.count("Decision:") == 1
    assert compose_summary(shown, facts) == shown


def test_a_summary_with_no_brief_is_left_as_it_is() -> None:
    assert compose_summary("Approve wave 1.", {}) == "Approve wave 1."


def test_the_skill_says_the_decision_is_rendered_from_the_facts() -> None:
    skill = skill_path("executive-summary.md").read_text()
    assert "Never state a decision" in skill
    assert "rendered from the report's facts" in skill
