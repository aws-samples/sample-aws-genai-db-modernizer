# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Oracle and SQL Server date/time and large-object columns get their Aurora types (#2219).

Each column is written the way a collector reports it: live Oracle rows carry
``timestamp(6) with time zone`` in ``data_type`` and the fractional digits in
``numeric_scale`` (``ALL_TAB_COLUMNS.DATA_SCALE``); SQL Server rows carry the bare
``sys.types`` name with the digits in ``numeric_scale`` (``sys.columns.scale``) and
no ``max_length`` for a ``(max)`` type; a DDL upload keeps the type as written
(``datetime2(7)``, ``nvarchar(MAX)``, ``RAW(16)``). The normalized type is the one
the collector gives, so a test only passes when the native type wins over it.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from src.contracts.schema_design_input import AgentTable
from src.tools.schema.aurora_common.ddl_generator import (
    DdlResult,
    generate_mysql_ddl,
    generate_pg_ddl,
)

_ROOT = Path(__file__).resolve().parents[5]

PG = "aurora_postgresql"
MY = "aurora_mysql"


def _table(columns: list[dict], name: str = "t") -> AgentTable:
    return AgentTable.model_validate(
        {"table_id": f"db.{name}", "table_name": name, "row_count": 1, "columns": columns}
    )


def _col(data_type: str, normalized: str | None, **extra) -> dict:
    return {
        "column_name": "c",
        "data_type": data_type,
        "normalized_data_type": normalized,
        "nullable": True,
        **extra,
    }


def _generate(target: str, tables: list[AgentTable], engine: str) -> DdlResult:
    generate = generate_pg_ddl if target == PG else generate_mysql_ddl
    return generate(tables, source_engine=engine)


def _one(target: str, engine: str, column: dict):
    result = _generate(target, [_table([column])], engine)
    return result.tables[0].columns[0], result


