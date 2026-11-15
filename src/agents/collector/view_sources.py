# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The CREATE statement of each collected view, which step 1 creates again (#2082).

A view holds no rows, so DMS never moves it: step 1 creates it on Aurora from the
statement kept here, in the job's routine sources (kind ``view``), as a routine's is.
The statement is built from the pieces the collection reads (the offline scripts' and
the live collectors' view rows carry the same fields):

- PostgreSQL: ``CREATE OR REPLACE VIEW "schema"."name" ("col", ...) [WITH
  (security_barrier = true, security_invoker = true)] AS <pg_get_viewdef text> [WITH
  LOCAL | CASCADED CHECK OPTION]``. ``pg_get_viewdef`` leaves a name visible on the
  collection's search path unqualified, so that path is kept with it (its session's
  ``search_path``) and the deploy script sets it before the statement.
- MySQL: ``CREATE OR REPLACE SQL SECURITY DEFINER | INVOKER VIEW `db`.`name` (`col`,
  ...) AS <VIEW_DEFINITION> [WITH LOCAL | CASCADED CHECK OPTION]``, the server's own
  stored text after ``AS``, with its ``character_set_client`` and
  ``collation_connection``. The ``DEFINER`` is left out, so the view's definer is the
  user who runs the deploy script (the routines deploy script names that user too).
  ``information_schema`` does not give a view's ``ALGORITHM``: the statement leaves it
  out, so the server's default (``UNDEFINED``) applies.

The column list is always written, from the view's columns in order: MySQL's
``VIEW_DEFINITION`` loses a view's own column names. A view an extension owns is never
built (``CREATE EXTENSION`` creates it). Names are quoted with the quote doubled; a row
whose pieces are missing or not of their shape gives no statement. MariaDB's views are
not built: step 1 carries views within one family only. Pure.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

__all__ = [
    "VIEW_ENGINES",
    "view_body_status",
    "view_columns",
    "view_create",
]

#: The engines whose views are carried (same family only: no MariaDB).
VIEW_ENGINES = ("postgresql", "mysql")
_CHECK_OPTIONS = {"NONE": "", "LOCAL": "LOCAL", "CASCADED": "CASCADED"}
_SECURITY = ("DEFINER", "INVOKER")
_MAX_COLUMNS = 4096


def _flag(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("t", "true", "1", "yes", "on")


def _name(value: object) -> str | None:
    """A name as collected: a non-empty string with no NUL (no engine allows one)."""
    return value if isinstance(value, str) and value and "\x00" not in value else None


def view_columns(row: Mapping[str, Any]) -> list[tuple[str, str | None]] | None:
    """The view's columns in order, ``(name, type)``; None when the row has none, or one
    is not a named column (a JSON text, as a MySQL live read may return it, is read)."""
    raw = row.get("columns")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(raw, list) or not raw or len(raw) > _MAX_COLUMNS:
        return None
    out: list[tuple[int, str, str | None]] = []
    for i, col in enumerate(raw):
        if not isinstance(col, Mapping):
            return None
        name = _name(col.get("name"))
        if name is None:
            return None
        kind = col.get("data_type")
        position = col.get("position")
        try:
            place = int(position) if position is not None and not isinstance(position, bool) else i
        except (TypeError, ValueError):
            return None
        out.append((place, name, str(kind) if isinstance(kind, str) and kind else None))
    out.sort(key=lambda c: c[0])
    if len({c[0] for c in out}) != len(out):
        return None
    return [(name, kind) for _, name, kind in out]


def _pg_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _mysql_ident(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def _check_clause(row: Mapping[str, Any]) -> str | None:
    option = str(row.get("check_option") or "NONE").strip().upper()
    if option not in _CHECK_OPTIONS:
        return None
    return f"\nWITH {_CHECK_OPTIONS[option]} CHECK OPTION" if _CHECK_OPTIONS[option] else ""


def view_create(engine: str, row: Mapping[str, Any]) -> str | None:
    """The view's ``CREATE OR REPLACE VIEW`` statement (module docstring), or None when it
    cannot be built from the row: another engine, an extension's view, no definition, no
    schema or name, no columns, or a check option or security type not of its shape."""
    if engine not in VIEW_ENGINES or _flag(row.get("extension_owned")):
        return None
    schema, name = _name(row.get("schema_name")), _name(row.get("view_name"))
    definition = row.get("definition")
    columns = view_columns(row)
    check = _check_clause(row)
    if not schema or not name or not isinstance(definition, str) or not columns or check is None:
        return None
    body = definition.strip()
    if engine == "postgresql":
        body = body.rstrip(";").rstrip()  # pg_get_viewdef ends its text with ';'
    if not body:
        return None
    if engine == "postgresql":
        options = [o for o in ("security_barrier", "security_invoker") if _flag(row.get(o))]
        with_ = f" WITH ({', '.join(f'{o} = true' for o in options)})" if options else ""
        cols = ", ".join(_pg_ident(c) for c, _ in columns)
        head = f"CREATE OR REPLACE VIEW {_pg_ident(schema)}.{_pg_ident(name)} ({cols}){with_} AS"
        return f"{head}\n{body}{check};"
    security = str(row.get("security_type") or "DEFINER").strip().upper()
    if security not in _SECURITY:
        return None
    cols = ", ".join(_mysql_ident(c) for c, _ in columns)
    head = (
        f"CREATE OR REPLACE SQL SECURITY {security} VIEW "
        f"{_mysql_ident(schema)}.{_mysql_ident(name)} ({cols}) AS"
    )
    return f"{head}\n{body}{check};"


def view_body_status(engine: str, row: Mapping[str, Any], *, include_bodies: bool) -> str | None:
    """The view row's ``body_status``: ``opted_out`` under ``include_routine_bodies = 0``,
    ``collected`` when its CREATE is built, ``not_readable`` when the collection user could
    not read its definition, ``not_supported`` otherwise; None for an extension's view, a
    row of another engine, or a row from a collection that predates #2082 (no columns
    read), which records nothing."""
    if engine not in VIEW_ENGINES or _flag(row.get("extension_owned")) or "columns" not in row:
        return None
    if not include_bodies:
        return "opted_out"
    if view_create(engine, row) is not None:
        return "collected"
    definition = row.get("definition")
    return (
        "not_readable"
        if not isinstance(definition, str) or not definition.strip()
        else ("not_supported")
    )


def view_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    """The collector output's #2082 fields of a view row (``View``): its schema, columns,
    body status and whether an extension owns it, each only when the row reports it."""
    out: dict[str, Any] = {}
    schema = _name(row.get("schema_name"))
    if schema:
        out["schema_name"] = schema
    columns = view_columns(row)
    if columns:
        out["columns"] = [{"name": n, "data_type": t} for n, t in columns]
    if row.get("body_status") in ("collected", "not_readable", "opted_out", "not_supported"):
        out["body_status"] = row["body_status"]
    if "extension_owned" in row:
        out["extension_owned"] = _flag(row.get("extension_owned"))
    return out
