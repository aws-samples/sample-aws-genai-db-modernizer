# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Read the customer's output of step 1's routine check script (#1947).

The file is untrusted input: the customer's machine wrote it. It is read strictly, and
refused whole on the first problem, never guessed at:

- at most :data:`MAX_OUTPUT_BYTES`, UTF-8, one line per row, each exactly seven
  tab-separated fields, no control character in any field, each at most
  :data:`MAX_FIELD` characters;
- the first line is the header ``routines-check.sql`` prints: its format, this step's
  engine, the SHA-256 of the step contract it was generated from, and the target database
  it asked about; the last line is its end line (a file cut short has none);
- every other line names a routine of the contract's routines section, by the keys the
  check script asks for (:func:`~src.tools.data_movement.routine_scripts.check_names`),
  once; a trigger's state and an event's status are among the values the engine has.

A refusal names the line number and what is wrong, never the line's content. Nothing of
the file is kept: the result (:class:`~src.contracts.routine_check.RoutineCheckResult`)
holds the contract's ids, fixed problem codes and counts. The file never reaches a model
input; if one ever reads the result, its fields are the contract's own.

Then each carried routine is compared with the rows of its name:

- PostgreSQL functions and procedures: the argument types (``oidvectortypes`` of
  ``proargtypes``) against the section's signature, modes and schema qualifiers dropped,
  so overloads are told apart; triggers by schema, table and name, and must be disabled
  (``tgenabled = 'D'``).
- MySQL procedures and functions: each parameter's mode and base type, and a function's
  return base type (``int(11)`` and ``INT`` are one type; lengths and character sets are
  not compared). Triggers and events are deferred to the cutover script and must be
  absent.

A routine that exists compiled: PostgreSQL's ``CREATE`` ran with ``check_function_bodies``
on, and MySQL parses a whole body at ``CREATE``. Linear in the lines and the routines.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.contracts.routine_check import RoutineCheckResult, RoutineMismatch, RoutineNotCarried
from src.contracts.transformation_contract import StepContract, StepRoutine, StepRoutines
from src.tools.data_movement.routine_scripts import (
    CHECK_FIELDS,
    CHECK_FORMAT,
    ROUTINES_CHECK,
    CheckScript,
    check_names,
    output_key,
)

__all__ = [
    "MAX_FIELD",
    "MAX_OUTPUT_BYTES",
    "RoutineCheckOutputError",
    "check_routines",
    "parse_check_output",
    "read_check",
]

MAX_OUTPUT_BYTES = 256 * 1024 * 1024
MAX_FIELD = 8192
_KINDS = {
    "aurora_postgresql": {"function", "procedure", "trigger"},
    "aurora_mysql": {"function", "procedure", "trigger", "event"},
}
_PG_TRIGGER_STATES = {"O", "D", "R", "A"}
_MYSQL_EVENT_STATES = {"ENABLED", "DISABLED", "SLAVESIDE_DISABLED", "REPLICA_SIDE_DISABLED"}
_BAD = {"Cc", "Cf", "Zl", "Zp", "Cs", "Co", "Cn"}
_MODES = ("INOUT ", "IN ", "OUT ", "VARIADIC ")
_MYSQL_ALIASES = {
    "integer": "int",
    "bool": "tinyint",
    "boolean": "tinyint",
    "dec": "decimal",
    "numeric": "decimal",
    "fixed": "decimal",
    "real": "double",
    "character": "char",
    "nchar": "char",
    "nvarchar": "varchar",
}
_MYSQL_BASE = re.compile(r"[a-z]+")


class RoutineCheckOutputError(ValueError):
    """The check output is refused: the message names the line and the problem only."""


Row = tuple[str, str, str, str, str, str, str]


def _clean(field: str) -> bool:
    return not any(unicodedata.category(c) in _BAD for c in field)


