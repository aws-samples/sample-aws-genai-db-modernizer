# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Loading step 1's inputs and compiling the contract into DMS settings (#1798)."""

from __future__ import annotations

import dataclasses
import json
import re

import pytest

from src.contracts.lineage_ops import ColumnRef, CopyOp, UnsupportedOp
from src.contracts.transformation_contract import FieldRule, Finding
from src.tools.data_movement.dms_step import (
    FULL_LOB_CHUNK_KB,
    DmsStepBundle,
    DmsStepError,
    DmsStepSkipped,
    bundle_inputs,
    contract_tables,
    load_step1,
    table_mappings,
    task_settings,
)
from src.tools.transformation_contract.hashing import canonical_json

from .dms_step_fixtures import make_bundle, make_step1_inputs, rewrite_contract, write_job

CONTRACT = "shop/job1/transformation-contract/v2/step-1.json"


def _creates(ddl: str) -> list[str]:
    return re.findall(r"CREATE TABLE [`\"]([^`\"]+)[`\"]", ddl)


def _blocked(bundle: DmsStepBundle, name: str) -> DmsStepBundle:
    error = Finding(
        code="unknown_table", severity="error", message=f"shop.{name} is not in the collection"
    )
    rules = [r.model_copy(update={"status": "blocked", "findings": [error]}) if r.target_table == name else r
             for r in bundle.contract.rules]  # fmt: skip
    contract = bundle.contract.model_copy(
        update={"rules": rules, "blocked_rule_count": 1, "status": "has_blocked_rules"}
    )
    return DmsStepBundle(
        plan=bundle.plan.model_copy(update={"contract": None}), contract=contract, ddl=bundle.ddl
    )


def test_the_job_files_load_with_the_contract_hash_and_a_ddl_for_every_table(tmp_path):
    store = write_job(tmp_path)
    inputs = load_step1(store, "shop", "job1")
    assert inputs.contract_sha256 == inputs.plan.contract.sha256
    assert inputs.contract_sha256 == store.read_bytes(
        "shop/job1/transformation-contract/v2/step-1.sha256").decode().strip()  # fmt: skip
    assert _creates(inputs.ddl) == ["audit_log", "customers", "orders"]
    assert inputs.skipped == ()


def test_a_partial_saved_design_still_gives_every_contract_table_its_ddl(tmp_path):
    # The saved design covers orders only (a design covers the tables routed to Aurora);
    # step 1 moves every wave 1 table, so the DDL comes from the whole target set.
    design = [{"table_name": "orders", "columns": [{"name": "id", "aurora_type": "BIGINT"},
                                                   {"name": "note", "aurora_type": "MEDIUMTEXT"}]}]  # fmt: skip
    inputs = load_step1(write_job(tmp_path, design=design), "shop", "job1")
    targets = sorted(r.target_table for r in inputs.contract.rules)
    assert _creates(inputs.ddl) == targets == ["audit_log", "customers", "orders"]
    # The saved design's type is the contract's type, and the DDL declares it.
    note = next(f for r in inputs.contract.rules for f in r.fields if f.target_column == "note")
    assert note.target_type == "MEDIUMTEXT"
    assert re.search(r"`note` MEDIUMTEXT", inputs.ddl)


def test_the_ddl_declares_every_contract_type(tmp_path):
    inputs = load_step1(write_job(tmp_path), "shop", "job1")
    for rule in inputs.contract.rules:
        create = next(
            s for s in inputs.ddl.split(";") if f"CREATE TABLE `{rule.target_table}`" in s
        )
        for f in rule.fields:
            assert f"`{f.target_column}` {f.target_type}" in create


def test_a_changed_contract_is_refused(tmp_path):
    store = write_job(tmp_path)
    loaded = load_step1(store, "shop", "job1", with_ddl=False)
    store.write_bytes(CONTRACT, canonical_json(loaded.contract.model_copy(update={"views": []})))
    with pytest.raises(DmsStepError, match="changed since the plan"):
        load_step1(store, "shop", "job1")


def test_a_reformatted_contract_file_is_refused_too(tmp_path):
    # The pin is the file's bytes: the same contract re-indented is not the pinned file.
    store = write_job(tmp_path)
    store.write_json(CONTRACT, json.loads(store.read_bytes(CONTRACT)))
    with pytest.raises(DmsStepError, match="changed since the plan"):
        load_step1(store, "shop", "job1", with_ddl=False)