# One case per row of the issue's table (and the source forms each row comes in):
# (id, engine, column, (pg type, pg flagged), (mysql type, mysql flagged)).
_ROWS = [
    # Oracle DATE -> TIMESTAMP(0) | DATETIME
    ("ora-date", "oracle", _col("date", "timestamp"), ("TIMESTAMP(0)", False), ("DATETIME", False)),
    ("ora-date-ddl", "oracle", _col("DATE", "date"), ("TIMESTAMP(0)", False), ("DATETIME", False)),
    # Oracle TIMESTAMP(p) -> TIMESTAMP(min(p,6)) | DATETIME(min(p,6)), flagged when p > 6
    ("ora-ts6", "oracle", _col("timestamp(6)", None, numeric_scale=6),
     ("TIMESTAMP(6)", False), ("DATETIME(6)", False)),
    ("ora-ts3", "oracle", _col("timestamp(3)", None, numeric_scale=3),
     ("TIMESTAMP(3)", False), ("DATETIME(3)", False)),
    ("ora-ts0", "oracle", _col("timestamp(0)", None, numeric_scale=0),
     ("TIMESTAMP(0)", False), ("DATETIME(0)", False)),
    ("ora-ts9", "oracle", _col("timestamp(9)", None, numeric_scale=9),
     ("TIMESTAMP(6)", True), ("DATETIME(6)", True)),
    ("ora-ts9-ddl", "oracle", _col("TIMESTAMP(9)", "timestamp", max_length=9),
     ("TIMESTAMP(6)", True), ("DATETIME(6)", True)),
    ("ora-ts-bare", "oracle", _col("TIMESTAMP", "timestamp"),
     ("TIMESTAMP(6)", False), ("DATETIME(6)", False)),
    # Oracle TIMESTAMP WITH TIME ZONE -> TIMESTAMPTZ | DATETIME(p), flagged
    ("ora-tstz", "oracle", _col("timestamp(6) with time zone", None, numeric_scale=6),
     ("TIMESTAMPTZ", False), ("DATETIME(6)", True)),
    ("ora-tstz3", "oracle", _col("timestamp(3) with time zone", None, numeric_scale=3),
     ("TIMESTAMPTZ", False), ("DATETIME(3)", True)),
    ("ora-tstz9", "oracle", _col("timestamp(9) with time zone", None, numeric_scale=9),
     ("TIMESTAMPTZ", True), ("DATETIME(6)", True)),
    ("ora-tstz-bare", "oracle", _col("timestamp with time zone", "timestamp"),
     ("TIMESTAMPTZ", False), ("DATETIME(6)", True)),
    # SQL Server datetime -> TIMESTAMP(3) | DATETIME(3)
    ("ms-datetime", "sqlserver", _col("datetime", "timestamp", numeric_scale=3, max_length=8),
     ("TIMESTAMP(3)", False), ("DATETIME(3)", False)),
    ("ms-datetime-ddl", "sqlserver", _col("DATETIME", "datetime"),
     ("TIMESTAMP(3)", False), ("DATETIME(3)", False)),
    # SQL Server datetime2(p) -> TIMESTAMP(min(p,6)) | DATETIME(min(p,6)), flagged when p > 6
    ("ms-dt2-7", "sqlserver", _col("datetime2", "timestamp", numeric_scale=7, max_length=8),
     ("TIMESTAMP(6)", True), ("DATETIME(6)", True)),
    ("ms-dt2-3", "sqlserver", _col("datetime2", "timestamp", numeric_scale=3),
     ("TIMESTAMP(3)", False), ("DATETIME(3)", False)),
    ("ms-dt2-0", "sqlserver", _col("datetime2", "timestamp", numeric_scale=0),
     ("TIMESTAMP(0)", False), ("DATETIME(0)", False)),
    ("ms-dt2-bare", "sqlserver", _col("datetime2", "timestamp"),
     ("TIMESTAMP(6)", True), ("DATETIME(6)", True)),
    ("ms-dt2-ddl", "sqlserver", _col("datetime2(7)", None, max_length=7),
     ("TIMESTAMP(6)", True), ("DATETIME(6)", True)),
    ("ms-dt2-ddl-2", "sqlserver", _col("DATETIME2(2)", None, max_length=2),
     ("TIMESTAMP(2)", False), ("DATETIME(2)", False)),
    # SQL Server smalldatetime -> TIMESTAMP(0) | DATETIME
    ("ms-smalldatetime", "sqlserver", _col("smalldatetime", "timestamp", numeric_scale=0),
     ("TIMESTAMP(0)", False), ("DATETIME", False)),
    # SQL Server datetimeoffset(p) -> TIMESTAMPTZ | DATETIME(p), flagged
    ("ms-dto-3", "sqlserver", _col("datetimeoffset", "timestamp", numeric_scale=3),
     ("TIMESTAMPTZ", False), ("DATETIME(3)", True)),
    ("ms-dto-7", "sqlserver", _col("datetimeoffset", "timestamp", numeric_scale=7),
     ("TIMESTAMPTZ", True), ("DATETIME(6)", True)),
    ("ms-dto-ddl", "sqlserver", _col("datetimeoffset(7)", None, max_length=7),
     ("TIMESTAMPTZ", True), ("DATETIME(6)", True)),
    # SQL Server time(p) -> TIME(min(p,6))
    ("ms-time-3", "sqlserver", _col("time", "string", numeric_scale=3),
     ("TIME(3)", False), ("TIME(3)", False)),
    ("ms-time-7", "sqlserver", _col("time", "string", numeric_scale=7),
     ("TIME(6)", True), ("TIME(6)", True)),
    ("ms-time-ddl", "sqlserver", _col("time(3)", "string", max_length=3),
     ("TIME(3)", False), ("TIME(3)", False)),
    # CLOB/NCLOB/LONG, text/ntext, (n)varchar(max) -> TEXT | LONGTEXT
    ("ora-clob", "oracle", _col("clob", "text", max_length=4000),
     ("TEXT", False), ("LONGTEXT", False)),
    ("ora-nclob", "oracle", _col("nclob", "text"), ("TEXT", False), ("LONGTEXT", False)),
    ("ora-long", "oracle", _col("long", "text"), ("TEXT", False), ("LONGTEXT", False)),
    ("ora-clob-ddl", "oracle", _col("CLOB", None), ("TEXT", False), ("LONGTEXT", False)),
    ("ms-text", "sqlserver", _col("text", "text", max_length=16),
     ("TEXT", False), ("LONGTEXT", False)),
    ("ms-ntext", "sqlserver", _col("ntext", "text", max_length=8),
     ("TEXT", False), ("LONGTEXT", False)),
    ("ms-varchar-max", "sqlserver", _col("varchar", "string", max_length=-1),
     ("TEXT", False), ("LONGTEXT", False)),
    ("ms-nvarchar-max", "sqlserver", _col("nvarchar", "string", max_length=-1),
     ("TEXT", False), ("LONGTEXT", False)),
    # No length collected: may be (max), may be a trimmed collection; flagged (ADR-028 D1).
    ("ms-varchar-no-length", "sqlserver", _col("varchar", "string"),
     ("TEXT", True), ("LONGTEXT", True)),
    ("ms-nvarchar-no-length", "sqlserver", _col("nvarchar", "string"),
     ("TEXT", True), ("LONGTEXT", True)),
    ("ms-nvarchar-max-ddl", "sqlserver", _col("nvarchar(MAX)", "string"),
     ("TEXT", False), ("LONGTEXT", False)),
    ("ms-varchar-max-ddl", "sqlserver", _col("VARCHAR(max)", "string"),
     ("TEXT", False), ("LONGTEXT", False)),
    # BLOB/LONG RAW, image, varbinary(max) -> BYTEA | LONGBLOB
    ("ora-blob", "oracle", _col("blob", "binary", max_length=4000),
     ("BYTEA", False), ("LONGBLOB", False)),
    ("ora-long-raw", "oracle", _col("long raw", "binary"), ("BYTEA", False), ("LONGBLOB", False)),
    ("ora-blob-ddl", "oracle", _col("BLOB", "blob"), ("BYTEA", False), ("LONGBLOB", False)),
    ("ms-image", "sqlserver", _col("image", "binary", max_length=16),
     ("BYTEA", False), ("LONGBLOB", False)),
    ("ms-varbinary-max", "sqlserver", _col("varbinary", "binary", max_length=-1),
     ("BYTEA", False), ("LONGBLOB", False)),
    ("ms-varbinary-no-length", "sqlserver", _col("varbinary", "binary"),
     ("BYTEA", False), ("LONGBLOB", True)),
    ("ms-varbinary-max-ddl", "sqlserver", _col("varbinary(max)", "binary"),
     ("BYTEA", False), ("LONGBLOB", False)),
    # RAW(n), varbinary(n) -> BYTEA | VARBINARY(n)
    ("ora-raw", "oracle", _col("raw", "binary", max_length=16),
     ("BYTEA", False), ("VARBINARY(16)", False)),
    ("ora-raw-ddl", "oracle", _col("RAW(2000)", None, max_length=2000),
     ("BYTEA", False), ("VARBINARY(2000)", False)),
    ("ms-varbinary", "sqlserver", _col("varbinary", "binary", max_length=16),
     ("BYTEA", False), ("VARBINARY(16)", False)),
    ("ms-varbinary-ddl", "sqlserver", _col("varbinary(8000)", "binary", max_length=8000),
     ("BYTEA", False), ("VARBINARY(8000)", False)),
    # Wider than SQL Server's 8000: Aurora MySQL's 65,535-byte row is shared, so off-row.
    ("ora-raw-extended", "oracle", _col("raw", "binary", max_length=32767),
     ("BYTEA", False), ("LONGBLOB", False)),
    ("ora-raw-no-length", "oracle", _col("raw", "binary"), ("BYTEA", False), ("LONGBLOB", True)),
]  # fmt: skip


