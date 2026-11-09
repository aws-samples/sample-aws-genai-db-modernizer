# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Run a rule plan on synthetic rows in DuckDB (#1763).

Identifiers are always quoted; every value is a bound parameter. Source columns load
as VARCHAR and every output column is a TRY_CAST to its type: a transform to its
``to_type``, a copy to its ``target_type``. A non-null input whose output is NULL is a
conversion failure.

What the target engine enforces beyond the DuckDB type is a predicate next to the cast
(#1830): a MySQL ``UNSIGNED`` decimal or float rejects negatives; ``CHAR(n)`` and
``VARCHAR(n)`` hold ``n`` characters (excess trailing spaces are cut, not rejected, in
both engines), PostgreSQL pads a ``CHAR(n)`` with spaces and MySQL returns it without
them; MySQL ``BINARY(n)`` and ``VARBINARY(n)`` hold ``n`` bytes (``BINARY`` padded with
zero bytes), and MySQL feeds bytes as the UTF-8 of the text (a PostgreSQL ``bytea``
source, ``SyntheticTable.hex_source_columns``, is decoded from its hex text first); MySQL
``FLOAT(M,D)`` is single precision, ``DOUBLE(M,D)`` double, both rounded to ``D`` places
as MySQL rounds (half to even after floor) and rejected when the rounded value has more
than ``M - D`` integer digits. The check models assignment (an INSERT into the target
column), not an explicit ``CAST``, and assumes ``utf8mb4`` MySQL text columns. A boolean source column
(``SyntheticTable.boolean_source_columns``) reaches Aurora MySQL as 1 or 0.

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
from typing import Literal

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


_CHARS = {"char", "character", "nchar", "national char"}  # fixed length, one when bare
_VARCHARS = {"varchar", "character varying", "nvarchar", "national varchar"}
_FLOATS = {"float", "double", "double precision", "real", "float4", "float8"}


@dataclass(frozen=True)
class _Target:
    """How a target type is checked: a DuckDB cast, then what the engine enforces on top."""

    type: str
    unsigned: bool = False  # a MySQL UNSIGNED decimal or float: no negative value
    chars: int | None = None  # CHAR(n) / VARCHAR(n): at most n characters
    pad: Literal["spaces", "trim"] | None = None  # CHAR(n): PostgreSQL pads, MySQL trims
    max_bytes: int | None = None  # MySQL BINARY(n) / VARBINARY(n): at most n bytes
    zero_pad: bool = False  # MySQL BINARY(n): padded with zero bytes to n
    digits: tuple[int, int] | None = None  # MySQL FLOAT(M,D) / DOUBLE(M,D)


def _size(paren: re.Match[str] | None, target_type: str) -> int | None:
    if paren is None:
        return None
    if not re.fullmatch(r"\s*\d+\s*", paren.group(1)):
        raise UnknownTargetType(target_type)
    n = int(paren.group(1))
    if n < 1:
        raise UnknownTargetType(target_type)
    return n


def _float(target_type: str, word: str, paren: re.Match[str] | None, mysql: bool,
           unsigned: bool) -> _Target:  # fmt: skip
    sizes = [] if paren is None else [p.strip() for p in paren.group(1).split(",")]
    if not all(p.isdigit() for p in sizes) or len(sizes) > 2:
        raise UnknownTargetType(target_type)
    if len(sizes) == 2:  # MySQL FLOAT(M,D) / DOUBLE(M,D); PostgreSQL has no such form
        m, d = int(sizes[0]), int(sizes[1])
        if not mysql or not (1 <= m <= 255 and d <= 30 and d <= m):
            raise UnknownTargetType(target_type)
        return _Target("FLOAT" if word == "float" else "DOUBLE", unsigned, digits=(m, d))
    if sizes and word != "float":
        raise UnknownTargetType(target_type)  # DOUBLE(p), REAL(p): no such type
    if word == "float":
        if not sizes:
            return _Target("FLOAT" if mysql else "DOUBLE", unsigned)
        return _Target("FLOAT" if int(sizes[0]) <= 24 else "DOUBLE", unsigned)
    if word == "real" and mysql:
        return _Target("DOUBLE", unsigned)  # MySQL's REAL is DOUBLE
    return _Target(_SIMPLE[word], unsigned)


def _target(target_type: str, engine: str) -> _Target:
    if engine not in _ENGINES:
        raise ValueError(f"unknown target engine: {engine!r}")
    mysql = engine == "aurora_mysql"
    t = " ".join(target_type.lower().split())
    if re.fullmatch(r"tinyint\(1\)", t) and mysql:
        return _Target("TINYINT")
    if t in ("boolean", "bool"):
        return _Target("TINYINT" if mysql else "BOOLEAN")
    paren = re.search(r"\((.*?)\)", t)
    base = " ".join(re.sub(r"\(.*?\)", " ", t).split())
    if base.startswith("interval") and not mysql:
        return _Target("INTERVAL")
    words = [w for w in base.split() if w not in _MODIFIERS]
    mods = {w for w in base.split() if w in _MODIFIERS}
    word = " ".join(words)
    if (mods and not mysql) or (word in _MYSQL_ONLY and not mysql) or (word in _PG_ONLY and mysql):
        raise UnknownTargetType(target_type)
    unsigned = bool(mods & {"unsigned", "zerofill"})
    if word in _INTS:
        return _Target(_INTS[word][1 if unsigned else 0])
    if word in ("decimal", "numeric", "dec", "fixed"):
        plain = re.sub(r"\s+(unsigned|signed|zerofill)", "", t)
        return _Target(_decimal(target_type, plain, engine), unsigned)
    if word in _FLOATS:
        return _float(target_type, word, paren, mysql, unsigned)
    if unsigned and word not in _TEXT:
        raise UnknownTargetType(target_type)  # an unsigned date, ...: no such type
    if word in _SIMPLE:
        return _Target(_SIMPLE[word])
    if word in _CHARS:
        return _Target(
            "VARCHAR", chars=_size(paren, target_type) or 1, pad="trim" if mysql else "spaces"
        )
    if word in _VARCHARS:
        return _Target("VARCHAR", chars=_size(paren, target_type))
    if word in _TEXT:
        return _Target("VARCHAR")
    if word in ("binary", "varbinary") and mysql:
        size = _size(paren, target_type)
        if word == "binary":
            return _Target("BLOB", max_bytes=size or 1, zero_pad=True)
        return _Target("BLOB", max_bytes=size)
    if word in _BLOB:
        return _Target("BLOB")
    raise UnknownTargetType(target_type)


def duckdb_type(target_type: str, engine: str) -> str:
    """The DuckDB type that predicts what the target engine accepts.

    Raises ``UnknownTargetType`` when no DuckDB type is faithful (callers treat the
    column as unchecked).
    """
    return _target(target_type, engine).type


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _checked_type(c: OutputColumn, engine: str) -> _Target | None:
    """How a column's cast is checked, or None if the column is unchecked."""
    try:
        return _target(c.cast_to or c.target_type, engine)
    except UnknownTargetType:
        return None


def _expr(src: str, target: _Target, engine: str, hex_source: bool = False) -> str:
    """The checked conversion of quoted source column ``src``: NULL when it fails.

    Only quoted identifiers, integers parsed from the type and floats computed from them
    reach the SQL text. ``hex_source``: ``src`` holds PostgreSQL hex text (``\\x0a``).
    """
    number = f"TRY_CAST({src} AS DOUBLE)"
    if target.digits is not None:
        # MySQL's Field_real::truncate: the fraction rounded half to even after floor,
        # then the range check against 10^(M-D) - 10^-D.
        m, d = target.digits
        limit = repr(10.0 ** (m - d) - 1.0 / 10.0**d)
        rounded = f"(floor({number}) + round_even(({number} - floor({number})) * 1e{d}, 0) / 1e{d})"
        value = f"CASE WHEN abs({rounded}) <= {limit} THEN TRY_CAST({rounded} AS {target.type}) END"
    elif target.type == "BLOB" and hex_source:
        value = f"CASE WHEN starts_with({src}, '\\x') THEN try(from_hex(substr({src}, 3))) END"
    elif target.type == "BLOB" and engine == "aurora_mysql":
        value = f"encode({src})"  # MySQL bytes: the UTF-8 of the text
    else:
        value = f"TRY_CAST({src} AS {target.type})"
    if target.unsigned:  # MySQL tests the sign before it rounds: -0.001 fails
        value = f"CASE WHEN {number} >= 0 THEN {value} END"
    if target.chars is not None:
        n = target.chars
        cut = f"left({src}, {n})"  # only spaces are cut: longer text fails the length test
        if target.pad == "spaces":
            cut = f"rpad({cut}, {n}, ' ')"
        elif target.pad == "trim":
            cut = f"rtrim({cut}, ' ')"
        value = f"CASE WHEN length(rtrim({src}, ' ')) <= {n} THEN {cut} END"
    if target.max_bytes is not None:
        n = target.max_bytes
        padded = f"{value} || repeat('\\x00'::BLOB, {n} - octet_length({value}))"
        value = f"CASE WHEN octet_length({value}) <= {n} THEN {padded if target.zero_pad else value} END"
    return value


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
    plan: RulePlan,
    source_relation: str,
    engine: str,
    unchecked_sources: Collection[str] = (),
    hex_sources: Collection[str] = (),
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
        target = _checked_type(c, engine)
        expr = (
            src
            if c.target_column in skip or target is None
            else _expr(src, target, engine, c.source_column in hex_sources)
        )
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


_TRUE = frozenset({"t", "true", "y", "yes", "on", "1"})
_FALSE = frozenset({"f", "false", "n", "no", "off", "0"})


def _as_mysql_boolean(text: str | None) -> str | None:
    """A boolean source value as Aurora MySQL receives it: 1 or 0 (other text unchanged)."""
    if text is None:
        return None
    word = text.strip().lower()
    return "1" if word in _TRUE else "0" if word in _FALSE else text


def run_rule(
    plan: RulePlan, table: SyntheticTable, *, source_columns: list[str], engine: str
) -> RuleRun:
    if table.table_id != plan.source_table:
        raise ValueError(
            f"table {table.table_id!r} is not the rule's source table {plan.source_table!r}"
        )
    sql = to_sql(plan, "src", engine, table.unchecked_source_columns, table.hex_source_columns)
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
        booleans = set(table.boolean_source_columns) if engine == "aurora_mysql" else set()
        values = [
            [
                _as_mysql_boolean(_as_text(r.get(c))) if c in booleans else _as_text(r.get(c))
                for c in source_columns
            ]
            for r in table.rows
        ]
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
