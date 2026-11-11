# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The triggers-enable scripts (#1948, renamed in #2113; ADR-034 Decision 6): generated
here, run by the customer. They turn on the triggers and events step 1 left off.

The modernizer never runs these scripts, gives no cutover advice and never connects to the
customer's databases (ADR-033 Decision 11, the maintainer decisions of 2026-11-06 on #1948,
2026-11-10 on #2062 and 2026-11-11 on #2113). ``scripts/run_dms_step.py triggers-scripts``
writes three files into ``data-movement/step-1/``, from the step contract's routines section:

- ``triggers-enable.sql``: on Aurora PostgreSQL, each trigger step 1's deploy script
  created disabled is set to its source state (``source_state``: O ``ENABLE TRIGGER``, A
  ``ENABLE ALWAYS TRIGGER``, R ``ENABLE REPLICA TRIGGER``, D stays disabled), in deploy
  order, in one transaction. A collection that records only ``enabled`` or ``disabled``
  (#2099) gets ``ENABLE TRIGGER`` or stays disabled, and the files say so: an always or
  replica-only trigger is then not told apart, so ``triggers.md`` gives the read-only source
  query that lists them for the customer to check by hand. On Aurora MySQL, the ``CREATE TRIGGER`` and ``CREATE EVENT``
  of each routine step 1 left to this script (``deferred_to_cutover``), in the
  routine-migration artifact's dependency order, triggers of one table, timing and event
  in their source ``ACTION_ORDER`` (creation order is firing order), each the exact text
  from the routine sources with the routine-migration rewrites applied again for the user
  who runs it and under its own ``sql_mode`` and collation, exactly as step 1's deploy
  script writes a routine (:func:`~src.tools.data_movement.routine_scripts.block_of`).
  Each MySQL statement commits on its own: after a run that stopped part way, ``--resume``
  writes ``triggers-enable-resume.sql`` with only the ones the check output shows missing.
- ``triggers-enable-check.sql``: read-only, the same catalog ``SELECT``\\s as step 1's routine
  check, asking only for those triggers and events, under its own header and end lines;
  on Aurora MySQL with events, also ``@@GLOBAL.event_scheduler``.
- ``triggers.md``: what each file does, how to run them, in what order, and where the
  check's output goes (``triggers-enable-check.tsv``, which ``run_dms_step.py triggers-check``
  reads). It says nothing about when: it opens with ``CUTOVER_DISCLAIMER``, its only word
  on the cutover.

There is no rollback section: nothing here disables a trigger again or drops a routine.