def _cases(target: str) -> list:
    index = 3 if target == PG else 4
    return [pytest.param(r[1], r[2], *r[index], id=f"{r[0]}-{target}") for r in _ROWS]


@pytest.mark.parametrize(("engine", "column", "expected", "flagged"), _cases(PG))
def test_aurora_postgresql_type(engine, column, expected, flagged):
    col, result = _one(PG, engine, column)
    assert (col.aurora_type, col.needs_judgment) == (expected, flagged)
    assert bool(result.residuals) is flagged
    assert col.script_derived is not flagged


@pytest.mark.parametrize(("engine", "column", "expected", "flagged"), _cases(MY))
def test_aurora_mysql_type(engine, column, expected, flagged):
    col, result = _one(MY, engine, column)
    assert (col.aurora_type, col.needs_judgment) == (expected, flagged)
    assert bool(result.residuals) is flagged
    assert col.script_derived is not flagged


def test_precision_over_six_says_how_many_digits_are_lost():
    col, result = _one(MY, "sqlserver", _col("datetime2", "timestamp", numeric_scale=7))
    assert "keeps 7 fractional-second digits" in col.judgment_reason
    assert "at most 6" in col.judgment_reason
    assert result.residuals[0]["fallback_type"] == "DATETIME(6)"


@pytest.mark.parametrize("target", [PG, MY])
@pytest.mark.parametrize("data_type", ["datetime2", "datetimeoffset", "time"])
def test_uncollected_precision_is_not_stated_as_fact(data_type, target):
    """No digits in the type and none collected: the reason says so, and names the
    source default (7) as what the column has only if it left them unset."""
    col, _ = _one(target, "sqlserver", _col(data_type, "timestamp"))
    assert col.needs_judgment
    assert "were not collected" in col.judgment_reason
    assert "default, if the column left them unset, is 7" in col.judgment_reason
    assert "keeps 7" not in col.judgment_reason


