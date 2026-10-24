"""``deterministic=False`` runs the reality check's model review, through the same
model entry points the pipeline uses; here they are patched (tests never call a model)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.assessment import assess
from src.contracts.model_flag import FlagReason, ModelFlag
from tests.unit.assessment.conftest import assessed, sample_collection

HANDLER = "src.agents.referee.reality_check_handler"


@pytest.fixture
def model() -> Iterator[dict[str, MagicMock]]:
    with (
        patch(f"{HANDLER}.require_valid_guardrail_config") as guardrail,
        patch(f"{HANDLER}.validate_consolidations", return_value=[]) as validate,
        patch(f"{HANDLER}._generate_executive_summary", return_value=None) as summary,
    ):
        yield {"guardrail": guardrail, "validate": validate, "summary": summary}


def _run(deterministic: bool, **options: Any) -> Any:
    return assess(
        {
            "database_name": "wordpress",
            "collection": sample_collection("wordpress"),
            "options": {"deterministic": deterministic, **options},
        }
    )


def test_deterministic_calls_no_model(model: dict[str, MagicMock]) -> None:
    result = _run(True)
    assert result.statements
    for entry in model.values():
        entry.assert_not_called()


def test_model_review_runs_and_accepting_it_changes_nothing(
    model: dict[str, MagicMock],
) -> None:
    result = _run(False, include_artifacts=True)
    model["guardrail"].assert_called_once()
    model["validate"].assert_called()
    model["summary"].assert_called_once()
    assert result.options.deterministic is False
    expected = assessed("wordpress", "reality_check")
    assert [s.model_dump() for s in result.statements] == [
        s.model_dump() for s in expected.statements
    ]
    assert result.artifacts is not None


def test_a_model_flag_moves_the_statement(model: dict[str, MagicMock]) -> None:
    """A query the model judges its consolidated engine unable to serve is re-routed by
    the scorer, and reports the flag as its reason."""
    moved = next(
        s
        for s in assessed("wordpress", "reality_check").statements
        if s.reason_code == "consolidated" and s.engine == "dynamodb"
    )
    model["validate"].return_value = [
        ModelFlag(
            query_ids=[moved.query_id],
            flagged_engine=moved.engine,
            reason_category=FlagReason.MISSING_FEATURE,
            explanation="cannot serve this statement",
        )
    ]
    result = _run(False)
    after = next(s for s in result.statements if s.query_id == moved.query_id)
    assert after.engine != moved.engine
    assert after.reason_code == "capability_flag"
    assert moved.engine in after.excluded


@pytest.mark.passes_on_main
def test_a_model_flag_is_a_model_flag_move_in_the_artifacts(model: dict[str, MagicMock]) -> None:
    """What the contract tells a caller to read (#1259): the review's change is a Move
    with actor ``model_flag`` in ``artifacts.assignment.moves``; the deterministic run
    records no such Move."""
    moved = next(
        s
        for s in assessed("wordpress", "reality_check").statements
        if s.reason_code == "consolidated" and s.engine == "dynamodb"
    )
    model["validate"].return_value = [
        ModelFlag(
            query_ids=[moved.query_id],
            flagged_engine=moved.engine,
            reason_category=FlagReason.MISSING_FEATURE,
            explanation="cannot serve this statement",
        )
    ]
    reviewed = _run(False, include_artifacts=True)
    assert reviewed.artifacts is not None
    flagged = [
        m
        for m in reviewed.artifacts.model_dump()["assignment"]["moves"]
        if m["actor"] == "model_flag"
    ]
    assert [m["query_id"] for m in flagged] == [moved.query_id]
    assert flagged[0]["from_engine"] == moved.engine
    after = next(s for s in reviewed.statements if s.query_id == moved.query_id)
    assert flagged[0]["to_engine"] == after.engine

    plain = _run(True, include_artifacts=True)
    assert plain.artifacts is not None
    moves = plain.artifacts.model_dump()["assignment"]["moves"]
    assert moves and all(m["actor"] != "model_flag" for m in moves)
