# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The routine proof's setup tables live in their source schema or database (#1970).

A routine body that names ``sales.orders`` (PostgreSQL) or ``inventory.stock`` (MySQL)
finds its table on the disposable engine, and on the Aurora test cluster's PostgreSQL
scratch database, where the runner role owns each schema. The disposable-engine tests start
a real server (``pgserver``, socket only; MySQL ones are ``integration`` tests on a local
``mysqld``); the Data API tests use :class:`FakeDataApi`, so nothing is sent anywhere."""

from __future__ import annotations

import datetime
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.tools.database import disposable_engine as de
from src.tools.routines.comparison import ComparedRoutine, compare_routines
from src.tools.routines.proof_inputs import (
    RoutineProofInput,
    build_proof_inputs,
)
from src.tools.routines.proof_screen import create_refusal
from src.tools.routines.proof_targets import CreateRefused, ProofTargetError, disposable_target
from tests.unit.tools.routines.fake_data_api import FakeDataApi
from tests.unit.tools.routines.test_data_api_target import _factory

needs_pg = pytest.mark.skipif(
    "16" not in de.available_majors("postgresql"), reason="pgserver (PostgreSQL 16) is needed"
)
needs_mysql = pytest.mark.skipif(not de.available_majors("mysql"), reason="no mysqld binary")


def _col(name, data_type, nullable=False, **kw):
    return {"column_name": name, "data_type": data_type, "nullable": nullable, **kw}


def _orders(schema, name="orders", **kw):
    return {"table_id": f"{schema}.{name}", "table_name": name, "schema_name": schema,
            "row_count": 4, "primary_key": ["id"],
            "columns": [_col("id", "integer"), _col("qty", "integer", True)], **kw}  # fmt: skip


def _proof(rid, kind, name, **kw):
    return RoutineProofInput(routine_id=rid, kind=kind, name=name, **kw)


def _compare(pairs, tables, engine="postgresql", version="16"):
    return compare_routines(pairs, tables, source=disposable_target(engine, version),
                            target=disposable_target(engine, version), dialect=engine)  # fmt: skip


def _skip_as_root() -> bool:
    return os.geteuid() == 0


# --------------------------------------------------------------------------- the setup


def test_the_setup_names_each_table_in_its_source_schema_on_postgresql():
    proof = _proof("public.f()", "function", "f", returns="integer", tables=("sales.orders",))
    inputs = build_proof_inputs([proof], [_orders("sales")], dialect="postgresql")
    assert inputs.setup.schemas == ("sales",)
    assert inputs.setup.ddl[0].startswith('CREATE TABLE "sales"."orders" (')
    assert [name for name, _ in inputs.setup.rows] == ["sales.orders"]


def test_an_index_and_a_foreign_key_name_the_schema_of_their_tables():
    lines = _orders("sales", "lines", foreign_keys=[
        {"constraint_name": "lines_fk", "columns": ["qty"], "referenced_table": "sales.orders",
         "referenced_columns": ["id"]}],
        indexes=[{"index_name": "lines_qty", "columns": ["qty"], "is_unique": False}])  # fmt: skip
    proof = _proof("public.f()", "function", "f", returns="integer", tables=("sales.lines",))
    ddl = build_proof_inputs([proof], [_orders("sales"), lines], dialect="postgresql").setup.ddl
    assert 'CREATE INDEX "lines_qty" ON "sales"."lines" ("qty");' in ddl
    fk = next(s for s in ddl if s.startswith("ALTER TABLE"))
    assert fk.startswith('ALTER TABLE "sales"."lines" ADD CONSTRAINT "lines_fk"')
    assert 'REFERENCES "sales"."orders" ("id")' in fk


def test_the_public_schema_and_a_table_with_no_schema_stay_unqualified():
    proof = _proof("public.f()", "function", "f", returns="integer", tables=("public.orders",))
    inputs = build_proof_inputs([proof], [_orders("public")], dialect="postgresql")
    assert inputs.setup.schemas == ()
    assert inputs.setup.ddl[0].startswith('CREATE TABLE "orders" (')
    assert [name for name, _ in inputs.setup.rows] == ["orders"]


def test_a_mysql_database_other_than_the_routines_own_gets_its_own_database():
    proof = _proof("shop.restock", "procedure", "restock", tables=("inventory.stock", "shop.t"))
    tables = [_orders("inventory", "stock"), _orders("shop", "t")]
    inputs = build_proof_inputs([proof], tables, dialect="mysql")
    assert inputs.setup.schemas == ("inventory",)
    ddl = " ".join(inputs.setup.ddl)
    assert "CREATE TABLE `inventory`.`stock` (" in ddl and "CREATE TABLE `t` (" in ddl


def test_a_schema_name_that_is_not_a_plain_identifier_is_never_written_into_a_statement():
    odd = _orders('sa"les')
    proof = _proof("public.f()", "function", "f", returns="integer", tables=('sa"les.orders',))
    inputs = build_proof_inputs([proof], [odd], dialect="postgresql")
    assert inputs.setup.schemas == () and inputs.setup.ddl[0].startswith('CREATE TABLE "orders" (')
    assert "schema name is not a plain identifier" in dict(inputs.setup.unseeded)['sa"les.orders']


# --------------------------------------------------------------------------- the screen


BODY = "CREATE FUNCTION f() RETURNS integer LANGUAGE sql AS $$ SELECT count(*)::int FROM sales.orders $$"


def test_a_body_naming_a_setup_schema_passes_the_screen_and_another_does_not():
    assert create_refusal(BODY, "postgresql", "function", "public") is not None
    assert create_refusal(BODY, "postgresql", "function", "public", ("sales",)) is None
    other = BODY.replace("sales.orders", "audit.orders")
    assert create_refusal(other, "postgresql", "function", "public", ("sales",)) is not None


def test_a_trigger_on_a_setup_schema_table_passes_the_screen_but_its_function_stays_in_public():
    on = "CREATE TRIGGER t AFTER INSERT ON sales.orders FOR EACH ROW EXECUTE FUNCTION f()"
    assert create_refusal(on, "postgresql", "trigger", "public") is not None
    assert create_refusal(on, "postgresql", "trigger", "public", ("sales",)) is None
    function = on.replace("FUNCTION f()", "FUNCTION sales.f()")
    assert create_refusal(function, "postgresql", "trigger", "public", ("sales",)) is not None
    mine = "CREATE TRIGGER t AFTER INSERT ON sales.orders FOR EACH ROW EXECUTE FUNCTION f()"
    assert create_refusal(mine, "postgresql", "trigger", "public", ("audit",)) is not None


def test_a_setup_schema_never_lets_a_routine_be_created_in_it():
    """On a target loaded with the setup, a body that names the schema's table loads, and a
    routine created in that schema is refused: the first fails on main, which refuses both."""
    target = _factory(FakeDataApi())()
    target.load_setup(_sales_setup())
    assert target.create(de.SourceRoutine("f", "function", BODY)) is None
    with pytest.raises(CreateRefused, match="schema other than"):
        target.create(
            de.SourceRoutine("sales.f", "function", BODY.replace("FUNCTION f", "FUNCTION sales.f"))
        )
    target.close()


def test_a_mysql_body_names_a_setup_database_exactly():
    text = "CREATE PROCEDURE p() BEGIN SELECT COUNT(*) FROM inventory.stock; END"
    assert create_refusal(text, "mysql", "procedure", "src") is not None
    assert create_refusal(text, "mysql", "procedure", "src", ("inventory",)) is None
    assert create_refusal(text, "mysql", "procedure", "src", ("Inventory",)) is not None


# --------------------------------------------------------------------------- PostgreSQL 16

COUNT = ("CREATE FUNCTION count_orders() RETURNS integer LANGUAGE sql AS $$ "
         "SELECT count(*)::int FROM sales.orders $$")  # fmt: skip
BUMP = ("CREATE PROCEDURE bump(n integer) LANGUAGE plpgsql AS $$ BEGIN "
        "UPDATE sales.orders SET qty = coalesce(qty, 0) + n WHERE id = 1; END $$")  # fmt: skip


def _pair(proof, original, carried=None):
    kind = proof.kind
    one = (de.SourceRoutine(proof.name, kind, original),)
    other = (de.SourceRoutine(proof.name, kind, carried or original),)
    return ComparedRoutine(proof, one, other)


@needs_pg
def test_a_routine_naming_a_non_public_schema_runs_on_postgresql_16():
    if _skip_as_root():
        return
    count = _proof("public.count_orders()", "function", "count_orders", returns="integer",
                   tables=("sales.orders",))  # fmt: skip
    bump = _proof("public.bump(integer)", "procedure", "bump", tables=("sales.orders",),
                  parameters=[{"name": "n", "data_type": "integer"}])  # fmt: skip
    changed = BUMP.replace("+ n", "+ n + 1")
    results = _compare(
        [_pair(count, COUNT), _pair(bump, BUMP, changed)], [_orders("sales")]
    ).results
    counted, bumped = results
    assert counted.outcome == "no_differences_found", counted
    assert counted.reason.endswith("succeeded on both sides)"), counted.reason
    # the write is seen in sales.orders: the schema's table is snapshotted, not just public
    assert bumped.outcome == "different", bumped
    assert {(d.aspect, d.table) for d in bumped.differences} == {("table", "sales.orders")}


@needs_pg
def test_the_runner_owns_the_schemas_it_loads_into_on_postgresql_16():
    if _skip_as_root():
        return
    proof = _proof("public.count_orders()", "function", "count_orders", returns="integer",
                   tables=("sales.orders",))  # fmt: skip
    setup = build_proof_inputs([proof], [_orders("sales")], dialect="postgresql").setup
    target = disposable_target("postgresql", "16")()
    try:
        target.load_setup(setup)
        assert "sales.orders" in target.tables()
        got = target._execute(
            "SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = 'sales'"
        )
        assert got.rows == [["routine_runner"]]
        assert set(target.column_types(["sales.orders"])["sales.orders"]) == {"id", "qty"}
        assert set(target.snapshot(["sales.orders"])["sales.orders"][0]) == {"id", "qty"}
    finally:
        target.close()


@needs_pg
@pytest.mark.passes_on_main  # an unqualified body already found its table in public
def test_an_unqualified_body_still_finds_a_table_of_another_schema_on_postgresql_16():
    if _skip_as_root():
        return
    text = COUNT.replace("sales.orders", "orders")
    proof = _proof("public.count_orders()", "function", "count_orders", returns="integer",
                   tables=("sales.orders",))  # fmt: skip
    (counted,) = _compare([_pair(proof, text)], [_orders("sales")]).results
    assert counted.outcome == "no_differences_found", counted


# --------------------------------------------------------------------------- MySQL (local mysqld)


@pytest.mark.integration
@needs_mysql
def test_a_routine_naming_another_database_runs_on_mysql():
    if _skip_as_root():
        return
    original = "CREATE PROCEDURE restock(IN n INT) BEGIN UPDATE inventory.stock SET qty = qty + n WHERE id = 1; END"
    carried = original.replace("qty + n", "qty + n + 1")
    proof = _proof("src.restock", "procedure", "restock", tables=("inventory.stock",),
                   parameters=[{"name": "n", "data_type": "int"}])  # fmt: skip
    one = (de.SourceRoutine("restock", "procedure", original),)
    other = (de.SourceRoutine("restock", "procedure", carried),)
    version = de.available_majors("mysql")[0]
    stock = _orders("inventory", "stock", columns=[_col("id", "int", is_auto_increment=True),
                                                   _col("qty", "int", True)])  # fmt: skip
    (result,) = _compare([ComparedRoutine(proof, one, other)], [stock], "mysql", version).results
    assert result.outcome == "different", result
    assert {(d.aspect, d.table) for d in result.differences} == {("table", "inventory.stock")}


@pytest.mark.integration
@needs_mysql
def test_the_extra_mysql_databases_belong_to_the_engine_and_leave_with_it():
    if _skip_as_root():
        return
    proof = _proof("src.restock", "procedure", "restock", tables=("inventory.stock",))
    stock = _orders("inventory", "stock", columns=[_col("id", "int", is_auto_increment=True),
                                                   _col("qty", "int", True)])  # fmt: skip
    setup = build_proof_inputs([proof], [stock], dialect="mysql").setup
    target = disposable_target("mysql", de.available_majors("mysql")[0])()
    try:
        target.load_setup(setup)
        assert "inventory.stock" in target.tables()
        assert set(target.column_types(["inventory.stock"])["inventory.stock"]) == {"id", "qty"}
        assert (
            "inventory.stock" in target.counter_state()
        )  # its AUTO_INCREMENT, found in its database
        # the runner holds the new database and nothing wider
        grants = target._execute("SHOW GRANTS FOR CURRENT_USER()").rows
        assert not any("ON *.*" in str(g[0]) and "ALL PRIVILEGES" in str(g[0]) for g in grants)
    finally:
        target.close()


# --------------------------------------------------------------------------- Aurora Data API (fake)


def _sales_setup(dialect="postgresql"):
    proof = _proof(
        "public.f(integer)", "function", "f", returns="integer", tables=("sales.orders",)
    )
    return build_proof_inputs([proof], [_orders("sales")], dialect=dialect).setup


def test_a_postgresql_scratch_database_gets_each_source_schema_owned_by_the_runner_role():
    client = FakeDataApi()
    target = _factory(client)()
    name = target.database
    target.load_setup(_sales_setup())
    role = f'"{target._runner.username}"'
    scratch = client.statements(name)
    made = scratch.index(f'CREATE SCHEMA "sales" AUTHORIZATION {role}')
    assert scratch.index('REVOKE ALL ON SCHEMA "sales" FROM PUBLIC') > made
    table = next(i for i, s in enumerate(scratch) if s.startswith('CREATE TABLE "sales"."orders"'))
    assert made < table
    # every statement of the load, and the schema, run in the scratch database, never the admin's
    assert all(s.database == name for s in client.sent
               if "sales" in s.sql and s.op in ("execute", "batch"))  # fmt: skip
    # the search path puts public first, then the schema, so an unqualified body still resolves
    path = [
        s
        for s in client.sent
        if s.sql.startswith("SET LOCAL search_path")
        and s.sql != "SET LOCAL search_path = pg_catalog"
    ]  # the master user's pin
    assert path and path[0].sql == 'SET LOCAL search_path = "public", "sales"'
    target.close()


def test_a_postgresql_target_lists_and_reads_the_schemas_tables():
    client = FakeDataApi()
    target = _factory(client)()
    target.load_setup(_sales_setup())
    target.prepare_calls()
    target.tables()
    listing = next(s.sql for s in client.sent if "FROM pg_tables" in s.sql)
    assert "'sales'" in listing and "'public'" in listing
    client.sent.clear()
    target.snapshot(["sales.orders"])
    target.column_types(["sales.orders"])
    assert any('"sales"."orders"' in s.sql for s in client.sent)
    typed = [p["value"]["stringValue"] for s in client.sent for p in s.parameters]
    assert typed.count('"sales"."orders"') == 2  # the column read and the type read
    target.close()


def test_a_mysql_setup_naming_another_database_is_refused_cleanly_on_the_test_cluster():
    client = FakeDataApi("mysql")
    target = _factory(client, engine="mysql")()
    with pytest.raises(ProofTargetError, match="other than the scratch"):
        target.load_setup(_sales_setup("mysql"))
    target.close()
    assert not client.databases  # nothing was left on the cluster


def test_the_data_api_row_change_names_the_schema_of_its_table():
    """The row change a trigger's proof generates names ``sales.orders``, and the target
    writes it into that schema."""
    trigger = _proof("sales.orders.t", "trigger", "t", tables=("sales.orders",),
                     trigger={"table": "sales.orders", "timing": "AFTER", "events": ["INSERT"]})  # fmt: skip
    inputs = build_proof_inputs([trigger], [_orders("sales")], dialect="postgresql")
    (change,) = [c for c in inputs.calls if c.action == "row_change"]
    assert change.name == "sales.orders"
    client = FakeDataApi()
    target = _factory(client)()
    target.load_setup(inputs.setup)
    target.prepare_calls()
    target.begin_call(datetime.datetime(2024, 6, 1, tzinfo=datetime.UTC), 0.5)
    target.run(change)
    assert any('INSERT INTO "sales"."orders"' in s.sql or '"sales"."orders"' in s.sql
               for s in client.sent if s.op == "execute" and "json_populate" in s.sql)  # fmt: skip
    target.end_call()
    target.close()


# --------------------------------------------------------------------------- review of #2335

PG_SCHEMAS = ("sales",)
MY_SCHEMAS = ("inventory",)


def _pg(body, schemas=PG_SCHEMAS, header="CREATE FUNCTION f() RETURNS integer LANGUAGE plpgsql AS"):
    return create_refusal(
        f"{header} $$ BEGIN {body} RETURN 1; END $$", "postgresql", "function", "public", schemas
    )


def _my(body, schemas=MY_SCHEMAS):
    return create_refusal(
        f"CREATE PROCEDURE p() BEGIN {body} END", "mysql", "procedure", "src", schemas
    )


@pytest.mark.parametrize(
    "body",
    [
        "CREATE TABLE inventory.evil (a int);",
        "CREATE VIEW inventory.v AS SELECT 1;",
        "CREATE TABLE src.x LIKE inventory.stock;",
        "ALTER TABLE inventory.stock ADD COLUMN x int;",
        "DROP TABLE inventory.stock;",
        "DROP TABLE `inventory`.`stock`;",
        "RENAME TABLE inventory.stock TO inventory.s2;",
        "TRUNCATE TABLE inventory.stock;",
        "TRUNCATE inventory.stock;",
        "CREATE INDEX i ON inventory.stock (id);",
        "SELECT id FROM inventory.stock INTO OUTFILE '/tmp/x';",
        "SELECT id FROM inventory.stock INTO DUMPFILE '/tmp/x';",
        "USE inventory;",
        "SELECT 1; DROP TABLE inventory.stock;",
        "CREATE TABLE INVENTORY.evil (a int);",
        "CREATE TABLE Inventory.evil (a int);",
        "DROP TABLE `Inventory`.`stock`;",
        "USE INVENTORY;",
        "SELECT id FROM Inventory.stock INTO OUTFILE '/tmp/x';",
    ],
)
def test_a_mysql_body_never_changes_or_writes_out_a_setup_database(body):
    assert _my(body) is not None, body


@pytest.mark.parametrize(
    "body",
    [
        "SELECT COUNT(*) FROM inventory.stock;",
        "UPDATE inventory.stock SET qty = 1 WHERE id = 1;",
        "INSERT INTO inventory.stock (id, qty) VALUES (9, 1);",
        "DELETE FROM inventory.stock WHERE id = 1;",
        "SELECT id FROM inventory.stock INTO @x;",
        "DROP TEMPORARY TABLE IF EXISTS t; SELECT COUNT(*) FROM inventory.stock;",
        "CREATE TEMPORARY TABLE t (a int); INSERT INTO t SELECT id FROM inventory.stock;",
    ],
)
def test_a_mysql_body_may_read_and_write_rows_of_a_setup_database(body):
    assert _my(body) is None, body


def test_a_mysql_call_or_function_into_a_setup_database_is_refused():
    assert _my("CALL inventory.p();") is not None
    assert _my("SELECT inventory.f();") is not None
    assert _my("CALL inventory.p();", ()) is not None


def test_a_postgresql_call_or_function_into_a_setup_schema_is_refused():
    """Review of #2335: ``schemas`` allows a table of a setup schema, never a call into it."""
    plain = create_refusal(
        "CREATE FUNCTION f() RETURNS integer LANGUAGE sql AS $$ SELECT sales.g() $$",
        "postgresql",
        "function",
        "public",
        PG_SCHEMAS,
    )
    assert plain is not None
    for body in ("CALL sales.p();", "PERFORM sales.g(1);", "PERFORM * FROM sales.g();",
                 'PERFORM "sales".g();', "PERFORM x FROM sales.g(1) x;",
                 "SELECT sales.g() INTO v;"):  # fmt: skip
        assert _pg(body) is not None, body


