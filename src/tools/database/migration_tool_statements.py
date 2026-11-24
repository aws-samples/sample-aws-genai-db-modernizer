# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The statements AWS DMS runs on the source, left out of the workload (#2375), and
the split that also leaves out SQL Server's statements on its own catalogs (#1894, the
rules are in :mod:`src.tools.database.system_catalog_statements`).

A collection taken while a DMS task reads the source records DMS's own statements next
to the application's. Routing must score only the application, so the collector leaves
DMS's statements out and records each one it left out (its id, the rule, the reason and
its executions) on ``queries.excluded_statements``; triage, the ``collect`` status line,
the approval gate and the deliverables report them.

The rules match what DMS itself writes on a source, never a statement's shape alone:

``dms_object``
    An ``INSERT`` or ``UPDATE`` whose every table is one DMS creates on a PostgreSQL
    source: the heartbeat table ``awsdms_heartbeat`` or the DDL capture table
    ``awsdms_ddl_audit``. A statement that also names another table is kept, and so is a
    read of either table (an application's lag monitor reads the heartbeat). DMS's
    control tables live on the target, so a source table of such a name is the
    application's and is kept.
``dms_replication_slot``
    PostgreSQL: a call to a logical decoding function that reads a replication slot's
    changes (``pg_logical_slot_get_changes``, ``..._peek_changes`` and their
    ``_binary_`` forms) that names no table, run by a role that also updates
    ``awsdms_heartbeat``. A slot read by any other role (an application's own change
    poller) is kept, and a slot read never marks a role as DMS's.
``dms_cursor``
    PostgreSQL: a psqlODBC cursor (``"SQL_CUR0x<address>"``, the driver DMS reads the
    source with) declared by the heartbeat's role, when its query has DMS's full-load
    shape (a plain column list from one schema-qualified table, nothing after it) or
    reads only the system catalogs (the ``pg_catalog`` and ``information_schema``
    schemas). The ``FETCH``, ``MOVE`` and ``CLOSE`` of such a cursor go with
    it, and so does any cursor use of a role that ran nothing but DMS's statements (its
    ``DECLARE`` can fall below the collection's floor). When the application logs in as
    the same role, its own cursors of another shape stay.

A role that updates (or inserts into) ``awsdms_heartbeat`` is the only evidence a role is
DMS's; a read of the heartbeat never marks one. The DDL capture table never
marks a role: its rows come from an event trigger that runs as the role that issued the
DDL. Both rules that need the role read ``role_id`` (the statement's role id, which
``scripts/collect-postgresql.sql`` and the live collector record; never a role name); a
collection without it keeps every slot read and cursor statement.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from typing import Any

from src.shared.excluded_statements import (
    DMS_CURSOR,
    DMS_OBJECT,
    DMS_REPLICATION_SLOT,
    ORIGINS,
    SQLSERVER_CATALOG,
    SQLSERVER_SYSTEM_PROCEDURE,
)
from src.tools.database.system_catalog_statements import catalog_statement_match

__all__ = [
    "DMS_CURSOR",
    "DMS_OBJECT",
    "DMS_REPLICATION_SLOT",
    "SQLSERVER_CATALOG",
    "SQLSERVER_SYSTEM_PROCEDURE",
    "split_migration_tool_statements",
]

HEARTBEAT_TABLE = "awsdms_heartbeat"
DDL_AUDIT_TABLE = "awsdms_ddl_audit"
# The tables DMS creates on a PostgreSQL source (the DMS guide: the heartbeat and the
# DDL capture). Its control tables are created on the target.
SOURCE_TABLES = frozenset({HEARTBEAT_TABLE, DDL_AUDIT_TABLE})
_SLOT_FUNCTIONS = frozenset(
    {
        "PG_LOGICAL_SLOT_GET_CHANGES",
        "PG_LOGICAL_SLOT_PEEK_CHANGES",
        "PG_LOGICAL_SLOT_GET_BINARY_CHANGES",
        "PG_LOGICAL_SLOT_PEEK_BINARY_CHANGES",
    }
)
# psqlODBC names a cursor "SQL_CUR" followed by the statement's address (%p).
_CURSOR = r'"(?P<name>SQL_CUR0x[0-9A-Fa-f]+)"'
_DECLARE_RE = re.compile(
    rf"\A\s*DECLARE\s+{_CURSOR}\s+(?:\w+\s+)*?CURSOR\b(?:\s+\w+)*?\s+FOR\s+(?P<body>.+)\Z",
    re.IGNORECASE | re.DOTALL,
)
_USE_RES = (
    re.compile(rf"\A\s*(?:FETCH|MOVE)\b[^\"']*?\b(?:IN|FROM)\s+{_CURSOR}\s*;?\s*\Z", re.I),
    re.compile(rf"\A\s*(?:FETCH|MOVE)\s+{_CURSOR}\s*;?\s*\Z", re.I),
    re.compile(rf"\A\s*CLOSE\s+{_CURSOR}\s*;?\s*\Z", re.I),
)
# DMS's full-load read, the one cursor query shape it declares for a table: a SELECT of a
# plain column list (each a name, quoted or not, optionally qualified by the table) FROM
# one schema-qualified table, and nothing after the table name. Anything else (a filter,
# a join, ORDER BY, OFFSET, FETCH FIRST, FOR UPDATE, DISTINCT, a function, TABLESAMPLE,
# LATERAL, a comma join, *) is not this shape.
_IDENT = r'(?:"[^"]+"|[A-Za-z_][A-Za-z0-9_$]*)'
_COLUMN = rf"{_IDENT}(?:\s*\.\s*{_IDENT})?"
_FULL_LOAD_RE = re.compile(
    rf"\A\s*SELECT\s+{_COLUMN}(?:\s*,\s*{_COLUMN})*\s+FROM\s+{_IDENT}\s*\.\s*{_IDENT}\s*;?\s*\Z",
    re.IGNORECASE,
)
_CATALOG_SCHEMAS = frozenset({"information_schema", "pg_catalog"})
# The system catalogs a query can name without their schema (pg_catalog is always first on
# the search path); any other unqualified pg_ name may be the application's own table.
_CATALOG_RELATIONS = frozenset(
    {
        "pg_attribute", "pg_attrdef", "pg_class", "pg_constraint", "pg_database",
        "pg_depend", "pg_description", "pg_enum", "pg_extension", "pg_index", "pg_indexes",
        "pg_inherits", "pg_namespace", "pg_proc", "pg_publication", "pg_publication_tables",
        "pg_replication_slots", "pg_roles", "pg_sequence", "pg_settings", "pg_stat_replication",
        "pg_tables", "pg_trigger", "pg_type", "pg_user", "pg_views",
    }
)  # fmt: skip

REASONS = {
    DMS_OBJECT: "writes only the table AWS DMS creates on the source ({objects})",
    DMS_REPLICATION_SLOT: (
        "reads a logical replication slot's changes ({objects}), run by the role that "
        "updates AWS DMS's heartbeat table"
    ),
    DMS_CURSOR: (
        "a psqlODBC cursor statement with AWS DMS's full-load shape, run by the role that "
        "updates AWS DMS's heartbeat table"
    ),
    # #1894: SQL Server's statements on its own catalogs (system_catalog_statements).
    SQLSERVER_CATALOG: "reads only SQL Server's system catalog views and DMVs ({objects})",
    SQLSERVER_SYSTEM_PROCEDURE: "runs only SQL Server's system catalog procedure {objects}",
}
# Appended when the application runs the same statement too (pg_stat_statements keys an
# entry on its role, so both rows share the statement's id).
SHARED_ID_SUFFIX = ":dms"
# The suffix per origin (``excluded_statements.ORIGINS``).
_SHARED_SUFFIXES = {"dms": SHARED_ID_SUFFIX, "catalog": ":catalog"}
SHARED_NOTE = (
    "; the application's own runs of the same statement stay in the workload under id {id}"
)


def _text(row: Mapping[str, Any]) -> str:
    return str(row.get("query_text") or row.get("query") or "")


def statement_id(row: Mapping[str, Any]) -> str:
    """The id the statement would have had: its ``digest`` (the offline rows), its
    ``query_id`` (the live ones), else the SHA-256 of its text, as the offline merge
    computes it."""
    for key in ("digest", "query_id"):
        if row.get(key):
            return str(row[key])
    return hashlib.sha256(_text(row).encode()).hexdigest()[:16]


def _role(row: Mapping[str, Any]) -> str:
    return str(row.get("role_id") or "")


def _object_match(text: str, source_engine: str) -> tuple[str, ...]:
    """DMS's source tables an INSERT or UPDATE writes, when it names no other table; else
    (). A read of the heartbeat (an application's lag monitor) is not DMS's."""
    from src.tools.sql import facade
    from src.tools.sql.query_type import statement_query_type

    if "awsdms_" not in text.lower():  # both tables' names hold it: no parse
        return ()
    if str(statement_query_type(text, source_engine)) not in ("INSERT", "UPDATE"):
        return ()
    refs = facade.table_references(text, source_engine).tables
    if not refs or not all((r.name or "").lower() in SOURCE_TABLES for r in refs):
        return ()
    return tuple(sorted({r.name.lower() for r in refs}))


def _slot_match(text: str, source_engine: str) -> tuple[str, ...]:
    from src.tools.sql import facade

    if "pg_logical_slot_" not in text.lower():
        return ()
    tokens = facade.sql_tokens(text, source_engine)
    calls = tokens.unqualified_calls | {
        c.split(".", 1)[1] for c in tokens.calls if c.startswith("PG_CATALOG.")
    }
    called = calls & _SLOT_FUNCTIONS
    if not called or facade.table_references(text, source_engine).tables:
        return ()
    return tuple(sorted(c.lower() for c in called))


def _cursor(text: str) -> tuple[str, str | None] | None:
    """A psqlODBC cursor statement's cursor name and, for a DECLARE, its query."""
    m = _DECLARE_RE.match(text)
    if m:
        return m.group("name"), m.group("body")
    for use in _USE_RES:
        m = use.match(text)
        if m:
            return m.group("name"), None
    return None


def _is_catalog(schema: str | None, name: str | None) -> bool:
    if schema:
        return schema.lower() in _CATALOG_SCHEMAS
    return (name or "").lower() in _CATALOG_RELATIONS


def _full_load_shape(body: str) -> bool:
    """DMS's full-load read (:data:`_FULL_LOAD_RE`), or a read of the system catalogs
    only (DMS reads the table metadata through them)."""
    from src.tools.sql import facade

    if _FULL_LOAD_RE.match(body):
        return True
    tables = facade.table_references(body, "postgresql").tables
    return bool(tables) and all(_is_catalog(t.schema, t.name) for t in tables)


def _is_postgres(source_engine: str) -> bool:
    return (source_engine or "").strip().lower() in ("postgresql", "postgres")


def split_migration_tool_statements(
    rows: Iterable[Any], source_engine: str, database_name: str | None = None
) -> tuple[list[Any], list[dict[str, Any]]]:
    """The rows to keep, and one record per statement left out: ``query_id``,
    ``rule``, ``reason`` and ``execution_count`` (summed over rows sharing an id).

    ``rows`` are collection statement rows (``query_text`` or ``query``, ``digest`` or
    ``query_id``, ``execution_count``, and on PostgreSQL ``role_id``). A row that is not
    a mapping is kept. With nothing to leave out, the rows come back unchanged. A left
    out statement whose id a kept row also has is recorded under the id with
    :data:`SHARED_ID_SUFFIX` (``:catalog`` for SQL Server's own), its reason naming the
    kept id.

    On a SQL Server source the statements SQL Server ran on its own catalogs go too
    (#1894, :mod:`src.tools.database.system_catalog_statements`); ``database_name``, the
    collected database, keeps a database named like a system schema its own.
    """
    rows = list(rows)
    postgres = _is_postgres(source_engine)
    matched: dict[int, tuple[str, tuple[str, ...]]] = {}
    slots: dict[int, tuple[str, ...]] = {}
    dms_roles: set[str] = set()
    for i, row in enumerate(rows):
        if not isinstance(row, Mapping):
            continue
        text = _text(row)
        if not text:
            continue
        objects = _object_match(text, source_engine)
        if objects:
            matched[i] = (DMS_OBJECT, objects)
            if HEARTBEAT_TABLE in objects and _role(row):
                dms_roles.add(_role(row))
        elif catalog := catalog_statement_match(row, text, source_engine, database_name):
            matched[i] = catalog
        elif postgres:
            functions = _slot_match(text, source_engine)
            if functions:
                slots[i] = functions
    if postgres and dms_roles:
        for i, functions in slots.items():
            if _role(rows[i]) in dms_roles:
                matched[i] = (DMS_REPLICATION_SLOT, functions)
        _match_cursors(rows, dms_roles, matched)
    if not matched:
        return rows, []
    kept = [row for i, row in enumerate(rows) if i not in matched]
    kept_ids = {statement_id(r) for r in kept if isinstance(r, Mapping)}
    records: dict[tuple[str, str], dict[str, Any]] = {}
    for i in sorted(matched):
        rule, objects = matched[i]
        row = rows[i]
        sid = statement_id(row)
        key = (sid, rule)
        record = records.get(key)
        calls = _count(row.get("execution_count"))
        if record is None:
            reason = REASONS[rule].format(objects=", ".join(objects))
            shared = sid in kept_ids
            suffix = _SHARED_SUFFIXES[ORIGINS[rule]]
            records[key] = {
                "query_id": sid + suffix if shared else sid,
                "rule": rule,
                "reason": reason + SHARED_NOTE.format(id=sid) if shared else reason,
                "execution_count": calls,
            }
        elif calls is not None:
            record["execution_count"] = (record["execution_count"] or 0) + calls
    return kept, list(records.values())


def _match_cursors(
    rows: list[Any], dms_roles: set[str], matched: dict[int, tuple[str, tuple[str, ...]]]
) -> None:
    """Mark the DMS roles' cursor statements DMS's (see the module docstring)."""
    cursors: dict[int, tuple[str, str | None]] = {}
    only_dms = dict.fromkeys(dms_roles, True)
    for i, row in enumerate(rows):
        role = _role(row) if isinstance(row, Mapping) else ""
        if role not in dms_roles:
            continue
        cursor = _cursor(_text(row))
        if cursor is not None:
            cursors[i] = cursor
        elif i not in matched:
            only_dms[role] = False
    dms_cursors: set[tuple[str, str]] = set()
    for i, (name, body) in cursors.items():
        if body is None:
            continue
        if _full_load_shape(body):
            dms_cursors.add((_role(rows[i]), name))
        else:  # a cursor DMS does not declare: the role is shared with the application
            only_dms[_role(rows[i])] = False
    for i, (name, _body) in cursors.items():
        role = _role(rows[i])
        if (role, name) in dms_cursors or only_dms[role]:
            matched[i] = (DMS_CURSOR, ())


def _count(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, float | str):
        try:
            return max(int(float(value)), 0)
        except ValueError:
            return None
    return None


def merge_excluded(*groups: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The records of ``groups`` in order, one per statement id and rule: a statement
    both the collection and Performance Insights report is recorded once."""
    seen: set[tuple[str, str]] = set()
    merged: list[dict[str, Any]] = []
    for group in groups:
        for record in group:
            key = (str(record.get("query_id")), str(record.get("rule")))
            if key not in seen:
                seen.add(key)
                merged.append(dict(record))
    return merged


def record_excluded(queries: Any, excluded: Iterable[Mapping[str, Any]]) -> None:
    """Set ``queries.excluded_statements`` (a collector ``Queries``) to ``excluded``;
    left None when there are none, so an output with nothing left out is unchanged."""
    from src.contracts.collector_output import ExcludedStatement

    records = [ExcludedStatement.model_validate(dict(r)) for r in excluded]
    if records:
        queries.excluded_statements = records
