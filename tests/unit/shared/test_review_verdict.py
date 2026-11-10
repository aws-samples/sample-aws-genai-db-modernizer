# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""One verdict rule for every engine's PE review, and a blocker always holds (#2081)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.shared.review_verdict import (
    APPROVED_WITH_BLOCKER,
    CHANGES_WITHOUT_REQUESTS,
    verdict_errors,
)
from src.shared.wave_readiness import open_review_count, review_warnings

BLOCKER = {"category": "indexing", "severity": "blocker", "target": "t", "requested_change": "x"}
WARNING = {**BLOCKER, "severity": "warning"}


@pytest.mark.parametrize(
    ("verdict", "requests", "errors"),
    [
        ("APPROVED", [BLOCKER], [APPROVED_WITH_BLOCKER]),
        ("APPROVED", [WARNING], []),
        ("APPROVED", [], []),
        ("CHANGES_REQUESTED", [], [CHANGES_WITHOUT_REQUESTS]),
        ("CHANGES_REQUESTED", [WARNING], []),
    ],
)
def test_the_rule(verdict: str, requests: list, errors: list) -> None:
    assert verdict_errors(verdict, requests) == errors


def test_dynamodbs_contract_enforces_the_same_rule() -> None:
    """DynamoDB's validator shares the check: the same messages, from the same place."""
    from src.contracts import dynamodb_pe_review as ddb

    with pytest.raises(ValidationError, match=APPROVED_WITH_BLOCKER):
        ddb.PEReviewResult.model_validate(
            {
                "verdict": "APPROVED",
                "summary": "s",
                "strengths": ["k"],
                "change_requests": [
                    {
                        "category": "key_design",
                        "severity": "blocker",
                        "target": "t",
                        "current_state": "c",
                        "requested_change": "r",
                        "rationale": "w",
                    }
                ],
            }
        )
    import inspect

    assert "verdict_errors(self.verdict, self.change_requests)" in inspect.getsource(ddb)


def _trace(verdict: str, *, blockers: int | None, requests: list | None = None) -> dict:
    entry: dict = {"type": "pe_review", "iteration": 0, "verdict": verdict}
    if blockers is not None:
        entry.update(blockers=blockers, warnings=[])
    trace: dict = {"trace": [entry]}
    if requests is not None:
        trace["pe_review"] = {"verdict": verdict, "change_requests": requests}
    return trace


def test_an_approval_that_lists_a_blocker_holds_its_wave() -> None:
    """A Bedrock answer (no verdict check on its contract) or a saved review: a blocker
    holds whatever the verdict, so it is never silently dropped."""
    held = _trace("APPROVED", blockers=1)
    assert open_review_count(held) == 1 and review_warnings(held) == []
    saved = _trace("APPROVED", blockers=None, requests=[BLOCKER, WARNING])
    assert open_review_count(saved) == 1


@pytest.mark.passes_on_main  # the cases that already held, or never did
def test_what_held_before_still_holds_and_warnings_never_do() -> None:
    assert open_review_count(_trace("CHANGES_REQUESTED", blockers=1)) == 1
    assert open_review_count(_trace("CHANGES_REQUESTED", blockers=0)) == 0
    assert open_review_count(_trace("APPROVED", blockers=0)) == 0
    assert open_review_count(_trace("APPROVED", blockers=None)) == 0  # an older record
    assert open_review_count(_trace("CHANGES_REQUESTED", blockers=None)) == 1
    assert open_review_count({"trace": [{"type": "pe_error", "iteration": 0}]}) == 0
