# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Read the customer's output of step 1's view check script (#2082).

The file is untrusted input: the customer's machine wrote it. It is read as strictly as
the routine check's (``routine_check``), in the same layout, and refused whole on the
first problem, never guessed at:

- at most :data:`~src.tools.data_movement.routine_check.MAX_OUTPUT_BYTES`, UTF-8, one
  line per row, each exactly seven tab-separated fields (``view``, schema, view name,
  column position, column name, column type, the relation's kind), no control character
  in any field, each at most ``MAX_FIELD`` characters;
- the first line is the header ``views-check.sql`` prints: its format, this step's engine,
  the SHA-256 of the step contract it was generated from, and the target database; the
  last line is its end line (a file cut short has none);
- every other line names a view the check script asks for, a column position (a number,
  or empty for a relation with no column), and a relation kind the engine has; no line
  twice, no position twice for one view.

A refusal names the line number and what is wrong, never the line's content. Nothing of
the file is kept: the result (:class:`~src.contracts.view_check.ViewCheckResult`) holds
the contract's view ids and column names, fixed problem codes and counts.

Then each carried view is compared with its lines: it must be a view (PostgreSQL relkind
``v``, MySQL ``VIEW``) with the source's column names in the same order (MySQL compares
names without case, as it does) and, where the collection gave a type, the same type:
PostgreSQL's ``format_type`` on both sides; MySQL's ``COLUMN_TYPE`` in lower case with an
integer's display width dropped (``int(11)`` and ``int`` are one type). Linear in the
lines and the views.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from src.contracts.transformation_contract import StepContract, StepView, StepViews
from src.contracts.view_check import ViewCheckResult, ViewMismatch, ViewNotCarried
from src.tools.data_movement.routine_check import (
    MAX_FIELD,
    MAX_OUTPUT_BYTES,
    RoutineCheckOutputError,
    _clean,
    _header_problem,
)
from src.tools.data_movement.routine_scripts import CHECK_FIELDS, CHECK_FORMAT
from src.tools.data_movement.view_scripts import VIEWS_CHECK, _names_safe, check_key

__all__ = ["check_views", "parse_view_output", "read_view_check"]

_KINDS = {
    "aurora_postgresql": {"r", "v", "m", "p", "f", "S", "c", "t"},
    "aurora_mysql": {"VIEW", "BASE TABLE", "SYSTEM VIEW"},
}
_VIEW = {"aurora_postgresql": "v", "aurora_mysql": "VIEW"}
_POSITION = re.compile(r"[1-9][0-9]{0,5}")
_MYSQL_WIDTH = re.compile(r"^(tinyint|smallint|mediumint|int|integer|bigint)\(\d+\)")
_SHOWN = 20

Line = tuple[int | None, str, str, str]  # position, column name, type, relation kind


def parse_view_output(
    data: bytes,
    section: StepViews,
    *,
    engine: str,
    contract_sha: str,
    target_database: str | None,
) -> dict[tuple[str, str], list[Line]]:
    """The output's view lines, by :func:`~src.tools.data_movement.view_scripts.check_key`,
    after the strict reading of the module docstring."""
    spec = VIEWS_CHECK
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
    asked = {
        check_key(v, engine, target_database)
        for v in section.views
        if v.carry == "carried" and _names_safe(v)
    }
    rows: dict[tuple[str, str], list[Line]] = {}
    seen: set[tuple[tuple[str, str], int | None]] = set()
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
        kind, schema, name, position, column, type_, relkind = fields
        key = (schema.lower(), name.lower()) if engine == "aurora_mysql" else (schema, name)
        if kind != "view" or key not in asked:
            raise RoutineCheckOutputError(
                f"line {number}: names a view the step's check script does not ask for "
                "(a file from another step, job or script?)"
            )
        if relkind not in _KINDS[engine]:
            raise RoutineCheckOutputError(
                f"line {number}: a relation kind the engine does not report"
            )
        if position == "":
            if column or type_:
                raise RoutineCheckOutputError(f"line {number}: a column with no position")
            place: int | None = None
        elif _POSITION.fullmatch(position) and column:
            place = int(position)
        else:
            raise RoutineCheckOutputError(f"line {number}: a column position that is not a number")
        if (key, place) in seen:
            raise RoutineCheckOutputError(f"line {number}: the same view column line twice")
        seen.add((key, place))
        rows.setdefault(key, []).append((place, column, type_, relkind))
    if not end:
        raise RoutineCheckOutputError(
            f"the check output has no end line: it was cut short; run {spec.script} again"
        )
    return rows


