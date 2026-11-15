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

import pytest

from src.tools.database import disposable_engine as de
from src.tools.routines.comparison import ComparedRoutine, compare_routines
from src.tools.routines.proof_inputs import (
    RoutineProofInput,
    build_proof_inputs,
)
from src.tools.routines.proof_screen import create_refusal
from src.tools.routines.proof_targets import ProofTargetError, disposable_target
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
    text = BODY.replace("FUNCTION f", "FUNCTION sales.f")
    assert create_refusal(text, "postgresql", "function", "public", ("sales",)) is not None


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
    role = f'"{name}_runner"'
    scratch = client.statements(name)
    made = scratch.index(f'CREATE SCHEMA "sales" AUTHORIZATION {role}')
    assert scratch.index('REVOKE ALL ON SCHEMA "sales" FROM PUBLIC') > made
    table = next(i for i, s in enumerate(scratch) if s.startswith('CREATE TABLE "sales"."orders"'))
    assert made < table
    # every statement of the load, and the schema, run in the scratch database, never the admin's
    assert all(s.database == name for s in client.sent
               if "sales" in s.sql and s.op in ("execute", "batch"))  # fmt: skip
    # the search path puts public first, then the schema, so an unqualified body still resolves
    path = [s for s in client.sent if s.sql.startswith("SET LOCAL search_path")]
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
