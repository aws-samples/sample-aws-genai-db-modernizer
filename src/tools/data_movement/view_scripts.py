# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1's view scripts (#2082): generated here, run by the customer.

The modernizer never connects to the customer's databases. After the full load,
``scripts/run_dms_step.py views`` writes three files into ``data-movement/step-1/``, from
the step contract's views section (``step1_views``):

- ``views-deploy.sql``: every view the section carries, in its deploy order (a view after
  the views it reads), each the exact ``CREATE OR REPLACE VIEW`` from the job's routine
  sources (``src.agents.collector.view_sources``), so a second run replaces each view with
  itself. It runs after the full load and before ``routines-deploy.sql``: a routine that
  reads a view needs it, and a view never calls a routine step 1 carries (the section
  leaves such a view out). It first creates, ``IF NOT EXISTS``, each schema (on Aurora
  MySQL, each database) a view lands in that step 1's DDL does not create, by the routines
  deploy script's rules (``schema_statements``: never PostgreSQL's ``public``, a MySQL
  database with the tables' character set and collation, nothing granted).

  On Aurora PostgreSQL it runs in one transaction, each view under the search path its
  definition was printed for. On Aurora MySQL each view runs under its own collation;
  its ``DEFINER`` is left out, so the user who runs the script is its definer (as the
  routines deploy script names that user), its ``SQL SECURITY`` is the source's, and its
  ``ALGORITHM`` is the server's default (the collection cannot read it). A MySQL view's
  text names its source database, so with another target database it is refused.
- ``views-check.sql``: read-only (a ``READ ONLY`` transaction of catalog ``SELECT``\\s):
  one tab-separated line per column of each view it asks for that exists on the target,
  between a header line naming the engine and the contract it was generated for and an
  end line; the same layout as ``routines-check.sql``.
- ``views.md``: how to run both, in order, and where the check's output goes
  (``views-check.tsv``, which ``run_dms_step.py views-check`` reads).

The deploy script is the one file outside the routine sources that holds a view's text:
the text never goes into a JSON artifact, a log, a printed line or a report. A name that
reaches a comment or a literal of either script is written safely (control characters
refused, quotes doubled); a view whose names or session do not pass is refused, by id and
reason code, never written, and so is each view that reads one refused.

Memory: the routine sources are read one chunk at a time, each carried view's block
spooled to a temporary file and copied into the deploy script in deploy order.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from src.agents.collector.routine_sources import read_routine_sources
from src.contracts.routine_sources import RoutineSource
from src.contracts.transformation_contract import StepContract, StepView, StepViews, TargetSpec
from src.shared.cutover_message import CUTOVER_DISCLAIMER
from src.tools.data_movement.routine_scripts import (
    _COLLATION,
    _DELIMITER_LINE,
    CHECK_FORMAT,
    PG_LOCK_TIMEOUT,
    CheckScript,
    RoutineScriptError,
    _comment,
    _mysql_literal,
    _pg_ident,
    _pg_literal,
    _safe,
    _select_line,
    schema_statements,
    step_schemas,
    write_atomic,
)
from src.tools.database.mysql_grants import GRANT_WILDCARD_NOTE
from src.tools.sql.view_statement import (
    AMBIGUOUS,
    CLIENT_COMMAND,
    NOT_ONE_STATEMENT,
    statement_problem,
)

__all__ = [
    "CHECK_FILE",
    "CHECK_OUTPUT",
    "DEPLOY_FILE",
    "INSTRUCTIONS_FILE",
    "REFUSED_WHY",
    "VIEWS_CHECK",
    "ViewScripts",
    "check_key",
    "left_out",
    "left_out_names",
    "render_view_check_script",
    "require_views",
    "write_view_scripts",
]