@pytest.mark.parametrize(
    "column",
    [
        _col("datetimeoffset", "timestamp", numeric_scale=3),
        _col("timestamp(6) with time zone", None, numeric_scale=6),
    ],
)
def test_mysql_offset_flag_says_the_offset_is_not_kept(column):
    engine = "sqlserver" if column["data_type"] == "datetimeoffset" else "oracle"
    col, _ = _one(MY, engine, column)
    assert "offset" in col.judgment_reason
    assert "UTC" in col.judgment_reason


def test_offset_and_precision_flags_are_both_given():
    col, _ = _one(MY, "sqlserver", _col("datetimeoffset", "timestamp", numeric_scale=7))
    assert "offset" in col.judgment_reason
    assert "7 fractional-second digits" in col.judgment_reason


@pytest.mark.parametrize(
    ("engine", "data_type", "normalized"),
    [
        ("oracle", "clob", "text"),
        ("oracle", "blob", "binary"),
        ("sqlserver", "nvarchar(MAX)", "string"),
        ("sqlserver", "ntext", "text"),
        ("sqlserver", "image", "binary"),
        ("sqlserver", "nvarchar", "string"),  # no length collected
    ],
)
def test_mysql_lob_in_a_key_is_a_residual_not_error_1170(engine, data_type, normalized):
    """A LOB column in a key: Aurora MySQL cannot index LONGTEXT/LONGBLOB in full (ERROR
    1170), so the draft gives a flagged VARCHAR(255), as for a PostgreSQL text key."""
    key = {**_col(data_type, normalized), "column_name": "k", "nullable": False}
    table = AgentTable.model_validate(
        {"table_id": "db.t", "table_name": "t", "row_count": 1, "columns": [key],
         "primary_key": ["k"]}
    )  # fmt: skip
    result = generate_mysql_ddl([table], source_engine=engine)
    col = result.tables[0].columns[0]
    assert (col.aurora_type, col.needs_judgment) == ("VARCHAR(255)", True)
    assert "cannot be indexed directly" in col.judgment_reason
    assert "LONG" not in result.full_ddl
    # Aurora PostgreSQL indexes TEXT and BYTEA: nothing to flag there.
    pg = generate_pg_ddl([table], source_engine=engine).tables[0].columns[0]
    assert pg.aurora_type in ("TEXT", "BYTEA")
    assert pg.needs_judgment is (data_type == "nvarchar")


@pytest.mark.parametrize(
    ("engine", "data_type", "normalized", "aurora_type"),
    [
        ("oracle", "clob", "text", "LONGTEXT"),
        ("sqlserver", "nvarchar(MAX)", "string", "LONGTEXT"),
        ("sqlserver", "varbinary(max)", "binary", "LONGBLOB"),
    ],
)
def test_mysql_lob_with_a_default_is_a_residual(engine, data_type, normalized, aurora_type):
    """Aurora MySQL takes only an expression DEFAULT on a LOB: flagged, not dropped silently."""
    col, result = _one(MY, engine, _col(data_type, normalized, default_value="'n/a'"))
    assert (col.aurora_type, col.needs_judgment) == (aurora_type, True)
    assert "has a DEFAULT" in col.judgment_reason
    assert [r["column"] for r in result.residuals] == ["c"]


