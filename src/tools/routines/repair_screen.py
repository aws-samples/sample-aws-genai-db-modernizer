# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What a model's repaired routine must keep (#1940, ADR-034 Decision 2).

The routine repair seam (``src.agents.routine_repair.handler``) refuses a candidate
``CREATE`` that is not exactly one statement of the routine's kind, renames the routine
(its kind, schema, name, or a trigger's table), changes its signature (argument types
or return type), creates an object the routine does not, builds and runs a statement at
run time the routine does not (``EXECUTE``, ``PREPARE``, by count and kind, #1941), names
a schema the routine does not, or changes nothing (:func:`refusal_of`). Each rule compares the candidate with the
routine's own redacted text, so what the routine already does is never held against it.

Pure: no I/O, no model, and no reason holds routine text. Passing these rules accepts
nothing: the proof (#1944, #1945) decides.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from src.contracts.routine_repair import RefusalCode
from src.tools.routines.migration import routine_signature
from src.tools.routines.mysql_checks import MySqlRoutine, check_mysql_routine
from src.tools.routines.postgresql_checks import parse_routine
from src.tools.routines.proof_screen import (
    called_functions,
    create_refusal,
    created_identity,
    created_objects,
    dynamic_executions,
    literal_sequence,
    named_schemas,
    privilege_clauses,
    trigger_shape,
    written_statements,
)

__all__ = ["RepairedRoutine", "hinted_calls", "refusal_of"]

_HINTED_CALL = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\(")
_SYSTEM_SCHEMAS = {"postgresql": {"pg_catalog", "information_schema"}, "mysql": set()}


@dataclass(frozen=True)
class RepairedRoutine:
    """The routine a candidate repairs: its kind, schema, name and redacted text."""

    kind: str
    schema_name: str | None
    name: str
    create_text: str
    sql_mode: str | None = None
    #: The new calls a repair may make: the functions the failed rules' fix hints name
    #: (``pg_current_wal_lsn`` for a renamed WAL function), lower case, unqualified.
    allowed_calls: frozenset[str] = frozenset()


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


def hinted_calls(hints: Iterable[str]) -> frozenset[str]:
    """The function names the fix hints write as calls (``pg_current_wal_lsn()``), lower
    case: the only new calls a repair may make."""
    return frozenset(m.group(1).casefold() for h in hints for m in _HINTED_CALL.finditer(h))


def _new_calls(new: frozenset[str], allowed: frozenset[str], engine: str) -> bool:
    """Whether ``new`` holds a call the fix hints do not allow: an allowed name may be
    written unqualified or, on PostgreSQL, in ``pg_catalog``."""
    for name in new:
        schema, _, bare = name.rpartition(".")
        if bare not in allowed or schema not in (
            "",
            "pg_catalog" if engine == "postgresql" else "",
        ):
            return True
    return False


def _same(text: str) -> str:
    return " ".join(text.split())


def _over(candidate: Counter[Any], original: Counter[Any]) -> bool:
    return bool(candidate - original)


def refusal_of(c: RepairedRoutine, text: str, engine: str) -> tuple[RefusalCode, str] | None:
    """Why the candidate ``text`` for ``c`` is refused, as ``(code, words)``; None when it
    may wait for the proof (module docstring). The proof's own screen runs on it first
    (``create_refusal``, with the schemas the routine's tables already sit in); each repair
    rule then compares it with the routine's own redacted text, so what the routine
    already does (another schema's table it reads, a temporary table it creates) is never
    held against it."""
    if _same(text) == _same(c.create_text):
        return "unchanged", "the candidate is the routine's own text, give or take whitespace"
    if not _one_statement(text, c, engine):
        return "not_one_statement", f"the candidate is not exactly one {c.kind} statement"
    mine, theirs = _identity(c.create_text, c, engine), _identity(text, c, engine)
    if theirs is None or mine is None:
        return "not_one_statement", "the candidate's header does not read"
    if mine != theirs:
        return "renamed", "the candidate creates another name, kind or trigger table"
    own = named_schemas(c.create_text, engine, c.kind)
    if own is None:
        return "not_one_statement", "the routine's own text does not read"
    namespace = (c.schema_name or "").casefold() if engine == "postgresql" else c.schema_name or ""
    why = create_refusal(text, engine, c.kind, namespace, sorted(own - {namespace}))
    if why is not None:
        return "screen_refused", f"the proof's screen refuses it: {why}"
    if routine_signature(text, engine, c.kind, c.sql_mode) != routine_signature(
        c.create_text, engine, c.kind, c.sql_mode
    ) or (
        c.kind == "trigger" and trigger_shape(text, engine) != trigger_shape(c.create_text, engine)
    ):
        return "signature_changed", (
            "the candidate changes the arguments, the return type, or a trigger's timing, "
            "events, level, order or function"
        )
    if privilege_clauses(text, engine) != privilege_clauses(c.create_text, engine):
        return "changes_privileges", "the candidate changes who the routine runs as"
    created, created_before = created_objects(text, engine), created_objects(c.create_text, engine)
    if created is None or created_before is None:
        return "not_one_statement", "the candidate's text does not read"
    if _over(created, created_before):
        return "adds_objects", "the candidate creates an object the routine does not"
    writes, writes_before = written_statements(text, engine), written_statements(
        c.create_text, engine
    )
    if writes is None or writes_before is None:
        return "not_one_statement", "the candidate's text does not read"
    if _over(writes, writes_before):
        return "adds_statements", "the candidate writes or changes an object the routine does not"
    dynamic, dynamic_before = dynamic_executions(text, engine), dynamic_executions(
        c.create_text, engine
    )
    if dynamic is None or dynamic_before is None:
        return "not_one_statement", "the candidate's text does not read"
    if _over(dynamic, dynamic_before):
        return "adds_dynamic_sql", (
            "the candidate builds and runs a statement at run time (EXECUTE, PREPARE) the "
            "routine does not"
        )
    calls, calls_before = called_functions(text, engine), called_functions(c.create_text, engine)
    if calls is None or calls_before is None:
        return "not_one_statement", "the candidate's text does not read"
    if _new_calls(calls - calls_before, c.allowed_calls, engine):
        return "adds_calls", (
            "the candidate calls a function the routine does not, and the failed rules' fix "
            "hints do not name it"
        )
    literals = literal_sequence(text, engine)
    if literals is None or literals != literal_sequence(c.create_text, engine):
        return "literals_changed", (
            "the candidate's literals differ from the routine's redacted ones in count or "
            "quote form"
        )
    named = named_schemas(text, engine, c.kind)
    if named is None:
        return "not_one_statement", "the candidate's text does not read"
    allowed = (
        own | _SYSTEM_SCHEMAS[engine] | ({c.schema_name.casefold()} if c.schema_name else set())
    )
    if named - allowed:
        return "names_other_schema", "the candidate names a schema the routine does not"
    return None