DEPLOY_FILE = "views-deploy.sql"
CHECK_FILE = "views-check.sql"
CHECK_OUTPUT = "views-check.tsv"
INSTRUCTIONS_FILE = "views.md"
#: Step 1's view check: its header and end lines, title and files.
VIEWS_CHECK = CheckScript(
    "modernizer-views-check", "modernizer-views-check-end", "Step 1 view check", CHECK_FILE,
    CHECK_OUTPUT, "views",
)  # fmt: skip
_STATEMENT_START = "CREATE OR REPLACE "
#: Before each MySQL view (#2082): the session's own sql_mode without the two flags that
#: change where a statement ends (NO_BACKSLASH_ESCAPES, ANSI_QUOTES), every other flag kept;
#: the mysql client follows the server's flag, so it splits the script as the check did.
MYSQL_SQL_MODE = (
    "SET SESSION sql_mode = TRIM(BOTH ',' FROM REPLACE(REPLACE(CONCAT(',', @@SESSION.sql_mode, "
    "','), ',NO_BACKSLASH_ESCAPES,', ','), ',ANSI_QUOTES,', ','));"
)
#: What each refusal of the deploy script means and what to do.
REFUSED_WHY: dict[str, str] = {
    "unsafe_name": "a name holds a control or separator character: create the view yourself",
    "source_missing": (
        "the job's routine sources hold no CREATE for it: collect again with the current "
        "collection script, or create the view yourself"
    ),
    "source_text_not_exact": "its collected CREATE is not exact: create the view yourself",
    "not_a_view_statement": "its collected statement is not a view's CREATE: collect again",
    "session_not_allowed": (
        "its collation is not one the script may set: create the view yourself"
    ),
    "delimiter_in_text": (
        "its text holds a line the mysql client reads as a command: create the view yourself"
    ),
    "target_database_differs": (
        "a MySQL view's text names its source database, and the step's target database is "
        "another one: create the view yourself in the target database, its text naming that "
        "database instead, then run views-check.sql again (it looks for the view there)"
    ),
    AMBIGUOUS: (
        "where its collected text ends depends on how the target reads backslashes "
        "(standard_conforming_strings, NO_BACKSLASH_ESCAPES): it is never written into a "
        "script; create the view yourself"
    ),
    NOT_ONE_STATEMENT: (
        "its collected text is not exactly one view statement (more than one statement, or "
        "one left open): it is never written into a script; check the source view and "
        "create it yourself"
    ),
    CLIENT_COMMAND: (
        "its collected text holds a psql or mysql client command (a backslash command, a "
        "psql variable or a client command line): it is never written into a script; check "
        "the source view and create it yourself"
    ),
    "depends_on_refused": "it reads a view the script leaves out: create both yourself",
}


@dataclass
class ViewScripts:
    """What :func:`write_view_scripts` wrote: counts, files and refusals, no text."""

    status: str  # "ready" or "nothing_to_deploy"
    carried: int = 0
    in_script: int = 0
    refused: list[tuple[str, str]] = field(default_factory=list)  # view id, code
    files: dict[str, str] = field(default_factory=dict)
    #: MySQL: the carried views that were SQL SECURITY DEFINER on the source.
    definer_views: list[str] = field(default_factory=list)


def require_views(contract: StepContract) -> StepViews:
    """The contract's views section; :class:`RoutineScriptError` when it has none."""
    if contract.views_section is None:
        raise RoutineScriptError(
            "the step 1 contract has no views section (written before #2082): run "
            "scripts/run_transformation_contract.py, run_data_movement_plan.py and "
            "render_dms_step.py again"
        )
    return contract.views_section


def _carried(section: StepViews) -> list[StepView]:
    return [v for v in section.views if v.carry == "carried"]


def check_key(row: StepView, engine: str, target_database: str | None = None) -> tuple[str, str]:
    """A view's key in the check output: its schema and name (lower case on MySQL). On Aurora
    MySQL it is looked for in the step's target database when the inputs name one: where
    the step's tables are, and where the customer was told to create it."""
    schema = row.schema_name or ""
    if engine == "aurora_mysql":
        return ((target_database or schema).lower(), row.name.lower())
    return (schema, row.name)


def _names_safe(row: StepView) -> bool:
    return all(_safe(v) for v in (row.schema_name, row.name)) and all(
        _safe(c.name) for c in row.columns
    )


# --------------------------------------------------------------------------- blocks


def _pg_block(
    row: StepView, source: RoutineSource, position: int, total: int
) -> tuple[str | None, str | None]:
    path = source.session.search_path or []
    if not all(_safe(p) for p in path):
        return None, "session_not_allowed"
    lines = [f"-- {position}/{total} view {_comment(row.view_id)}"]
    lines.append(
        f"SET LOCAL search_path TO {', '.join(_pg_ident(p) for p in path)};"
        if path
        else "SET LOCAL search_path TO pg_catalog;"
    )
    lines.append(source.create_statement.rstrip())
    return "\n".join(lines) + "\n\n", None


