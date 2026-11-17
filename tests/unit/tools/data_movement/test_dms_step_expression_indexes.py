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
import unicodedata

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


def _ix(
    name,
    expressions,
    *,
    columns=(),
    unique=False,
    method="btree",
    predicate=None,
    keys=None,
    after="",
):
    """A collected expression index; its ``index_def`` is what ``pg_get_indexdef`` writes for
    it: ``keys`` (default: the expressions) is the key list as written there, ``after`` what
    follows the key list before the predicate (NULLS NOT DISTINCT, INCLUDE, WITH)."""
    definition = (
        f"CREATE {'UNIQUE ' if unique else ''}INDEX {name} ON public.t USING {method} "
        f"({expressions if keys is None else keys}){after}"
        + (f" WHERE {predicate}" if predicate else "")
    )
    return {
        "index_name": name,
        "columns": list(columns),
        "is_unique": unique,
        "index_type": method,
        "predicate": predicate,
        "has_expressions": True,
        "index_expressions": expressions,
        "index_def": definition,
    }


def _pg(tmp_path, **indexes):
    return load_step1(write_pg_job(tmp_path, indexes=indexes), "shop", "job1")


def _creates_index(ddl: str, name: str) -> list[str]:
    return [s.strip() for s in ddl.split("\n") if s.startswith("CREATE") and f'"{name}"' in s]


def _unprintable(text: str) -> list[str]:
    """Every control, format or line separator character of ``text`` but its newlines."""
    return [
        ch
        for ch in text
        if ch != "\n" and unicodedata.category(ch) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
    ]


def _left_out_lines(text: str) -> list[str]:
    head = text.split("Source objects not created", 1)
    if len(head) == 1:
        return []
    """Each listed object's line, its wrapped continuation lines joined to it."""
    entries: list[str] = []
    for ln in head[1].splitlines()[1:]:
        if ln.startswith("-- - "):
            entries.append(ln)
        elif not entries:
            continue  # the heading's own wrapped line
        elif ln.startswith("-- ") and not ln.startswith("-- - "):
            entries[-1] += " " + ln[3:]
        else:
            break
    return entries


# ---------------------------------------------------------------------------
# Recreated
# ---------------------------------------------------------------------------


def test_an_expression_index_on_a_text_cast_column_is_created_again(tmp_path):
    inputs = _pg(tmp_path, audit_log=[_ix("audit_log_lower_msg", "lower((msg)::text)")])

    assert _creates_index(inputs.ddl, "audit_log_lower_msg") == [
        'CREATE INDEX "audit_log_lower_msg" ON "audit_log" ((lower("msg")));'
    ]
    assert inputs.left_out_objects == ()
    assert "Source objects not created" not in render_ddl(inputs)


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
    ("lower(note)", {"columns": ["id"]}, "this release does not recreate indexes that mix"),
    ("lower(note)", {"method": "hash"}, "this release recreates B-tree expression indexes only"),
    ("lower(note)", {"predicate": "(created_at)::date > now()"}, "its predicate is outside"),
    ('"LOWER"(note)', {}, _OUTSIDE),  # a quoted name is another function than lower
    ('"left"(note, \u0663)', {}, _OUTSIDE),  # an Arabic-Indic digit is no length
    ("COALESCE(note, \uff11::text)", {}, _OUTSIDE),  # nor a fullwidth one a number
    ("lower(note)", {"predicate": "(id = \u0663)"}, "its predicate is outside"),
]


@pytest.mark.parametrize("expressions, extra, reason", LEFT_OUT)
def test_an_index_the_ddl_cannot_check_is_listed_not_created(tmp_path, expressions, extra, reason):
    inputs = _pg(tmp_path, orders=[_ix("orders_ix", expressions, **extra)])

    assert _creates_index(inputs.ddl, "orders_ix") == []
    [line] = _left_out_lines(render_ddl(inputs))
    assert line.startswith(f"-- - index public.orders.orders_ix: {reason}")


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
    # control, format and line separator characters inside a literal (#2381 review)
    "COALESCE(note, 'x\x00'), COALESCE(note, 'DROP TABLE orders')",
    "COALESCE(note, 'x\x0b DROP TABLE orders')",
    "COALESCE(note, 'x\u2028DROP TABLE orders')",
    "COALESCE(note, 'x\u2029DROP TABLE orders')",
    "COALESCE(note, 'x\u202eDROP TABLE orders')",
    "COALESCE(note, 'x\u200bDROP TABLE orders')",
    "COALESCE(note, 'x\x85DROP TABLE orders')",
]


@pytest.mark.parametrize("expressions", HOSTILE)
def test_hostile_expression_text_is_refused_and_never_reaches_a_file(tmp_path, expressions):
    inputs = _pg(tmp_path, orders=[_ix("orders_ix", expressions, unique=True)])

    assert _creates_index(inputs.ddl, "orders_ix") == []
    files = render_files(inputs)
    [line] = _left_out_lines(files["step-1.ddl.sql"])
    assert line.startswith(f"-- - index public.orders.orders_ix: {_OUTSIDE}")
    for text in files.values():
        assert "DROP TABLE" not in text
        assert not _unprintable(text)
        assert "TABLESPACE" not in text
        assert "fillfactor" not in text