def test_a_postgresql_body_may_still_read_and_write_a_setup_schemas_tables():
    for body in ("PERFORM * FROM sales.orders;", "INSERT INTO sales.orders (id) VALUES (1);",
                 "UPDATE sales.orders SET qty = 1;", "DELETE FROM sales.orders;",
                 "PERFORM count(*) FROM sales.orders o JOIN sales.lines l ON (l.id = o.id);"):  # fmt: skip
        assert _pg(body) is None, body


def test_a_postgresql_trigger_when_clause_cannot_call_into_a_setup_schema():
    base = ("CREATE TRIGGER t AFTER UPDATE ON sales.orders FOR EACH ROW WHEN ({when}) "
            "EXECUTE FUNCTION f()")  # fmt: skip
    ok = create_refusal(
        base.format(when="NEW.qty > 1"), "postgresql", "trigger", "public", PG_SCHEMAS
    )
    assert ok is None
    bad = create_refusal(
        base.format(when="sales.f(NEW.id)"), "postgresql", "trigger", "public", PG_SCHEMAS
    )
    assert bad is not None


@pytest.mark.parametrize(
    "body",
    [
        "CREATE FUNCTION public.lower(varchar) RETURNS text LANGUAGE sql AS $f$ SELECT 'x' $f$;",
        "CREATE OR REPLACE FUNCTION lower(varchar) RETURNS text LANGUAGE sql AS $f$ SELECT 'x' $f$;",
        "CREATE PROCEDURE q() LANGUAGE sql AS $f$ SELECT 1 $f$;",
        "CREATE OPERATOR === (LEFTARG = int, RIGHTARG = int, FUNCTION = int4eq);",
        "CREATE AGGREGATE a(int) (SFUNC = int4pl, STYPE = int);",
        "ALTER FUNCTION g(varchar) RENAME TO lower;",
        "ALTER FUNCTION g(varchar) SET SCHEMA sales;",
    ],
)
def test_a_postgresql_body_never_creates_a_function_or_an_operator(body):
    """The same signature never wins over ``pg_catalog``, but a more specific one does, so
    no function, procedure, operator or aggregate is created from a body, schema or not."""
    assert _pg(body, ()) is not None, body
    assert _pg(body) is not None, body