def _mysql_block(
    row: StepView, source: RoutineSource, position: int, total: int
) -> tuple[str | None, str | None]:
    collation = source.session.collation_connection
    if collation and not _COLLATION.match(collation):
        return None, "session_not_allowed"
    if _DELIMITER_LINE.search(source.create_statement):
        return None, "delimiter_in_text"
    lines = [f"-- {position}/{total} view {_comment(row.view_id)}", "SET NAMES utf8mb4;",
             MYSQL_SQL_MODE]  # fmt: skip
    if collation:
        lines.append(f"SET SESSION collation_connection = {_mysql_literal(collation)};")
    lines.append(source.create_statement.rstrip())
    return "\n".join(lines) + "\n\n", None


def _block(
    engine: str, row: StepView, source: RoutineSource, position: int, total: int
) -> tuple[str | None, str | None]:
    if not source.exact:
        return None, "source_text_not_exact"
    text = source.create_statement.lstrip()
    if not text.upper().startswith(_STATEMENT_START) or " VIEW " not in text[:200].upper():
        return None, "not_a_view_statement"
    # The routine sources may come from an edited collection file: the text reaches the
    # script only as exactly one view statement (ADR-034 Decision 5).
    problem = statement_problem(engine, source.create_statement.strip(), row.schema_name or "",
                                row.name)  # fmt: skip
    if problem:
        return None, problem
    if engine == "aurora_postgresql":
        return _pg_block(row, source, position, total)
    return _mysql_block(row, source, position, total)


# --------------------------------------------------------------------------- the files


def _view_schemas(rows: list[StepView], engine: str, tables_in: set[str]) -> list[str]:
    skip = tables_in | ({"public"} if engine == "aurora_postgresql" else set())
    return sorted({r.schema_name for r in rows if r.schema_name and r.schema_name not in skip})


def _deploy_header(
    engine: str, section: StepViews, in_script: int, contract_sha: str, schemas: list[str],
    target: TargetSpec,
) -> str:  # fmt: skip
    made = schema_statements(engine, schemas, target, views=True)
    lines = [
        f"-- {CUTOVER_DISCLAIMER}",
        "-- Step 1 views: generated by the modernizer; you run it, it never connects to your "
        "databases.",
        f"-- Step contract SHA-256: {contract_sha}",
        f"-- {in_script} of the {section.carried} views step 1 creates, in dependency order "
        "(each after the views it reads).",
        "-- Run it after the DMS full load is complete and before routines-deploy.sql, as the "
        "user the views should belong to, never the admin user: that user owns each view.",
        "-- Each view is CREATE OR REPLACE VIEW: running the script again replaces each one "
        "with itself.",
        "-- Then run views-check.sql (read-only) as views.md says.",
    ]
    if engine == "aurora_postgresql":
        lines += [
            "-- One transaction: if any statement fails, nothing is created; fix it and run "
            "the script again.",
            "-- standard_conforming_strings on, whatever the server's setting: a backslash in a "
            "'...' literal is a plain character, as the views' check read it.",
            "-- Each view runs under the search path its definition was collected with.",
            "-- A view without security_invoker reads its tables with its owner's rights, and "
            "its owner is the user who runs this script.",
            f"-- lock_timeout {PG_LOCK_TIMEOUT}, below PostgreSQL's default deadlock_timeout (1s): "
            "while DMS applies changes, a lock this script waits for makes the script fail, "
            "never DMS's apply; run the script again.",
            "",
            "\\set ON_ERROR_STOP on",
            "SET client_encoding = 'UTF8';",
            "SET standard_conforming_strings = on;",
            "BEGIN;",
            f"SET LOCAL lock_timeout = '{PG_LOCK_TIMEOUT}';",
            "",
            *made,
        ]
    else:
        lines += [
            "-- DEFINER is not carried, so every view is SQL SECURITY INVOKER: it reads its "
            "tables with the rights of whoever selects from it, never with this script's user's.",
            "-- A view that was SQL SECURITY DEFINER on the source needs its readers to have "
            "their own rights on its tables (views.md names each one).",
            "-- ALGORITHM is not in the collection: each view gets the server's default "
            "(UNDEFINED).",
            "-- MySQL commits each CREATE on its own: the client stops at the first error; "
            "views-check.sql then shows which views exist.",
            "-- The session's sql_mode keeps every flag but NO_BACKSLASH_ESCAPES and ANSI_QUOTES, "
            "before each view: the view text is read with backslash escapes and backtick names, "
            "as its check read it, whatever the server's mode.",
            "",
            "SET NAMES utf8mb4;",
            MYSQL_SQL_MODE,
            "",
            *made,
        ]
    return "\n".join(lines) + "\n"


