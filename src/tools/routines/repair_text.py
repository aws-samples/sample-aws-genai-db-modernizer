# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A routine's redacted ``CREATE``, from the collection, for the repair model (#1940).

The model never sees the exact text of the routine sources (ADR-034 Decisions 2 and 4):
it works on the collection's text, whose literals were redacted when it was read
(#1625). A PostgreSQL routine's collected ``definition`` is already its whole ``CREATE``
(``pg_get_functiondef``, ``pg_get_triggerdef``); a MySQL routine's is only its body, so
its header is the one the routines file holds (#2444): the header of the collected
``CREATE`` there (everything before the body, parameters and characteristics included),
with its literals redacted the way the collection's are (``redact_definition``), so the
model sees the routine's real signature and no literal (:func:`mysql_headers`). With no
such header, it is written from the collected fields, the way the offline script's
rebuild writes it (``src.agents.collector.mysql_routine_create``). A text that already
starts with ``CREATE`` (a DDL upload) is used as it is. A function's or procedure's text
is never given to a model when its signature is not the routine-migration artifact's, or
the artifact read none (:func:`signature_mismatch`: a collector row that does not list
its parameters, say): a repair written on it would change the routine's signature. The
repair result records such a routine with ``attention`` ``signature_mismatch``.

Pure: no I/O (:func:`mysql_headers` reads the sources it is handed;
``repair_proof.routine_headers`` hands it the job's routines file). None when the row has
no text to repair from. Moved here from ``src.agents.routine_repair`` (#2444), so
step 1's rebuild (``repair_proof.seen_texts``) reads it without the repair seam.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any, Protocol

from src.agents.collector.mysql_routine_create import (
    definer_clause,
    quote_ident,
    routine_create,
    trigger_create,
)

if TYPE_CHECKING:
    from src.contracts.routine_migration import RoutineMigration, RoutineMigrationContract
    from src.shared.sql_spans import SpanSettings


class _Session(Protocol):
    @property
    def sql_mode(self) -> str | None: ...


class _Source(Protocol):
    """What :func:`mysql_headers` reads of a routines-file entry (``RoutineSource``)."""

    @property
    def kind(self) -> str: ...

    @property
    def create_statement(self) -> str: ...

    @property
    def session(self) -> _Session: ...

    def key(self) -> tuple[str, str, str]: ...


_STARTS_WITH_CREATE = re.compile(r"^\s*CREATE\b", re.IGNORECASE)
Ref = tuple[str, str]


def mysql_headers(
    migration: RoutineMigrationContract,
    sources: Iterable[_Source],
    settings: SpanSettings | None = None,
) -> dict[Ref, str]:
    """The redacted header of each MySQL function and procedure of ``migration`` whose
    ``CREATE`` the routines file holds, keyed ``(kind, routine_id)`` (module docstring).
    Only the headers are kept, each with its literals redacted (``redact_definition``,
    read with ``settings``, the collection's): never a body or a literal."""
    from src.tools.routines.migration import mysql_header_text
    from src.tools.sql.redact import redact_definition

    if migration.source_engine != "mysql":
        return {}
    wanted: dict[tuple[str, str, str], Ref] = {
        (r.kind, r.schema_name or "", r.specific_name or ""): (r.kind, r.routine_id)
        for r in migration.routines
        if r.kind in ("function", "procedure")
    }
    out: dict[Ref, str] = {}
    for source in sources:
        ref = wanted.get(source.key())
        if ref is None:
            continue
        head = mysql_header_text(source.create_statement, source.kind, source.session.sql_mode)
        if head is not None:
            out[ref] = redact_definition(head, "mysql", settings)
    return out


def _value(item: object) -> str:
    return str(getattr(item, "value", item) or "")


def _mysql_procedure(
    row: Mapping[str, Any], kind: str, body: str, header: str | None
) -> str | None:
    if header is not None:
        return f"{header}\n{body}"
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


def redacted_create(
    row: Mapping[str, Any], engine: str, kind: str, header: str | None = None
) -> str | None:
    """The redacted ``CREATE`` of a collection routine row (module docstring), or None.
    ``header`` is a MySQL function's or procedure's redacted header from the routines
    file (:func:`mysql_headers`)."""
    body = row.get("definition")
    if not isinstance(body, str) or not body.strip():
        return None
    if _STARTS_WITH_CREATE.match(body):
        return body
    if engine != "mysql":
        return None
    if kind in ("function", "procedure"):
        return _mysql_procedure(row, kind, body, header)
    if kind == "trigger":
        return _mysql_trigger(row, body)
    if kind == "event":
        return _mysql_event(row, body)
    return None


def signature_mismatch(text: str, row: RoutineMigration, engine: str, sql_mode: str | None) -> bool:
    """Whether ``text`` must not go to a model for the function or procedure ``row``: the
    routine-migration artifact read no signature for it, or ``text``'s arguments or return
    type are not the ones it read (module docstring). Always False for a trigger or event."""
    from src.tools.routines.migration import routine_signature

    if row.kind not in ("function", "procedure"):
        return False
    if row.signature is None:
        return True
    return routine_signature(text, engine, row.kind, sql_mode) != (row.signature, row.returns)
