# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What a routine's text has to pass before it is compared side by side (#1944).

The comparison (:mod:`src.tools.routines.comparison`) fails closed: a text this module
cannot read, or that could reach outside what the comparison controls, is refused, and
the routine is *could not compare* with the reason. Nothing here runs SQL, and no reason
holds routine text.

:func:`create_refusal` (a ``ProofTarget`` asks it before every ``CREATE``) refuses:

- a text that is not exactly one statement of its kind: on PostgreSQL the server's own
  parser (``parse_routine``), on MySQL the body-structure check (``check_mysql_routine``);
- a header outside the expected form: ``CREATE [OR REPLACE]``, on MySQL an optional
  ``DEFINER = user@host`` or ``DEFINER = CURRENT_USER[()]``, on PostgreSQL an optional
  ``CONSTRAINT`` before ``TRIGGER``, then the kind (``FUNCTION``, ``PROCEDURE``,
  ``TRIGGER``, ``EVENT``) and the name;
- a created name, or a trigger's ``ON`` table, qualified with any schema but the
  target's (on MySQL compared exactly, since a server that keeps case tells ``SRC`` from
  ``src``; on PostgreSQL an unquoted name folds to lower case);
- a text that sets ``search_path`` (a function's ``SET`` clause or a statement in its
  body) or calls ``set_config``;
- a body whose tables or calls, as the parser reads them, name another schema
  (``pg_catalog`` and ``information_schema`` aside);
- a name written so that the text and the server read it differently: a PostgreSQL
  ``U&"..."`` Unicode-escaped name, a MySQL ``/*! ... */`` versioned comment; and on
  PostgreSQL every name the statement binds is read again from the parser's tree, which
  decodes and folds names as the server does;
- a text that changes the role it runs as (``SET [LOCAL|SESSION] ROLE``, ``SET SESSION
  AUTHORIZATION``).

What it does not refuse, as known limits: ``SECURITY DEFINER`` (on a disposable engine
every routine runs as one role), and anything inside a statement built at run time,
which :func:`dynamic_sql` reports instead so the comparison does not compare the routine.