def render_view_check_script(
    section: StepViews, engine: str, contract_sha: str, target_database: str | None
) -> str:
    """The read-only check script: catalog ``SELECT``\\s in a ``READ ONLY`` transaction,
    asking only for the section's carried views with safe names, one line per column."""
    spec = VIEWS_CHECK
    keys = sorted(
        {check_key(r, engine, target_database) for r in _carried(section) if _names_safe(r)}
    )
    header = (spec.magic, CHECK_FORMAT, engine, contract_sha, target_database or "", "", "")
    end = (spec.end, "", "", "", "", "", "")
    if engine == "aurora_postgresql":
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
        if keys:
            values = ",\n  ".join(f"({_pg_literal(s)}, {_pg_literal(n)})" for s, n in keys)
            out.append(
                f"WITH expected(nspname, relname) AS (VALUES\n  {values})\n"  # nosec B608 -- a script the customer runs; every value is a quoted literal of a checked name
                "SELECT 'view', n.nspname, c.relname, coalesce(a.attnum::text, ''),"
                " coalesce(a.attname::text, ''),"
                " coalesce(pg_catalog.format_type(a.atttypid, a.atttypmod), ''), c.relkind::text\n"
                "FROM expected e\n"
                "JOIN pg_catalog.pg_namespace n ON n.nspname = e.nspname\n"
                "JOIN pg_catalog.pg_class c ON c.relnamespace = n.oid AND c.relname = e.relname\n"
                "LEFT JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0"
                " AND NOT a.attisdropped\n"
                "WHERE c.relkind NOT IN ('i', 'I')\n"
                "ORDER BY 2, 3, a.attnum;"
            )
        out += [_select_line(end, _pg_literal), "COMMIT;"]
        return "\n".join(out) + "\n"
    out = [
        f"-- {spec.title}: read-only, information_schema SELECTs only, in a READ ONLY "
        "transaction.",
        f"-- Run: mysql <connection options> --batch --skip-column-names < {spec.script} "
        f"> {spec.output}",
        "START TRANSACTION READ ONLY;",
        _select_line(header, _mysql_literal),
    ]
    if keys:
        pairs = ", ".join(f"({_mysql_literal(s)}, {_mysql_literal(n)})" for s, n in keys)
        out.append(
            "SELECT 'view', t.TABLE_SCHEMA, t.TABLE_NAME, COALESCE(c.ORDINAL_POSITION, ''),"  # nosec B608 -- a script the customer runs; every value is a quoted literal of a checked name
            " COALESCE(c.COLUMN_NAME, ''), COALESCE(c.COLUMN_TYPE, ''), t.TABLE_TYPE\n"
            "FROM information_schema.TABLES t\n"
            "LEFT JOIN information_schema.COLUMNS c ON c.TABLE_SCHEMA = t.TABLE_SCHEMA"
            " AND c.TABLE_NAME = t.TABLE_NAME\n"
            f"WHERE (LOWER(t.TABLE_SCHEMA), LOWER(t.TABLE_NAME)) IN ({pairs})\n"
            "ORDER BY 2, 3, c.ORDINAL_POSITION;"
        )
    out += [_select_line(end, _mysql_literal), "COMMIT;"]
    return "\n".join(out) + "\n"


