# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1's read-only comparison scripts (#1998): generated here, run by the customer.

The modernizer never connects to the customer's databases (the maintainer decision of
2026-11-09 on #1998). ``scripts/run_dms_step.py verify-scripts`` writes three files into
``data-movement/step-1/``, from the step contract alone:

- ``verify-source.sql`` for the source and ``verify-target.sql`` for Aurora: for each
  table step 1 moves (:func:`~src.tools.data_movement.dms_step.contract_tables`), in its
  own ``READ ONLY`` transaction, the row count and an order-independent checksum: the sum
  of the first and of the next 60 bits of each row's SHA-256. A row's hash is the SHA-256 of
  its columns' own SHA-256 digests (each of a canonical text, or of the bytes of a binary
  column; ``N`` for NULL), joined: no intermediate value grows with the row's size, so a
  large LOB never meets MySQL's ``max_allowed_packet``. Every row is hashed whole, so a
  table without a key (which DMS validation cannot check) is compared as fully as one with
  a key; the sums keep a duplicate row (an XOR would cancel it). Each table line also counts
  the rows whose hash came out NULL (a canonical value the server could not build): any such
  row makes the table ``not_compared``, never ``match``.
- ``verify.md``: how to run them, twice each, with a read-only session, and what each
  result means.

Each script prints a strict tab-separated file: a header line (format, side, engine, the
SHA-256 of the step contract and the server's UTC time when the run started, which ties a
second run to the first), one line per table (its position in the contract's table list,
the row count, the NULL-hash count and the two sums: never a table name or a value), and an
end line. A table whose query fails (it is missing, or the user may not read it) prints no
line, and the script goes on to the end line; ``verify`` reports that table ``missing``.

Canonical text, the same on the source and on Aurora of one family (PostgreSQL and Aurora
PostgreSQL, MySQL and Aurora MySQL), chosen per column from the contract's target type:

- NULL is ``N``; any other value is the SHA-256 of its canonical text in UTF-8 (of its
  bytes for a binary column), so NULL, an empty string and a separator never collide.
- numbers: their text, a decimal's trailing fractional zeros dropped (``1.50`` and
  ``1.500000`` are one value: a scale the copy changed is not a difference); PostgreSQL
  ``money`` through ``numeric`` (no locale).
- floats: PostgreSQL's IEEE bytes in hex (``float4send``/``float8send``: the same on every
  version, whatever ``extra_float_digits`` says); MySQL's shortest round-trip text. A zero
  of either sign is ``0``.
- booleans ``t`` or ``f``; MySQL ``BIT`` in hex.
- dates and times: ISO text with six fractional digits; PostgreSQL ``timestamptz`` in UTC
  and an era marker (``AD``/``BC``), ``infinity`` as is; MySQL with ``time_zone`` set to
  UTC for ``TIMESTAMP``.
- binary (``bytea``, ``BINARY``, ``VARBINARY``, ``BLOB``, MySQL spatial types): the bytes.
- JSON: PostgreSQL ``json`` and ``jsonb`` through ``jsonb`` (key order and whitespace
  dropped); MySQL ``JSON``'s own normalised text (a MariaDB source stores JSON as text, so
  it is compared as stored).
- PostgreSQL float arrays element by element (each float's bytes, with the array's
  dimensions), so PostgreSQL 11 and 12+ agree; other arrays and every other type (``interval``, ``uuid``, enums, ``inet``, ...):
  their text under pinned session settings (``TimeZone`` UTC, ``DateStyle`` ISO,
  ``IntervalStyle`` postgres, ``extra_float_digits`` 3, ``bytea_output`` hex). MySQL text
  types converted to ``utf8mb4``, so a table whose character set changed still compares.

Names reach the scripts only as quoted identifiers; a table or column whose name holds a
control or separator character is left out (``not_checked``), never written. Cost: each
query reads its whole table once (a full scan), one table at a time.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from src.contracts.lineage_ops import UnsupportedOp
from src.contracts.transformation_contract import Rule, StepContract
from src.tools.data_movement.dms_step import contract_tables
from src.tools.data_movement.source_retention import FAMILY, family_of

