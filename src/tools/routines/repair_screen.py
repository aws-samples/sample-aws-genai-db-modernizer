# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What a model's repaired routine must keep (#1940, ADR-034 Decision 2).

The routine repair seam (``src.agents.routine_repair.handler``) refuses a candidate
``CREATE`` that is not exactly one statement of the routine's kind, renames the routine
(its kind, schema, name, or a trigger's table), changes its signature (argument types
or return type), creates an object the routine does not, names a schema the routine does
not, or changes nothing (:func:`refusal_of`). Each rule compares the candidate with the
routine's own redacted text, so what the routine already does is never held against it.

Pure: no I/O, no model, and no reason holds routine text. Passing these rules accepts
nothing: the proof (#1944, #1945) decides.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.contracts.routine_repair import RefusalCode
from src.tools.routines.migration import routine_signature
from src.tools.routines.mysql_checks import MySqlRoutine, check_mysql_routine
from src.tools.routines.postgresql_checks import parse_routine
from src.tools.routines.proof_screen import create_count, created_identity, named_schemas

__all__ = ["RepairedRoutine", "refusal_of"]

_SYSTEM_SCHEMAS = {"postgresql": {"pg_catalog", "information_schema"}, "mysql": set()}


@dataclass(frozen=True)
class RepairedRoutine:
    """The routine a candidate repairs: its kind, schema, name and redacted text."""

    kind: str
    schema_name: str | None
    name: str
    create_text: str
    sql_mode: str | None = None


def _identity(text: str, c: RepairedRoutine, engine: str) -> tuple[Any, ...] | None:
    found = created_identity(text, engine)
    if found is None:
        return None
    kind, schema, name, on_schema, on_table = found
    own = c.schema_name.casefold() if engine == "postgresql" and c.schema_name else c.schema_name
    # An unqualified name lands in the routine's own schema.
    return (kind, schema or own, name, (on_schema or own) if on_table else None, on_table)


def _one_statement(text: str, c: RepairedRoutine, engine: str) -> bool:
    if engine == "postgresql":
        return c.kind in ("function", "procedure", "trigger") and (
            parse_routine(text, c.kind) is None  # type: ignore[arg-type]
        )
    try:
        verdict = check_mysql_routine(
            MySqlRoutine(
                routine_id=f"{c.schema_name}.{c.name}" if c.schema_name else c.name,
                kind=c.kind,  # type: ignore[arg-type]
                create_sql=text,
                sql_mode=c.sql_mode or "",
            ),
            target_engine="aurora_mysql",
            source_version=None,
        )
    except Exception:  # noqa: BLE001 -- a text the checker cannot read is refused
        return False
    return verdict.parse_failure is None


def refusal_of(c: RepairedRoutine, text: str, engine: str) -> tuple[RefusalCode, str] | None:
    """Why the candidate ``text`` for ``c`` is refused, as ``(code, words)``; None when it
    may wait for the proof. Each rule compares it with the routine's own redacted text, so
    what the routine already does (another schema it reads, a temporary table it creates)
    is never held against it."""
    if text.strip() == c.create_text.strip():
        return "unchanged", "the candidate is the routine's own text"
    if not _one_statement(text, c, engine):
        return "not_one_statement", f"the candidate is not exactly one {c.kind} statement"
    mine, theirs = _identity(c.create_text, c, engine), _identity(text, c, engine)
    if theirs is None or mine is None:
        return "not_one_statement", "the candidate's header does not read"
    if mine != theirs:
        return "renamed", "the candidate creates another name, kind or trigger table"
    if routine_signature(text, engine, c.kind, c.sql_mode) != routine_signature(
        c.create_text, engine, c.kind, c.sql_mode
    ):
        return "signature_changed", "the candidate changes the arguments or the return type"
    before, after = create_count(c.create_text, engine), create_count(text, engine)
    if before is None or after is None:
        return "not_one_statement", "the candidate's text does not read"
    if after > before:
        return "adds_objects", "the candidate creates an object the routine does not"
    named, own = named_schemas(text, engine, c.kind), named_schemas(c.create_text, engine, c.kind)
    if named is None or own is None:
        return "not_one_statement", "the candidate's text does not read"
    allowed = (
        own | _SYSTEM_SCHEMAS[engine] | ({c.schema_name.casefold()} if c.schema_name else set())
    )
    if named - allowed:
        return "names_other_schema", "the candidate names a schema the routine does not"
    return None