def _instructions(engine: str, scripts: ViewScripts, section: StepViews, output_path: str,
                  created: int) -> str:  # fmt: skip
    pg = engine == "aurora_postgresql"
    deploy = (
        'psql "host=<Aurora writer endpoint> port=5432 dbname=<database> user=<user> '
        f'sslmode=verify-full" -X -f {DEPLOY_FILE}'
        if pg
        else "mysql --host=<Aurora writer endpoint> --user=<user> -p "
        f"--default-character-set=utf8mb4 < {DEPLOY_FILE}"
    )
    check = (
        'psql "host=<Aurora writer endpoint> port=5432 dbname=<database> user=<user> '
        f'sslmode=verify-full" -X -f {CHECK_FILE} -o {CHECK_OUTPUT}'
        if pg
        else "mysql --host=<Aurora writer endpoint> --user=<user> -p --batch --skip-column-names "
        f"< {CHECK_FILE} > {CHECK_OUTPUT}"
    )
    lines = [
        CUTOVER_DISCLAIMER,
        "",
        "# Step 1 views",
        "",
        "The modernizer never connects to your databases. You run these two scripts on your "
        "Aurora writer, with your own credentials.",
        "",
        f"- Created by `{DEPLOY_FILE}`: {scripts.in_script} of {section.carried} views, in "
        "dependency order. Each is `CREATE OR REPLACE VIEW`, so running it again is safe.",
        f"- Not carried: {section.not_carried} (each is yours to create; the step contract's "
        "views section and the gate name each one and why).",
    ]
    if created:
        lines.append(
            f"- Schemas it creates, which step 1's DDL does not: {created} (`{DEPLOY_FILE}` "
            "names them). Each is owned by the user that runs it, and the script grants "
            "nothing: `USAGE` on each schema, and `SELECT` on its views, for the application "
            "user is yours to add."
            if pg
            else f"- Databases it creates, which step 1's DDL does not: {created} (`{DEPLOY_FILE}` "
            "names them). Each is created by the user that runs it, and the script grants "
            "nothing: `SELECT` on each database for the application user is yours to add."
        )
    if scripts.refused:
        lines.append(
            f"- Left out of the script: {len(scripts.refused)} (`run_dms_step.py views` names "
            "each one and why); until you create them, the check reports them missing."
        )
    if pg:
        lines.append(
            "- Each view is owned by the user who runs the script, and a view without "
            "`security_invoker` reads its tables with its owner's rights: run it as the user "
            "the views should belong to, never the admin user."
        )
    else:
        lines.append(
            "- Every view is created `SQL SECURITY INVOKER`: the `DEFINER` is not carried, and "
            "a `SQL SECURITY DEFINER` view would read its tables with the rights of the user "
            "who runs the script. Its `ALGORITHM` is the server's default: the collection "
            "cannot read the source's."
        )
        if scripts.definer_views:
            lines.append(
                f"- `SQL SECURITY DEFINER` on the source, `INVOKER` here: "
                f"{', '.join(f'`{_comment(v).replace(chr(96), chr(39))}`' for v in scripts.definer_views)}. "
                "Their readers need `SELECT` on the views' tables; to keep definer rights, "
                "`ALTER` the view `SQL SECURITY DEFINER` as the least-privileged user that "
                "should own it, never the admin user."
            )
        lines.append(f"- {GRANT_WILDCARD_NOTE}")
    lines += [
        "",
        "## Order",
        "",
        "1. Wait until the DMS full load is complete (`/move` shows it at 100%).",
        f"2. Run the deploy script, as the user the views should belong to, before "
        f"`routines-deploy.sql`:\n\n   ```\n   {deploy}\n   ```",
        f"3. Run the read-only check script and save its output next to these files, at "
        f"`{output_path}`:\n\n   ```\n   {check}\n   ```",
        "4. Ask `/move step-1` to read the check (`run_dms_step.py views-check`), then run "
        "`routines-deploy.sql` as `routines.md` says.",
        "",
    ]
    lines.append(
        "The deploy script runs in one transaction: if a statement fails, nothing is created; "
        "fix the cause and run it again. If it stops on a lock timeout (`canceling statement "
        "due to lock timeout`), DMS was applying changes to a table a view reads: run it "
        "again as it is."
        if pg
        else "MySQL commits each CREATE on its own and the client stops at the first error: "
        "the check then shows which views exist; fix the cause and run the script again."
    )
    return "\n".join(lines) + "\n"


