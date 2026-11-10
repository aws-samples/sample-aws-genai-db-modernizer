# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Read the customer's output of step 1's verify scripts and compare them (#1998).

Each file is untrusted input: the customer's machine wrote it. It is read strictly and
refused whole on the first problem, never guessed at (the design of the routine check's
reader, ``src/tools/data_movement/routine_check.py``, #1947):

- at most :data:`MAX_OUTPUT_BYTES`, UTF-8, one line per row, each exactly six
  tab-separated fields, no control character in any field, each at most :data:`MAX_FIELD`
  characters;
- the first line is the header the script prints: its format, its side (``source`` or
  ``target``), that side's engine, the SHA-256 of the step contract it was generated
  from and the server's UTC time when the run started; the last line is its end line (a
  file cut short has none);
- every other line is ``table``, the position of a table the script asks for (once), and
  four non-negative integers: the row count, the rows whose hash came out NULL and the two
  checksum sums.

A second run is tied to the first: each side's second file must have started after that
side's first, so a second pair left from an earlier session never decides a result
(``verify-scripts`` also removes every output file when it writes new scripts); and a
run's source and Aurora files must start at most :data:`MAX_PAIR_GAP` apart, or the
comparison is refused: the two sides come from different sessions.

A refusal names the file, the line number and what is wrong, never a line's content.
Nothing of the files is kept but the row counts: the result
(:class:`~src.contracts.verify_result.VerifyResult`) holds the contract's table ids, the
counts and each table's status, never a checksum.

Per table, with the first run's source and target lines and, when the customer made it,
the second run's: ``missing`` when a file has no line for it; else ``not_compared`` when a
line counts a NULL hash (those rows were not compared, so never ``match``); else ``match``
when the latest run's two lines agree (count and both sums); else ``changing`` when either
side's two runs differ (writes were still arriving, or DMS was still applying them); else
``differ``. Linear in the lines.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.contracts.transformation_contract import StepContract
from src.contracts.verify_result import (
    STATUS_ORDER,
    VerifyNotChecked,
    VerifyResult,
    VerifyStatus,
    VerifyTable,
)
from src.tools.data_movement.verify_scripts import (
    END,
    FIELDS,
    FORMAT,
    MAGIC,
    OUTPUTS,
    Side,
    family,
    side_engine,
    verify_tables,
)

__all__ = [
    "MAX_FIELD",
    "MAX_OUTPUT_BYTES",
    "VerifyOutputError",
    "compare",
    "parse_output",
    "read_verify",
]

MAX_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_FIELD = 128
#: The most a run's source and Aurora files may start apart (each server's own UTC clock):
#: further apart, they come from different sessions (the customer reran one side only), and
#: their comparison would say nothing about one moment of the data.
MAX_PAIR_GAP = timedelta(hours=1)
_BAD = {"Cc", "Cf", "Zl", "Zp", "Cs", "Co", "Cn"}
_NUMBER = re.compile(r"0|[1-9][0-9]{0,39}")
_STARTED = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}")

#: A table's line: row count, NULL-hash rows, first sum, second sum.
Counts = tuple[int, int, int, int]


@dataclass(frozen=True)
class Output:
    """One parsed output file: when its run started (server UTC) and its table lines."""

    started: str
    rows: dict[int, Counts]


class VerifyOutputError(ValueError):
    """An output file is refused: the message names the file, the line and the problem only."""


def _clean(field: str) -> bool:
    return not any(unicodedata.category(c) in _BAD for c in field)


