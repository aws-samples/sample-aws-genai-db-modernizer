# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The Aurora MySQL draft never writes a CREATE TABLE that fails with ERROR 1118 (#2239).

SQL Server and Oracle let a row overflow its page, so nine ``varbinary(8000)`` columns,
or many wide ``nvarchar(n)`` columns, are legal there. Aurora MySQL counts each column's
full width (utf8mb4: 4 bytes per character) against 65,535 bytes per row, and InnoDB
needs the in-row part of a record to fit 8,126 bytes. The draft moves the widest
VARCHAR and VARBINARY columns that are in no key to TEXT or BLOB until the row fits,
flags each one it moves, and notes a table it cannot make fit. The byte counts are
checked against a MySQL 8.0 server in ``test_mysql_row_size.py``.
"""

from __future__ import annotations

import pytest

from src.contracts.schema_design_input import AgentTable
from src.tools.schema.aurora_common.ddl_generator import (
    DdlResult,
    generate_mysql_ddl,
    generate_pg_ddl,
)


def _col(name: str, data_type: str, normalized: str, **extra) -> dict:
    return {
        "column_name": name,
        "data_type": data_type,
        "normalized_data_type": normalized,
        "nullable": extra.pop("nullable", True),
        **extra,
    }


_ID = _col("id", "int", "integer", nullable=False)


def _table(columns: list[dict], name: str = "t", **extra) -> AgentTable:
    return AgentTable.model_validate(
        {
            "table_id": f"db.{name}",
            "table_name": name,
            "row_count": 1,
            "columns": [_ID, *columns],
            "primary_key": ["id"],
            **extra,
        }
    )


def _varbinary(n: int, count: int) -> list[dict]:
    return [_col(f"b{i}", "varbinary", "binary", max_length=n) for i in range(count)]


def _nvarchar(n: int, count: int, prefix: str = "s") -> list[dict]:
    return [_col(f"{prefix}{i}", "nvarchar", "string", max_length=n) for i in range(count)]


def _types(result: DdlResult) -> dict[str, str]:
    return {c.name: c.aurora_type for t in result.tables for c in t.columns}


def _row_notes(result: DdlResult) -> list[dict]:
    return [n for n in result.column_notes if n["kind"] == "row_size"]


def test_nine_varbinary_8000_columns_fit_with_one_moved_to_blob():
    result = generate_mysql_ddl([_table(_varbinary(8000, 9))], source_engine="sqlserver")
    types = _types(result)
    assert types["b8"] == "BLOB"
    assert [types[f"b{i}"] for i in range(8)] == ["VARBINARY(8000)"] * 8
    (residual,) = [r for r in result.residuals if r["column"] == "b8"]
    assert residual["fallback_type"] == "BLOB"
    assert "65,535" in residual["reason"] and "ERROR 1118" in residual["reason"]
    assert "VARBINARY(8000)" in residual["reason"]
    (note,) = _row_notes(result)
    assert note["table"] == "t" and "b8" in note["reason"]
    assert "`b8` BLOB" in result.full_ddl


def test_wide_nvarchar_columns_fit_with_the_last_ones_moved_to_text():
    """nvarchar(4000) is 16,002 bytes in utf8mb4: five of them are 80,010."""
    result = generate_mysql_ddl([_table(_nvarchar(4000, 5))], source_engine="sqlserver")
    types = _types(result)
    assert types["s4"] == "TEXT"
    assert [types[f"s{i}"] for i in range(4)] == ["VARCHAR(4000)"] * 4
    assert [r["column"] for r in result.residuals] == ["s4"]


def test_many_short_columns_fit_the_innodb_record():
    """Sixty nvarchar(50): 12,068 bytes, under 65,535, but InnoDB keeps each in the row
    (200 bytes, not over 255), so the record is over 8,126 bytes. 25 move (measured)."""
    result = generate_mysql_ddl([_table(_nvarchar(50, 60))], source_engine="sqlserver")
    types = _types(result)
    moved = sorted((n for n, t in types.items() if t == "TEXT"), key=lambda n: int(n[1:]))
    assert moved == [f"s{i}" for i in range(35, 60)]
    assert all(types[f"s{i}"] == "VARCHAR(50)" for i in range(35))
    reason = next(r["reason"] for r in result.residuals if r["column"] == "s59")
    assert "8,126" in reason and "ERROR 1118" in reason


def test_key_and_indexed_columns_never_move():
    """The last column is in an index and the one before it in a foreign key, so the
    move goes to the next one back."""
    parent = AgentTable.model_validate(
        {"table_id": "db.p", "table_name": "p", "row_count": 1,
         "columns": [_col("k", "varbinary", "binary", max_length=8000, nullable=False)],
         "primary_key": ["k"]}  # fmt: skip
    )
    table = _table(
        _varbinary(8000, 9),
        indexes=[{"index_name": "ix_b8", "columns": ["b8"], "is_unique": False}],
        foreign_keys=[
            {"constraint_name": "fk_b7", "columns": ["b7"], "referenced_table": "p",
             "referenced_columns": ["k"]}  # fmt: skip
        ],
    )
    result = generate_mysql_ddl([table, parent], source_engine="sqlserver")
    types = _types(result)
    assert (types["b8"], types["b7"], types["b6"]) == ("VARBINARY(8000)", "VARBINARY(8000)", "BLOB")


def test_a_table_that_cannot_fit_is_noted_with_what_to_do():
    """460 DECIMAL(38,10) columns are 8,280 fixed-width bytes, over the InnoDB record:
    no column can move, so the table is noted and nothing is moved."""
    columns = [
        _col(f"d{i}", "decimal", "decimal", numeric_precision=38, numeric_scale=10)
        for i in range(460)
    ]
    result = generate_mysql_ddl([_table(columns)], source_engine="sqlserver")
    assert not [t for t in _types(result).values() if t in ("TEXT", "BLOB")]
    (note,) = _row_notes(result)
    assert note["table"] == "t"
    assert "split the table" in note["reason"]
    assert "ERROR 1118" in note["reason"]


@pytest.mark.parametrize("engine", ["oracle", "postgresql", "mysql"])
def test_every_source_engine_gets_the_check(engine):
    """Only the target decides the limit: Oracle VARCHAR2(4000 CHAR), a PostgreSQL
    varchar(4000) and a latin1 MySQL varchar(4000) all become VARCHAR(4000) in utf8mb4."""
    data_type = {"oracle": "varchar2", "postgresql": "character varying", "mysql": "varchar"}
    columns = [_col(f"s{i}", data_type[engine], "string", max_length=4000) for i in range(5)]
    result = generate_mysql_ddl([_table(columns)], source_engine=engine)
    assert _types(result)["s4"] in ("TEXT", "MEDIUMTEXT")
    assert _row_notes(result)


@pytest.mark.passes_on_main
def test_a_row_under_both_limits_and_the_postgresql_draft_are_unchanged():
    narrow = _table(_varbinary(8000, 7) + _nvarchar(50, 10))
    result = generate_mysql_ddl([narrow], source_engine="sqlserver")
    assert "BLOB" not in result.full_ddl and "TEXT" not in result.full_ddl
    assert not result.residuals and not _row_notes(result)
    wide = generate_pg_ddl([_table(_varbinary(8000, 9))], source_engine="sqlserver")
    assert set(_types(wide).values()) == {"BIGINT", "BYTEA"}
    assert not wide.residuals


def test_the_schema_output_carries_one_row_size_note_per_table():
    """The merged design names what moved in ``app_layer_notes``, so the deliverables
    say it (the table definition already holds the BLOB)."""
    from src.tools.schema.aurora_common.delta_merge import AuroraDesignBase, merge_design_delta

    base = AuroraDesignBase(
        engine="aurora_mysql",
        job_id="j",
        source_database="app",
        source_engine="sqlserver",
        tables=[_table(_varbinary(8000, 9))],
    )
    result = merge_design_delta(base, {"delta_version": "1.0"})
    assert result.errors == []
    (note,) = [n for n in result.output["app_layer_notes"] if n["feature"] == "row_size"]
    assert note["source_object"] == "t" and "b8" in note["recommendation"]
    (table,) = result.output["table_definitions"]
    assert {c["name"]: c["aurora_type"] for c in table["columns"]}["b8"] == "BLOB"
    assert {c["name"]: c["needs_judgment"] for c in table["columns"]}["b8"] is True


def test_a_column_another_tables_key_references_never_moves():
    """The parent column a foreign key references is in no primary key or index of the
    design, and its row is over the limit: it keeps its type (the child's key needs it),
    and the column before it moves instead."""
    parent = _table(_varbinary(8000, 9), name="p")
    child = AgentTable.model_validate(
        {"table_id": "db.c", "table_name": "c", "row_count": 1,
         "columns": [_ID, _col("ref", "varbinary", "binary", max_length=8000)],
         "primary_key": ["id"],
         "foreign_keys": [{"constraint_name": "fk_c_p", "columns": ["ref"],
                           "referenced_table": "p", "referenced_columns": ["b8"]}]}
    )  # fmt: skip
    result = generate_mysql_ddl([parent, child], source_engine="sqlserver")
    types = {(t.table_name, c.name): c.aurora_type for t in result.tables for c in t.columns}
    assert types[("p", "b8")] == "VARBINARY(8000)"
    assert types[("p", "b7")] == "BLOB"


def test_a_models_nvarchar_override_moves_like_a_varchar():
    """PR #2617 review: an NVARCHAR(n) override passes the allowlist and counts 3 bytes per
    character, so it moves to TEXT too, rather than leaving the table noted as unable to
    fit."""
    from src.tools.schema.aurora_common.ddl_generator import TypeOverride

    table = _table(_nvarchar(4000, 6))
    overrides = {("t", f"s{i}"): TypeOverride("NVARCHAR(4000)") for i in range(6)}
    result = generate_mysql_ddl([table], overrides, source_engine="sqlserver")
    types = _types(result)
    assert types["s5"] == "TEXT"
    assert [types[f"s{i}"] for i in range(5)] == ["NVARCHAR(4000)"] * 5
    (note,) = _row_notes(result)
    assert "moved to TEXT or BLOB" in note["reason"]
