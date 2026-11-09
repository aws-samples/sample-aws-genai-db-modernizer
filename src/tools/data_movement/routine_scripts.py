# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1's routine scripts (#1947, ADR-034 Decision 6): generated here, run by the customer.

The modernizer never connects to the customer's databases (the maintainer decision of
2026-11-09 on #1947). After the full load, ``scripts/run_dms_step.py routines`` writes
three files into ``data-movement/step-1/``:

- ``routines-deploy.sql``: every routine the step contract's routines section carries,
  in its deploy order, each the exact ``CREATE`` from the job's routine sources with the
  routine-migration rewrites applied again for the user who runs it (a MySQL
  ``DEFINER``). On Aurora PostgreSQL the script runs in one transaction with
  ``check_function_bodies`` on, and each trigger is disabled in that transaction, so no
  replicated change ever meets it enabled. On Aurora MySQL each routine runs under its
  own ``sql_mode`` and connection collation, as ``mysqldump`` does; MySQL triggers and
  events are not in it (they belong to the cutover script, #1948).
- ``routines-check.sql``: read-only (a ``READ ONLY`` transaction of catalog ``SELECT``\\s):
  one tab-separated line per routine of the section that exists on the target, between a
  header line naming the engine and the contract it was generated for and an end line.
- ``routines.md``: how to run both, in order, and where the check's output goes
  (``routines-check.tsv``, which ``run_dms_step.py routines-check`` reads).

The deploy script is the one file outside the routine sources that holds a routine's
text: the text never goes into a JSON artifact, a log, a printed line or a report, and no
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
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.agents.collector.routine_sources import read_routine_sources
from src.contracts.routine_migration import RoutineMigrationContract
from src.contracts.routine_sources import RoutineSource
from src.contracts.transformation_contract import StepContract, StepRoutine, StepRoutines

__all__ = [
    "CHECK_FILE",
    "CHECK_OUTPUT",
    "DEPLOY_FILE",
    "INSTRUCTIONS_FILE",
    "CHECK_MAGIC",
    "CHECK_END",
    "RoutineScriptError",
    "RoutineScripts",
    "check_names",
    "render_check_script",
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
    section: StepRoutines, engine: str, target_database: str | None
) -> dict[tuple[str, ...], StepRoutine]:
    """The key each section row has in the check output, for every row whose names may be
    written into the check script: PostgreSQL ``(kind, schema, name)`` or ``("trigger",
    schema, table, name)``; MySQL the same lower case, in the target database."""
    out: dict[tuple[str, ...], StepRoutine] = {}
    for row in section.routines:
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


def _pg_block(row: StepRoutine, source: RoutineSource, total: int) -> tuple[str | None, str | None]:
    lines = [f"-- {row.order}/{total} {row.kind} {_comment(row.routine_id)}"]
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
    total: int,
    migration: RoutineMigrationContract,
    user: str,
    database: str | None,
) -> tuple[str | None, str | None]:
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
    lines = [f"-- {row.order}/{total} {row.kind} {_comment(row.routine_id)}"]
    if database:
        lines.append(f"USE {_mysql_ident(database)};")
    lines.append(f"SET SESSION sql_mode = {_mysql_literal(session.sql_mode or '')};")
    if collation:
        lines.append(f"SET SESSION collation_connection = {_mysql_literal(collation)};")
    lines += [f"DELIMITER {delimiter}", text.rstrip() + delimiter, "DELIMITER ;"]
    return "\n".join(lines) + "\n\n", None


# --------------------------------------------------------------------------- the files


def _deploy_header(engine: str, section: StepRoutines, in_script: int, contract_sha: str) -> str:
    lines = [
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
            "the source triggers' effects, so they stay off until cutover.",
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
        ]
    else:
        lines += [
            "-- MySQL triggers and events are not here: MySQL cannot create a trigger "
            "disabled, and an enabled event would run on Aurora while DMS replicates the "
            "source's own event's effects; the cutover script creates them.",
            "-- MySQL commits each CREATE on its own: the client stops at the first error; "
            "routines-check.sql then shows which routines exist.",
            "",
            "SET NAMES utf8mb4;",
            "",
        ]
    return "\n".join(lines) + "\n"