def test_a_missing_plan_or_contract_names_the_script_to_run(tmp_path):
    with pytest.raises(DmsStepError, match="scripts/run_data_movement_plan.py"):
        load_step1(write_job(tmp_path / "a", plan=False), "shop", "job1")
    # A plan written before its contract names none.
    with pytest.raises(DmsStepError, match="scripts/run_transformation_contract.py"):
        load_step1(write_job(tmp_path / "b", contract=False), "shop", "job1")
    store = write_job(tmp_path / "c")
    store.delete(CONTRACT)
    with pytest.raises(DmsStepError, match="scripts/run_transformation_contract.py"):
        load_step1(store, "shop", "job1")


def test_a_plan_without_a_contract_hash_is_refused(tmp_path):
    store = write_job(tmp_path)
    plan = store.read_json("shop/job1/data-movement/step-1/plan.json")
    plan["contract"]["sha256"] = None
    store.write_json("shop/job1/data-movement/step-1/plan.json", plan)
    with pytest.raises(DmsStepError, match="pins no"):
        load_step1(store, "shop", "job1", with_ddl=False)


def test_without_the_ddl_nothing_else_is_read(tmp_path):
    store = write_job(tmp_path)
    store.delete("shop/job1/collector/output.json")
    assert load_step1(store, "shop", "job1", with_ddl=False).ddl == ""
    with pytest.raises(DmsStepError, match="collector"):
        load_step1(store, "shop", "job1")


def test_the_unbounded_varchar_setting_rides_on_the_inputs():
    inputs = make_step1_inputs("aurora_postgresql")
    assert inputs.plan.replication is not None
    assert inputs.unbounded_varchar == inputs.plan.replication.unbounded_varchar


def test_blocked_rules_are_left_out_and_said_why():
    inputs = bundle_inputs(_blocked(make_bundle(), "orders"))
    assert [(s.table_id, s.reason) for s in inputs.skipped] == [
        ("shop.orders", "shop.orders is not in the collection")
    ]
    assert [t.table_id for t in contract_tables(inputs.contract)] == [
        "shop.audit_log",
        "shop.customers",
    ]
    rules = table_mappings(inputs)["rules"]
    assert "orders" not in [r["object-locator"]["table-name"] for r in rules]


def test_every_rule_blocked_is_refused():
    b = make_bundle()
    error = Finding(code="unknown_table", severity="error", message="not in the collection")
    rules = [
        r.model_copy(update={"status": "blocked", "findings": [error]}) for r in b.contract.rules
    ]
    contract = b.contract.model_copy(
        update={"rules": rules, "blocked_rule_count": 3, "status": "has_blocked_rules"}
    )
    plan = b.plan.model_copy(update={"contract": None})
    with pytest.raises(DmsStepError, match="every step 1 rule is blocked"):
        bundle_inputs(DmsStepBundle(plan=plan, contract=contract, ddl=b.ddl))


def test_no_rules_is_a_skip():
    b = make_bundle()
    empty = b.contract.model_copy(update={"rules": [], "views": [], "status": "no_relational_anchor",
                                          "status_reason": "no relational anchor"})  # fmt: skip
    with pytest.raises(DmsStepSkipped, match="no relational anchor"):
        bundle_inputs(
            DmsStepBundle(
                plan=b.plan.model_copy(update={"contract": None}), contract=empty, ddl=b.ddl
            )
        )


def test_a_bundle_whose_contract_differs_from_the_plan_pin_is_refused():
    b = make_bundle()
    changed = b.contract.model_copy(update={"views": []})
    with pytest.raises(DmsStepError, match="changed since the plan"):
        bundle_inputs(DmsStepBundle(plan=b.plan, contract=changed, ddl=b.ddl))


def test_a_bundle_ddl_missing_a_table_is_refused():
    b = make_bundle()
    ddl = b.ddl.replace("CREATE TABLE `orders`", "CREATE TABLE `orders_old`")
    with pytest.raises(DmsStepError, match="shop.orders"):
        bundle_inputs(DmsStepBundle(plan=b.plan, contract=b.contract, ddl=ddl))


def test_a_renamed_table_is_refused():
    b = make_bundle()
    rules = [r.model_copy(update={"target_table": "orders_v2"}) if r.target_table == "orders" else r
             for r in b.contract.rules]  # fmt: skip
    renamed = b.contract.model_copy(update={"rules": rules})
    with pytest.raises(DmsStepError, match="renames"):
        bundle_inputs(
            DmsStepBundle(
                plan=b.plan.model_copy(update={"contract": None}), contract=renamed, ddl=b.ddl
            )
        )