def parse_output(
    data: bytes,
    *,
    name: str,
    side: Side,
    engine: str,
    contract_sha: str,
    positions: set[int],
) -> Output:
    """The file's table lines by table position, after the strict reading of the module
    docstring; :class:`VerifyOutputError` on the first problem."""
    if len(data) > MAX_OUTPUT_BYTES:
        raise VerifyOutputError(f"{name}: over {MAX_OUTPUT_BYTES} bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise VerifyOutputError(f"{name}: not UTF-8 text") from None
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines:
        raise VerifyOutputError(f"{name}: empty: run the script again")
    header = (MAGIC, FORMAT, side, engine, contract_sha)
    rows: dict[int, Counts] = {}
    started = ""
    end = False
    for number, raw in enumerate(lines, start=1):
        line = raw[:-1] if raw.endswith("\r") else raw
        if end:
            raise VerifyOutputError(f"{name}: line {number}: a line after the end line")
        fields = line.split("\t")
        if len(fields) != FIELDS:
            raise VerifyOutputError(
                f"{name}: line {number}: {len(fields)} tab-separated fields, not {FIELDS}"
            )
        if any(len(f) > MAX_FIELD or not _clean(f) for f in fields):
            raise VerifyOutputError(
                f"{name}: line {number}: a field is too long or holds a control character"
            )
        if number == 1:
            if tuple(fields[:5]) != header:
                raise VerifyOutputError(f"{name}: {_header_problem(fields, header)}")
            if not _STARTED.fullmatch(fields[5]):
                raise VerifyOutputError(f"{name}: line 1: not the run's start time")
            started = fields[5]
            continue
        if fields[0] == END:
            if any(fields[1:]):
                raise VerifyOutputError(f"{name}: line {number}: the end line has values")
            end = True
            continue
        if fields[0] != "table" or not _NUMBER.fullmatch(fields[1]):
            raise VerifyOutputError(f"{name}: line {number}: not a table line")
        position = int(fields[1])
        if position not in positions:
            raise VerifyOutputError(
                f"{name}: line {number}: names a table the script does not ask for "
                "(a file from another step, job or script?)"
            )
        if position in rows:
            raise VerifyOutputError(f"{name}: line {number}: the same table twice")
        if not all(_NUMBER.fullmatch(f) for f in fields[2:]):
            raise VerifyOutputError(f"{name}: line {number}: a count that is not a whole number")
        rows[position] = (int(fields[2]), int(fields[3]), int(fields[4]), int(fields[5]))
    if not end:
        raise VerifyOutputError(f"{name}: no end line: it was cut short; run the script again")
    return Output(started=started, rows=rows)


def _header_problem(fields: list[str], header: tuple[str, ...]) -> str:
    if fields[0] != MAGIC or fields[1] != FORMAT:
        return "line 1 is not the header a verify script prints"
    if fields[2] != header[2]:
        return f"the output of the other side's script, not the {header[2]}'s"
    if fields[3] != header[3]:
        return "the output of a script for another engine"
    return (
        "the output of a verify script of another step contract: run run_dms_step.py "
        "verify-scripts, then the new scripts"
    )


Pair = tuple[Counts | None, Counts | None]


def _status(first: Pair, second: Pair | None) -> tuple[VerifyStatus, list[Side], list[Side]]:
    """The table's status, the sides with no line for it, the sides that counted a NULL hash."""
    runs = [first] if second is None else [first, second]
    sides: tuple[Side, Side] = ("source", "target")
    missing = [side for i, side in enumerate(sides) if any(r[i] is None for r in runs)]
    if missing:
        return "missing", missing, []
    unhashed = [side for i, side in enumerate(sides) if any(_nulls(r[i]) for r in runs)]
    if unhashed:
        return "not_compared", [], unhashed
    source, target = runs[-1]
    if _checksum(source) == _checksum(target):
        return "match", [], []
    if second is not None and (first[0] != second[0] or first[1] != second[1]):
        return "changing", [], []
    return "differ", [], []


def _moment(output: Output) -> datetime:
    return datetime.fromisoformat(output.started).replace(tzinfo=UTC)


def _hours(gap: timedelta) -> str:
    hours = gap.total_seconds() / 3600
    return f"{hours:g} hour" + ("" if hours == 1 else "s")


def _nulls(counts: Counts | None) -> int:
    return 0 if counts is None else counts[1]


def _checksum(counts: Counts | None) -> tuple[int, int, int] | None:
    return None if counts is None else (counts[0], counts[2], counts[3])


def compare(
    contract: StepContract,
    outputs: dict[tuple[Side, int], dict[int, Counts]],
    *,
    job_id: str,
    database_name: str,
    contract_sha: str,
    now: datetime | None = None,
) -> VerifyResult:
    """The result from the parsed outputs, keyed by (side, run); the second run is used
    when both of its files are there."""
    tables, left = verify_tables(contract)
    second_run = ("source", 2) in outputs and ("target", 2) in outputs
    rows: list[VerifyTable] = []
    for t in tables:
        first = (outputs[("source", 1)].get(t.n), outputs[("target", 1)].get(t.n))
        second = (
            (outputs[("source", 2)].get(t.n), outputs[("target", 2)].get(t.n))
            if second_run
            else None
        )
        status, missing, unhashed = _status(first, second)
        latest = second or first
        rows.append(VerifyTable(
            table_id=t.table_id, status=status, dms_validated=t.keyed,
            source_rows=None if latest[0] is None else latest[0][0],
            target_rows=None if latest[1] is None else latest[1][0],
            missing_on=missing, unhashed_on=unhashed))  # fmt: skip
    rows.sort(key=lambda r: r.table_id)
    fam = family(contract)
    return VerifyResult(
        job_id=job_id,
        database_name=database_name,
        source_engine=fam,  # type: ignore[arg-type]
        target_engine=contract.target.engine,  # type: ignore[arg-type]
        contract_sha256=contract_sha,
        checked_at=now or datetime.now(UTC),
        second_run=second_run,
        status=max((r.status for r in rows), key=STATUS_ORDER.index),
        tables=rows,
        not_checked=[VerifyNotChecked(table_id=i, reason="unsafe_name") for i, _ in sorted(left)],
    )


def read_verify(
    step_dir: Path,
    contract: StepContract,
    *,
    job_id: str,
    database_name: str,
    contract_sha: str,
    now: datetime | None = None,
) -> VerifyResult | None:
    """The result from the output files in ``step_dir``; None while the first run's two
    files are not both there. One file of the second run without the other is refused, and
    so is a second-run file that did not start after its side's first."""
    present = {key for key, name in OUTPUTS.items() if (step_dir / name).is_file()}
    if not {("source", 1), ("target", 1)} <= present:
        return None
    if len({k for k in present if k[1] == 2}) == 1:
        alone = next(OUTPUTS[k] for k in present if k[1] == 2)
        raise VerifyOutputError(
            f"{alone}: the second run needs both files: run the other script again too, or "
            "remove it"
        )
    tables, _ = verify_tables(contract)
    positions = {t.n for t in tables}
    parsed: dict[tuple[Side, int], Output] = {}
    for key in sorted(present):
        side, _run = key
        path = step_dir / OUTPUTS[key]
        if path.stat().st_size > MAX_OUTPUT_BYTES:
            raise VerifyOutputError(f"{OUTPUTS[key]}: over {MAX_OUTPUT_BYTES} bytes")
        parsed[key] = parse_output(
            path.read_bytes(),
            name=OUTPUTS[key],
            side=side,
            engine=side_engine(contract, side),
            contract_sha=contract_sha,
            positions=positions,
        )
    sides: tuple[Side, Side] = ("source", "target")
    for side in sides:
        later = parsed.get((side, 2))
        if later is not None and later.started <= parsed[(side, 1)].started:
            raise VerifyOutputError(
                f"{OUTPUTS[(side, 2)]}: its run did not start after {OUTPUTS[(side, 1)]}'s "
                "(a second run left from an earlier session?): run the second pair again, or "
                "remove both of its files"
            )
    for run in (1, 2):
        if ("source", run) not in parsed:
            continue
        gap = abs(_moment(parsed[("source", run)]) - _moment(parsed[("target", run)]))
        if gap > MAX_PAIR_GAP:
            raise VerifyOutputError(
                f"{OUTPUTS[('source', run)]} and {OUTPUTS[('target', run)]}: their runs started "
                f"more than {_hours(MAX_PAIR_GAP)} apart (different sessions?): run both "
                "scripts again, one right after the other"
            )
    return compare(contract, {k: v.rows for k, v in parsed.items()}, job_id=job_id,
                   database_name=database_name, contract_sha=contract_sha, now=now)  # fmt: skip