def parse_check_output(
    data: bytes,
    section: StepRoutines,
    *,
    engine: str,
    contract_sha: str,
    target_database: str | None,
    asked: Iterable[StepRoutine] | None = None,
    spec: CheckScript = ROUTINES_CHECK,
    settings: dict[str, frozenset[str]] | None = None,
) -> dict[tuple[str, ...], list[Row]]:
    """The output's routine lines, by their check key, after the strict reading of the
    module docstring; :class:`RoutineCheckOutputError` on the first problem. ``asked`` and
    ``spec`` read the output of another check script built the same way (the cutover
    check, #1948): the routines it asks for and its header and end lines. ``settings``
    names the setting lines it prints (their first field, every other field empty but the
    last, which is one of the values given), each at most once, under the key ``(name,)``."""
    if len(data) > MAX_OUTPUT_BYTES:
        raise RoutineCheckOutputError(f"the check output is over {MAX_OUTPUT_BYTES} bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise RoutineCheckOutputError("the check output is not UTF-8 text") from None
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines:
        raise RoutineCheckOutputError(f"the check output is empty: run {spec.script} again")
    header = (spec.magic, CHECK_FORMAT, engine, contract_sha, target_database or "", "", "")
    names = check_names(section, engine, target_database, rows=asked)
    rows: dict[tuple[str, ...], list[Row]] = {}
    seen: set[Row] = set()
    end = False
    for number, raw in enumerate(lines, start=1):
        line = raw[:-1] if raw.endswith("\r") else raw
        if end:
            raise RoutineCheckOutputError(f"line {number}: a line after the end line")
        fields = line.split("\t")
        if len(fields) != CHECK_FIELDS:
            raise RoutineCheckOutputError(
                f"line {number}: {len(fields)} tab-separated fields, not {CHECK_FIELDS}"
            )
        if any(len(f) > MAX_FIELD or not _clean(f) for f in fields):
            raise RoutineCheckOutputError(
                f"line {number}: a field is too long or holds a control character"
            )
        if number == 1:
            if tuple(fields) != header:
                raise RoutineCheckOutputError(_header_problem(fields, header, spec))
            continue
        if fields[0] == spec.end:
            if any(fields[1:]):
                raise RoutineCheckOutputError(f"line {number}: the end line has values")
            end = True
            continue
        row: Row = tuple(fields)  # type: ignore[assignment]
        if settings and row[0] in settings:
            if any(row[1:6]) or row[6] not in settings[row[0]]:
                raise RoutineCheckOutputError(
                    f"line {number}: a setting value the script does not print"
                )
            if (row[0],) in rows:
                raise RoutineCheckOutputError(f"line {number}: the same setting line twice")
            rows[(row[0],)] = [row]
            continue
        key = _key(row, engine)
        if row[0] not in _KINDS[engine] or key not in names:
            raise RoutineCheckOutputError(
                f"line {number}: names a routine the step's check script does not ask for "
                "(a file from another step, job or script?)"
            )
        if row in seen:
            raise RoutineCheckOutputError(f"line {number}: the same routine line twice")
        _check_state(row, engine, number)
        seen.add(row)
        rows.setdefault(key, []).append(row)
    if not end:
        raise RoutineCheckOutputError(
            f"the check output has no end line: it was cut short; run {spec.script} again"
        )
    return rows


def _header_problem(fields: list[str], header: tuple[str, ...], spec: CheckScript) -> str:
    if fields[0] != spec.magic or fields[1] != CHECK_FORMAT:
        return f"line 1 is not the header {spec.script} prints"
    if fields[2] != header[2]:
        return "the check output is for another Aurora engine"
    if fields[3] != header[3]:
        return (
            f"the check output comes from a {spec.script} of another step contract: run "
            f"run_dms_step.py {spec.command}, then the new {spec.script}"
        )
    return "the check output's header names another target database"


def _key(row: Row, engine: str) -> tuple[str, ...]:
    kind, schema, table, name = row[0], row[1], row[2], row[3]
    if engine == "aurora_mysql":
        return (kind, schema.lower(), "", name.lower())
    return (kind, schema, table if kind == "trigger" else "", name)


def _check_state(row: Row, engine: str, number: int) -> None:
    kind, state = row[0], row[6]
    if engine == "aurora_postgresql" and kind == "trigger":
        ok = state in _PG_TRIGGER_STATES
    elif engine == "aurora_mysql" and kind == "event":
        ok = state in _MYSQL_EVENT_STATES
    else:
        ok = state == ""
    if not ok:
        raise RoutineCheckOutputError(f"line {number}: a state the engine does not report")


# --------------------------------------------------------------------------- comparison


def _split(text: str) -> list[str]:
    """Comma-separated items at parenthesis depth 0, stripped."""
    out: list[str] = []
    current: list[str] = []
    depth = 0
    for c in text:
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        if c == "," and depth == 0:
            out.append("".join(current).strip())
            current = []
        else:
            current.append(c)
    last = "".join(current).strip()
    if last or out:
        out.append(last)
    return out


def _pg_types(signature: str) -> tuple[str, ...]:
    """PostgreSQL argument types, modes and schema qualifiers dropped, lower case."""
    out = []
    for item in _split(signature):
        text = " ".join(item.split())
        for mode in _MODES:
            if text.upper().startswith(mode):
                text = text[len(mode) :]
                break
        if "." in text and "(" not in text.split(".", 1)[0]:
            text = text.split(".", 1)[1]
        out.append(text.strip('"').lower())
    return tuple(out)


def _mysql_base(type_text: str) -> str:
    match = _MYSQL_BASE.match(type_text.strip().lower())
    base = match.group(0) if match else type_text.strip().lower()
    if base == "double" or base.startswith("double"):
        return "double"
    return _MYSQL_ALIASES.get(base, base)


def _mysql_params(signature: str, kind: str) -> tuple[tuple[str, str], ...]:
    out = []
    for item in _split(signature):
        text = " ".join(item.split())
        mode = ""
        for m in ("INOUT ", "IN ", "OUT "):
            if text.upper().startswith(m):
                mode, text = m.strip(), text[len(m) :]
                break
        if not mode or kind == "function":
            mode = "IN"  # a function's parameters are IN; some servers report no mode
        out.append((mode, _mysql_base(text)))
    return tuple(out)


def _signature_key(row: StepRoutine | Row, engine: str) -> tuple[Any, ...] | None:
    if isinstance(row, tuple):
        kind, args, returns = row[0], row[4], row[5]
    else:
        if row.signature is None:
            return None
        kind, args, returns = row.kind, row.signature, row.returns or ""
    if engine == "aurora_postgresql":
        return _pg_types(args)
    ret = _mysql_base(returns) if kind == "function" and returns else ""
    return (_mysql_params(args, kind), ret)


def _compare(
    row: StepRoutine,
    found: list[Row],
    claimed: set[tuple[Any, ...]],
    engine: str,
) -> str | None:
    """The problem with a carried or deferred routine, or None when it is confirmed."""
    if row.carry == "deferred_to_cutover":
        return "present_before_cutover" if found else None
    if row.kind == "trigger":
        if not found:
            return "missing"
        return None if all(r[6] == "D" for r in found) else "trigger_enabled"
    want = _signature_key(row, engine)
    if want is None:
        return None if found else "missing"
    if any(_signature_key(r, engine) == want for r in found):
        return None
    others = [r for r in found if _signature_key(r, engine) not in claimed]
    return "signature_mismatch" if others else "missing"


def check_routines(
    section: StepRoutines,
    rows: dict[tuple[str, ...], list[Row]] | None,
    *,
    job_id: str,
    database_name: str,
    engine: str,
    contract_sha: str,
    target_database: str | None,
    rows_read: int = 0,
    now: datetime | None = None,
) -> RoutineCheckResult:
    """The result for ``section`` from the parsed output ``rows`` (None when there was
    nothing to check: no routine carried or deferred)."""
    rows = rows or {}
    names: dict[tuple[str, ...], list[StepRoutine]] = {}
    for r in section.routines:
        names.setdefault(output_key(r, engine, target_database), []).append(r)
    mismatches: list[RoutineMismatch] = []
    not_carried: list[RoutineNotCarried] = []
    confirmed = 0
    for key, members in names.items():
        claimed = {k for m in members if (k := _signature_key(m, engine)) is not None}
        found = rows.get(key, [])
        for row in members:
            if row.carry == "not_carried":
                not_carried.append(
                    RoutineNotCarried(routine_id=row.routine_id, kind=row.kind, reasons=row.reasons)
                )
                continue
            others = claimed - ({_signature_key(row, engine)} - {None})
            problem = _compare(row, found, others, engine)
            if problem is not None:
                mismatches.append(
                    RoutineMismatch(routine_id=row.routine_id, kind=row.kind, problem=problem)  # type: ignore[arg-type]
                )
            elif row.carry == "carried":
                confirmed += 1
    mismatches.sort(key=lambda m: (m.kind, m.routine_id))
    not_carried.sort(key=lambda m: (m.kind, m.routine_id))
    return RoutineCheckResult(
        job_id=job_id,
        database_name=database_name,
        target_engine=engine,  # type: ignore[arg-type]
        contract_sha256=contract_sha,
        checked_at=now or datetime.now(UTC),
        status="fail" if mismatches or not_carried else "pass",
        carried=section.carried,
        confirmed=confirmed,
        deferred=section.deferred,
        rows_read=rows_read,
        mismatches=mismatches,
        not_carried=not_carried,
    )


def read_check(
    path: Path,
    contract: StepContract,
    *,
    job_id: str,
    database_name: str,
    contract_sha: str,
    target_database: str | None,
    now: datetime | None = None,
) -> RoutineCheckResult | None:
    """The result from the output file at ``path``; None when the section asks for a
    check and the file is not there yet. A section with nothing to check needs no file."""
    section = contract.routines
    if section is None:
        raise RoutineCheckOutputError(
            "the step 1 contract has no routines section (written before #1947)"
        )
    engine = contract.target.engine
    needs_file = section.carried > 0 or section.deferred > 0
    parsed: dict[tuple[str, ...], list[Row]] | None = None
    count = 0
    if needs_file:
        if not path.is_file():
            return None
        if path.stat().st_size > MAX_OUTPUT_BYTES:
            raise RoutineCheckOutputError(f"the check output is over {MAX_OUTPUT_BYTES} bytes")
        parsed = parse_check_output(
            path.read_bytes(),
            section,
            engine=engine,
            contract_sha=contract_sha,
            target_database=target_database,
        )
        count = sum(len(v) for v in parsed.values())
    return check_routines(
        section,
        parsed,
        job_id=job_id,
        database_name=database_name,
        engine=engine,
        contract_sha=contract_sha,
        target_database=target_database,
        rows_read=count,
        now=now,
    )