def _type(text: str | None, engine: str) -> str | None:
    if text is None:
        return None
    folded = " ".join(text.split()).lower()
    if engine == "aurora_mysql":
        folded = _MYSQL_WIDTH.sub(
            lambda m: "int" if m.group(1) == "integer" else m.group(1), folded
        )
    return folded


def _compare(row: StepView, found: list[Line], engine: str) -> tuple[str, list[str]] | None:
    """The problem with a carried view and the columns it is about, or None when confirmed."""
    if not found:
        return "missing", []
    if any(f[3] != _VIEW[engine] for f in found):
        return "not_a_view", []
    have = sorted((f for f in found if f[0] is not None), key=lambda f: f[0] or 0)
    fold = (lambda s: s.lower()) if engine == "aurora_mysql" else (lambda s: s)
    want = [fold(c.name) for c in row.columns]
    if [fold(f[1]) for f in have] != want:
        names = [c.name for i, c in enumerate(row.columns)
                 if i >= len(have) or fold(have[i][1]) != want[i]]  # fmt: skip
        # A column the target has past the source's last: named by its position, never by
        # the file's text.
        names += [f"column {i + 1}, not on the source" for i in range(len(want), len(have))]
        return "columns_differ", names[:_SHOWN]
    differ = [
        c.name for c, f in zip(row.columns, have, strict=True)
        if c.data_type is not None and _type(c.data_type, engine) != _type(f[2], engine)
    ]  # fmt: skip
    return ("column_types_differ", differ[:_SHOWN]) if differ else None


def check_views(
    section: StepViews,
    rows: dict[tuple[str, str], list[Line]] | None,
    *,
    job_id: str,
    database_name: str,
    engine: str,
    contract_sha: str,
    rows_read: int = 0,
    now: datetime | None = None,
    target_database: str | None = None,
) -> ViewCheckResult:
    """The result for ``section`` from the parsed output ``rows``."""
    rows = rows or {}
    mismatches: list[ViewMismatch] = []
    not_carried: list[ViewNotCarried] = []
    confirmed = 0
    for view in section.views:
        if view.carry == "not_carried":
            not_carried.append(ViewNotCarried(view_id=view.view_id, reasons=view.reasons))
            continue
        problem = _compare(view, rows.get(check_key(view, engine, target_database), []), engine)
        if problem is None:
            confirmed += 1
        else:
            mismatches.append(ViewMismatch(view_id=view.view_id, problem=problem[0],  # type: ignore[arg-type]
                                           columns=problem[1]))  # fmt: skip
    mismatches.sort(key=lambda m: m.view_id)
    not_carried.sort(key=lambda m: m.view_id)
    return ViewCheckResult(
        job_id=job_id,
        database_name=database_name,
        target_engine=engine,  # type: ignore[arg-type]
        contract_sha256=contract_sha,
        checked_at=now or datetime.now(UTC),
        status="fail" if mismatches else "pass",
        carried=section.carried,
        confirmed=confirmed,
        rows_read=rows_read,
        mismatches=mismatches,
        not_carried=not_carried,
    )


def read_view_check(
    path: Path,
    contract: StepContract,
    *,
    job_id: str,
    database_name: str,
    contract_sha: str,
    target_database: str | None,
    now: datetime | None = None,
) -> ViewCheckResult | None:
    """The result from the output file at ``path``; None when the section carries a view
    and the file is not there yet. A section that carries none needs no file."""
    section = contract.views_section
    if section is None:
        raise RoutineCheckOutputError(
            "the step 1 contract has no views section (written before #2082)"
        )
    engine = contract.target.engine
    parsed = None
    count = 0
    if section.carried:
        if not path.is_file():
            return None
        if path.stat().st_size > MAX_OUTPUT_BYTES:
            raise RoutineCheckOutputError(f"the check output is over {MAX_OUTPUT_BYTES} bytes")
        parsed = parse_view_output(path.read_bytes(), section, engine=engine,
                                   contract_sha=contract_sha, target_database=target_database)  # fmt: skip
        count = sum(len(v) for v in parsed.values())
    return check_views(section, parsed, job_id=job_id, database_name=database_name, engine=engine,
                       contract_sha=contract_sha, rows_read=count, now=now,
                       target_database=target_database)  # fmt: skip
