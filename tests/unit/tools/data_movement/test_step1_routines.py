# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1's routines section (#1947): which routines the deploy script carries, in what
order, and why each other one is not carried."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from src.contracts.routine_migration import RoutineMigrationContract, RoutineRef
from src.contracts.transformation_contract import Finding, StepContract, StepRoutines
from src.storage.local_store import LocalArtifactStore
from src.tools.data_movement.step1_routines import attach_step_routines, build_step_routines
from src.tools.routines.migration import routine_migration_path
from src.tools.transformation_contract.hashing import canonical_json
from tests.unit.tools.data_movement.dms_step_fixtures import make_contract
from tests.unit.tools.data_movement.routine_fixtures import (
    dep_routine,
    dep_table,
    migration,
    row,
)
from tests.unit.tools.sql._timing import assert_cost_ratio


def _section(rows, engine="postgresql", cycles=None) -> StepRoutines:
    artifact = migration(rows, engine, cycles)
    return build_step_routines(artifact, make_contract(artifact.target_engine), version=2)


def _by_id(section: StepRoutines) -> dict[str, object]:
    return {r.routine_id: r for r in section.routines}


def test_no_routines_gives_an_empty_section_that_says_so():
    section = build_step_routines(None, make_contract("aurora_postgresql"), version=None)
    assert (section.source, section.total, section.routines) == ("none", 0, [])


def test_every_routine_needing_attention_is_not_carried_with_its_reasons():
    section = _section([
        row("public.a(integer)", status="needs_attention", reasons=["postgresql-routine-language"]),
        row("public.b(integer)", status="needs_attention", reasons=["source_text_not_collected"]),
    ])  # fmt: skip
    assert (section.carried, section.not_carried) == (0, 2)
    assert {r.routine_id: r.reasons for r in section.routines} == {
        "public.a(integer)": ["postgresql-routine-language"],
        "public.b(integer)": ["source_text_not_collected"],
    }
    assert all(r.order is None and r.enabled_as is None for r in section.routines)


def test_a_dependency_chain_is_carried_in_order_and_a_broken_link_stops_what_follows():
    chain = [
        row("public.a(integer)", dependencies=[dep_table("public.orders")]),
        row("public.b(integer)", dependencies=[dep_routine("function", "public.a(integer)")]),
        row("public.c(integer)", dependencies=[dep_routine("function", "public.b(integer)")]),
    ]
    section = _section(chain)
    assert [(r.routine_id, r.order) for r in section.routines] == [
        ("public.a(integer)", 1), ("public.b(integer)", 2), ("public.c(integer)", 3)]  # fmt: skip
    chain[0]["dependencies"] = [dep_table("public.archive")]  # a table step 1 does not create
    broken = _by_id(_section(chain))
    assert broken["public.a(integer)"].reasons == ["depends_on_table_not_in_step"]
    assert broken["public.b(integer)"].reasons == ["depends_on_not_carried"]
    assert broken["public.c(integer)"].reasons == ["depends_on_not_carried"]  # transitively


def test_a_dependency_cycle_is_never_carried():
    refs = [RoutineRef(kind="function", routine_id=f"public.{n}()") for n in "ab"]
    section = _section(
        [row("public.a()", status="needs_attention", reasons=["dependency_cycle"], cycle=0,
             dependencies=[dep_routine("function", "public.b()")]),
         row("public.b()", status="needs_attention", reasons=["dependency_cycle"], cycle=0,
             dependencies=[dep_routine("function", "public.a()")]),
         row("public.c()", dependencies=[dep_routine("function", "public.a()")])],
        cycles=[refs],
    )  # fmt: skip
    assert section.carried == 0
    assert _by_id(section)["public.c()"].reasons == ["depends_on_not_carried"]


def test_overloads_are_two_rows_and_only_the_carriable_one_is_carried():
    section = _section([
        row("public.f(integer)", name="f", specific="f_1", signature="integer"),
        row("public.f(text)", name="f", specific="f_2", signature="text",
            status="needs_attention", reasons=["postgresql-routine-language"]),
    ])  # fmt: skip
    rows = _by_id(section)
    assert (rows["public.f(integer)"].carry, rows["public.f(integer)"].specific_name) == (
        "carried",
        "f_1",
    )
    assert rows["public.f(text)"].carry == "not_carried"


