# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The orchestrator prompt agrees with itself on its pauses and step numbers.

The prompt numbers its run sequence (``1a.`` to ``8.``) and refers back to the
steps by number. A reference to the wrong step, or a second count of the
required pauses, leaves the model two instructions to reconcile. Nothing here
calls a model.
"""

from __future__ import annotations

import re

import pytest

from src.atx_orchestrator.orchestrator import SYSTEM_PROMPT

FLAT = " ".join(SYSTEM_PROMPT.split())


def _sequence() -> dict[str, str]:
    """The run sequence's steps by number, from "Then run this sequence" to the next bullet."""
    start = SYSTEM_PROMPT.index("Then run this sequence")
    block = SYSTEM_PROMPT[start:]
    block = block[: block.index("\n  - ", 1)]
    parts = re.split(r"^ {6}(\d+[a-z]?)\. ", block, flags=re.M)
    return {
        number: " ".join(text.split())
        for number, text in zip(parts[1::2], parts[2::2], strict=True)
    }


@pytest.mark.passes_on_main  # the step list the references below rely on
def test_the_sequence_has_the_steps_the_references_name() -> None:
    assert list(_sequence()) == ["1a", "2", "3", "4", "5", "6", "7", "8"]


def test_the_prompt_counts_the_required_pauses_once() -> None:
    assert "TWO required pauses" in FLAT
    assert "one required pause" not in FLAT.lower()


def test_the_assignment_review_gate_points_at_its_steps() -> None:
    steps = _sequence()
    assert "the assignment-review gate (steps 4 and 5)" in FLAT
    assert "present_assignment_review" in steps["4"] and "REQUIRED stop" in steps["4"]
    assert "finalize_assignment_review" in steps["5"]


def test_schema_design_points_at_its_step() -> None:
    steps = _sequence()
    assert "run_schema_design_<engine>_via_a2a" in steps["6"]
    assert "runs schema-design (step 6) before synthesis" in FLAT
    assert "(step 4) before synthesis" not in FLAT


@pytest.mark.passes_on_main  # guards references added later
def test_every_step_reference_names_a_step_of_the_sequence() -> None:
    steps = _sequence()
    for match in re.finditer(r"\(steps? (\d+)(?: and (\d+))?\)", FLAT):
        for number in filter(None, match.groups()):
            assert number in steps, match.group(0)
