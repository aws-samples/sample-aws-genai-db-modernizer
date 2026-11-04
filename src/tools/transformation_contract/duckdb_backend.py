# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Run a rule plan on synthetic rows in DuckDB (#1763).

Identifiers are always quoted; every value is a bound parameter. Source columns load
as VARCHAR and every output column is a TRY_CAST to its type: a transform to its
``to_type``, a copy to its ``target_type``. A non-null input whose output is NULL is a
conversion failure.

A column is unchecked, not failed, when no DuckDB type answers like the target engine
(``duckdb_type`` raises ``UnknownTargetType``) or when the generator could not model
its source column (``SyntheticTable.unchecked_source_columns``). It passes through as
text, the rest of the rule is still checked, and ``RuleRun.unchecked_columns`` lists it.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass
from decimal import Decimal

import duckdb

from src.tools.synthetic_data.generator import SyntheticTable
from src.tools.transformation_contract.plan_ir import OutputColumn, RulePlan

_ENGINES = ("aurora_mysql", "aurora_postgresql")


class UnknownTargetType(ValueError):
    """A target type the DuckDB backend has no faithful mapping for."""


_INTS = {"tinyint": ("TINYINT", "UTINYINT"), "smallint": ("SMALLINT", "USMALLINT"),
         "int2": ("SMALLINT", "USMALLINT"), "smallserial": ("SMALLINT", "USMALLINT"),
         "serial2": ("SMALLINT", "USMALLINT"),
         "mediumint": ("INTEGER", "UINTEGER"), "int": ("INTEGER", "UINTEGER"),
         "integer": ("INTEGER", "UINTEGER"), "int4": ("INTEGER", "UINTEGER"),
         "serial": ("INTEGER", "UINTEGER"), "serial4": ("INTEGER", "UINTEGER"),
         "bigint": ("BIGINT", "UBIGINT"), "int8": ("BIGINT", "UBIGINT"),
         "bigserial": ("BIGINT", "UBIGINT"), "serial8": ("BIGINT", "UBIGINT")}  # fmt: skip
_TEXT = ("char", "varchar", "text", "mediumtext", "longtext", "tinytext", "character varying",
         "character", "citext", "enum", "set", "nchar", "nvarchar", "national char",
         "national varchar")  # fmt: skip
_BLOB = ("blob", "longblob", "mediumblob", "tinyblob", "bytea", "varbinary", "binary")
_SIMPLE = {"date": "DATE", "datetime": "TIMESTAMP", "timestamp": "TIMESTAMP",
           "timestamp without time zone": "TIMESTAMP", "timestamptz": "TIMESTAMPTZ",
           "timestamp with time zone": "TIMESTAMPTZ", "time": "TIME",
           "time without time zone": "TIME", "timetz": "TIMETZ",
           "time with time zone": "TIMETZ", "uuid": "UUID", "json": "JSON", "jsonb": "JSON",
           "real": "FLOAT", "float4": "FLOAT", "float8": "DOUBLE",
           "double": "DOUBLE", "double precision": "DOUBLE"}  # fmt: skip
# Spellings only one engine accepts; the other engine's DDL cannot hold them, so they
# are unchecked there rather than checked against the wrong engine.
_MYSQL_ONLY = (
    frozenset("""tinyint mediumint datetime year tinytext mediumtext longtext tinyblob mediumblob
    longblob blob binary varbinary double enum set nchar nvarchar fixed dec""".split())
    | {"national char", "national varchar"}
)
_PG_ONLY = frozenset(
    """timestamptz timetz interval uuid bytea citext jsonb int2 int4 int8 float4 float8 serial
    serial2 serial4 serial8 smallserial bigserial money""".split()
) | {"character varying", "timestamp with time zone", "time with time zone",
     "timestamp without time zone", "time without time zone"}  # fmt: skip
_MODIFIERS = ("unsigned", "signed", "zerofill")


def _decimal(target_type: str, t: str, engine: str) -> str:
    m = re.fullmatch(r"(?:decimal|numeric|dec|fixed)(?:\s*\((\d+)(?:\s*,\s*(\d+))?\))?", t)
    if m is None:
        raise UnknownTargetType(target_type)
    if m.group(1) is None:
        if engine == "aurora_postgresql":  # unconstrained numeric: arbitrary precision
            raise UnknownTargetType(target_type)
        p, s = 10, 0  # MySQL's default
    else:
        p, s = int(m.group(1)), int(m.group(2) or 0)
    if p < 1 or p > 38 or s > p:
        raise UnknownTargetType(target_type)
    return f"DECIMAL({p},{s})"


