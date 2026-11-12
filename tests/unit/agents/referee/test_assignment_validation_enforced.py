# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A hard validation error stops the version from being written (#2152).

Every pass that writes an assignment version refuses one the validator rejects: a cache
or non-system-of-record owner, a placeholder engine, an engine that did not analyse the
query, and a co-dependency group, write group or routine unit on more than one engine
(ADR-031 Decision 4, Amendments 9 and 21). The check fails closed: when the units cannot
be built, the version is refused, never written unchecked. A customer's own split of a
unit stands while every engine it is on is one the customer picked (Amendment 21), and
a split a version saved before units were placed whole already had is carried as a
warning while the unit gains no engine, on every version derived from it.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from src.agents.analysis.handler import run_analysis
from src.agents.referee import assignment_handler, assignment_overrides, assignment_validator
from src.agents.referee import reality_check_handler as rc_handler
from src.agents.referee.assignment_handler import run_assignment_resolver
from src.agents.referee.assignment_overrides import (
    AssignmentValidationFailed,
    QueryOverrideInput,
    apply_assignment_overrides,
)
from src.agents.referee.assignment_resolver import build_co_dependency_groups
from src.agents.referee.assignment_validator import AssignmentValidator
from src.agents.referee.move_ledger import apply_to_assignment
from src.agents.referee.post_schema_routing_handler import PostSchemaRouting
from src.agents.referee.reality_check_handler import run_reality_check_handler
from src.agents.referee.triage_handler import run_triage
from src.contracts.assignment_models import Assignment
from src.contracts.move_ledger import MovePass, ReasonCode
from src.contracts.post_schema_router_output import QueryRouting
from src.storage.local_store import LocalArtifactStore

REPO = Path(__file__).resolve().parents[4]
FIXTURE = REPO / "tests" / "fixtures" / "sqlserver_routine_units_collection.json"
DB = "RoutineUnits"
JOB = "job-2152"
# The two application reads given a significant join, so they form a co-dependency group.
JOINED = ("app-order-get", "app-order-total")
# A write group: dbo.Inventory's two writes.
INVENTORY_WRITES = ("app-restock", "po-take-stock")
# dbo.usp_PlaceOrder's statements; po-insert-item writes a table nothing else writes.
PROCEDURE = ("po-insert-order", "po-insert-item", "po-take-stock")


def _collection() -> dict:
    collection: dict = json.loads(FIXTURE.read_text())
    for q in collection["queries"]["query_patterns"]:
        if q["query_id"] in JOINED:
            q["tables_accessed"] = ["dbo.Orders", "dbo.OrderItems"]
            q["has_joins"] = True
            q["join_count"] = 2
    return collection


def _prepared(base: Path) -> LocalArtifactStore:
    """Collection, triage and every selected engine's analysis: what the first
    assignment reads."""
    store = LocalArtifactStore(base_dir=str(base))
    store.write_json(f"{DB}/{JOB}/collector/output.json", _collection())
    run_triage(JOB, DB, store)
    triage = store.read_json(f"{DB}/{JOB}/referee-triage/triage.json")
    for engine in [a["agent_type"] for a in triage["selected_agents"]]:
        run_analysis(JOB, DB, engine, store, llm_mode="none")
    return store


def _path(version: int) -> str:
    return f"{DB}/{JOB}/assignment/v{version}/assignment.json"


def _engines(raw: dict) -> dict[str, str]:
    return {qa["query_id"]: qa["assigned_engine"] for qa in raw["query_assignments"]}


def _other(engine: str) -> str:
    """An engine other than ``engine`` that may own any of these statements."""
    return "dynamodb" if engine != "dynamodb" else "aurora_postgresql"


@pytest.fixture
def prepared(tmp_path) -> LocalArtifactStore:
    return _prepared(tmp_path)


@pytest.fixture
def routed(tmp_path) -> LocalArtifactStore:
    """The deterministic first assignment, v1, as routing wrote it."""
    store = _prepared(tmp_path)
    run_assignment_resolver(JOB, DB, store)
    return store