def test_no_length_in_a_key_keeps_both_reasons():
    key = {**_col("nvarchar", "string"), "column_name": "k", "nullable": False}
    table = AgentTable.model_validate(
        {"table_id": "db.t", "table_name": "t", "row_count": 1, "columns": [key],
         "primary_key": ["k"]}
    )  # fmt: skip
    col = generate_mysql_ddl([table], source_engine="sqlserver").tables[0].columns[0]
    assert "no known length" in col.judgment_reason
    assert "cannot be indexed directly" in col.judgment_reason


def test_two_extended_raw_columns_fit_one_mysql_row():
    """Two RAW(32767) columns would need 65,534 bytes in one row (ERROR 1118 with the
    row's other columns): both go off-row as LONGBLOB."""
    raw = [{**_col("raw", "binary", max_length=32767), "column_name": n} for n in ("a", "b")]
    result = generate_mysql_ddl([_table(raw)], source_engine="oracle")
    assert [c.aurora_type for c in result.tables[0].columns] == ["LONGBLOB", "LONGBLOB"]
    assert "VARBINARY" not in result.full_ddl


@pytest.mark.parametrize("target", [PG, MY])
def test_local_time_zone_is_an_instant(target):
    """TIMESTAMP WITH LOCAL TIME ZONE is not in the issue's table; it holds an instant
    (no offset), so it is TIMESTAMPTZ on Aurora PostgreSQL and a UTC DATETIME(p) with
    a time-zone note on Aurora MySQL, as a PostgreSQL timestamptz is."""
    column = _col("timestamp(6) with local time zone", None, numeric_scale=6)
    col, result = _one(target, "oracle", column)
    assert not col.needs_judgment
    if target == PG:
        assert col.aurora_type == "TIMESTAMPTZ"
    else:
        assert col.aurora_type == "DATETIME(6)"
        assert [n["kind"] for n in result.column_notes] == ["time_zone"]


# ---------------------------------------------------------------------------
# What must not change
# ---------------------------------------------------------------------------

