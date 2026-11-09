# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The step 1 gate's routines check (#1947): every carried routine confirmed by the
customer's check output, a routine step 1 cannot carry failing it, named."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from src.contracts.dms_gate import DmsGate
from src.contracts.routine_check import RoutineCheckResult, RoutineMismatch
from src.tools.data_movement.dms_gate import routines_check, run_gate
from src.tools.data_movement.step1_routines import build_step_routines
from tests.unit.tools.aws.aws_stubs import StubbedCredMgr

from . import gate_stubs as g
from .dms_step_fixtures import make_step1_inputs
from .routine_fixtures import migration, row

INPUTS = make_step1_inputs()


def _inputs(rows=None):
    section = build_step_routines(
        migration(rows, "mysql") if rows else None, INPUTS.contract, version=2 if rows else None
    )
    return dataclasses.replace(
        INPUTS, contract=INPUTS.contract.model_copy(update={"routines": section})
    )


def _result(inputs, mismatches=(), sha=None, confirmed=1):
    section = inputs.contract.routines
    nc = [{"routine_id": r.routine_id, "kind": r.kind, "reasons": r.reasons}
          for r in section.routines if r.carry == "not_carried"]  # fmt: skip
    return RoutineCheckResult(
        job_id="job1", database_name="shop", target_engine="aurora_mysql",
        contract_sha256=sha or inputs.contract_sha256, checked_at=datetime(2026, 11, 9, tzinfo=UTC),
        status="fail" if mismatches or nc else "pass", carried=section.carried,
        confirmed=confirmed, deferred=section.deferred, rows_read=1,
        mismatches=list(mismatches), not_carried=nc,
    )  # fmt: skip


_CARRIED = [row("shop.p", "procedure", schema="shop", status="rewritten", signature="")]


def test_an_old_contract_has_no_routines_check():
    assert routines_check(INPUTS, None) is None
    cred = StubbedCredMgr()
    g.queue(cred, INPUTS.contract_sha256)
    assert [c.id for c in run_gate(INPUTS, g.request(), g.clients(cred), now=g.NOW).checks] == [
        "load", "lag", "parity"]  # fmt: skip


def test_no_routines_passes():
    check = routines_check(_inputs(), None)
    assert (check.status, check.summary) == ("pass", "routines: none in the collection")


def test_every_carried_routine_confirmed_passes_labelled_static():
    inputs = _inputs(_CARRIED)
    check = routines_check(inputs, _result(inputs))
    assert check.status == "pass" and check.reasons == []
    assert (check.evidence["proof"], check.evidence["label"]) == (
        "static",
        "checked, not load-tested",
    )
    cred = StubbedCredMgr()
    g.queue(cred, inputs.contract_sha256)
    gate = run_gate(inputs, g.request(), g.clients(cred), now=g.NOW, routines=_result(inputs))
    assert [c.id for c in gate.checks] == ["load", "lag", "parity", "routines"]


def test_without_the_check_output_the_check_is_pending():
    check = routines_check(_inputs(_CARRIED), None)
    assert check.status == "pending" and "routines-check" in check.reasons[0]


def test_a_check_read_for_another_contract_is_pending():
    inputs = _inputs(_CARRIED)
    assert routines_check(inputs, _result(inputs, sha="f" * 64)).status == "pending"


def test_a_mismatch_fails_naming_the_routine():
    inputs = _inputs(_CARRIED)
    miss = RoutineMismatch(routine_id="shop.p", kind="procedure", problem="signature_mismatch")
    check = routines_check(inputs, _result(inputs, [miss], confirmed=0))
    assert (check.status, check.reasons) == (
        "fail",
        ["routines: procedure shop.p: signature mismatch"],
    )


def test_a_routine_that_cannot_be_carried_fails_even_before_any_check():
    inputs = _inputs([*_CARRIED, row("shop.q", "procedure", schema="shop", status="needs_attention",
                                     reasons=["mysql-routine-set-global"])])  # fmt: skip
    check = routines_check(inputs, None)
    assert check.status == "fail"
    assert "routines: procedure shop.q: not carried: mysql-routine-set-global" in check.reasons


def test_many_not_carried_routines_are_named_up_to_a_limit_then_counted():
    rows = [row(f"shop.q{i}", "procedure", schema="shop", status="needs_attention", reasons=["x"])
            for i in range(60)]  # fmt: skip
    check = routines_check(_inputs(rows), None)
    assert len(check.reasons) == 51 and check.reasons[-1].startswith("routines: and 10 more")


def test_the_gate_refuses_checks_out_of_order():
    data = {"job_id": "j", "database_name": "d", "region": "us-east-1", "stack_name": "s",
            "task_arn": "t", "checked_at": "2026-11-09T00:00:00Z", "window_minutes": 15,
            "max_lag_seconds": 60, "contract_sha256": "a" * 64, "status": "pass",
            "cutover_message_id": "m"}  # fmt: skip
    check = {"status": "pass", "summary": "ok"}
    ids = ["load", "lag", "parity", "routines"]
    assert DmsGate(**data, checks=[{**check, "id": i} for i in ids]).checks[-1].id == "routines"
    with pytest.raises(ValidationError, match="checks must be"):
        DmsGate(**data, checks=[{**check, "id": i} for i in ["routines", "load", "lag", "parity"]])