def test_the_screen_still_accepts_the_routines_own_header_whatever_its_words():
    assert (
        create_refusal(
            "CREATE OR REPLACE FUNCTION f() RETURNS integer LANGUAGE sql AS $$ SELECT 1 $$",
            "postgresql",
            "function",
            "public",
            PG_SCHEMAS,
        )
        is None
    )


def test_a_table_in_a_reserved_schema_is_left_out_with_its_reason_not_the_whole_setup():
    proof = _proof(
        "public.f()",
        "function",
        "f",
        returns="integer",
        tables=("sales.orders", "information_schema.tables"),
    )
    odd = _orders("information_schema", "tables")
    inputs = build_proof_inputs([proof], [_orders("sales"), odd], dialect="postgresql")
    assert inputs.setup.schemas == ("sales",)
    assert "reserved" in dict(inputs.setup.unseeded)["information_schema.tables"]
    assert not any("tables" in d for d in inputs.setup.ddl)
    my = _proof("src.p", "procedure", "p", tables=("mysql.user",))
    mine = build_proof_inputs([my], [_orders("mysql", "user")], dialect="mysql")
    assert mine.setup.schemas == () and mine.setup.ddl == ()
    assert "reserved" in dict(mine.setup.unseeded)["mysql.user"]


def test_a_plain_identifier_ends_with_its_last_character_not_a_newline():
    assert de._IDENT.match("sales\n") is None and de._IDENT.match("sales") is not None
    from src.tools.routines import proof_inputs

    assert proof_inputs._IDENT.match("sales\n") is None
    with pytest.raises(ValueError, match="plain schema name"):
        de.create_namespaces(SimpleNamespace(stopped=False, _backend=None), ["sales\n"])


