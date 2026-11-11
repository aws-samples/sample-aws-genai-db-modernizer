# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1's read-only LOB size script and the strict reader of its output (#2123).

The MySQL source endpoint truncates a LOB above the task's limit with no task error (in a live
run a 64 KB limit cut four WordPress rows, caught only by DMS validation), so a moved MySQL
table whose LOB columns the collection did not measure gets no silent size: preflight's
``lob_sizes`` check stops, and step 1 writes ``lob-sizes.sql`` for the customer to run on the
source with their own credentials. The modernizer never connects to a database: it reads only
the output file, ``lob-sizes.tsv``.

The script (MySQL and MariaDB): ``SET SESSION TRANSACTION READ ONLY``, then one
``START TRANSACTION READ ONLY`` per table, reading the table once for all of its LOB columns
(``MAX(LENGTH(column))`` and ``COUNT(column)``, every identifier a quoted one of a checked
name). It prints lengths and counts only, never a value.

The output is untrusted input, read strictly and refused whole on the first problem, never
guessed at (the design of the verify and routine check readers):

- at most :data:`MAX_OUTPUT_BYTES`, UTF-8, one line per row, each exactly :data:`FIELDS`
  tab-separated fields with no control character, each at most :data:`MAX_FIELD` characters;
- the first line is the header the script prints: its format, the SHA-256 of the step contract
  it was written for, the SHA-256 of the table and column list it measures (so a script from
  before the plan changed is refused) and the server's UTC time when the run started; the last
  line is its end line (a file cut short has none);
- every other line is ``column``, the table's and the column's positions (from 1, in the order
  the script lists them; each once) and two non-negative integers: the longest value in bytes
  and the non-NULL values;
- every column the script asks for has its line (a table whose query failed, missing or not
  readable, prints none: measure it, or the file is refused).

