# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Each foreign key's referenced schema and each database's collation, collected (#2531).

The collection scripts report the schema of a foreign key's referenced table
(``referenced_schema_name``, from each engine's catalog), and the MySQL script each
database's default character set and collation (``metadata.schemas``). The collector
output keeps them on ``ForeignKey.referenced_schema`` and ``SourceDatabase.schemas``. A
collection written before #2531 has neither and loads as before. The scripts run on real
servers in ``tests/unit/tools/database/test_postgres_foreign_keys_live_server.py`` and
``tests/integration/test_collect_mysql_collations.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.agents.collector.offline_ingest import build_offline_collector_output
from src.tools.database.offline_parser import parse_offline_collection

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"

# The catalog each script reads the referenced schema from.
_CATALOG = {
    "mysql": r"'referenced_schema_name', kcu\.referenced_table_schema",
    "postgresql": r"rn\.nspname AS referenced_schema_name",
    "sqlserver": r"rs\.name AS referenced_schema_name",
    "oracle": r"'referenced_schema_name' VALUE LOWER\(a\.R_OWNER\)",
}


def _fk_section(engine: str) -> str:
    sql = (SCRIPTS / f"collect-{engine}.sql").read_text()
    start = re.search(r"\d+\.\s*Foreign Keys", sql)
    assert start is not None, engine
    end = re.search(r"\d+\.\s*Primary Keys", sql[start.end() :])
    assert end is not None, engine
    return sql[start.start() : start.end() + end.start()]


@pytest.mark.parametrize("engine", sorted(_CATALOG))
def test_every_script_reports_the_referenced_schema(engine: str) -> None:
    assert re.search(_CATALOG[engine], _fk_section(engine))


def test_the_postgresql_script_reads_the_namespace_of_the_referenced_table() -> None:
    section = _fk_section("postgresql")
    assert "JOIN pg_catalog.pg_namespace rn ON rn.oid = rcl.relnamespace" in section
    assert "n.nspname AS schema_name" in section


def test_the_sqlserver_script_reads_the_schema_of_the_referenced_table() -> None:
    assert "INNER JOIN sys.schemas rs ON rt.schema_id = rs.schema_id" in _fk_section("sqlserver")


def test_the_mysql_script_lists_each_databases_collation_from_schemata() -> None:
    text = " ".join((SCRIPTS / "collect-mysql.sql").read_text().split())
    assert "'schemas', ( SELECT JSON_ARRAYAGG(JSON_OBJECT( 'schema_name', s.SCHEMA_NAME," in text
    assert (
        "'default_collation', s.DEFAULT_COLLATION_NAME)) FROM information_schema.SCHEMATA s" in text
    )
    assert ("SELECT k.REFERENCED_TABLE_SCHEMA FROM information_schema.KEY_COLUMN_USAGE k "
            "WHERE k.TABLE_SCHEMA = @db AND k.REFERENCED_TABLE_NAME IS NOT NULL") in text  # fmt: skip


def _fk_row(table: str, name: str, referenced: str, **extra: str) -> dict:
    return {"table_name": table, "constraint_name": name, "column_name": "ref_id",
            "ordinal_position": 1, "referenced_table_name": referenced,
            "referenced_column_name": "id", **extra}  # fmt: skip


def _built(engine: str, tables: list[dict], fks: list[dict], metadata: dict | None = None):
    raw = {
        "metadata": {"version": "8.0.36", "database_name": "app", "engine": engine,
                     **(metadata or {})},
        "tables": [{"row_count": 1, **t} for t in tables],
        "columns": [{"table_name": t["table_name"], "column_name": c, "data_type": "int",
                     "is_nullable": "NO", "ordinal_position": i + 1}
                    for t in tables for i, c in enumerate(("id", "ref_id"))],
        "foreign_keys": fks,
    }  # fmt: skip
    return build_offline_collector_output(
        parse_offline_collection(raw), job_id="j", database_name="app", s3_key="k"
    )


def _keys(output: dict) -> dict[str, list[tuple[str, str, str | None]]]:
    return {
        t["table_id"]: [
            (fk["constraint_name"], fk["referenced_table"], fk.get("referenced_schema"))
            for fk in t.get("foreign_keys") or []
        ]
        for t in output["database_schema"]["tables"]
    }


def test_a_mysql_collection_keeps_the_referenced_database_and_each_databases_collation():
    schemas = [
        {"schema_name": "app", "default_character_set": "utf8mb4",
         "default_collation": "utf8mb4_unicode_ci"},
        {"schema_name": "billing", "default_character_set": "latin1",
         "default_collation": "latin1_swedish_ci"},
    ]  # fmt: skip
    output = _built(
        "mysql",
        [{"table_name": "orders"}, {"table_name": "customers"}],
        [_fk_row("orders", "fk_c", "customers", referenced_schema_name="billing"),
         _fk_row("customers", "fk_self", "customers", referenced_schema_name="app")],
        {"schemas": schemas},
    )  # fmt: skip
    assert _keys(output) == {
        "app.orders": [("fk_c", "customers", "billing")],
        "app.customers": [("fk_self", "customers", "app")],
    }
    assert output["metadata"]["source_database"]["schemas"] == schemas


def test_two_schemas_tables_of_one_name_keep_their_own_keys():
    """PostgreSQL reports each key's own schema (#2531): ``sales.orders`` and
    ``archive.orders`` each get theirs, never both."""
    output = _built(
        "postgresql",
        [{"table_name": "orders", "schema_name": "sales"},
         {"table_name": "orders", "schema_name": "archive"},
         {"table_name": "customers", "schema_name": "sales"}],
        [_fk_row("orders", "fk_sales", "customers", schema_name="sales",
                 referenced_schema_name="sales"),
         _fk_row("orders", "fk_archive", "customers", schema_name="archive",
                 referenced_schema_name="sales")],
    )  # fmt: skip
    keys = _keys(output)
    assert keys["sales.orders"] == [("fk_sales", "customers", "sales")]
    assert keys["archive.orders"] == [("fk_archive", "customers", "sales")]


def test_an_older_sqlserver_collection_gives_each_schemas_table_its_own_keys():
    """SQL Server rows carried the key's own ``schema_name`` before #2531: an older file with
    ``sales.orders`` and ``archive.orders`` now gives each table only its own keys, where it
    gave both tables both. It has no referenced schema."""
    output = _built(
        "sqlserver",
        [{"table_name": "orders", "schema_name": "sales"},
         {"table_name": "orders", "schema_name": "archive"},
         {"table_name": "customers", "schema_name": "sales"}],
        [_fk_row("orders", "fk_sales", "customers", schema_name="sales"),
         _fk_row("orders", "fk_archive", "customers", schema_name="archive")],
    )  # fmt: skip
    by_schema = {
        t["schema_name"]: [fk["constraint_name"] for fk in t.get("foreign_keys") or []]
        for t in output["database_schema"]["tables"]
        if t["table_name"] == "orders"
    }
    assert by_schema == {"sales": ["fk_sales"], "archive": ["fk_archive"]}


def test_a_blank_or_null_referenced_schema_is_not_kept():
    output = _built(
        "mysql",
        [{"table_name": "orders"}, {"table_name": "customers"}],
        [_fk_row("orders", "fk_a", "customers", referenced_schema_name="NULL"),
         _fk_row("orders", "fk_b", "customers", referenced_schema_name=" "),
         _fk_row("orders", "fk_c", "customers", referenced_schema_name="crm")],
    )  # fmt: skip
    assert _keys(output)["app.orders"] == [
        ("fk_a", "customers", None), ("fk_b", "customers", None), ("fk_c", "customers", "crm")
    ]  # fmt: skip


def test_a_schemas_entry_with_no_name_is_skipped_and_a_repeat_keeps_the_first():
    output = _built(
        "mysql",
        [{"table_name": "orders"}],
        [],
        {"schemas": [{"schema_name": "", "default_collation": "utf8mb4_bin"},
                     {"schema_name": "app", "default_collation": "utf8mb4_bin"},
                     {"schema_name": "app", "default_collation": "latin1_bin"}, "x"]},
    )  # fmt: skip
    assert output["metadata"]["source_database"]["schemas"] == [
        {"schema_name": "app", "default_character_set": None, "default_collation": "utf8mb4_bin"}
    ]


@pytest.mark.passes_on_main  # a collection written before #2531 loads as before
@pytest.mark.parametrize("engine", ["mysql", "postgresql", "sqlserver", "oracle"])
def test_an_older_collection_loads_with_neither(engine):
    output = _built(
        engine,
        [{"table_name": "orders", "schema_name": "app"}, {"table_name": "customers",
                                                         "schema_name": "app"}],
        [_fk_row("orders", "fk_c", "customers")],
    )  # fmt: skip
    (fk,) = next(t for t in output["database_schema"]["tables"] if t["table_name"] == "orders")[
        "foreign_keys"
    ]
    assert "referenced_schema" not in fk
    assert "schemas" not in output["metadata"]["source_database"]


@pytest.mark.passes_on_main  # a key row with no schema reaches every table of its name
def test_a_key_row_with_no_schema_reaches_each_table_of_its_name():
    output = _built(
        "postgresql",
        [{"table_name": "orders", "schema_name": "sales"},
         {"table_name": "orders", "schema_name": "archive"},
         {"table_name": "customers", "schema_name": "sales"}],
        [_fk_row("orders", "fk_c", "customers")],
    )  # fmt: skip
    keys = _keys(output)
    assert keys["sales.orders"] == keys["archive.orders"] == [("fk_c", "customers", None)]