def test_a_hostile_index_name_stays_inside_its_comment_line(tmp_path):
    name = "ix\nDROP TABLE orders; --"
    inputs = _pg(tmp_path, orders=[_ix(name, "(note IS NULL)")])

    ddl = render_ddl(inputs)
    [line] = _left_out_lines(ddl)
    assert line.startswith("-- - index public.orders.ix DROP TABLE orders; --: ")
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
    assert "The source objects it cannot create are listed below." in ddl
    assert [ln.split(":")[0] for ln in _left_out_lines(ddl)] == [
        "-- - index public.orders.orders_flag"
    ]
    assert _creates_index(inputs.ddl, "orders_lower_note")


def test_a_mysql_functional_index_is_listed_not_created(tmp_path):
    ix = _ix("orders_lower_note", "lower(`note`)")
    inputs = load_step1(write_job(tmp_path, indexes={"orders": [ix]}), "shop", "job1")

    assert "orders_lower_note" not in inputs.ddl
    [line] = _left_out_lines(render_ddl(inputs))
    assert line == (
        "-- - index shop.orders.orders_lower_note: this release recreates PostgreSQL expression "
        "indexes only."
    )


def test_the_delivery_counts_the_indexes_it_does_not_create(tmp_path):
    store = write_pg_job(tmp_path, indexes={"orders": [_ix("orders_flag", "(note IS NULL)"),
                                                       _ix("orders_lower", "lower(note)")]})  # fmt: skip

    assert write_step1_delivery(store, "shop", "job1")["objects_not_created"] == 1


def test_a_bundle_carries_the_indexes_its_ddl_does_not_create():
    left_out = {"table_id": "public.orders", "name": "orders_flag", "reason": "a reason"}
    bundle = make_bundle("aurora_postgresql").model_dump()
    inputs = bundle_inputs(DmsStepBundle.model_validate({**bundle, "left_out_objects": [left_out]}))

    assert _left_out_lines(render_ddl(inputs)) == [
        "-- - index public.orders.orders_flag: a reason."
    ]


# ---------------------------------------------------------------------------
# The source definition says more than the expression (#2381 review)
# ---------------------------------------------------------------------------

_MORE = "its source definition has more than its expression and predicate"

# The four the review reproduced on PostgreSQL 16, then INCLUDE, WITH and a missing text.
SAYS_MORE = [
    ({"keys": "lower(note) text_pattern_ops"}, "an operator class"),
    ({"keys": "lower(note) DESC NULLS LAST"}, "a sort order"),
    ({"keys": 'lower(note) COLLATE "C"', "unique": True}, "a collation"),
    ({"after": " NULLS NOT DISTINCT", "unique": True}, "NULLS NOT DISTINCT"),
    ({"after": " INCLUDE (id)"}, "INCLUDE"),
    ({"after": " WITH (fillfactor='50')"}, "storage parameters"),
]


@pytest.mark.parametrize("extra, what", SAYS_MORE, ids=[w for _, w in SAYS_MORE])
def test_an_index_whose_definition_says_more_is_listed_not_created(tmp_path, extra, what):
    inputs = _pg(tmp_path, orders=[_ix("orders_ix", "lower(note)", **extra)])

    assert _creates_index(inputs.ddl, "orders_ix") == []
    [line] = _left_out_lines(render_ddl(inputs))
    assert line.startswith(f"-- - index public.orders.orders_ix: {_MORE}")


def test_an_index_with_no_collected_definition_is_listed_not_created(tmp_path):
    inputs = _pg(tmp_path, orders=[{**_ix("orders_ix", "lower(note)"), "index_def": None}])

    assert _creates_index(inputs.ddl, "orders_ix") == []
    [line] = _left_out_lines(render_ddl(inputs))
    assert line.startswith(f"-- - index public.orders.orders_ix: {_MORE}")


def test_a_definition_whose_uniqueness_differs_is_listed_not_created(tmp_path):
    ix = {**_ix("orders_ix", "lower(note)", unique=True), "is_unique": False}
    inputs = _pg(tmp_path, orders=[ix])

    assert _creates_index(inputs.ddl, "orders_ix") == []
    [line] = _left_out_lines(render_ddl(inputs))
    assert line.startswith(f"-- - index public.orders.orders_ix: {_MORE}")


