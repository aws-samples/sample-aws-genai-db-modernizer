# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The routine repair contract (#1940): a model's answer, and what the seam made of it.

Imported inside each test, so on a branch without the module every test fails on its
own rather than the file failing to import.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest
from pydantic import ValidationError


def _contract():
    return importlib.import_module("src.contracts.routine_repair")


def _row(state: str, **extra: Any) -> dict[str, Any]:
    return {"kind": "function", "routine_id": "public.f(integer)", "state": state, **extra}


def _result(rows: list[dict[str, Any]], *, llm_mode: str = "external", **extra: Any):
    c = _contract()
    counts = dict.fromkeys(c.REPAIR_STATES, 0)
    for row in rows:
        if row["state"] in counts:
            counts[row["state"]] += 1
    return c.RoutineRepairContract.model_validate(
        {
            "job_id": "j",
            "database_name": "d",
            "assignment_version": 1,
            "llm_mode": llm_mode,
            "source_engine": "postgresql",
            "target_engine": "aurora_postgresql",
            "counts": counts,
            "routines": rows,
            **extra,
        }
    )


def test_a_repaired_row_keeps_its_candidate_and_note():
    result = _result(
        [_row("repaired_pending_proof", repaired_create="CREATE ...", change_note="x")]
    )
    assert result.routines[0].state == "repaired_pending_proof"


@pytest.mark.parametrize(
    "row",
    [
        _row("proved"),
        _row("carried"),
        _row("repaired_pending_proof"),
        _row("repaired_pending_proof", repaired_create="CREATE ..."),
        _row("no_answer", repaired_create="CREATE ...", change_note="x"),
        _row("refused"),
        _row("no_answer", refusal="renamed"),
        _row("refused", refusal="looks_wrong"),
    ],
    ids=[
        "proved",
        "carried",
        "pending-bare",
        "pending-no-note",
        "kept-candidate",
        "refused-no-code",
        "code-not-refused",
        "unknown-code",
    ],
)
def test_a_row_that_does_not_fit_its_state_is_refused(row):
    with pytest.raises(ValidationError):
        _result([row])


def test_counts_must_match_and_rows_be_unique():
    c = _contract()
    with pytest.raises(ValidationError):
        _result([_row("no_answer")], counts=dict.fromkeys(c.REPAIR_STATES, 0))
    with pytest.raises(ValidationError):
        _result([_row("no_answer"), _row("no_answer")])


def test_mode_none_repairs_nothing():
    with pytest.raises(ValidationError):
        _result(
            [_row("repaired_pending_proof", repaired_create="CREATE ...", change_note="x")],
            llm_mode="none",
        )
    assert _result([_row("not_repaired")], llm_mode="none").counts["not_repaired"] == 1


def test_an_answer_holds_only_a_create_and_a_note_per_routine():
    c = _contract()
    entry = {
        "kind": "function",
        "routine_id": "public.f(integer)",
        "repaired_create": "CREATE ...",
        "change_note": "  Renames a call.\n Nothing else. ",
    }
    answer = c.parse_routine_repair_response({"repairs": [entry]})
    assert answer.repairs[0].change_note == "Renames a call. Nothing else."
    for blank in ({"change_note": " \n "}, {"repaired_create": "  "}):
        with pytest.raises(ValidationError):
            c.parse_routine_repair_response({"repairs": [{**entry, **blank}]})
    for extra in ({"state": "proved"}, {"proof": "passed"}, {"carried": True}):
        with pytest.raises(ValidationError):
            c.parse_routine_repair_response({"repairs": [{**entry, **extra}]})
    with pytest.raises(ValidationError):
        c.parse_routine_repair_response({"repairs": [], "status": "proved"})


def test_the_states_never_include_proved():
    c = _contract()
    assert "proved" not in c.REPAIR_STATES
    assert set(c.REPAIR_STATES) == {
        "repaired_pending_proof",
        "refused",
        "no_answer",
        "not_repaired",
    }