A trigger or event the triggers-enable script cannot act on is left out and named, by id and reason code,
in the script's header and on the printed line: one step 1 does not carry (its contract
reasons); one whose source state (PostgreSQL, ``trigger_state_unknown``) or firing order
(two or more MySQL triggers on one table, timing and event without a full
``ACTION_ORDER``, ``trigger_order_unknown``) the collection does not give, never guessed;
or one step 1's deploy script left out (a PostgreSQL trigger it never created) or this
script cannot write (the same reason codes as the deploy script). The triggers-enable
script is, with step 1's deploy script, the only file outside the routine sources that holds a
routine's text; the text never goes into a JSON artifact, a log, a printed line or a
report, and no hash of it is kept. Linear in the routines; the sources are read one chunk
at a time.
"""

from __future__ import annotations

import re
import tempfile
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.contracts.routine_migration import RoutineMigrationContract
from src.contracts.routine_sources import RoutineSource
from src.contracts.transformation_contract import StepContract, StepRoutine, StepRoutines
from src.shared.cutover_message import CUTOVER_DISCLAIMER
from src.tools.data_movement.routine_scripts import (
    PG_LOCK_TIMEOUT,
    CheckScript,
    RoutineScriptError,
    _comment,
    _pg_ident,
    _table_name,
    block_of,
    output_key,
    read_migration,
    render_check_script,
    require_section,
    spool_blocks,
    spooled_chunks,
    write_atomic,
)

__all__ = [
    "TRIGGERS_CHECK",
    "TRIGGERS_CHECK_FILE",
    "TRIGGERS_CHECK_OUTPUT",
    "TRIGGERS_FILE",
    "TRIGGERS_INSTRUCTIONS",
    "EVENT_SCHEDULER",
    "LEGACY_CHECK",
    "RESUME_FILE",
    "TriggersScripts",
    "event_state",
    "excluded_rows",
    "expected_rows",
    "left_out_rows",
    "write_triggers_scripts",
]

TRIGGERS_FILE = "triggers-enable.sql"
TRIGGERS_CHECK_FILE = "triggers-enable-check.sql"
TRIGGERS_CHECK_OUTPUT = "triggers-enable-check.tsv"
TRIGGERS_INSTRUCTIONS = "triggers.md"
RESUME_FILE = "triggers-enable-resume.sql"
TRIGGERS_CHECK = CheckScript(
    "modernizer-triggers-enable-check",
    "modernizer-triggers-enable-check-end",
    "Triggers-enable check",
    TRIGGERS_CHECK_FILE,
    TRIGGERS_CHECK_OUTPUT,
    "triggers-scripts",
)
#: The check #1948 shipped, before the rename (#2113): an output its script printed, saved
#: under its old name, is still read (a legacy alias, like the old subcommand names).
LEGACY_CHECK = CheckScript(
    "modernizer-cutover-check",  # legacy alias (#2113)
    "modernizer-cutover-check-end",  # legacy alias (#2113)
    "Triggers-enable check",
    "cutover-check.sql",  # legacy alias (#2113)
    "cutover-check.tsv",  # legacy alias (#2113)
    "triggers-scripts",
)
_FILES = (TRIGGERS_FILE, TRIGGERS_CHECK_FILE, TRIGGERS_INSTRUCTIONS)
#: The kinds the triggers-enable script acts on, per target.
TRIGGER_KINDS = {"aurora_postgresql": ("trigger",), "aurora_mysql": ("trigger", "event")}
#: How the triggers-enable script sets a PostgreSQL trigger's source state (D: it stays disabled).
STATE_CLAUSE = {
    "O": "ENABLE TRIGGER",
    "A": "ENABLE ALWAYS TRIGGER",
    "R": "ENABLE REPLICA TRIGGER",
    "enabled": "ENABLE TRIGGER",  # the collection records only enabled or disabled (#2099)
}
#: The tgenabled each source state leaves on the target, which the check compares.
TARGET_STATE = {"O": "O", "A": "A", "R": "R", "D": "D", "enabled": "O", "disabled": "D"}
#: The source states the collection gave only as enabled or disabled (#2099).
FOLDED = ("enabled", "disabled")
#: Read-only, for the customer to run on the PostgreSQL source: the triggers whose
#: always-fire or replica-only state a collection that records only enabled or disabled
#: does not show.
SOURCE_ALWAYS_REPLICA_QUERY = (
    "SELECT n.nspname, c.relname, t.tgname, t.tgenabled FROM pg_catalog.pg_trigger t "
    "JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid "
    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
    "WHERE NOT t.tgisinternal AND t.tgenabled IN ('A', 'R') ORDER BY 1, 2, 3;"
)
_FOLDED_NOTE = (
    "the collection records only enabled or disabled; an always-fire or replica-only trigger "
    "on the source is enabled as an ordinary trigger or left disabled: check those by hand"
)
#: The check output's setting line for the MySQL event scheduler, and its values.
EVENT_SCHEDULER = "event_scheduler"
SCHEDULER_VALUES = frozenset({"ON", "OFF", "DISABLED"})
_EVENT_SCHEDULER_SELECT = (
    "SELECT 'event_scheduler', '', '', '', '', '', UPPER(@@GLOBAL.event_scheduler);"
)


@dataclass
class TriggersScripts:
    """What :func:`write_triggers_scripts` wrote: counts, files and exclusions, no text."""

    status: str  # "ready" or "nothing_to_do"
    expected: int = 0  # triggers to set (PostgreSQL), triggers and events to create (MySQL)
    in_script: int = 0
    stays_disabled: int = 0  # PostgreSQL triggers disabled on the source: left disabled
    state_folded: int = 0  # PostgreSQL triggers whose source state is only enabled or disabled
    events_enabled: int = 0  # MySQL events the script creates enabled: they fire on schedule
    events_replica_disabled: int = 0  # MySQL events created DISABLE ON REPLICA: they never run
    already_there: int = 0  # --resume: routines the check output shows on the target
    excluded: list[tuple[str, str, str]] = field(default_factory=list)  # kind, id, reason
    files: dict[str, str] = field(default_factory=dict)


def _candidates(section: StepRoutines, engine: str) -> list[StepRoutine]:
    if engine == "aurora_postgresql":
        return [r for r in section.routines if r.carry == "carried" and r.kind == "trigger"]
    return [r for r in section.routines if r.carry == "deferred_to_cutover"]


def _table_of(row: StepRoutine) -> tuple[str, str]:
    return ((row.schema_name or "").lower(), (_table_name(row) or "").lower())


def left_out_rows(section: StepRoutines, engine: str) -> list[tuple[StepRoutine, str]]:
    """The routines the triggers-enable script would act on but leaves out, never guessing, with the
    reason the contract shows: a PostgreSQL trigger whose source state the collection does
    not give (``trigger_state_unknown``); two or more MySQL triggers on one table, timing
    and event without a distinct ``ACTION_ORDER`` each, or on a table where one trigger's
    timing and event are not known (``trigger_order_unknown``)."""
    rows = _candidates(section, engine)
    if engine == "aurora_postgresql":
        return [(r, "trigger_state_unknown") for r in rows if r.source_state is None]
    by_table: dict[tuple[str, str], list[StepRoutine]] = defaultdict(list)
    for row in rows:
        if row.kind == "trigger":
            by_table[_table_of(row)].append(row)
    out: list[tuple[StepRoutine, str]] = []
    for members in by_table.values():
        if len(members) < 2:
            continue
        if any(m.fires is None for m in members):
            out += [(m, "trigger_order_unknown") for m in members]
            continue
        groups: dict[str, list[StepRoutine]] = defaultdict(list)
        for m in members:
            groups[str(m.fires)].append(m)
        for group in groups.values():
            orders = [m.action_order for m in group]
            if len(group) > 1 and (None in orders or len(set(orders)) != len(orders)):
                out += [(m, "trigger_order_unknown") for m in group]
    return out


def expected_rows(section: StepRoutines, engine: str) -> list[StepRoutine]:
    """The routines the triggers-enable script acts on, in the section's order: the PostgreSQL triggers
    step 1 carried (created disabled), or the MySQL triggers and events it deferred, less
    the ones :func:`left_out_rows` names."""
    left = {(r.kind, r.routine_id) for r, _ in left_out_rows(section, engine)}
    return [r for r in _candidates(section, engine) if (r.kind, r.routine_id) not in left]


def excluded_rows(section: StepRoutines, engine: str) -> list[StepRoutine]:
    """The triggers (and MySQL events) step 1 does not carry: the triggers-enable script cannot act on
    them, and each fails the check until its reasons are settled."""
    kinds = TRIGGER_KINDS.get(engine, ())
    return [r for r in section.routines if r.carry == "not_carried" and r.kind in kinds]


# --------------------------------------------------------------------------- MySQL events

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")


def _header_words(text: str, backslash_escapes: bool) -> list[str] | None:
    """The upper-case words of a ``CREATE EVENT`` before its top-level ``DO``, each quoted
    string or identifier one ``?`` and comments skipped; None when there is no ``DO``."""
    words: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in "'\"`":
            j = i + 1
            while j < n:
                if text[j] == "\\" and c != "`" and backslash_escapes:
                    j += 2
                    continue
                if text[j] == c:
                    if j + 1 < n and text[j + 1] == c:
                        j += 2
                        continue
                    break
                j += 1
            words.append("?")
            i = j + 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        elif c == "#" or (text.startswith("--", i) and (i + 2 >= n or text[i + 2] in " \t\n")):
            end = text.find("\n", i)
            i = n if end < 0 else end + 1
        elif match := _WORD.match(text, i):
            word = match.group(0).upper()
            if word == "DO":
                return words
            words.append(word)
            i = match.end()
        else:
            i += 1
    return None


def event_state(create_sql: str, sql_mode: str | None = None) -> str | None:
    """The status a ``CREATE EVENT`` gives the event, as ``information_schema.EVENTS``
    reports it: ``ENABLED`` (``ENABLE``, or no clause), ``DISABLED`` or
    ``REPLICA_SIDE_DISABLED`` (``DISABLE ON SLAVE`` or ``DISABLE ON REPLICA``, which older
    servers report as ``SLAVESIDE_DISABLED``); None when the text has no ``DO``. Only this
    status leaves the text."""
    escapes = "NO_BACKSLASH_ESCAPES" not in (sql_mode or "").upper()
    words = _header_words(create_sql, escapes)
    if words is None:
        return None
    if "SCHEDULE" in words:
        words = words[words.index("SCHEDULE") + 1 :]
    for i, word in enumerate(words):
        if word == "ENABLE":
            return "ENABLED"
        if word == "DISABLE":
            on = words[i + 1 : i + 3]
            return "REPLICA_SIDE_DISABLED" if on[:1] == ["ON"] else "DISABLED"
    return "ENABLED"


# --------------------------------------------------------------------------- the files


def _ordered(rows: list[StepRoutine], migration: RoutineMigrationContract) -> list[StepRoutine]:
    """``rows`` in the routine-migration artifact's deploy (dependency) order, then the
    MySQL triggers of one table, timing and event moved into their ``ACTION_ORDER``
    within the places the group holds (MySQL fires them in creation order)."""
    place = {(r.kind, r.routine_id): i for i, r in enumerate(migration.routines)}
    out = sorted(rows, key=lambda r: (place.get((r.kind, r.routine_id), len(place)), r.routine_id))
    groups: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    for i, row in enumerate(out):
        if row.kind == "trigger" and row.fires is not None:
            groups[(*_table_of(row), row.fires)].append(i)
    for slots in groups.values():
        members = sorted((out[i] for i in slots), key=lambda r: r.action_order or 0)
        for i, row in zip(slots, members, strict=True):
            out[i] = row
    return out


# What the script's triggers do once they are on: facts about the files, never when to run them.
_PG_EFFECT = (
    "Triggers fire once they are on.",
    "DMS applies its changes with session_replication_role = replica, so an O trigger does not "
    "fire for DMS's own writes, but an ALWAYS (A) or REPLICA (R) trigger does.",
)
_MYSQL_EFFECT = (
    "A MySQL trigger fires for DMS's writes too.",
    "While DMS applies source changes, a trigger this script created on Aurora applies the "
    "source trigger's effects a second time.",
)


def _pg_set_to(scripts: TriggersScripts) -> str:
    """How the header says the triggers are set: their source state only when the
    collection gives every one's exact letter (#2099)."""
    if not scripts.state_folded:
        return " to their source state"
    if scripts.state_folded == scripts.in_script:
        return " enabled or disabled as the collection records"
    exact = scripts.in_script - scripts.state_folded
    return (
        f": {exact} to their source state, {scripts.state_folded} enabled or disabled as "
        "the collection records"
    )