# ---------------------------------------------------------------------------
# The header lines stay comment lines
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\x85", "\u202e", "\x0b", "\x00"])
def test_a_bundle_reason_with_a_line_separator_stays_on_its_comment_line(separator):
    left_out = {"table_id": "public.orders", "name": "orders_flag",
                "reason": f"a reason{separator}DROP TABLE orders"}  # fmt: skip
    bundle = make_bundle("aurora_postgresql").model_dump()
    inputs = bundle_inputs(DmsStepBundle.model_validate({**bundle, "left_out_objects": [left_out]}))

    ddl = render_ddl(inputs)
    assert _left_out_lines(ddl) == [
        "-- - index public.orders.orders_flag: a reason DROP TABLE orders."
    ]
    assert not _unprintable(ddl)


# ---------------------------------------------------------------------------
# A plain partial index's predicate (the Aurora draft's path on main too)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["\x00", "\u2028", "\u202e", "\x0b"])
def test_a_plain_partial_index_predicate_with_a_control_character_is_not_carried(tmp_path, bad):
    plain = {"index_name": "orders_open", "columns": ["note"], "is_unique": False,
             "index_type": "btree", "predicate": f"(note = 'a{bad}b')"}  # fmt: skip
    inputs = _pg(tmp_path, orders=[plain])

    [statement] = _creates_index(inputs.ddl, "orders_open")
    assert statement == 'CREATE INDEX "orders_open" ON "orders" ("note");'
    assert not _unprintable(render_ddl(inputs))


@pytest.mark.passes_on_main  # main carries a printable predicate; the guard must keep it
def test_a_plain_partial_index_with_a_printable_predicate_keeps_it(tmp_path):
    plain = {"index_name": "orders_open", "columns": ["note"], "is_unique": False,
             "index_type": "btree", "predicate": "(note = 'caf\u00e9')"}  # fmt: skip
    inputs = _pg(tmp_path, orders=[plain])

    [statement] = _creates_index(inputs.ddl, "orders_open")
    assert statement.endswith("WHERE ( \"note\" = 'caf\u00e9' );")


# ---------------------------------------------------------------------------
# A name no SQL file may carry (#2381 review 2)
# ---------------------------------------------------------------------------

_UNSAFE_NAME = "its name holds a control, format, surrogate or line separator character"


@pytest.mark.parametrize("bad", ["\x00", "\u2028", "\u202e", "\ud800"])
def test_a_recreated_index_with_an_unsafe_name_is_listed_not_created(tmp_path, bad):
    inputs = _pg(
        tmp_path, orders=[_ix(f"a{bad} tail", "lower(note)"), _ix("SELECT 1", "lower(note)")]
    )

    files = render_files(inputs)
    assert not any(_unprintable(t) for t in files.values())
    assert _creates_index(inputs.ddl, "SELECT 1")
    [line] = _left_out_lines(files["step-1.ddl.sql"])
    assert line.startswith(f"-- - index public.orders.a tail: {_UNSAFE_NAME}")


@pytest.mark.parametrize("bad", ["\x00", "\u2028", "\u202e", "\ud800"])
def test_a_plain_index_with_an_unsafe_name_is_listed_not_created(tmp_path, bad):
    plain = [{"index_name": f"a{bad} tail", "columns": ["note"], "is_unique": False},
             {"index_name": "SELECT 1", "columns": ["note"], "is_unique": False}]  # fmt: skip
    inputs = _pg(tmp_path, orders=plain)

    files = render_files(inputs)
    assert not any(_unprintable(t) for t in files.values())
    assert _creates_index(inputs.ddl, "SELECT 1")
    [line] = _left_out_lines(files["step-1.ddl.sql"])
    assert line.startswith(f"-- - index public.orders.a tail: {_UNSAFE_NAME}")


@pytest.mark.parametrize("bad", ["\x00", "\u2028", "\u202e", "\ud800"])
def test_a_column_with_an_unsafe_name_is_listed_not_created(tmp_path, bad):
    extra = {"column_name": f"no{bad}te2", "data_type": "text", "nullable": True,
             "ordinal_position": 4}  # fmt: skip
    store = write_pg_job(tmp_path, columns={"orders": [extra]})
    inputs = load_step1(store, "shop", "job1")

    files = render_files(inputs)
    assert not any(_unprintable(t) for t in files.values())
    # The contract finds no target column for it and blocks its table, which is listed.
    ddl = files["step-1.ddl.sql"]
    assert 'CREATE TABLE "orders"' not in inputs.ddl
    assert "-- - public.orders: no\ufffdte2 has no column in the Aurora DDL" in ddl


@pytest.mark.parametrize(
    "expressions, extra",
    [("COALESCE(note, 'x\ud800')", {}), ("lower(note)", {"predicate": "(note = 'a\ud800')"})],
    ids=["literal", "predicate"],
)
def test_a_lone_surrogate_is_refused_by_both_grammars(tmp_path, expressions, extra):
    inputs = _pg(tmp_path, orders=[_ix("orders_ix", expressions, **extra)])

    assert _creates_index(inputs.ddl, "orders_ix") == []
    files = render_files(inputs)  # a surrogate in a file would not even encode
    assert not any(_unprintable(t) for t in files.values())
    assert _left_out_lines(files["step-1.ddl.sql"])