def test_the_harness_reads_sequences_and_types_through_pg_catalog():
    """A function named ``quote_ident`` planted in ``public`` must not run in a harness query,
    which on the test cluster is run by the master user outside the role's transaction."""
    for sql in (
        de._PG_SEQUENCE_STATE,
        de._PG_COLUMN_TYPES,
        de._PG_TABLE_SEQUENCES,
        de._PG_SET_SEQUENCE,
    ):
        for word in re.findall(
            r"(?<![\w.])(quote_ident|format_type|pg_get_serial_sequence|setval)\(", sql
        ):
            raise AssertionError(f"{word} is not qualified with pg_catalog in: {sql}")
    source = Path("src/tools/routines/data_api_target.py").read_text()
    assert not re.findall(r"(?<![\w.])(?:quote_ident|format_type|to_jsonb|setval)\(", source)


@needs_pg
def test_a_function_planted_in_public_never_runs_in_a_harness_query_on_postgresql_16():
    if _skip_as_root():
        return
    handle = de.start("postgresql", "16")
    try:
        de.load(
            handle,
            ["CREATE SEQUENCE planted_seq"],
            {},
            [
                de.SourceRoutine(
                    "quote_ident",
                    "function",
                    "CREATE FUNCTION quote_ident(n name) RETURNS text LANGUAGE sql AS $$ SELECT 'HIJACKED' $$",
                )
            ],
        )
        names = [name for name, _, _ in de.counters(handle)]
        assert names == ["public.planted_seq"], names
    finally:
        de.stop(handle)