def test_a_postgresql_trigger_is_carried_disabled_and_one_on_a_blocked_table_is_not():
    rows = [row("public.trg_fn()"),
            row("public.orders.trg", "trigger", name="trg", table="public.orders",
                dependencies=[dep_routine("function", "public.trg_fn()")])]  # fmt: skip
    fn, trg = _section(rows).routines
    assert (trg.carry, trg.order, trg.enabled_as) == ("carried", 2, "disabled")
    assert fn.enabled_as == "enabled"
    artifact = migration(rows)
    contract = make_contract("aurora_postgresql")
    blocked = contract.model_copy(update={"rules": [
        r if r.target_table != "orders" else r.model_copy(update={"status": "blocked", "findings": [
            Finding(code="x", severity="error", message="blocked")]})
        for r in contract.rules]})  # fmt: skip
    blocked = StepContract.model_validate({**blocked.model_dump(mode="json"), "blocked_rule_count": 1,
                                           "status": "has_blocked_rules"})  # fmt: skip
    trigger = _by_id(build_step_routines(artifact, blocked, version=2))["public.orders.trg"]
    assert trigger.reasons == ["depends_on_table_not_in_step"]


def test_mysql_triggers_and_events_are_left_to_the_cutover_script():
    section = _section([
        row("shop.place_order", "procedure", schema="shop", status="rewritten",
            dependencies=[dep_table("shop.orders")]),
        row("shop.orders_bi", "trigger", schema="shop", table="shop.orders"),
        row("shop.nightly", "event", schema="shop"),
    ], engine="mysql")  # fmt: skip
    rows = _by_id(section)
    assert (rows["shop.place_order"].carry, rows["shop.place_order"].migration_status) == (
        "carried", "rewritten")  # fmt: skip
    for rid in ("shop.orders_bi", "shop.nightly"):
        assert (rows[rid].carry, rows[rid].enabled_as, rows[rid].order) == (
            "deferred_to_cutover", "absent", None)  # fmt: skip
    assert (section.carried, section.deferred, section.not_carried) == (1, 2, 0)


def test_the_section_never_holds_routine_text():
    section = _section([row("public.a(integer)")])
    schema = json.dumps(StepRoutines.model_json_schema())
    for word in ("create_statement", "definition", "body", '"statement"', "sha256"):
        assert word not in schema
    assert "CREATE" not in section.model_dump_json()


def test_counts_that_disagree_with_the_rows_are_refused():
    section = _section([row("public.a(integer)")]).model_dump()
    with pytest.raises(ValidationError, match="counts disagree"):
        StepRoutines.model_validate({**section, "carried": 0})
    carried = section["routines"][0]
    with pytest.raises(ValidationError, match="order and status"):
        StepRoutines.model_validate({**section, "routines": [{**carried, "order": None}]})
    with pytest.raises(ValidationError, match="without a reason"):
        StepRoutines.model_validate({**section, "carried": 0, "not_carried": 1, "routines": [
            {**carried, "carry": "not_carried", "order": None, "migration_status": None,
             "enabled_as": None}]})  # fmt: skip


def test_a_contract_without_the_section_keeps_its_bytes():
    contract = make_contract("aurora_postgresql")
    data = canonical_json(contract)
    assert b"routines" not in data
    assert canonical_json(StepContract.model_validate_json(data)) == data


def _store_with(tmp_path, versions: dict[int, RoutineMigrationContract]) -> LocalArtifactStore:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    for v, artifact in versions.items():
        store.write_json(
            routine_migration_path("shop", "job1", v), artifact.model_dump(mode="json")
        )
    return store


def test_the_section_reads_the_newest_artifact_at_or_below_the_contracts_version(tmp_path):
    old, new = migration([row("public.a()")]), migration([row("public.b()")])
    store = _store_with(tmp_path, {1: old, 3: new})
    contract = attach_step_routines(store, "shop", "job1", make_contract("aurora_postgresql"))
    assert (contract.routines.routine_migration_version, contract.routines.routines[0].routine_id) == (
        1, "public.a()")  # fmt: skip


def test_a_contract_with_no_relational_step_gets_no_section(tmp_path):
    store = _store_with(tmp_path, {2: migration([row("public.a()")])})
    empty = StepContract(job_id="job1", assignment_version=2, step=1, target={"engine": "dynamodb"},
                         status="no_relational_anchor", status_reason="no relational anchor")  # fmt: skip
    assert attach_step_routines(store, "shop", "job1", empty).routines is None


def test_the_section_does_not_depend_on_the_routines_input_order():
    rows = [row(f"public.f{i}()", dependencies=[dep_table("public.orders")]) for i in range(5)]
    first = _section(rows)
    assert _section(list(rows)).model_dump() == first.model_dump()


def test_a_hundred_thousand_routines_build_in_linear_time():
    def artifact(n: int):
        return migration(
            [row(f"public.f{i}()", dependencies=[dep_table("public.orders")]) for i in range(n)]
        )

    large, reference = artifact(100_000), artifact(10_000)
    contract = make_contract("aurora_postgresql")
    assert_cost_ratio(
        lambda: build_step_routines(large, contract, version=2),
        lambda: build_step_routines(reference, contract, version=2),
        25,
        max_rounds=3,
    )