def test_a_name_that_is_not_a_plain_identifier_is_refused():
    b = make_bundle()
    rules = [r.model_copy(update={"source_table": "shop.or%ders", "target_table": "or%ders"})
             if r.target_table == "orders" else r for r in b.contract.rules]  # fmt: skip
    odd = b.contract.model_copy(update={"rules": rules})
    with pytest.raises(DmsStepError, match="plain identifier"):
        bundle_inputs(
            DmsStepBundle(
                plan=b.plan.model_copy(update={"contract": None}), contract=odd, ddl=b.ddl
            )
        )


def test_a_plan_and_contract_for_different_engines_or_versions_are_refused():
    b = make_bundle()
    plan = b.plan.model_copy(update={"contract": None, "assignment_version": 3})
    with pytest.raises(DmsStepError, match="assignment v2"):
        bundle_inputs(DmsStepBundle(plan=plan, contract=b.contract, ddl=b.ddl))
    rep = b.plan.replication.model_copy(update={"target_engine": "aurora_postgresql"})
    plan = b.plan.model_copy(update={"contract": None, "replication": rep})
    with pytest.raises(DmsStepError, match="moves into"):
        bundle_inputs(DmsStepBundle(plan=plan, contract=b.contract, ddl=b.ddl))


def test_keyless_tables_are_marked_and_views_left_out():
    tables = contract_tables(make_step1_inputs().contract)
    assert [(t.table_id, t.keyed) for t in tables] == [
        ("shop.audit_log", False),
        ("shop.customers", True),
        ("shop.orders", True),
    ]


@pytest.mark.parametrize("engine", ["aurora_mysql", "aurora_postgresql"])
def test_mappings_select_exactly_the_contract_tables(engine):
    inputs = make_step1_inputs(engine)
    rules = table_mappings(inputs)["rules"]
    selections = [r for r in rules if r["rule-type"] == "selection"]
    schema = "shop" if engine == "aurora_mysql" else "public"
    assert [
        (r["object-locator"]["schema-name"], r["object-locator"]["table-name"]) for r in selections
    ] == [(schema, "audit_log"), (schema, "customers"), (schema, "orders")]
    assert all(r["rule-action"] == "include" for r in selections)
    assert {r["rule-type"] for r in rules} == {"selection"}
    assert not [r for r in rules if r["rule-type"] == "transformation"]
    assert "%" not in json.dumps(rules) and "v_orders" not in json.dumps(rules)
    assert len({r["rule-id"] for r in rules}) == len(rules)
    assert len({r["rule-name"] for r in rules}) == len(rules)
    assert all(r["rule-id"].isdigit() for r in rules)


def test_no_mapping_rule_carries_lob_settings():
    # DMS rejects the per-table full LOB rule (bulk-max-size 0) when the task is created, and
    # the nonzero form is combination full LOB mode, which MySQL and PostgreSQL lack (#1863).
    for engine in ("aurora_mysql", "aurora_postgresql"):
        rules = table_mappings(make_step1_inputs(engine))["rules"]
        assert not [r for r in rules if r["rule-type"] == "table-settings"]
        assert "lob-settings" not in json.dumps(rules)


def test_the_task_never_runs_full_lob_mode_from_a_mysql_plan_with_a_full_table():
    # #2123: a plan written before then lists a MySQL LONGTEXT table in full mode; the task
    # still runs limited, since full LOB mode inserts NULL into a NOT NULL LOB column first.
    inputs = make_step1_inputs()
    assert any(t.mode == "full" for t in inputs.plan.replication.lob_tables)
    t = task_settings(inputs)["TargetMetadata"]
    assert (t["SupportLobs"], t["FullLobMode"], t["LimitedSizeLobMode"]) == (True, False, True)
    assert t["LobChunkSize"] == FULL_LOB_CHUNK_KB


