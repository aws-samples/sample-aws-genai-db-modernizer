# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The generated setup and calls run on a real disposable engine (#1942).

``pgserver`` (PostgreSQL 16) is a dev dependency, so the PostgreSQL tests run wherever
the dev extra is installed, CI included. Each one loads the setup and a few fixture
routines, runs every generated call and row change, and checks what came back.

The fixture routines do not absorb edge values: a NULL key breaks a NOT NULL column, the
highest integer overflows ``n + 1``. A routine that errors on an edge value is a valid
outcome, which the later comparison treats as data. What must never happen is a call
the generator built wrongly: a routine that cannot be found or is ambiguous, a value the
type does not read, a table or column that does not exist. ``_malformed`` lists those.

The MySQL tests are marked ``integration``: the gated CI run has no ``mysqld``.
"""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404 - runs this repo's own collect script
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

from src.tools.database import disposable_engine as de
from src.tools.routines.proof_inputs import (
    RoutineProofInput,
    build_proof_inputs,
    load_setup,
    run_call,
)

REPO = Path(__file__).resolve().parents[4]
needs_pg = pytest.mark.skipif(
    "16" not in de.available_majors("postgresql"), reason="pgserver (PostgreSQL 16) is needed"
)
# Errors that mean the generator built a call wrongly, never that a routine met an edge.
_MALFORMED = ("UndefinedFunction", "AmbiguousFunction", "SyntaxError", "UndefinedTable",
              "UndefinedColumn", "InvalidTextRepresentation", "InvalidDatetimeFormat",
              "InvalidBinaryRepresentation", "CannotCoerce", "DatatypeMismatch",
              "1064 (42000)", "1305 (42000)", "1146 (42S02)", "1054 (42S22)", "1366 (HY000)",
              "1292 (22007)", "1265 (01000)")  # fmt: skip


def _malformed(error: str | None) -> bool:
    return error is not None and any(e in error for e in _MALFORMED)


def _as_root() -> bool:
    """One CI image runs as root, where every start is refused: assert that instead."""
    if os.geteuid() != 0:
        return False
    with pytest.raises(de.EngineStartError, match="running as root"):
        de.start("postgresql", "16")
    return True


def _run_all(h: de.EngineHandle, inputs) -> dict[tuple[str, str], de.CallResult]:
    return {(c.routine_id, c.label): run_call(h, c) for c in inputs.calls}


def _col(name, data_type, nullable=False, **kw):
    return {"column_name": name, "data_type": data_type, "nullable": nullable, **kw}


TABLES: list[dict[str, Any]] = [
    {"table_id": "public.customers", "table_name": "customers", "row_count": 300,
     "primary_key": ["id"],
     "columns": [_col("id", "integer"), _col("name", "character varying", max_length=40),
                 _col("email", "character varying", True, max_length=80),
                 _col("joined", "date", True), _col("vip", "boolean", default_value="false")],
     "indexes": [{"index_name": "customers_email", "columns": ["email"], "is_unique": True}]},
    {"table_id": "public.orders", "table_name": "orders", "row_count": 4000, "primary_key": ["id"],
     "columns": [_col("id", "bigint"), _col("customer_id", "integer"),
                 _col("total", "numeric(10,2)", True, null_rate=0.2, min_value=0, max_value=900),
                 _col("status", "character varying", max_length=12, default_value="'new'"),
                 _col("placed", "timestamp without time zone", default_value="now()")],
     "foreign_keys": [{"constraint_name": "orders_customer", "columns": ["customer_id"],
                       "referenced_table": "customers", "referenced_columns": ["id"]}]},
    {"table_id": "public.order_audit", "table_name": "order_audit", "row_count": 5,
     "primary_key": ["id"],
     "columns": [_col("id", "integer", is_auto_increment=True), _col("order_id", "bigint", True),
                 _col("op", "text", True)]},
    # every NOT NULL column has a literal default, so an INSERT of the key alone needs them
    {"table_id": "public.flags", "table_name": "flags", "row_count": 3, "primary_key": ["id"],
     "columns": [_col("id", "integer"), _col("flag", "character(1)", max_length=1,
                                             default_value="'Y'::bpchar"),
                 _col("since", "date", default_value="'2001-02-03'::date"),
                 _col("at", "timestamp without time zone",
                      default_value="'2001-02-03 04:05:06'::timestamp without time zone"),
                 _col("tm", "time without time zone", default_value="'01:02:03'::time"),
                 _col("u", "uuid", default_value="'7d444840-9dc0-11d1-b245-5ffdce74fad2'::uuid"),
                 _col("note", "text", True)]},
]  # fmt: skip


def _routine(routine_id, kind, name, **kw):
    return RoutineProofInput(routine_id=routine_id, kind=kind, name=name, **kw)


ROUTINES = [
    _routine("public.describe(integer,text,date,boolean,numeric)", "function", "describe",
             returns="text", tables=("customers",),
             parameters=[{"name": "n", "data_type": "integer"}, {"name": "s", "data_type": "text"},
                         {"name": "d", "data_type": "date"}, {"name": "b", "data_type": "boolean"},
                         {"name": "m", "data_type": "numeric(6,2)"}]),
    _routine("public.spend(integer)", "function", "spend", returns="record", tables=("orders",),
             parameters=[{"name": "cid", "data_type": "integer"},
                         {"name": "orders", "data_type": "bigint", "mode": "OUT"},
                         {"name": "total", "data_type": "numeric", "mode": "OUT"}]),
    _routine("public.place_order(integer,numeric,character varying)", "procedure", "place_order",
             tables=("orders", "customers"),
             parameters=[{"name": "cid", "data_type": "integer"},
                         {"name": "amount", "data_type": "numeric(10,2)"},
                         {"name": "label", "data_type": "character varying(12)", "mode": "INOUT"},
                         {"name": "new_id", "data_type": "bigint", "mode": "OUT"}]),
    _routine("public.add_flag(text)", "procedure", "add_flag", tables=("flags",),
             parameters=[{"name": "note", "data_type": "text"}]),
    _routine("public.log_order()", "function", "log_order", returns="trigger",
             tables=("order_audit",)),
    _routine("public.orders.orders_log", "trigger", "orders_log",
             trigger={"table": "orders", "timing": "AFTER", "events": "INSERT OR UPDATE OR DELETE"}),
    _routine("public.orders.orders_total", "trigger", "orders_total",
             trigger={"table": "orders", "timing": "BEFORE", "events": ["UPDATE"],
                      "columns": ["total"]}),
]  # fmt: skip

# The routines' own text: fixture routines written for this test, nothing collected.
SOURCES = [
    de.SourceRoutine("describe", "function",
        "CREATE FUNCTION describe(n integer, s text, d date, b boolean, m numeric(6,2)) "
        "RETURNS text LANGUAGE sql AS $$ SELECT concat_ws('|', n + 1, length(s), d + 1, NOT b, "
        "m * 2, (SELECT count(*) FROM customers WHERE id = n)) $$"),
    de.SourceRoutine("spend", "function",
        "CREATE FUNCTION spend(cid integer, OUT orders bigint, OUT total numeric) "
        "LANGUAGE sql AS $$ SELECT count(*), sum(o.total) FROM orders o "
        "WHERE o.customer_id = cid $$"),
    de.SourceRoutine("place_order", "procedure",
        "CREATE PROCEDURE place_order(cid integer, amount numeric, INOUT label varchar, "
        "OUT new_id bigint) LANGUAGE plpgsql AS $$ BEGIN "
        "SELECT max(id) + 1 INTO new_id FROM orders; "
        "INSERT INTO orders (id, customer_id, total) VALUES (new_id, cid, amount); "
        "label := upper(label); END $$"),
    de.SourceRoutine("add_flag", "procedure",
        "CREATE PROCEDURE add_flag(note text) LANGUAGE sql AS $$ "
        "INSERT INTO flags (id, note) VALUES ((SELECT max(f.id) + 1 FROM flags f), note) $$"),
    de.SourceRoutine("log_order", "function",
        "CREATE FUNCTION log_order() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
        "INSERT INTO order_audit (order_id, op) VALUES (CASE WHEN TG_OP = 'DELETE' THEN OLD.id "
        "ELSE NEW.id END, TG_OP); RETURN NULL; END $$"),
    de.SourceRoutine("orders_log", "trigger",
        "CREATE TRIGGER orders_log AFTER INSERT OR UPDATE OR DELETE ON orders "
        "FOR EACH ROW EXECUTE FUNCTION log_order()"),
    de.SourceRoutine("orders_total_fn", "function",
        "CREATE FUNCTION orders_total_fn() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
        "INSERT INTO order_audit (order_id, op) VALUES (NEW.id, 'TOTAL'); RETURN NEW; END $$"),
    de.SourceRoutine("orders_total", "trigger",
        "CREATE TRIGGER orders_total BEFORE UPDATE OF total ON orders "
        "FOR EACH ROW EXECUTE FUNCTION orders_total_fn()"),
]  # fmt: skip


@needs_pg
def test_the_generated_setup_loads_and_every_call_runs_on_postgresql_16():
    inputs = build_proof_inputs(ROUTINES, TABLES, dialect="postgresql", seed=11)
    assert {s.routine_id for s in inputs.skipped} == {"public.log_order()"}
    if _as_root():
        return
    with de.running("postgresql", "16") as h:
        load_setup(h, inputs, SOURCES)
        seeded = de.snapshot(h, ["order_audit"])["order_audit"]
        results = _run_all(h, inputs)
        audit = de.snapshot(h, ["order_audit"])["order_audit"]
    assert [k for k, r in results.items() if _malformed(r.error)] == []
    # every baseline call and every row change completes ...
    for (routine_id, label), result in results.items():
        if label in ("baseline", "insert", "update", "delete"):
            assert result.error is None, (routine_id, label, result.error)
    # ... the add_flag baseline only because each NOT NULL column kept a default of its type
    assert results[("public.add_flag(text)", "baseline")].error is None
    # ... and an edge value may make a routine fail, which is data
    assert "NotNullViolation" in results[("public.place_order(integer,numeric,"
                                          "character varying)", "cid=null")].error  # fmt: skip
    assert (
        "out of range"
        in results[("public.describe(integer,text,date,boolean,numeric)", "n=max")].error
    )
    ops = [r["op"] for r in audit]
    assert {"INSERT", "UPDATE", "DELETE", "TOTAL"} <= set(ops)
    # the procedure's own INSERTs went past the synthetic keys of the audit table
    assert len({r["id"] for r in audit}) == len(audit) > len(seeded)


@needs_pg
def test_out_and_inout_arguments_come_back_from_postgresql():
    inputs = build_proof_inputs(ROUTINES[1:3], TABLES, dialect="postgresql", seed=2)
    if _as_root():
        return
    with de.running("postgresql", "16") as h:
        load_setup(h, inputs, SOURCES[1:3])
        spend = next(c for c in inputs.calls if c.name == "spend" and c.label == "baseline")
        assert len(run_call(h, spend).rows[0]) == 2  # orders and total
        place = next(c for c in inputs.calls if c.name == "place_order" and c.label == "baseline")
        label, new_id = run_call(h, place).rows[0]
        assert label == "A" and new_id > 0


# (routine id, name, parameter type, is an enum)
OVERLOADS = [
    ("public.f(integer)", "f", "integer", False), ("public.f(bigint)", "f", "bigint", False),
    ("public.g(integer)", "g", "integer", False), ("public.g(text)", "g", "text", False),
    ('public.g("char")', "g", '"char"', False),
    ('public.k("Mood")', "k", 'public."Mood"', True), ("public.k(text)", "k", "text", False),
]  # fmt: skip


@needs_pg
def test_each_call_reaches_its_own_overload():
    """``f(integer)`` and ``f(bigint)`` (text alone is ambiguous between them),
    ``g(integer)``, ``g(text)`` and ``g("char")`` (text alone silently picks
    ``g(text)``), and ``k(public."Mood")`` (a quoted user enum) with ``k(text)``: every
    call, NULL ones included, runs the overload it was generated for."""
    routines = [
        _routine(rid, "function", name, returns="text",
                 parameters=[{"name": "x", "data_type": t, "is_enum": enum}])
        for rid, name, t, enum in OVERLOADS
    ]  # fmt: skip
    sources = [
        de.SourceRoutine(name, "function", f"CREATE FUNCTION {name}(x {t}) RETURNS text "
                         f"LANGUAGE sql AS $$ SELECT '{t}'::text $$")
        for _, name, t, _ in OVERLOADS
    ]  # fmt: skip
    inputs = build_proof_inputs(routines, [], dialect="postgresql")
    assert inputs.skipped == ()
    assert {c.routine_id for c in inputs.calls} == {rid for rid, _, _, _ in OVERLOADS}
    if _as_root():
        return
    with de.running("postgresql", "16") as h:
        de.load(h, ["CREATE TYPE \"Mood\" AS ENUM ('low', 'high')"])  # the routine's own type
        load_setup(h, inputs, sources)
        for call in inputs.calls:
            expected = next(t for rid, _, t, _ in OVERLOADS if rid == call.routine_id)
            result = run_call(h, call)
            assert result.error is None, (call.routine_id, call.label, result.error)
            assert result.rows == [[expected]], (call.routine_id, call.label)


@needs_pg
def test_bytes_money_char_and_enum_arguments_run_on_postgresql():
    r = _routine("public.kinds(bytea,money,\"char\",mood)", "function", "kinds", returns="text",
                 parameters=[{"name": "b", "data_type": "bytea"},
                             {"name": "m", "data_type": "money"},
                             {"name": "c", "data_type": '"char"'},
                             {"name": "e", "data_type": "mood", "is_enum": True}])  # fmt: skip
    source = de.SourceRoutine("kinds", "function",
        'CREATE FUNCTION kinds(b bytea, m money, c "char", e mood) RETURNS text LANGUAGE sql '
        "AS $$ SELECT concat_ws('|', length(b), m, c, e) $$")  # fmt: skip
    inputs = build_proof_inputs([r], [], dialect="postgresql")
    if _as_root():
        return
    with de.running("postgresql", "16") as h:
        de.load(h, ["CREATE TYPE mood AS ENUM ('sad', 'ok', 'happy')"])  # the routine's own type
        load_setup(h, inputs, [source])
        results = {c.label: run_call(h, c) for c in inputs.calls}
    assert {k: v.error for k, v in results.items() if v.error} == {}
    assert results["baseline"].rows[0][0].endswith("|sad")  # the first label is the baseline
    assert results["e=last"].rows[0][0].endswith("|happy")
    assert results["b=bytes"].rows[0][0].startswith("2|")


# ------------------------------------------------------------------ the discourse shape


@pytest.fixture(scope="module")
def discourse_tables(tmp_path_factory) -> list[dict[str, Any]]:
    """The bundled discourse sample's tables, through the collector."""
    root = tmp_path_factory.mktemp("discourse")
    with zipfile.ZipFile(REPO / "docs" / "examples" / "discourse" / "discourse.zip") as z:
        z.extractall(root / "in")
    proc = subprocess.run(  # nosec B603 - fixed interpreter plus this repo's own script args
        [sys.executable, "scripts/run_collect.py", "--file",
         str(root / "in" / "discourse-collection.json"), "--db", "sample", "--job-id",
         "proof1942", "--artifact-root", str(root / "a")],
        cwd=REPO, capture_output=True, text=True,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(
        (root / "a" / "sample" / "proof1942" / "collector" / "output.json").read_text()
    )
    tables: list[dict[str, Any]] = out["database_schema"]["tables"]
    return tables


def test_the_discourse_tables_with_an_extension_type_are_left_out_and_named(discourse_tables):
    every = _routine("public.everything()", "function", "everything", returns="integer",
                     tables=tuple(t["table_id"] for t in discourse_tables))  # fmt: skip
    inputs = build_proof_inputs([every], discourse_tables, dialect="postgresql")
    unseeded = dict(inputs.setup.unseeded)
    halfvec = sorted(k for k, v in unseeded.items() if "vector extension (halfvec)" in v)
    assert len(halfvec) == 3, unseeded
    ddl = "\n".join(inputs.setup.ddl)
    assert "HALFVEC" not in ddl.upper()
    assert len([d for d in inputs.setup.ddl if d.startswith("CREATE TABLE")]) == len(
        discourse_tables
    ) - len(halfvec)


@needs_pg
def test_the_discourse_setup_loads_on_postgresql_16(discourse_tables):
    every = _routine("public.everything()", "function", "everything", returns="integer",
                     tables=tuple(t["table_id"] for t in discourse_tables))  # fmt: skip
    inputs = build_proof_inputs([every], discourse_tables, dialect="postgresql", max_rows=3)
    if _as_root():
        return
    with de.running("postgresql", "16") as h:
        load_setup(h, inputs)
        loaded = de.execute(h, "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")
    assert loaded.rows == [[len([d for d in inputs.setup.ddl if d.startswith("CREATE TABLE")])]]


# ---------------------------------------------------------------- MySQL (not in the gated run)


def _my(column):
    types = {"character varying": f"varchar({column.get('max_length')})", "boolean": "tinyint(1)",
             "timestamp without time zone": "datetime", "numeric(10,2)": "decimal(10,2)",
             "integer": "int"}  # fmt: skip
    defaults = {"now()": "CURRENT_TIMESTAMP", "'new'": "new", "false": "0"}
    default = column.get("default_value")
    return dict(column, data_type=types.get(column["data_type"], column["data_type"]),
                default_value=defaults.get(default, default))  # fmt: skip


MY_TABLES = [
    dict(t, table_id="src." + t["table_name"], columns=[_my(c) for c in t["columns"]])
    for t in TABLES
    if t["table_name"] != "flags"
] + [
    # short types and an ENUM, each NOT NULL with a literal default (MySQL reports it unquoted)
    {"table_id": "src.flags", "table_name": "flags", "row_count": 3, "primary_key": ["id"],
     "columns": [_col("id", "int"), _col("flag", "char(1)", max_length=1, default_value="Y"),
                 _col("code", "char(2)", max_length=2, default_value="ab"),
                 _col("v1", "varchar(1)", max_length=1, default_value="x"),
                 _col("b", "binary(1)", max_length=1, default_value="z"),
                 _col("since", "date", default_value="2001-02-03"),
                 _col("at", "datetime", default_value="2001-02-03 04:05:06"),
                 _col("ts", "timestamp", default_value="2001-02-03 04:05:06"),
                 _col("tm", "time", default_value="01:02:03"),
                 _col("y", "year", default_value="1999"),
                 _col("mood", "enum('sad','happy')", default_value="sad"),
                 _col("note", "text", True)]},
]  # fmt: skip
MY_ROUTINES = [
    _routine("src.describe_it", "function", "describe_it", returns="text", tables=("customers",),
             parameters=[{"name": n, "data_type": t} for n, t in [
                 ("n", "int"), ("s", "varchar(20)"), ("d", "date"), ("t", "timestamp"),
                 ("b", "tinyint(1)"), ("m", "decimal(6,2)"), ("u", "int unsigned"),
                 ("f", "double"), ("j", "json"), ("y", "year"), ("tm", "time"),
                 ("e", "enum('lo','hi')")]]),
    _routine("src.place_order", "procedure", "place_order", tables=("orders",),
             parameters=[{"name": "cid", "data_type": "int"},
                         {"name": "label", "data_type": "varchar(12)", "mode": "INOUT"},
                         {"name": "new_id", "data_type": "bigint", "mode": "OUT"}]),
    _routine("src.add_flag", "procedure", "add_flag", tables=("flags",),
             parameters=[{"name": "note", "data_type": "text"}]),
    *[_routine(f"src.orders_{e.lower()}", "trigger", f"orders_{e.lower()}", tables=("order_audit",),
               trigger={"table": "orders", "timing": "AFTER", "events": [e]})
      for e in ("INSERT", "UPDATE", "DELETE")],
]  # fmt: skip
MY_SOURCES = [
    de.SourceRoutine("describe_it", "function",
        "CREATE FUNCTION describe_it(n INT, s VARCHAR(20), d DATE, t TIMESTAMP, b TINYINT(1), "
        "m DECIMAL(6,2), u INT UNSIGNED, f DOUBLE, j JSON, y YEAR, tm TIME, e ENUM('lo','hi')) "
        "RETURNS TEXT DETERMINISTIC READS SQL DATA RETURN CONCAT_WS('|', n, LENGTH(s), d, t, b, "
        "m, u, f, j, y, tm, e, (SELECT COUNT(*) FROM customers WHERE id = n))"),
    de.SourceRoutine("place_order", "procedure",
        "CREATE PROCEDURE place_order(IN cid INT, INOUT label VARCHAR(12), OUT new_id BIGINT) "
        "BEGIN SELECT MAX(id) + 1 INTO new_id FROM orders; "
        "INSERT INTO orders (id, customer_id) VALUES (new_id, cid); SET label = UPPER(label); END"),
    de.SourceRoutine("add_flag", "procedure",
        "CREATE PROCEDURE add_flag(IN note TEXT) BEGIN DECLARE nid INT; "
        "SELECT MAX(id) + 1 INTO nid FROM flags; INSERT INTO flags (id, note) VALUES (nid, note); "
        "END"),
    *[de.SourceRoutine(f"orders_{e.lower()}", "trigger",
        f"CREATE TRIGGER orders_{e.lower()} AFTER {e} ON orders FOR EACH ROW INSERT INTO "
        f"order_audit (order_id, op) VALUES ({'OLD' if e == 'DELETE' else 'NEW'}.id, '{e}')")
      for e in ("INSERT", "UPDATE", "DELETE")],
]  # fmt: skip


@pytest.mark.integration
@pytest.mark.skipif(not de.available_majors("mysql"), reason="no mysqld binary")
def test_the_generated_setup_loads_and_every_call_runs_on_mysql():
    """Short CHAR, VARCHAR and BINARY columns, dates and an ENUM, each NOT NULL with a
    literal default, load; an INSERT of the key alone takes their defaults."""
    inputs = build_proof_inputs(MY_ROUTINES, MY_TABLES, dialect="mysql", seed=3)
    assert inputs.skipped == ()
    if os.geteuid() == 0:
        return
    with de.running("mysql", de.available_majors("mysql")[0]) as h:
        load_setup(h, inputs, MY_SOURCES)
        results = _run_all(h, inputs)
        ops = {r["op"] for r in de.snapshot(h, ["order_audit"])["order_audit"]}
        flags = de.snapshot(h, ["flags"])["flags"]
    assert [(k, r.error) for k, r in results.items() if _malformed(r.error)] == []
    for (routine_id, label), result in results.items():
        if label in ("baseline", "insert", "update", "delete"):
            assert result.error is None, (routine_id, label, result.error)
    assert {"INSERT", "UPDATE", "DELETE"} <= ops
    assert {r["mood"] for r in flags} <= {"e1", "e2"}  # placeholders, never the labels


def test_the_mysql_round_trip_is_deselected_not_skipped_in_a_gated_run():
    test = test_the_generated_setup_loads_and_every_call_runs_on_mysql
    assert "integration" in {m.name for m in test.pytestmark}