def test_the_contract_writer_attaches_the_section_from_the_jobs_artifact(tmp_path):
    from src.tools.transformation_contract.write import write_step1
    from tests.unit.tools.data_movement.dms_step_fixtures import write_job

    store = write_job(tmp_path, plan=False)  # a collection with no routines
    raw = store.read_json("shop/job1/transformation-contract/v2/step-1.json")
    assert raw["routines"] == {"source": "none", "routine_migration_version": None, "total": 0,
                               "carried": 0, "not_carried": 0, "deferred": 0, "routines": []}  # fmt: skip
    artifact = migration([row("shop.p", "procedure", schema="shop", status="rewritten",
                              dependencies=[dep_table("shop.orders")])], "mysql")  # fmt: skip
    store.write_json(routine_migration_path("shop", "job1", 2), artifact.model_dump(mode="json"))
    write_step1(store, "shop", "job1")
    section = store.read_json("shop/job1/transformation-contract/v2/step-1.json")["routines"]
    assert (section["carried"], section["routines"][0]["routine_id"]) == (1, "shop.p")


def test_a_routine_naming_a_view_is_not_carried_and_names_the_view():
    # The artifact records a view as a table dependency (#1934); step 1 creates no views.
    artifact = migration([
        row("public.a()", dependencies=[dep_table("public.v_orders")]),
        row("public.b()", dependencies=[dep_table("public.order_summary"), dep_table("public.orders")]),
        row("public.c()", dependencies=[dep_table("public.archive")]),
    ])  # fmt: skip
    section = build_step_routines(artifact, make_contract("aurora_postgresql"), version=2,
                                  views={"public.order_summary"})  # fmt: skip
    rows = _by_id(section)
    assert rows["public.a()"].reasons == ["depends_on_view:public.v_orders"]  # the contract's view
    assert rows["public.b()"].reasons == [
        "depends_on_view:public.order_summary"
    ]  # the collection's
    assert rows["public.c()"].reasons == ["depends_on_table_not_in_step"]


def test_the_contract_writer_tells_the_collections_views_apart(tmp_path):
    from tests.unit.tools.data_movement.dms_step_fixtures import write_job

    store = write_job(tmp_path, contract=False, plan=False)
    artifact = migration([row("shop.p", "procedure", schema="shop", status="rewritten",
                              dependencies=[dep_table("shop.v_orders")])], "mysql")  # fmt: skip
    store.write_json(routine_migration_path("shop", "job1", 2), artifact.model_dump(mode="json"))
    contract = make_contract("aurora_mysql").model_copy(update={"views": []})
    section = attach_step_routines(store, "shop", "job1", contract).routines
    assert section.routines[0].reasons == ["depends_on_view:shop.v_orders"]


def test_a_routine_naming_one_the_section_never_settled_is_not_carried():
    artifact = migration(
        [row("public.g()", dependencies=[dep_routine("function", "public.unknown()")])]
    )
    assert _by_id(_section_of(artifact))["public.g()"].reasons == ["depends_on_not_carried"]


def _section_of(artifact):
    return build_step_routines(artifact, make_contract(artifact.target_engine), version=2)


def test_a_routine_body_the_customer_opted_out_of_is_not_carried_with_its_reason(tmp_path):
    # include_routine_bodies = 0 (#1933): no routine sources, every body opted_out.
    from src.tools.data_movement.routine_scripts import write_routine_scripts
    from src.tools.routines.migration import run_routine_migration_deterministic

    procedure = {
        "procedure_id": "f1", "procedure_name": "f1", "schema_name": "public",
        "procedure_type": "FUNCTION", "specific_name": "f1_16385", "language": "sql",
        "owner": "app", "security_definer": False, "body_status": "opted_out",
        "definition": None, "depends_on": [],
    }  # fmt: skip
    collector = {"metadata": {"source_database": {"engine": "postgresql", "version": "16.2"}},
                 "database_schema": {"tables": [], "procedures": [procedure]}}  # fmt: skip
    artifact = run_routine_migration_deterministic(
        collector, None, None, job_id="job1", database_name="shop", assignment_version=2
    )
    assert artifact is not None
    contract = make_contract("aurora_postgresql")
    section = build_step_routines(artifact, contract, version=2)
    (routine,) = section.routines
    assert (routine.carry, routine.reasons) == ("not_carried", ["source_text_not_collected"])
    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json(routine_migration_path("shop", "job1", 2), artifact.model_dump(mode="json"))
    scripts = write_routine_scripts(store, "shop", "job1",
                                    contract.model_copy(update={"routines": section}), "a" * 64,
                                    tmp_path / "step")  # fmt: skip
    assert scripts.status == "nothing_to_deploy" and not (tmp_path / "step").exists()