# (engine, data_type, normalized, extra, pg type, mysql type), as main resolves them.
_UNCHANGED = [
    ("mysql", "longtext", "text", {}, "TEXT", "LONGTEXT"),
    ("mysql", "text", "text", {}, "TEXT", "TEXT"),
    ("mysql", "datetime", "datetime", {}, "TIMESTAMP", "DATETIME"),
    ("mysql", "datetime(6)", "datetime", {}, "TIMESTAMP(6)", "DATETIME(6)"),
    ("mysql", "timestamp", "timestamp", {}, "TIMESTAMPTZ", "TIMESTAMP"),
    ("mysql", "timestamp(3)", "timestamp", {}, "TIMESTAMPTZ(3)", "TIMESTAMP(3)"),
    ("mysql", "time", "string", {}, "TIME", "TIME"),
    ("mysql", "blob", "blob", {}, "BYTEA", "BLOB"),
    ("mysql", "varbinary(16)", "binary", {"max_length": 16}, "BYTEA", "VARBINARY(16)"),
    ("postgresql", "timestamp with time zone", "timestamp", {}, "TIMESTAMPTZ", "DATETIME(6)"),
    ("postgresql", "timestamptz", "timestamp", {"datetime_precision": 3},
     "TIMESTAMPTZ(3)", "DATETIME(3)"),
    ("postgresql", "timestamp without time zone", "datetime", {}, "TIMESTAMP", "DATETIME(6)"),
    ("postgresql", "text", "text", {}, "TEXT", "LONGTEXT"),
    ("postgresql", "bytea", "binary", {}, "BYTEA", "LONGBLOB"),
    ("postgresql", "time without time zone", "string", {}, "TIME", "TIME(6)"),
    # Sources with no native map keep the shared normalized mapping.
    ("db2", "timestamp", "timestamp", {}, "TIMESTAMPTZ", "TIMESTAMP"),
    ("db2", "clob", "text", {}, "TEXT", "TEXT"),
    ("db2", "blob", "binary", {}, "BYTEA", "BLOB"),
    ("", "date", "timestamp", {}, "TIMESTAMPTZ", "TIMESTAMP"),
    # Oracle and SQL Server types outside the issue's table keep it too.
    ("sqlserver", "nvarchar", "string", {"max_length": 50}, "VARCHAR(50)", "VARCHAR(50)"),
    ("sqlserver", "bit", "boolean", {}, "BOOLEAN", "BOOLEAN"),
    ("sqlserver", "int", "integer", {}, "BIGINT", "BIGINT"),
    ("sqlserver", "date", "date", {}, "DATE", "DATE"),
    ("sqlserver", "money", "decimal", {"numeric_precision": 19, "numeric_scale": 4},
     "NUMERIC(19,4)", "DECIMAL(19,4)"),
    ("sqlserver", "uniqueidentifier", "uuid", {}, "UUID", "CHAR(36)"),
    ("sqlserver", "timestamp", "binary", {"max_length": 8}, "BYTEA", "BLOB"),  # rowversion
    ("oracle", "number", "decimal", {"numeric_precision": 10, "numeric_scale": 0},
     "NUMERIC(10,0)", "DECIMAL(10,0)"),
    ("oracle", "varchar2", "string", {"max_length": 40}, "VARCHAR(40)", "VARCHAR(40)"),
    ("oracle", "bfile", "binary", {}, "BYTEA", "BLOB"),
]  # fmt: skip


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    ("engine", "data_type", "normalized", "extra", "pg_type", "mysql_type"), _UNCHANGED
)
def test_other_sources_and_types_resolve_as_before(
    engine, data_type, normalized, extra, pg_type, mysql_type
):
    column = _col(data_type, normalized, **extra)
    pg, _ = _one(PG, engine, column)
    my, _ = _one(MY, engine, column)
    assert (pg.aurora_type, my.aurora_type) == (pg_type, mysql_type)
    assert not pg.needs_judgment and not my.needs_judgment


# ---------------------------------------------------------------------------
# End to end: collector rows -> AgentTable -> DDL
# ---------------------------------------------------------------------------


def _types(result: DdlResult) -> dict[tuple[str, str], str]:
    return {(t.table_name, c.name): c.aurora_type for t in result.tables for c in t.columns}


def _agent(tables) -> list[AgentTable]:
    return [AgentTable.model_validate(t.model_dump(mode="json")) for t in tables]


@pytest.fixture(scope="module")
def dnn_tables() -> list[AgentTable]:
    """The bundled DNN Platform sample: a SQL Server collection with no customer data
    (docs/examples/dnn/README.md), through the offline builder as the pipeline reads it."""
    from src.agents.collector.mysql_collector import _build_tables
    from src.tools.database.offline_parser import parse_offline_collection

    with zipfile.ZipFile(_ROOT / "docs" / "examples" / "dnn" / "dnn.zip") as zf:
        raw = json.loads(zf.read("dnn-collection.json"))
    return _agent(_build_tables(parse_offline_collection(raw)["tables"], "DNN"))


_DNN = {
    # column: (collected type, Aurora PostgreSQL, Aurora MySQL)
    ("AnonymousUsers", "CreationDate"): ("datetime", "TIMESTAMP(3)", "DATETIME(3)"),
    ("SiteLog", "DateTime"): ("smalldatetime", "TIMESTAMP(0)", "DATETIME"),
    ("aspnet_Membership", "Comment"): ("ntext", "TEXT", "LONGTEXT"),
    ("Files", "Content"): ("image", "BYTEA", "LONGBLOB"),
}
# Offline (max): collect-sqlserver.sql emits no length for -1, which cannot be told
# from a length that was not collected, so the LOB is the fallback and it is flagged.
_DNN_NO_LENGTH = ("ContentItems", "Content")


