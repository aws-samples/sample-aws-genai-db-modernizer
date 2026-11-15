# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Synthetic step 1 gate files (#1801), as ``/move``'s read-only gate writes them.

Every name and figure is made up: a ``shop`` schema with three tables and a view. Each
builder returns the JSON of a ``DmsGate`` (``src.contracts.dms_gate``), so a test reads it
exactly as the deliverables read ``data-movement/step-1/gate.json``.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from src.contracts.dms_gate import NOT_VALIDATED, DmsGate, GateCheck, GateException, GateTable
from src.shared.cutover_message import CUTOVER_MESSAGE_ID

CHECKED_AT = dt.datetime(2026, 11, 14, 9, 30, 12, tzinfo=dt.UTC)
SHA = "a" * 64
LOAD_PASS = "The task runs in CDC; every contract table is loaded"


def _table(name: str, rows: int, status: Any = "pass", state: str = "Validated", **kw: Any):
    return GateTable(table=name, rows_loaded=rows, table_state="Table completed",
                     validation_state=state, status=status, **kw)  # fmt: skip


def _checks(load: list[str], lag: list[str], parity: list[str], *, source: str = "2",
            target: str = "3", parity_worst: Any = "pending") -> list[GateCheck]:  # fmt: skip

    def status(reasons: list[str], worst: Any) -> Any:
        return worst if reasons else "pass"

    return [
        GateCheck(id="load", status=status(load, "pending"), summary=LOAD_PASS, reasons=load,
                  evidence={"task_status": "running", "full_load_percent": "100",
                            "rows_loaded": "1234567", "tables_reported": "3"}),
        GateCheck(id="lag", status=status(lag, "fail"), summary="lag", reasons=lag,
                  evidence={"window_minutes": "15", "max_lag_seconds": "30",
                            "CDCLatencySource_max": source, "CDCLatencyTarget_max": target}),
        GateCheck(id="parity", status=status(parity, parity_worst), summary="parity", reasons=parity,
                  evidence={"keyed_tables": "2"}),
    ]  # fmt: skip


def _gate(status: Any, checks: list[GateCheck], tables: list[GateTable],
          exceptions: list[GateException], **kw: Any) -> dict[str, Any]:  # fmt: skip
    return DmsGate(
        job_id="job1", database_name="shop", region="us-east-1", stack_name="dm-job1-step-1",
        task_arn="arn:aws:dms:us-east-1:111122223333:task:TASK", checked_at=CHECKED_AT,
        window_minutes=15, max_lag_seconds=30, contract_sha256=SHA, status=status,
        cutover_message_id=CUTOVER_MESSAGE_ID, checks=checks, tables=tables,
        exceptions=exceptions, **kw,
    ).model_dump(mode="json")  # fmt: skip


def _audit_log(rows: int = 12) -> list[GateException]:
    return [GateException(table="shop.audit_log", kind="table", reason=NOT_VALIDATED,
                          rows_loaded=rows),
            GateException(table="shop.order_totals", kind="view", reason=NOT_VALIDATED)]  # fmt: skip


def passed() -> dict[str, Any]:
    """Every check passes: two keyed tables validated, a keyless table and a view named."""
    tables = [
        _table("shop.audit_log", 12, status="exception", state="No primary key",
               reason=NOT_VALIDATED),
        _table("shop.customers", 1234),
        _table("shop.orders", 1233321),
    ]  # fmt: skip
    return _gate("pass", _checks([], [], []), tables, _audit_log())


def pending() -> dict[str, Any]:
    """A table still in its first validation: the gate is pending."""
    why = "validation Pending records: the first validation has not finished"
    tables = [
        _table("shop.audit_log", 12, status="exception", state="No primary key",
               reason=NOT_VALIDATED),
        _table("shop.customers", 1234),
        _table("shop.orders", 1233321, status="pending", state="Pending records", reason=why),
    ]  # fmt: skip
    return _gate("pending", _checks([], [], [f"shop.orders: {why}"]), tables, _audit_log())


def failed() -> dict[str, Any]:
    """Records that did not validate, and lag over the limit: the gate fails."""
    why = "validation Mismatched records: 3 failed and 1 suspended records"
    tables = [
        _table("shop.audit_log", 12, status="exception", state="No primary key",
               reason=NOT_VALIDATED),
        _table("shop.customers", 1234),
        _table("shop.orders", 1233321, status="fail", state="Mismatched records", reason=why,
               validation_failed_records=3, validation_suspended_records=1),
    ]  # fmt: skip
    lag = ["2 of 15 CDCLatencyTarget datapoints at or over 30 s (highest 42 s)"]
    return _gate("fail", _checks([], lag, [f"shop.orders: {why}"], target="42", parity_worst="fail"), tables,
                 _audit_log())  # fmt: skip


def many_findings(n: int) -> dict[str, Any]:
    """``n`` tables still in their first validation, each one a finding."""
    why = "validation Pending records: the first validation has not finished"
    tables = [_table(f"shop.t{i:03d}", i, status="pending", state="Pending records", reason=why)
              for i in range(n)]  # fmt: skip
    reasons = [f"shop.t{i:03d}: {why}" for i in range(n)]
    return _gate("pending", _checks([], [], reasons), tables, [])


def hostile() -> dict[str, Any]:
    """Collection and DMS text that would be live markup if it were not escaped."""
    name = "shop.`x`|<script>alert(1)</script>"
    reason = "not validated: a key on a type DMS cannot | validate on <b>![x](http://e/x.png)"
    tables = [_table(name, 7, status="exception", state="No | primary *key*", reason=reason)]
    exceptions = [GateException(table=name, kind="table", reason=reason, rows_loaded=7)]
    return _gate("pass", _checks([], [], []), tables, exceptions)


UI_FIXTURE = "src/ui/src/components/__tests__/fixtures/stepGateFacts.json"


def write_ui_fixture() -> None:
    """Write the results page test's block: the facts of ``failed()`` (checked equal in
    ``tests/unit/shared/test_step_gate_facts.py``)."""
    import json
    from pathlib import Path

    from src.shared.report_facts import step_gate_facts

    path = Path(__file__).resolve().parents[3] / UI_FIXTURE
    path.write_text(json.dumps(step_gate_facts(failed()), indent=2) + "\n")


if __name__ == "__main__":
    write_ui_fixture()