def _spool(
    store: Any, db: str, job_id: str, contract: StepContract, target_database: str | None,
    spool: IO[bytes],
) -> tuple[dict[str, tuple[int, int]], dict[str, str]]:  # fmt: skip
    """Each carried view's block spooled to ``spool`` (its ``(offset, length)``) and the
    reason code of each view the script leaves out, in deploy order."""
    engine = contract.target.engine
    carried = _carried(require_views(contract))
    total = len(carried)
    refused: dict[str, str] = {}
    pending: dict[tuple[str, str, str], StepView] = {}
    for row in carried:
        if not _names_safe(row):
            refused[row.view_id] = "unsafe_name"
        elif (engine == "aurora_mysql" and target_database
              and target_database != (row.schema_name or "")):  # fmt: skip
            refused[row.view_id] = "target_database_differs"
        else:
            pending[("view", row.schema_name or "", row.name)] = row
    spans: dict[str, tuple[int, int]] = {}
    for source in read_routine_sources(store, db, job_id):
        if source.kind != "view":
            continue
        found = pending.pop(source.key(), None)
        if found is None:
            continue
        text, code = _block(engine, found, source, found.order or 0, total)
        if text is None:
            refused[found.view_id] = code or "refused"
            continue
        data = text.encode("utf-8")
        spans[found.view_id] = (spool.tell(), len(data))
        spool.write(data)
        del text, data
    for row in pending.values():
        refused[row.view_id] = "source_missing"
    # A view reading one left out is left out too: its CREATE would fail on it.
    for row in carried:  # deploy order: each view after the ones it reads
        if row.view_id not in refused and any(r in refused for r in row.reads):
            refused[row.view_id] = "depends_on_refused"
            spans.pop(row.view_id, None)
    return spans, refused


def left_out(
    store: Any, db: str, job_id: str, contract: StepContract, target_database: str | None
) -> dict[str, str]:
    """The carried views ``views-deploy.sql`` leaves out, with their codes, without writing
    anything: what the routines deploy and the gate read (a routine on such a view would
    fail its ``CREATE``; the gate names why). Empty without a views section."""
    if contract.views_section is None or not contract.views_section.carried:
        return {}
    with tempfile.TemporaryFile() as spool:
        return _spool(store, db, job_id, contract, target_database, spool)[1]


def left_out_names(contract: StepContract, refused: dict[str, str]) -> set[str]:
    """The left-out views by the names a routine's dependency uses: lower-case
    ``schema.name`` and view id."""
    out: set[str] = set()
    for v in (contract.views_section.views if contract.views_section else ()):
        if v.view_id in refused:
            out.add(v.view_id.lower())
            out.add(f"{v.schema_name}.{v.name}".lower() if v.schema_name else v.name.lower())
    return out


def write_view_scripts(
    store: Any,
    db: str,
    job_id: str,
    contract: StepContract,
    contract_sha: str,
    step_dir: Path,
    *,
    target_database: str | None = None,
) -> ViewScripts:
    """Write the step's view scripts and instructions into ``step_dir`` (module
    docstring). ``nothing_to_deploy`` (and no file, an earlier run's removed) when the
    section carries no view."""
    section = require_views(contract)
    engine = contract.target.engine
    carried = _carried(section)
    if not carried:
        for name in (DEPLOY_FILE, CHECK_FILE, INSTRUCTIONS_FILE):
            (step_dir / name).unlink(missing_ok=True)  # the deploy script holds view text
        return ViewScripts(status="nothing_to_deploy")
    step_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile(dir=step_dir) as spool:
        spans, refused = _spool(store, db, job_id, contract, target_database, spool)
        in_rows = [r for r in carried if r.view_id in spans]
        schemas = _view_schemas(in_rows, engine, step_schemas(contract))

        def deploy_chunks() -> Iterator[bytes]:
            yield _deploy_header(engine, section, len(spans), contract_sha, schemas,
                                 contract.target).encode("utf-8")  # fmt: skip
            for row in in_rows:
                offset, length = spans[row.view_id]
                spool.seek(offset)
                yield spool.read(length)
            if engine == "aurora_postgresql":
                yield b"COMMIT;\n"

        write_atomic(step_dir / DEPLOY_FILE, deploy_chunks())
    check = render_view_check_script(section, engine, contract_sha, target_database)
    write_atomic(step_dir / CHECK_FILE, iter([check.encode("utf-8")]))
    scripts = ViewScripts(status="ready", carried=section.carried, in_script=len(in_rows),
                          refused=sorted(refused.items()),
                          definer_views=[r.view_id for r in carried
                                         if r.source_security == "DEFINER"])  # fmt: skip
    output = str(step_dir / CHECK_OUTPUT)
    write_atomic(
        step_dir / INSTRUCTIONS_FILE,
        iter([_instructions(engine, scripts, section, output, len(schemas)).encode("utf-8")]),
    )
    scripts.files = {n: str(step_dir / n) for n in (DEPLOY_FILE, CHECK_FILE, INSTRUCTIONS_FILE)}
    return scripts
