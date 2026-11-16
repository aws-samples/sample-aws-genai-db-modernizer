# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The routine scripts and step 1's views (#2082): a routine that reads a view the views
deploy script leaves out is left out too, so it can never roll back the PostgreSQL
routines deploy; routines.md names views only when step 1 creates some; MySQL grant notes
warn about database-name wildcards."""

from __future__ import annotations

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