# --------------------------------------------------------------------------- MySQL grants


def test_grant_database_escapes_the_wildcards_and_the_quote():
    from src.tools.database.mysql_grants import grant_database

    assert grant_database("my_ql") == "`my\\_ql`"
    assert grant_database("sales%") == "`sales\\%`"
    assert grant_database("a`b") == "`a``b`"
    assert grant_database("a\\b") == "`a\\\\b`"
    assert grant_database("plain") == "`plain`"


def test_every_grant_on_a_database_in_the_source_goes_through_the_escaping_helper():
    """MySQL reads ``_`` and ``%`` in the database part of a GRANT as wildcards, so a
    grant written any other way gives rights on every look-alike database (``my_ql`` on
    ``mysql``). Whole statements are read: the grant and its ``ON db.*`` may sit on
    different lines of a string."""
    bad = []
    for path in sorted(Path("src").rglob("*.py")):
        if path.name == "mysql_grants.py":
            continue
        text = path.read_text()
        for m in _GRANT.finditer(text):
            if "grant_database(" not in m.group(0):
                bad.append(f"{path}:{text.count(chr(10), 0, m.start()) + 1}")
    assert not bad, bad


_GRANT = re.compile(r"GRANT\b[^;]{0,400}?\bON\b[^;]{0,200}?\.\*", re.S)