def _resolving_with(mp: pytest.MonkeyPatch, change: Callable[[Assignment], dict[str, str]]) -> None:
    """The real first assignment, with ``change``'s ``{query_id: engine}`` written over it
    after routing, as a routing defect would."""
    original = assignment_handler.AssignmentResolver.resolve

    def resolve(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        assignment = original(self, *args, **kwargs)
        moved = change(assignment)
        qas = [
            (
                qa.model_copy(update={"assigned_engine": moved[qa.query_id]})
                if qa.query_id in moved
                else qa
            )
            for qa in assignment.query_assignments
        ]
        return assignment.model_copy(update={"query_assignments": qas})

    mp.setattr(assignment_handler.AssignmentResolver, "resolve", resolve)


def _engine_of(assignment: Assignment, qid: str) -> str:
    return next(qa.assigned_engine for qa in assignment.query_assignments if qa.query_id == qid)


def _refused(refused: pytest.ExceptionInfo[RuntimeError]) -> Any:
    """The refusal, which must be ``assignment_validator.InvalidAssignment`` (a
    ``RuntimeError``; named, not imported, so this file loads on main's code)."""
    assert type(refused.value).__name__ == "InvalidAssignment", repr(refused.value)
    return refused.value


def _refused_first_assignment(store: LocalArtifactStore, needle: str) -> Any:
    with pytest.raises(RuntimeError) as refused:
        run_assignment_resolver(JOB, DB, store)
    exc = _refused(refused)
    assert any(needle in e for e in exc.errors), exc.errors
    # The commands and the UI show the exception's text: it names the pass and the error.
    assert str(exc).startswith("first assignment: assignment validation failed")
    assert needle in str(exc)
    assert not store.exists(_path(1))
    assert not store.exists(f"{DB}/{JOB}/assignment/v1/validation.json")
    return exc


# ------------------------------------------------------- the routing itself is valid


@pytest.mark.passes_on_main  # no drift: routing already keeps the units whole
def test_the_routed_first_assignment_keeps_every_unit_whole(routed) -> None:
    """No drift: the router places each unit on one engine, so its version is written."""
    raw = routed.read_json(_path(1))
    validation = routed.read_json(f"{DB}/{JOB}/assignment/v1/validation.json")
    assert validation["valid"] and validation["errors"] == []
    engines = _engines(raw)
    assert len({engines[q] for q in JOINED}) == 1
    assert len({engines[q] for q in INVENTORY_WRITES}) == 1
    assert len({engines[q] for q in PROCEDURE}) == 1
    # The join really is a co-dependency group, so the tests below split a real one.
    collector = routed.read_json(f"{DB}/{JOB}/collector/output.json")
    groups = build_co_dependency_groups(
        collector["queries"]["query_patterns"], collector["database_schema"]["tables"]
    )
    assert sorted(JOINED) in [sorted(g) for g in groups]


# ------------------------------------------- the first assignment, one per error kind


def test_a_cache_owner_stops_the_first_assignment(prepared, monkeypatch) -> None:
    _resolving_with(monkeypatch, lambda a: {"app-stock-report": "elasticache"})
    _refused_first_assignment(prepared, "is owned by 'elasticache', the cache layer")


def test_a_write_on_an_engine_that_is_no_system_of_record_stops_it(prepared, monkeypatch) -> None:
    _resolving_with(monkeypatch, lambda a: {"po-insert-item": "opensearch"})
    _refused_first_assignment(prepared, "Query po-insert-item is a write owned by OpenSearch")


def test_a_placeholder_engine_stops_it(prepared, monkeypatch) -> None:
    _resolving_with(monkeypatch, lambda a: {"app-stock-report": "neptune"})
    _refused_first_assignment(prepared, "a future placeholder engine")


def test_an_engine_that_did_not_analyse_the_query_stops_it(prepared, monkeypatch) -> None:
    # DocumentDB's analysis is missing (its run failed), and a query is put on it anyway.
    Path(prepared.base_dir, DB, JOB, "analysis-documentdb", "analysis.json").unlink()
    _resolving_with(monkeypatch, lambda a: {"app-stock-report": "documentdb"})
    exc = _refused_first_assignment(
        prepared, "DocumentDB has no analysis (it failed or did not run), yet 1 statement"
    )
    assert any("Run its analysis again, then the assignment." in e for e in exc.errors)


def test_a_co_dependency_group_on_two_engines_stops_it(prepared, monkeypatch) -> None:
    _resolving_with(monkeypatch, lambda a: {JOINED[1]: _other(_engine_of(a, JOINED[0]))})
    exc = _refused_first_assignment(prepared, "form one placement unit")
    assert any(all(q in e for q in JOINED) for e in exc.errors)


def test_a_write_group_on_two_engines_stops_it(prepared, monkeypatch) -> None:
    _resolving_with(
        monkeypatch,
        lambda a: {INVENTORY_WRITES[0]: _other(_engine_of(a, INVENTORY_WRITES[1]))},
    )
    exc = _refused_first_assignment(prepared, "a table has one writer")
    assert any(all(q in e for q in INVENTORY_WRITES) for e in exc.errors)


def test_a_routine_unit_on_two_engines_stops_it(prepared, monkeypatch) -> None:
    _resolving_with(
        monkeypatch, lambda a: {"po-insert-item": _other(_engine_of(a, "po-insert-order"))}
    )
    exc = _refused_first_assignment(prepared, "form one placement unit")
    # The unit's six members: three ids, then the count, so the line stays short.
    (unit_error,) = [e for e in exc.errors if "form one placement unit" in e]
    assert "and 3 more" in unit_error


def test_units_that_cannot_be_built_stop_it(prepared, monkeypatch) -> None:
    """Fails closed: a grouping error is a hard error, never a skipped check."""

    def broken(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("grouping failed")

    monkeypatch.setattr(assignment_validator, "placement_groups", broken)
    _refused_first_assignment(prepared, "could not be built (RuntimeError)")


# ------------------------------------------------------------- every later version


def _sweep_moving(mp: pytest.MonkeyPatch, qid: str, engine: str) -> None:
    """The sanity sweep, then a move of ``qid`` to ``engine`` through the ledger: a
    reality-check pass that splits the unit ``qid`` is in."""
    original = rc_handler.sanity_sweep

    def sweep(revised, consolidations, *args, ledger, **kwargs):  # noqa: ANN001, ANN003
        revised, consolidations, decisions = original(
            revised, consolidations, *args, ledger=ledger, **kwargs
        )
        for qa in revised:
            if qa["query_id"] == qid and qa["assigned_engine"] != engine:
                apply_to_assignment(
                    ledger,
                    qa,
                    engine,
                    pass_=MovePass.SWEEP,
                    reason_code=ReasonCode.SWEEP_ORPHAN,
                    score_after=50.0,
                )
        return revised, consolidations, decisions

    mp.setattr(rc_handler, "sanity_sweep", sweep)


def test_a_reality_check_that_splits_a_unit_writes_no_version(routed, monkeypatch) -> None:
    engine = _engines(routed.read_json(_path(1)))["po-insert-order"]
    _sweep_moving(monkeypatch, "po-insert-item", _other(engine))
    with pytest.raises(RuntimeError) as refused:
        run_reality_check_handler(JOB, DB, routed, llm_mode="none")
    exc = _refused(refused)
    assert str(exc).startswith("reality check: ")
    assert any("form one placement unit" in e for e in exc.errors)
    assert not routed.exists(_path(2))


def test_a_post_schema_reroute_that_splits_a_unit_is_refused(routed) -> None:
    raw = routed.read_json(_path(1))
    engine = _engines(raw)["po-insert-order"]
    routing = QueryRouting(
        query_id="po-insert-item",
        from_engine=engine,
        to_engine=_other(engine),
        reason="the design cannot serve it",
        score_after=50.0,
    )
    handler = PostSchemaRouting(routed, redesign=lambda *a, **k: None)
    with pytest.raises(RuntimeError) as refused:
        handler._rerouted_assignment(JOB, DB, raw, 1, 2, [routing])
    exc = _refused(refused)
    assert str(exc).startswith("post-schema reroute: ")
    assert any("form one placement unit" in e for e in exc.errors)


def test_a_customer_edit_whose_grouping_fails_is_refused(routed, monkeypatch) -> None:
    """#2154's swallow on this path: an error building the units refuses the edit
    instead of skipping the one-writer rejection and the group propagation."""

    def broken(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("grouping failed")

    monkeypatch.setattr(assignment_overrides, "placement_groups", broken)
    engine = _engines(routed.read_json(_path(1)))["app-restock"]
    with pytest.raises(AssignmentValidationFailed) as refused:
        apply_assignment_overrides(
            routed, DB, JOB, [QueryOverrideInput("app-restock", assigned_engine=_other(engine))]
        )
    assert any("could not be built (RuntimeError)" in e for e in refused.value.errors)
    assert not routed.exists(_path(2))


@pytest.mark.passes_on_main
def test_a_customer_split_of_a_unit_stands(routed) -> None:
    """Amendment 21: a customer edit that picks two engines inside one unit is the
    customer's choice; it is written, and the feasibility review names it."""
    engine = _engines(routed.read_json(_path(1)))["po-insert-order"]
    result = apply_assignment_overrides(
        routed,
        DB,
        JOB,
        [
            QueryOverrideInput("po-insert-item", assigned_engine=_other(engine)),
            QueryOverrideInput("po-insert-order", assigned_engine=engine),
        ],
    )
    engines = _engines(routed.read_json(_path(result.assignment.version)))
    assert engines["po-insert-item"] != engines["po-insert-order"]


# ----------------------------------------- a saved version that splits a unit (mutation)


def _split_saved_group(store: LocalArtifactStore, *, legacy: bool = False) -> dict:
    """v1 as saved, with the co-dependency group's second member moved to another engine
    in the file (the claims audit's mutation)."""
    raw = store.read_json(_path(1))
    engine = _engines(raw)[JOINED[0]]
    for qa in raw["query_assignments"]:
        if qa["query_id"] == JOINED[1]:
            qa["assigned_engine"] = _other(engine)
    if legacy:
        raw.pop("contract_version", None)  # saved before units were placed whole
    store.write_json(_path(1), raw)
    return raw


def test_a_saved_version_with_a_split_group_fails_revalidation(routed) -> None:
    raw = _split_saved_group(routed)
    collector = routed.read_json(f"{DB}/{JOB}/collector/output.json")
    analysis = {
        e: routed.read_json(f"{DB}/{JOB}/analysis-{e}/analysis.json")
        for e in {qa["assigned_engine"] for qa in raw["query_assignments"]}
        if routed.exists(f"{DB}/{JOB}/analysis-{e}/analysis.json")
    }
    result = AssignmentValidator().validate(Assignment.model_validate(raw), collector, analysis)
    assert not result.valid
    assert any(
        "form one placement unit" in e and all(q in e for q in JOINED) for e in result.errors
    )


def test_an_edit_of_a_saved_version_with_a_split_group_is_refused(routed) -> None:
    _split_saved_group(routed)
    with pytest.raises(AssignmentValidationFailed) as refused:
        apply_assignment_overrides(
            routed, DB, JOB, [QueryOverrideInput("app-stock-report", in_scope=False)]
        )
    assert str(refused.value).startswith("customer gate: ")
    assert any(all(q in e for q in JOINED) for e in refused.value.errors)
    assert not routed.exists(_path(2))


CARRIED = "this job was saved before that rule"


def test_a_split_saved_before_units_were_whole_is_carried_version_after_version(routed) -> None:
    """Checklist 6: a job saved before the rule still edits, again and again; each new
    version carries the old split as a warning, never as an error."""
    _split_saved_group(routed, legacy=True)
    for scope in (False, True, False):
        result = apply_assignment_overrides(
            routed, DB, JOB, [QueryOverrideInput("app-stock-report", in_scope=scope)]
        )
        version = result.assignment.version
        assert routed.exists(_path(version))
        carried = [w for w in result.assignment.validation_warnings if CARRIED in w]
        assert len(carried) == 1 and all(q in carried[0] for q in JOINED), carried
    assert version == 4  # three edits on v1, the last two from versions stamped today


def test_a_carried_split_that_gains_an_engine_is_an_error(routed) -> None:
    """A split old routing made is carried only while its unit stays on engines it had:
    a member the pass moves to a third engine is an error."""
    from src.agents.referee.assignment_validator import legacy_baseline  # new in #2152

    legacy_raw = _split_saved_group(routed, legacy=True)
    on = {_engines(legacy_raw)[q] for q in JOINED}
    third = next(e for e in ("documentdb", "dynamodb", "aurora_postgresql") if e not in on)
    moved = json.loads(json.dumps(legacy_raw))
    for qa in moved["query_assignments"]:
        if qa["query_id"] == JOINED[1]:
            qa["assigned_engine"] = third
    moved["contract_version"] = "1.26"
    result = _validate(routed, moved, legacy=legacy_baseline([legacy_raw]))
    assert any(
        "form one placement unit" in e and all(q in e for q in JOINED) for e in result.errors
    )
    assert not any(CARRIED in w for w in result.warnings)
    # Unmoved, the same version carries it.
    kept = _validate(
        routed, {**legacy_raw, "contract_version": "1.26"}, legacy=legacy_baseline([legacy_raw])
    )
    assert kept.valid and any(CARRIED in w for w in kept.warnings)


def _validate(store: LocalArtifactStore, raw: dict, **kwargs: Any) -> Any:
    collector = store.read_json(f"{DB}/{JOB}/collector/output.json")
    analysis = {
        e: store.read_json(f"{DB}/{JOB}/analysis-{e}/analysis.json")
        for e in ("aurora_mysql", "aurora_postgresql", "dynamodb", "documentdb", "opensearch")
        if store.exists(f"{DB}/{JOB}/analysis-{e}/analysis.json")
    }
    return AssignmentValidator().validate(
        Assignment.model_validate(raw), collector, analysis, **kwargs
    )


def _picked(raw: dict, picks: dict[str, str], moves: dict[str, str] | None = None) -> dict:
    """``raw`` with ``picks`` set as customer picks and ``moves`` set as routing would."""
    out: dict = json.loads(json.dumps(raw))
    for qa in out["query_assignments"]:
        if qa["query_id"] in picks:
            qa["assigned_engine"] = picks[qa["query_id"]]
            qa["customer_override"] = True
        if qa["query_id"] in (moves or {}):
            qa["assigned_engine"] = (moves or {})[qa["query_id"]]
    return out


def test_a_customer_split_leaves_no_unpicked_member_on_a_third_engine(routed) -> None:
    """The customer exception covers only the engines the customer picked: an unpicked
    member on any other engine is an error."""
    raw = routed.read_json(_path(1))
    engine = _engines(raw)["po-insert-order"]
    other = _other(engine)
    third = next(e for e in ("documentdb", "opensearch") if e not in (engine, other))
    picks = {"po-insert-item": other, "po-insert-order": engine}
    assert _validate(routed, _picked(raw, picks)).valid
    result = _validate(routed, _picked(raw, picks, {"trg-audit": third}))
    assert any("form one placement unit" in e for e in result.errors), result.errors


def test_a_later_pass_cannot_hide_behind_a_customer_split(routed, monkeypatch) -> None:
    """``customer_override`` stays set on later versions: a reality check that moves an
    unpicked member of a customer-split unit to a third engine is still refused."""
    engine = _engines(routed.read_json(_path(1)))["po-insert-order"]
    other = _other(engine)
    result = apply_assignment_overrides(
        routed,
        DB,
        JOB,
        [
            QueryOverrideInput("po-insert-item", assigned_engine=other),
            QueryOverrideInput("po-insert-order", assigned_engine=engine),
        ],
    )
    assert result.assignment.version == 2
    third = next(e for e in ("documentdb", "opensearch") if e not in (engine, other))
    _sweep_moving(monkeypatch, "trg-audit", third)
    with pytest.raises(RuntimeError) as refused:
        run_reality_check_handler(JOB, DB, routed, llm_mode="none", assignment_version=2)
    exc = _refused(refused)
    assert any("form one placement unit" in e for e in exc.errors)
    assert not routed.exists(_path(3))


def test_a_customer_split_of_a_tables_writes_is_an_error(routed) -> None:
    """The customer exception never covers a table's writes (ADR-031 Amendment 9)."""
    raw = routed.read_json(_path(1))
    engine = _engines(raw)[INVENTORY_WRITES[1]]
    picks = {INVENTORY_WRITES[0]: _other(engine), INVENTORY_WRITES[1]: engine}
    result = _validate(routed, _picked(raw, picks))
    assert any("a table has one writer" in e for e in result.errors), result.errors


def test_a_split_group_is_reported_once(prepared, monkeypatch) -> None:
    """A co-dependency group in a refused unit gives the error only, not the old
    warning as well."""
    _resolving_with(monkeypatch, lambda a: {JOINED[1]: _other(_engine_of(a, JOINED[0]))})
    exc = _refused_first_assignment(prepared, "form one placement unit")
    assert not any("Co-dependent queries" in w for w in exc.warnings)


def test_assess_reports_a_refused_version_as_a_run_error(monkeypatch) -> None:
    """``assess()`` raises its own ``AssessmentRunError`` with the refusal's message."""
    from src.assessment import assess
    from src.assessment.runner import AssessmentRunError
    from tests.unit.assessment.conftest import collection

    _resolving_with(monkeypatch, lambda a: {a.query_assignments[0].query_id: "neptune"})
    with pytest.raises(AssessmentRunError) as refused:
        assess({"database_name": "trailgear", "collection": collection("trailgear")})
    message = str(refused.value)
    assert message.startswith("first assignment: assignment validation failed")
    assert "a future placeholder engine" in message and message.endswith("; no result built")
    assert type(refused.value.__cause__).__name__ == "InvalidAssignment"


def test_a_post_schema_rollback_that_restates_a_split_is_refused(routed) -> None:
    """The rollback is checked as every writer is: a restated version that splits a
    unit (here, a saved file edited by hand) is refused before it is written."""
    raw = _split_saved_group(routed)
    handler = PostSchemaRouting(routed, redesign=lambda *a, **k: None)
    with pytest.raises(RuntimeError) as refused:
        handler._check_restated(JOB, DB, raw, {**raw, "version": 3, "previous_version": 2})
    exc = _refused(refused)
    assert str(exc).startswith("post-schema rollback: ")
    assert any(all(q in e for q in JOINED) for e in exc.errors)