__all__ = [
    "END",
    "FIELDS",
    "FORMAT",
    "INSTRUCTIONS_FILE",
    "MAGIC",
    "OUTPUTS",
    "SCRIPTS",
    "VerifyScriptError",
    "VerifyScripts",
    "VerifyTable",
    "render_script",
    "verify_tables",
    "write_verify_scripts",
]

Side = Literal["source", "target"]
MAGIC = "modernizer-verify"
END = "modernizer-verify-end"
FORMAT = "1"
# header: magic, format, side, engine, contract SHA-256, run start (UTC);
# table: "table", n, rows, NULL-hash rows, a, b
FIELDS = 6
SCRIPTS: dict[Side, str] = {"source": "verify-source.sql", "target": "verify-target.sql"}
#: The output files ``verify`` reads, by side and pass (the second pass is optional).
OUTPUTS: dict[tuple[Side, int], str] = {
    ("source", 1): "verify-source.tsv",
    ("target", 1): "verify-target.tsv",
    ("source", 2): "verify-source-2.tsv",
    ("target", 2): "verify-target-2.tsv",
}
INSTRUCTIONS_FILE = "verify.md"
_BAD_CATEGORIES = {"Cc", "Cf", "Zl", "Zp", "Cs", "Co", "Cn"}


class VerifyScriptError(ValueError):
    """The scripts cannot be generated for this step."""


@dataclass(frozen=True)
class VerifyTable:
    """One moved table as both scripts read it: its position (``n``, from 1, in the
    contract's sorted table list) and its columns, (source column, target column, type)."""

    n: int
    table_id: str
    schema: str
    name: str
    target_name: str
    keyed: bool
    columns: tuple[tuple[str, str, str], ...]


@dataclass
class VerifyScripts:
    """What :func:`write_verify_scripts` wrote: counts and paths, no value."""

    tables: int = 0
    columns: int = 0
    not_checked: list[tuple[str, str]] = field(default_factory=list)  # table id, reason
    files: dict[str, str] = field(default_factory=dict)


def _safe(name: str) -> bool:
    return bool(name) and not any(unicodedata.category(c) in _BAD_CATEGORIES for c in name)


def family(contract: StepContract) -> str:
    """``postgresql`` or ``mysql``: the family both sides of step 1 share."""
    engine = contract.target.engine
    found = engine.removeprefix("aurora_")
    if found == engine or found not in set(FAMILY.values()):
        raise VerifyScriptError(
            f"verify compares a source with Aurora of the same family; the step's target is "
            f"{contract.target.engine}"
        )
    return found


def _columns(rule: Rule) -> tuple[tuple[str, str, str], ...]:
    """The rule's copied (or cast) columns, by target column: an unsupported field has no
    value the copy is known to carry."""
    return tuple(
        sorted(
            (f.source_column, f.target_column, f.target_type)
            for f in rule.fields
            if not isinstance(f.op, UnsupportedOp)
        )
    )


def verify_tables(contract: StepContract) -> tuple[list[VerifyTable], list[tuple[str, str]]]:
    """The tables both scripts read, numbered from 1, and the ones left out (table id,
    reason): a table or column name that holds a control or separator character."""
    rules = {r.source_table: r for r in contract.rules}
    out: list[VerifyTable] = []
    left: list[tuple[str, str]] = []
    for n, t in enumerate(contract_tables(contract), start=1):
        rule = rules[t.table_id]
        columns = _columns(rule)
        names = [t.schema, t.name, rule.target_table, *(c for s, d, _ in columns for c in (s, d))]
        if not all(_safe(x) for x in names):
            left.append((t.table_id, "unsafe_name"))
            continue
        out.append(VerifyTable(n=n, table_id=t.table_id, schema=t.schema, name=t.name,
                               target_name=rule.target_table, keyed=t.keyed,
                               columns=columns))  # fmt: skip
    return out, left