def _header(engine: str, contract_sha: str, scripts: TriggersScripts, resume: bool) -> list[str]:
    pg = engine == "aurora_postgresql"
    what = (
        f"sets the {scripts.in_script} triggers step 1 created disabled{_pg_set_to(scripts)}; "
        f"{scripts.stays_disabled} of them stay disabled"
        if pg
        else f"creates the {scripts.in_script} MySQL triggers and events step 1 left to this script"
    )
    lines = [
        f"-- {CUTOVER_DISCLAIMER}",
        "-- Triggers enable: turns on the triggers and events step 1 left off. Generated by "
        "the modernizer, which never connects to your databases.",
        f"-- Step contract SHA-256: {contract_sha}",
        f"-- This script {what}; {scripts.expected} expected, in dependency order.",
        f"-- {' '.join(_PG_EFFECT if pg else _MYSQL_EFFECT)}",
    ]
    if pg and scripts.state_folded:
        lines.append(
            f"-- {scripts.state_folded} of them: {_FOLDED_NOTE}, with the query in "
            f"{TRIGGERS_INSTRUCTIONS}."
        )
    if resume:
        lines.append(
            f"-- Resume: {scripts.already_there} are on the target already, as "
            f"{TRIGGERS_CHECK_OUTPUT} shows; only the others are here, in the same order."
        )
    if not pg:
        lines.append(
            "-- An event runs on its schedule as soon as it is created, when event_scheduler "
            f"is ON: {scripts.events_enabled} of the events here are created enabled."
        )
    lines += [
        "-- No rollback: nothing here disables a trigger again or drops a routine.",
        f"-- Then run {TRIGGERS_CHECK_FILE} (read-only) as {TRIGGERS_INSTRUCTIONS} says.",
    ]
    lines += [
        f"-- Left out: {kind} {_comment(rid)} ({_comment(reason)})"
        for kind, rid, reason in scripts.excluded
    ]
    if pg:
        lines += [
            "-- One transaction: if any statement fails, no trigger changes; fix it and run "
            "the script again.",
            "",
            "\\set ON_ERROR_STOP on",
            "SET client_encoding = 'UTF8';",
            "BEGIN;",
            f"SET LOCAL lock_timeout = '{PG_LOCK_TIMEOUT}';",
            "",
        ]
    else:
        lines += [
            "-- MySQL commits each CREATE on its own and the client stops at the first error: "
            f"{TRIGGERS_INSTRUCTIONS} says how to resume.",
            "",
            "SET NAMES utf8mb4;",
            "",
        ]
    return lines


