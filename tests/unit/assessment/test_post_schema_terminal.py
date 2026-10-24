"""``assess()`` marks a statement the post-schema routing left terminal (#1497).

A statement a design flags is moved by the post-schema routing to its best remaining
engine (#1492). When none is left in the engine scope (``no_kept_engine``), or the
routing reached its last pass (``depth_limit``), the router records it as terminal and
it stays on the flagging engine. The result marks it (``statements[].post_schema_terminal``)
and counts it (``summary.post_schema_terminal_count``), from the router's terminal
records, without changing routing: the assignment is the same with or without the
marker. Tests never call a model (``tests/assess_models.py``).
"""

from __future__ import annotations

from typing import Any, get_args
from unittest.mock import patch

from src.agents.referee.flagged_unservable import (
    SOURCE_DESIGN_REVIEW,
    flagged_unservable_explanation,
)
from src.assessment import AssessmentResult
from src.assessment.models import FlagReasonCode, PostSchemaTerminalCause
from src.assessment.result_builder import RunState, post_schema_terminals
from src.contracts.model_flag import FlagReason
from src.contracts.post_schema_router_output import TerminalQuery
from src.storage.local_store import LocalArtifactStore
from tests.assess_models import report_unsupported, write_fake_design
from tests.unit.assessment.test_post_schema_routing import DB, _FlaggingDesigner, _run, _statement

SCOPE = ["dynamodb", "elasticache"]


def _marked(result: AssessmentResult) -> dict[str, Any]:
    return {
        s.query_id: s.post_schema_terminal.model_dump()
        for s in result.statements
        if s.post_schema_terminal is not None
    }


def test_a_flagged_statement_with_no_kept_engine_is_marked_and_counted(tmp_path: Any) -> None:
    """In a scope with no other owner, the flagged DynamoDB read stays on DynamoDB with
    its reason code, marked terminal with cause no_kept_engine, and counted once."""
    designer = _FlaggingDesigner()
    result, _ = _run("pstkept", designer, tmp_path, target_engines=SCOPE)
    assert designer.flagged
    flagged = _statement(result, designer.flagged)
    assert flagged.engine == "dynamodb"
    assert flagged.reason_code != "post_schema_reroute"
    assert _marked(result) == {
        designer.flagged: {
            "engine": "dynamodb",
            # A designer's unsupported pattern is a missing-feature flag.
            "flag_reason": "missing_feature",
            "cause": "no_kept_engine",
            "reason": flagged_unservable_explanation(
                SOURCE_DESIGN_REVIEW, "no_kept_engine", "dynamodb", "missing_feature"
            ),
        }
    }
    assert result.summary.post_schema_terminal_count == 1


def test_the_marker_states_what_the_report_states(tmp_path: Any) -> None:
    """At ``"synthesis"`` the marker's sentence is the report's own row for the
    statement (``flagged_unservable``): the deliverables and the result agree."""
    designer = _FlaggingDesigner()
    result, _ = _run("pstsyn", designer, tmp_path, target_engines=SCOPE, run_through="synthesis")
    assert result.artifacts is not None
    rows = result.artifacts.model_dump()["synthesis_report"]["flagged_unservable"]
    by_query = {r["query_id"]: r for r in rows if r["source"] == SOURCE_DESIGN_REVIEW}
    marked = _marked(result)
    assert list(marked) == [designer.flagged] == list(by_query)
    row, marker = by_query[designer.flagged], marked[designer.flagged]
    assert (marker["engine"], marker["cause"], marker["flag_reason"], marker["reason"]) == (
        row["engine"],
        row["cause"],
        row["reason_category"],
        row["explanation"],
    )
    assert result.summary.post_schema_terminal_count == 1


class _CascadingDesigner(_FlaggingDesigner):
    """Each DynamoDB design reports a different statement unsupported, so every pass
    moves one and the last pass leaves one terminal at its depth limit."""

    def __init__(self) -> None:
        super().__init__()
        self.flagged_ids: list[str] = []

    def __call__(self, **kw: Any) -> None:
        engine, version, store = kw["target_type"], kw["assignment_version"], kw["store"]
        self.calls.append((engine, version))
        if self.first_version is None:
            self.first_version = version
        write_fake_design(kw["job_id"], kw["database_name"], engine, store, version)
        if engine == "dynamodb":
            self.flagged_ids.append(
                report_unsupported(
                    store, kw["database_name"], kw["job_id"], version, skip=self.flagged_ids
                )
            )