# --------------------------------------------------------------------------- canonical text


def _base(target_type: str) -> str:
    text = " ".join(target_type.strip().lower().split())
    text = re.sub(r"\([^)]*\)", "", text)  # lengths, precisions and scales
    return " ".join(text.replace(" unsigned", "").replace(" zerofill", "").split())


def _pg_text(column: str, target_type: str) -> str:
    """PostgreSQL's canonical text of a non-NULL ``column`` (an already quoted name)."""
    raw = target_type.strip().lower()
    base = _base(target_type)
    c = column
    if raw.endswith("]"):
        element = re.sub(r"(\[\d*\])+$", "", base).strip()
        send = _pg_float_send(element, target_type)
        if send is None:
            return f"{c}::text"
        each = (f"CASE WHEN u_v1998.e_v1998 IS NULL THEN 'N' WHEN u_v1998.e_v1998 = 0 THEN '0' "
                f"ELSE encode({send}(u_v1998.e_v1998), 'hex') END")  # fmt: skip
        return (f"COALESCE(array_dims({c}), '') || '=' || COALESCE((SELECT string_agg({each}, ',' "  # nosec B608 -- a script the customer runs; c is a quoted identifier of a checked name
                f"ORDER BY u_v1998.o_v1998) FROM unnest({c}) WITH ORDINALITY AS "
                f"u_v1998(e_v1998, o_v1998)), '')")  # fmt: skip
    if base in ("boolean", "bool"):
        return f"CASE WHEN {c} THEN 't' ELSE 'f' END"
    if base in ("numeric", "decimal", "money"):
        t = f"{c}::numeric::text" if base == "money" else f"{c}::text"
        return f"CASE WHEN strpos({t}, '.') > 0 THEN rtrim(rtrim({t}, '0'), '.') ELSE {t} END"
    send = _pg_float_send(base, target_type)
    if send is not None:
        return f"CASE WHEN {c} = 0 THEN '0' ELSE encode({send}({c}), 'hex') END"
    if base in ("timestamptz", "timestamp with time zone"):
        return (f"CASE WHEN isfinite({c}) THEN to_char({c} AT TIME ZONE 'UTC', "
                f"'YYYY-MM-DD HH24:MI:SS.US BC') ELSE {c}::text END")  # fmt: skip
    if base in ("timestamp", "timestamp without time zone"):
        return (f"CASE WHEN isfinite({c}) THEN to_char({c}, 'YYYY-MM-DD HH24:MI:SS.US BC') "
                f"ELSE {c}::text END")  # fmt: skip
    if base == "date":
        return f"CASE WHEN isfinite({c}) THEN to_char({c}, 'YYYY-MM-DD BC') ELSE {c}::text END"
    if base in ("time", "time without time zone"):
        return f"to_char({c}::interval, 'HH24:MI:SS.US')"
    if base == "bytea":
        return f"encode({c}, 'hex')"
    if base in ("json", "jsonb"):
        return f"{c}::jsonb::text"
    return f"{c}::text"


def _pg_float_send(base: str, target_type: str) -> str | None:
    """``float4send`` or ``float8send`` for a PostgreSQL float type, else None."""
    if base in ("real", "float4") or (base == "float" and _float_bits(target_type) <= 24):
        return "float4send"
    if base in ("double precision", "float8", "float"):
        return "float8send"
    return None


def _float_bits(target_type: str) -> int:
    match = re.search(r"\(\s*(\d+)\s*\)", target_type)
    return int(match.group(1)) if match else 53


_MYSQL_TEXT = {"char", "varchar", "tinytext", "text", "mediumtext", "longtext", "enum", "set",
               "nchar", "nvarchar", "national char", "national varchar", "character",
               "character varying"}  # fmt: skip
