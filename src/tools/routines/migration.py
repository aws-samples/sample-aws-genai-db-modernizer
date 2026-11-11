# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The routine-migration phase (#1934, ADR-034): how each collected routine migrates.

Deterministic, no model, in every LLM mode. For one job and one assignment version it
reads the collection, the assignment and the job's routine sources (the exact ``CREATE``
statements, ADR-034 Decision 4), runs the routine checks (#1935, #1936, #1951, #1952),
the safe header rewrites and the dependency check and order (#1937) on every
PostgreSQL or MySQL function, procedure, trigger and event, and writes one
:class:`~src.contracts.routine_migration.RoutineMigrationContract` at
``<db>/<job>/routines/v<N>/routine_migration.json``. Nothing is written for a
collection with no routines, or from an engine routines are not carried from yet.

What each routine is checked against:

- **Target**: the same-family Aurora engine (ADR-034 Decision 1), on the line the gap
  check's move lands on for PostgreSQL (:func:`~src.tools.routines.postgresql_checks.
  routine_target_line`), and the source's own line for MySQL when Aurora lists it (else
  every line).
- **Text**: the exact ``CREATE`` from the routine sources, read one chunk at a time and
  never kept: only the rule ids and findings of the checks and the rule ids of the
  rewrites are (no diff: a deploy applies the same rules to the exact text again). A
  routine with no entry there (no routine sources, a body not collected) is
  ``needs_attention`` with ``source_text_not_collected``; a MySQL rebuild around a
  literal is checked but ``needs_attention`` with ``source_text_not_exact``.
- **Target user**: Aurora's default master user (``postgres``, ``admin``), named in the
  artifact; a deploy re-applies the rewrites for its own user.
- **Not collected, so not applied**: the source's ``search_path`` (so the ``search_path``
  pin is not applied), each function's extensions, and whether a ``SECURITY DEFINER``
  routine's owner is a superuser (that check is ``unknown``, never a pass).
- **Dependencies**: step 1 mirrors the whole database onto Aurora, so every table and
  view the collection has is there; a table or routine the collection lacks fails the
  dependency rule (``depends_on_table_not_collected``,
  ``depends_on_routine_not_collected``). A table the end-state assignment moves off
  Aurora is recorded (``tables_leaving_aurora``, with its engines) and never changes the
  status. A dependency cycle makes each member ``needs_attention``
  (``dependency_cycle``), and the artifact lists it. A routine naming one that needs
  attention, directly or through others, needs attention too
  (``depends_on_needs_attention``, the routines it names in ``blocked_by``).

- **Rows read**: every row with code or a ``body_status``. A row with neither, from
  any source, is a name only: a collection written before #1652 lists routines so, the
  functions and aggregates an extension owns among them (``CREATE EXTENSION`` creates
  them on Aurora; today's collectors leave them out by ``pg_depend``'s extension
  membership). It is not read (:func:`listed_by_name_only`, #2124), and the run log
  counts it. A DDL upload's or an ``assess()`` collection's row with code and no status
  is read.
Status: only a failed check (one whose impact is not informational) or a reason code
makes a routine ``needs_attention``. A check whose outcome is ``unknown`` (it could not be
checked: an incomplete dependency list, the superuser flag) never changes the status, so
``as_is`` and ``rewritten`` do not mean every check passed: the row lists the unknown ones.

A PostgreSQL routine dependency is matched by name, not by argument types (the collection
records the callee's name; overloads share it), so a caller of ``f(integer)`` is ordered
after, and blocked by, every overload of ``f``: safe, but it may name the wrong overload.

Routines never change routing (ADR-034 Decision 8): this module reads the assignment and
nothing in routing or synthesis reads this module.

Cost: linear in the routines (the dependency order adds a log factor); one chunk of
routine sources in memory at a time, the per-routine records small and text-free.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import pgparse

from src.contracts.routine_migration import (
    RoutineCheckEntry,
    RoutineDependencyEntry,
    RoutineMigration,
    RoutineMigrationContract,
    RoutineRef,
    RoutineRewriteEntry,
    RoutineRuleEntry,
    TableLeavingAurora,
)
from src.contracts.routine_sources import RoutineSource
from src.shared.catalog.gaps import aurora_version_lines, parse_version_line
from src.shared.catalog.routine_checks import routine_check_rules
from src.shared.catalog.routine_rewrites import routine_rewrite_rules
from src.storage.routine_migration_store import (  # re-exported: the phase's readers
    latest_routine_migration,
    read_routine_migration,
    routine_migration_path,
)
from src.tools.routines.dependencies import (
    MovedObjects,
    RoutineNode,
    check_dependencies,
    dependency_order,
    postgresql_node,
)
from src.tools.routines.mysql_checks import MySqlRoutine, check_mysql_routine, mysql_header
from src.tools.routines.postgresql_checks import (
    PostgresRoutine,
    check_postgresql_routine,
    routine_target_line,
)
from src.tools.routines.rewrites import rewrite_mysql_routine, rewrite_postgresql_routine
from src.tools.routines.verdict import RoutineCheckResult, RoutineDependency, RoutineVerdict

__all__ = [
    "DEFAULT_TARGET_USERS",
    "RoutineMigrationError",
    "error_message",
    "latest_routine_migration",
    "listed_by_name_only",
    "name_only_routine_count",
    "name_only_routines_of_job",
    "ensure_routine_migration",
    "read_routine_migration",
    "routine_migration_path",
    "run_routine_migration_deterministic",
    "write_routine_migration",
]

logger = logging.getLogger(__name__)

#: Aurora's default master user name per target engine: the user the rewrites name
#: until a deploy names its own.
DEFAULT_TARGET_USERS = {"aurora_postgresql": "postgres", "aurora_mysql": "admin"}
_TARGETS = {"postgresql": "aurora_postgresql", "mysql": "aurora_mysql"}
_SUPERUSER_UNKNOWN = "the collection does not say whether the owner is a superuser"
_NOT_COLLECTED = "source_text_not_collected"
_NOT_EXACT = "source_text_not_exact"
_CYCLE = "dependency_cycle"
_TABLE_MISSING = "depends_on_table_not_collected"
_ROUTINE_MISSING = "depends_on_routine_not_collected"
_BLOCKED = "depends_on_needs_attention"
# PostgreSQL's internal type names, as pg_get_function_identity_arguments prints them.
_PG_TYPES = {
    "int2": "smallint",
    "int4": "integer",
    "int8": "bigint",
    "float4": "real",
    "float8": "double precision",
    "bool": "boolean",
    "varchar": "character varying",
    "bpchar": "character",
    "timestamp": "timestamp without time zone",
    "timestamptz": "timestamp with time zone",
    "time": "time without time zone",
    "timetz": "time with time zone",
    "varbit": "bit varying",
}
_PG_MODES = {"FUNC_PARAM_INOUT": "INOUT ", "FUNC_PARAM_VARIADIC": "VARIADIC "}
_MYSQL_CHARACTERISTICS = {
    "COMMENT",
    "LANGUAGE",
    "NOT",
    "DETERMINISTIC",
    "CONTAINS",
    "NO",
    "READS",
    "MODIFIES",
    "SQL",
}


class RoutineMigrationError(Exception):
    """The phase failed on one routine: its message names the exception type and the
    routine, never the exception's own message, which may quote the routine's text."""


def error_message(exc: BaseException) -> str:
    """A message for a failure of the phase that holds no routine text: the routine and
    the exception type for a :class:`RoutineMigrationError`, else the type only."""
    if isinstance(exc, RoutineMigrationError):
        return f"routine migration failed: {exc}"
    return f"routine migration failed: {type(exc).__name__}"


# --------------------------------------------------------------------------- the routines


@dataclass
class _Routine:
    """One routine of the collection, and what the phase finds about it."""

    kind: str
    schema: str | None
    name: str
    specific: str | None
    table: str | None
    row: Mapping[str, Any]
    routine_id: str = ""
    signature: str | None = None
    returns: str | None = None
    source_text: Literal["exact", "not_exact", "not_collected"] = "not_collected"
    checks: list[RoutineCheckEntry] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    rules_passed: int = 0
    rewrites: list[RoutineRewriteEntry] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    base_status: str = "as_is"
    node: RoutineNode | None = None

    @property
    def ref(self) -> str:
        return f"{self.kind}:{self.routine_id}"

    @property
    def source_key(self) -> tuple[str, str, str]:
        """The routine sources' key for it: kind, schema, specific name."""
        return (self.kind, self.schema or "", self.specific or "")


def _text(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _value(item: object) -> str:
    return str(getattr(item, "value", item) or "")


def _kind_of(row: Mapping[str, Any]) -> str:
    return "procedure" if _value(row.get("procedure_type")).upper() == "PROCEDURE" else "function"


def listed_by_name_only(row: Mapping[str, Any]) -> bool:
    """True for a routine row with neither a ``body_status`` nor any code
    (``definition``), whatever produced it: a collection written before #1652 lists every
    routine so, functions and aggregates an extension owns among them (``CREATE
    EXTENSION`` creates those on Aurora; today's collectors leave them out by
    ``pg_depend``'s extension membership), and such a row cannot say which it is. It is
    not a routine to carry or to migrate by hand (#2124). A row that carries code is
    read, status or not: a DDL upload (``parse_ddl_routines``) and an ``assess()``
    collection give code with no status, and such a routine still gets its verdict, the
    HIGH to-do included."""
    return _text(_value(row.get("body_status"))) is None and _text(row.get("definition")) is None


def name_only_routine_count(collector: Mapping[str, Any]) -> int:
    """How many of the collection's routine rows :func:`listed_by_name_only` leaves out."""
    schema = collector.get("database_schema") or {}
    return sum(
        1
        for key in ("procedures", "triggers", "events")
        for row in schema.get(key) or []
        if isinstance(row, Mapping) and listed_by_name_only(row)
    )


def name_only_routines_of_job(store: Any, database_name: str, job_id: str) -> int:
    """:func:`name_only_routine_count` of the job's collector output; 0 when it has none."""
    key = f"{database_name}/{job_id}/collector/output.json"
    if store.exists(key) is not True:
        return 0
    collector = store.read_json(key)
    return name_only_routine_count(collector) if isinstance(collector, Mapping) else 0


def _collect(schema: Mapping[str, Any], engine: str) -> list[_Routine]:
    routines: list[_Routine] = []
    for row in schema.get("procedures") or []:
        if isinstance(row, Mapping) and listed_by_name_only(row):
            continue  # #2124: a collection before #1652, names only
        if isinstance(row, Mapping) and _text(row.get("procedure_name")):
            routines.append(
                _Routine(
                    kind=_kind_of(row),
                    schema=_text(row.get("schema_name")),
                    name=str(row["procedure_name"]).strip(),
                    specific=_text(row.get("specific_name")) or _text(row.get("procedure_name")),
                    table=None,
                    row=row,
                )
            )
    for row in schema.get("triggers") or []:
        if isinstance(row, Mapping) and listed_by_name_only(row):
            continue
        if isinstance(row, Mapping) and _text(row.get("trigger_name")):
            table = _text(row.get("table_id"))
            routines.append(
                _Routine(
                    kind="trigger",
                    schema=_text(row.get("schema_name")),
                    name=str(row["trigger_name"]).strip(),
                    specific=f"{table or ''}.{str(row['trigger_name']).strip()}",
                    table=table,
                    row=row,
                )
            )
    if engine == "mysql":
        for row in schema.get("events") or []:
            if isinstance(row, Mapping) and listed_by_name_only(row):
                continue
            if isinstance(row, Mapping) and _text(row.get("event_name")):
                routines.append(
                    _Routine(
                        kind="event",
                        schema=_text(row.get("schema_name")),
                        name=str(row["event_name"]).strip(),
                        specific=str(row["event_name"]).strip(),
                        table=None,
                        row=row,
                    )
                )
    return routines


def _qualified(schema: str | None, name: str) -> str:
    return f"{schema}.{name}" if schema else name


def _assign_ids(routines: list[_Routine], engine: str) -> None:
    """Each routine's ``routine_id``; two that would share one (an old collection with no
    signatures) are told apart by a ``[n]`` suffix in collection order."""
    for r in routines:
        if engine == "postgresql" and r.kind in ("function", "procedure"):
            r.signature, r.returns = _pg_signature(r.row.get("definition"), r.kind)
            base = _qualified(r.schema, r.name)
            r.routine_id = f"{base}({r.signature})" if r.signature is not None else base
        elif engine == "postgresql" and r.kind == "trigger":
            r.routine_id = _qualified(r.schema, f"{r.table}.{r.name}" if r.table else r.name)
        else:
            r.routine_id = _qualified(r.schema, r.name)
    seen: dict[tuple[str, str], int] = {}
    for r in routines:
        key = (r.kind, r.routine_id)
        if key in seen:
            seen[key] += 1
            r.routine_id = f"{r.routine_id}[{seen[key]}]"
        else:
            seen[key] = 1


# --------------------------------------------------------------------------- signatures


def _pg_type(node: Mapping[str, Any]) -> str:
    names = [
        str((n.get("String") or {}).get("sval") or "")
        for n in node.get("names") or ()
        if isinstance(n, Mapping)
    ]
    if names and names[0] == "pg_catalog":
        names = names[1:]
    if len(names) == 1:
        names = [_PG_TYPES.get(names[0], names[0])]
    text = ".".join(names)
    if node.get("pct_type"):
        text += "%TYPE"
    text += "[]" * len(node.get("arrayBounds") or ())
    return ("SETOF " if node.get("setof") else "") + text


def _pg_signature(definition: object, kind: str) -> tuple[str | None, str | None]:
    """The identity argument types of a PostgreSQL ``CREATE FUNCTION`` / ``PROCEDURE``
    and its return type, from PostgreSQL's parser; types only, never a default. Read from
    the collection's text (its literals are redacted, its types are not). ``(None, None)``
    when there is no text or it does not parse."""
    if not isinstance(definition, str) or not definition.strip():
        return None, None
    try:
        tree = pgparse.parse(definition)
        stmt = (tree[0].get("stmt") or {}).get("CreateFunctionStmt") if len(tree) == 1 else None
    except Exception:  # noqa: BLE001 -- text that does not parse has no signature
        return None, None
    if not isinstance(stmt, Mapping):
        return None, None
    skipped = {"FUNC_PARAM_TABLE"} | ({"FUNC_PARAM_OUT"} if kind == "function" else set())
    args = []
    for item in stmt.get("parameters") or ():
        param = item.get("FunctionParameter") if isinstance(item, Mapping) else None
        if not isinstance(param, Mapping) or param.get("mode") in skipped:
            continue
        prefix = "OUT " if param.get("mode") == "FUNC_PARAM_OUT" else ""
        args.append(prefix + _PG_MODES.get(str(param.get("mode")), "") + _pg_type(param["argType"]))
    returns = stmt.get("returnType")
    return ", ".join(args), (_pg_type(returns) if isinstance(returns, Mapping) else None)


def _render(tokens: Sequence[Any]) -> str:
    """Tokens as text, every string literal written ``?``."""
    out = ""
    for tok in tokens:
        text = (
            "?"
            if tok.kind in ("STRING", "NATIONAL_STRING", "HEX_STRING", "BIT_STRING")
            else tok.text
        )
        if out and not out.endswith("(") and text not in (")", ",", "("):
            out += " "
        out += text
    return out.replace(",", ", ")


def _mysql_signature(routine: MySqlRoutine) -> tuple[str | None, str | None]:
    """A MySQL routine's parameters (mode and type each, no name) and a function's return
    type, from its header's tokens; every string literal (an ``ENUM`` value) is ``?``."""
    if routine.kind not in ("function", "procedure"):
        return None, None
    header = mysql_header(routine)
    if header is None:
        return None, None
    toks = [t for t in header.tokens if t.start < header.body_start]
    try:
        opening = next(i for i, t in enumerate(toks) if t.kind == "L_PAREN")
    except StopIteration:
        return None, None
    depth, closing = 0, None
    params: list[list[Any]] = []
    current: list[Any] = []
    for i in range(opening + 1, len(toks)):
        tok = toks[i]
        if tok.kind == "L_PAREN":
            depth += 1
        elif tok.kind == "R_PAREN":
            if depth == 0:
                closing = i
                break
            depth -= 1
        if tok.kind == "COMMA" and depth == 0:
            params.append(current)
            current = []
        else:
            current.append(tok)
    if closing is None:
        return None, None
    if current:
        params.append(current)
    rendered = []
    for param in params:
        mode = (
            param[0].text.upper()
            if param and param[0].text.upper() in ("IN", "OUT", "INOUT")
            else ""
        )
        rest = param[1:] if mode else param
        rendered.append((mode + " " if mode else "") + _render(rest[1:]))
    returns = None
    after = toks[closing + 1 :]
    if after and after[0].text.upper() == "RETURNS":
        kept = []
        for tok in after[1:]:
            if tok.text.upper() in _MYSQL_CHARACTERISTICS:
                break
            kept.append(tok)
        returns = _render(kept) or None
    return ", ".join(rendered), returns


# --------------------------------------------------------------------------- checks


def _compact(verdict: RoutineVerdict, r: _Routine) -> None:
    for c in verdict.checks:
        if c.outcome == "pass":
            r.rules_passed += 1
        else:
            r.checks.append(
                RoutineCheckEntry(rule_id=c.rule_id, outcome=c.outcome, findings=list(c.findings))
            )


def _pg_dependencies(row: Mapping[str, Any]) -> list[RoutineDependency]:
    """The collection's ``pg_depend`` dependencies as the dependency check reads them."""
    out = []
    for dep in row.get("depends_on") or ():
        kind = _value(dep.get("kind")) if isinstance(dep, Mapping) else ""
        name = str(dep.get("name") or "") if isinstance(dep, Mapping) else ""
        if kind in ("table", "view"):
            target = name
        elif kind == "function":
            target = name.split("(", 1)[0]
        else:
            continue
        schema, _, base = target.rpartition(".")
        base = base.strip().strip('"')
        if base:
            out.append(
                RoutineDependency(
                    kind="table" if kind != "function" else "routine",
                    name=base.lower(),
                    schema_name=schema.strip().strip('"').lower() or None,
                    routine_kind="function" if kind == "function" else None,
                )
            )
    return out


def _with_superuser_unknown(verdict: RoutineVerdict) -> RoutineVerdict:
    """The ownership check left ``unknown``: the collection does not say whether the owner
    of a ``SECURITY DEFINER`` routine is a superuser."""
    checks = tuple(
        (
            RoutineCheckResult(
                **{**c.model_dump(), "outcome": "unknown", "findings": (_SUPERUSER_UNKNOWN,)}
            )
            if c.check == "ownership"
            else c
        )
        for c in verdict.checks
    )
    return verdict.model_copy(update={"checks": checks})


@dataclass(frozen=True)
class _Target:
    engine: str
    line: str | None
    user: str
    version: str | None


def _check_pg(r: _Routine, text: str, target: _Target) -> None:
    row = r.row
    definer = bool(row.get("security_definer")) if r.kind != "trigger" else False
    routine = PostgresRoutine(
        routine_id=r.ref,
        kind=r.kind,  # type: ignore[arg-type]
        create_sql=text,
        language=None if r.kind == "trigger" else (_text(row.get("language")) or "unknown"),
        owner=_text(row.get("owner")),
        owner_is_superuser=None if r.kind == "trigger" else False,
        security_definer=definer,
    )
    verdict = check_postgresql_routine(
        routine,
        target_engine=target.engine,
        target_line=str(target.line),
        source_version=target.version,
    )
    if definer:
        verdict = _with_superuser_unknown(verdict)
    r.node = postgresql_node(
        routine,
        schema_name=r.schema,
        name=r.name,
        dependencies=_pg_dependencies(row),
        dependencies_complete=False if r.kind != "trigger" else None,
    )
    rules = [rule for rule in routine_rewrite_rules() if rule.match.rewrite != "search_path"]
    done = rewrite_postgresql_routine(
        routine, verdict, target_owner=target.user, search_path=("public",), rules=rules
    )
    _record(r, verdict, done.record)


def _check_mysql(r: _Routine, text: str, sql_mode: str | None, target: _Target) -> None:
    row = r.row
    security = row.get("security_definer")
    routine = MySqlRoutine(
        routine_id=_qualified(r.schema, r.name),
        kind=r.kind,  # type: ignore[arg-type]
        create_sql=text,
        definer=_text(row.get("owner")),
        sql_mode=sql_mode if sql_mode is not None else str(row.get("sql_mode") or ""),
        security_type=None if security is None else ("DEFINER" if security else "INVOKER"),
    )
    verdict = check_mysql_routine(
        routine,
        target_engine=target.engine,
        target_line=target.line,
        target_user=target.user,
        source_version=target.version,
    )
    r.signature, r.returns = _mysql_signature(routine)
    r.node = RoutineNode(
        routine_id=r.ref,
        source_engine="mysql",
        kind=r.kind,  # type: ignore[arg-type]
        schema_name=r.schema,
        name=r.name,
        dependencies=verdict.dependencies,
        dependencies_complete=verdict.dependencies_complete,
    )
    done = rewrite_mysql_routine(routine, verdict, target_user=target.user)
    _record(r, verdict, done.record)


def _record(r: _Routine, verdict: RoutineVerdict, record: Any) -> None:
    _compact(verdict, r)
    r.failed = list(record.failed_rules)
    r.base_status = record.status
    r.rewrites = [
        RoutineRewriteEntry(rule_id=w.rule_id, rewrite=w.rewrite) for w in record.rewrites
    ]
    r.notes = sorted({n.code for n in record.notes})


def _textless_node(r: _Routine, engine: str) -> RoutineNode:
    deps: list[RoutineDependency] = []
    if engine == "postgresql":
        deps = _pg_dependencies(r.row)
        if r.kind == "trigger" and r.table:
            schema, _, table = r.table.rpartition(".")
            deps.append(
                RoutineDependency(
                    kind="table", name=table.lower(), schema_name=schema.lower() or None
                )
            )
    return RoutineNode(
        routine_id=r.ref,
        source_engine=engine,  # type: ignore[arg-type]
        kind=r.kind,  # type: ignore[arg-type]
        schema_name=r.schema,
        name=r.name,
        dependencies=tuple(deps),
        dependencies_complete=False,
    )


# --------------------------------------------------------------------------- the target


def _family(collector: Mapping[str, Any]) -> str:
    source = (collector.get("metadata") or {}).get("source_database") or {}
    engine = _value(source.get("engine")).strip().lower()
    if engine in ("mysql", "mariadb"):
        return "mysql"
    return "postgresql" if engine in ("postgresql", "postgres") else engine


def _target(collector: Mapping[str, Any], engine: str) -> _Target:
    metadata = collector.get("metadata") or {}
    version = _text((metadata.get("source_database") or {}).get("version"))
    target_engine = _TARGETS[engine]
    if engine == "postgresql":
        installed = {
            str(e.get("name")).strip().lower(): (_text(e.get("version")))
            for e in metadata.get("extensions") or ()
            if isinstance(e, Mapping) and _text(e.get("name"))
        }
        line = routine_target_line(version, installed or None, target_engine) or (
            routine_target_line(version, installed or None, target_engine, target_choice="newest")
        )
    else:
        own = parse_version_line("mysql", version)
        lines = aurora_version_lines()[target_engine].lines
        line = next((ln for ln in lines if parse_version_line("mysql", ln) == own), None)
    return _Target(target_engine, line, DEFAULT_TARGET_USERS[target_engine], version)


def _collected_tables(collector: Mapping[str, Any], schemas: set[str]) -> list[tuple[str, str]]:
    """Every table and view the collection has, as ``(schema, name)``: step 1 mirrors the
    whole database onto Aurora, whatever the end-state assignment says. A name with no
    schema counts in every routine schema."""
    schema = collector.get("database_schema") or {}
    pairs: list[tuple[str, str]] = []
    for kind, name_key in (("tables", "table_name"), ("views", "view_name")):
        for obj in schema.get(kind) or ():
            if not isinstance(obj, Mapping) or not _text(obj.get(name_key)):
                continue
            owner = _text(obj.get("schema_name"))
            for s in [owner] if owner else ["", *schemas]:
                pairs.append(((s or "").lower(), str(obj[name_key]).strip().lower()))
    return pairs


def _leaving_aurora(
    collector: Mapping[str, Any], assignment: Mapping[str, Any] | None, target: str
) -> dict[tuple[str, str], TableLeavingAurora]:
    """The collection's tables the end-state assignment puts on other engines only (no
    Aurora source of truth, no Aurora replica), by ``(schema, name)``, lower case."""
    mappings = {
        str(m.get("table_id")): m
        for m in (assignment or {}).get("table_assignments") or ()
        if isinstance(m, Mapping)
    }
    out: dict[tuple[str, str], TableLeavingAurora] = {}
    for table in (collector.get("database_schema") or {}).get("tables") or ():
        if not isinstance(table, Mapping) or not _text(table.get("table_name")):
            continue
        m = mappings.get(str(table.get("table_id")))
        engines = sorted({str(e) for e in (m or {}).get("engines") or ()})
        truth = _text((m or {}).get("source_of_truth"))
        if m is None or target in engines or truth == target:
            continue
        schema, name = _text(table.get("schema_name")), str(table["table_name"]).strip()
        out[((schema or "").lower(), name.lower())] = TableLeavingAurora(
            table=_qualified(schema, name), engines=engines, source_of_truth=truth
        )
    return out


# --------------------------------------------------------------------------- the phase


def run_routine_migration_deterministic(
    collector: Mapping[str, Any],
    assignment: Mapping[str, Any] | None,
    sources: Iterable[RoutineSource] | None,
    *,
    job_id: str,
    database_name: str,
    assignment_version: int,
) -> RoutineMigrationContract | None:
    """The routine-migration artifact for ``collector`` under ``assignment`` (module
    docstring). ``sources`` are the job's routine sources, read once as they come (None
    or empty when the job has none). None when the collection lists no routine, or comes from an
    engine routines are not carried from yet."""
    engine = _family(collector)
    if engine not in _TARGETS:
        return None
    routines = _collect(collector.get("database_schema") or {}, engine)
    if not routines:
        return None
    _assign_ids(routines, engine)
    target = _target(collector, engine)
    by_source: dict[tuple[str, str, str], _Routine | None] = {}
    for r in routines:
        by_source.setdefault(r.source_key, r)
    read = 0
    for source in sources if sources is not None else ():
        read += 1
        found = by_source.pop((source.kind, source.schema_name or "", source.specific_name), None)
        if found is None:
            continue
        found.source_text = "exact" if source.exact else "not_exact"
        try:
            if engine == "postgresql":
                _check_pg(found, source.create_statement, target)
            else:
                _check_mysql(found, source.create_statement, source.session.sql_mode, target)
        except Exception as exc:  # noqa: BLE001 -- its message may quote the routine's text
            raise RoutineMigrationError(
                f"{type(exc).__name__} while checking {found.kind} {found.routine_id}"
            ) from None
    schemas = {r.schema.lower() for r in routines if r.schema}
    for r in routines:
        if r.node is None:
            r.node = _textless_node(r, engine)
    nodes = [r.node for r in routines if r.node is not None]
    moved = MovedObjects.of(nodes, _collected_tables(collector, schemas))
    leaving = _leaving_aurora(collector, assignment, target.engine)
    by_key: dict[tuple[str, str, str], list[RoutineRef]] = {}
    for r in routines:
        by_key.setdefault(_node(r).key, []).append(_ref_of(r))
    check_rules = {rule.id: rule for rule in routine_check_rules()}
    for r in routines:
        result = check_dependencies(_node(r), moved, target_engine=target.engine)
        if result is None:
            continue
        if result.outcome == "pass":
            r.rules_passed += 1
            continue
        r.checks.append(
            RoutineCheckEntry(
                rule_id=result.rule_id, outcome=result.outcome, findings=list(result.findings)
            )
        )
        if result.outcome == "fail" and check_rules[result.rule_id].impact != "informational":
            r.failed.append(result.rule_id)
    order = dependency_order(nodes)
    cycle_of = {ref: i for i, members in enumerate(order.cycles) for ref in members}
    by_ref = {r.ref: r for r in routines}
    drafts = {
        ref: _draft(by_ref[ref], cycle_of.get(ref), moved, by_key, leaving) for ref in order.order
    }
    # A routine naming one that needs attention needs attention too, transitively: in
    # deploy order every routine it names (outside its own cycle) is already settled.
    for ref in order.order:
        draft = drafts[ref]
        blocked = {
            (dep_ref.kind, dep_ref.routine_id): dep_ref
            for dep in draft["dependencies"]
            for dep_ref in dep.routines
            if (other := f"{dep_ref.kind}:{dep_ref.routine_id}") != ref
            and drafts[other]["status"] == "needs_attention"
        }
        if blocked:
            draft["blocked_by"] = [blocked[k] for k in sorted(blocked)]
            draft["reasons"].append(_BLOCKED)
            draft["status"] = "needs_attention"
    # Each row is built once, from its draft, then the draft is dropped.
    rows = [
        RoutineMigration(**drafts.pop(ref), deploy_order=i + 1) for i, ref in enumerate(order.order)
    ]
    cycles = [[_ref_of(by_ref[ref]) for ref in members] for members in order.cycles]
    counts = {s: 0 for s in ("as_is", "rewritten", "model_repaired", "needs_attention")}
    for row in rows:
        counts[row.status] += 1
    return RoutineMigrationContract(
        job_id=job_id,
        database_name=database_name,
        assignment_version=assignment_version,
        source_engine=engine,  # type: ignore[arg-type]
        source_version=target.version,
        target_engine=target.engine,  # type: ignore[arg-type]
        target_line=target.line,
        target_user=target.user,
        routine_sources=read > 0,
        total=len(rows),
        counts=counts,  # type: ignore[arg-type]
        cycles=cycles,
        rules=_rules(rows),
        routines=rows,
    )


def _node(r: _Routine) -> RoutineNode:
    if r.node is None:
        raise ValueError(f"{r.kind} {r.routine_id} has no dependency node")
    return r.node


def _ref_of(r: _Routine) -> RoutineRef:
    return RoutineRef(kind=r.kind, routine_id=r.routine_id)  # type: ignore[arg-type]


def _draft(
    r: _Routine,
    cycle: int | None,
    moved: MovedObjects,
    by_key: Mapping[tuple[str, str, str], list[RoutineRef]],
    leaving: Mapping[tuple[str, str], TableLeavingAurora],
) -> dict[str, Any]:
    """The fields of the routine's row before the transitive rule and its deploy order
    are set (a plain dict: the row is validated once, when both are known)."""
    node = _node(r)
    dependencies: dict[tuple[str, str, str | None], RoutineDependencyEntry] = {}
    tables_leaving: dict[str, TableLeavingAurora] = {}
    for dep in node.dependencies:
        schema, name = node.resolve(dep)
        if dep.kind == "table":
            found = (schema, name) in moved.tables
            refs: list[RoutineRef] = []
            if (schema, name) in leaving:
                tables_leaving[leaving[(schema, name)].table] = leaving[(schema, name)]
        else:
            keys = node.resolve_routine(dep)
            refs = [ref for key in keys for ref in by_key.get(key, ())]
            found = bool(refs)
        # One entry per object: a trigger's function, named by the collection and by
        # the statement (with and without its schema), is one dependency.
        key = (dep.kind, _qualified(schema or None, name), dep.routine_kind)
        if key not in dependencies:
            dependencies[key] = RoutineDependencyEntry(
                kind=dep.kind,
                name=key[1],
                routine_kind=dep.routine_kind,
                found=found,
                routines=refs,
            )
        elif refs and not dependencies[key].routines:
            dependencies[key] = dependencies[key].model_copy(
                update={"routines": refs, "found": True}
            )
    reasons = sorted(set(r.failed))
    if r.source_text == "not_collected":
        reasons.append(_NOT_COLLECTED)
    elif r.source_text == "not_exact":
        reasons.append(_NOT_EXACT)
    if any(not d.found and d.kind == "table" for d in dependencies.values()):
        reasons.append(_TABLE_MISSING)
    if any(not d.found and d.kind == "routine" for d in dependencies.values()):
        reasons.append(_ROUTINE_MISSING)
    if cycle is not None:
        reasons.append(_CYCLE)
    return {
        "routine_id": r.routine_id,
        "kind": r.kind,
        "schema_name": r.schema,
        "name": r.name,
        "specific_name": r.specific,
        "table": r.table,
        "signature": r.signature,
        "returns": r.returns,
        "source_text": r.source_text,
        "body_status": _text(_value(r.row.get("body_status"))),
        "status": "needs_attention" if reasons else r.base_status,
        "reasons": reasons,
        "blocked_by": [],
        "cycle": cycle,
        "dependencies": list(dependencies.values()),
        "dependencies_complete": node.dependencies_complete,
        "tables_leaving_aurora": [tables_leaving[t] for t in sorted(tables_leaving)],
        "rewrites": r.rewrites,
        "notes": r.notes,
        "checks": sorted(r.checks, key=lambda c: c.rule_id),
        "rules_passed": r.rules_passed,
    }


def _rules(rows: Sequence[RoutineMigration]) -> list[RoutineRuleEntry]:
    named = {c.rule_id for row in rows for c in row.checks} | {
        w.rule_id for row in rows for w in row.rewrites
    }
    out = []
    for rule in routine_check_rules():
        if rule.id in named:
            out.append(
                RoutineRuleEntry(
                    rule_id=rule.id,
                    rule="check",
                    impact=rule.impact,
                    reason=rule.reason,
                    fix_hint=rule.fix_hint,
                )
            )
    for rewrite in routine_rewrite_rules():
        if rewrite.id in named:
            out.append(RoutineRuleEntry(rule_id=rewrite.id, rule="rewrite", reason=rewrite.reason))
    return sorted(out, key=lambda e: e.rule_id)


# --------------------------------------------------------------------------- the store


def write_routine_migration(
    store: Any, database_name: str, job_id: str, assignment_version: int
) -> tuple[str | None, RoutineMigrationContract | None]:
    """Run the phase for ``assignment_version`` and write its artifact; ``(key, artifact)``,
    or ``(None, None)`` when there is nothing to write (no routines, or an engine routines
    are not carried from yet). The routine sources are read one chunk at a time."""
    from src.agents.collector.routine_sources import read_routine_sources
    from src.storage.assignment_versioning import read_assignment

    collector = store.read_json(f"{database_name}/{job_id}/collector/output.json")
    assignment = read_assignment(store, database_name, job_id, assignment_version)
    built = run_routine_migration_deterministic(
        collector,
        assignment,
        read_routine_sources(store, database_name, job_id),
        job_id=job_id,
        database_name=database_name,
        assignment_version=assignment_version,
    )
    if built is None:
        return None, None
    artifact: RoutineMigrationContract = built
    key = routine_migration_path(database_name, job_id, assignment_version)
    store.write_json(key, artifact.model_dump(mode="json"))
    return key, artifact


def ensure_routine_migration(
    store: Any, database_name: str, job_id: str
) -> tuple[int, str | None, RoutineMigrationContract | None, bool]:
    """The artifact for the job's effective assignment version, written when that version
    has none yet: ``(version, key, artifact, written)``; ``key`` and ``artifact`` are None
    when there is nothing to write. A version that has its artifact is left as it is."""
    from src.storage.assignment_versioning import resolve_downstream_assignment_version

    version = resolve_downstream_assignment_version(store, database_name, job_id)
    key = routine_migration_path(database_name, job_id, version)
    if store.exists(key) is True:
        return version, key, None, False
    written, artifact = write_routine_migration(store, database_name, job_id, version)
    return version, written, artifact, artifact is not None
