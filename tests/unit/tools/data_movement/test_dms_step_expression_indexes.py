# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1's DDL and the source's expression indexes (#2381).

A PostgreSQL expression index (``ON t (lower(email))``) is collected with its expression
text. Step 1 moves the source schema as is: on Aurora PostgreSQL it creates each expression
index whose keys pass the strict grammar (``sql_safety.render_index_expressions``),
re-rendered from the parse; every other one is listed in the DDL file with a fixed reason.
Collector text never reaches the file as written, whatever it holds.
"""

from __future__ import annotations

import re

import pytest

from src.tools.data_movement.dms_step import (
    DmsStepBundle,
    bundle_inputs,
    load_step1,
    render_ddl,
    render_files,
    write_step1_delivery,
)

from .dms_step_fixtures import make_bundle, write_job, write_pg_job


def _ix(name, expressions, *, columns=(), unique=False, method="btree", predicate=None):
    return {
        "index_name": name,
        "columns": list(columns),
        "is_unique": unique,
        "index_type": method,
        "predicate": predicate,
        "has_expressions": True,
        "index_expressions": expressions,
        "index_def": f"CREATE INDEX {name} ON public.t USING {method} ({expressions})",
    }


def _pg(tmp_path, **indexes):
    return load_step1(write_pg_job(tmp_path, indexes=indexes), "shop", "job1")


def _creates_index(ddl: str, name: str) -> list[str]:
    return [s.strip() for s in ddl.split("\n") if s.startswith("CREATE") and f'"{name}"' in s]


def _left_out_lines(text: str) -> list[str]:
    head = text.split("Source indexes not created", 1)
    if len(head) == 1:
        return []
    return [ln for ln in head[1].splitlines()[1:] if ln.startswith("-- - ")]


# ---------------------------------------------------------------------------
# Recreated
# ---------------------------------------------------------------------------


def test_an_expression_index_on_a_text_cast_column_is_created_again(tmp_path):
    inputs = _pg(tmp_path, audit_log=[_ix("audit_log_lower_msg", "lower((msg)::text)")])

    assert _creates_index(inputs.ddl, "audit_log_lower_msg") == [
        'CREATE INDEX "audit_log_lower_msg" ON "audit_log" ((lower("msg")));'
    ]
    assert inputs.left_out_indexes == ()
    assert "Source indexes not created" not in render_ddl(inputs)


def test_a_unique_partial_expression_index_keeps_its_uniqueness_and_predicate(tmp_path):
    ix = _ix("orders_note_key", "COALESCE(note, ''::text), md5(note)", unique=True,
             predicate="(note IS NOT NULL)")  # fmt: skip
    inputs = _pg(tmp_path, orders=[ix])

    assert _creates_index(inputs.ddl, "orders_note_key") == [
        'CREATE UNIQUE INDEX "orders_note_key" ON "orders" '
        '((COALESCE("note", \'\'::text)), (md5("note"))) WHERE ( "note" IS NOT NULL );'
    ]


def test_the_recreated_index_comes_after_the_tables_and_before_the_foreign_keys(tmp_path):
    inputs = _pg(tmp_path, audit_log=[_ix("audit_log_lower_msg", "lower((msg)::text)")])

    ddl = inputs.ddl
    assert ddl.rindex("CREATE TABLE") < ddl.index('CREATE INDEX "audit_log_lower_msg"')


# ---------------------------------------------------------------------------
# Listed, with a fixed reason
# ---------------------------------------------------------------------------

_OUTSIDE = "its expression is outside what this release can check"

LEFT_OUT = [
    # (expression, extra index fields, the reason's start)
    ("(note IS NULL)", {}, _OUTSIDE),  # a boolean key
    ("((note)::jsonb ->> 'a'::text)", {}, _OUTSIDE),  # a JSON path
    ("pg_sleep(10)", {}, _OUTSIDE),  # a function outside the set
    ("lower(note) || 'x'", {}, _OUTSIDE),  # an operator
    ("lower((id)::text)", {}, _OUTSIDE),  # a cast that changes a non-text column's type
    ("lower(nope)", {}, _OUTSIDE),  # not a column of the table
    ("note", {}, _OUTSIDE),  # a bare column: no expression at all
    ("lower(note)", {"columns": ["id"]}, "its keys mix columns and expressions"),
    ("lower(note)", {"method": "hash"}, "this release recreates B-tree expression indexes only"),
    ("lower(note)", {"predicate": "(created_at)::date > now()"}, "its predicate is outside"),
]


@pytest.mark.parametrize("expressions, extra, reason", LEFT_OUT)
def test_an_index_the_ddl_cannot_check_is_listed_not_created(tmp_path, expressions, extra, reason):
    inputs = _pg(tmp_path, orders=[_ix("orders_ix", expressions, **extra)])

    assert _creates_index(inputs.ddl, "orders_ix") == []
    [line] = _left_out_lines(render_ddl(inputs))
    assert line.startswith(f"-- - public.orders.orders_ix: {reason}")


HOSTILE = [
    "lower(note)); DROP TABLE orders; --",
    "lower(note)) /* x */",
    "lower(note)\nDROP TABLE orders",
    "lower('a\\'); DROP TABLE orders; --')",
    'lower("no""te")',
    "lower(note), lower(note)" + ", lower(note)" * 40,  # over the length cap
    "lower(" * 10 + "note" + ")" * 10,  # nested past the depth cap
    "lower(note)) WITH (fillfactor=10",
    "lower(note)) TABLESPACE evil (",
    "COALESCE(note, 'x''; DROP TABLE orders; --')",  # a second statement inside a literal
]


@pytest.mark.parametrize("expressions", HOSTILE)
def test_hostile_expression_text_is_refused_and_never_reaches_a_file(tmp_path, expressions):
    inputs = _pg(tmp_path, orders=[_ix("orders_ix", expressions, unique=True)])

    assert _creates_index(inputs.ddl, "orders_ix") == []
    files = render_files(inputs)
    [line] = _left_out_lines(files["step-1.ddl.sql"])
    assert line.startswith(f"-- - public.orders.orders_ix: {_OUTSIDE}")
    for text in files.values():
        assert "DROP TABLE" not in text
        assert "TABLESPACE" not in text
        assert "fillfactor" not in text


def test_a_hostile_index_name_stays_inside_its_comment_line(tmp_path):
    name = "ix\nDROP TABLE orders; --"
    inputs = _pg(tmp_path, orders=[_ix(name, "(note IS NULL)")])

    ddl = render_ddl(inputs)
    [line] = _left_out_lines(ddl)
    assert line.startswith("-- - public.orders.ix DROP TABLE orders; --: ")
    assert not any(re.match(r"\s*DROP", ln) for ln in ddl.splitlines())


def test_a_recreated_index_with_a_hostile_name_is_quoted(tmp_path):
    name = 'ix"; DROP TABLE orders; --'
    inputs = _pg(tmp_path, orders=[_ix(name, "lower(note)")])

    [statement] = [s for s in inputs.ddl.splitlines() if s.startswith("CREATE INDEX")]
    assert statement == 'CREATE INDEX "ix""; DROP TABLE orders; --" ON "orders" ((lower("note")));'


def test_the_ddl_file_says_which_indexes_it_leaves_and_why(tmp_path):
    inputs = _pg(tmp_path, orders=[_ix("orders_flag", "(note IS NULL)"),
                                   _ix("orders_lower_note", "lower(note)")])  # fmt: skip

    ddl = render_ddl(inputs)
    assert "The source indexes it cannot create are listed below." in ddl
    assert [ln.split(":")[0] for ln in _left_out_lines(ddl)] == ["-- - public.orders.orders_flag"]
    assert _creates_index(inputs.ddl, "orders_lower_note")


def test_a_mysql_functional_index_is_listed_not_created(tmp_path):
    ix = _ix("orders_lower_note", "lower(`note`)")
    inputs = load_step1(write_job(tmp_path, indexes={"orders": [ix]}), "shop", "job1")

    assert "orders_lower_note" not in inputs.ddl
    [line] = _left_out_lines(render_ddl(inputs))
    assert line == (
        "-- - shop.orders.orders_lower_note: this release recreates PostgreSQL expression "
        "indexes only."
    )


def test_the_delivery_counts_the_indexes_it_does_not_create(tmp_path):
    store = write_pg_job(tmp_path, indexes={"orders": [_ix("orders_flag", "(note IS NULL)"),
                                                       _ix("orders_lower", "lower(note)")]})  # fmt: skip

    assert write_step1_delivery(store, "shop", "job1")["indexes_not_created"] == 1


def test_a_bundle_carries_the_indexes_its_ddl_does_not_create():
    left_out = {"table_id": "public.orders", "index_name": "orders_flag", "reason": "a reason"}
    bundle = make_bundle("aurora_postgresql").model_dump()
    inputs = bundle_inputs(DmsStepBundle.model_validate({**bundle, "left_out_indexes": [left_out]}))

    assert _left_out_lines(render_ddl(inputs)) == ["-- - public.orders.orders_flag: a reason."]
