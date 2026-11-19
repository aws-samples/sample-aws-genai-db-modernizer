# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The design view of a single-pass external schema design request (#2463).

DocumentDB, OpenSearch and ElastiCache are designed in one pass from one request file
(``llm_requests/schema_design_<engine>.json``). ``prepare_schema_design_input`` scopes
that request to the engine's queries and tables, but it still carries the collector's
records as collected: every routine of the database, every collector field of every
table, column and query, and null or empty fields throughout. Aurora gets a compact
design view instead (``aurora_common.design_view``, #273), and each DynamoDB group gets
its own (``group_input``, #272). This module gives the three single-pass engines theirs.

The view keeps the request's shape (``collector_output.database_schema``,
``collector_output.queries.query_patterns``, ``analysis_output``, ``design_metrics``,
``decision_trace``), so the commands and skills read the same keys, and it keeps
everything the designers read:

- every in-scope query with the fields of the schema design projection
  (``AgentQueryPattern``: its id, full text, type, rates, tables, filters, sort and the
  telemetry figures), and every query header field, ``query_pattern_groups`` included;
- every in-scope table with the projected table, column, index and foreign-key fields
  plus the native ``data_type`` (the ones a DynamoDB group input keeps);
- the triggers on those tables or whose body writes one, and the views those queries
  read or that are defined on those tables (``referenced_tables``);
- when the engine's output contract has ``migration_notes`` (DocumentDB and ElastiCache
  today; :func:`reads_routines`), the routines its design reads: its skill writes a
  migration note per procedure, function and trigger that needs application-layer
  replacement, and a cache's invalidation needs every write to a cached table. A routine
  is kept when its body names an in-scope table (read with the source dialect's parser,
  ``proof_screen.read_touched_tables``; it does not tell reads from writes, so every
  routine that writes one is kept), when its ``referenced_tables`` or ``depends_on`` name
  one, when an in-scope query runs in it (``parent_routine_ids``) or calls it by name
  (``CALL p(...)``, ``EXEC p``, ``SELECT f(...)``), when it is a kept trigger's function,
  or when a kept routine calls it. A routine whose body is missing, does not parse or runs dynamic SQL is
  kept too when the scope has a table: what cannot be read is never dropped. Only compiled code with no body (an
  extension's ``C`` or ``INTERNAL`` function) that nothing above links is left out.
  OpenSearch reads no routine: its contract has no migration notes;
- the analysis, ``cache_invalidation_context``, ``metadata`` and ``metrics`` with only
  their null and empty fields dropped. A ``[]`` the collector contract tells apart from
  null is kept (:data:`EMPTY_IS_A_VALUE`: ``parent_routine_ids: []`` says a statement ran
  in no routine, ``depends_on: []`` that the catalog records none);
- ``design_metrics``, ``decision_trace``, ``redesign_request``, ``output_schema`` and
  every other top-level field as they are.

Dropped: the routines the engine's design does not read, the database's scheduled
events, collector fields outside the projection (such as ``parsed_facts``, the
per-percentile timings and the I/O counters), and null or empty fields. No in-scope
query, table, column, rate or figure is dropped.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Any

from src.agents.referee.table_resolution import TableNameResolver
from src.agents.schema_design.group_input import (
    _QUERY_FIELDS,
    _TRIGGER_FIELDS,
    _VIEW_FIELDS,
    _empty,
    _group_views,
    _slim,
    _slim_table,
)
from src.tools.database.routine_links import routine_id
from src.tools.routines.proof_screen import called_names, dynamic_sql, read_touched_tables
from src.tools.sql.query_facts import source_engine_of

SINGLE_PASS_VIEW_ENGINES = frozenset({"documentdb", "opensearch", "elasticache"})
"""The engines whose external request is this design view."""

EMPTY_IS_A_VALUE = frozenset({"parent_routine_ids", "depends_on", "extensions", "enum_types"})
"""Fields whose ``[]`` the collector contract tells apart from null (``[]``: none; null:
not recorded), so :func:`drop_empty` keeps it."""

# The routine parser's dialect for each source engine it reads; any other source's
# bodies cannot be read here, so every bodied routine of it is kept.
_ROUTINE_DIALECTS = {"mysql": "mysql", "mariadb": "mysql", "postgresql": "postgresql"}
# Languages whose body is SQL the routine parser reads (None: MySQL, which records none).
_SQL_LANGUAGES = frozenset({"sql", "plpgsql"})
# Compiled code: no SQL body exists to read, so it names no table in text.
_COMPILED_LANGUAGES = frozenset({"c", "internal"})
# A routine an in-scope query runs by name (any dialect): CALL p, EXEC p, EXECUTE p.
_CALLED = re.compile(r"\b(?:CALL|EXEC|EXECUTE)\s+((?:[\w$#@]+|[`\"\[][^`\"\]]+[`\"\]])"
                     r"(?:\s*\.\s*(?:[\w$#@]+|[`\"\[][^`\"\]]+[`\"\]]))*)", re.I)  # fmt: skip
# The placeholder table of the CREATE TRIGGER a MySQL trigger body is wrapped in.
_TRIGGER_TABLE = "__trigger_table__"

# Database-schema sections the view rebuilds or leaves out; every other one is kept.
_REBUILT_SCHEMA_KEYS = ("tables", "triggers", "views", "procedures")
# Scheduled events are routines no single-pass design reads; triggers outside the DML
# list are carried as routines, never designed against (#2023).
_DROPPED_SCHEMA_KEYS = ("events", "non_dml_triggers")


def reads_routines(output_schema: dict | None) -> bool:
    """Whether a design with ``output_schema`` (its output contract's JSON Schema) reads
    routines: the contract has ``migration_notes``, one per routine the design replaces
    or must account for."""
    return "migration_notes" in ((output_schema or {}).get("properties") or {})


def drop_empty(value: Any) -> Any:
    """``value`` without null, empty-string, empty-list or empty-object dict fields,
    recursively, but a ``[]`` in :data:`EMPTY_IS_A_VALUE`. List items are never dropped,
    so positions and counts stay."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            kept = drop_empty(item)
            if not _empty(kept) or (key in EMPTY_IS_A_VALUE and kept == []):
                out[key] = kept
        return out
    if isinstance(value, list):
        return [drop_empty(v) for v in value]
    return value


def _slim_keeping(record: dict, fields: set[str], kind: str) -> dict:
    """``_slim`` that keeps a ``[]`` in :data:`EMPTY_IS_A_VALUE`."""
    slim = _slim(record, fields, kind)
    for key in EMPTY_IS_A_VALUE & fields:
        if record.get(key) == []:
            slim[key] = []
    return slim


def _routine_name(name: object) -> str:
    """A routine name folded for matching: no quotes or argument list, lower case, the
    last dotted part (a trigger's ``function_id`` may or may not name its schema)."""
    text = re.sub(r"\(.*\)\s*$", "", str(name or "")).strip()
    text = "".join(c for c in text if c not in '`"[]').lower()
    return text.rsplit(".", 1)[-1].strip()


def _routine_names(routine: dict) -> set[str]:
    return {_routine_name(routine.get(k)) for k in ("procedure_name", "procedure_id")} - {""}


def _create_text(record: dict, kind: str, dialect: str) -> str | None:
    """The record's ``definition`` as a CREATE the dialect's parser reads: PostgreSQL's is
    one already (``pg_get_functiondef``, ``pg_get_triggerdef``); a MySQL definition is the
    body only, so it is wrapped in a placeholder header (only the tables matter here)."""
    text = str(record.get("definition") or "").strip()
    if not text:
        return None
    if dialect != "mysql" or re.match(r"CREATE\b", text, re.I):
        return text
    if kind == "trigger":
        return f"CREATE TRIGGER `t` AFTER INSERT ON `{_TRIGGER_TABLE}` FOR EACH ROW {text}"
    if kind == "function":
        return f"CREATE FUNCTION `r`() RETURNS int {text}"
    return f"CREATE PROCEDURE `r`() {text}"


def _body_tables(record: dict, kind: str, dialect: str | None) -> tuple[str, ...] | None:
    """The tables the record's body names, or None when it has no body or the body cannot
    be read (an unknown dialect, a language that is not SQL, a body that does not parse, a
    body that runs dynamic SQL)."""
    language = str(record.get("language") or "").lower()
    if dialect is None or (language and language not in _SQL_LANGUAGES):
        return None
    text = _create_text(record, kind, dialect)
    if text is None:
        return None
    if dynamic_sql([text], dialect):  # it builds a statement at run time: no table is visible
        return None
    found = read_touched_tables(text, dialect, kind)
    return None if found is None else tuple(t for t in found if t != _TRIGGER_TABLE)


def _routine_kind(routine: dict) -> str:
    return "procedure" if str(routine.get("procedure_type")) == "PROCEDURE" else "function"


def _calls_in(text: str, dialect: str | None) -> set[str]:
    """The routine names ``text`` runs: ``CALL``/``EXEC`` targets, and with a known dialect
    every name called as a function."""
    names = {_routine_name(m.group(1)) for m in _CALLED.finditer(text or "")}
    if dialect is not None:
        names |= {_routine_name(n) for n in called_names(text or "", dialect)}
    return names - {""}


class _Scope:
    """The in-scope tables, and how a written name resolves to one."""

    def __init__(self, collector_output: dict):
        tables = (collector_output.get("database_schema") or {}).get("tables") or []
        self.ids = {str(t["table_id"]) for t in tables if t.get("table_id")}
        self.keys = self.ids | {str(t["table_name"]) for t in tables if t.get("table_name")}
        self._resolver = TableNameResolver.from_collector(collector_output)

    def owns(self, name: object) -> bool:
        text = str(name or "")
        if not text:
            return False
        if text in self.keys:
            return True
        resolved = self._resolver.resolve(text) if self._resolver is not None else None
        return resolved in self.ids

    def any(self, names: Iterable[object]) -> bool:
        return any(self.owns(n) for n in names)


def _catalog_tables(routine: dict) -> list[object]:
    return [*(routine.get("referenced_tables") or [])] + [
        d.get("name")
        for d in routine.get("depends_on") or []
        if isinstance(d, dict) and d.get("kind") in ("table", "view")
    ]


def _kept_triggers(
    schema: dict, scope: _Scope, dialect: str | None, routine_tables: Callable[[str], Any]
) -> list[dict]:
    """The triggers on an in-scope table or whose body names one (a PostgreSQL trigger's
    body is its function's), and, when the scope has a table, any whose body cannot be
    read."""
    kept = []
    for trigger in schema.get("triggers") or []:
        if scope.owns(trigger.get("table_id")):
            kept.append(trigger)
            continue
        own = _body_tables(trigger, "trigger", dialect)
        function = _routine_name(trigger.get("function_id"))
        if function:  # PostgreSQL: the trigger runs its function, whose body writes
            named = routine_tables(function)
            own = None if named is None else (*(own or ()), *named)
        if (own is None and scope.ids) or (own is not None and scope.any(own)):
            kept.append(trigger)
    return kept


def _routines_read(
    schema: dict,
    queries: list[dict],
    triggers: list[dict],
    scope: _Scope,
    dialect: str | None,
    body: Callable[[dict], tuple[str, ...] | None],
) -> list[dict]:
    """The routines a design with migration notes reads (see the module docstring)."""
    routines = schema.get("procedures") or []
    parents = {str(r) for q in queries for r in q.get("parent_routine_ids") or []}
    wanted = {_routine_name(t.get("function_id")) for t in triggers if t.get("function_id")}
    for query in queries:
        wanted |= _calls_in(str(query.get("query_text") or ""), dialect)
    kept: dict[int, dict] = {}
    for i, routine in enumerate(routines):
        ids = {
            str(routine.get("procedure_id") or ""),
            routine_id(routine.get("schema_name"), routine.get("procedure_name")) or "",
            str(routine.get("specific_name") or ""),
        } - {""}
        language = str(routine.get("language") or "").lower()
        tables = body(routine)
        # An empty scope has no table a routine could touch, read or not.
        unread = tables is None and language not in _COMPILED_LANGUAGES and bool(scope.ids)
        if (
            ids & parents
            or _routine_names(routine) & wanted
            or scope.any(_catalog_tables(routine))
            or (tables is not None and scope.any(tables))
            or unread
        ):
            kept[i] = routine
    # A routine a kept routine calls is part of the same logic: keep it as well.
    frontier = list(kept.values())
    while frontier:
        called: set[str] = set()
        for routine in frontier:
            text = _create_text(routine, _routine_kind(routine), dialect or "")
            called |= _calls_in(text or "", dialect)
        frontier = []
        for i, routine in enumerate(routines):
            if i not in kept and _routine_names(routine) & called:
                kept[i] = routine
                frontier.append(routine)
    return [drop_empty(routines[i]) for i in sorted(kept)]


def _views_kept(collector_output: dict, queries: list[dict], scope: _Scope) -> list[dict]:
    """The views the queries read and the views defined on an in-scope table."""
    read = {str(v["view_id"]) for v in _group_views(collector_output, queries)}
    return [
        _slim_keeping(v, _VIEW_FIELDS, "view")
        for v in (collector_output.get("database_schema") or {}).get("views") or []
        if str(v.get("view_id")) in read or scope.any(v.get("referenced_tables") or [])
    ]


def _schema_view(collector_output: dict, queries: list[dict], routines: bool) -> dict:
    schema = collector_output.get("database_schema") or {}
    tables = schema.get("tables") or []
    scope = _Scope(collector_output)
    dialect = _ROUTINE_DIALECTS.get(source_engine_of(collector_output))
    cache: dict[int, tuple[str, ...] | None] = {}

    def body(routine: dict) -> tuple[str, ...] | None:
        if id(routine) not in cache:
            cache[id(routine)] = _body_tables(routine, _routine_kind(routine), dialect)
        return cache[id(routine)]

    by_name: dict[str, list[dict]] = {}
    for routine in schema.get("procedures") or []:
        for name in _routine_names(routine):
            by_name.setdefault(name, []).append(routine)

    def routine_tables(name: str) -> tuple[str, ...] | None:
        """Every table the routines called ``name`` name; None when one cannot be read."""
        found: list[str] = []
        for routine in by_name.get(name) or []:
            named = body(routine)
            if named is None and not _catalog_tables(routine):
                return None
            found += [*(named or ()), *map(str, _catalog_tables(routine))]
        return tuple(found) if by_name.get(name) else None

    view: dict[str, Any] = {"tables": [_slim_table(t) for t in tables]}
    raw_triggers = _kept_triggers(schema, scope, dialect, routine_tables)
    if raw_triggers:
        view["triggers"] = [_slim_keeping(t, _TRIGGER_FIELDS, "trigger") for t in raw_triggers]
    views = _views_kept(collector_output, queries, scope)
    if views:
        view["views"] = views
    if routines:
        kept = _routines_read(schema, queries, raw_triggers, scope, dialect, body)
        if kept:
            view["procedures"] = kept
    for key, value in schema.items():
        if key in _REBUILT_SCHEMA_KEYS or key in _DROPPED_SCHEMA_KEYS:
            continue
        kept_value = drop_empty(value)
        if not _empty(kept_value) or (key in EMPTY_IS_A_VALUE and kept_value == []):
            view[key] = kept_value
    return view


def _collector_view(collector_output: dict, routines: bool) -> dict:
    source_queries = collector_output.get("queries") or {}
    queries = source_queries.get("query_patterns") or []
    # A design that reads routines keeps the link from a query to the routine it ran in.
    fields = _QUERY_FIELDS | {"parent_routine_ids"} if routines else _QUERY_FIELDS
    queries_view: dict[str, Any] = {
        "query_patterns": [_slim_keeping(q, fields, "query") for q in queries]
    }
    for key, value in source_queries.items():
        kept = drop_empty(value)
        if key != "query_patterns" and not _empty(kept):
            queries_view[key] = kept
    view: dict[str, Any] = {}
    for key, value in collector_output.items():
        if key == "database_schema":
            view[key] = _schema_view(collector_output, queries, routines)
        elif key == "queries":
            view[key] = queries_view
        else:
            kept = drop_empty(value)
            if not _empty(kept):
                view[key] = kept
    return view


def single_pass_design_view(llm_request: dict, output_schema: dict) -> dict:
    """The design view of ``llm_request`` (``prepare_schema_design_input``'s result for
    one of :data:`SINGLE_PASS_VIEW_ENGINES`), with ``output_schema`` (the engine's output
    contract, which says whether the design reads routines) as its ``output_schema``. A
    new dict; ``llm_request`` is not modified. Every other field but ``collector_output``
    and ``analysis_output`` is kept as is."""
    view = dict(llm_request)
    routines = reads_routines(output_schema)
    if isinstance(llm_request.get("collector_output"), dict):
        view["collector_output"] = _collector_view(llm_request["collector_output"], routines)
    if isinstance(llm_request.get("analysis_output"), dict):
        view["analysis_output"] = drop_empty(llm_request["analysis_output"])
    view["output_schema"] = output_schema
    return view
