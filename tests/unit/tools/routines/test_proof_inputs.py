# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Test calls and synthetic rows for routines (#1942): edge values per type family,
argument modes, trigger row changes, foreign-key order, determinism, caps, and no
literal of the collection in the output."""

from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from src.tools.database import disposable_engine as de
from src.tools.routines import proof_inputs as pi
from src.tools.routines.proof_inputs import (
    MAX_CALLS_PER_ROUTINE,
    PROOF_ROWS,
    RoutineParameter,
    RoutineProofInput,
    build_proof_inputs,
    iter_routine_calls,
    parameter_cases,
    routine_calls,
)
from tests.unit.tools.sql._timing import assert_linear

SENTINEL = "zq9xliteral"


def _col(name, data_type, nullable=False, **kw):
    return {"column_name": name, "data_type": data_type, "nullable": nullable, **kw}


def _fk(name, cols, table, refs):
    return {
        "constraint_name": name,
        "columns": cols,
        "referenced_table": table,
        "referenced_columns": refs,
    }


def _table(name, columns, pk=("id",), fks=(), row_count=100, **kw):
    return {
        "table_id": f"app.{name}",
        "table_name": name,
        "schema_name": "app",
        "row_count": row_count,
        "primary_key": list(pk),
        "columns": columns,
        "foreign_keys": list(fks),
        **kw,
    }


CUSTOMERS = _table(
    "customers",
    [
        _col("id", "integer"),
        _col("name", "character varying", max_length=30),
        _col(
            "tier",
            "character varying",
            max_length=8,
            default_value=f"'{SENTINEL}'::character varying",
        ),
        _col("created", "timestamp without time zone", default_value="now()"),
    ],
    indexes=[
        {"index_name": "c_name", "columns": ["name"], "is_unique": True},
        {
            "index_name": "c_part",
            "columns": ["tier"],
            "is_unique": False,
            "predicate": f"tier = '{SENTINEL}'",
        },
    ],
)
ORDERS = _table(
    "orders",
    [
        _col("id", "bigint"),
        _col("customer_id", "integer"),
        _col("total", "numeric(8,2)", True),
    ],
    fks=[_fk("o_customer", ["customer_id"], "customers", ["id"])],
)
AUDIT = _table("audit", [_col("id", "integer", is_auto_increment=True), _col("note", "text", True)])
TABLES = [CUSTOMERS, ORDERS, AUDIT]


def _p(data_type, name="x", mode="IN", **kw):
    return RoutineParameter(name=name, data_type=data_type, mode=mode, **kw)


def _fn(name, *params, kind="function", tables=(), returns="integer"):
    return RoutineProofInput(
        routine_id=f"app.{name}",
        kind=kind,
        name=f"app.{name}",
        parameters=params,
        returns=returns,
        tables=tables,
    )


def _trigger(name, table, events, timing="AFTER", columns=()):
    return RoutineProofInput(
        routine_id=f"app.{table}.{name}",
        kind="trigger",
        name=name,
        trigger={"table": table, "timing": timing, "events": events, "columns": columns},
    )


def _labels(cases):
    return [label for label, _ in cases]


# ----------------------------------------------------------------------------- edge values


# fmt: off
@pytest.mark.parametrize(
    ("data_type", "kw", "dialect", "expected"),
    [
        ("integer", {}, "postgresql",
         [("null", None), ("zero", 0), ("max", 2147483647), ("min", -2147483648), ("minus_one", -1)]),
        ("smallint", {}, "postgresql",
         [("null", None), ("zero", 0), ("max", 32767), ("min", -32768), ("minus_one", -1)]),
        ("bigint", {}, "mysql",
         [("null", None), ("zero", 0), ("max", 2**63 - 1), ("min", -(2**63)), ("minus_one", -1)]),
        ("int unsigned", {}, "mysql", [("null", None), ("zero", 0), ("max", 4294967295), ("min", 0)]),
        ("tinyint(1)", {}, "mysql",
         [("null", None), ("zero", 0), ("max", 127), ("min", -128), ("minus_one", -1)]),
        ("numeric(5,2)", {}, "postgresql",
         [("null", None), ("zero", "0"), ("max", "999.99"), ("min", "-999.99"),
          ("smallest_step", "0.01"), ("nan", "NaN")]),
        ("decimal", {"numeric_precision": 3, "numeric_scale": 0}, "mysql",
         [("null", None), ("zero", "0"), ("max", "999"), ("min", "-999"), ("smallest_step", "1")]),
        ("double precision", {}, "postgresql",
         [("null", None), ("zero", 0.0), ("negative", -1.5), ("max", 1.7976931348623157e308),
          ("nan", "NaN"), ("infinity", "Infinity"), ("-infinity", "-Infinity")]),
        ("float", {}, "mysql", [("null", None), ("zero", 0.0), ("negative", -1.5), ("max", 3.4e38)]),
        ("character varying(3)", {}, "postgresql",
         [("null", None), ("empty", ""), ("one_char", "a"), ("max_length", "zzz")]),
        ("varchar(10)", {}, "mysql",
         [("null", None), ("empty", ""), ("one_char", "a"), ("quote", "it's"),
          ("non_ascii", "été"), ("max_length", "z" * 10)]),
        ("text", {}, "postgresql",
         [("null", None), ("empty", ""), ("one_char", "a"), ("quote", "it's"), ("non_ascii", "été")]),
        ("date", {}, "postgresql",
         [("null", None), ("min", "0001-01-01"), ("max", "9999-12-31"),
          ("infinity", "infinity"), ("-infinity", "-infinity")]),
        ("date", {}, "mysql", [("null", None), ("min", "1000-01-01"), ("max", "9999-12-31")]),
        ("timestamp", {}, "mysql",
         [("null", None), ("min", "1970-01-02 00:00:00"), ("max", "2038-01-18 00:00:00")]),
        ("datetime", {}, "mysql",
         [("null", None), ("min", "1000-01-01 00:00:00"), ("max", "9999-12-31 23:59:59")]),
        ("timestamp with time zone", {}, "postgresql",
         [("null", None), ("min", "0001-01-01 00:00:00"), ("max", "9999-12-31 23:59:59"),
          ("infinity", "infinity"), ("-infinity", "-infinity")]),
        ("time", {}, "mysql",
         [("null", None), ("min", "00:00:00"), ("max", "23:59:59"), ("lowest", "-838:59:59"),
          ("highest", "838:59:59")]),
        ("boolean", {}, "postgresql", [("null", None), ("true", True), ("false", False)]),
        ("uuid", {}, "postgresql",
         [("null", None), ("nil", "00000000-0000-0000-0000-000000000000"),
          ("max", "ffffffff-ffff-ffff-ffff-ffffffffffff")]),
        ("jsonb", {}, "postgresql",
         [("null", None), ("empty_object", "{}"), ("empty_array", "[]"), ("json_null", "null")]),
        ("year", {}, "mysql", [("null", None), ("min", 1901), ("max", 2155)]),
        ("integer[]", {}, "postgresql", [("null", None), ("empty", "{}"), ("null_element", "{NULL}")]),
        ("ARRAY", {}, "postgresql", [("null", None), ("empty", "{}"), ("null_element", "{NULL}")]),
        ("interval", {}, "postgresql", [("null", None)]),
    ],
)
# fmt: on
def test_each_type_family_gets_its_edge_values(data_type, kw, dialect, expected):
    assert parameter_cases(_p(data_type, **kw), dialect) == expected


def test_a_string_edge_never_exceeds_the_declared_length():
    for n in (1, 2, 3, 4, 5):
        cases = parameter_cases(_p("varchar", max_length=n), "mysql")
        assert all(v is None or len(v) <= n for _, v in cases)
        assert ("max_length", "z" * n) in cases
    assert _labels(parameter_cases(_p("char(1)"), "postgresql")) == [
        "null",
        "empty",
        "one_char",
        "max_length",
    ]


# ------------------------------------------------------------------------------ calls


def test_a_call_per_edge_value_with_the_other_arguments_at_baseline():
    calls, cut = routine_calls(_fn("f", _p("integer", "a"), _p("text", "b")), "mysql")
    assert not cut
    assert calls[0].label == "baseline" and calls[0].args == (1, "a")
    by_label = {c.label: c.args for c in calls}
    assert by_label["a=null"] == (None, "a")
    assert by_label["a=max"] == (2147483647, "a")
    assert by_label["b=empty"] == (1, "")
    assert by_label["all_null"] == (None, None)
    a_cases = len(parameter_cases(_p("integer"), "mysql"))
    b_cases = len(parameter_cases(_p("text"), "mysql"))
    # b=one_char repeats the baseline, so it is not called twice
    assert len(calls) == 1 + a_cases + b_cases - 1 + 1
    assert len({c.args for c in calls}) == len(calls)


def test_a_routine_without_parameters_gets_one_call():
    calls, _ = routine_calls(_fn("now_ish"), "postgresql")
    assert [(c.label, c.args, c.modes) for c in calls] == [("baseline", (), ())]


def test_postgresql_arguments_are_bound_as_text_so_they_resolve_to_any_parameter_type():
    calls, _ = routine_calls(
        _fn("f", _p("smallint"), _p("boolean", "b"), _p("real", "r")), "postgresql"
    )
    assert calls[0].args == ("1", "true", "1.0")
    assert all(a is None or isinstance(a, str) for c in calls for a in c.args)


def test_out_arguments_are_left_out_of_a_postgresql_function_call():
    f = _fn("split", _p("integer", "a"), _p("integer", "lo", "OUT"), _p("integer", "hi", "TABLE"))
    calls, _ = routine_calls(f, "postgresql")
    assert {c.modes for c in calls} == {("IN",)}
    assert {len(c.args) for c in calls} == {1}


def test_a_procedure_takes_out_as_null_and_varies_inout():
    p = _fn(
        "p",
        _p("integer", "a"),
        _p("text", "io", "INOUT"),
        _p("bigint", "o", "out"),
        kind="procedure",
    )
    for dialect in ("postgresql", "mysql"):
        calls, _ = routine_calls(p, dialect)
        assert {c.modes for c in calls} == {("IN", "INOUT", "OUT")}
        assert all(c.args[2] is None for c in calls)
        labels = {c.label for c in calls}
        assert "io=empty" in labels and not any(label.startswith("o=") for label in labels)


def test_an_unnamed_parameter_is_labelled_by_position():
    calls, _ = routine_calls(_fn("f", RoutineParameter(data_type="integer")), "mysql")
    assert "$1=null" in {c.label for c in calls}


def test_trigger_functions_and_variadic_routines_are_skipped_with_why():
    tf = _fn("audit_fn", returns="trigger")
    var = _fn("total", _p("integer[]", "xs", "VARIADIC"))
    out = list(iter_routine_calls([tf, var, _fn("ok")], "postgresql"))
    assert [type(o).__name__ for o in out] == ["SkippedRoutine", "SkippedRoutine", "ProofCall"]
    assert "trigger" in out[0].reason and "VARIADIC" in out[1].reason
    with pytest.raises(ValueError, match="VARIADIC"):
        routine_calls(var, "postgresql")


def test_calls_are_capped_per_routine():
    many = _fn("wide", *[_p("integer", f"a{i}") for i in range(30)])
    calls, cut = routine_calls(many, "mysql")
    assert cut and len(calls) == MAX_CALLS_PER_ROUTINE
    inputs = build_proof_inputs([many], [], dialect="mysql")
    assert inputs.truncated == ("app.wide",)


def test_the_input_refuses_a_trigger_without_its_spec_and_a_spec_on_a_function():
    with pytest.raises(ValidationError):
        RoutineProofInput(routine_id="t", kind="trigger", name="t")
    with pytest.raises(ValidationError):
        RoutineProofInput(
            routine_id="f",
            kind="function",
            name="f",
            trigger={"table": "t", "timing": "AFTER", "events": ["INSERT"]},
        )
    spec = _trigger("t", "orders", "insert or update").trigger
    assert spec.events == ("INSERT", "UPDATE")


# ------------------------------------------------------------------------- triggers


def _sql(call, dialect="postgresql"):
    """The statement ``run_call`` builds for a row change."""
    return de.row_change_sql(dialect, call.kind, call.name, call.columns, call.where)


def _changes(inputs, routine_id):
    return [c for c in inputs.calls if c.routine_id == routine_id]


@pytest.mark.parametrize("dialect", ["postgresql", "mysql"])
def test_a_trigger_gets_the_row_change_of_each_event(dialect):
    t = _trigger("orders_audit", "orders", ["DELETE", "INSERT", "UPDATE"])
    inputs = build_proof_inputs([t], TABLES, dialect=dialect, seed=1)
    changes = _changes(inputs, t.routine_id)
    assert [c.kind for c in changes] == ["INSERT", "UPDATE", "DELETE"]
    assert {c.action for c in changes} == {"row_change"} and {c.name for c in changes} == {"orders"}
    q = '"' if dialect == "postgresql" else "`"
    ins, upd, dele = changes
    assert _sql(ins, dialect) == (
        f"INSERT INTO {q}orders{q} ({q}id{q}, {q}customer_id{q}, {q}total{q}) "
        "VALUES (%s, %s, %s)"
    )
    assert _sql(upd, dialect) == f"UPDATE {q}orders{q} SET {q}total{q} = %s WHERE {q}id{q} = %s"
    assert _sql(dele, dialect) == f"DELETE FROM {q}orders{q} WHERE {q}id{q} = %s"
    rows = dict(inputs.setup.rows)["orders"]
    seeded = {r["id"] for r in rows}
    assert ins.args[0] not in seeded  # a held-back row: its key is new
    assert ins.args[1] in {r["id"] for r in dict(inputs.setup.rows)["customers"]}
    assert upd.args[1] == rows[0]["id"] and dele.args == (rows[-1]["id"],)


def test_the_row_changes_run_before_the_calls():
    t = _trigger("orders_audit", "orders", ["INSERT"])
    inputs = build_proof_inputs([_fn("aaa"), t], TABLES, dialect="postgresql")
    assert [c.action for c in inputs.calls] == ["row_change", "call"]


def test_an_update_of_trigger_sets_its_column_and_a_key_keeps_its_value():
    t = _trigger("t_total", "orders", ["UPDATE"], columns=("TOTAL",))
    upd = _changes(build_proof_inputs([t], TABLES, dialect="postgresql"), t.routine_id)[0]
    assert _sql(upd).startswith('UPDATE "orders" SET "total" = %s')
    k = _trigger("t_key", "orders", ["UPDATE"], columns=("customer_id",))
    inputs = build_proof_inputs([k], TABLES, dialect="postgresql")
    upd = _changes(inputs, k.routine_id)[0]
    assert _sql(upd).startswith('UPDATE "orders" SET "customer_id" = %s')
    assert upd.args[0] == dict(inputs.setup.rows)["orders"][0]["customer_id"]


def test_two_triggers_on_one_table_insert_and_delete_different_rows():
    a = _trigger("a", "orders", ["INSERT", "DELETE"])
    b = _trigger("b", "orders", ["INSERT", "DELETE"])
    calls = build_proof_inputs([a, b], TABLES, dialect="mysql").calls
    inserts = [c.args[0] for c in calls if c.kind == "INSERT"]
    deletes = [c.args for c in calls if c.kind == "DELETE"]
    assert len(set(inserts)) == 2 and len(set(deletes)) == 2


def test_a_table_without_a_key_is_matched_on_its_comparable_columns():
    log = _table(
        "log",
        [_col("at", "date"), _col("score", "double precision"), _col("note", "text", True)],
        pk=(),
    )
    t = _trigger("log_del", "log", ["DELETE"])
    inputs = build_proof_inputs([t], [log], dialect="postgresql")
    dele = _changes(inputs, t.routine_id)[0]
    assert _sql(dele).startswith('DELETE FROM "log" WHERE "at" = %s')
    assert '"score"' not in _sql(dele)  # a float never decides which row is matched


@pytest.mark.parametrize(
    ("trigger", "reason"),
    [
        (_trigger("v", "orders", ["INSERT"], timing="INSTEAD OF"), "view"),
        (_trigger("tr", "orders", ["TRUNCATE"]), "TRUNCATE"),
        (_trigger("gone", "nowhere", ["INSERT"]), "not in the collection"),
    ],
)
def test_a_trigger_with_no_row_change_to_generate_is_skipped_with_why(trigger, reason):
    inputs = build_proof_inputs([trigger], TABLES, dialect="postgresql")
    assert not _changes(inputs, trigger.routine_id)
    assert [s.routine_id for s in inputs.skipped] == [trigger.routine_id]
    assert reason in inputs.skipped[0].reason


def test_a_trigger_on_an_unseeded_table_is_skipped_with_the_tables_reason():
    shapes = _table("shapes", [_col("id", "integer"), _col("g", "interval")])
    t = _trigger("s_ins", "shapes", ["INSERT"])
    inputs = build_proof_inputs([t], [shapes], dialect="postgresql")
    assert "no synthetic rows" in inputs.skipped[0].reason
    assert dict(inputs.setup.unseeded)["app.shapes"]


# ---------------------------------------------------------------------------- setup


def test_the_setup_creates_every_touched_table_and_its_parents_in_order():
    f = _fn("order_total", _p("integer"), tables=("orders",))
    inputs = build_proof_inputs([f], TABLES, dialect="postgresql")
    assert inputs.setup.tables == ("app.customers", "app.orders")  # audit is not touched
    assert [t for t, _ in inputs.setup.rows] == ["customers", "orders"]
    ddl = inputs.setup.ddl
    creates = [d for d in ddl if d.startswith("CREATE TABLE")]
    assert [d.split('"')[1] for d in creates] == ["customers", "orders"]
    assert ddl[-1].startswith('ALTER TABLE "orders" ADD CONSTRAINT "o_customer" FOREIGN KEY')
    assert all(len(rows) <= PROOF_ROWS + 4 for _, rows in inputs.setup.rows)


def test_a_table_a_routine_names_that_the_collection_lacks_is_listed():
    f = _fn("f", tables=("orders", "ghost"))
    inputs = build_proof_inputs([f], TABLES, dialect="mysql")
    assert inputs.setup.missing == (("app.f", "ghost"),)


def test_a_name_that_is_not_a_plain_identifier_gets_no_rows():
    odd = _table("odd-name", [_col("id", "int")])
    inputs = build_proof_inputs([_fn("f", tables=("odd-name",))], [odd], dialect="mysql")
    assert inputs.setup.rows == ()
    assert "plain identifier" in dict(inputs.setup.unseeded)["app.odd-name"]


def test_two_tables_of_one_name_keep_the_first():
    other = dict(ORDERS, table_id="sales.orders", schema_name="sales", foreign_keys=[])
    f = _fn("f", tables=("app.orders", "sales.orders"))
    inputs = build_proof_inputs([f], TABLES + [other], dialect="mysql")
    assert "sales.orders" not in inputs.setup.tables
    assert "same name" in dict(inputs.setup.unseeded)["sales.orders"]


def test_no_literal_of_the_collection_reaches_the_setup_or_the_calls():
    """A literal default becomes a plain value of the column's kind, a default with
    no literal stays, and a partial index (its predicate holds a literal) is left out."""
    f = _fn("f", _p("text"), tables=("customers",))
    t = _trigger("c_ins", "customers", ["INSERT", "UPDATE", "DELETE"])
    for dialect in ("postgresql", "mysql"):
        out = build_proof_inputs([f, t], TABLES, dialect=dialect, seed=4).to_json()
        assert SENTINEL not in out
    ddl = "\n".join(build_proof_inputs([f], TABLES, dialect="postgresql").setup.ddl)
    assert "\"tier\" VARCHAR(8) NOT NULL DEFAULT ''" in ddl
    assert "DEFAULT CURRENT_TIMESTAMP" in ddl
    assert '"c_name"' in ddl and '"c_part"' not in ddl


# ---------------------------------------------------------------------- determinism


def _everything(dialect, seed):
    routines = [
        _fn("order_total", _p("integer", "cid"), tables=("orders",)),
        _fn(
            "add",
            _p("numeric(8,2)", "amt"),
            _p("text", "io", "INOUT"),
            kind="procedure",
            tables=("orders", "audit"),
        ),
        _trigger("orders_audit", "orders", ["INSERT", "UPDATE", "DELETE"]),
    ]
    return build_proof_inputs(routines, TABLES, dialect=dialect, seed=seed)


@pytest.mark.parametrize("dialect", ["postgresql", "mysql"])
def test_the_same_inputs_and_seed_give_byte_identical_output(dialect):
    first = _everything(dialect, 7).to_json()
    assert first == _everything(dialect, 7).to_json()
    assert json.loads(first)["seed"] == 7
    # the routines' and tables' order does not matter
    shuffled = build_proof_inputs(
        list(
            reversed(
                [
                    _fn("order_total", _p("integer", "cid"), tables=("orders",)),
                    _fn(
                        "add",
                        _p("numeric(8,2)", "amt"),
                        _p("text", "io", "INOUT"),
                        kind="procedure",
                        tables=("orders", "audit"),
                    ),
                    _trigger("orders_audit", "orders", ["INSERT", "UPDATE", "DELETE"]),
                ]
            )
        ),
        list(reversed(TABLES)),
        dialect=dialect,
        seed=7,
    )
    assert shuffled.to_json() == first
    other = _everything(dialect, 8)
    assert other.to_json() != first
    assert other.calls[3:] == _everything(dialect, 7).calls[3:]  # calls do not use the seed


# ---------------------------------------------------------------------------- scale


def _library(n):
    return [
        RoutineProofInput.model_construct(
            routine_id=f"app.f{i}",
            kind="function",
            name=f"app.f{i}",
            parameters=(
                RoutineParameter.model_construct(
                    name="a",
                    data_type="integer",
                    mode="IN",
                    max_length=None,
                    numeric_precision=None,
                    numeric_scale=None,
                ),
            ),
            returns="integer",
            trigger=None,
            tables=(),
        )
        for i in range(n)
    ]


def _count_calls(routines):
    return sum(1 for _ in iter_routine_calls(routines, "postgresql"))


def test_100k_routines_stream_their_calls_in_linear_time():
    """The calls of 100,000 routines are generated one routine at a time, and ten times
    the routines take about ten times as long (the shared relative timing check: a
    ratio against the same process's own 10,000-routine run)."""
    per = len(parameter_cases(RoutineParameter(data_type="integer"), "postgresql")) + 1
    assert _count_calls(_library(100_000)) == 100_000 * per
    assert_linear(_library, _count_calls, max_rounds=4, sizes=(10_000, 100_000))


def test_rows_are_capped_whatever_the_row_count():
    huge = [dict(t, row_count=10**12) for t in TABLES]
    inputs = build_proof_inputs([_fn("f", tables=("orders", "audit"))], huge, dialect="mysql")
    edges = {"customers": 2, "orders": 1, "audit": 0}
    for table, rows in inputs.setup.rows:
        assert len(rows) <= max(PROOF_ROWS, edges[table])


def test_the_module_never_takes_a_routines_text():
    assert "create_sql" not in RoutineProofInput.model_fields
    assert not hasattr(pi.ProofCall, "create_sql")


# ------------------------------------------------------------------- review round (#1969)


def _flags(dialect, sentinel=SENTINEL):
    """A table with a NOT NULL literal default on each short or date-like type, an
    ENUM and a SET (MySQL), and an expression index, each holding ``sentinel``."""
    q = "'" if dialect == "postgresql" else ""  # how each collector reports a default
    my = dialect == "mysql"
    return _table("flags", [
        _col("id", "integer"),
        _col("flag", "char(1)", max_length=1, default_value=f"{q}{sentinel}{q}"),
        _col("code", "varchar(2)", max_length=2, default_value=f"{q}{sentinel}{q}"),
        _col("d", "date", default_value=f"{q}2001-02-03{q}"),
        _col("dt", "datetime" if my else "timestamp without time zone",
             default_value=f"{q}2001-02-03 04:05:06{q}"),
        _col("tm", "time", default_value=f"{q}01:02:03{q}"),
        _col("u", "char(36)" if my else "uuid", max_length=36 if my else None,
             default_value=f"{q}{sentinel}{q}"),
        _col("e", f"enum('{sentinel}','b')" if my else "text", default_value=f"{q}{sentinel}{q}"),
        _col("st", f"set('{sentinel}','x')" if my else "text", True),
        _col("y", "year" if my else "integer", default_value="1999"),
    ], indexes=[
        {"index_name": "fx", "columns": ["code"], "is_unique": False, "has_expressions": True,
         "index_expressions": f"lower('{sentinel}')",
         "index_def": f"CREATE INDEX fx ON flags (lower('{sentinel}'))"},
        {"index_name": "fp", "columns": ["code"], "is_unique": False,
         "predicate": f"code <> '{sentinel}'"},
    ])  # fmt: skip


@pytest.mark.parametrize(
    ("dialect", "expected"),
    [
        ("mysql", ["`flag` CHAR(1) NOT NULL DEFAULT ''", "`code` VARCHAR(2) NOT NULL DEFAULT ''",
                   "`d` DATE NOT NULL DEFAULT '2024-01-01'",
                   "`dt` DATETIME NOT NULL DEFAULT '2024-01-01 00:00:00'",
                   "`tm` TIME NOT NULL DEFAULT '00:00:00'", "`u` CHAR(36) NOT NULL DEFAULT ''",
                   "`e` ENUM('e1','e2') NOT NULL DEFAULT 'e1'", "`st` SET('e1','e2')",
                   "`y` YEAR NOT NULL DEFAULT '2024'"]),
        ("postgresql", ['"flag" CHAR(1) NOT NULL DEFAULT \'\'',
                        "\"d\" DATE NOT NULL DEFAULT '2024-01-01'",
                        "\"dt\" TIMESTAMP NOT NULL DEFAULT '2024-01-01 00:00:00'",
                        "\"tm\" TIME NOT NULL DEFAULT '00:00:00'",
                        "\"u\" UUID NOT NULL DEFAULT '00000000-0000-0000-0000-000000000000'"]),
    ],
)  # fmt: skip
def test_a_literal_default_becomes_a_plain_loadable_value_of_its_type(dialect, expected):
    """MySQL reports a default unquoted, so its empty string is '' (never ''''''); a
    date, time, timestamp, uuid and year default keeps a value of its own type."""
    ddl = build_proof_inputs([_fn("f", tables=("flags",))], [_flags(dialect)], dialect=dialect)
    create = ddl.setup.ddl[0]
    for line in expected:
        assert line in create, line
    assert "''''" not in create


@pytest.mark.parametrize("dialect", ["postgresql", "mysql"])
def test_no_literal_of_the_collection_reaches_any_part_of_the_output(dialect):
    """Defaults, a partial index's predicate, an expression index's text and a MySQL
    ENUM or SET value list all hold the sentinel; none of it reaches the DDL, the rows
    or the calls."""
    f = _fn("f", _p("text"), tables=("flags",))
    t = _trigger("f_all", "flags", ["INSERT", "UPDATE", "DELETE"])
    out = build_proof_inputs([f, t], [_flags(dialect)], dialect=dialect, seed=3)
    assert SENTINEL not in out.to_json()
    assert not out.setup.unseeded  # the NOT NULL ENUM column gets placeholder values
    if dialect == "mysql":
        rows = dict(out.setup.rows)["flags"]
        assert {r["e"] for r in rows} <= {"e1", "e2"}


def test_postgresql_calls_cast_each_argument_to_its_declared_type():
    f = _fn("f", _p("integer"), _p("character varying(12)", "s"), _p("numeric(10, 2)", "m"),
            _p('"char"', "c"), _p('public."Mood"', "e", is_enum=True))  # fmt: skip
    calls, _ = routine_calls(f, "postgresql")
    assert {c.casts for c in calls} == {
        ("integer", "character varying(12)", "numeric(10, 2)", '"char"', '"public"."Mood"')
    }
    assert {c.casts for c in routine_calls(f, "mysql")[0]} == {()}


@pytest.mark.parametrize(
    "data_type", ["boolean and pg_sleep(3) is null", "ARRAY", "USER-DEFINED", "interval day"]
)
def test_a_routine_with_a_type_no_call_can_cast_to_gets_no_call_and_says_why(data_type):
    """Never an uncast call: it could reach another overload. The routine is skipped
    with the parameter named, and the type text never reaches a call."""
    f = _fn("f", _p("integer", "a"), _p(data_type, "b"))
    out = build_proof_inputs([f], [], dialect="postgresql")
    assert [c for c in out.calls if c.routine_id == "app.f"] == []
    assert [(s.routine_id, s.reason) for s in out.skipped] == [
        ("app.f", "parameter b has a type no call can cast to, so no call is generated")
    ]
    assert "pg_sleep" not in out.to_json()
    with pytest.raises(ValueError, match="no call can cast to"):
        routine_calls(f, "postgresql")
    assert routine_calls(f, "mysql")[0]  # MySQL has no overloads and writes no cast


def _pg_handle():
    """A stand-in engine handle: anything that reached SQL would fail on it differently."""
    return SimpleNamespace(engine="postgresql", stopped=False, _backend=None)


@pytest.mark.parametrize(
    "change",
    [
        {"casts": ("boolean and pg_sleep(3) is null",)},
        {"casts": (None,)},
        {"casts": ()},
        {"casts": ("integer", "integer")},
        {"kind": "trigger"},
        {"kind": "function; drop"},
        {"action": "sql"},
    ],
)
def test_a_tampered_call_is_refused_before_any_sql_runs(change):
    call, _ = routine_calls(_fn("f", _p("integer")), "postgresql")
    with pytest.raises(ValueError):
        pi.run_call(_pg_handle(), dataclasses.replace(call[0], **change))


def test_a_mysql_call_with_a_cast_is_refused():
    call = routine_calls(_fn("f", _p("int")), "mysql")[0][0]
    tampered = dataclasses.replace(call, casts=("int",))
    with pytest.raises(ValueError, match="no cast"):
        pi.run_call(SimpleNamespace(engine="mysql", stopped=False, _backend=None), tampered)


@pytest.mark.parametrize(
    ("param", "dialect", "expected"),
    [
        (_p("bytea"), "postgresql", [("null", None), ("empty", "\\x"), ("bytes", "\\x00ff")]),
        (_p("varbinary(1)", max_length=1), "mysql", [("null", None), ("empty", "")]),
        (_p("blob"), "mysql", [("null", None), ("empty", ""), ("bytes", "ab")]),
        (_p("money"), "postgresql",
         [("null", None), ("zero", "0"), ("max", "92233720368547758.07"),
          ("min", "-92233720368547758.08")]),
        (_p('"char"'), "postgresql", [("null", None), ("empty", ""), ("one_char", "a")]),
        (_p("enum('x','y')"), "mysql", [("null", None), ("first", 1)]),
        (_p("set('x','y')"), "mysql", [("null", None), ("empty", ""), ("first", 1)]),
        (_p("app.mood", is_enum=True), "postgresql",
         [("null", None), ("first", pi.ENUM_FIRST), ("last", pi.ENUM_LAST)]),
    ],
)  # fmt: skip
def test_bytes_money_char_and_enum_parameters_get_values_besides_null(param, dialect, expected):
    """No enum label comes from the collection: MySQL takes an ENUM by its position and
    a SET by its bit mask, and a PostgreSQL enum's label is looked up on the engine."""
    assert parameter_cases(param, dialect) == expected


def test_a_table_with_an_extension_type_is_left_out_and_the_rest_load():
    vec = _table("embeddings", [_col("id", "integer"), _col("v", "halfvec(3)", True)])
    child = _table("uses", [_col("id", "integer"), _col("emb_id", "integer")],
                   fks=[_fk("u_emb", ["emb_id"], "embeddings", ["id"])])  # fmt: skip
    f = _fn("f", tables=("embeddings", "uses", "orders"))
    out = build_proof_inputs([f], TABLES + [vec, child], dialect="postgresql")
    assert dict(out.setup.unseeded)["app.embeddings"] == (
        "column v needs the vector extension (halfvec)"
    )
    ddl = "\n".join(out.setup.ddl)
    assert '"embeddings"' not in ddl and "HALFVEC" not in ddl
    assert 'CREATE TABLE "uses"' in ddl and '"u_emb"' not in ddl  # its key has no table
    assert {t for t, _ in out.setup.rows} >= {"customers", "orders", "uses"}
    kept = build_proof_inputs([f], TABLES + [vec, child], dialect="postgresql",
                              extensions=("vector",))  # fmt: skip
    assert "app.embeddings" not in dict(kept.setup.unseeded)


def _scaled_collection(n):
    """``n`` routines over ``n // 10`` tables in a chain of foreign keys, a trigger for
    every tenth routine."""
    tables = [
        _table(f"t{i}", [_col("id", "integer"), _col("prev", "integer", True)],
               fks=[_fk(f"t{i}_prev", ["prev"], f"t{i - 1}", ["id"])] if i else [],
               row_count=3)  # fmt: skip
        for i in range(max(1, n // 10))
    ]
    routines = [
        _trigger(f"tr{i}", f"t{i // 10}", ["INSERT", "DELETE"]) if i % 10 == 0
        else _fn(f"f{i}", _p("integer"), tables=(f"t{i // 10}",))
        for i in range(n)
    ]  # fmt: skip
    return routines, tables


def test_the_whole_build_is_linear_in_routines_and_tables():
    """``build_proof_inputs`` with tables, foreign keys and triggers: eight times the
    routines and tables take about eight times as long (the shared relative check)."""
    assert_linear(
        _scaled_collection,
        lambda c: build_proof_inputs(c[0], c[1], dialect="postgresql"),
        max_rounds=3,
        sizes=(500, 4_000),
    )


def test_reserved_rows_of_a_self_referencing_table_are_pointed_at_by_none():
    """Three triggers on a three-row tree: each INSERT's held-back row and each
    DELETE's row are referenced by no inserted or held-back row."""
    tree = _table("node", [_col("id", "integer"), _col("parent_id", "integer", True)],
                  fks=[_fk("n_parent", ["parent_id"], "node", ["id"])], row_count=3)  # fmt: skip
    triggers = [_trigger(f"t{i}", "node", ["INSERT", "DELETE"]) for i in range(3)]
    out = build_proof_inputs(triggers, [tree], dialect="postgresql")
    rows = dict(out.setup.rows)["node"]
    inserted = [c.args for c in out.calls if c.kind == "INSERT"]
    deleted = {c.args[0] for c in out.calls if c.kind == "DELETE"}
    assert len(inserted) == len(deleted) == 3
    pointed = {r["parent_id"] for r in rows} | {a[1] for a in inserted}
    assert not deleted & pointed
    assert not {a[0] for a in inserted} & pointed


def test_a_call_list_holds_no_sql_and_a_tampered_name_is_refused_when_it_runs():
    assert not {"sql"} & {f.name for f in dataclasses.fields(pi.ProofCall)}
    t = _trigger("o_ins", "orders", ["INSERT"])
    call = _changes(build_proof_inputs([t], TABLES, dialect="postgresql"), t.routine_id)[0]
    bad = dataclasses.replace(call, name='orders"; DROP TABLE customers; --')
    with pytest.raises(ValueError, match="plain identifier"):
        pi.run_call(SimpleNamespace(engine="postgresql"), bad)
