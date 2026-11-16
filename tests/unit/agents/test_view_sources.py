# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Each collected view's CREATE, kept in the routine sources (#2082)."""

from __future__ import annotations

import pytest

from src.agents.collector.routine_sources import (
    iter_collection_sources,
    iter_routine_sources,
    mark_view_statuses,
)
from src.agents.collector.view_sources import view_body_status, view_create, view_fields

_COLS = [{"name": "id", "position": 1, "data_type": "integer"},
         {"name": "total", "position": 2, "data_type": "numeric(10,2)"}]  # fmt: skip


def _pg(**kw):
    return {"view_name": "order_totals", "schema_name": "sales", "is_updatable": "NO",
            "definition": " SELECT o.id,\n    o.total\n   FROM sales.orders o\n  WHERE o.note = 'x';",
            "check_option": "NONE", "security_barrier": False, "security_invoker": False,
            "extension_owned": False, "columns": _COLS, "search_path": ["public"], **kw}  # fmt: skip


def _my(**kw):
    return {"view_name": "order_totals", "schema_name": "shop", "is_updatable": "YES",
            "definition": "select `shop`.`orders`.`id` AS `id`,`shop`.`orders`.`total` AS `total` "
                          "from `shop`.`orders` where (`shop`.`orders`.`note` = _utf8mb4'x')",
            "check_option": "NONE", "security_type": "DEFINER", "character_set_client": "utf8mb4",
            "collation_connection": "utf8mb4_0900_ai_ci", "columns": _COLS, **kw}  # fmt: skip


def test_a_postgresql_view_is_created_again_with_its_columns_options_and_check_option():
    text = view_create("postgresql", _pg(security_barrier=True, security_invoker="t",
                                         check_option="CASCADED"))  # fmt: skip
    assert text == (
        'CREATE OR REPLACE VIEW "sales"."order_totals" ("id", "total") WITH (security_barrier = '
        "true, security_invoker = true) AS\nSELECT o.id,\n    o.total\n   FROM sales.orders o\n"
        "  WHERE o.note = 'x'\nWITH CASCADED CHECK OPTION;"
    )
    assert view_create("postgresql", _pg()).endswith("WHERE o.note = 'x';")  # no option, no check


def test_a_mysql_definer_view_is_created_invoker_with_its_check_option():
    # A DEFINER view would read its tables with the deploying user's rights: never widened.
    text = view_create("mysql", _my(security_type="DEFINER", check_option="LOCAL"))
    assert view_fields(_my(security_type="definer"))["security_type"] == "DEFINER"
    assert text.startswith(
        "CREATE OR REPLACE SQL SECURITY INVOKER VIEW `shop`.`order_totals` (`id`, `total`) AS\n"
    )
    assert text.endswith("_utf8mb4'x')\nWITH LOCAL CHECK OPTION;")
    assert "DEFINER" not in text.split(" VIEW ")[0].replace("SQL SECURITY INVOKER", "")
    assert "ALGORITHM" not in text


def test_hostile_names_are_quoted_with_the_quote_doubled():
    cols = [{"name": 'a"b', "position": 1, "data_type": "text"}, {"name": "c`d", "position": 2}]
    pg = view_create("postgresql", _pg(schema_name='s"x', view_name='v"; DROP', columns=cols))
    assert pg.startswith('CREATE OR REPLACE VIEW "s""x"."v""; DROP" ("a""b", "c`d") AS')
    my = view_create("mysql", _my(schema_name="s`x", view_name="v`; DROP", columns=cols))
    assert my.startswith("CREATE OR REPLACE SQL SECURITY INVOKER VIEW `s``x`.`v``; DROP` "
                         '(`a"b`, `c``d`) AS')  # fmt: skip


