"""The customer-text guard on ``Assignment.moves``, the move ledger (#616).

The e2e wording guard reported the raw engine ids in every Move's runner-up,
candidate ladder and replaced engine. A Move is a machine record: every string
in it is an engine id, a query id, an enum code (pass, reason code, gate,
actor), a triage signal name or a hex group key. None is prose, so they are
exempt by schema path. A test here keeps the ledger free of free-text fields,
so a prose field added later must be scanned (or shown never rendered) before
this exemption covers it. The assignment's own reasons are still scanned.
"""

from __future__ import annotations

import enum
import types
import typing

from pydantic import BaseModel

from src.agents.referee.move_ledger import group_key
from src.contracts.assignment_models import Assignment
from src.contracts.move_ledger import CandidateScore, EngineScore, GroupGate, Move, MoveEvidence
from src.tools.validation.customer_text_guard import ID_FIELD_PATHS, ID_FIELDS, scan_json

# Each ledger model and the JSON key the guard's walk reaches it through
# (the parent key of its fields, with list indexes collapsed).
LEDGER_MODELS: dict[str, type[BaseModel]] = {
    "moves": Move,
    "evidence": MoveEvidence,
    "candidates": CandidateScore,
    "runner_up": EngineScore,
    "group_gates": GroupGate,
}


def _assignment(reason: str = "Highest fit for Amazon DynamoDB") -> dict:
    move = {
        "seq": 1,
        "query_id": "q1",
        "pass": "utility_pin",
        "from_engine": "dynamodb",
        "to_engine": "aurora_mysql",
        "score_before": 80,
        "score_after": 40,
        "runner_up": {"engine": "documentdb", "score": 70},
        "reason_code": "override.utility",
        "evidence": {
            "candidates": [
                {"engine": "aurora_mysql", "score": 40},
                {"engine": "documentdb", "score": None, "gate": "capability"},
                {"engine": "dynamodb", "score": 80},
                {"engine": "opensearch", "score": None, "gate": "write_gate"},
            ],
            "group_scores": {"aurora_mysql": 40.0},
            "group_gates": [
                {"engine": "documentdb", "query_id": "q2", "gate": "capability"},
                {"engine": "opensearch", "query_id": "q1", "gate": "write_gate"},
            ],
            "signal": "text_search",
            "replaced_engine": "dynamodb",
        },
        "group_key": group_key(["q1", "q2"]),  # derived, as the resolver does
        "tied_engines": ["aurora_mysql", "dynamodb"],
        "actor": "rule",
    }
    return Assignment.model_validate(
        {
            "job_id": "j",
            "version": 1,
            "status": "auto_generated",
            "timestamp": "2026-10-14T00:00:00Z",
            "query_assignments": [
                {
                    "query_id": "q1",
                    "assigned_engine": "aurora_mysql",
                    "confidence": 40,
                    "source_tables": ["t1"],
                    "assignment_reason": reason,
                }
            ],
            "table_assignments": [],
            "co_dependency_groups": [],
            "validation_warnings": [],
            "moves": [move],
        }
    ).model_dump(mode="json")


def test_a_moves_structured_fields_are_not_findings() -> None:
    assert scan_json(_assignment()) == []


def test_an_all_gated_group_fallback_is_not_a_finding() -> None:
    """#817: a co-dependency group no engine can serve records each engine's
    gate in ``evidence.group_gates``; the resolver's own output for it (the
    Move and the reason) passes the guard."""
    from tests.unit.agents.referee.test_all_gated_group_reason import (
        RECURSIVE_JOIN,
        SELF_JOIN,
        _query,
        _resolve,
    )

    result = _resolve(
        [_query("qa", SELF_JOIN), _query("qb", RECURSIVE_JOIN)],
        ["documentdb", "dynamodb"],
        ["documentdb", "dynamodb", "aurora_postgresql"],
    )
    data = result.model_dump(mode="json")
    assert any(m["evidence"]["group_gates"] for m in data["moves"])
    assert scan_json(data) == []


def test_the_assignments_own_reason_is_still_scanned() -> None:
    findings = scan_json(_assignment(reason="Highest fit for dynamodb"))
    assert [(f.kind, f.path, f.text) for f in findings] == [
        ("engine_id", "$.query_assignments[0].assignment_reason", "dynamodb")
    ]


def test_an_engine_field_outside_the_ledger_is_still_scanned() -> None:
    findings = scan_json({"notes": [{"engine": "dynamodb"}]})
    assert [f.text for f in findings] == ["dynamodb"]


def _carries_text(annotation: object) -> bool:
    """True when a field can hold a string (str or a str enum), at any nesting."""
    if annotation is str:
        return True
    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        return issubclass(annotation, str)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return False  # its own fields are checked under its own key
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType, list, dict, tuple, set):
        args = typing.get_args(annotation)
        if origin is dict:
            args = args[1:]  # keys are never scanned
        return any(_carries_text(a) for a in args)
    return False


def test_every_string_field_in_the_ledger_is_a_declared_machine_field() -> None:
    """The ledger has no free-text field. A new string field on a Move (a note,
    an explanation) fails here until it is either kept out of this exemption
    and scanned, or shown never to reach a customer."""
    for parent, model in LEDGER_MODELS.items():
        for name, info in model.model_fields.items():
            key = info.alias or name
            if not _carries_text(info.annotation):
                continue
            assert key in ID_FIELDS or (parent, key) in ID_FIELD_PATHS, (parent, key)