@pytest.mark.parametrize("target", [PG, MY])
def test_dnn_sample_end_to_end(dnn_tables, target):
    result = _generate(target, dnn_tables, "sqlserver")
    types = _types(result)
    index = 1 if target == PG else 2
    assert {key: types[key] for key in _DNN} == {key: v[index] for key, v in _DNN.items()}
    # Every date/time and large-object column, not only the ones named above: none is
    # left on the old types or for review (the sample has no datetime2, datetimeoffset or
    # time column, so no precision above 6 digits).
    lob_or_time = {"datetime", "smalldatetime", "ntext", "image"}
    in_scope = {
        (t.table_name, c.column_name)
        for t in dnn_tables
        for c in t.columns
        if c.data_type in lob_or_time
    }
    assert len(in_scope) > 300
    forbidden = {"TIMESTAMPTZ"} if target == PG else {"TIMESTAMP", "TEXT", "BLOB"}
    assert not {k: types[k] for k in in_scope if types[k].split("(")[0] in forbidden}
    # Only a LOB with a DEFAULT is left for review on Aurora MySQL (an expression default only).
    defaults = {
        (t.table_name, c.column_name) for t in dnn_tables for c in t.columns if c.default_value
    }
    left = [r for r in result.residuals if (r["table"], r["column"]) in in_scope]
    assert all((r["table"], r["column"]) in defaults for r in left)
    assert all("has a DEFAULT" in r["reason"] for r in left)
    assert bool(left) is (target == MY)
    no_length = {
        (t.table_name, c.column_name)
        for t in dnn_tables
        for c in t.columns
        if c.data_type == "nvarchar" and c.max_length is None
    }
    assert _DNN_NO_LENGTH in no_length
    flagged = {(r["table"], r["column"]): r for r in result.residuals}
    assert types[_DNN_NO_LENGTH] == ("TEXT" if target == PG else "LONGTEXT")
    assert all("may be (max)" in flagged[k]["reason"] for k in no_length if k in flagged)
    assert no_length <= set(flagged)


def _oracle_live_tables() -> list[AgentTable]:
    """Rows as OracleRemoteCollector.collect_columns returns them (ALL_TAB_COLUMNS,
    data_type lower-cased), through the live collector's own builder."""
    from src.agents.collector.oracle_collector import _build_tables

    def row(name, data_type, length, char_length=0, scale=None):
        return {"column_name": name, "data_type": data_type, "max_length": length,
                "char_length": char_length, "data_precision": None, "data_scale": scale,
                "is_nullable": "Y", "is_identity": "NO"}  # fmt: skip

    columns = [
        {**row("id", "number", 22), "data_precision": 10, "data_scale": 0, "is_nullable": "N"},
        row("placed", "date", 7),
        row("at", "timestamp(9)", 11, scale=9),
        row("at_tz", "timestamp(6) with time zone", 13, scale=6),
        row("body", "clob", 4000),
        row("pic", "blob", 4000),
        row("tag", "raw", 16),
        row("name", "varchar2", 400, char_length=100),
    ]
    raw = {"tables": [{"table_name": "orders", "schema_name": "app", "columns": columns,
                       "primary_key": ["id"]}]}  # fmt: skip
    return _agent(_build_tables(raw))


def _oracle_ddl_tables() -> list[AgentTable]:
    """An Oracle DDL upload through the DDL parser and the shared builder."""
    from src.agents.collector.mysql_collector import _build_tables_from_ddl
    from src.tools.database.ddl_parser import parse_ddl

    ddl = """CREATE TABLE orders (
      id NUMBER(10) PRIMARY KEY,
      placed DATE NOT NULL,
      at TIMESTAMP(9),
      body CLOB,
      pic BLOB,
      tag RAW(16),
      name VARCHAR2(100)
    );"""
    return _agent(_build_tables_from_ddl(parse_ddl(ddl, "app", dialect="oracle"), "app"))


_ORACLE = {
    "placed": ("TIMESTAMP(0)", "DATETIME"),
    "at": ("TIMESTAMP(6)", "DATETIME(6)"),
    "body": ("TEXT", "LONGTEXT"),
    "pic": ("BYTEA", "LONGBLOB"),
    "tag": ("BYTEA", "VARBINARY(16)"),
    "name": ("VARCHAR(100)", "VARCHAR(100)"),
}