def duckdb_type(target_type: str, engine: str) -> str:
    """The DuckDB type that predicts what the target engine accepts.

    Raises ``UnknownTargetType`` when no DuckDB type is faithful (callers treat the
    column as unchecked).
    """
    if engine not in _ENGINES:
        raise ValueError(f"unknown target engine: {engine!r}")
    mysql = engine == "aurora_mysql"
    t = " ".join(target_type.lower().split())
    if re.fullmatch(r"tinyint\(1\)", t) and mysql:
        return "TINYINT"
    if t in ("boolean", "bool"):
        return "TINYINT" if mysql else "BOOLEAN"
    paren = re.search(r"\((.*?)\)", t)
    base = " ".join(re.sub(r"\(.*?\)", " ", t).split())
    if base.startswith("interval") and not mysql:
        return "INTERVAL"
    words = [w for w in base.split() if w not in _MODIFIERS]
    mods = {w for w in base.split() if w in _MODIFIERS}
    word = " ".join(words)
    if (mods and not mysql) or (word in _MYSQL_ONLY and not mysql) or (word in _PG_ONLY and mysql):
        raise UnknownTargetType(target_type)
    unsigned = bool(mods & {"unsigned", "zerofill"})
    if word in _INTS:
        return _INTS[word][1 if unsigned else 0]
    if unsigned and word not in _TEXT:
        raise UnknownTargetType(target_type)  # unsigned decimals and floats: no DuckDB twin
    if word in ("decimal", "numeric", "dec", "fixed"):
        return _decimal(target_type, re.sub(r"\s+(unsigned|signed|zerofill)", "", t), engine)
    if word == "float":
        first = int(paren.group(1).split(",")[0]) if paren else None
        if first is None:
            return "FLOAT" if mysql else "DOUBLE"
        return "FLOAT" if first <= 24 else "DOUBLE"
    if word == "real" and mysql:
        return "DOUBLE"
    if word in _SIMPLE:
        return _SIMPLE[word]
    if word in _TEXT:
        return "VARCHAR"
    if word in _BLOB:
        return "BLOB"
    raise UnknownTargetType(target_type)


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _checked_type(c: OutputColumn, engine: str) -> str | None:
    """The DuckDB type of a column's cast, or None if the column is unchecked."""
    try:
        return duckdb_type(c.cast_to or c.target_type, engine)
    except UnknownTargetType:
        return None


def unchecked_columns(
    plan: RulePlan, engine: str, unchecked_sources: Collection[str] = ()
) -> list[str]:
    """Target columns no check can answer for, in plan order."""
    return [
        c.target_column
        for c in plan.columns
        if c.source_column in unchecked_sources or _checked_type(c, engine) is None
    ]


def to_sql(
    plan: RulePlan, source_relation: str, engine: str, unchecked_sources: Collection[str] = ()
) -> str:
    skip = set(unchecked_columns(plan, engine, unchecked_sources))
    names = [c.target_column for c in plan.columns]
    for name in dict.fromkeys(names):
        if names.count(name) > 1:
            raise ValueError(f"two plan columns write target column {name!r}")
    items = []
    for c in plan.columns:
        if c.source_column is None:
            raise ValueError(f"column {c.target_column!r} has no source column")
        src = quote_ident(c.source_column)
        expr = src if c.target_column in skip else f"TRY_CAST({src} AS {_checked_type(c, engine)})"
        items.append(f"{expr} AS {quote_ident(c.target_column)}")
    return f"SELECT {', '.join(items)} FROM {quote_ident(source_relation)}"  # nosec B608 -- quoted ids, mapped types


@dataclass(frozen=True)
class CastFailure:
    target_column: str
    value: object
    to_type: str


@dataclass(frozen=True)
class RuleRun:
    rows: list[dict[str, object]]
    cast_failures: list[CastFailure]
    unchecked_columns: list[str]  # target columns not checked, in plan order


def _as_text(value: object) -> str | None:
    """How an input value is fed to the backend: plain text, never an exponent."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def run_rule(
    plan: RulePlan, table: SyntheticTable, *, source_columns: list[str], engine: str
) -> RuleRun:
    if table.table_id != plan.source_table:
        raise ValueError(
            f"table {table.table_id!r} is not the rule's source table {plan.source_table!r}"
        )
    sql = to_sql(plan, "src", engine, table.unchecked_source_columns)
    for c in plan.columns:
        if c.source_column not in source_columns:
            raise ValueError(
                f"column {c.target_column!r} reads {c.source_column!r}, not in source_columns"
            )
    unchecked = unchecked_columns(plan, engine, table.unchecked_source_columns)
    con = duckdb.connect(":memory:")
    try:
        con.execute(  # nosemgrep: formatted-sql-query, sqlalchemy-execute-raw-query -- constant table name, every column through quote_ident, in-memory DuckDB
            f"CREATE TABLE src ({', '.join(f'{quote_ident(c)} VARCHAR' for c in source_columns)})"
        )
        values = [[_as_text(r.get(c)) for c in source_columns] for r in table.rows]
        if values:
            con.executemany(
                f"INSERT INTO src VALUES ({', '.join('?' * len(source_columns))})",  # nosec B608 -- placeholders only
                values,
            )
        cur = con.execute(sql)
        names = [d[0] for d in cur.description]
        out = [dict(zip(names, row, strict=True)) for row in cur.fetchall()]
    finally:
        con.close()
    failures = [
        CastFailure(c.target_column, src.get(c.source_column or ""), c.cast_to or c.target_type)
        for src, row in zip(table.rows, out, strict=True)
        for c in plan.columns
        if c.target_column not in unchecked
        and src.get(c.source_column or "") is not None
        and row[c.target_column] is None
    ]
    return RuleRun(out, failures, unchecked)
