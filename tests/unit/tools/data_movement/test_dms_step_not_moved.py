# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A table the contract flags as not moved (#1918): created by the DDL, left out of the DMS
task, named by the gate and by /move's summary.

DMS does not migrate tsvector (its guide lists TSVECTOR as unsupported); in a live run the
table failed the load. The contract flags it instead, so the task runs and the customer
rebuilds the column after cutover (the application's search reindex).
"""

from __future__ import annotations

from scripts import run_dms_step as script
from src.contracts.transformation_contract import NOT_MOVED, Finding, StepContract
from src.storage.local_store import LocalArtifactStore
from src.tools.data_movement.dms_gate import parity_check, read_task
from src.tools.data_movement.dms_step import (
    DmsStepBundle,
    bundle_inputs,
    contract_tables,
    render_ddl,
    table_mappings,
)
from tests.unit.tools.aws.aws_stubs import StubbedCredMgr

from . import gate_stubs as g
from .dms_step_fixtures import make_contract, make_ddl, make_plan, rewrite_contract, write_pg_job

WHY = (
    "DMS does not migrate tsvector; rebuilding it is yours (e.g. the application's search "
    "reindex): step 1 creates orders and does not move its rows (search is tsvector)"
)


def _bundle() -> DmsStepBundle:
    contract = _flag_orders(make_contract("aurora_postgresql"))
    return DmsStepBundle(plan=make_plan(contract), contract=contract, ddl=make_ddl(contract))


def _selected(mapping) -> list[str]:
    return [
        r["object-locator"]["table-name"] for r in mapping["rules"] if r["rule-action"] == "include"
    ]


def test_the_dms_task_leaves_a_not_moved_table_out():
    inputs = bundle_inputs(_bundle())
    assert "orders" not in _selected(table_mappings(inputs))
    assert "customers" in _selected(table_mappings(inputs))
    assert "public.orders" not in {t.table_id for t in contract_tables(inputs.contract)}
    assert [(n.table_id, n.reason) for n in inputs.not_moved] == [("public.orders", WHY)]


def test_the_ddl_still_creates_a_not_moved_table_and_names_it():
    ddl = render_ddl(bundle_inputs(_bundle()))
    text = " ".join(line.removeprefix("-- ") for line in ddl.splitlines())
    assert 'CREATE TABLE "orders"' in ddl
    assert "Tables created but not moved" in text and f"- public.orders: {WHY}" in text
    # Not a blocked rule: the blocked list stays empty.
    assert "Tables left out: none." in text


def test_a_not_moved_table_is_not_in_the_task_lob_sizing():
    # The fixture plan lists public.orders as a LOB table; once it is not moved, the DDL
    # header's LOB paragraph names only the moved one.
    ddl = " ".join(
        line.removeprefix("-- ") for line in render_ddl(bundle_inputs(_bundle())).splitlines()
    )
    assert "The tables: public.customers." in ddl


def test_without_a_not_moved_rule_the_ddl_has_no_such_list():
    contract = make_contract("aurora_postgresql")
    plain = DmsStepBundle(plan=make_plan(contract), contract=contract, ddl=make_ddl(contract))
    assert "Tables created but not moved" not in render_ddl(bundle_inputs(plain))


def test_the_gate_names_a_not_moved_table_as_a_known_exception():
    inputs = bundle_inputs(_bundle())
    cred = StubbedCredMgr()
    stats = [g.stat(t.table_id) for t in contract_tables(inputs.contract)]
    g.queue(cred, inputs.contract_sha256, stats=stats)
    check, tables, exceptions = parity_check(
        inputs, read_task(g.clients(cred), g.request(), now=g.NOW)
    )
    got = [(e.table, e.kind, e.reason) for e in exceptions if e.kind == "not_moved"]
    assert got == [("public.orders", "not_moved", WHY)]
    assert "public.orders" not in [t.table for t in tables]
    assert check.status != "fail"


def _flag_orders(contract: StepContract) -> StepContract:
    rules = []
    for r in contract.rules:
        if r.source_table == "public.orders":
            finding = Finding(code=NOT_MOVED, severity="warning", message=WHY,
                              table_id=r.source_table, column="search")  # fmt: skip
            r = r.model_copy(update={"moves_rows": False, "findings": [*r.findings, finding]})
        rules.append(r)
    return contract.model_copy(update={"rules": rules})


def _contract(store: LocalArtifactStore) -> StepContract:
    raw = store.read_json("shop/job1/transformation-contract/v2/step-1.json")
    return StepContract.model_validate(raw)


def test_the_move_summary_lists_the_not_moved_tables(tmp_path):
    # summary reads the whole step 1 input (the finished job), as render_dms_step.py does.
    store = write_pg_job(tmp_path)
    rewrite_contract(store, _flag_orders(_contract(store)))
    out = script.summary(str(tmp_path), "shop", "job1")
    assert out["contract"]["not_moved"] == [{"table": "public.orders", "reason": WHY}]
    assert WHY in out["contract"]["warnings"]


def test_an_unflagged_contract_lists_no_not_moved_table(tmp_path):
    write_pg_job(tmp_path)
    assert script.summary(str(tmp_path), "shop", "job1")["contract"]["not_moved"] == []
