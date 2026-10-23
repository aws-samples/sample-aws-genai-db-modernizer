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


def test_an_engine_line_without_report_text_still_has_a_true_reason() -> None:
    """A line the report and the check left without text (no ranking line, no recorded
    reason) gets a sentence true of its role, never another role's (#1390)."""
    from src.assessment.result_builder import build_engine_lines

    state = RunState(
        run_through="reality_check",
        collector={"metadata": {"source_database": {"engine": "mysql"}}},
        triage={},
        analyses={"dynamodb": {}, "aurora_mysql": {}, "documentdb": {}},
        lineage=[
            {"version": 2, "query_assignments": [{"query_id": "q1", "assigned_engine": "dynamodb"}]}
        ],
        reality_check={
            "consolidations": [
                {"from_engine": "documentdb", "to_engine": "dynamodb", "query_count": 1}
            ],
            "before_distribution": {"documentdb": 1, "dynamodb": 0},
        },
    )
    lines = {e.engine: e for e in build_engine_lines(state, {"dynamodb": 1}, [], None)}
    assert (lines["dynamodb"].role, lines["dynamodb"].reason) == (
        "owner",
        "DynamoDB serves 1 statement.",
    )
    removed = lines["documentdb"]
    assert (removed.role, removed.consolidated_into) == ("consolidated", "dynamodb")
    assert removed.reason == "The reality check moved its statements to DynamoDB."
    assert (lines["aurora_mysql"].role, lines["aurora_mysql"].reason) == (
        "not_selected",
        "Analysed; no statement was placed on Aurora MySQL.",
    )
