# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Read the customer's output of the triggers-enable check script (#1948, renamed in #2113).

The file is untrusted input: the customer's machine wrote it. It is read exactly as step
1's routine check output is (:func:`~src.tools.data_movement.routine_check.parse_check_output`:
size, UTF-8, seven tab-separated fields per line, no control character, the header and end
lines, only the routines the script asks for, each once), under the triggers-enable check's own
header and end lines, so neither script's output passes for the other's. A refusal names
the line number and what is wrong, never the line's content; nothing of the file is kept.

Then each routine the triggers-enable script acts on is compared with the rows of its name:

- Aurora PostgreSQL: each trigger step 1 carried exists in its source state (``tgenabled``
  equal to the contract's ``source_state``: O, A, R, or D for one that stays disabled; O or
  D for a state the collection gives only as enabled or disabled, #2099).
- Aurora MySQL: each trigger step 1 deferred exists; each event exists with the status its
  source ``CREATE EVENT`` gives (:func:`~src.tools.data_movement.triggers_scripts.event_state`,
  read from the routine sources one chunk at a time; only the status leaves the text); and
  when one is created enabled, ``@@GLOBAL.event_scheduler`` (the check's setting line) is ON.

A trigger or event step 1 does not carry, or one the triggers-enable script leaves out for a
reason the contract shows (:func:`~src.tools.data_movement.triggers_scripts.left_out_rows`),
fails the result, named with its reasons. Linear in the lines and the routines.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.agents.collector.routine_sources import read_routine_sources
from src.contracts.routine_check import RoutineNotCarried
from src.contracts.transformation_contract import StepContract, StepRoutine
from src.contracts.triggers_enable_check import (
    TriggersEnableCheckResult,
    TriggersEnableLeftOut,
    TriggersEnableMismatch,
)
from src.tools.data_movement.routine_check import (
    MAX_OUTPUT_BYTES,
    RoutineCheckOutputError,
    Row,
    parse_check_output,
)
from src.tools.data_movement.routine_scripts import CheckScript, output_key
from src.tools.data_movement.triggers_scripts import (
    EVENT_SCHEDULER,
    LEGACY_CHECK,
    SCHEDULER_VALUES,
    TARGET_STATE,
    TRIGGERS_CHECK,
    TRIGGERS_CHECK_OUTPUT,
    event_state,
    excluded_rows,
    expected_rows,
    left_out_rows,
)

__all__ = [
    "check_output",
    "check_triggers",
    "event_states",
    "parse_triggers_output",
    "read_triggers_check",
]


def check_output(step_dir: Path) -> Path:
    """The check output to read in ``step_dir``: ``triggers-enable-check.tsv``, or, when
    only that one is there, the output a job from before #2113 saved under the old name
    (``LEGACY_CHECK.output``)."""
    new, old = step_dir / TRIGGERS_CHECK_OUTPUT, step_dir / LEGACY_CHECK.output
    return old if not new.is_file() and old.is_file() else new


def _spec(data: bytes) -> CheckScript:
    """The check whose header the output starts with: a script generated before #2113
    prints the old one (``LEGACY_CHECK``)."""
    legacy = data.startswith(LEGACY_CHECK.magic.encode("utf-8") + b"\t")
    return LEGACY_CHECK if legacy else TRIGGERS_CHECK


#: The statuses information_schema.EVENTS reports for each status a CREATE EVENT gives.
_EVENT_STATUSES = {
    "ENABLED": {"ENABLED"},
    "DISABLED": {"DISABLED"},
    "REPLICA_SIDE_DISABLED": {"REPLICA_SIDE_DISABLED", "SLAVESIDE_DISABLED"},
}


def event_states(
    store: Any, db: str, job_id: str, rows: list[StepRoutine]
) -> dict[tuple[str, str], str | None]:
    """The status each MySQL event of ``rows`` is created with, from its exact text in the
    routine sources (absent when the source has no text for it)."""
    wanted: dict[tuple[str, str, str], tuple[str, str]] = {
        (r.kind, r.schema_name or "", r.specific_name or ""): (r.kind, r.routine_id)
        for r in rows
        if r.kind == "event"
    }
    out: dict[tuple[str, str], str | None] = {}
    if not wanted:
        return out
    for source in read_routine_sources(store, db, job_id):
        key = wanted.get(source.key())
        if key is not None and source.exact:
            out[key] = event_state(source.create_statement, source.session.sql_mode)
    return out


def _problem(
    row: StepRoutine, found: list[Row], engine: str, states: dict[tuple[str, str], str | None]
) -> str | None:
    if not found:
        return "missing"
    if engine == "aurora_postgresql":
        want = TARGET_STATE.get(row.source_state or "")
        return None if all(r[6] == want for r in found) else "trigger_state_differs"
    if row.kind == "event":
        state = states.get((row.kind, row.routine_id))
        allowed = _EVENT_STATUSES.get(state or "")
        if allowed is not None and not all(r[6] in allowed for r in found):
            return "event_status_differs"
    return None


def check_triggers(
    contract: StepContract,
    rows: dict[tuple[str, ...], list[Row]] | None,
    states: dict[tuple[str, str], str | None],
    *,
    job_id: str,
    database_name: str,
    contract_sha: str,
    target_database: str | None,
    rows_read: int = 0,
    now: datetime | None = None,
) -> TriggersEnableCheckResult:
    """The result for the contract's triggers and events to turn on, from the parsed output ``rows``
    (None when there was nothing to check) and the events' source ``states``."""
    section = contract.routines
    assert section is not None  # nosec B101 -- read_triggers_check refuses a contract without one
    engine = contract.target.engine
    rows = rows or {}
    expected = expected_rows(section, engine)
    mismatches: list[TriggersEnableMismatch] = []
    for row in expected:
        found = rows.get(output_key(row, engine, target_database), [])
        problem = _problem(row, found, engine, states)
        if problem is not None:
            mismatches.append(
                TriggersEnableMismatch(routine_id=row.routine_id, kind=row.kind, problem=problem)  # type: ignore[arg-type]
            )
    mismatches.sort(key=lambda m: (m.kind, m.routine_id))
    left_out = sorted(
        (
            TriggersEnableLeftOut(routine_id=r.routine_id, kind=r.kind, reason=reason)  # type: ignore[arg-type]
            for r, reason in left_out_rows(section, engine)
        ),
        key=lambda m: (m.kind, m.routine_id),
    )
    scheduled = sum(
        1 for r in expected if r.kind == "event" and states.get((r.kind, r.routine_id)) == "ENABLED"
    )
    setting = rows.get((EVENT_SCHEDULER,))
    scheduler = setting[0][6] if setting else None
    not_carried = sorted(
        (
            RoutineNotCarried(routine_id=r.routine_id, kind=r.kind, reasons=r.reasons)
            for r in excluded_rows(section, engine)
        ),
        key=lambda m: (m.kind, m.routine_id),
    )
    return TriggersEnableCheckResult(
        job_id=job_id,
        database_name=database_name,
        target_engine=engine,  # type: ignore[arg-type]
        contract_sha256=contract_sha,
        checked_at=now or datetime.now(UTC),
        status=(
            "fail"
            if mismatches or not_carried or left_out or (scheduled and scheduler != "ON")
            else "pass"
        ),
        expected=len(expected),
        confirmed=len(expected) - len(mismatches),
        rows_read=rows_read,
        mismatches=mismatches,
        not_carried=not_carried,
        left_out=left_out,
        events_scheduled=scheduled,
        event_scheduler=scheduler,  # type: ignore[arg-type]
    )


def parse_triggers_output(
    path: Path, contract: StepContract, contract_sha: str, target_database: str | None
) -> dict[tuple[str, ...], list[Row]]:
    """The triggers-enable check output at ``path``, read strictly (module docstring): its routine
    lines by check key, and its event scheduler line under ``(EVENT_SCHEDULER,)``. A
    script with events to check prints that line, so an output without it is refused."""
    section = contract.routines
    if section is None:
        raise RoutineCheckOutputError(
            "the step 1 contract has no routines section (written before #1947)"
        )
    engine = contract.target.engine
    expected = expected_rows(section, engine)
    if path.stat().st_size > MAX_OUTPUT_BYTES:
        raise RoutineCheckOutputError(f"the check output is over {MAX_OUTPUT_BYTES} bytes")
    data = path.read_bytes()
    spec = _spec(data)
    parsed = parse_check_output(
        data,
        section,
        engine=engine,
        contract_sha=contract_sha,
        target_database=target_database,
        asked=expected,
        spec=spec,
        settings={EVENT_SCHEDULER: SCHEDULER_VALUES} if engine == "aurora_mysql" else None,
    )
    if any(r.kind == "event" for r in expected) and (EVENT_SCHEDULER,) not in parsed:
        raise RoutineCheckOutputError(
            f"the check output has no {EVENT_SCHEDULER} line: run {spec.script} again"
        )
    return parsed


def read_triggers_check(
    path: Path,
    contract: StepContract,
    store: Any,
    *,
    job_id: str,
    database_name: str,
    contract_sha: str,
    target_database: str | None,
    now: datetime | None = None,
) -> TriggersEnableCheckResult | None:
    """The result from the output file at ``path``; None when the script acts on some
    routine and the file is not there yet. With nothing to act on, no file is needed."""
    section = contract.routines
    if section is None:
        raise RoutineCheckOutputError(
            "the step 1 contract has no routines section (written before #1947)"
        )
    engine = contract.target.engine
    expected = expected_rows(section, engine)
    parsed: dict[tuple[str, ...], list[Row]] | None = None
    count = 0
    states: dict[tuple[str, str], str | None] = {}
    if expected:
        if not path.is_file():
            return None
        parsed = parse_triggers_output(path, contract, contract_sha, target_database)
        count = sum(len(v) for k, v in parsed.items() if k != (EVENT_SCHEDULER,))
        states = event_states(store, database_name, job_id, expected)
    return check_triggers(
        contract,
        parsed,
        states,
        job_id=job_id,
        database_name=database_name,
        contract_sha=contract_sha,
        target_database=target_database,
        rows_read=count,
        now=now,
    )
