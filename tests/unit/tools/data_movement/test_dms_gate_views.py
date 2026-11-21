# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The step 1 gate's views check and view exceptions (#2082): every carried view confirmed
by the customer's check output; a view step 1 does not create named, with what to do."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from src.contracts.dms_gate import NOT_VALIDATED, VIEW_CARRIED, DmsGate, GateException
from src.contracts.view_check import ViewCheckResult, ViewMismatch
from src.tools.data_movement.dms_gate import run_gate, view_exceptions, views_check, write_gate
from src.tools.data_movement.step1_routines import build_step_routines
from tests.unit.tools.aws.aws_stubs import StubbedCredMgr

from . import gate_stubs as g
from .dms_step_fixtures import make_step1_inputs
from .routine_fixtures import migration, row
from .view_fixtures import contract_with_views, view

INPUTS = make_step1_inputs()


def _inputs(rows):
    contract = contract_with_views(rows, "aurora_mysql", contract=INPUTS.contract)
    return dataclasses.replace(INPUTS, contract=contract)


def _result(inputs, mismatches=(), sha=None):
    section = inputs.contract.views_section
    return ViewCheckResult(
        job_id="job1", database_name="shop", target_engine="aurora_mysql",
        contract_sha256=sha or inputs.contract_sha256, checked_at=datetime(2026, 11, 15, tzinfo=UTC),
        status="fail" if mismatches else "pass", carried=section.carried,
        confirmed=section.carried - len(mismatches), rows_read=1, mismatches=list(mismatches),
        not_carried=[{"view_id": v.view_id, "reasons": v.reasons}
                     for v in section.views if v.carry == "not_carried"],
    )  # fmt: skip


_ROWS = [
    view("v_orders", engine="aurora_mysql"),
    view("v_far", engine="aurora_mysql", tables=["shop.gone"]),
]


def _gate(inputs, **kw):
    cred = StubbedCredMgr()
    g.queue(cred, inputs.contract_sha256)
    return run_gate(inputs, g.request(), g.clients(cred), now=g.NOW, **kw)


def test_an_old_contract_keeps_its_checks_and_its_fixed_view_exception():
    assert views_check(INPUTS, None) is None
    assert [(e.table, e.reason) for e in view_exceptions(INPUTS)] == [
        ("shop.v_orders", NOT_VALIDATED)
    ]
    assert [c.id for c in _gate(INPUTS).checks] == ["load", "lag", "parity"]


def test_a_carried_view_is_pending_until_its_check_is_read_then_passes():
    inputs = _inputs(_ROWS)
    check = views_check(inputs, None)
    assert check.status == "pending" and "views-check.sql" in check.reasons[0]
    other = views_check(inputs, _result(inputs, sha="c" * 64))
    assert other.status == "pending" and "another step contract" in other.reasons[0]
    passed = views_check(inputs, _result(inputs))
    assert (passed.status, passed.reasons, passed.evidence["confirmed"]) == ("pass", [], "1")
    gate = _gate(inputs, views=_result(inputs))
    assert [c.id for c in gate.checks] == ["load", "lag", "parity", "views"]
    DmsGate.model_validate(gate.model_dump(mode="json"))


def test_a_mismatch_fails_naming_the_view_and_its_columns():
    inputs = _inputs(_ROWS)
    bad = _result(
        inputs, [ViewMismatch(view_id="v_orders", problem="columns_differ", columns=["id"])]
    )
    check = views_check(inputs, bad)
    assert (check.status, check.reasons) == ("fail", ["views: v_orders: columns differ (id)"])


def test_a_view_not_carried_is_a_named_exception_with_what_to_do_not_a_failure():
    inputs = _inputs(_ROWS)
    exceptions = {e.table: e.reason for e in view_exceptions(inputs)}
    assert exceptions["v_orders"] == VIEW_CARRIED
    assert exceptions["v_far"].startswith(
        "not carried: depends_on_table_not_in_step: it reads a table"
    )
    assert views_check(inputs, _result(inputs)).status == "pass"
    gate = _gate(inputs, views=_result(inputs))
    assert {e.table for e in gate.exceptions if e.kind == "view"} == {"v_orders", "v_far"}