:func:`touched_tables` lists the tables a text names, from the same reads (MySQL: the
checker's dependencies; PostgreSQL: each statement of the body through the SQL facade).
A dynamic statement is never read, so the comparison also treats an "undefined table"
error at run time as a table it could not observe.

:func:`nondeterministic_calls` names the functions a text calls whose value the
comparison cannot freeze the same way on both sides (``clock_timestamp``,
``gen_random_uuid``, MySQL ``SYSDATE`` and ``UUID``, ...; ``random`` and ``RAND`` only
when the two sides cannot seed them alike). Calls are read from the tokens, the body's
included. :func:`called_names` lists every name a text calls, so the comparison also
reads a routine of the batch that is called without being listed as a dependency.
:func:`dynamic_sql` reports a statement built at run time (``EXECUTE`` or ``PREPARE ...
FROM`` of anything but one string literal), and :func:`named_time_zones` a MySQL text
that names a time zone. None of these sees inside a dynamic statement.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence

from src.tools.routines.mysql_checks import MySqlRoutine, check_mysql_routine
from src.tools.routines.postgresql_checks import (
    created_schemas,
    parse_routine,
    trigger_references,
)
from src.tools.sql.facade import (
    RoutineToken,
    RoutineTokenError,
    routine_statement_refs,
    routine_tokens,
)

__all__ = [
    "NONDETERMINISTIC",
    "SEEDED_RANDOM",
    "called_names",
    "create_refusal",
    "dynamic_sql",
    "named_time_zones",
    "nondeterministic_calls",
    "touched_tables",
]

#: Functions whose value no setting freezes the same way on both sides.
NONDETERMINISTIC = {
    "postgresql": frozenset({
        "clock_timestamp", "statement_timestamp", "timeofday", "gen_random_uuid",
        "uuid_generate_v1", "uuid_generate_v1mc", "uuid_generate_v4", "uuidv4", "uuidv7",
        "random_normal", "pg_backend_pid", "txid_current", "pg_current_xact_id",
        "txid_current_if_assigned", "pg_current_xact_id_if_assigned", "txid_current_snapshot",
        "pg_current_snapshot", "pg_postmaster_start_time", "pg_conf_load_time",
        "inet_client_addr", "inet_client_port",
    }),
    "mysql": frozenset({
        "sysdate", "uuid", "uuid_short", "connection_id",
    }),
}  # fmt: skip
#: The random functions each engine seeds per call (PostgreSQL ``setseed``, MySQL
#: ``rand_seed1``/``rand_seed2``): the same draws only on the same major, seed accepted.
SEEDED_RANDOM = {"postgresql": frozenset({"random"}), "mysql": frozenset({"rand"})}
_KINDS = {"FUNCTION", "PROCEDURE", "TRIGGER", "EVENT"}
_SYSTEM_SCHEMAS = {"postgresql": {"pg_catalog", "information_schema"}, "mysql": set()}
_STATEMENT_START = {"SELECT", "INSERT", "UPDATE", "DELETE", "WITH", "MERGE", "PERFORM"}
_DEPTH = 3
_UNICODE_NAME = re.compile(r"[Uu]&[\"']")


def _tokens_deep(text: str, engine: str, depth: int = 0) -> Iterator[RoutineToken]:
    """The tokens of ``text`` and of every string in it (a body), down to three levels.
    Raises ``RoutineTokenError`` when the outer text does not tokenize."""
    for tok in routine_tokens(text, engine):
        yield tok
        if depth < _DEPTH and (tok.command_rest or tok.kind.endswith("STRING")):
            try:
                yield from _tokens_deep(tok.text, engine, depth + 1)
            except RoutineTokenError:
                continue  # a string that is not SQL holds no call


def _levels(text: str, engine: str, depth: int = 0) -> Iterator[list[RoutineToken]]:
    """The tokens of ``text``, then those of every string in it, each level on its own:
    the tokens that follow a token in one list are the ones that follow it in its text."""
    tokens = list(routine_tokens(text, engine))
    yield tokens
    if depth < _DEPTH:
        for tok in tokens:
            if tok.command_rest or tok.kind.endswith("STRING"):
                try:
                    yield from _levels(tok.text, engine, depth + 1)
                except RoutineTokenError:
                    continue


def nondeterministic_calls(
    texts: Sequence[str], engine: str, *, random_seeded: bool
) -> tuple[str, ...]:
    """The functions ``texts`` call that the comparison cannot freeze, sorted; a text
    that does not tokenize gives ``("an unreadable text",)``. ``random_seeded`` says
    whether the two sides seed the engine's random function alike."""
    flagged = set(NONDETERMINISTIC[engine])
    if not random_seeded:
        flagged |= SEEDED_RANDOM[engine]
    found: set[str] = set()
    for text in texts:
        try:
            tokens = list(_tokens_deep(text, engine))
        except RoutineTokenError:
            return ("an unreadable text",)
        for tok, nxt in zip(tokens, tokens[1:], strict=False):
            if nxt.kind == "L_PAREN" and tok.text.casefold() in flagged:
                found.add(tok.text.casefold())
    return tuple(sorted(found))


def called_names(text: str, engine: str) -> tuple[str, ...]:
    """The names ``text`` calls (a name before ``(``, or after ``CALL``), lower case and
    without their schema; body included. Empty when the text does not tokenize."""
    found: set[str] = set()
    try:
        levels = list(_levels(text, engine))
    except RoutineTokenError:
        return ()
    for tokens in levels:
        for i, tok in enumerate(tokens[:-1]):
            nxt = tokens[i + 1]
            if nxt.kind == "L_PAREN" and tok.kind in _NAME:
                found.add(tok.text.casefold())
            if tok.text.upper() == "CALL" and nxt.kind in _NAME and not nxt.command_rest:
                j = i + 1
                while j + 2 < len(tokens) and tokens[j + 1].kind == "DOT":
                    j += 2
                found.add(tokens[j].text.casefold())
    return tuple(sorted(found))


_ENDS = {"SEMICOLON", "INTO", "USING"}


def dynamic_sql(texts: Sequence[str], engine: str) -> bool:
    """Whether ``texts`` run a statement built at run time, which no screen can read:
    PostgreSQL ``EXECUTE`` of anything but one string literal (``EXECUTE 'a' || b``,
    ``EXECUTE format(...)``), MySQL ``PREPARE ... FROM`` anything but one string literal
    (``FROM @sql``). A text that does not tokenize counts as dynamic."""
    for text in texts:
        try:
            levels = list(_levels(text, engine))
        except RoutineTokenError:
            return True
        for tokens in levels:
            if _builds_a_statement(tokens, engine):
                return True
    return False


def _builds_a_statement(tokens: Sequence[RoutineToken], engine: str) -> bool:
    """Whether one level of tokens runs a statement that is not one string literal."""
    for i, tok in enumerate(tokens):
        word = tok.text.upper()
        if engine == "postgresql" and word == "EXECUTE":
            nxt = tokens[i + 1] if i + 1 < len(tokens) else None
            if nxt is not None and nxt.text.upper() in ("FUNCTION", "PROCEDURE"):
                continue  # CREATE TRIGGER ... EXECUTE FUNCTION f()
            if not _one_literal(tokens, i + 1, engine):
                return True
        if engine == "mysql" and word == "PREPARE":
            rest = tokens[i + 1] if i + 1 < len(tokens) else None
            if rest is not None and rest.command_rest:  # the tokenizer kept the rest as text
                try:
                    inner = list(routine_tokens(rest.text, engine))
                except RoutineTokenError:
                    return True
                at = next((j for j, t in enumerate(inner) if t.text.upper() == "FROM"), None)
                if at is None or not _one_literal(inner, at + 1, engine):
                    return True
                continue
            at = next(
                (j for j in range(i, min(i + 4, len(tokens))) if tokens[j].text.upper() == "FROM"),
                None,
            )
            if at is None or not _one_literal(tokens, at + 1, engine):
                return True
    return False


def _one_literal(tokens: Sequence[RoutineToken], i: int, engine: str = "postgresql") -> bool:
    if i < len(tokens) and tokens[i].command_rest:  # the tokenizer kept the rest as text
        try:
            rest = list(routine_tokens(tokens[i].text, engine))
        except RoutineTokenError:
            return False
        return _one_literal(rest, 0, engine) if rest and not rest[0].command_rest else False
    if i >= len(tokens) or not tokens[i].kind.endswith("STRING"):
        return False
    return (
        i + 1 >= len(tokens) or tokens[i + 1].kind in _ENDS or tokens[i + 1].text.upper() in _ENDS
    )


_ZONE = re.compile(r"^(?:[A-Za-z]+(?:[/_+-][A-Za-z0-9_+-]+)+|UTC|GMT|Z|SYSTEM|[A-Z]{3,5})$")


def named_time_zones(texts: Sequence[str], engine: str) -> bool:
    """Whether ``texts`` name a time zone (``CONVERT_TZ(..., 'Europe/Paris')``, ``SET
    time_zone = 'UTC'``), which a MySQL server without zone tables reads as NULL. Always
    False on PostgreSQL, which has its zones built in."""
    if engine != "mysql":
        return False
    for text in texts:
        try:
            tokens = list(_tokens_deep(text, engine))
        except RoutineTokenError:
            return True
        if not any(t.text.casefold() in ("convert_tz", "time_zone") for t in tokens):
            continue
        if any(t.kind.endswith("STRING") and _ZONE.match(t.text.strip()) for t in tokens):
            return True
    return False


def _pg_body_statements(text: str) -> Iterator[str]:
    """Each SQL statement of a PostgreSQL body, from its first statement keyword (a
    PL/pgSQL ``IF ... THEN`` or ``RETURN QUERY`` before it is left out; ``PERFORM`` is read
    as ``SELECT``)."""
    for tok in routine_tokens(text, "postgresql"):
        if not tok.kind.endswith("STRING"):
            continue
        try:
            inner = routine_tokens(tok.text, "postgresql")
        except RoutineTokenError:
            continue
        piece: list[RoutineToken] = []
        for t in [*inner, None]:
            if t is None or t.kind == "SEMICOLON":
                start = next(
                    (i for i, p in enumerate(piece) if p.text.upper() in _STATEMENT_START), None
                )
                if start is not None:
                    first, last = piece[start], piece[-1]
                    sql = tok.text[first.start : last.end + 1]
                    yield "SELECT" + sql[7:] if first.text.upper() == "PERFORM" else sql
                piece = []
            else:
                piece.append(t)


def _references(
    text: str, engine: str, kind: str
) -> tuple[list[tuple[str | None, str]], list[tuple[str | None, str]]]:
    """``(tables, calls)`` the text names, as ``(schema, name)`` pairs, lower case."""
    tables: list[tuple[str | None, str]] = []
    calls: list[tuple[str | None, str]] = []
    if engine == "mysql":
        verdict = check_mysql_routine(
            MySqlRoutine(routine_id="proof.routine", kind=kind, create_sql=text),  # type: ignore[arg-type]
            target_engine="aurora_mysql",
        )
        for d in verdict.dependencies:
            (tables if d.kind == "table" else calls).append((d.schema_name, d.name))
        return tables, calls
    if kind == "trigger":
        for d in trigger_references(text) or ():
            (tables if d.kind == "table" else calls).append((d.schema_name, d.name))
        return tables, calls
    for sql in _pg_body_statements(text):
        refs = routine_statement_refs(sql, "postgresql")
        tables.extend(refs.tables)
        calls.extend(refs.calls)
    return tables, calls


def touched_tables(text: str, engine: str, kind: str) -> tuple[str, ...]:
    """The tables ``text`` names, as written (``name`` or ``schema.name``), sorted."""
    try:
        tables, _ = _references(text, engine, kind)
    except RoutineTokenError:
        return ()
    return tuple(sorted({f"{s}.{n}" if s else n for s, n in tables}))


def _qualified(
    tokens: Sequence[RoutineToken], i: int
) -> tuple[RoutineToken | None, RoutineToken | None, int]:
    if i >= len(tokens):
        return None, None, i
    if i + 2 < len(tokens) and tokens[i + 1].kind == "DOT":
        return tokens[i], tokens[i + 2], i + 3
    return None, tokens[i], i + 1


def _same_schema(tok: RoutineToken, namespace: str, engine: str) -> bool:
    if engine == "postgresql":
        written = tok.text if tok.kind == "IDENTIFIER" else tok.text.casefold()
        return written == namespace
    return tok.text == namespace  # MySQL: exact, a server that keeps case tells them apart


_NAME = {"VAR", "IDENTIFIER", "STRING"}


def _after_definer(tokens: Sequence[RoutineToken], i: int) -> int | None:
    """The index after a MySQL ``DEFINER = ...`` clause at ``i`` (or ``i`` when there is
    none); None when the clause is not ``user@host`` or ``CURRENT_USER[()]``."""
    if i >= len(tokens) or tokens[i].text.upper() != "DEFINER":
        return i
    if i + 1 >= len(tokens) or tokens[i + 1].kind != "EQ":
        return None
    j = i + 2
    if j < len(tokens) and tokens[j].kind == "CURRENT_USER":
        j += 1
        if j + 1 < len(tokens) and tokens[j].kind == "L_PAREN" and tokens[j + 1].kind == "R_PAREN":
            j += 2
        return j
    if (
        j + 2 < len(tokens)
        and tokens[j].kind in _NAME
        and tokens[j + 1].kind == "PARAMETER"
        and tokens[j + 1].text == "@"
        and tokens[j + 2].kind in _NAME
    ):
        return j + 3
    return None


def create_refusal(text: str, engine: str, kind: str, namespace: str) -> str | None:
    """Why ``text`` (one ``CREATE`` of ``kind``) may not be created in ``namespace`` for a
    comparison, or None (see the module docstring for what is refused)."""
    if engine == "postgresql" and _UNICODE_NAME.search(text):
        return "it writes a Unicode-escaped name (U&), which the screen does not read"
    if engine == "mysql" and "/*!" in text:
        return "it holds a versioned comment (/*!), which the server reads as code"
    try:
        tokens = list(routine_tokens(text, engine))
        deep = list(_tokens_deep(text, engine))
    except RoutineTokenError:
        return "its text could not be read"
    if engine == "postgresql":
        if kind not in ("function", "procedure", "trigger") or parse_routine(text, kind) is not None:  # type: ignore[arg-type]
            return f"its text is not exactly one {kind} statement the parser reads"
    elif (
        check_mysql_routine(
            MySqlRoutine(routine_id="proof.routine", kind=kind, create_sql=text),  # type: ignore[arg-type]
            target_engine="aurora_mysql",
        ).parse_failure
        is not None
    ):
        return f"its text is not exactly one {kind} statement the parser reads"
    if not tokens or tokens[0].kind != "CREATE":
        return "its header is not in the expected form"
    i = 1
    if [t.text.upper() for t in tokens[i : i + 2]] == ["OR", "REPLACE"]:
        i += 2
    if engine == "mysql":
        after = _after_definer(tokens, i)
        if after is None:
            return "its DEFINER clause is not in the expected form"
        i = after
    if engine == "postgresql" and i < len(tokens) and tokens[i].text.upper() == "CONSTRAINT":
        i += 1
    if i >= len(tokens) or tokens[i].text.upper() not in _KINDS:
        return "its header is not in the expected form"
    created = tokens[i].text.upper()
    i += 1
    if [t.text.upper() for t in tokens[i : i + 3]] == ["IF", "NOT", "EXISTS"]:
        i += 3
    schema, name, i = _qualified(tokens, i)
    if name is None or name.kind not in _NAME:
        return "its header is not in the expected form"
    places = [(schema, "its name")]
    if created == "TRIGGER":
        on = next((j for j in range(i, len(tokens)) if tokens[j].kind == "ON"), None)
        if on is not None:
            places.append((_qualified(tokens, on + 1)[0], "its table"))
    for written, what in places:
        if written is not None and not _same_schema(written, namespace, engine):
            return f"{what} is qualified with a schema other than the target's ({namespace})"
    if engine == "postgresql":  # the parser's own reading of every name the statement binds
        parsed = created_schemas(text, kind)  # type: ignore[arg-type]
        if parsed is None:
            return "its header is not in the expected form"
        if any(sch is not None and sch != namespace for sch in parsed):
            return f"its name is qualified with a schema other than the target's ({namespace})"
    words = [t.text.upper() for t in deep]
    for i, word in enumerate(words):
        if word == "ROLE" and i and words[i - 1] in ("SET", "LOCAL", "SESSION"):
            return "it changes the role it runs as"
        if word == "AUTHORIZATION" and i and words[i - 1] == "SESSION":
            return "it changes the role it runs as"
    if any(t.text.casefold() == "search_path" for t in deep):
        return "it sets search_path, so names would not resolve in the target's schema"
    if any(t.text.casefold() == "set_config" for t in deep):
        return "it calls set_config, which may set search_path"
    try:
        tables, calls = _references(text, engine, kind)
    except RoutineTokenError:
        return "its text could not be read"
    allowed = {namespace if engine == "mysql" else namespace.casefold()} | _SYSTEM_SCHEMAS[engine]
    for schema_name, _ in [*tables, *calls]:
        if schema_name is not None and schema_name not in allowed:
            return "its body names an object in a schema other than the target's"
    return None
