# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#2478: the Aurora design leaves out the tables its keys cannot tell apart, and the
tables of a system schema, each with a note.

A design of one schema keys a table by its bare name, case and quote marks folded
(``table_keys.table_key``), so ``Orders`` and ``orders`` share one key. Like two schemas'
``Sales.orders`` and ``sales.orders`` (#2479), neither is designed. A table in a system
schema of the source engine (``pg_catalog`` on PostgreSQL, ``mysql`` on MySQL) is the
database's own: the design does not create it. A schema another engine reserves is a user
schema here (``sys`` on PostgreSQL, ``pg_app`` on MySQL), so it is designed. The left-out machinery (#2487) names each one in every deliverable.
Designs saved before still load as they were written.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.contracts.aurora_mysql_model_output import AuroraMySQLModelOutputContract
from src.contracts.aurora_postgresql_model_output import AuroraPostgresqlModelOutputContract
from src.contracts.schema_design_input import AgentTable
from src.shared.left_out_objects import left_out_objects
from src.shared.table_keys import DesignTableKeys
from src.tools.schema.aurora_common.delta_merge import AuroraDesignBase, merge_design_delta
from src.tools.schema.aurora_common.saved_design import check_design_ddl
from tests.unit.tools.schema.aurora_common.test_schema_qualified_tables import (
    DB,
    JOB,
    _base,
    _merged,
    _table,
)

FIXTURES = Path(__file__).resolve().parents[5] / "tests/fixtures"


def _names(design: dict) -> list[tuple[str | None, str]]:
    return [(t.get("schema_name"), t["table_name"]) for t in design["table_definitions"]]


def _notes(design: dict, feature: str) -> list[dict]:
    return [n for n in design["app_layer_notes"] if n["feature"] == feature]


@pytest.mark.parametrize("engine", ["aurora_postgresql", "aurora_mysql"])
def test_one_schemas_tables_whose_names_fold_to_one_are_left_out_with_a_note(engine) -> None:
    """``Orders`` and ``orders`` in one schema share the bare key ``orders``: neither is
    designed, each gets a ``colliding_table_name`` note, and a delta naming either finds
    no table. The design's other table is created as before."""
    tables = [
        _table("Orders", "public"),
        _table("orders", "public", index="by_note2"),
        _table("items", "public"),
    ]
    base = _base(engine, tables)
    assert not base.qualified
    design = _merged(engine, tables)
    assert _names(design) == [(None, "items")]
    assert design["generated_ddl"].count("CREATE TABLE ") == 1
    notes = _notes(design, "colliding_table_name")
    assert [n["source_object"] for n in notes] == ["Orders", "orders"]
    assert all("its name matches another table" in n["recommendation"] for n in notes)
    assert [o["name"] for o in left_out_objects(design["app_layer_notes"])] == ["Orders", "orders"]
    delta = {"delta_version": "1.0", "tables": [{"table_name": "orders",
             "add_indexes": [{"index_name": "ix_x", "columns": ["id", "note"]}]}]}  # fmt: skip
    result = merge_design_delta(base, delta)
    assert result.output is None and "unknown table" in result.errors[0]


@pytest.mark.passes_on_main
def test_two_schemas_tables_whose_names_differ_only_in_case_stay_apart() -> None:
    """In a design of two schemas, ``sales.Orders`` and ``archive.orders`` have two keys:
    both are designed, each in its own schema."""
    design = _merged("aurora_postgresql", [_table("Orders", "sales"), _table("orders", "archive")])
    assert _names(design) == [("sales", "Orders"), ("archive", "orders")]
    assert _notes(design, "colliding_table_name") == []


def _merged_from(source_engine: str, engine: str, tables: list[AgentTable]) -> dict:
    base = AuroraDesignBase(
        engine=engine, job_id=JOB, source_database=DB, source_engine=source_engine, tables=tables
    )
    result = merge_design_delta(base, {"delta_version": "1.0"})
    assert result.errors == [] and result.output is not None
    return result.output


_SYSTEM = [
    ("postgresql", "aurora_postgresql", "pg_catalog"),
    ("postgresql", "aurora_postgresql", "information_schema"),
    ("postgresql", "aurora_postgresql", "pg_toast"),
    ("postgresql", "aurora_postgresql", "pg_temp_3"),
    ("postgresql", "aurora_postgresql", "pg_toast_temp_3"),
    ("mysql", "aurora_mysql", "mysql"),
    ("mysql", "aurora_mysql", "information_schema"),
    ("mysql", "aurora_mysql", "performance_schema"),
    ("mysql", "aurora_mysql", "sys"),
    ("mariadb", "aurora_mysql", "sys"),
]


@pytest.mark.parametrize("source_engine,engine,schema", _SYSTEM)
def test_a_system_schemas_table_is_left_out_with_a_note(source_engine, engine, schema) -> None:
    """A table of a system schema of the source engine gets no ``CREATE TABLE`` and no
    ``CREATE SCHEMA``, but a ``system_schema`` note that names it; it does not make the
    design one of several schemas, so the user schema's tables keep their bare names."""
    tables = [_table("orders", "public"), _table("audit", schema)]
    design = _merged_from(source_engine, engine, tables)
    assert _names(design) == [(None, "orders")]
    assert schema not in design["generated_ddl"].split("-- Not created")[0]
    assert "CREATE SCHEMA" not in design["generated_ddl"]
    (note,) = _notes(design, "system_schema")
    assert note["source_object"] == f"{schema}.audit"
    assert note["recommendation"].startswith("Table not created: it is in the system schema")
    (obj,) = left_out_objects(design["app_layer_notes"])
    assert (obj["name"], obj["label"], obj["object"]) == (
        f"{schema}.audit",
        "System schema",
        "table",
    )
    assert design["generated_ddl"].endswith(f"-- Not created: {schema}.audit: {obj['reason']}")
    if engine == "aurora_mysql":
        AuroraMySQLModelOutputContract.model_validate(design)
    else:
        AuroraPostgresqlModelOutputContract.model_validate(design)


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    "source_engine,engine,schema",
    [
        ("postgresql", "aurora_postgresql", "sys"),
        ("postgresql", "aurora_postgresql", "mysql"),
        ("postgresql", "aurora_postgresql", "performance_schema"),
        ("mysql", "aurora_mysql", "pg_app"),
        ("mysql", "aurora_mysql", "pg_catalog"),
        ("sqlserver", "aurora_postgresql", "pg_catalog"),
    ],
)
def test_a_schema_only_another_engine_reserves_is_designed(source_engine, engine, schema) -> None:
    """#2478 review: the system schemas are the source engine's own. ``sys`` is an ordinary
    PostgreSQL schema (apps ported from Oracle or SQL Server use it) and ``pg_app`` an
    ordinary MySQL database, so their tables are designed, each in its schema, with no
    ``system_schema`` note."""
    design = _merged_from(
        source_engine, engine, [_table("orders", "public"), _table("orders", schema)]
    )
    assert _names(design) == [("public", "orders"), (schema, "orders")]
    assert _notes(design, "system_schema") == []
    assert design["generated_ddl"].count("CREATE TABLE ") == 2


def test_a_system_schemas_table_in_a_design_of_two_schemas_is_left_out() -> None:
    """With two user schemas the design stays schema-qualified; the system table is still
    left out and gets no ``CREATE SCHEMA``."""
    tables = [_table("orders", "sales"), _table("orders", "archive"), _table("audit", "pg_catalog")]
    design = _merged("aurora_postgresql", tables)
    assert _names(design) == [("sales", "orders"), ("archive", "orders")]
    assert 'CREATE SCHEMA IF NOT EXISTS "pg_catalog"' not in design["generated_ddl"]
    assert [n["source_object"] for n in _notes(design, "system_schema")] == ["pg_catalog.audit"]


def test_which_schemas_are_system_schemas() -> None:
    from src.shared.table_keys import is_system_schema, spans_schemas

    pg = (
        "pg_catalog",
        "PG_CATALOG",
        '"pg_temp_3"',
        "pg_toast",
        "pg_toast_temp_1",
        "information_schema",
    )
    assert all(is_system_schema(s, "postgresql") for s in pg)
    my = ("mysql", "MySQL", "information_schema", "performance_schema", "sys")
    assert all(is_system_schema(s, e) for s in my for e in ("mysql", "mariadb"))
    assert not any(
        is_system_schema(s, "postgresql") for s in ("sys", "mysql", "performance_schema")
    )
    assert not any(is_system_schema(s, "mysql") for s in ("pg_app", "pg_catalog", "pg_toast"))
    assert not any(is_system_schema(s, "postgresql") for s in (None, "", "public", "mysql_app"))
    assert not any(is_system_schema("pg_catalog", e) for e in (None, "", "sqlserver", "oracle"))
    assert not spans_schemas(["public", "pg_catalog"], "postgresql")
    assert spans_schemas(["public", "sys"], "postgresql")
    assert spans_schemas(["shop", "pg_app"], "mysql")
    assert not spans_schemas(["shop", "sys"], "mysql")
    assert spans_schemas(["sales", "archive", "pg_catalog"], "postgresql")


def test_a_query_on_a_left_out_system_table_keeps_its_own_key() -> None:
    """#2478 review: a design of ``public`` plus ``pg_catalog`` is one of one schema, so its
    keys name no schema. A query on ``pg_catalog.audit`` still names no design table, and
    its row keeps ``pg_catalog.audit``, never the user table ``audit``: from the source
    engine (the design's own reader) and from the design's ``system_schema`` notes (a
    saved design's reader). ``public.audit`` and ``audit`` still read as the user table."""
    pairs = [("audit", None), ("users", None)]
    for keys in (
        DesignTableKeys(pairs, DB, source_engine="postgresql"),
        DesignTableKeys(pairs, DB, system_schemas={"pg_catalog"}),
    ):
        assert keys.key_of("pg_catalog.audit") is None
        assert keys.key_of('"PG_CATALOG"."audit"') is None
        assert keys.row_key("pg_catalog.audit") == "pg_catalog.audit"
        assert keys.key_of("public.audit") == keys.key_of("audit") == "audit"
        assert keys.row_key(f"{DB}.audit") == "audit"
    # On MySQL ``sys`` is a system schema, on PostgreSQL a user schema the design lacks.
    my = DesignTableKeys([("orders", None)], "shop", source_engine="mysql")
    assert my.key_of("sys.orders") is None and my.row_key("sys.orders") == "sys.orders"
    assert my.key_of("shop.orders") == "orders"
    pg = DesignTableKeys([("orders", None)], "shop", source_engine="postgresql")
    assert pg.key_of("sys.orders") == "orders"
    # A database named like a system schema is the database, not the schema.
    named_sys = DesignTableKeys([("orders", None)], "sys", source_engine="mysql")
    assert named_sys.key_of("sys.orders") == "orders"


def test_a_query_on_a_left_out_system_table_targets_no_access_pattern_table() -> None:
    """A query on ``pg_catalog.audit`` finds no design table: it never takes the user
    table ``audit``'s index, while a query on ``public.audit`` does."""
    from src.contracts.schema_design_input import AgentQueryPattern, QueryType
    from src.tools.schema.aurora_common.access_patterns import derive_access_patterns

    def query(qid: str, table: str) -> AgentQueryPattern:
        return AgentQueryPattern(
            query_id=qid, query_text=f"SELECT * FROM {table} WHERE note = $1",
            query_type=QueryType.SELECT, frequency_per_hour=3600, tables_accessed=[table],
            filter_columns=["note"],
        )  # fmt: skip

    base = _base("aurora_postgresql", [_table("audit", "public"), _table("audit", "pg_catalog")])
    assert not base.qualified
    queries = [query("q1", "public.audit"), query("q2", "pg_catalog.audit")]
    derived = derive_access_patterns(
        queries, base.tables, base.engine, base.source_engine, source_database=DB
    )
    by_id = {p["pattern_id"]: p for p in derived.patterns}
    assert by_id["q1"]["index_used"] == "by_note" and by_id["q1"]["load_testable"]
    assert by_id["q2"]["index_used"] == "__seq_scan__" and not by_id["q2"]["load_testable"]


@pytest.mark.passes_on_main
@pytest.mark.parametrize("name", ["case_only_names", "system_schema"])
def test_a_design_saved_before_2478_still_loads_as_written(name) -> None:
    """A design main wrote before #2478 (``Orders`` and ``orders`` both created; a
    ``pg_catalog`` table created in its schema) still passes the saved-design check and
    the contract, unchanged."""
    saved = json.loads((FIXTURES / f"aurora_postgresql_{name}_pre_2478.json").read_text())
    checked = check_design_ddl(saved, "aurora_postgresql")
    assert checked["generated_ddl"] == saved["generated_ddl"]
    assert len(checked["table_definitions"]) == 2
    AuroraPostgresqlModelOutputContract.model_validate(checked)