def _deploy_footer(engine: str) -> str:
    return "COMMIT;\n" if engine == "aurora_postgresql" else ""


def render_check_script(
    section: StepRoutines, engine: str, contract_sha: str, target_database: str | None
) -> str:
    """The read-only check script: catalog ``SELECT``\\s in a ``READ ONLY`` transaction,
    asking only for the section's routines, one tab-separated line each, between a header
    and an end line."""
    names = check_names(section, engine, target_database)
    header = (CHECK_MAGIC, CHECK_FORMAT, engine, contract_sha, target_database or "", "", "")
    if engine == "aurora_postgresql":
        return _pg_check(names, header)
    return _mysql_check(names, header)


def _select_line(values: Iterable[str], literal: Any) -> str:
    return "SELECT " + ", ".join(literal(v) for v in values) + ";"


def _pg_check(names: dict[tuple[str, ...], StepRoutine], header: tuple[str, ...]) -> str:
    routines = sorted({(k[1], k[3]) for k in names if k[0] in ("function", "procedure")})
    triggers = sorted({(k[1], k[2], k[3]) for k in names if k[0] == "trigger"})
    out = [
        "-- Step 1 routine check: read-only, catalog SELECTs only, in a READ ONLY transaction.",
        '-- Run: psql "<connection>" -X -f routines-check.sql -o routines-check.tsv',
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
    out += [_select_line((CHECK_END, "", "", "", "", "", ""), _pg_literal), "COMMIT;"]
    return "\n".join(out) + "\n"


def _mysql_check(names: dict[tuple[str, ...], StepRoutine], header: tuple[str, ...]) -> str:
    by_kind: dict[str, list[tuple[str, str]]] = {}
    for key in sorted(names):
        by_kind.setdefault(key[0], []).append((key[1], key[3]))

    def pairs(kind: str) -> str:
        return ", ".join(
            f"({_mysql_literal(s)}, {_mysql_literal(n)})" for s, n in sorted(set(by_kind[kind]))
        )

    out = [
        "-- Step 1 routine check: read-only, information_schema SELECTs only, in a READ ONLY "
        "transaction.",
        "-- Run: mysql <connection options> --batch --skip-column-names < routines-check.sql "
        "> routines-check.tsv",
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
    out += [_select_line((CHECK_END, "", "", "", "", "", ""), _mysql_literal), "COMMIT;"]
    return "\n".join(out) + "\n"


def _instructions(
    engine: str, scripts: RoutineScripts, section: StepRoutines, output_path: str, user: str
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
        "# Step 1 routines",
        "",
        "The modernizer never connects to your databases. You run these two scripts on your "
        "Aurora writer, with your own credentials.",
        "",
        f"- Carried by `{DEPLOY_FILE}`: {scripts.in_script} of {section.carried} routines, in "
        "dependency order." + (" Triggers are created disabled." if pg else ""),
        f"- Not carried: {section.not_carried} (each blocks cutover until settled; the step "
        "contract's routines section names each one and why).",
    ]
    if not pg:
        lines.append(
            f"- Left to the cutover script: {section.deferred} MySQL triggers and events "
            "(MySQL cannot create a trigger disabled, and an enabled event would run on Aurora "
            "while DMS replicates the source's own event's effects)."
        )
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


def _write_atomic(path: Path, chunks: Iterator[bytes]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as out:
        for chunk in chunks:
            out.write(chunk)
    os.replace(tmp, path)


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
    from src.tools.routines.migration import read_routine_migration

    section = contract.routines
    if section is None:
        raise RoutineScriptError(
            "the step 1 contract has no routines section (written before #1947): run "
            "scripts/run_transformation_contract.py, run_data_movement_plan.py and "
            "render_dms_step.py again"
        )
    engine = contract.target.engine
    carried = _routine_order(section)
    if not carried and not section.deferred:
        # An earlier run's scripts (the deploy script holds routine text) must not linger.
        for name in (DEPLOY_FILE, CHECK_FILE, INSTRUCTIONS_FILE):
            (step_dir / name).unlink(missing_ok=True)
        return RoutineScripts(status="nothing_to_deploy", deferred=0)
    migration = None
    if section.routine_migration_version is not None:
        migration = read_routine_migration(store, db, job_id, section.routine_migration_version)
    if migration is None:
        raise RoutineScriptError(
            f"no routine-migration artifact at routines/v{section.routine_migration_version}: "
            "run scripts/run_transformation_contract.py again"
        )
    user = target_user or migration.target_user
    total = len(carried)
    # A PostgreSQL rewrite that changes the text (the search_path pin) is never applied by
    # the artifact today (the source's search_path is not collected); the deploy uses the
    # exact text, so a routine that has one is refused rather than deployed without it.
    text_rewrites = {
        (r.kind, r.routine_id)
        for r in migration.routines
        if engine == "aurora_postgresql" and any(w.rewrite == "search_path" for w in r.rewrites)
    }
    refused: dict[tuple[str, str], str] = {}
    pending: dict[tuple[str, str, str], StepRoutine] = {}
    for row in carried:
        if (row.kind, row.routine_id) in text_rewrites:
            refused[(row.kind, row.routine_id)] = "rewrite_not_reapplied"
        elif not _names_safe(row):
            refused[(row.kind, row.routine_id)] = "unsafe_name"
        else:
            pending[(row.kind, row.schema_name or "", row.specific_name or "")] = row
    step_dir.mkdir(parents=True, exist_ok=True)
    spans: dict[int, tuple[int, int]] = {}
    with tempfile.TemporaryFile(dir=step_dir) as spool:
        for source in read_routine_sources(store, db, job_id):
            found = pending.pop(source.key(), None)
            if found is None:
                continue
            row = found
            block: str | None
            code: str | None
            if not source.exact:
                block, code = None, "source_text_not_exact"
            elif engine == "aurora_postgresql":
                block, code = _pg_block(row, source, total)
            else:
                database = _target_schema(row, engine, target_database)
                block, code = _mysql_block(row, source, total, migration, user, database)
            if block is None:
                refused[(row.kind, row.routine_id)] = code or "refused"
                continue
            data = block.encode("utf-8")
            spans[int(row.order or 0)] = (spool.tell(), len(data))
            spool.write(data)
            del block, data
        for row in pending.values():
            refused[(row.kind, row.routine_id)] = "source_missing"
        # A routine naming one left out is left out too: its CREATE could fail on it.
        deps = _dependencies(migration)
        for row in carried:
            key = (row.kind, row.routine_id)
            if key not in refused and any(d in refused for d in deps.get(key, ())):
                refused[key] = "depends_on_refused"
                spans.pop(int(row.order or 0), None)
        in_script = len(spans)

        def deploy_chunks() -> Iterator[bytes]:
            yield _deploy_header(engine, section, in_script, contract_sha).encode("utf-8")
            for row in carried:
                span = spans.get(int(row.order or 0))
                if span is None:
                    continue
                spool.seek(span[0])
                yield spool.read(span[1])
            yield _deploy_footer(engine).encode("utf-8")

        _write_atomic(step_dir / DEPLOY_FILE, deploy_chunks())
    check = render_check_script(section, engine, contract_sha, target_database)
    _write_atomic(step_dir / CHECK_FILE, iter([check.encode("utf-8")]))
    refusals = sorted((k, i, c) for (k, i), c in refused.items())
    scripts = RoutineScripts(
        status="ready",
        carried=section.carried,
        in_script=in_script,
        deferred=section.deferred,
        refused=refusals,
    )
    output = str(step_dir / CHECK_OUTPUT)
    _write_atomic(
        step_dir / INSTRUCTIONS_FILE,
        iter([_instructions(engine, scripts, section, output, user).encode("utf-8")]),
    )
    scripts.files = {
        name: str(step_dir / name) for name in (DEPLOY_FILE, CHECK_FILE, INSTRUCTIONS_FILE)
    }
    return scripts