_MYSQL_BINARY = {"binary", "varbinary", "tinyblob", "blob", "mediumblob", "longblob", "geometry",
                 "point", "linestring", "polygon", "multipoint", "multilinestring", "multipolygon",
                 "geometrycollection", "geomcollection"}  # fmt: skip


def _mysql_text(column: str, target_type: str) -> str:
    """MySQL's canonical text of a non-NULL ``column`` (an already quoted name)."""
    base = _base(target_type)
    first = base.split(" ")[0] if base else base
    c = column
    if first in ("decimal", "numeric", "dec", "fixed"):
        t = f"CAST({c} AS CHAR)"
        return (f"CASE WHEN LOCATE('.', {t}) > 0 THEN TRIM(TRAILING '.' FROM TRIM(TRAILING '0' "
                f"FROM {t})) ELSE {t} END")  # fmt: skip
    if first in ("float", "double", "real"):
        return f"CASE WHEN {c} = 0 THEN '0' ELSE CAST({c} AS CHAR) END"
    if first == "bit":
        return f"LOWER(HEX({c}))"
    if first == "date":
        return f"DATE_FORMAT({c}, '%Y-%m-%d')"
    if first in ("datetime", "timestamp"):
        return f"DATE_FORMAT({c}, '%Y-%m-%d %H:%i:%s.%f')"
    if first == "time":
        return f"TIME_FORMAT({c}, '%H:%i:%s.%f')"
    if base in _MYSQL_TEXT or first in _MYSQL_TEXT:
        return f"CONVERT({c} USING utf8mb4)"
    if first in _MYSQL_BINARY:
        return f"LOWER(HEX({c}))"
    if first == "json":
        return f"CAST({c} AS CHAR)"
    return f"CONVERT(CAST({c} AS CHAR) USING utf8mb4)"


