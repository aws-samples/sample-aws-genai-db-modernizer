# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1's routine scripts (#1947, ADR-034 Decision 6): generated here, run by the customer.

The modernizer never connects to the customer's databases (the maintainer decision of
2026-11-09 on #1947). After the full load, ``scripts/run_dms_step.py routines`` writes
three files into ``data-movement/step-1/``:

- ``routines-deploy.sql``: every routine the step contract's routines section carries,
  in its deploy order, each the exact ``CREATE`` from the job's routine sources with the
  routine-migration rewrites applied again for the user who runs it (a MySQL
  ``DEFINER``). It first creates, ``IF NOT EXISTS``, each schema (on Aurora MySQL, each
  database) its routines land in that step 1's own DDL does not create (#2148): never
  PostgreSQL's ``public`` nor the schema of a table the step moves. A MySQL database gets
  no character set or collation, as ``step-1.ddl.sql`` gives the tables' database, and
  nothing is granted on what it creates; a MySQL database that differs from the moved
  tables' only by case is refused (by its routines' ids, ``database_case_differs``, with
  :data:`CASE_DIFFERS_WHY`). On Aurora PostgreSQL the
  script runs in one transaction with ``check_function_bodies`` on, and each trigger is
  disabled in that transaction, so no replicated change ever meets it enabled. On Aurora MySQL each routine runs under its
  own ``sql_mode`` and connection collation, as ``mysqldump`` does; MySQL triggers and
  events are not in it (they belong to the triggers-enable script, #1948).
- ``routines-check.sql``: read-only (a ``READ ONLY`` transaction of catalog ``SELECT``\\s):
  one tab-separated line per routine of the section that exists on the target, between a
  header line naming the engine and the contract it was generated for and an end line.
- ``routines.md``: how to run both, in order, and where the check's output goes
  (``routines-check.tsv``, which ``run_dms_step.py routines-check`` reads).

The deploy script (with the triggers-enable script, #1948, which builds its MySQL blocks the same
way) is the one file outside the routine sources that holds a routine's text: the text
never goes into a JSON artifact, a log, a printed line or a report, and no
hash of it is kept. Identifiers that reach a comment or a literal of either script are
written safely (control characters refused, quotes doubled); a routine whose names or
session values do not pass is refused, by id and reason code, never written.

Memory: the routine sources are read one chunk at a time (``read_routine_sources``),
each carried statement spooled to a temporary file as it is read and copied into the
deploy script in deploy order, so only the offsets are held.
"""

from __future__ import annotations

import os
import re
import tempfile
import unicodedata
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from src.agents.collector.routine_sources import read_routine_sources
from src.contracts.routine_migration import RoutineMigrationContract
from src.contracts.routine_sources import RoutineSource
from src.contracts.transformation_contract import StepContract, StepRoutine, StepRoutines
from src.shared.cutover_message import CUTOVER_DISCLAIMER

__all__ = [
    "CASE_DIFFERS",
    "CASE_DIFFERS_WHY",
    "CHECK_FILE",
    "CHECK_OUTPUT",
    "DEPLOY_FILE",
    "INSTRUCTIONS_FILE",
    "CHECK_MAGIC",
    "CHECK_END",
    "ROUTINES_CHECK",
    "CheckScript",
    "RoutineScriptError",
    "RoutineScripts",
    "block_of",
    "case_differs_keys",
    "check_names",
    "read_migration",
    "render_check_script",
    "require_section",
    "spool_blocks",
    "spooled_chunks",
    "write_atomic",
    "write_routine_scripts",
]

DEPLOY_FILE = "routines-deploy.sql"
CHECK_FILE = "routines-check.sql"
CHECK_OUTPUT = "routines-check.tsv"
INSTRUCTIONS_FILE = "routines.md"
#: The first and last fields of the check output's header and end lines.
CHECK_MAGIC = "modernizer-routines-check"
CHECK_END = "modernizer-routines-check-end"
CHECK_FORMAT = "1"
CHECK_FIELDS = 7  # kind, schema, table, name, arguments, returns, state
#: The PostgreSQL deploy's lock_timeout: below the default deadlock_timeout (1s), so a
#: lock wait against DMS's apply fails the deploy, never the apply.
PG_LOCK_TIMEOUT = "500ms"
_SQL_MODE = re.compile(r"^[A-Z_]+(,[A-Z_]+)*$")
_COLLATION = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_DELIMITERS = (";;", "$$", "//", "!!", "%%")
#: The refusal of a MySQL routine whose database differs from a moved table's only by case.
CASE_DIFFERS = "database_case_differs"
#: What :data:`CASE_DIFFERS` means and what to do, for the command output and ``routines.md``.
CASE_DIFFERS_WHY = (
    "its database differs from a moved table's database only by case, and the collection "
    "cannot tell whether the target folds case. Rename one side on the source, or create "
    "these routines by hand in the database the application uses, then run "
    "routines-check.sql again; until then the check reports them missing."
)
#: :data:`CASE_DIFFERS_WHY` for a trigger or event the triggers-enable script leaves out: the
#: check that reports it missing is triggers-enable-check.sql.
CASE_DIFFERS_TRIGGERS_WHY = (
    "its database differs from a moved table's database only by case, and the collection "
    "cannot tell whether the target folds case. Rename one side on the source, or create "
    "these triggers and events by hand in the database the application uses, then run "
    "triggers-enable-check.sql again; until then the check reports them missing."
)
_DELIMITER_LINE = re.compile(r"(?im)^\s*delimiter\b")
_BAD_CATEGORIES = {"Cc", "Cf", "Zl", "Zp", "Cs", "Co", "Cn"}


class RoutineScriptError(ValueError):
    """The scripts cannot be generated (no routines section, no artifact)."""


@dataclass
class RoutineScripts:
    """What :func:`write_routine_scripts` wrote: counts, files and refusals, no text."""

    status: str  # "ready" or "nothing_to_deploy"
    carried: int = 0
    in_script: int = 0
    deferred: int = 0
    refused: list[tuple[str, str, str]] = field(default_factory=list)  # kind, id, code
    files: dict[str, str] = field(default_factory=dict)


def _safe(text: str | None) -> bool:
    """A name that may be written into a script: not empty, no control or separator
    character (a newline would end a comment or a line of the check output)."""
    if not text:
        return False
    return not any(unicodedata.category(c) in _BAD_CATEGORIES for c in text)


def _comment(text: str) -> str:
    """``text`` for a ``--`` comment line: every control or separator character is ``?``."""
    return "".join("?" if unicodedata.category(c) in _BAD_CATEGORIES else c for c in text)


def _pg_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _pg_literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _mysql_ident(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def _mysql_literal(text: str) -> str:
    return "'" + text.replace("\\", "\\\\").replace("'", "''") + "'"


def _table_name(row: StepRoutine) -> str | None:
    """A trigger's own table name, without its schema."""
    if not row.table:
        return None
    if row.schema_name and row.table.lower().startswith(row.schema_name.lower() + "."):
        return row.table[len(row.schema_name) + 1 :]
    return row.table.rpartition(".")[2] if "." in row.table else row.table


def _target_schema(row: StepRoutine, engine: str, target_database: str | None) -> str | None:
    """The schema or database the routine lands in: on Aurora MySQL the step's target
    database when the inputs name one, else the routine's own."""
    if engine == "aurora_mysql" and target_database:
        return target_database
    return row.schema_name


def _names_safe(row: StepRoutine) -> bool:
    table = _table_name(row) if row.kind == "trigger" else "-"
    return all(_safe(v) for v in (row.schema_name, row.name, table))


def check_names(
    section: StepRoutines,
    engine: str,
    target_database: str | None,
    *,
    rows: Iterable[StepRoutine] | None = None,
) -> dict[tuple[str, ...], StepRoutine]:
    """The key each section row (or each of ``rows``) has in the check output, for every
    row whose names may be written into the check script: PostgreSQL ``(kind, schema,
    name)`` or ``("trigger", schema, table, name)``; MySQL the same lower case, in the
    target database."""
    out: dict[tuple[str, ...], StepRoutine] = {}
    for row in section.routines if rows is None else rows:
        if not _names_safe(row):
            continue
        out.setdefault(output_key(row, engine, target_database), row)
    return out


def output_key(row: StepRoutine, engine: str, target_database: str | None) -> tuple[str, ...]:
    """The check-output key of a section row (:func:`check_names`)."""
    schema = _target_schema(row, engine, target_database) or ""
    if engine == "aurora_mysql":
        return (row.kind, schema.lower(), "", row.name.lower())
    table = (_table_name(row) or "") if row.kind == "trigger" else ""
    return (row.kind, schema, table, row.name)


# --------------------------------------------------------------------------- statements


def _statement(text: str) -> str:
    body = text.rstrip()
    return body if body.endswith(";") else body + ";"


def _pg_block(
    row: StepRoutine, source: RoutineSource, position: int, total: int
) -> tuple[str | None, str | None]:
    lines = [f"-- {position}/{total} {row.kind} {_comment(row.routine_id)}"]
    if row.schema_name:
        lines.append(f"SET search_path TO {_pg_ident(row.schema_name)}, public;")
    lines.append(_statement(source.create_statement))
    if row.kind == "trigger":
        table = _table_name(row)
        if not table or not row.schema_name:
            return None, "trigger_table_unknown"
        lines.append(
            f"ALTER TABLE {_pg_ident(row.schema_name)}.{_pg_ident(table)} "
            f"DISABLE TRIGGER {_pg_ident(row.name)};"
        )
    return "\n".join(lines) + "\n\n", None


def _mysql_text(
    row: StepRoutine, source: RoutineSource, migration: RoutineMigrationContract, user: str
) -> tuple[str | None, str | None]:
    """The routine's text with the routine-migration rewrites applied for ``user``."""
    from src.tools.routines.mysql_checks import MySqlRoutine, check_mysql_routine
    from src.tools.routines.rewrites import rewrite_mysql_routine

    routine = MySqlRoutine(
        routine_id=row.routine_id,
        kind=row.kind,
        create_sql=source.create_statement,
        sql_mode=source.session.sql_mode or "",
    )
    verdict = check_mysql_routine(
        routine,
        target_engine=migration.target_engine,
        target_line=migration.target_line,
        target_user=user,
        source_version=migration.source_version,
    )
    done = rewrite_mysql_routine(routine, verdict, target_user=user)
    if done.record.status == "needs_attention":
        return None, "needs_attention_for_target_user"
    return done.target_create, None


def _mysql_block(
    row: StepRoutine,
    source: RoutineSource,
    position: int,
    total: int,
    migration: RoutineMigrationContract,
    user: str,
    database: str | None,
) -> tuple[str | None, str | None]:
    """A MySQL routine's block: its own ``sql_mode`` and collation, the rewrites applied
    for ``user``, between ``DELIMITER`` lines (the deploy script and the triggers-enable script)."""
    session = source.session
    if session.sql_mode and not _SQL_MODE.match(session.sql_mode):
        return None, "session_not_allowed"
    collation = session.collation_connection
    if collation and not _COLLATION.match(collation):
        return None, "session_not_allowed"
    text, refused = _mysql_text(row, source, migration, user)
    if text is None:
        return None, refused
    if _DELIMITER_LINE.search(text):
        return None, "delimiter_in_text"
    delimiter = next((d for d in _DELIMITERS if d not in text), None)
    if delimiter is None:
        return None, "delimiter_in_text"
    lines = [f"-- {position}/{total} {row.kind} {_comment(row.routine_id)}"]
    if database:
        lines.append(f"USE {_mysql_ident(database)};")
    lines.append(f"SET SESSION sql_mode = {_mysql_literal(session.sql_mode or '')};")
    if collation:
        lines.append(f"SET SESSION collation_connection = {_mysql_literal(collation)};")
    lines += [f"DELIMITER {delimiter}", text.rstrip() + delimiter, "DELIMITER ;"]
    return "\n".join(lines) + "\n\n", None


# --------------------------------------------------------------------------- the files


def step_schemas(contract: StepContract) -> set[str]:
    """The schemas (MySQL databases) step 1's own DDL creates: those of the tables it moves,
    the same ``contract_tables`` ``render_ddl`` reads (a blocked rule's table and a rule that
    moves no rows, #1918, are not among them)."""
    from src.tools.data_movement.dms_step import contract_tables

    return {t.schema for t in contract_tables(contract) if t.schema}


def case_clash(schema: str | None, tables_in: set[str], engine: str) -> bool:
    """A MySQL database that differs from one step 1's DDL creates only by case: on a
    cluster with ``lower_case_table_names=0`` creating it would make a second, empty one."""
    if engine != "aurora_mysql" or not schema or schema in tables_in:
        return False
    return schema.lower() in {s.lower() for s in tables_in}


def case_differs_keys(
    contract: StepContract, migration: RoutineMigrationContract, target_database: str | None
) -> set[tuple[str, str]]:
    """The carried routines the deploy leaves out for a case-only database clash: each one
    refused as :data:`CASE_DIFFERS`, and each that names one of those (directly or not, so
    refused as ``depends_on_refused``) and clears with the same fix. ``run_dms_step.py``
    gives each :data:`CASE_DIFFERS_WHY`."""
    engine = contract.target.engine
    tables_in = step_schemas(contract)
    deps = _dependencies(migration)
    out: set[tuple[str, str]] = set()
    for row in _routine_order(require_section(contract)):  # deploy order: dependencies first
        key = (row.kind, row.routine_id)
        clash = case_clash(_target_schema(row, engine, target_database), tables_in, engine)
        if clash or any(d in out for d in deps.get(key, ())):
            out.add(key)
    return out


def routine_schemas(
    rows: Iterable[StepRoutine], engine: str, target_database: str | None, tables_in: set[str]
) -> list[str]:
    """The schemas (MySQL databases) the deploy script creates before its routines (#2148):
    each one ``rows`` land in, except PostgreSQL's ``public`` (every database has it) and a
    schema step 1's DDL creates (:func:`step_schemas`). Names compare exactly, as the target
    compares them. Sorted."""
    skip = tables_in | ({"public"} if engine == "aurora_postgresql" else set())
    found = {_target_schema(r, engine, target_database) for r in rows}
    return sorted(s for s in found if s and _safe(s) and s not in skip)


def schema_statements(engine: str, schemas: list[str], *, triggers: bool = False) -> list[str]:
    if not schemas:
        return []
    if engine == "aurora_postgresql":
        return [
            "-- The schemas the routines live in that step 1's DDL does not create (the user that "
            "runs this script needs CREATE on the database).",
            "-- Each is owned by the user that runs this script, and the script grants nothing: "
            "USAGE on each schema for the application user is yours to add.",
            *(f"CREATE SCHEMA IF NOT EXISTS {_pg_ident(s)};" for s in schemas),
            "",
        ]
    from src.tools.data_movement.dms_step import SERVER_DEFAULTS

    if triggers:  # the triggers-enable script (#2273): its databases hold triggers and events
        return [
            "-- The databases its triggers and events live in that step 1's DDL does not create "
            "(one the routines deploy script already created is left as it is).",
            "-- The user that runs this script needs CREATE and EVENT on each new database: "
            "creating a database gives its creator no privileges on it.",
            "-- Each event runs as its DEFINER, which needs the privileges its body uses; the "
            "script grants nothing.",
            f"-- Each CREATE DATABASE here is like step-1.ddl.sql's. {SERVER_DEFAULTS}",
            *(f"CREATE DATABASE IF NOT EXISTS {_mysql_ident(s)};" for s in schemas),
            "",
        ]

    # As step-1.ddl.sql creates the tables' database: no character set or collation, so a
    # routine's parameters take the same server default as the moved tables' columns.
    return [
        "-- The databases the routines live in that step 1's DDL does not create (the user that "
        "runs this script needs CREATE on them).",
        "-- Each is created by the user that runs this script, and the script grants nothing: "
        "EXECUTE on each database for the application user is yours to add.",
        f"-- Each CREATE DATABASE here is like step-1.ddl.sql's. {SERVER_DEFAULTS}",
        *(f"CREATE DATABASE IF NOT EXISTS {_mysql_ident(s)};" for s in schemas),
        "",
    ]


def _deploy_header(
    engine: str,
    section: StepRoutines,
    in_script: int,
    contract_sha: str,
    schemas: list[str] | None = None,
) -> str:
    made = schema_statements(engine, schemas or [])
    lines = [
        f"-- {CUTOVER_DISCLAIMER}",
        "-- Step 1 routines: generated by the modernizer; you run it, it never connects to "
        "your databases.",
        f"-- Step contract SHA-256: {contract_sha}",
        f"-- {in_script} of the {section.carried} routines step 1 carries, in dependency order.",
        "-- Run it once, after the DMS full load is complete, as the user the routines "
        "should belong to.",
        "-- Checked, not load-tested: the routines passed the static checks; no load-and-run "
        "proof ran yet.",
        "-- Then run routines-check.sql (read-only) as routines.md says.",
    ]
    if engine == "aurora_postgresql":
        lines += [
            "-- One transaction: if any statement fails, nothing is created; fix it and run "
            "the script again.",
            "-- Triggers are created and disabled in that transaction: DMS already applies "
            "the source triggers' effects, so they stay off; the triggers-enable script turns them "
            "on.",
            f"-- lock_timeout {PG_LOCK_TIMEOUT}, below PostgreSQL's default deadlock_timeout (1s): "
            "while DMS applies changes, a lock this script waits for makes the script fail, "
            "never DMS's apply; run the script again.",
            "",
            "\\set ON_ERROR_STOP on",
            "SET client_encoding = 'UTF8';",
            "SET check_function_bodies = on;",
            "BEGIN;",
            f"SET LOCAL lock_timeout = '{PG_LOCK_TIMEOUT}';",
            "",
            *made,
        ]
    else:
        lines += [
            "-- MySQL triggers and events are not here: MySQL cannot create a trigger "
            "disabled, and an enabled event would run on Aurora while DMS replicates the "
            "source's own event's effects; the triggers-enable script creates them.",
            "-- MySQL commits each CREATE on its own: the client stops at the first error; "
            "routines-check.sql then shows which routines exist.",
            "",
            "SET NAMES utf8mb4;",
            "",
            *made,
        ]
    return "\n".join(lines) + "\n"


def _deploy_footer(engine: str) -> str:
    return "COMMIT;\n" if engine == "aurora_postgresql" else ""


@dataclass(frozen=True)
class CheckScript:
    """What tells one read-only check script (and its output) from another: the first
    field of its header and end lines, its title and its file names."""

    magic: str
    end: str
    title: str
    script: str
    output: str
    command: str  # the run_dms_step.py subcommand that writes the script


#: Step 1's routine check (#1947).
ROUTINES_CHECK = CheckScript(
    CHECK_MAGIC, CHECK_END, "Step 1 routine check", CHECK_FILE, CHECK_OUTPUT, "routines"
)


def render_check_script(
    section: StepRoutines,
    engine: str,
    contract_sha: str,
    target_database: str | None,
    *,
    rows: Iterable[StepRoutine] | None = None,
    spec: CheckScript = ROUTINES_CHECK,
    extra: Iterable[str] = (),
) -> str:
    """The read-only check script: catalog ``SELECT``\\s in a ``READ ONLY`` transaction,
    asking only for the section's routines (or ``rows`` of it), one tab-separated line
    each, between a header and an end line; ``extra`` statements (read-only ``SELECT``\\s
    of a setting) run just before the end line."""
    names = check_names(section, engine, target_database, rows=rows)
    header = (spec.magic, CHECK_FORMAT, engine, contract_sha, target_database or "", "", "")
    if engine == "aurora_postgresql":
        return _pg_check(names, header, spec, list(extra))
    return _mysql_check(names, header, spec, list(extra))


def _select_line(values: Iterable[str], literal: Any) -> str:
    return "SELECT " + ", ".join(literal(v) for v in values) + ";"


def _pg_check(
    names: dict[tuple[str, ...], StepRoutine],
    header: tuple[str, ...],
    spec: CheckScript,
    extra: list[str],
) -> str:
    routines = sorted({(k[1], k[3]) for k in names if k[0] in ("function", "procedure")})
    triggers = sorted({(k[1], k[2], k[3]) for k in names if k[0] == "trigger"})
    out = [
        f"-- {spec.title}: read-only, catalog SELECTs only, in a READ ONLY transaction.",
        f'-- Run: psql "<connection>" -X -f {spec.script} -o {spec.output}',
        "\\set QUIET on",
        "\\set ON_ERROR_STOP on",
        "\\pset format unaligned",
        "\\pset tuples_only on",
        "\\pset footer off",
        "\\pset null ''",
        "\\pset fieldsep '\\t'",
        "BEGIN TRANSACTION READ ONLY;",
        _select_line(header, _pg_literal),
    ]
    if routines:
        values = ",\n  ".join(f"({_pg_literal(s)}, {_pg_literal(n)})" for s, n in routines)
        out.append(
            f"WITH expected(nspname, proname) AS (VALUES\n  {values})\n"  # nosec B608 -- a script the customer runs; every value is a quoted literal of a checked name
            "SELECT CASE p.prokind WHEN 'p' THEN 'procedure' ELSE 'function' END, n.nspname, '',"
            " p.proname, pg_catalog.oidvectortypes(p.proargtypes), '', ''\n"
            "FROM pg_catalog.pg_proc p\n"
            "JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace\n"
            "JOIN expected e ON e.nspname = n.nspname AND e.proname = p.proname\n"
            "WHERE p.prokind IN ('f', 'p')\nORDER BY 1, 2, 4, 5;"
        )
    if triggers:
        values = ",\n  ".join(
            f"({_pg_literal(s)}, {_pg_literal(t)}, {_pg_literal(n)})" for s, t, n in triggers
        )
        out.append(
            f"WITH expected(nspname, relname, tgname) AS (VALUES\n  {values})\n"  # nosec B608 -- a script the customer runs; every value is a quoted literal of a checked name
            "SELECT 'trigger', n.nspname, c.relname, t.tgname, '', '', t.tgenabled::text\n"
            "FROM pg_catalog.pg_trigger t\n"
            "JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid\n"
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace\n"
            "JOIN expected e ON e.nspname = n.nspname AND e.relname = c.relname"
            " AND e.tgname = t.tgname\n"
            "WHERE NOT t.tgisinternal\nORDER BY 2, 3, 4;"
        )
    out += [*extra, _select_line((spec.end, "", "", "", "", "", ""), _pg_literal), "COMMIT;"]
    return "\n".join(out) + "\n"


def _mysql_check(
    names: dict[tuple[str, ...], StepRoutine],
    header: tuple[str, ...],
    spec: CheckScript,
    extra: list[str],
) -> str:
    by_kind: dict[str, list[tuple[str, str]]] = {}
    for key in sorted(names):
        by_kind.setdefault(key[0], []).append((key[1], key[3]))

    def pairs(kind: str) -> str:
        return ", ".join(
            f"({_mysql_literal(s)}, {_mysql_literal(n)})" for s, n in sorted(set(by_kind[kind]))
        )

    out = [
        f"-- {spec.title}: read-only, information_schema SELECTs only, in a READ ONLY "
        "transaction.",
        f"-- Run: mysql <connection options> --batch --skip-column-names < {spec.script} "
        f"> {spec.output}",
        "SET SESSION group_concat_max_len = 1048576;",
        "START TRANSACTION READ ONLY;",
        _select_line(header, _mysql_literal),
    ]
    routine_pairs = [
        f"(LOWER(r.ROUTINE_SCHEMA), LOWER(r.ROUTINE_NAME)) IN ({pairs(k)})"
        for k in ("function", "procedure")
        if k in by_kind
    ]
    if routine_pairs:
        out.append(
            "SELECT LOWER(r.ROUTINE_TYPE), r.ROUTINE_SCHEMA, '', r.ROUTINE_NAME,\n"  # nosec B608 -- a script the customer runs; every value is a quoted literal of a checked name
            "  COALESCE((SELECT GROUP_CONCAT(CONCAT_WS(' ', p.PARAMETER_MODE, p.DTD_IDENTIFIER)"
            " ORDER BY p.ORDINAL_POSITION SEPARATOR ', ')\n"
            "    FROM information_schema.PARAMETERS p WHERE p.SPECIFIC_SCHEMA = r.ROUTINE_SCHEMA"
            " AND p.SPECIFIC_NAME = r.SPECIFIC_NAME AND p.ROUTINE_TYPE = r.ROUTINE_TYPE"
            " AND p.ORDINAL_POSITION > 0), ''),\n"
            "  COALESCE(r.DTD_IDENTIFIER, ''), ''\n"
            "FROM information_schema.ROUTINES r\n"
            f"WHERE {' OR '.join(routine_pairs)}\nORDER BY 1, 2, 4;"
        )
    if "trigger" in by_kind:
        out.append(
            "SELECT 'trigger', TRIGGER_SCHEMA, EVENT_OBJECT_TABLE, TRIGGER_NAME, '', '', ''\n"  # nosec B608 -- a script the customer runs; every value is a quoted literal of a checked name
            "FROM information_schema.TRIGGERS\n"
            f"WHERE (LOWER(TRIGGER_SCHEMA), LOWER(TRIGGER_NAME)) IN ({pairs('trigger')})\n"
            "ORDER BY 2, 4;"
        )
    if "event" in by_kind:
        out.append(
            "SELECT 'event', EVENT_SCHEMA, '', EVENT_NAME, '', '', STATUS\n"  # nosec B608 -- a script the customer runs; every value is a quoted literal of a checked name
            "FROM information_schema.EVENTS\n"
            f"WHERE (LOWER(EVENT_SCHEMA), LOWER(EVENT_NAME)) IN ({pairs('event')})\n"
            "ORDER BY 2, 4;"
        )
    out += [*extra, _select_line((spec.end, "", "", "", "", "", ""), _mysql_literal), "COMMIT;"]
    return "\n".join(out) + "\n"


def _instructions(
    engine: str,
    scripts: RoutineScripts,
    section: StepRoutines,
    output_path: str,
    user: str,
    created: int = 0,
) -> str:
    pg = engine == "aurora_postgresql"
    deploy = (
        'psql "host=<Aurora writer endpoint> port=5432 dbname=<database> user=<user> '
        'sslmode=verify-full" -X -f routines-deploy.sql'
        if pg
        else "mysql --host=<Aurora writer endpoint> --user=<user> -p "
        "--default-character-set=utf8mb4 < routines-deploy.sql"
    )
    check = (
        'psql "host=<Aurora writer endpoint> port=5432 dbname=<database> user=<user> '
        f'sslmode=verify-full" -X -f routines-check.sql -o {CHECK_OUTPUT}'
        if pg
        else "mysql --host=<Aurora writer endpoint> --user=<user> -p --batch --skip-column-names "
        f"< routines-check.sql > {CHECK_OUTPUT}"
    )
    lines = [
        CUTOVER_DISCLAIMER,
        "",
        "# Step 1 routines",
        "",
        "The modernizer never connects to your databases. You run these two scripts on your "
        "Aurora writer, with your own credentials.",
        "",
        f"- Carried by `{DEPLOY_FILE}`: {scripts.in_script} of {section.carried} routines, in "
        "dependency order." + (" Triggers are created disabled." if pg else ""),
        f"- Not carried: {section.not_carried} (each is yours to settle; the step "
        "contract's routines section names each one and why).",
    ]
    if not pg:
        lines.append(
            f"- Left to the triggers-enable script: {section.deferred} MySQL triggers and events "
            "(MySQL cannot create a trigger disabled, and an enabled event would run on Aurora "
            "while DMS replicates the source's own event's effects)."
        )
    if created:
        lines.append(
            f"- Schemas it creates, which step 1's DDL does not: {created} (`{DEPLOY_FILE}` "
            "names them). Each is owned by the user that runs it, and the script grants "
            "nothing: `USAGE` on each schema for the application user is yours to add."
            if pg
            else f"- Databases it creates, which step 1's DDL does not: {created} (`{DEPLOY_FILE}` "
            "names them). Each is created by the user that runs it, and the script grants "
            "nothing: `EXECUTE` on each database for the application user is yours to add."
        )
    clashes = sum(1 for _, _, code in scripts.refused if code == CASE_DIFFERS)
    if clashes:
        lines.append(f"- Left out as `{CASE_DIFFERS}`: {clashes}. For each, {CASE_DIFFERS_WHY}")
    if scripts.refused:
        lines.append(
            f"- Left out of the script: {len(scripts.refused)} (`run_dms_step.py routines` "
            "names each one and why)."
        )
    lines += [
        "- Checked, not load-tested: the routines passed the static checks; no load-and-run "
        "proof ran yet.",
        "- Step 1 does not create views: a routine that depends on a view is not carried in "
        "step 1.",
        "",
        "## Order",
        "",
        "1. Wait until the DMS full load is complete (`/move` shows it at 100%).",
        f"2. Run the deploy script once, as the user the routines should belong to:\n\n"
        f"   ```\n   {deploy}\n   ```",
        f"3. Run the read-only check script and save its output next to these files, at "
        f"`{output_path}`:\n\n   ```\n   {check}\n   ```",
        "4. Ask `/move step-1` to read the check (`run_dms_step.py routines-check`).",
        "",
    ]
    if pg:
        lines.append(
            "The deploy script runs in one transaction: if a statement fails, nothing is "
            "created; fix the cause and run it again. If it stops on a lock timeout "
            "(`canceling statement due to lock timeout`), DMS was applying changes to a table "
            "the script needed: run it again as it is."
        )
    else:
        lines.append(
            "MySQL commits each CREATE on its own and the client stops at the first error: "
            "the check then shows which routines exist. Run the deploy script as "
            f"`{_comment(user).replace('`', '')}`, the user its DEFINER clauses name; for "
            "another user, generate it again with `run_dms_step.py routines --target-user`."
        )
    return "\n".join(lines) + "\n"


def _routine_order(section: StepRoutines) -> list[StepRoutine]:
    return [r for r in section.routines if r.carry == "carried"]


def _dependencies(
    migration: RoutineMigrationContract,
) -> dict[tuple[str, str], set[tuple[str, str]]]:
    return {
        (row.kind, row.routine_id): {
            (str(ref.kind), ref.routine_id) for d in row.dependencies for ref in d.routines
        }
        - {(row.kind, row.routine_id)}
        for row in migration.routines
    }


def write_atomic(path: Path, chunks: Iterator[bytes]) -> None:
    """Write ``chunks`` to ``path`` through a temporary file, replaced at the end."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as out:
        for chunk in chunks:
            out.write(chunk)
    os.replace(tmp, path)


def read_migration(
    store: Any, db: str, job_id: str, section: StepRoutines
) -> RoutineMigrationContract:
    """The routine-migration artifact the section was read from."""
    from src.tools.routines.migration import read_routine_migration

    migration = None
    if section.routine_migration_version is not None:
        migration = read_routine_migration(store, db, job_id, section.routine_migration_version)
    if migration is None:
        raise RoutineScriptError(
            f"no routine-migration artifact at routines/v{section.routine_migration_version}: "
            "run scripts/run_transformation_contract.py again"
        )
    return migration


def require_section(contract: StepContract) -> StepRoutines:
    """The contract's routines section; :class:`RoutineScriptError` when it has none."""
    if contract.routines is None:
        raise RoutineScriptError(
            "the step 1 contract has no routines section (written before #1947): run "
            "scripts/run_transformation_contract.py, run_data_movement_plan.py and "
            "render_dms_step.py again"
        )
    return contract.routines


RoutineKey = tuple[str, str]  # kind, routine_id
BlockOf = Callable[[StepRoutine, RoutineSource, int, int], tuple[str | None, str | None]]


def block_of(
    engine: str, migration: RoutineMigrationContract, user: str, target_database: str | None
) -> BlockOf:
    """How one routine's ``CREATE`` is written into a script, ``(block, None)`` or
    ``(None, reason code)``: the exact text, and on Aurora MySQL its session and the
    routine-migration rewrites for ``user``."""

    def block(
        row: StepRoutine, source: RoutineSource, position: int, total: int
    ) -> tuple[str | None, str | None]:
        if engine == "aurora_postgresql":
            return _pg_block(row, source, position, total)
        database = _target_schema(row, engine, target_database)
        return _mysql_block(row, source, position, total, migration, user, database)

    return block


def spool_blocks(
    store: Any,
    db: str,
    job_id: str,
    rows: list[StepRoutine],
    block: BlockOf,
    spool: IO[bytes],
    *,
    engine: str,
    migration: RoutineMigrationContract,
    positions: dict[RoutineKey, tuple[int, int]] | None = None,
) -> tuple[dict[RoutineKey, tuple[int, int]], dict[RoutineKey, str]]:
    """Each of ``rows``' blocks spooled to ``spool`` as the routine sources are read, one
    chunk at a time: the ``(offset, length)`` of each block written, and the reason code of
    each routine refused (its text, names or session do not pass, its source is missing, or
    it names a routine refused, checked in the order of ``rows``). ``positions`` numbers a
    row's comment line (default: its place in ``rows``)."""
    where = positions or {(r.kind, r.routine_id): (i + 1, len(rows)) for i, r in enumerate(rows)}
    # A PostgreSQL rewrite that changes the text (the search_path pin) is never applied by
    # the artifact today (the source's search_path is not collected); the scripts use the
    # exact text, so a routine that has one is refused rather than written without it.
    text_rewrites = {
        (r.kind, r.routine_id)
        for r in migration.routines
        if engine == "aurora_postgresql" and any(w.rewrite == "search_path" for w in r.rewrites)
    }
    refused: dict[RoutineKey, str] = {}
    pending: dict[tuple[str, str, str], StepRoutine] = {}
    for row in rows:
        if (row.kind, row.routine_id) in text_rewrites:
            refused[(row.kind, row.routine_id)] = "rewrite_not_reapplied"
        elif not _names_safe(row):
            refused[(row.kind, row.routine_id)] = "unsafe_name"
        else:
            pending[(row.kind, row.schema_name or "", row.specific_name or "")] = row
    spans: dict[RoutineKey, tuple[int, int]] = {}
    for source in read_routine_sources(store, db, job_id):
        found = pending.pop(source.key(), None)
        if found is None:
            continue
        key = (found.kind, found.routine_id)
        text: str | None
        code: str | None
        if not source.exact:
            text, code = None, "source_text_not_exact"
        else:
            text, code = block(found, source, *where[key])
        if text is None:
            refused[key] = code or "refused"
            continue
        data = text.encode("utf-8")
        spans[key] = (spool.tell(), len(data))
        spool.write(data)
        del text, data
    for row in pending.values():
        refused[(row.kind, row.routine_id)] = "source_missing"
    # A routine naming one left out is left out too: its CREATE could fail on it.
    deps = _dependencies(migration)
    for row in rows:
        key = (row.kind, row.routine_id)
        if key not in refused and any(d in refused for d in deps.get(key, ())):
            refused[key] = "depends_on_refused"
            spans.pop(key, None)
    return spans, refused


def spooled_chunks(
    spool: IO[bytes], rows: Iterable[StepRoutine], spans: dict[RoutineKey, tuple[int, int]]
) -> Iterator[bytes]:
    """The spooled blocks of ``rows``, in that order (a refused row has none)."""
    for row in rows:
        span = spans.get((row.kind, row.routine_id))
        if span is None:
            continue
        spool.seek(span[0])
        yield spool.read(span[1])


def write_routine_scripts(
    store: Any,
    db: str,
    job_id: str,
    contract: StepContract,
    contract_sha: str,
    step_dir: Path,
    *,
    target_database: str | None = None,
    target_user: str | None = None,
) -> RoutineScripts:
    """Write the step's routine scripts and instructions into ``step_dir`` (module
    docstring). ``nothing_to_deploy`` (and no file) when the section carries no routine
    and defers none."""
    section = require_section(contract)
    engine = contract.target.engine
    carried = _routine_order(section)
    if not carried and not section.deferred:
        # An earlier run's scripts (the deploy script holds routine text) must not linger.
        for name in (DEPLOY_FILE, CHECK_FILE, INSTRUCTIONS_FILE):
            (step_dir / name).unlink(missing_ok=True)
        return RoutineScripts(status="nothing_to_deploy", deferred=0)
    migration = read_migration(store, db, job_id, section)
    user = target_user or migration.target_user
    step_dir.mkdir(parents=True, exist_ok=True)
    tables_in = step_schemas(contract)
    write_block = block_of(engine, migration, user, target_database)

    def block(
        row: StepRoutine, source: RoutineSource, position: int, total: int
    ) -> tuple[str | None, str | None]:
        if case_clash(_target_schema(row, engine, target_database), tables_in, engine):
            return None, CASE_DIFFERS
        return write_block(row, source, position, total)

    with tempfile.TemporaryFile(dir=step_dir) as spool:
        spans, refused = spool_blocks(
            store, db, job_id, carried, block, spool, engine=engine, migration=migration,
        )  # fmt: skip
        in_script = len(spans)
        in_rows = [r for r in carried if (r.kind, r.routine_id) in spans]
        schemas = routine_schemas(in_rows, engine, target_database, tables_in)

        def deploy_chunks() -> Iterator[bytes]:
            header = _deploy_header(engine, section, in_script, contract_sha, schemas)
            yield header.encode("utf-8")
            yield from spooled_chunks(spool, carried, spans)
            yield _deploy_footer(engine).encode("utf-8")

        write_atomic(step_dir / DEPLOY_FILE, deploy_chunks())
    check = render_check_script(section, engine, contract_sha, target_database)
    write_atomic(step_dir / CHECK_FILE, iter([check.encode("utf-8")]))
    refusals = sorted((k, i, c) for (k, i), c in refused.items())
    scripts = RoutineScripts(
        status="ready",
        carried=section.carried,
        in_script=in_script,
        deferred=section.deferred,
        refused=refusals,
    )
    output = str(step_dir / CHECK_OUTPUT)
    write_atomic(
        step_dir / INSTRUCTIONS_FILE,
        iter([_instructions(engine, scripts, section, output, user, len(schemas)).encode("utf-8")]),
    )
    scripts.files = {
        name: str(step_dir / name) for name in (DEPLOY_FILE, CHECK_FILE, INSTRUCTIONS_FILE)
    }
    return scripts
