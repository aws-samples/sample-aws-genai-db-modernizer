# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The step 1 gate result (#1800)."""

from __future__ import annotations

import datetime as dt

import pytest

from src.contracts.dms_gate import NOT_VALIDATED, DmsGate
from src.shared.contract_versioning import contract_for_artifact, current_version


def _gate(status="pass", statuses=("pass", "pass", "pass"), **extra):
    return {"job_id": "job1", "database_name": "shop", "region": "us-east-1", "stack_name": "job1-dms-step-1",
            "task_arn": "arn:aws:dms:us-east-1:111122223333:task:ABCDEFGHIJKLMNOP",
            "checked_at": dt.datetime(2026, 11, 3, tzinfo=dt.UTC), "window_minutes": 15, "max_lag_seconds": 5,
            "contract_sha256": "a" * 64, "status": status, "cutover_message_id": "cutover-decision-v1",
            "checks": [{"id": i, "status": s, "summary": "x"} for i, s in zip(("load", "lag", "parity"), statuses, strict=True)],
            **extra}  # fmt: skip


def test_the_decision_is_always_the_customers():
    g = DmsGate.model_validate(_gate())
    assert g.cutover_decision == "customer" and g.contract_version == current_version(DmsGate)
    with pytest.raises(ValueError):
        DmsGate.model_validate(_gate(cutover_decision="go"))


def test_status_is_the_worst_check():
    assert DmsGate.model_validate(_gate("pending", ("pass", "pending", "pass"))).status == "pending"
    with pytest.raises(ValueError, match="status"):
        DmsGate.model_validate(_gate("pass", ("pass", "pending", "fail")))
    with pytest.raises(ValueError, match="checks"):
        DmsGate.model_validate({**_gate(), "checks": _gate()["checks"][::-1]})


def test_exceptions_carry_the_fixed_reason():
    exc = {"table": "shop.audit_log", "kind": "table", "reason": NOT_VALIDATED, "rows_loaded": 12}
    assert DmsGate.model_validate(_gate(exceptions=[exc])).exceptions[0].rows_loaded == 12
    assert (
        DmsGate.model_validate(_gate(exceptions=[{"table": "shop.v", "kind": "view"}]))
        .exceptions[0]
        .reason
        == NOT_VALIDATED
    )
    with pytest.raises(ValueError):
        DmsGate.model_validate(_gate(exceptions=[{**exc, "reason": "skipped"}]))
    with pytest.raises(ValueError):
        DmsGate.model_validate(_gate(exceptions=[{**exc, "kind": "view", "reason": "other"}]))


def test_named_exceptions_beyond_unvalidated_tables():
    lob = {
        "table": "shop.docs",
        "kind": "keyless_lob",
        "reason": "no primary key: DMS can lose LOB values",
        "rows_loaded": 3,
    }
    field = {"table": "shop.t", "kind": "unsupported_field", "column": "d", "reason": "zero date"}
    skipped = {"table": "shop.s", "kind": "skipped_rule", "reason": "blocked by an error finding"}
    g = DmsGate.model_validate(_gate(exceptions=[lob, field, skipped]))
    assert [e.kind for e in g.exceptions] == ["keyless_lob", "unsupported_field", "skipped_rule"]
    with pytest.raises(ValueError, match="column"):
        DmsGate.model_validate(_gate(exceptions=[{**field, "column": None}]))
    with pytest.raises(ValueError, match="column"):
        DmsGate.model_validate(_gate(exceptions=[{**lob, "column": "x"}]))
    with pytest.raises(ValueError):
        DmsGate.model_validate(_gate(exceptions=[{**lob, "reason": ""}]))
    with pytest.raises(ValueError):
        DmsGate.model_validate(_gate(exceptions=[{**lob, "kind": "other"}]))


def test_the_path_is_registered():
    assert contract_for_artifact("shop/job1/data-movement/step-1/gate.json") is DmsGate