def _pg_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _mysql_ident(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def _pg_literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _mysql_literal(text: str) -> str:
    return "'" + text.replace("\\", "\\\\").replace("'", "''") + "'"


def _comment(text: str) -> str:
    """``text`` for a ``--`` comment line: control characters and backslashes (a client
    meta-command such as ``\\!``) are ``?``."""
    return "".join(
        "?" if c == "\\" or unicodedata.category(c) in _BAD_CATEGORIES else c for c in text
    )


_ROW = "verify_row_1998"  # the table's alias: an outer column is never taken for an inner name


def _pg_digest(column: str, target_type: str) -> str:
    """A column's 64-hex SHA-256 (``N`` for NULL): of its bytes for ``bytea``, else of its
    canonical text in UTF-8. Fixed size, whatever the value's size."""
    c = f"{_ROW}.{column}"
    if _base(target_type) == "bytea":
        return f"CASE WHEN {c} IS NULL THEN 'N' ELSE encode(sha256({c}), 'hex') END"
    t = _pg_text(c, target_type)
    return f"CASE WHEN {c} IS NULL THEN 'N' ELSE encode(sha256(convert_to({t}, 'UTF8')), 'hex') END"


def _mysql_digest(column: str, target_type: str) -> str:
    """As :func:`_pg_digest`: ``SHA2`` of a binary column's bytes, else of its canonical
    text in ``utf8mb4``. A value the server cannot build (over ``max_allowed_packet``) is
    NULL, so the row's hash is NULL and the table line counts it."""
    first = (_base(target_type).split(" ") or [""])[0]
    if first in _MYSQL_BINARY:
        return f"CASE WHEN {column} IS NULL THEN 'N' ELSE SHA2({column}, 256) END"
    t = _mysql_text(column, target_type)
    return f"CASE WHEN {column} IS NULL THEN 'N' ELSE SHA2({t}, 256) END"


def _pg_query(table: VerifyTable, side: Side) -> str:
    parts = [_pg_digest(_pg_ident(s if side == "source" else d), t) for s, d, t in table.columns]
    row = " || '|' || ".join(parts) if parts else "''"
    name = table.name if side == "source" else table.target_name
    relation = f"{_pg_ident(table.schema)}.{_pg_ident(name)}"
    sums = ", ".join(
        f"COALESCE(sum(('x' || substr(h, {start}, 15))::bit(60)::bigint), 0)::text"
        for start in (1, 16)
    )
    return (
        f"SELECT 'table', '{table.n}', count(*)::text, (count(*) - count(h))::text, {sums}\n"  # nosec B608 -- a script the customer runs; every name is a quoted identifier of a checked name
        f"FROM (SELECT encode(sha256(convert_to({row}, 'UTF8')), 'hex') AS h "
        f"FROM {relation} AS {_ROW}) AS r;"
    )


def _mysql_query(table: VerifyTable, side: Side, database: str | None) -> str:
    parts = [
        _mysql_digest(_mysql_ident(s if side == "source" else d), t) for s, d, t in table.columns
    ]
    row = "CONCAT(" + ", '|', ".join(parts) + ")" if parts else "''"
    schema = table.schema if side == "source" or not database else database
    name = table.name if side == "source" else table.target_name
    relation = f"{_mysql_ident(schema)}.{_mysql_ident(name)}"
    sums = ", ".join(
        f"CAST(COALESCE(SUM(CAST(CONV(SUBSTRING(h, {start}, 15), 16, 10) AS UNSIGNED)), 0) AS CHAR)"
        for start in (1, 16)
    )
    return (
        f"SELECT 'table', '{table.n}', CAST(COUNT(*) AS CHAR), CAST(COUNT(*) - COUNT(h) AS CHAR), "
        f"{sums}\n"  # nosec B608 -- a script the customer runs; every name is a quoted identifier of a checked name
        f"FROM (SELECT SHA2({row}, 256) AS h FROM {relation}) AS r;"
    )


def _header(side: Side, engine: str, contract_sha: str) -> tuple[str, ...]:
    """The header's fixed fields; the script adds the server's UTC start time."""
    return (MAGIC, FORMAT, side, engine, contract_sha)


_PG_NOW = "to_char(clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US')"
_MYSQL_NOW = "DATE_FORMAT(UTC_TIMESTAMP(6), '%Y-%m-%dT%H:%i:%s.%f')"


def side_engine(contract: StepContract, side: Side) -> str:
    """The engine a side's header names: the source family, or the Aurora engine."""
    return family(contract) if side == "source" else contract.target.engine


def render_script(
    contract: StepContract,
    contract_sha: str,
    side: Side,
    *,
    target_database: str | None = None,
) -> str:
    """The read-only script for one side (module docstring). ``target_database``: the
    Aurora MySQL database DMS writes into (``TargetDatabaseName``); PostgreSQL keeps the
    source schema on both sides."""
    fam = family(contract)
    tables, left = verify_tables(contract)
    header = _header(side, side_engine(contract, side), contract_sha)
    end = (END, "", "", "", "", "")
    where = "the source" if side == "source" else "Aurora"
    lines = [
        f"-- Step 1 verify, {where}: generated by the modernizer; you run it, it never connects "
        "to your databases.",
        f"-- Step contract SHA-256: {contract_sha}",
        f"-- Read-only: one SELECT per table, each in a READ ONLY transaction. {len(tables)} "
        "tables; each is read in full (a full scan).",
        "-- It prints row counts and checksums only, never a value. See verify.md.",
    ]
    if left:
        lines.append(f"-- Left out (a name holds a control character): {len(left)} tables.")
    if fam == "postgresql":
        lines += [
            "\\set QUIET on",
            "\\set ON_ERROR_STOP off",
            "\\pset format unaligned",
            "\\pset tuples_only on",
            "\\pset footer off",
            "\\pset null ''",
            "\\pset fieldsep '\\t'",
            "SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;",
            "SET client_encoding = 'UTF8';",
            "SET TimeZone = 'UTC';",
            "SET DateStyle = 'ISO, YMD';",
            "SET IntervalStyle = 'postgres';",
            "SET extra_float_digits = 3;",
            "SET bytea_output = 'hex';",
            "SELECT " + ", ".join(_pg_literal(v) for v in header) + f", {_PG_NOW};",
        ]
        for t in tables:
            lines += [f"-- {t.n}: {_comment(t.table_id)}", "BEGIN TRANSACTION READ ONLY;",
                      _pg_query(t, side), "COMMIT;"]  # fmt: skip
        lines.append("SELECT " + ", ".join(_pg_literal(v) for v in end) + ";")
    else:
        lines += [
            "SET NAMES utf8mb4;",
            "SET SESSION time_zone = '+00:00';",
            "SET SESSION TRANSACTION READ ONLY;",
            "SELECT " + ", ".join(_mysql_literal(v) for v in header) + f", {_MYSQL_NOW};",
        ]
        for t in tables:
            lines += [f"-- {t.n}: {_comment(t.table_id)}", "START TRANSACTION READ ONLY;",
                      _mysql_query(t, side, target_database), "COMMIT;"]  # fmt: skip
        lines.append("SELECT " + ", ".join(_mysql_literal(v) for v in end) + ";")
    return "\n".join(lines) + "\n"


def _instructions(fam: str, tables: int, left: int, step_dir: Path) -> str:
    pg = fam == "postgresql"

    def run(side: Side, n: int) -> str:
        out = step_dir / OUTPUTS[(side, n)]
        host = "<source host>" if side == "source" else "<Aurora reader or writer endpoint>"
        if pg:
            port = "5432"
            return (f'psql "host={host} port={port} dbname=<database> user=<read-only user> '
                    f'sslmode=verify-full" -X -f {SCRIPTS[side]} -o {out}')  # fmt: skip
        return (f"mysql --host={host} --user=<read-only user> -p --ssl-mode=VERIFY_IDENTITY "
                f"--batch --skip-column-names --force < {SCRIPTS[side]} > {out}")  # fmt: skip

    lines = [
        "# Step 1 verify (optional)",
        "",
        "An independent, read-only comparison of the source and Aurora: for each table step 1 "
        "moves, a row count and an order-independent checksum over every row and every copied "
        "column. The modernizer never connects to your databases: you run the two scripts with "
        "your own credentials, and `/move` reads only their output files.",
        "",
        "Verify is optional and does not change the gate: the gate's evidence stays AWS DMS's own "
        "(load, lag and validation). A table without a key, which DMS validation cannot check, is "
        "compared here like any other.",
        "",
        f"- Tables compared: {tables}."
        + (f" Left out: {left} (a name holds a control " "character)." if left else ""),
        "- Read-only: each script sets its session read-only and reads each table in its own "
        + (
            "`BEGIN TRANSACTION READ ONLY` transaction (`SET SESSION CHARACTERISTICS AS "
            "TRANSACTION READ ONLY` first)."
            if pg
            else "`START TRANSACTION READ ONLY` transaction (`SET SESSION TRANSACTION READ ONLY` first)."
        ),
        "  Use a user that may only read these tables.",
        "- What the files hold: row counts and checksums only, never a value or a table name. "
        "`verify.json` keeps the row counts and each table's result, not the checksums.",
        "- Cost: each table is read in full (a full scan), one table at a time; on a large "
        "table that is as much I/O as reading it all once. Run it when the source can take "
        "that load; on Aurora, a reader endpoint keeps it off the writer.",
        "",
        "## Order",
        "",
        "Run each script twice, in this order, while DMS keeps replicating; then ask `/move "
        "step-1` to read them (`run_dms_step.py verify`). Each file's header holds the "
        "server's time when the run started: the second run of a side must start after its "
        "first, or `verify` refuses it; and a run's source and Aurora files must start at most "
        "1 hour apart, or `verify` refuses the comparison (they come from different sessions): "
        "run both sides together, one right after the other. Running `verify-scripts` again "
        "removes every output file, so run all of them again after it.",
        "",
        "1. The source:",
        "",
        f"   ```\n   {run('source', 1)}\n   ```",
        "2. Aurora:",
        "",
        f"   ```\n   {run('target', 1)}\n   ```",
        "3. The source again (optional, recommended):",
        "",
        f"   ```\n   {run('source', 2)}\n   ```",
        "4. Aurora again (with step 3):",
        "",
        f"   ```\n   {run('target', 2)}\n   ```",
        "",
        "A table whose query fails (missing, or the user may not read it) prints no line; the "
        "script goes on " + ("(`ON_ERROR_STOP` off)" if pg else "(`--force`)") + " and the "
        "error is on your terminal, never in the file.",
        "",
        "## What each result means",
        "",
        "- `match`: the same row count and checksum on both sides (in the second run, when you "
        "made one).",
        "- `changing`: the two sides differ, and the source or Aurora changed between your two "
        "runs (writes were still arriving, or DMS was still applying them), so the comparison "
        "cannot settle. Run both again when the table is quiet, or after you stop writes for "
        "cutover.",
        "- `differ`: the two sides differ and neither changed between the runs (or you made one "
        "run only: a second run tells a changing table apart). A write that lands between your "
        "second source run and your second Aurora run is seen on neither side's change, so a "
        "quiet table that took one write in that window shows `differ`: run both again. Look at "
        "the table's DMS validation and table statistics before cutover.",
        "- `not_compared`: a script counted rows whose hash came out NULL (a value the server "
        "could not put in canonical form, such as a MySQL text value over `max_allowed_packet` "
        "once converted to `utf8mb4`): those rows were not compared, so the table is never "
        "reported `match`. Raise `max_allowed_packet` for the session's server and run again.",
        "- `missing`: a script printed no line for the table on one side: it is missing there, "
        "or the user may not read it.",
        "",
        "Canonical forms: numbers without trailing fractional zeros, floats by their bytes on "
        "PostgreSQL (in arrays too, element by element) and their shortest text on MySQL, times "
        "in UTC with six fractional digits, binary values by their bytes, JSON normalised; a "
        "column the contract casts to another type is compared in its Aurora type's form, so a "
        "cast that changes a value shows as `differ`.",
        "",
        "Limits: the PostgreSQL script needs PostgreSQL 11 or later on both sides (`sha256`); "
        "MySQL floats compare by their text, so a source and Aurora of different major "
        "versions may print one differently; a MariaDB source stores JSON as text, so a JSON "
        "value is compared as stored there (whitespace and key order count).",
    ]
    return "\n".join(lines) + "\n"


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(text.encode("utf-8"))
    os.replace(tmp, path)


def write_verify_scripts(
    contract: StepContract,
    contract_sha: str,
    step_dir: Path,
    *,
    source_engine: str | None,
    target_database: str | None = None,
) -> VerifyScripts:
    """Write both scripts and ``verify.md`` into ``step_dir``. Refuses a step whose source
    and target are not one family (step 1 is homogeneous) or that moves no table."""
    fam = family(contract)
    if source_engine is not None and family_of(source_engine) != fam:
        raise VerifyScriptError(
            f"verify compares a {fam} source with {contract.target.engine}; the plan's source is "
            f"{source_engine}"
        )
    tables, left = verify_tables(contract)
    if not tables:
        raise VerifyScriptError("step 1 moves no table verify can read: nothing to compare")
    step_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = {}
    for side, name in SCRIPTS.items():
        _write_atomic(step_dir / name, render_script(contract, contract_sha, side,
                                                     target_database=target_database))  # fmt: skip
        files[name] = str(step_dir / name)
    _write_atomic(
        step_dir / INSTRUCTIONS_FILE, _instructions(fam, len(tables), len(left), step_dir)
    )
    files[INSTRUCTIONS_FILE] = str(step_dir / INSTRUCTIONS_FILE)
    return VerifyScripts(
        tables=len(tables),
        columns=sum(len(t.columns) for t in tables),
        not_checked=left,
        files=files,
    )
