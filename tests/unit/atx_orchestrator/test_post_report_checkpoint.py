# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The orchestrator prompt's post-report checkpoint (ADR-029 Amendment 1, #753).

ADR-029 Amendment 1 defers job completion: after the report the orchestrator asks whether the
customer wants to adjust routing or finish, and calls ``complete_assessment`` only after the
customer confirms. The status transitions are covered in ``test_job_status.py``; these tests
guard the prompt text that gates the terminal call on the customer's answer. Nothing here
calls a model.
"""

from __future__ import annotations

import re

from src.atx_orchestrator.orchestrator import PIPELINE_TOOLS, SYSTEM_PROMPT
from src.atx_orchestrator.tools import complete_assessment

FLAT = " ".join(SYSTEM_PROMPT.split())


def _step(number: int) -> str:
    """Text of numbered pipeline step ``number``, up to the next numbered step or bullet block."""
    match = re.search(
        rf"^ {{6}}{number}\. (.*?)(?=^ {{6}}\d+\. |^ {{2}}- )", SYSTEM_PROMPT, re.S | re.M
    )
    assert match, f"step {number} is missing from the orchestrator prompt"
    return match.group(1)


class TestPostReportCheckpointStep:
    def test_step_8_follows_synthesis_and_asks_adjust_or_finish(self):
        assert "run_synthesis_via_a2a" in _step(7)
        step = _step(8)
        assert "Present the report" in step
        assert "adjust" in step and "routing" in step and "finish" in step

    def test_step_8_waits_for_the_answer(self):
        assert "WAIT for their answer" in _step(8)

    def test_step_8_calls_complete_assessment_only_when_done(self):
        step = _step(8)
        done_branch = step[step.index("If they are done") :]
        assert "complete_assessment(job_id, database_name)" in done_branch
        assert "Do NOT call it until they confirm" in done_branch

    def test_step_8_routes_a_change_back_through_reentry(self):
        change_branch = _step(8).split("If they are done")[0]
        for name in (
            "reopen_assignment_review",
            "redispatch_after_reroute",
            "run_synthesis_via_a2a",
        ):
            assert name in change_branch
        assert "complete_assessment" not in change_branch


class TestCompletionIsNeverAutomatic:
    def test_no_earlier_step_calls_complete_assessment(self):
        for number in range(1, 8):
            assert "complete_assessment" not in _step(
                number
            ), f"step {number} must not complete the job"

    def test_checkpoint_is_a_required_pause(self):
        assert "TWO required pauses" in FLAT
        assert "post-report checkpoint (step 8)" in FLAT
        assert "do not call complete_assessment until the customer confirms" in FLAT

    def test_tool_description_gates_on_the_customers_confirmation(self):
        assert "ONLY when the customer" in FLAT
        assert "Do NOT call it automatically after" in FLAT

    def test_reentry_loops_back_to_the_checkpoint(self):
        assert "return to the post-report checkpoint (step 8)" in FLAT
        assert "only when they confirm they are done and you call complete_assessment" in FLAT

    def test_tool_is_registered_with_the_orchestrator(self):
        assert complete_assessment in PIPELINE_TOOLS
