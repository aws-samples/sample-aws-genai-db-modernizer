# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The routine scripts and step 1's views (#2082): a routine that reads a view the views
deploy script leaves out is left out too, so it can never roll back the PostgreSQL
routines deploy; routines.md names views only when step 1 creates some; MySQL grant notes
warn about database-name wildcards."""

from __future__ import annotations

import pytest

from src.tools.data_movement.routine_scripts import INSTRUCTIONS_FILE, VIEW_LEFT_OUT
from src.tools.data_movement.step1_routines import build_step_routines
from src.tools.data_movement.step1_views import carried_view_names
from tests.unit.tools.data_movement.routine_fixtures import (
    dep_routine,
    dep_table,
    migration,
    mysql_source,
    pg_source,
    row,
)
from tests.unit.tools.data_movement.test_routine_scripts import _PG_FN, _job, _write
from tests.unit.tools.data_movement.view_fixtures import contract_with_views, pg_view_source, view


def _summary():
    return {**view("order_summary"), "view_id": "public.order_summary"}


def _contract(artifact, views):
    contract = contract_with_views(views)
    section = build_step_routines(artifact, contract, version=2,
                                  views={"public.order_summary"},
                                  carried_views=carried_view_names(contract))  # fmt: skip
    return contract.model_copy(update={"routines": section})


_ROWS = [
    row("public.reads_view()", specific="rv_1", dependencies=[dep_table("public.order_summary")]),
    row("public.calls_it()", specific="ci_2", dependencies=[dep_routine("function", "public.reads_view()")]),
    row("public.plain()", specific="pl_3"),
]  # fmt: skip
_SOURCES = [pg_source("rv_1", _PG_FN.format(name="reads_view")),
            pg_source("ci_2", _PG_FN.format(name="calls_it")),
            pg_source("pl_3", _PG_FN.format(name="plain"))]  # fmt: skip


def test_a_routine_on_a_view_the_views_script_leaves_out_is_left_out_with_its_readers(tmp_path):
    artifact = migration(_ROWS)
    contract = _contract(artifact, [_summary()])
    assert [r.carry for r in contract.routines.routines] == ["carried"] * 3
    store = _job(tmp_path, artifact, _SOURCES)  # no CREATE for the view: source_missing
    scripts = _write(tmp_path, store, contract)
    assert scripts.refused == [("function", "public.calls_it()", "depends_on_refused"),
                               ("function", "public.reads_view()", VIEW_LEFT_OUT)]  # fmt: skip
    assert scripts.in_script == 1
    assert (
        f"Left out as `{VIEW_LEFT_OUT}`: 1" in (tmp_path / "step" / INSTRUCTIONS_FILE).read_text()
    )


def test_a_routine_on_a_view_the_views_script_creates_is_carried(tmp_path):
    artifact = migration(_ROWS)
    contract = _contract(artifact, [_summary()])
    text = 'CREATE OR REPLACE VIEW "public"."order_summary" ("id") AS\n SELECT 1::bigint AS id;'
    store = _job(tmp_path, artifact, [*_SOURCES, pg_view_source("order_summary", text)])
    scripts = _write(tmp_path, store, contract)
    assert (scripts.in_script, scripts.refused) == (3, [])
    assert "views-deploy.sql" in (tmp_path / "step" / INSTRUCTIONS_FILE).read_text()


def test_routines_md_names_views_only_when_step_1_creates_some(tmp_path):
    artifact = migration([row("public.plain()", specific="pl_3")])
    contract = _contract(artifact, [view("far", tables=["public.gone"])])
    _write(tmp_path, _job(tmp_path, artifact, _SOURCES[2:]), contract)
    assert "views-deploy.sql" not in (tmp_path / "step" / INSTRUCTIONS_FILE).read_text()


def test_mysql_grant_notes_warn_that_underscore_and_percent_are_wildcards(tmp_path):
    from tests.unit.tools.data_movement.routine_fixtures import contract_with_routines

    artifact = migration([row("my_db.p", "procedure", schema="my_db", status="rewritten",
                              signature="", specific="p")], "mysql")  # fmt: skip
    contract = contract_with_routines(artifact)
    store = _job(tmp_path, artifact, [mysql_source("p", "CREATE DEFINER=`app`@`%` PROCEDURE `p`() "
                                                   "BEGIN END", schema="my_db")], "mysql")  # fmt: skip
    _write(tmp_path, store, contract)
    note = "write each as \\_ and \\% (my\\_db, not my_db)"
    assert note in (tmp_path / "step" / "routines-deploy.sql").read_text()
    assert note in (tmp_path / "step" / INSTRUCTIONS_FILE).read_text()


def test_every_postgresql_script_pins_standard_conforming_strings_before_its_transaction(tmp_path):
    """The checks read a backslash in '...' as a plain character; a target whose parameter
    group turns standard_conforming_strings off reads it as an escape (#2333 review)."""
    from tests.unit.tools.data_movement.test_triggers_scripts import _pg_case as triggers_case
    from tests.unit.tools.data_movement.test_triggers_scripts import _read, _write_scripts

    pin = "SET standard_conforming_strings = on;"
    artifact = migration(_ROWS)
    contract = _contract(artifact, [_summary()])
    text = 'CREATE OR REPLACE VIEW "public"."order_summary" ("id") AS\n SELECT 1::bigint AS id;'
    store = _job(tmp_path, artifact, [*_SOURCES, pg_view_source("order_summary", text)])
    _write(tmp_path, store, contract)
    from src.tools.data_movement.view_scripts import write_view_scripts

    write_view_scripts(store, "shop", "job1", contract, "a" * 64, tmp_path / "step")
    for name in ("routines-deploy.sql", "views-deploy.sql"):
        assert pin in (tmp_path / "step" / name).read_text().split("BEGIN;", 1)[0], name
    store, contract = triggers_case(tmp_path / "t")
    _write_scripts(tmp_path / "t", store, contract)
    assert pin in _read(tmp_path / "t", "triggers-enable.sql").split("BEGIN;", 1)[0]


def test_the_triggers_enable_script_pins_the_source_value_too(tmp_path):
    from tests.unit.tools.data_movement.test_triggers_scripts import _pg_case as triggers_case
    from tests.unit.tools.data_movement.test_triggers_scripts import _read, _write_scripts

    store, contract = triggers_case(tmp_path)
    store.write_json("shop/job1/collector/output.json", {"metadata": {"source_database": {
        "engine": "postgresql", "settings": {"standard_conforming_strings": "off"}}}})  # fmt: skip
    _write_scripts(tmp_path, store, contract)
    head = _read(tmp_path, "triggers-enable.sql").split("BEGIN;", 1)[0]
    assert "SET standard_conforming_strings = off;" in head and "as on the source" in head


@pytest.mark.parametrize(
    "recorded,pinned",
    [
        ("off; DROP TABLE public.audit_log", "on"),
        ("true", "on"),
        ("", "on"),
        ("0", "on"),
        (None, "on"),  # missing
        ("OFF", "off"),
    ],
)
def test_a_hand_edited_recorded_setting_never_reaches_the_pin(tmp_path, recorded, pinned):
    """Only on or off (any case) is pinned; anything else in an edited collection file pins
    on, never written into the script (#2333 review, round 4)."""
    from src.tools.data_movement.view_scripts import write_view_scripts

    text = 'CREATE OR REPLACE VIEW "public"."v" ("id") AS\n SELECT 1::bigint AS id;'
    store = _job(tmp_path, migration([row("public.plain()", specific="pl_3")]),
                 [pg_view_source("v", text)])  # fmt: skip
    settings = {} if recorded is None else {"standard_conforming_strings": recorded}
    store.write_json("shop/job1/collector/output.json", {"metadata": {"source_database": {
        "engine": "postgresql", "settings": settings}}})  # fmt: skip
    write_view_scripts(store, "shop", "job1", contract_with_views([view("v")]), "a" * 64,
                       tmp_path / "step")  # fmt: skip
    pins = [ln for ln in (tmp_path / "step" / "views-deploy.sql").read_text().splitlines()
            if ln.startswith("SET standard_conforming_strings")]  # fmt: skip
    assert pins == [f"SET standard_conforming_strings = {pinned};"]
    assert "DROP" not in (tmp_path / "step" / "views-deploy.sql").read_text()
