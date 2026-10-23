"""Edge cases of the result builder that the samples do not reach (#1182)."""

from __future__ import annotations

import pytest

from src.assessment.result_builder import RunState, build_relational_engine


def _state(choice: dict) -> RunState:
    return RunState(
        run_through="reality_check",
        collector={"metadata": {"source_database": {"engine": "sqlserver"}}},
        triage={},
        lineage=[{"version": 1, "aurora_engine_choice": choice}],
    )


@pytest.mark.parametrize("reason", [None, "", "   "])
def test_a_missing_choice_reason_is_never_the_text_none(reason: str | None) -> None:
    choice = {"engine": "aurora_postgresql", "source_engine": "sqlserver"}
    if reason is not None:
        choice["reason"] = reason
    relational = build_relational_engine(_state(choice))
    assert relational is not None
    assert relational.engine == "aurora_postgresql"
    assert (
        relational.reason == "Aurora PostgreSQL is the Aurora engine routing chose for the source"
    )


def test_a_recorded_reason_is_kept() -> None:
    relational = build_relational_engine(_state({"engine": "aurora_mysql", "reason": "why"}))
    assert relational is not None and relational.reason == "why"
