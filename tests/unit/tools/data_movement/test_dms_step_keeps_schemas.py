# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1 keeps each table's source schema in the target (#2494).

A source of several schemas (MySQL: several databases) moves each table into the schema
of its own name on Aurora: ``sales.orders`` and ``archive.orders`` are two tables there,
as in the schema design (#2455). A step of one schema writes what it wrote before
(``test_one_schema_samples_write_what_they_wrote_before``).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from src.storage.local_store import LocalArtifactStore
from src.tools.data_movement.dms_control import CONTROL_SCHEMA
from src.tools.data_movement.dms_step import (
    DDL_FILE,
    DMS_TEMPLATE,
    DmsStepError,
    load_step1,
    render_dms_template,
    render_files,
    table_mappings,
)
from src.tools.data_movement.plan import write_plans
from src.tools.data_movement.verify_scripts import render_script
from src.tools.transformation_contract.write import write_step1
from tests.unit.tools.transformation_contract.conftest import col, collector, table

from . import step1_samples

_AURORA = {"mysql": "aurora_mysql", "postgresql": "aurora_postgresql"}


def _tables() -> list[dict]:
    """``orders`` in two schemas (different columns), a case-only pair across them, and a
    foreign key from ``sales.items`` to its own schema's ``orders``."""
    items = table("sales", "items", [col("id", "bigint", 1), col("order_id", "bigint", 2)],
                  pk=["id"])  # fmt: skip
    items["foreign_keys"] = [{"constraint_name": "fk_items_order", "columns": ["order_id"],
                              "referenced_table": "orders", "referenced_columns": ["id"]}]  # fmt: skip
    return [
        table("sales", "orders", [col("id", "bigint", 1), col("total", "int", 2)], pk=["id"]),
        table("archive", "orders", [col("id", "bigint", 1)], pk=["id"]),
        table("sales", "Customers", [col("id", "bigint", 1)], pk=["id"]),
        table("archive", "customers", [col("id", "bigint", 1)], pk=["id"]),
        items,
    ]


def _job(root: Path, engine: str, tables: list[dict] | None = None) -> LocalArtifactStore:
    tables = tables if tables is not None else _tables()
    coll = collector(engine, tables)
    coll["metadata"]["source_database"].update(database_name="sales", version="16.4")
    wave = {"wave": 1, "title": "Move to Aurora", "engines": [_AURORA[engine]],
            "moves_from": [engine], "homogeneity": "homogeneous",
            "tables": [t["table_id"] for t in tables], "gate": "Aurora serves every query."}  # fmt: skip
    store = LocalArtifactStore(str(root))
    store.write_json("sales/job1/synthesis/v2/report.json",
                     {"assignment_summary": {"version": 2}, "migration_waves": [wave]})  # fmt: skip
    store.write_json(
        "sales/job1/assignment/v2/assignment.json", {"version": 2, "query_assignments": []}
    )
    store.write_json("sales/job1/collector/output.json", coll)
    write_step1(store, "sales", "job1")
    write_plans(store, "sales", "job1")
    return store


_PAIRS = [("archive", "customers"), ("archive", "orders"), ("sales", "Customers"),
          ("sales", "items"), ("sales", "orders")]  # fmt: skip


def _selections(inputs) -> list[tuple[str, str]]:
    rules = table_mappings(inputs)["rules"]
    # No rule renames: DMS writes each table into the schema (database) of its source's name.
    assert {r["rule-type"] for r in rules} <= {"selection", "table-settings", "validation"}
    return [
        (r["object-locator"]["schema-name"], r["object-locator"]["table-name"])
        for r in rules
        if r["rule-type"] == "selection"
    ]


def _create_table(ddl: str, q: str, schema: str, name: str) -> str:
    found = re.search(rf"CREATE TABLE {q}{schema}{q}\.{q}{name}{q} \(\n(.*?)\n\);", ddl, re.S)
    assert found, f"no CREATE TABLE for {schema}.{name}"
    return found.group(1)


def test_postgresql_tables_of_one_name_move_each_into_its_own_schema(tmp_path):
    inputs = load_step1(_job(tmp_path, "postgresql"), "sales", "job1")
    assert inputs.contract.status == "valid" and inputs.skipped == ()
    assert _selections(inputs) == _PAIRS
    ddl = render_files(inputs)[DDL_FILE]
    # Each schema once (#2525 review): the file's own statement, before the enum types.
    schemas = re.findall(r'^CREATE SCHEMA IF NOT EXISTS "([^"]+)";', ddl, re.M)
    assert schemas == [CONTROL_SCHEMA, "archive", "sales"]
    assert "SET search_path" not in ddl
    assert '"total"' in _create_table(ddl, '"', "sales", "orders")
    assert '"total"' not in _create_table(ddl, '"', "archive", "orders")
    for schema, name in _PAIRS:
        _create_table(ddl, '"', schema, name)
        assert f'-- ALTER TABLE "{schema}"."{name}" REPLICA IDENTITY FULL;' in ddl
    assert re.search(r'REFERENCES "sales"\."orders"', ddl)
    for schema in ("archive", "sales"):
        assert f'-- GRANT USAGE, CREATE ON SCHEMA "{schema}" TO dms_user;' in ddl
        assert f'IN SCHEMA "{schema}" TO dms_user;' in ddl
    assert "The table names are not qualified" not in ddl


def test_mysql_tables_of_one_name_move_each_into_its_own_database(tmp_path):
    inputs = load_step1(_job(tmp_path, "mysql"), "sales", "job1")
    assert inputs.contract.status == "valid" and inputs.skipped == ()
    assert _selections(inputs) == _PAIRS
    files = render_files(inputs)
    ddl = files[DDL_FILE]
    created = re.findall(r"^CREATE DATABASE IF NOT EXISTS `([^`]+)`", ddl, re.M)
    assert created == [CONTROL_SCHEMA, "archive", "sales"]
    assert "CREATE SCHEMA" not in ddl  # each database once, with the file's collation clause
    assert not re.search(r"^USE ", ddl, re.M)
    assert "`total`" in _create_table(ddl, "`", "sales", "orders")
    assert "`total`" not in _create_table(ddl, "`", "archive", "orders")
    for schema, name in _PAIRS:
        _create_table(ddl, "`", schema, name)
    for database in ("archive", "sales"):
        assert f"ON `{database}`.* TO 'dms_user'@'%';" in ddl
    template = yaml.safe_load(files[DMS_TEMPLATE])
    target = template["Resources"]["TargetEndpoint"]["Properties"]
    # DMS writes each source database into the target database of its name: no DatabaseName.
    assert target["MySqlSettings"]["TargetDbType"] == "multiple-databases"
    assert "DatabaseName" not in target
    assert "TargetDatabaseName" not in template["Parameters"]


@pytest.mark.parametrize(
    "engine",
    [pytest.param("mysql", marks=pytest.mark.passes_on_main),  # a lint guard of the new endpoint
     "postgresql"],
)  # fmt: skip
def test_the_stack_of_several_schemas_passes_cfn_lint(tmp_path, engine):
    from cfnlint.api import ManualArgs, lint

    inputs = load_step1(_job(tmp_path, engine), "sales", "job1")
    text = render_files(inputs)[DMS_TEMPLATE]
    found = lint(text, config=ManualArgs(regions=["us-east-1"], ignore_checks=["W3037"]))
    assert [f"{m.rule.id} {m.message}" for m in found] == []


def test_mysql_verify_reads_each_tables_own_database_on_aurora(tmp_path):
    from src.tools.data_movement.dms_step import step_target_database

    inputs = load_step1(_job(tmp_path, "mysql"), "sales", "job1")
    database = step_target_database(inputs, None)
    assert database is None
    script = render_script(inputs.contract, inputs.contract_sha256, "target",
                           target_database=database)  # fmt: skip
    assert "FROM `archive`.`orders`" in script and "FROM `sales`.`orders`" in script
    with pytest.raises(DmsStepError, match="its own database"):
        step_target_database(inputs, "other")


def test_mysql_verify_of_one_database_reads_the_target_database(tmp_path):
    from src.tools.data_movement.dms_step import step_target_database

    tables = [table("sales", "orders", [col("id", "bigint", 1)], pk=["id"])]
    inputs = load_step1(_job(tmp_path, "mysql", tables), "sales", "job1")
    script = render_script(inputs.contract, inputs.contract_sha256, "target",
                           target_database=step_target_database(inputs, "other"))  # fmt: skip
    assert "FROM `other`.`orders`" in script
    assert render_dms_template(inputs)["Parameters"]["TargetDatabaseName"]["Default"] == "sales"


@pytest.mark.passes_on_main  # main's form for one moved schema, kept (#2494)
def test_a_step_whose_other_schema_moves_nothing_keeps_its_one_schema_form(tmp_path):
    """A collector of two schemas whose second schema's tables are all left out (a name
    that folds to another's in its schema) moves one schema: the DDL is the one-schema
    form, its table names bare in the schema it creates and sets."""
    tables = [table("sales", "orders", [col("id", "bigint", 1)], pk=["id"]),
              table("archive", "Logs", [col("id", "bigint", 1)]),
              table("archive", "logs", [col("id", "bigint", 1)])]  # fmt: skip
    inputs = load_step1(_job(tmp_path, "postgresql", tables), "sales", "job1")
    assert [s.table_id for s in inputs.skipped] == ["archive.Logs", "archive.logs"]
    ddl = render_files(inputs)[DDL_FILE]
    assert 'SET search_path TO "sales";' in ddl
    assert re.findall(r'CREATE TABLE ("[^(]+) \(', ddl) == ['"orders"']


@pytest.mark.passes_on_main  # the digests were recorded on main: this guards its output (#2494)
@pytest.mark.parametrize("name", step1_samples.SAMPLES)
def test_one_schema_samples_write_what_they_wrote_before(name):
    """The wordpress (MySQL) and discourse (PostgreSQL) samples are one schema each: every
    file step 1 writes for them is byte for byte main's before #2494."""
    recorded = json.loads(step1_samples.DIGESTS.read_text())[name]
    assert step1_samples.digests(name) == recorded


@pytest.mark.parametrize("engine,q", [("postgresql", '"'), ("mysql", "`")])
def test_a_foreign_key_never_lands_on_another_schemas_table(tmp_path, engine, q):
    """#2525 review: ``sales.items`` references ``orders``. ``sales.orders`` and
    ``sales.Orders`` are left out (their names fold to one), and ``archive.orders`` moves.
    The key resolves to its own schema's table, which does not move, so it is left out with
    a reason, never pointed at ``archive.orders``."""
    tables = [t for t in _tables() if t["table_id"] not in ("sales.Customers", "archive.customers")]
    tables.append(table("sales", "Orders", [col("id", "bigint", 1)], pk=["id"]))
    inputs = load_step1(_job(tmp_path, engine, tables), "sales", "job1")
    assert [s.table_id for s in inputs.skipped] == ["sales.Orders", "sales.orders"]
    ddl = render_files(inputs)[DDL_FILE]
    assert "REFERENCES" not in ddl
    _create_table(ddl, q, "sales", "items")
    _create_table(ddl, q, "archive", "orders")
    left = [(o.table_id, o.kind, o.name) for o in inputs.left_out_objects]
    assert ("sales.items", "foreign key", "fk_items_order") in left
    assert "fk_items_order" in ddl  # the file names the key it leaves out, with the reason


@pytest.mark.passes_on_main  # main's one-database form, kept and now documented (#2525 review)
def test_mysql_tables_of_one_moved_database_go_into_the_target_database(tmp_path):
    """A MySQL step whose other database moves no table keeps the one-database form: its
    tables go into TargetDatabaseName (default: the collected database, ``sales`` here),
    whatever their own database is called (``archive``)."""
    tables = [table("archive", "orders", [col("id", "bigint", 1)], pk=["id"]),
              table("sales", "Logs", [col("id", "bigint", 1)]),
              table("sales", "logs", [col("id", "bigint", 1)])]  # fmt: skip
    inputs = load_step1(_job(tmp_path, "mysql", tables), "sales", "job1")
    files = render_files(inputs)
    assert "USE `sales`;" in files[DDL_FILE] and "CREATE TABLE `orders`" in files[DDL_FILE]
    params = yaml.safe_load(files[DMS_TEMPLATE])["Parameters"]
    assert params["TargetDatabaseName"]["Default"] == "sales"


@pytest.mark.parametrize("engine,q", [("postgresql", '"'), ("mysql", "`")])
def test_a_one_schema_step_lists_a_key_whose_table_does_not_move(tmp_path, engine, q):
    """#2530: the design keeps ``fk_items_order`` (``orders`` and ``Orders`` are left out of
    step 1 only), so step 1's own check drops it. A step of one schema lists it with the
    reason a step of several schemas gives, and no ``REFERENCES`` is written."""
    items = table("sales", "items", [col("id", "bigint", 1), col("order_id", "bigint", 2)],
                  pk=["id"])  # fmt: skip
    items["foreign_keys"] = [{"constraint_name": "fk_items_order", "columns": ["order_id"],
                              "referenced_table": "orders", "referenced_columns": ["id"]}]  # fmt: skip
    tables = [items, table("sales", "orders", [col("id", "bigint", 1)], pk=["id"]),
              table("sales", "Orders", [col("id", "bigint", 1)], pk=["id"])]  # fmt: skip
    inputs = load_step1(_job(tmp_path, engine, tables), "sales", "job1")
    assert [s.table_id for s in inputs.skipped] == ["sales.Orders", "sales.orders"]
    ddl = render_files(inputs)[DDL_FILE]
    assert "REFERENCES" not in ddl
    assert f"CREATE TABLE {q}items{q} (" in ddl
    assert [(o.table_id, o.kind, o.name) for o in inputs.left_out_objects] == [
        ("sales.items", "foreign key", "fk_items_order")
    ]
    header = " ".join(line.removeprefix("--").strip() for line in ddl.splitlines())
    assert "foreign key sales.items.fk_items_order" in header
    assert "the table it references does not move in step 1, so the key is left out" in header