def test_no_view_carried_passes_without_a_file():
    inputs = _inputs([view("v_far", engine="aurora_mysql", tables=["shop.gone"])])
    check = views_check(inputs, None)
    assert (check.status, check.summary) == ("pass", "views: 0 of 0 carried views confirmed; 1 view "
                                                     "not created on Aurora by step 1: create each "
                                                     "yourself (the exceptions name each one and "
                                                     "why)")  # fmt: skip
    assert views_check(_inputs([]), None).summary == "views: none in the collection"


def test_the_views_check_runs_before_the_routines_check():
    inputs = _inputs(_ROWS)
    section = build_step_routines(migration([row("shop.p", "procedure", schema="shop", status="rewritten",
                                                 signature="")], "mysql"), inputs.contract, version=2)  # fmt: skip
    both = dataclasses.replace(
        inputs, contract=inputs.contract.model_copy(update={"routines": section})
    )
    assert [c.id for c in _gate(both).checks] == ["load", "lag", "parity", "views", "routines"]


def test_a_view_exception_reason_is_one_of_the_three_forms():
    GateException(table="v", kind="view", reason=VIEW_CARRIED)
    GateException(table="v", kind="view", reason="not carried: body_opted_out: collect again")
    with pytest.raises(ValidationError):
        GateException(table="v", kind="view", reason="anything else")
    with pytest.raises(ValidationError):
        DmsGate.model_validate({**_gate(_inputs(_ROWS)).model_dump(mode="json"),
                                "checks": [c.model_dump() for c in reversed(_gate(_inputs(_ROWS)).checks)]})  # fmt: skip


def test_write_gate_reads_the_views_check_file(tmp_path, monkeypatch):
    from src.storage.local_store import LocalArtifactStore
    from src.tools.data_movement import dms_gate

    inputs = _inputs(_ROWS)
    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json("shop/job1/data-movement/step-1/views-check.json",
                     _result(inputs, [ViewMismatch(view_id="v_orders", problem="missing")]).model_dump(mode="json"))  # fmt: skip
    monkeypatch.setattr(dms_gate, "load_step1", lambda *a, **k: inputs)
    cred = StubbedCredMgr()
    g.queue(cred, inputs.contract_sha256)
    _, gate = write_gate(store, "shop", "job1", g.request(), g.clients(cred), now=g.NOW)
    views = next(c for c in gate.checks if c.id == "views")
    assert (views.status, views.reasons) == ("fail", ["views: v_orders: missing"])


def test_a_carried_view_the_script_leaves_out_says_why_in_its_exception(tmp_path, monkeypatch):
    """No VIEW_CARRIED for a view views-deploy.sql refused: the gate reads the same
    refusal from the job's routine sources, and counts it among the views to create."""
    from src.storage.local_store import LocalArtifactStore
    from src.tools.data_movement import dms_gate

    inputs = _inputs(_ROWS)  # no routine sources: v_orders is source_missing
    store = LocalArtifactStore(base_dir=str(tmp_path))
    monkeypatch.setattr(dms_gate, "load_step1", lambda *a, **k: inputs)
    cred = StubbedCredMgr()
    g.queue(cred, inputs.contract_sha256)
    _, gate = write_gate(store, "shop", "job1", g.request(), g.clients(cred), now=g.NOW)
    reasons = {e.table: e.reason for e in gate.exceptions if e.kind == "view"}
    assert reasons["v_orders"].startswith(
        "not carried: left out of views-deploy.sql, source_missing:"
    )
    assert "collect again" in reasons["v_orders"]
    views = next(c for c in gate.checks if c.id == "views")
    assert "2 views not created on Aurora by step 1: create each yourself" in views.summary
    assert {e.table: e.reason for e in view_exceptions(inputs, {})}["v_orders"] == VIEW_CARRIED


def test_the_views_the_gate_names_use_the_checked_target_database(tmp_path, monkeypatch):
    """#2530: a target database name in the inputs file is refused on a step that keeps each
    source database, as in every script of step 1, not passed on to the view check."""
    from src.storage.local_store import LocalArtifactStore
    from src.tools.data_movement import dms_gate, dms_step

    inputs = _inputs(_ROWS)
    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json(
        "shop/job1/data-movement/step-1/inputs.json", {"target_database_name": "other"}
    )
    assert dms_gate._views_left_out(store, "shop", "job1", inputs) is not None
    monkeypatch.setattr(dms_step, "keeps_schemas", lambda contract: True)
    with pytest.raises(dms_step.DmsStepError, match="target database name does not apply"):
        dms_gate._views_left_out(store, "shop", "job1", inputs)