def _pg_state(row: StepRoutine, position: int, total: int) -> str:
    line = f"-- {position}/{total} trigger {_comment(row.routine_id)}"
    state = row.source_state or ""
    clause = STATE_CLAUSE.get(state)
    if clause is None:  # D or disabled: it stays disabled
        why = "disabled on the source" if state in FOLDED else "as on the source"
        return f"{line}: stays disabled, {why}\n"
    label = "enabled on the source" if state in FOLDED else f"source state {state}"
    table = _table_name(row) or ""
    return (
        f"{line} ({label})\n"
        f"ALTER TABLE {_pg_ident(row.schema_name or '')}.{_pg_ident(table)} "
        f"{clause} {_pg_ident(row.name)};\n"
    )


def _instructions(engine: str, scripts: TriggersScripts, output: str) -> str:
    pg = engine == "aurora_postgresql"

    def mysql(script: str) -> str:
        return (
            "mysql --host=<Aurora writer endpoint> --user=<user> -p "
            f"--default-character-set=utf8mb4 < {script}"
        )

    run = (
        'psql "host=<Aurora writer endpoint> port=5432 dbname=<database> user=<user> '
        f'sslmode=verify-full" -X -f {TRIGGERS_FILE}'
        if pg
        else mysql(TRIGGERS_FILE)
    )
    check = (
        'psql "host=<Aurora writer endpoint> port=5432 dbname=<database> user=<user> '
        f'sslmode=verify-full" -X -f {TRIGGERS_CHECK_FILE} -o {TRIGGERS_CHECK_OUTPUT}'
        if pg
        else "mysql --host=<Aurora writer endpoint> --user=<user> -p --batch --skip-column-names "
        f"< {TRIGGERS_CHECK_FILE} > {TRIGGERS_CHECK_OUTPUT}"
    )
    what = (
        f"sets {scripts.in_script} of the {scripts.expected} triggers step 1 created disabled "
        f"to their state on the source, as the collection gives it: {scripts.stays_disabled} "
        "stay disabled, and each other one is enabled (`ENABLE`, or `ENABLE ALWAYS` or "
        "`ENABLE REPLICA` where the collection gives that state)"
        if pg
        else f"creates {scripts.in_script} of the {scripts.expected} MySQL triggers and events "
        "step 1 left to this script; triggers of one table, timing and event in their source order"
    )
    effect = _PG_EFFECT if pg else _MYSQL_EFFECT
    lines = [
        CUTOVER_DISCLAIMER,
        "",
        "# Turn on the triggers and events step 1 left off",
        "",
        "The modernizer never connects to your databases: you run these scripts on your Aurora "
        "writer, with your own credentials.",
        "",
        f"- `{TRIGGERS_FILE}` {what}, in dependency order.",
        f"- Left out: {len(scripts.excluded)} (`run_dms_step.py triggers-scripts` and the "
        "script's header name each one and why). A trigger or event left out, or one step 1 "
        "does not carry, fails the check until its reasons are settled.",
        "- No rollback: there is no script that disables the triggers again or drops what "
        "this one creates.",
        "",
        "## Warnings",
        "",
        f"- **{effect[0]}** {effect[1]}",
    ]
    if pg and scripts.state_folded:
        lines.append(
            f"- **Always-fire and replica-only triggers:** for {scripts.state_folded} of the "
            f"triggers, {_FOLDED_NOTE}. Run this read-only query on the source to list them, "
            "and set each one listed yourself on Aurora (`ALTER TABLE ... ENABLE ALWAYS "
            "TRIGGER ...` or `ENABLE REPLICA TRIGGER`):\n\n"
            f"  ```sql\n  {SOURCE_ALWAYS_REPLICA_QUERY}\n  ```"
        )
    if not pg:
        lines.append(
            "- **MySQL events fire on their schedule once created**, when the cluster's "
            f"`event_scheduler` is ON: {scripts.events_enabled} of the events this script "
            "creates are enabled, as on the source. "
            f"{scripts.events_replica_disabled} are created `DISABLE ON SLAVE` (or `DISABLE "
            "ON REPLICA`), as the source shows them (usually a collection read from a replica): "
            "they never run on Aurora; enable each one yourself if it should."
        )
    steps: list[str] = []
    if pg:
        steps.append("Run `step-1.sequences.sql` first, as the data-movement README says.")
    elif scripts.events_enabled:
        steps.append(
            "Make sure the cluster's event scheduler is ON: `event_scheduler` is a parameter "
            "of your Aurora DB cluster parameter group; set it to `ON` there (your change to "
            "your cluster). The check fails while it is OFF."
        )
    steps += [
        f"Run the triggers-enable script once:\n\n   ```\n   {run}\n   ```",
        "Run the read-only check script and save its output next to these files, at "
        f"`{output}`:\n\n   ```\n   {check}\n   ```",
        "Ask `/move` to read the check (`run_dms_step.py triggers-check`); it reads the file "
        "and nothing else.",
    ]
    if not pg:
        steps.append(
            "If the triggers-enable script stopped on an error: MySQL kept every CREATE before it. Fix "
            "the cause, run the check script again (as above), then `run_dms_step.py "
            f"triggers-scripts --resume`: it writes `{RESUME_FILE}` with only the triggers and "
            "events the check output shows missing, in the same order (it refuses, naming the "
            "triggers, when a trigger is on Aurora while one that fires before it on the same "
            "table, timing and event is not). Run it the same way "
            f"(`{mysql(RESUME_FILE)}`), then the check again; repeat until the check passes."
        )
    lines += ["", "## Order", ""]
    lines += [f"{i}. {text}" for i, text in enumerate(steps, start=1)]
    lines += ["", "Step 1 does not move the views, or the routines its routines section does not "
              "carry: recreating them on Aurora is yours.", ""]  # fmt: skip
    if pg:
        lines.append(
            "The triggers-enable script runs in one transaction: if a statement fails, no trigger "
            "changes; fix the cause and run it again. On a lock timeout, run it again as it is."
        )
    else:
        lines.append(
            "Its DEFINER clauses name the user given with `run_dms_step.py triggers-scripts "
            "--target-user` (else the routine-migration artifact's); run it as that user."
        )
    return "\n".join(lines) + "\n"