def test_a_statement_still_flagged_at_the_last_pass_is_marked_depth_limit(tmp_path: Any) -> None:
    designer = _CascadingDesigner()
    result, store = _run("pstdepth", designer, tmp_path)
    first = designer.first_version
    assert first is not None and len(designer.flagged_ids) == 3
    moved, last = designer.flagged_ids[:2], designer.flagged_ids[2]
    # Two passes moved a statement each; the third pass only marks.
    assert result.summary.assignment_version == first + 2
    for qid in moved:
        assert _statement(result, qid).engine == "aurora_mysql"
        assert _statement(result, qid).post_schema_terminal is None
    stays = _statement(result, last)
    assert stays.engine == "dynamodb" and stays.reason_code != "post_schema_reroute"
    assert _marked(result) == {
        last: {
            "engine": "dynamodb",
            "flag_reason": "missing_feature",
            "cause": "depth_limit",
            "reason": flagged_unservable_explanation(
                SOURCE_DESIGN_REVIEW, "depth_limit", "dynamodb", "missing_feature"
            ),
        }
    }
    assert result.summary.post_schema_terminal_count == 1
    router = store.read_json(f"{DB}/pstdepth/post-schema-router/router_output.json")
    assert [(t["query_id"], t["cause"]) for t in router["terminal"]] == [(last, "depth_limit")]


def test_an_unflagged_run_and_a_moved_statement_have_no_marker(tmp_path: Any) -> None:
    unflagged, _ = _run("pstnone", _FlaggingDesigner(flag=False), tmp_path / "none")
    assert _marked(unflagged) == {}
    assert unflagged.summary.post_schema_terminal_count == 0

    designer = _FlaggingDesigner()
    moved, _ = _run("pstmoved", designer, tmp_path / "moved")
    assert _statement(moved, designer.flagged).reason_code == "post_schema_reroute"
    assert _marked(moved) == {}
    assert moved.summary.post_schema_terminal_count == 0


def _assignments(store: LocalArtifactStore, job_id: str) -> list[dict[str, Any]]:
    out = []
    version = 1
    while store.exists(key := f"{DB}/{job_id}/assignment/v{version}/assignment.json"):
        a = store.read_json(key)
        out.append({k: a.get(k) for k in ("query_assignments", "moves", "unservable_queries")})
        version += 1
    return out


def test_the_marker_changes_no_routing(tmp_path: Any) -> None:
    """The same terminal run with the marker code switched off writes the same
    assignment versions and returns the same result, but for the marker and its count."""
    with_marker, store_a = _run("pstrta", _FlaggingDesigner(), tmp_path / "a", target_engines=SCOPE)
    with patch("src.assessment.result_builder.post_schema_terminals", return_value={}):
        without, store_b = _run("pstrtb", _FlaggingDesigner(), tmp_path / "b", target_engines=SCOPE)
    assert with_marker.summary.post_schema_terminal_count == 1
    assert without.summary.post_schema_terminal_count == 0
    assert _assignments(store_a, "pstrta") == _assignments(store_b, "pstrtb")
    unmarked = {"post_schema_terminal"}
    assert [s.model_dump(exclude=unmarked) for s in with_marker.statements] == [
        s.model_dump(exclude=unmarked) for s in without.statements
    ]
    assert with_marker.summary.model_dump(
        exclude={"post_schema_terminal_count"}
    ) == without.summary.model_dump(exclude={"post_schema_terminal_count"})
    assert with_marker.groups == without.groups


def _state(assignments: list[dict[str, Any]], terminal: list[dict[str, Any]]) -> RunState:
    return RunState(
        run_through="schema_design",
        collector={},
        triage={},
        lineage=[{"version": 3, "query_assignments": assignments}],
        router_terminal=terminal,
    )


def test_a_record_holds_only_while_the_statement_stays_on_the_flagged_engine() -> None:
    state = _state(
        [
            {"query_id": "q-stays", "assigned_engine": "dynamodb"},
            {"query_id": "q-moved", "assigned_engine": "aurora_mysql"},
            {"query_id": "q-out", "assigned_engine": "dynamodb", "in_scope": False},
            {"query_id": "q-plain", "assigned_engine": "dynamodb"},
        ],
        [
            {"query_id": "q-stays", "flagged_engine": "dynamodb", "cause": "no_kept_engine"},
            {"query_id": "q-stays", "flagged_engine": "dynamodb", "cause": "depth_limit"},
            {"query_id": "q-moved", "flagged_engine": "dynamodb", "cause": "no_kept_engine"},
            {"query_id": "q-out", "flagged_engine": "dynamodb", "cause": "no_kept_engine"},
            {"query_id": "q-gone", "flagged_engine": "dynamodb", "cause": "no_kept_engine"},
        ],
    )
    marked = post_schema_terminals(state)
    assert list(marked) == ["q-stays"]
    # The first record counts; a record with no category reads as "other".
    assert (marked["q-stays"].cause, marked["q-stays"].flag_reason) == ("no_kept_engine", "other")


def test_no_record_means_no_marker() -> None:
    assert (
        post_schema_terminals(_state([{"query_id": "q", "assigned_engine": "dynamodb"}], [])) == {}
    )


def test_the_public_codes_are_the_routers() -> None:
    assert set(get_args(FlagReasonCode)) == {r.value for r in FlagReason}
    assert set(get_args(PostSchemaTerminalCause)) == set(
        get_args(TerminalQuery.model_fields["cause"].annotation)
    )