def test_the_grant_scan_sees_a_grant_split_over_two_string_lines():
    sample = 'x = ("GRANT ALTER, CREATE "\n     f"ON `{db}`.* TO u")'
    found = _GRANT.search(sample)
    assert found and "grant_database(" not in found.group(0)


@pytest.mark.integration
@needs_mysql
def test_a_database_named_with_an_underscore_gives_the_runner_nothing_on_mysql_the_system_one():
    if _skip_as_root():
        return
    handle = de.start("mysql", de.available_majors("mysql")[0])
    try:
        de.create_namespaces(handle, ["inventory", "my_ql"])
        grants = " ".join(
            str(r[0]) for r in de.execute(handle, "SHOW GRANTS FOR CURRENT_USER()").rows
        )
        assert "`my\\_ql`" in grants and "`my_ql`.*" not in grants  # the escaping itself
        assert de.execute(handle, "SELECT user FROM mysql.user").error is not None
        assert de.execute(handle, "CREATE TABLE mysql.planted (a int)").error is not None
        assert de.execute(handle, "INSERT INTO mysql.db (Host) VALUES ('x')").error is not None
        assert de.execute(handle, "CREATE TABLE my_ql.ok (a int)").error is None  # its own is fine
    finally:
        de.stop(handle)


def test_the_dms_user_grants_and_the_runner_grant_on_the_test_cluster_escape_the_wildcards():
    from src.tools.data_movement.dms_step import _dms_user_lines

    lines = " ".join(_dms_user_lines("aurora_mysql", "8.0", "my_ql", "public"))
    assert "`my\\_ql`.*" in lines and "`awsdms\\_control`.*" in lines
    assert "`my_ql`" not in lines and "`awsdms_control`" not in lines
    client = FakeDataApi("mysql")
    target = _factory(client, engine="mysql")()
    grant = next(s.sql for s in client.sent if s.sql.startswith("GRANT ALL PRIVILEGES ON"))
    assert target.database.replace("_", "\\_") in grant and f"`{target.database}`" not in grant
    target.close()