@pytest.mark.parametrize("target", [PG, MY])
@pytest.mark.parametrize("tables", [_oracle_live_tables, _oracle_ddl_tables], ids=["live", "ddl"])
def test_oracle_collection_end_to_end(tables, target):
    result = _generate(target, tables(), "oracle")
    types = {c: t for (_, c), t in _types(result).items()}
    index = 0 if target == PG else 1
    assert {c: types[c] for c in _ORACLE} == {c: v[index] for c, v in _ORACLE.items()}
    flagged = [r["column"] for r in result.residuals if r["column"] in _ORACLE]
    assert flagged == ["at"]  # TIMESTAMP(9): 3 digits lost
    if "at_tz" in types:  # the DDL parser drops WITH TIME ZONE; the live row keeps it
        assert types["at_tz"] == ("TIMESTAMPTZ" if target == PG else "DATETIME(6)")
        offset_flagged = any(r["column"] == "at_tz" for r in result.residuals)
        assert offset_flagged is (target == MY)


@pytest.mark.parametrize("target", [PG, MY])
def test_sqlserver_live_collection_end_to_end(target):
    """Rows as SQLServerRemoteCollector.collect_columns returns them (sys.columns:
    max_length -1 for a (max) type, the fractional digits in scale)."""
    from src.agents.collector.sqlserver_collector import _build_tables

    def row(name, data_type, length, scale=0):
        return {"column_name": name, "data_type": data_type, "max_length": length,
                "numeric_precision": 0, "numeric_scale": scale, "is_nullable": "YES",
                "is_identity": "NO"}  # fmt: skip

    columns = [
        {**row("id", "int", 4), "is_nullable": "NO", "is_identity": "YES"},
        row("placed", "datetime", 8, scale=3),
        row("at", "datetime2", 8, scale=7),
        row("t", "time", 5, scale=3),
        row("notes", "nvarchar", -1),
        row("pic", "image", 16),
        row("bin", "varbinary", 16),
        row("big", "varbinary", -1),
    ]
    raw = {"tables": [{"table_name": "orders", "schema_name": "dbo", "columns": columns,
                       "primary_key": ["id"]}]}  # fmt: skip
    result = _generate(target, _agent(_build_tables(raw)), "sqlserver")
    types = {c: t for (_, c), t in _types(result).items()}
    expected = {
        PG: {"placed": "TIMESTAMP(3)", "at": "TIMESTAMP(6)", "t": "TIME(3)", "notes": "TEXT",
             "pic": "BYTEA", "bin": "BYTEA", "big": "BYTEA"},
        MY: {"placed": "DATETIME(3)", "at": "DATETIME(6)", "t": "TIME(3)", "notes": "LONGTEXT",
             "pic": "LONGBLOB", "bin": "VARBINARY(16)", "big": "LONGBLOB"},
    }[target]  # fmt: skip
    assert {c: types[c] for c in expected} == expected
    # datetime2(7) loses a digit. The collector turns max_length -1 into no length, so
    # the (max) columns are flagged too (BYTEA on Aurora PostgreSQL needs no length).
    flagged = {PG: ["at", "notes"], MY: ["at", "notes", "big"]}[target]
    assert [r["column"] for r in result.residuals] == flagged


@pytest.mark.parametrize("target", [PG, MY])
def test_adventureworks_trimmed_lengthless_strings_stay_flagged(target):
    """The trimmed AdventureWorks fixture has no max_length on its strings (nvarchar(50)
    and the like in the public sample): each is a residual, never a silent (max) LOB."""
    path = _ROOT / "tests" / "fixtures" / "adventureworks_trimmed_collection.json"
    raw = json.loads(path.read_text())["database_schema"]["tables"]
    # The fixture leaves out nullable and row_count; neither bears on the column type.
    tables = [
        AgentTable.model_validate(
            {**t, "row_count": 1, "columns": [{"nullable": True, **c} for c in t["columns"]]}
        )
        for t in raw
    ]
    result = _generate(target, tables, "sqlserver")
    lengthless = {
        (t.table_name, c.column_name)
        for t in tables
        for c in t.columns
        if c.data_type in ("varchar", "nvarchar") and c.max_length is None
    }
    assert len(lengthless) == 8
    flagged = {(r["table"], r["column"]) for r in result.residuals}
    assert lengthless <= flagged
    types = _types(result)
    assert {types[k] for k in lengthless} == {"TEXT" if target == PG else "LONGTEXT"}