A refusal names the file, the line number and what is wrong, never a line's content.
"""

from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from src.contracts.data_movement_plan import LobTable
from src.contracts.lob_sizes import LobSizeColumn, LobSizes
from src.tools.data_movement.dms_sizing import LOB_SIZES_SQL, needs_measuring

__all__ = [
    "END",
    "FIELDS",
    "FORMAT",
    "INSTRUCTIONS_FILE",
    "MAGIC",
    "MAX_FIELD",
    "MAX_OUTPUT_BYTES",
    "OUTPUT",
    "SCRIPT",
    "LobSizesError",
    "MeasureColumn",
    "layout_sha",
    "measure_columns",
    "parse_output",
    "render_instructions",
    "render_script",
]

SCRIPT = LOB_SIZES_SQL
OUTPUT = "lob-sizes.tsv"
INSTRUCTIONS_FILE = "lob-sizes.md"
MAGIC = "modernizer-lob-sizes"
END = "modernizer-lob-sizes-end"
FORMAT = "1"
# header: magic, format, contract SHA-256, layout SHA-256, run start (UTC);
# column: "column", table position, column position, longest value in bytes, non-NULL values
FIELDS = 5
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_FIELD = 128
_BAD = {"Cc", "Cf", "Zl", "Zp", "Cs", "Co", "Cn"}
_NUMBER = re.compile(r"0|[1-9][0-9]{0,39}")
_SHA = re.compile(r"[0-9a-f]{64}")
_STARTED = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}")
_MYSQL_NOW = "DATE_FORMAT(UTC_TIMESTAMP(6), '%Y-%m-%dT%H:%i:%s.%f')"


class LobSizesError(ValueError):
    """The script cannot be written, or an output file is refused (file, line and problem only)."""


@dataclass(frozen=True)
class MeasureColumn:
    """One column the script measures: the table's position ``n`` and the column's ``k``."""

    n: int
    k: int
    table: str
    schema: str
    name: str
    column: str


def _clean(text: str) -> bool:
    return bool(text) and not any(unicodedata.category(c) in _BAD for c in text)


def measure_columns(
    lob_tables: list[LobTable], dialect: str | None, moved: set[str]
) -> list[MeasureColumn]:
    """The moved tables' LOB columns the script measures (:func:`needs_measuring`), in order.
    A name with a control character cannot be quoted safely: refused."""
    out: list[MeasureColumn] = []
    tables = [t for t in needs_measuring(lob_tables, dialect) if t.table in moved]
    for n, t in enumerate(tables, start=1):
        schema, _, name = t.table.rpartition(".")
        if not all(_clean(x) for x in (schema, name, *t.columns)):
            raise LobSizesError(
                f"table {n}: a schema, table or column name holds a control character"
            )
        out += [
            MeasureColumn(n, k, t.table, schema, name, c) for k, c in enumerate(t.columns, start=1)
        ]
    return out


def layout_sha(columns: list[MeasureColumn]) -> str:
    """SHA-256 of the measured table and column list, in the script's order."""
    text = "".join(f"{c.n}\t{c.k}\t{c.table}\t{c.column}\n" for c in columns)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _ident(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def _literal(text: str) -> str:
    return "'" + text.replace("\\", "\\\\").replace("'", "''") + "'"


def _comment(text: str) -> str:
    return text.replace("\n", " ").replace("\r", " ")


def render_script(columns: list[MeasureColumn], contract_sha: str) -> str:
    """The read-only MySQL script (module docstring) for ``columns``."""
    if not columns:
        raise LobSizesError("no LOB column to measure")
    header = (MAGIC, FORMAT, contract_sha, layout_sha(columns))
    tables = sorted({c.n for c in columns})
    lines = [
        "-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.",
        "-- SPDX-License-Identifier: MIT-0",
        "--",
        "-- Step 1 LOB sizes, the source: generated by the modernizer; you run it, it never "
        "connects to your databases.",
        f"-- Step contract SHA-256: {contract_sha}",
        f"-- Read-only: one SELECT per table, each in a READ ONLY transaction. {len(tables)} tables, "
        f"{len(columns)} LOB columns; each table is read in full (a full scan).",
        "-- It prints the longest value's length in bytes and the non-NULL count per column, never "
        f"a value. See {INSTRUCTIONS_FILE}.",
        "SET NAMES utf8mb4;",
        "SET SESSION TRANSACTION READ ONLY;",
        "SELECT " + ", ".join(_literal(v) for v in header) + f", {_MYSQL_NOW};",
    ]
    for n in tables:
        cols = [c for c in columns if c.n == n]
        first = cols[0]
        relation = f"{_ident(first.schema)}.{_ident(first.name)}"
        measures = ", ".join(
            f"MAX(LENGTH({_ident(c.column)})) AS m{c.k}, COUNT({_ident(c.column)}) AS v{c.k}"
            for c in cols
        )
        picks = " UNION ALL ".join(f"SELECT '{c.k}' AS k" for c in cols)
        longest = "CASE p.k " + " ".join(f"WHEN '{c.k}' THEN m.m{c.k}" for c in cols) + " END"
        count = "CASE p.k " + " ".join(f"WHEN '{c.k}' THEN m.v{c.k}" for c in cols) + " END"
        lines += [
            f"-- {n}: {_comment(first.table)}",
            "START TRANSACTION READ ONLY;",
            # A file the customer runs: every name is a quoted identifier of a checked name.
            f"SELECT 'column', '{n}', p.k, CAST(COALESCE({longest}, 0) AS CHAR), CAST({count} AS CHAR)\n"  # nosec B608 -- every name is a quoted identifier of a checked name
            f"FROM (SELECT {measures} FROM {relation}) AS m CROSS JOIN ({picks}) AS p ORDER BY p.k + 0;",
            "COMMIT;",
        ]
    lines.append("SELECT " + ", ".join(_literal(v) for v in (END, "", "", "", "")) + ";")
    return "\n".join(lines) + "\n"


def render_instructions(columns: list[MeasureColumn], step_dir: str) -> str:
    """``lob-sizes.md``: what the script does, how to run it and what /move does next.
    ``step_dir``: the step's folder under the artifact root (``<db>/<job>/data-movement/step-1``).
    """
    tables = sorted({(c.n, c.table) for c in columns})
    out = f"<artifact root>/{step_dir}/{OUTPUT}"
    shown = ", ".join(t for _, t in tables[:10]) + (
        f" and {len(tables) - 10} more" if len(tables) > 10 else ""
    )
    lines = [
        "# Step 1 LOB sizes",
        "",
        f"The collection did not measure the LOB columns of {len(tables)} moved "
        f"table{'' if len(tables) == 1 else 's'} ({shown}). The MySQL source endpoint truncates a "
        "value longer than the task's LOB limit with no task error, so step 1 does not guess a "
        "size: preflight's `lob_sizes` check stops until each table is measured.",
        "",
        "The modernizer never connects to your databases: you run `lob-sizes.sql` on the source "
        "with your own credentials, and `/move` reads only its output file.",
        "",
        "- Read-only: the script sets its session read-only and reads each table in its own "
        "`START TRANSACTION READ ONLY` transaction. Use a user that may only read these tables.",
        "- What the file holds: per LOB column, the longest value's length in bytes "
        "(`MAX(LENGTH(column))`) and the non-NULL count, never a value or a name.",
        "- Cost: each table is read in full (a full scan), one table at a time. Run it when the "
        "source can take that load. Each table's READ ONLY transaction keeps one read view open for "
        "its whole scan, which holds back InnoDB's undo purge on a large, busy table until it ends.",
        "",
        "## Run it",
        "",
        "With the MySQL client:",
        "",
        "```",
        f"mysql --host=<source host> --user=<read-only user> -p --ssl-mode=VERIFY_IDENTITY "
        f"--default-character-set=utf8mb4 --batch --skip-column-names --force < {SCRIPT} > {out}",
        "```",
        "",
        "With the MariaDB client (it has no `--ssl-mode`; `--ssl-verify-server-cert` is MySQL's "
        "`VERIFY_IDENTITY`):",
        "",
        "```",
        f"mariadb --host=<source host> --user=<read-only user> -p --ssl-ca=<CA bundle> --ssl-verify-server-cert "
        f"--default-character-set=utf8mb4 --batch --skip-column-names --force < {SCRIPT} > {out}",
        "```",
        "",
        "Then ask `/move step-1` to read it (`run_dms_step.py lob-sizes`): it sizes each table "
        "from the longest value (2 bytes per byte of a character LOB, 1.25 times headroom), writes "
        "the plan and `dms-step-1.yaml` again, and the preflight runs again. A table whose query "
        "fails (missing, or the user may not read it) prints no line, and the file is refused: "
        "fix the access and run the script again. Measure again before the full load if the data "
        "grew.",
        "",
        "## Skipping the measurement",
        "",
        'Only if you choose to: set `"lob_sizes": "skip"` in step 1\'s `inputs.json` and run '
        "`run_dms_step.py lob-sizes`. Each unmeasured table then runs at 1024 KB, the replication "
        "instance is sized for that (it costs memory per LOB column), and a longer value is "
        "truncated with only a warning in the task log: only DMS validation reports it.",
    ]
    return "\n".join(lines) + "\n"


def parse_output(
    data: bytes,
    *,
    name: str,
    contract_sha: str,
    columns: list[MeasureColumn],
) -> tuple[str, dict[tuple[int, int], tuple[int, int]]]:
    """(the run's start time, (n, k) -> (longest bytes, non-NULL values)) after the strict
    reading of the module docstring; :class:`LobSizesError` on the first problem."""
    if len(data) > MAX_OUTPUT_BYTES:
        raise LobSizesError(f"{name}: over {MAX_OUTPUT_BYTES} bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise LobSizesError(f"{name}: not UTF-8 text") from None
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines:
        raise LobSizesError(f"{name}: empty: run the script again")
    expected = {(c.n, c.k) for c in columns}
    rows: dict[tuple[int, int], tuple[int, int]] = {}
    started = ""
    end = False
    for number, raw in enumerate(lines, start=1):
        line = raw[:-1] if raw.endswith("\r") else raw
        if end:
            raise LobSizesError(f"{name}: line {number}: a line after the end line")
        fields = line.split("\t")
        if len(fields) != FIELDS:
            raise LobSizesError(
                f"{name}: line {number}: {len(fields)} tab-separated fields, not {FIELDS}"
            )
        if any(
            len(f) > MAX_FIELD or any(unicodedata.category(c) in _BAD for c in f) for f in fields
        ):
            raise LobSizesError(
                f"{name}: line {number}: a field is too long or holds a control character"
            )
        if number == 1:
            if fields[0] != MAGIC or fields[1] != FORMAT:
                raise LobSizesError(f"{name}: line 1 is not the header lob-sizes.sql prints")
            if not _SHA.fullmatch(fields[2]) or fields[2] != contract_sha:
                raise LobSizesError(f"{name}: the output of a lob-sizes.sql of another step contract: run "
                                    "the step's current lob-sizes.sql")  # fmt: skip
            if fields[3] != layout_sha(columns):
                raise LobSizesError(f"{name}: the output of a lob-sizes.sql for other tables or columns (the "
                                    "plan changed since): run the step's current lob-sizes.sql")  # fmt: skip
            if not _STARTED.fullmatch(fields[4]):
                raise LobSizesError(f"{name}: line 1: not the run's start time")
            started = fields[4]
            continue
        if fields[0] == END:
            if any(fields[1:]):
                raise LobSizesError(f"{name}: line {number}: the end line has values")
            end = True
            continue
        if fields[0] != "column" or not all(_NUMBER.fullmatch(f) for f in fields[1:]):
            raise LobSizesError(f"{name}: line {number}: not a column line")
        key = (int(fields[1]), int(fields[2]))
        if key not in expected:
            raise LobSizesError(
                f"{name}: line {number}: names a column the script does not ask for"
            )
        if key in rows:
            raise LobSizesError(f"{name}: line {number}: the same column twice")
        rows[key] = (int(fields[3]), int(fields[4]))
    if not end:
        raise LobSizesError(f"{name}: no end line: it was cut short; run the script again")
    missing = sorted(expected - set(rows))
    if missing:
        n, k = missing[0]
        raise LobSizesError(f"{name}: no line for table {n} column {k} ({len(missing)} missing): its "
                            "query failed (see your terminal); fix it and run the script again")  # fmt: skip
    return started, rows


def measured(
    columns: list[MeasureColumn],
    rows: dict[tuple[int, int], tuple[int, int]],
    started: str,
    *,
    job_id: str,
    database_name: str,
    contract_sha: str,
    earlier: LobSizes | None = None,
) -> LobSizes:
    """``lob-sizes.json`` from a read output, keeping an earlier file's other columns."""
    found = {(c.table, c.column): LobSizeColumn(table=c.table, column=c.column,
                                                max_value_bytes=rows[(c.n, c.k)][0],
                                                values=rows[(c.n, c.k)][1]) for c in columns}  # fmt: skip
    for c in earlier.columns if earlier else []:
        found.setdefault((c.table, c.column), c)
    return LobSizes(job_id=job_id, database_name=database_name, choice="measured",
                    recorded_at=datetime.now(UTC), contract_sha256=contract_sha, measured_at=started,
                    columns=[found[k] for k in sorted(found)])  # fmt: skip


def skipped_choice(*, job_id: str, database_name: str, earlier: LobSizes | None = None) -> LobSizes:
    """``lob-sizes.json`` for the customer's choice to skip measuring, keeping an earlier
    measurement (so measuring after all needs no new run for those columns)."""
    return LobSizes(job_id=job_id, database_name=database_name, choice="skipped", recorded_at=datetime.now(UTC),
                    columns=earlier.columns if earlier else [],
                    contract_sha256=earlier.contract_sha256 if earlier else None,
                    measured_at=earlier.measured_at if earlier else None)  # fmt: skip


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(text.encode("utf-8"))
    os.replace(tmp, path)
