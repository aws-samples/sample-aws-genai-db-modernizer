# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A routine's redacted ``CREATE``, from the collection, for the repair model (#1940).

The model never sees the exact text of the routine sources (ADR-034 Decisions 2 and 4):
it works on the collection's text, whose literals were redacted when it was read
(#1625). A PostgreSQL routine's collected ``definition`` is already its whole ``CREATE``
(``pg_get_functiondef``, ``pg_get_triggerdef``); a MySQL routine's is only its body, so
its header is written here from the collected fields, the way the offline script's
rebuild writes it (``src.agents.collector.mysql_routine_create``). A text that already
starts with ``CREATE`` (a DDL upload) is used as it is.

Pure: no I/O. None when the row has no text to repair from.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from src.agents.collector.mysql_routine_create import (
    definer_clause,
    quote_ident,
    routine_create,
    trigger_create,
)

_STARTS_WITH_CREATE = re.compile(r"^\s*CREATE\b", re.IGNORECASE)


def _value(item: object) -> str:
    return str(getattr(item, "value", item) or "")


def _mysql_procedure(row: Mapping[str, Any], kind: str, body: str) -> str | None:
    params = []
    for i, p in enumerate(row.get("parameters") or [], start=1):
        if not isinstance(p, Mapping):
            continue
        params.append(
            {
                "ordinal": i,
                "mode": _value(p.get("parameter_mode")) or "IN",
                "parameter_name": p.get("parameter_name") or f"p{i}",
                "dtd_identifier": p.get("data_type") or "",
            }
        )
    if kind == "function" and row.get("return_type"):
        params.append({"ordinal": 0, "dtd_identifier": row["return_type"]})
    security = row.get("security_definer")
    return routine_create(
        {
            "definition": body,
            "routine_type": kind.upper(),
            "routine_name": row.get("procedure_name"),
            "definer": row.get("owner"),
            "security_type": "INVOKER" if security is False else "DEFINER",
            "sql_mode": row.get("sql_mode"),
        },
        params,
    )


def _mysql_trigger(row: Mapping[str, Any], body: str) -> str | None:
    table = str(row.get("table_id") or "").rpartition(".")[2]
    return trigger_create(
        {
            "definition": body,
            "timing": _value(row.get("timing")),
            "event_type": _value(row.get("event_type")),
            "trigger_name": row.get("trigger_name"),
            "table_name": table,
            "definer": row.get("owner"),
        }
    )


def _mysql_event(row: Mapping[str, Any], body: str) -> str | None:
    schedule = str(row.get("schedule") or "").strip()
    name = row.get("event_name")
    if not schedule or not name:
        return None
    return (
        f"CREATE {definer_clause(row.get('owner'))}EVENT {quote_ident(name)} "
        f"ON SCHEDULE {schedule} DO {body}"
    )


def redacted_create(row: Mapping[str, Any], engine: str, kind: str) -> str | None:
    """The redacted ``CREATE`` of a collection routine row (module docstring), or None."""
    body = row.get("definition")
    if not isinstance(body, str) or not body.strip():
        return None
    if _STARTS_WITH_CREATE.match(body):
        return body
    if engine != "mysql":
        return None
    if kind in ("function", "procedure"):
        return _mysql_procedure(row, kind, body)
    if kind == "trigger":
        return _mysql_trigger(row, body)
    if kind == "event":
        return _mysql_event(row, body)
    return None