@pytest.mark.passes_on_main  # the limited mode #1863 must keep
def test_the_task_runs_in_limited_lob_mode_at_the_plans_size_when_no_table_is_full():
    b = make_bundle()
    lobs = [
        t.model_copy(update={"mode": "limited", "max_size_kb": 8})
        for t in b.plan.replication.lob_tables
    ]
    rep = b.plan.replication.model_copy(update={"lob_tables": lobs, "lob_max_size_kb": 512})
    plan = b.plan.model_copy(update={"replication": rep})
    inputs = bundle_inputs(DmsStepBundle(plan=plan, contract=b.contract, ddl=b.ddl))
    t = task_settings(inputs)["TargetMetadata"]
    assert (t["SupportLobs"], t["FullLobMode"], t["LimitedSizeLobMode"]) == (True, False, True)
    assert (t["LobMaxSize"], t["LobChunkSize"]) == (512, FULL_LOB_CHUNK_KB)


def test_a_full_lob_table_that_is_blocked_does_not_turn_on_full_lob_mode():
    inputs = bundle_inputs(_blocked(make_bundle(), "customers"))
    t = task_settings(inputs)["TargetMetadata"]
    assert (t["FullLobMode"], t["LimitedSizeLobMode"], t["LobMaxSize"]) == (False, True, 64)


def test_task_settings_are_fixed():
    s = task_settings(make_step1_inputs())
    assert s["FullLoadSettings"]["TargetTablePrepMode"] == "DO_NOTHING"
    assert s["TargetMetadata"]["BatchApplyEnabled"] is False
    assert s["ValidationSettings"]["EnableValidation"] is True
    assert s["ChangeProcessingDdlHandlingPolicy"] == {
        "HandleSourceTableDropped": False,
        "HandleSourceTableTruncated": False,
        "HandleSourceTableAltered": False,
    }
    assert s["Logging"]["EnableLogging"] is True


@pytest.mark.passes_on_main  # the limited mode #1863 must keep
def test_the_lob_size_is_the_plans():
    b = make_bundle()
    lobs = [
        t.model_copy(update={"mode": "limited", "max_size_kb": 8})
        for t in b.plan.replication.lob_tables
    ]
    rep = b.plan.replication.model_copy(update={"lob_max_size_kb": 512, "lob_tables": lobs})
    plan = b.plan.model_copy(update={"replication": rep})
    s = task_settings(bundle_inputs(DmsStepBundle(plan=plan, contract=b.contract, ddl=b.ddl)))
    assert s["TargetMetadata"]["LobMaxSize"] == 512


def _block_in_store(store, name: str) -> None:
    contract = load_step1(store, "shop", "job1", with_ddl=False).contract
    error = Finding(code="unknown_table", severity="error", message=f"shop.{name} is blocked")
    rules = [r.model_copy(update={"status": "blocked", "findings": [error]}) if r.target_table == name else r
             for r in contract.rules]  # fmt: skip
    rewrite_contract(store, contract.model_copy(
        update={"rules": rules, "blocked_rule_count": 1, "status": "has_blocked_rules"}))  # fmt: skip


def test_a_foreign_key_between_moved_tables_is_kept(tmp_path):
    ddl = load_step1(write_job(tmp_path, fk=True), "shop", "job1").ddl
    assert re.search(r"ALTER TABLE `orders` ADD CONSTRAINT `fk_orders_customer` FOREIGN KEY "
                     r"\(`customer_id`\) REFERENCES `customers` \(`id`\)", ddl)  # fmt: skip


def test_a_foreign_key_to_a_blocked_table_is_dropped(tmp_path):
    store = write_job(tmp_path, fk=True)
    _block_in_store(store, "customers")
    inputs = load_step1(store, "shop", "job1")
    assert [s.table_id for s in inputs.skipped] == ["shop.customers"]
    assert _creates(inputs.ddl) == ["audit_log", "orders"]
    assert "REFERENCES" not in inputs.ddl and "fk_orders_customer" not in inputs.ddl
    assert "`customer_id` BIGINT" in inputs.ddl  # the column moves; only the constraint goes


def test_a_contract_column_the_ddl_does_not_declare_is_refused(tmp_path):
    store = write_job(tmp_path)
    contract = load_step1(store, "shop", "job1", with_ddl=False).contract
    ghost = FieldRule(source_column="ghost", target_column="ghost", target_type="TEXT",
                      op=CopyOp(op="copy", column=ColumnRef(table_id="shop.orders", column="ghost")))  # fmt: skip
    rules = [r.model_copy(update={"fields": [*r.fields, ghost]}) if r.target_table == "orders" else r
             for r in contract.rules]  # fmt: skip
    rewrite_contract(store, contract.model_copy(update={"rules": rules}))
    with pytest.raises(
        DmsStepError, match="shop.orders.ghost: the DDL declares None, the contract TEXT"
    ):
        load_step1(store, "shop", "job1")