def test_the_columns_are_written_in_their_position_order():
    cols = [{"name": "b", "position": 2}, {"name": "a", "position": 1}]
    assert '("a", "b")' in view_create("postgresql", _pg(columns=cols))
    # A MySQL live read returns the column list as JSON text.
    assert "(`a`, `b`)" in view_create("mysql", _my(columns='[{"name": "b", "position": 2}, '
                                                         '{"name": "a", "position": 1}]'))  # fmt: skip


@pytest.mark.parametrize(
    "engine,change",
    [
        ("postgresql", {"extension_owned": True}),
        ("postgresql", {"definition": None}),
        ("postgresql", {"definition": "  ;"}),
        ("postgresql", {"schema_name": None}),
        ("postgresql", {"columns": []}),
        ("postgresql", {"columns": [{"name": "a", "position": 1}, {"name": "b", "position": 1}]}),
        ("postgresql", {"check_option": "SOMETIMES"}),
        ("mysql", {"security_type": "OWNER"}),
        ("mysql", {"definition": ""}),
        ("mysql", {"view_name": "a\x00b"}),
    ],
)
def test_a_row_that_is_not_a_whole_view_gives_no_statement(engine, change):
    base = _pg if engine == "postgresql" else _my
    assert view_create(engine, base(**change)) is None


def test_mariadb_and_other_engines_carry_no_view():
    assert view_create("mariadb", _my()) is None
    assert view_create("sqlserver", _my()) is None
    assert list(iter_routine_sources("mariadb", procedures=[], triggers=[], views=[_my()])) == []


def test_the_body_status_says_whether_the_create_was_kept():
    assert view_body_status("postgresql", _pg(), include_bodies=True) == "collected"
    assert view_body_status("postgresql", _pg(), include_bodies=False) == "opted_out"
    assert (
        view_body_status("postgresql", _pg(definition=None), include_bodies=True) == "not_readable"
    )
    assert view_body_status("mysql", _my(security_type="?"), include_bodies=True) == "not_supported"
    old = {"view_name": "v", "definition": "SELECT 1"}  # a collection that predates #2082
    assert view_body_status("postgresql", old, include_bodies=True) is None
    assert view_body_status("postgresql", _pg(extension_owned=True), include_bodies=True) is None
    rows = [_pg(), old]
    mark_view_statuses("postgresql", rows, include_bodies=True)
    assert [r.get("body_status") for r in rows] == ["collected", None]


def test_the_collector_output_fields_carry_no_text():
    fields = view_fields({**_pg(), "body_status": "collected"})
    assert fields == {
        "schema_name": "sales", "body_status": "collected", "extension_owned": False,
        "columns": [{"name": "id", "data_type": "integer"},
                    {"name": "total", "data_type": "numeric(10,2)"}],
    }  # fmt: skip
    assert view_fields({"view_name": "v", "definition": "SELECT 1"}) == {}


def test_view_sources_carry_their_session_and_are_exact():
    (pg,) = iter_routine_sources("postgresql", procedures=[], triggers=[],
                                 views=[_pg(search_path=["sales", "public"])])  # fmt: skip
    assert (pg.kind, pg.schema_name, pg.specific_name, pg.origin, pg.exact) == (
        "view", "sales", "order_totals", "pg_get_viewdef", True)  # fmt: skip
    assert pg.session.search_path == ["sales", "public"]
    (my,) = iter_routine_sources("mysql", procedures=[], triggers=[], views=[_my()])
    assert (my.origin, my.session.collation_connection, my.session.search_path) == (
        "information_schema", "utf8mb4_0900_ai_ci", None)  # fmt: skip


def test_a_collection_with_the_body_opt_out_keeps_no_view_create():
    raw = {"metadata": {"engine": "postgresql", "routine_capture": {"include_bodies": False}},
           "views": [_pg()]}  # fmt: skip
    assert list(iter_collection_sources(raw)) == []
    raw["metadata"]["routine_capture"]["include_bodies"] = True
    assert [s.kind for s in iter_collection_sources(raw)] == ["view"]