def _resumable(rows: list[StepRoutine], there: list[StepRoutine]) -> None:
    """Refuse a resume that cannot keep a MySQL trigger group's firing order: a trigger of
    one table, timing and event is on the target while one that fires before it is not, so
    creating the missing one now would make it fire after (MySQL fires in creation order)."""
    present = {(r.kind, r.routine_id) for r in there}
    groups: dict[tuple[str, str, str], list[StepRoutine]] = defaultdict(list)
    for row in rows:
        if row.kind == "trigger" and row.fires is not None and row.action_order is not None:
            groups[(*_table_of(row), row.fires)].append(row)
    for members in groups.values():
        missing = [m for m in members if (m.kind, m.routine_id) not in present]
        later = [m for m in members if (m.kind, m.routine_id) in present]
        if (
            missing
            and later
            and max(m.action_order or 0 for m in later) > min(m.action_order or 0 for m in missing)
        ):
            names = ", ".join(sorted(m.routine_id for m in members))
            raise RoutineScriptError(
                f"cannot resume: of the triggers {names} (one table, timing and event), one is on "
                "the target while one that fires before it is not, and creating that one now "
                "would make it fire later; drop the later ones on Aurora, run the check again, "
                "then resume"
            )


def write_triggers_scripts(
    store: Any,
    db: str,
    job_id: str,
    contract: StepContract,
    contract_sha: str,
    step_dir: Path,
    *,
    target_database: str | None = None,
    target_user: str | None = None,
    present: set[tuple[str, ...]] | None = None,
) -> TriggersScripts:
    """Write the triggers-enable scripts and instructions into ``step_dir`` (module
    docstring). ``nothing_to_do`` (and no file) when they act on no routine. With ``present``
    (the check-output keys of the routines already on the target, Aurora MySQL only),
    write only ``triggers-enable-resume.sql``, without them."""
    section = require_section(contract)
    engine = contract.target.engine
    pg = engine == "aurora_postgresql"
    if present is not None and pg:
        raise RoutineScriptError(
            "the PostgreSQL triggers-enable script runs in one transaction: run triggers-enable.sql "
            "again as it is"
        )
    expected = expected_rows(section, engine)
    excluded = [(r.kind, r.routine_id, ",".join(r.reasons)) for r in excluded_rows(section, engine)]
    excluded += [(r.kind, r.routine_id, reason) for r, reason in left_out_rows(section, engine)]
    if not expected:
        # An earlier run's script (on MySQL it holds routine text) must not linger.
        for name in (*_FILES, RESUME_FILE):
            (step_dir / name).unlink(missing_ok=True)
        return TriggersScripts(status="nothing_to_do", excluded=sorted(excluded))
    migration = read_migration(store, db, job_id, section)
    user = target_user or migration.target_user
    carried = [r for r in section.routines if r.carry == "carried"]
    ordered = expected if pg else _ordered(expected, migration)
    positions: dict[tuple[str, str], tuple[int, int]] = {
        (r.kind, r.routine_id): (int(r.order or 0), len(carried)) for r in carried
    }
    positions.update({(r.kind, r.routine_id): (i + 1, len(ordered)) for i, r in enumerate(ordered)})
    deploy = block_of(engine, migration, user, target_database)
    states: dict[tuple[str, str], str | None] = {}

    def block(
        row: StepRoutine, source: RoutineSource, position: int, total: int
    ) -> tuple[str | None, str | None]:
        text, code = deploy(row, source, position, total)
        if text is not None and row.carry == "deferred_to_cutover" and row.kind == "event":
            states[(row.kind, row.routine_id)] = event_state(
                source.create_statement, source.session.sql_mode
            )
        return text, code

    step_dir.mkdir(parents=True, exist_ok=True)
    # Step 1's deploy script is planned again (its blocks spooled and dropped), so a
    # trigger it left out, or a routine it left out that one here names, is left out too.
    rows = carried if pg else [*carried, *ordered]
    with tempfile.TemporaryFile(dir=step_dir) as spool:
        spans, refused = spool_blocks(
            store, db, job_id, rows, block, spool, engine=engine, migration=migration,
            positions=positions,
        )  # fmt: skip
        keep = [r for r in ordered if (r.kind, r.routine_id) not in refused]
        excluded += [(r.kind, r.routine_id, refused[(r.kind, r.routine_id)]) for r in ordered
                     if (r.kind, r.routine_id) in refused]  # fmt: skip
        there = [r for r in keep if present and output_key(r, engine, target_database) in present]
        _resumable(keep, there)
        keep = [r for r in keep if r not in there]

        def state(row: StepRoutine) -> str | None:
            return states.get((row.kind, row.routine_id))

        scripts = TriggersScripts(
            status="ready",
            expected=len(expected),
            in_script=len(keep),
            stays_disabled=sum(1 for r in keep if pg and r.source_state in ("D", "disabled")),
            state_folded=sum(1 for r in keep if pg and r.source_state in FOLDED),
            events_enabled=sum(1 for r in keep if state(r) == "ENABLED"),
            events_replica_disabled=sum(1 for r in keep if state(r) == "REPLICA_SIDE_DISABLED"),
            already_there=len(there),
            excluded=sorted(excluded),
        )

        def chunks() -> Iterator[bytes]:
            head = _header(engine, contract_sha, scripts, present is not None)
            yield ("\n".join(head) + "\n").encode("utf-8")
            if pg:
                for i, row in enumerate(keep, start=1):
                    yield _pg_state(row, i, len(keep)).encode("utf-8")
                yield b"\nCOMMIT;\n"
            else:
                yield from spooled_chunks(spool, keep, spans)

        target = RESUME_FILE if present is not None else TRIGGERS_FILE
        write_atomic(step_dir / target, chunks())
    if present is not None:
        scripts.files = {RESUME_FILE: str(step_dir / RESUME_FILE)}
        return scripts
    (step_dir / RESUME_FILE).unlink(missing_ok=True)
    events = any(r.kind == "event" for r in expected)
    check = render_check_script(
        section, engine, contract_sha, target_database, rows=expected, spec=TRIGGERS_CHECK,
        extra=[_EVENT_SCHEDULER_SELECT] if events else (),
    )  # fmt: skip
    write_atomic(step_dir / TRIGGERS_CHECK_FILE, iter([check.encode("utf-8")]))
    output = str(step_dir / TRIGGERS_CHECK_OUTPUT)
    write_atomic(
        step_dir / TRIGGERS_INSTRUCTIONS,
        iter([_instructions(engine, scripts, output).encode("utf-8")]),
    )
    scripts.files = {name: str(step_dir / name) for name in _FILES}
    return scripts
