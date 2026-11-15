# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1's views section (#2082): which views it creates, in what order, and why not."""

from __future__ import annotations

import pytest

from src.storage.local_store import LocalArtifactStore
from src.tools.data_movement.step1_routines import build_step_routines
from src.tools.data_movement.step1_views import attach_step_views, build_step_views, why
from src.tools.transformation_contract.hashing import canonical_json
from tests.unit.tools.data_movement.dms_step_fixtures import make_contract
from tests.unit.tools.data_movement.routine_fixtures import dep_table, migration, row
from tests.unit.tools.data_movement.view_fixtures import contract_with_views, schema, view


def _carry(section):
    return {v.view_id: (v.order if v.carry == "carried" else v.reasons) for v in section.views}


def test_views_on_views_come_after_the_views_they_read_whatever_their_names():
    rows = [
        view("a_top", views=["m_mid", "z_base"]),
        view("m_mid", views=["z_base"]),
        view("z_base"),
        view("b_alone"),
    ]
    section = contract_with_views(rows).views_section
    assert [v.view_id for v in section.views] == ["b_alone", "z_base", "m_mid", "a_top"]
    assert [v.order for v in section.views] == [1, 2, 3, 4]
    assert next(v for v in section.views if v.view_id == "a_top").reads == ["m_mid", "z_base"]


def test_a_view_on_a_view_not_carried_is_not_carried_and_the_chain_stops_there():
    rows = [view("top", views=["mid", "base"]), view("mid", views=["base"]),
            view("base", tables=["public.elsewhere"])]  # fmt: skip
    assert _carry(contract_with_views(rows).views_section) == {
        "base": ["depends_on_table_not_in_step"],
        "mid": ["depends_on_not_carried"],
        "top": ["depends_on_not_carried"],
    }


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"status": None}, "body_not_collected"),
        ({"status": "opted_out"}, "body_opted_out"),
        ({"status": "not_readable"}, "body_not_readable"),
        ({"status": "not_supported"}, "body_not_supported"),
        ({"columns": []}, "body_not_collected"),
        ({"lineage": "partial"}, "lineage_not_resolved"),
        ({"tables": ["public.orders", "public.gone"]}, "depends_on_table_not_in_step"),
    ],
)
def test_each_view_step_1_cannot_create_is_refused_with_a_reason_and_a_next_step(change, reason):
    kw = dict(change)
    if kw.get("columns") == []:
        rowv = view("v")
        rowv["columns"] = []
    else:
        rowv = view("v", **kw)
    (only,) = contract_with_views([rowv]).views_section.views
    assert (only.carry, only.reasons) == ("not_carried", [reason])
    assert why(only.reasons)  # every reason says what to do


def test_a_blocked_table_is_not_in_the_step():
    contract = make_contract("aurora_postgresql")
    rules = [r.model_copy(update={"status": "blocked"}) if r.source_table == "public.orders" else r
             for r in contract.rules]  # fmt: skip
    blocked = contract.model_copy(update={"rules": rules, "blocked_rule_count": 1,
                                          "status": "has_blocked_rules"})  # fmt: skip
    (only,) = contract_with_views([view("v")], contract=blocked).views_section.views
    assert only.reasons == ["depends_on_table_not_in_step"]


def test_a_view_that_calls_a_source_routine_waits_for_the_routines_and_is_not_carried():
    procs = [{"procedure_id": "public.order_total", "procedure_name": "order_total"},
             {"procedure_id": "public.unused", "procedure_name": "unused"}]  # fmt: skip
    rows = [view("calls", definition='SELECT public."order_total"(o.id) FROM public.orders o'),
            view("names", definition="SELECT o.order_total_x, o.unused FROM public.orders o")]  # fmt: skip
    assert _carry(contract_with_views(rows, procedures=procs).views_section) == {
        "names": 1, "calls": ["depends_on_routine:public.order_total"]}  # fmt: skip


def test_an_extensions_view_is_never_in_the_section():
    section = contract_with_views(
        [view("pg_stat_statements", extension_owned=True), view("v")]
    ).views_section
    assert [v.view_id for v in section.views] == ["v"]
    assert contract_with_views([view("x", extension_owned=True)]).views_section.source == "none"


def test_another_family_carries_no_view():
    base = make_contract("aurora_postgresql")
    section = build_step_views(schema([view("v")]), base, source_engine="mysql")
    assert _carry(section) == {"v": ["target_engine_differs"]}


def test_the_section_holds_no_view_text_and_an_old_contract_keeps_its_bytes(tmp_path):
    contract = contract_with_views([view("v", definition="SELECT 'v1ew-s3cr3t-2082'")])
    assert b"v1ew-s3cr3t" not in canonical_json(contract)
    old = make_contract("aurora_postgresql")
    assert b"views_section" not in canonical_json(old)


def test_attach_reads_the_collection_and_leaves_other_contracts_alone(tmp_path):
    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json("shop/job1/collector/output.json", {
        "metadata": {"source_database": {"engine": "postgresql"}},
        "database_schema": {"views": [view("v")], "procedures": []}})  # fmt: skip
    attached = attach_step_views(store, "shop", "job1", make_contract("aurora_postgresql"))
    assert _carry(attached.views_section) == {"v": 1}
    other = make_contract("aurora_postgresql").model_copy(update={"step": 2})
    assert attach_step_views(store, "shop", "job1", other).views_section is None


def test_a_routine_naming_a_carried_view_is_carried_and_one_naming_another_is_not():
    contract = contract_with_views([view("order_summary"), view("far", tables=["public.gone"])])
    artifact = migration([
        row("public.uses_summary()", dependencies=[dep_table("public.order_summary")]),
        row("public.uses_far()", dependencies=[dep_table("public.far")]),
    ])  # fmt: skip
    from src.tools.data_movement.step1_views import carried_view_names

    section = build_step_routines(artifact, contract, version=2,
                                  views={"public.order_summary", "public.far"},
                                  carried_views=carried_view_names(contract))  # fmt: skip
    got = {r.routine_id: (r.carry, r.reasons) for r in section.routines}
    assert got == {
        "public.uses_summary()": ("carried", []),
        "public.uses_far()": ("not_carried", ["depends_on_view:public.far"]),
    }
    # Without the views section (a contract before #2082) both stay held back, as before.
    before = build_step_routines(artifact, contract, version=2,
                                 views={"public.order_summary", "public.far"})  # fmt: skip
    assert {r.carry for r in before.routines} == {"not_carried"}


def test_the_contract_writer_attaches_views_before_routines(tmp_path):
    """attach_step_routines reads the views section the writer attached first."""
    from src.tools.data_movement import step1_routines

    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json("shop/job1/collector/output.json", {
        "metadata": {"source_database": {"engine": "postgresql"}},
        "database_schema": {"views": [view("order_summary")], "procedures": []}})  # fmt: skip
    artifact = migration([row("public.uses_summary()",
                              dependencies=[dep_table("public.order_summary")])])  # fmt: skip
    store.write_json(
        "shop/job1/routines/v2/routine_migration.json", artifact.model_dump(mode="json")
    )
    contract = attach_step_views(store, "shop", "job1", make_contract("aurora_postgresql"))
    with_routines = step1_routines.attach_step_routines(store, "shop", "job1", contract)
    assert [r.carry for r in with_routines.routines.routines] == ["carried"]
    without = step1_routines.attach_step_routines(store, "shop", "job1",
                                                  make_contract("aurora_postgresql"))  # fmt: skip
    assert [r.reasons for r in without.routines.routines] == [
        ["depends_on_view:public.order_summary"]
    ]