def test_a_job_at_another_assignment_version_than_the_plan_is_refused(tmp_path):
    store = write_job(tmp_path)
    report = store.read_json("shop/job1/synthesis/v2/report.json")
    store.write_json(
        "shop/job1/synthesis/v3/report.json", {**report, "assignment_summary": {"version": 3}}
    )
    store.write_json(
        "shop/job1/assignment/v3/assignment.json", {"version": 3, "query_assignments": []}
    )
    with pytest.raises(DmsStepError, match="the job reads assignment v3, the plan v2"):
        load_step1(store, "shop", "job1")
    assert load_step1(store, "shop", "job1", with_ddl=False).plan.assignment_version == 2


def test_unsupported_fields_are_listed_next_to_the_skipped_rules():
    b = make_bundle()
    hook = UnsupportedOp(op="unsupported", description="no Aurora type for geometry")
    rules = [r.model_copy(update={"fields": [r.fields[0], r.fields[1].model_copy(update={"op": hook})]})
             if r.target_table == "orders" else r for r in b.contract.rules]  # fmt: skip
    contract = b.contract.model_copy(update={"rules": rules, "unsupported_count": 1})
    plan = b.plan.model_copy(update={"contract": None})
    inputs = bundle_inputs(DmsStepBundle(plan=plan, contract=contract, ddl=b.ddl))
    assert [(u.table_id, u.column, u.description) for u in inputs.unsupported] == [
        ("shop.orders", "note", "no Aurora type for geometry")
    ]
    assert make_step1_inputs().unsupported == ()


def test_an_unsupported_field_of_a_blocked_rule_is_not_listed():
    b = make_bundle()
    hook = UnsupportedOp(op="unsupported", description="x")
    error = Finding(code="unknown_table", severity="error", message="blocked")
    rules = [r.model_copy(update={"status": "blocked", "findings": [error],
                                  "fields": [r.fields[0], r.fields[1].model_copy(update={"op": hook})]})
             if r.target_table == "orders" else r for r in b.contract.rules]  # fmt: skip
    contract = b.contract.model_copy(update={"rules": rules, "unsupported_count": 1, "blocked_rule_count": 1,
                                             "status": "has_blocked_rules"})  # fmt: skip
    inputs = bundle_inputs(DmsStepBundle(plan=b.plan.model_copy(update={"contract": None}), contract=contract,
                                         ddl=b.ddl))  # fmt: skip
    assert inputs.unsupported == () and [s.table_id for s in inputs.skipped] == ["shop.orders"]


@pytest.mark.parametrize("engine", ["aurora_mysql", "aurora_postgresql"])
def test_the_ddl_check_reads_if_not_exists_and_a_schema_prefix(engine):
    b = make_bundle(engine)
    q = "`" if engine == "aurora_mysql" else '"'
    ddl = (b.ddl.replace(f"CREATE TABLE {q}audit_log{q}", f"CREATE TABLE IF NOT EXISTS {q}audit_log{q}")
           .replace(f"CREATE TABLE {q}orders{q}", f"CREATE TABLE {q}shop{q}.{q}orders{q}")
           .replace(f"CREATE TABLE {q}customers{q}", f"CREATE TABLE IF NOT EXISTS {q}s{q}.{q}customers{q}"))  # fmt: skip
    inputs = bundle_inputs(DmsStepBundle(plan=b.plan, contract=b.contract, ddl=ddl))
    assert inputs.ddl == ddl
    # The schema is never taken for the table.
    wrong = b.ddl.replace(f"CREATE TABLE {q}orders{q}", f"CREATE TABLE {q}orders{q}.{q}other{q}")
    with pytest.raises(DmsStepError, match="orders"):
        bundle_inputs(DmsStepBundle(plan=b.plan, contract=b.contract, ddl=wrong))


def test_a_plan_without_a_replication_section_is_an_error_not_an_assert():

    from src.tools.data_movement.dms_step import DmsStepError, _replication
    from tests.unit.tools.data_movement.dms_step_fixtures import make_step1_inputs

    inputs = make_step1_inputs()
    bare = dataclasses.replace(inputs, plan=inputs.plan.model_copy(update={"replication": None}))
    with pytest.raises(DmsStepError, match="run scripts/run_data_movement_plan.py first"):
        _replication(bare)
    with pytest.raises(DmsStepError, match="no replication section"):
        bare.unbounded_varchar  # noqa: B018
