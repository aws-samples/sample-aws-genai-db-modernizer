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
  (``pg_catalog`` and ``information_schema`` aside; a table of a schema the setup created
  its tables in, ``schemas``, may be named, #1970);
  with such schemas, a call into one (also a ``CALL`` or ``PERFORM``, read from the tokens), a
  MySQL statement naming one that creates, alters, drops, renames or truncates, writes a file
  or runs ``USE``, and a function, procedure, operator or aggregate created or altered from a
  body (on PostgreSQL, whatever the schemas) are refused too. On MySQL only reads and row
  changes of a setup database's tables remain; on PostgreSQL DDL on those tables passes the
  screen and is undone by the call's ``ROLLBACK``;
- a name written so that the text and the server read it differently: a PostgreSQL
  ``U&"..."`` Unicode-escaped name, a MySQL ``/*! ... */`` versioned comment; and on
  PostgreSQL every name the statement binds is read again from the parser's tree, which
  decodes and folds names as the server does;
- a text that changes the role it runs as (``SET [LOCAL|SESSION] ROLE``, ``SET SESSION
  AUTHORIZATION``, ``RESET ROLE``, ``RESET ALL``, ``RESET SESSION AUTHORIZATION``, the
  ``session_authorization`` parameter set or reset, ``DISCARD ALL``), wherever it is
  written: a routine header's ``SET role`` clause after ``LANGUAGE`` or ``STABLE`` too, since
  it switches the role on entry and back on return; only an UPDATE's SET list names a column.
  Each token is read as one word, its comments dropped, a quoted name unquoted and the word
  upper-cased, so ``RESET "role"`` and ``RESET/*x*/role`` are ``RESET ROLE``. Matching words
  can miss a spelling: the test cluster's proof (#1945), which confines each call with ``SET
  LOCAL ROLE``, also reads ``current_user`` and ``session_user`` after every call, which sees
  the role a call ends with (``data_api_target``; ``set_config`` is refused below);
- a text that acts outside the scratch database: ``CREATE``, ``ALTER`` or ``DROP`` of a
  user, role, database or schema, ``RENAME USER``, ``GRANT``, ``REVOKE``, ``SET
  GLOBAL``/``PERSIST`` (or an ``@@GLOBAL.`` assignment), ``SET PASSWORD``, ``INSTALL`` and
  ``UNINSTALL`` (a plugin or component) and ``KILL``, since such a statement commits on its
  own on MySQL and outlives the call's rollback.

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
from collections import Counter
from collections.abc import Collection, Iterator, Sequence

from src.tools.routines.mysql_checks import MySqlRoutine, check_mysql_routine, statement_kinds
from src.tools.routines.postgresql_checks import (
    RoutineParts,
    created_schemas,
    parse_routine,
    routine_parts,
    trigger_references,
)
from src.tools.sql.facade import (
    RoutineToken,
    RoutineTokenError,
    routine_statement_refs,
    routine_tokens,
)

__all__ = [
    "BUILT_IN_OPERATORS",
    "CATALOG_TYPES",
    "NONDETERMINISTIC",
    "ROUTINE_STATEMENTS",
    "SEEDED_RANDOM",
    "called_functions",
    "called_names",
    "create_count",
    "create_refusal",
    "created_identity",
    "created_objects",
    "literal_sequence",
    "dynamic_executions",
    "dynamic_sql",
    "named_schemas",
    "named_time_zones",
    "operators_and_types",
    "privilege_clauses",
    "run_statements",
    "trigger_shape",
    "written_statements",
    "nondeterministic_calls",
    "read_touched_tables",
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
            # Only the parse and the dependencies are read here, not the version rules.
            source_version=None,
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


def read_touched_tables(text: str, engine: str, kind: str) -> tuple[str, ...] | None:
    """:func:`touched_tables`, or None when ``text`` does not parse as a ``kind``
    (``procedure``, ``function`` or ``trigger``) of ``engine``, so a caller can tell "names
    no table" from "could not be read" (#2463)."""
    try:
        if engine == "mysql":
            verdict = check_mysql_routine(
                MySqlRoutine(routine_id="proof.routine", kind=kind, create_sql=text),  # type: ignore[arg-type]
                target_engine="aurora_mysql",
                source_version=None,
            )
            if verdict.parse_failure is not None:
                return None
        elif engine == "postgresql":
            if kind == "trigger":
                if trigger_references(text) is None:
                    return None
            elif parse_routine(text, kind) is not None:  # type: ignore[arg-type]
                return None
        else:
            return None
        tables, _ = _references(text, engine, kind)
    except (RoutineTokenError, ValueError):
        return None
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


#: A comment the tokenizer can leave on a word (``/*x*/role``, ``-- note`` then a newline).
_COMMENT = re.compile(r"/\*.*?\*/|--[^\n]*(?:\n|$)", re.S)


def _unquoted(text: str) -> str | None:
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ('"', "`"):
        return text[1:-1].replace(text[0] * 2, text[0])
    return None


def _word(tok: RoutineToken) -> str:
    """A token as one normalised word: a quoted identifier (``"role"``, a MySQL backticked
    name) unquoted; otherwise its comments dropped and its first word kept (the tokenizer
    joins ``/*x*/role`` into one token, and MySQL's ``USER a TO b`` into another); then
    upper-cased, so ``RESET "role"`` and ``RESET/*x*/role`` read as ``RESET ROLE``."""
    text = tok.text
    plain = _unquoted(text)
    if plain is None:
        parts = _COMMENT.sub(" ", text).split()
        text = parts[0] if parts else ""
        plain = _unquoted(text)
    return (plain if plain is not None else text).upper()


#: Statements that change the role a call runs as, or act outside the scratch database
#: (accounts, privileges, other databases, the server's global settings): as normalised
#: word sequences, each matched at any place of any level of the text.
_ROLE_CHANGES: tuple[tuple[str, ...], ...] = (
    ("SET", "ROLE"), ("SET", "LOCAL", "ROLE"), ("SET", "SESSION", "ROLE"),
    ("RESET", "ROLE"), ("RESET", "ALL"), ("RESET", "SESSION"), ("DISCARD", "ALL"),
    ("SESSION", "AUTHORIZATION"), ("RESET", "SESSION_AUTHORIZATION"),
    ("SET", "SESSION_AUTHORIZATION"), ("SET", "LOCAL", "SESSION_AUTHORIZATION"),
    ("SET", "SESSION", "SESSION_AUTHORIZATION"),
)  # fmt: skip
_ACCOUNT_CHANGES: tuple[tuple[str, ...], ...] = (
    ("CREATE", "USER"), ("ALTER", "USER"), ("DROP", "USER"), ("RENAME", "USER"),
    ("CREATE", "ROLE"), ("ALTER", "ROLE"), ("DROP", "ROLE"), ("GRANT",), ("REVOKE",),
    ("CREATE", "DATABASE"), ("DROP", "DATABASE"), ("ALTER", "DATABASE"),
    ("CREATE", "SCHEMA"), ("DROP", "SCHEMA"), ("ALTER", "SCHEMA"),
    ("SET", "GLOBAL"), ("SET", "PERSIST"), ("SET", "PERSIST_ONLY"), ("SET", "PASSWORD"),
    ("INSTALL",), ("UNINSTALL",), ("KILL",),
)  # fmt: skip


#: The words a statement of a body can follow.
_STATEMENT_AFTER = {";", "BEGIN", "THEN", "ELSE", "LOOP", "DO", "AS", "$$"}


#: Words that make a statement a routine header, whose ``SET`` clauses set the routine's own
#: configuration (a ``SET role`` there switches the role on entry and back on return).
_HEADER_WORDS = {"FUNCTION", "PROCEDURE", "ROUTINE", "TRIGGER"}


def _in_update(words: Sequence[str], i: int) -> bool:
    """Whether the ``SET`` at ``i`` is an UPDATE's SET list: the statement it is in (back to
    the last statement boundary) has ``UPDATE`` before it, as ``UPDATE t SET role = ...`` and
    ``ON CONFLICT ... DO UPDATE SET role = ...`` do, and no routine header word stands
    between the last ``;`` and it (a parameter or type named ``"update"`` reads as UPDATE)."""
    j = i - 1
    while j >= 0 and words[j] not in _STATEMENT_AFTER:
        j -= 1
    if "UPDATE" not in words[j + 1 : i]:
        return False
    k = i - 1
    while k >= 0 and words[k] != ";":
        if words[k] in _HEADER_WORDS:
            return False
        k -= 1
    return True


def _role_or_account_change(text: str, engine: str) -> str | None:
    """Why ``text`` may change its role or act outside the scratch database, or None."""
    try:
        levels = list(_levels(text, engine))
    except RoutineTokenError:
        return "its text could not be read"
    for tokens in levels:
        words = [_word(t) for t in tokens]
        for i in range(len(words)):
            if (
                words[i] == "@@"
                and i + 1 < len(words)
                and words[i + 1] in ("GLOBAL", "PERSIST", "PERSIST_ONLY")
            ):
                return "it changes a global setting of the server"
            if tuple(words[i : i + 2]) == ("SESSION", "AUTHORIZATION"):
                return "it changes the role it runs as"
            # A role change anywhere, not only at a statement's start: a function's own
            # ``SET role`` clause (after LANGUAGE, STABLE, ...) switches the role on entry and
            # back on return, so no read after the call sees it. Only an UPDATE's SET list
            # (``UPDATE t SET role = ...``, ``DO UPDATE SET role``) sets a column instead.
            for pattern in _ROLE_CHANGES:
                if tuple(words[i : i + len(pattern)]) == pattern and not _in_update(words, i):
                    return "it changes the role it runs as"
            if i and words[i - 1] not in _STATEMENT_AFTER:
                continue  # an account word mid-statement names a column, not an account
            for pattern in _ACCOUNT_CHANGES:
                if tuple(words[i : i + len(pattern)]) == pattern:
                    return "it changes accounts, privileges, databases or global settings"
    return None


#: What changes a MySQL database's own objects, or writes a file: refused in a statement that
#: names a database the setup made, whose tables a body may only read and write rows of.
_MY_CHANGES = {"CREATE", "ALTER", "DROP", "RENAME", "TRUNCATE", "OUTFILE", "DUMPFILE"}
#: A word before ``schema.name (`` that makes it a table with a column list, not a call.
_PG_TABLE_BEFORE = {"INTO", "ON", "TABLE", "COPY", "ANALYZE", "ANALYSE", "VACUUM", "ONLY"}
_PG_CREATED = {"FUNCTION", "PROCEDURE", "OPERATOR", "AGGREGATE"}


def _setup_name(tok: RoutineToken, schema: str, engine: str) -> bool:
    """Whether ``tok`` is the setup schema ``schema``. A MySQL database name is read in any
    letter case (a server with ``lower_case_table_names`` 1 or 2 treats ``INVENTORY`` as
    ``inventory``); refusing more is safe."""
    if engine == "mysql":
        return tok.text.casefold() == schema.casefold()
    return _same_schema(tok, schema, engine)


def _names_schema(
    toks: Sequence[RoutineToken], i: int, schemas: Collection[str], engine: str
) -> bool:
    """Whether ``toks[i]`` is a setup schema written before a dot (``sales.``)."""
    tok = toks[i]
    return (
        tok.kind in ("VAR", "IDENTIFIER")
        and i + 1 < len(toks)
        and toks[i + 1].kind == "DOT"
        and any(_setup_name(tok, s, engine) for s in schemas)
    )


def _mysql_setup_refusal(
    text: str, schemas: Collection[str], depth: int = 0, inherited: frozenset[str] = frozenset()
) -> str | None:
    """Statement by statement (a command's rest, and a string, read one level down with the
    command's word kept): one that names a setup database and holds any of ``_MY_CHANGES``,
    or is ``USE`` of it, is refused. The routine's own leading ``CREATE`` is not counted."""
    try:
        toks = routine_tokens(text, "mysql")
    except RoutineTokenError:
        return None if depth else "its text could not be read"
    statement: list[int] = []
    for i in [*range(len(toks)), len(toks)]:
        if i < len(toks) and toks[i].kind != "SEMICOLON":
            statement.append(i)
            continue
        words = set(inherited) | {
            toks[j].text.upper()
            for j in statement
            if toks[j].kind not in ("IDENTIFIER", "STRING") and (depth, j) != (0, 0)
        }
        names = any(_names_schema(toks, j, schemas, "mysql") for j in statement)
        for j in statement:
            if toks[j].text.upper() == "USE" and toks[j].kind not in ("IDENTIFIER", "STRING"):
                if j + 1 < len(toks) and any(_setup_name(toks[j + 1], s, "mysql") for s in schemas):
                    names = True
        if "USE" in inherited and statement:  # ``USE`` is a command: its rest is a string
            names = names or any(_setup_name(toks[statement[0]], s, "mysql") for s in schemas)
        if names and words & (_MY_CHANGES | {"USE"}):
            return "its text changes or writes out a database the setup made"
        if depth < _DEPTH:
            for j in statement:
                tok = toks[j]
                if tok.command_rest or tok.kind.endswith("STRING"):
                    kept = (
                        frozenset({toks[j - 1].text.upper()})
                        if tok.command_rest and j
                        else frozenset()
                    )
                    why = _mysql_setup_refusal(
                        tok.text,
                        schemas,
                        depth + 1,
                        kept | (inherited if tok.command_rest else frozenset()),
                    )
                    if why is not None:
                        return why
        statement = []
    return None


def _setup_schema_refusal(text: str, engine: str, schemas: Collection[str]) -> str | None:
    """What a body may not do with the schemas the setup made (#1970), read from the tokens
    of the text and of every string in it. MySQL: a statement that names one of the
    databases may not create, alter, drop, rename or truncate anything, write a file, or
    ``USE`` it, so only reads and row changes of its tables remain. PostgreSQL: no call of a
    function in one of the schemas (``sales.f(...)`` after any word but the few that put a
    table before its column list), and, schema or not, no ``CREATE`` or ``ALTER`` of a
    function, procedure, operator or aggregate from a body (so no ``RENAME`` or ``SET
    SCHEMA`` of one either): a more specific signature in ``public`` or a setup schema would
    win over ``pg_catalog``'s for the routine's own calls. A routine whose own header is
    that signature is not read for it. MySQL's check is conservative on words: ``TRUNCATE(``
    the function, a column named ``dumpfile`` or a message string that holds one of the
    words, in a statement that names a setup database, is refused too."""
    try:
        levels = list(_levels(text, engine))
    except RoutineTokenError:
        return "its text could not be read"
    for depth, toks in enumerate(levels):
        if engine == "postgresql":
            for i, tok in enumerate(toks):
                if (depth, i) != (0, 0) and tok.text.upper() in ("CREATE", "ALTER"):
                    after = [t.text.upper() for t in toks[i + 1 : i + 4]]
                    if after[:2] == ["OR", "REPLACE"]:
                        after = after[2:]
                    if after and after[0] in _PG_CREATED:
                        return "its body creates or alters a function, procedure, operator or aggregate"
                if (
                    _names_schema(toks, i, schemas, engine)
                    and i + 3 < len(toks)
                    and toks[i + 3].kind == "L_PAREN"
                    and (i == 0 or toks[i - 1].text.upper() not in _PG_TABLE_BEFORE)
                ):
                    return "its text calls a function in a schema the setup made"
            continue
    if engine == "mysql" and schemas:
        return _mysql_setup_refusal(text, schemas)
    return None


def create_refusal(
    text: str, engine: str, kind: str, namespace: str, schemas: Collection[str] = ()
) -> str | None:
    """Why ``text`` (one ``CREATE`` of ``kind``) may not be created in ``namespace`` for a
    comparison, or None (see the module docstring for what is refused).

    ``schemas`` are the source schemas (PostgreSQL) or databases (MySQL) the setup created
    its tables in (#1970): a body's table or a trigger's ``ON`` table may name one, since the
    setup put it there. A routine is never created in one, and the tokens of the text are
    read for the rest (:func:`_setup_schema_refusal`): no call into one, no MySQL statement
    that changes or writes out one, no function or operator created from a body. A call to
    another schema's routine is refused as before; a call the parser does not read (a
    ``CALL`` or ``PERFORM`` in PL/pgSQL) is read from the tokens."""
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
            # Only the parse and the dependencies are read here, not the version rules.
            source_version=None,
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
        if written is None or _same_schema(written, namespace, engine):
            continue
        if what == "its table" and any(_same_schema(written, s, engine) for s in schemas):
            continue
        return f"{what} is qualified with a schema other than the target's ({namespace})"
    settled = {s.casefold() for s in schemas}
    if engine == "postgresql":  # the parser's own reading of every name the statement binds
        parsed = created_schemas(text, kind)  # type: ignore[arg-type]
        if parsed is None:
            return "its header is not in the expected form"
        # a trigger's table comes first (it may sit in a setup schema), its function last
        named = parsed[1:] if kind == "trigger" else parsed
        table = parsed[:1] if kind == "trigger" else ()
        if any(sch is not None and sch != namespace for sch in named) or any(
            sch is not None and sch != namespace and sch.casefold() not in settled for sch in table
        ):
            return f"its name is qualified with a schema other than the target's ({namespace})"
    why = _role_or_account_change(text, engine)
    if why is not None:
        return why
    if any(t.text.casefold() == "search_path" for t in deep):
        return "it sets search_path, so names would not resolve in the target's schema"
    if any(t.text.casefold() == "set_config" for t in deep):
        return "it calls set_config, which may set search_path"
    why = _setup_schema_refusal(text, engine, schemas)
    if why is not None:
        return why
    try:
        tables, calls = _references(text, engine, kind)
    except RoutineTokenError:
        return "its text could not be read"
    own = {namespace if engine == "mysql" else namespace.casefold()} | _SYSTEM_SCHEMAS[engine]
    # a setup schema holds tables only: a call into it is still another schema's routine
    held = {s if engine == "mysql" else s.casefold() for s in schemas}
    for schema_name, _ in calls:
        if schema_name is not None and schema_name not in own:
            return "its body names an object in a schema other than the target's"
    for schema_name, _ in tables:
        if schema_name is not None and schema_name not in own | held:
            return "its body names an object in a schema other than the target's"
    return None


# --------------------------------------------------------------------------- repair screen
# What the routine repair seam (#1940) compares between a routine's text and a model's
# candidate: the name it creates, the schemas it names and the objects it creates. Each
# reads the same tokens and references as the screen above, and returns None for a text
# it cannot read, so the caller refuses it.


def _name_of(tok: RoutineToken, engine: str) -> str:
    # PostgreSQL folds an unquoted name to lower case; a quoted one, and MySQL's, as written.
    if engine == "postgresql" and tok.kind != "IDENTIFIER":
        return tok.text.casefold()
    return tok.text


def created_identity(
    text: str, engine: str
) -> tuple[str, str | None, str, str | None, str | None] | None:
    """What ``text`` (one ``CREATE``) creates: ``(kind word, schema, name, trigger table's
    schema, trigger table)``, names as the server reads them; None when the header does
    not read as ``CREATE [OR REPLACE] [DEFINER ...] [CONSTRAINT] <kind> [IF NOT EXISTS]
    <name>``."""
    try:
        tokens = list(routine_tokens(text, engine))
    except RoutineTokenError:
        return None
    if not tokens or tokens[0].kind != "CREATE":
        return None
    i = 1
    if [t.text.upper() for t in tokens[i : i + 2]] == ["OR", "REPLACE"]:
        i += 2
    if engine == "mysql":
        after = _after_definer(tokens, i)
        if after is None:
            return None
        i = after
    if engine == "postgresql" and i < len(tokens) and tokens[i].text.upper() == "CONSTRAINT":
        i += 1
    if i >= len(tokens) or tokens[i].text.upper() not in _KINDS:
        return None
    created = tokens[i].text.upper()
    i += 1
    if [t.text.upper() for t in tokens[i : i + 3]] == ["IF", "NOT", "EXISTS"]:
        i += 3
    schema, name, i = _qualified(tokens, i)
    if name is None or name.kind not in _NAME:
        return None
    on_schema = on_table = None
    if created == "TRIGGER":
        on = next((j for j in range(i, len(tokens)) if tokens[j].kind == "ON"), None)
        if on is None:
            return None
        table_schema, table, _ = _qualified(tokens, on + 1)
        if table is None or table.kind not in _NAME:
            return None
        on_schema = _name_of(table_schema, engine) if table_schema is not None else None
        on_table = _name_of(table, engine)
    return (
        created,
        _name_of(schema, engine) if schema is not None else None,
        _name_of(name, engine),
        on_schema,
        on_table,
    )


def named_schemas(text: str, engine: str, kind: str) -> frozenset[str] | None:
    """Every schema ``text`` names: the created name's and a trigger table's, and those of
    the tables and routines its body names, as the screen reads them (lower case); None
    when it cannot be read."""
    identity = created_identity(text, engine)
    if identity is None:
        return None
    try:
        tables, calls = _references(text, engine, kind)
    except Exception:  # noqa: BLE001 -- a text the readers cannot read is refused
        return None
    found = {s for s, _ in (*tables, *calls) if s}
    found |= {s for s in (identity[1], identity[3]) if s}
    return frozenset(s.casefold() for s in found)


def create_count(text: str, engine: str) -> int | None:
    """How many ``CREATE`` keywords ``text`` holds, its body included (three levels of
    strings deep); None when it does not tokenize."""
    try:
        return sum(1 for tok in _tokens_deep(text, engine) if tok.kind == "CREATE")
    except RoutineTokenError:
        return None


#: Words between ``CREATE`` and the kind of object it creates.
_CREATE_MODIFIERS = {
    "OR", "REPLACE", "TEMP", "TEMPORARY", "UNLOGGED", "GLOBAL", "LOCAL", "UNIQUE",
    "CONSTRAINT", "RECURSIVE", "MATERIALIZED", "DEFAULT", "TRUSTED", "PROCEDURAL",
}  # fmt: skip
#: The statements that write or change an object, named with the two words after them.
_WRITES = {
    "INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE", "ALTER", "DROP", "COPY", "REPLACE",
    "NOTIFY", "COMMENT", "LOCK", "CALL", "REFRESH", "RENAME",
}  # fmt: skip


def _key(tok: RoutineToken, engine: str) -> str:
    return _name_of(tok, engine) if tok.kind in _NAME else tok.text.upper()


def _deep_levels(text: str, engine: str) -> list[list[RoutineToken]] | None:
    try:
        return list(_levels(text, engine))
    except RoutineTokenError:
        return None


def _levels_in_language(text: str, engine: str) -> list[tuple[list[RoutineToken], bool]] | None:
    """:func:`_levels`, each level with whether it is a PL/pgSQL routine's own body (where
    ``SELECT ... INTO`` assigns variables); None when the text does not tokenize."""
    out: list[tuple[list[RoutineToken], bool]] = []

    def walk(source: str, depth: int, plpgsql: bool, top: bool) -> None:
        tokens = list(routine_tokens(source, engine))
        out.append((tokens, plpgsql))
        if depth >= _DEPTH:
            return
        words = [t.text.casefold() for t in tokens]
        language = next((words[i + 1] for i, w in enumerate(words[:-1]) if w == "language"), None)
        for i, tok in enumerate(tokens):
            if not (tok.command_rest or tok.kind.endswith("STRING")):
                continue
            body = top and i > 0 and tokens[i - 1].text.upper() == "AS"
            try:
                walk(tok.text, depth + 1, body and language == "plpgsql", False)
            except RoutineTokenError:
                continue

    try:
        walk(text, 0, False, True)
    except RoutineTokenError:
        return None
    return out


#: The statements after which an ``INTO`` names a target the statement fills or creates.
_INTO_OWNERS = {"SELECT", "INSERT", "MERGE", "FETCH", "MOVE", "COPY", "IMPORT", "RETURNING"}
_INTO_TABLE = {"TEMP", "TEMPORARY", "UNLOGGED", "TABLE"}


def _selected_into(
    tokens: Sequence[RoutineToken], engine: str, plpgsql: bool
) -> Iterator[tuple[str, ...]]:
    """The tables a PostgreSQL ``SELECT ... INTO [TEMP | UNLOGGED] [TABLE] <name>``
    creates. In a PL/pgSQL body a bare ``INTO <name>`` assigns a variable, so only the
    forms with ``TEMP``, ``UNLOGGED`` or ``TABLE`` count there."""
    if engine != "postgresql":
        return
    owner: str | None = None
    for i, tok in enumerate(tokens):
        word = tok.text.upper()
        if tok.kind == "SEMICOLON":
            owner = None
        elif word in _INTO_OWNERS:
            owner = word
        elif word == "INTO" and owner == "SELECT":
            j, temp, table = i + 1, "", False
            while j < len(tokens) and tokens[j].text.upper() in _INTO_TABLE:
                temp = "TEMP" if tokens[j].text.upper() in ("TEMP", "TEMPORARY") else temp
                table = True
                j += 1
            if plpgsql and not table:
                continue
            schema, name, _ = _qualified(tokens, j)
            if name is not None:
                yield (
                    temp,
                    "TABLE",
                    _name_of(schema, engine) if schema is not None else "",
                    _key(name, engine),
                )


def created_objects(text: str, engine: str) -> Counter[tuple[str, ...]] | None:
    """Each object ``text`` creates, its body and every string in it included (a
    statement run with ``EXECUTE``): ``(TEMP or "", kind word, schema, name)``, counted,
    a PostgreSQL ``SELECT ... INTO`` table included (:func:`_selected_into`); None when it
    does not tokenize."""
    levels = _levels_in_language(text, engine)
    if levels is None:
        return None
    found: Counter[tuple[str, ...]] = Counter()
    for tokens, plpgsql in levels:
        found.update(_selected_into(tokens, engine, plpgsql))
        for i, tok in enumerate(tokens):
            if tok.kind != "CREATE":
                continue
            j, temp = i + 1, ""
            while j < len(tokens):
                if engine == "mysql" and tokens[j].text.upper() == "DEFINER":
                    after = _after_definer(tokens, j)
                    j = after if after is not None else j + 1
                    continue
                word = tokens[j].text.upper()
                if word not in _CREATE_MODIFIERS:
                    break
                temp = "TEMP" if word in ("TEMP", "TEMPORARY") else temp
                j += 1
            kind = tokens[j].text.upper() if j < len(tokens) else ""
            j += 1
            if [t.text.upper() for t in tokens[j : j + 3]] == ["IF", "NOT", "EXISTS"]:
                j += 3
            schema, name, _ = _qualified(tokens, j)
            found[
                (
                    temp,
                    kind,
                    _name_of(schema, engine) if schema is not None else "",
                    _key(name, engine) if name is not None else "",
                )
            ] += 1
    return found


def written_statements(text: str, engine: str) -> Counter[tuple[str, ...]] | None:
    """Each statement ``text`` runs that writes or changes an object (``INSERT``,
    ``UPDATE``, ``DELETE``, ``MERGE``, ``TRUNCATE``, ``ALTER``, ``DROP``, ..., and a MySQL
    ``INTO OUTFILE`` or ``INTO DUMPFILE``), with the two tokens after it, counted, its body and strings included; None when it does not
    tokenize."""
    levels = _deep_levels(text, engine)
    if levels is None:
        return None
    found: Counter[tuple[str, ...]] = Counter()
    for tokens in levels:
        for i, tok in enumerate(tokens):
            word = tok.text.upper()
            if word in ("OUTFILE", "DUMPFILE") and engine == "mysql":
                # SELECT ... INTO OUTFILE writes a file (#2399 review), as COPY does.
                before = tokens[i - 1].text.upper() if i else ""
                if before == "INTO" and tok.kind != "IDENTIFIER":
                    found[(word, *(_key(t, engine) for t in tokens[i + 1 : i + 3]))] += 1
                continue
            if word in _WRITES and tok.kind not in ("IDENTIFIER",):
                found[(tok.text.upper(), *(_key(t, engine) for t in tokens[i + 1 : i + 3]))] += 1
    return found


def dynamic_executions(text: str, engine: str) -> Counter[tuple[str, ...]] | None:
    """Each statement ``text`` builds and runs at run time, counted by kind, its body and
    strings included (#1941 review): a PL/pgSQL ``EXECUTE`` (not a trigger's ``EXECUTE
    FUNCTION``) by what it runs (a string literal, ``format(...)``, anything else) and
    whether it binds ``USING``, and a MySQL ``PREPARE`` or ``EXECUTE`` (not ``DEALLOCATE
    PREPARE``); None when it does not tokenize."""
    levels = _deep_levels(text, engine)
    if levels is None:
        return None
    found: Counter[tuple[str, ...]] = Counter()
    for tokens in levels:
        for i, tok in enumerate(tokens):
            word = tok.text.upper()
            if tok.kind == "IDENTIFIER" or tok.kind.endswith("STRING"):
                continue
            nxt = tokens[i + 1] if i + 1 < len(tokens) else None
            before = tokens[i - 1].text.upper() if i else ""
            if engine == "postgresql" and word == "EXECUTE":
                if nxt is not None and nxt.text.upper() in ("FUNCTION", "PROCEDURE"):
                    continue
                if nxt is None:
                    what = "nothing"
                elif nxt.kind.endswith("STRING") and not nxt.command_rest:
                    what = "literal"
                elif nxt.text.lower() == "format":
                    what = "format"
                else:
                    what = "expression"
                rest = []
                for t in tokens[i + 1 :]:
                    if t.kind == "SEMICOLON":
                        break
                    rest.append(t.text.upper())
                found[("EXECUTE", what, "USING" if "USING" in rest else "")] += 1
            elif engine == "mysql" and word in ("PREPARE", "EXECUTE") and before != "DEALLOCATE":
                found[(word,)] += 1
    return found


def literal_sequence(text: str, engine: str) -> tuple[tuple[str, str], ...] | None:
    """Every literal ``text`` holds, in order, with its kind (quote form) and any MySQL
    character-set introducer, three levels of strings deep; a PostgreSQL routine's own
    body (the string after ``AS``) is read through, not counted. None when it does not
    tokenize."""
    out: list[tuple[str, str]] = []

    def walk(tokens: Sequence[RoutineToken], depth: int, top: bool) -> None:
        previous: RoutineToken | None = None
        for tok in tokens:
            body = (
                top
                and engine == "postgresql"
                and previous is not None
                and previous.text.upper() == "AS"
            )
            # A statement's unread tail (``command_rest``) is code, not a literal: read through.
            literal = tok.kind.endswith("STRING") and not body and not tok.command_rest
            if tok.kind == "INTRODUCER" or literal:
                out.append((tok.kind, tok.text))
            if depth < _DEPTH and (tok.command_rest or tok.kind.endswith("STRING")):
                try:
                    inner = list(routine_tokens(tok.text, engine))
                except RoutineTokenError:
                    inner = []
                walk(inner, depth + 1, False)
            previous = tok

    try:
        walk(list(routine_tokens(text, engine)), 0, True)
    except RoutineTokenError:
        return None
    return tuple(out)


def privilege_clauses(text: str, engine: str) -> tuple[tuple[str, ...], ...] | None:
    """Who a routine runs as, as its header says it: a MySQL ``DEFINER = ...`` and ``SQL
    SECURITY`` clause, a PostgreSQL ``[EXTERNAL] SECURITY DEFINER | INVOKER``, in order;
    None when it does not tokenize."""
    try:
        tokens = list(routine_tokens(text, engine))
    except RoutineTokenError:
        return None
    found: list[tuple[str, ...]] = []
    for i, tok in enumerate(tokens):
        word = tok.text.upper()
        if (
            engine == "mysql"
            and word == "DEFINER"
            and i + 1 < len(tokens)
            and tokens[i + 1].kind == "EQ"
        ):
            end = _after_definer(tokens, i) or i + 1
            found.append(("DEFINER", *(t.text for t in tokens[i + 1 : end])))
        elif word in ("SECURITY", "SQL SECURITY") and i + 1 < len(tokens):
            found.append(("SECURITY", tokens[i + 1].text.upper()))
    return tuple(found)


def trigger_shape(text: str, engine: str) -> tuple[str, ...] | None:
    """What a trigger fires on and runs, from its header: the words between its name and
    ``ON`` (timing and events), its ``FOR EACH`` level, its ``WHEN`` condition and the
    function it executes (PostgreSQL), and the trigger it ``FOLLOWS`` or ``PRECEDES``
    (MySQL); None when the header does not read."""
    identity = created_identity(text, engine)
    if identity is None or identity[0] != "TRIGGER":
        return None
    tokens = list(routine_tokens(text, engine))
    start = next((i for i, t in enumerate(tokens) if t.text.upper() == "TRIGGER"), None)
    if start is None:
        return None
    _, _, i = _qualified(tokens, start + 1)
    shape: list[str] = []
    while i < len(tokens) and tokens[i].kind != "ON":
        shape.append(_key(tokens[i], engine))
        i += 1
    for j, tok in enumerate(tokens[i:], start=i):
        word = tok.text.upper()
        if word == "EACH" and j + 1 < len(tokens):
            shape += ["EACH", tokens[j + 1].text.upper()]
        elif word in ("FOLLOWS", "PRECEDES") and j + 1 < len(tokens):
            shape += [word, _key(tokens[j + 1], engine)]
        elif engine == "postgresql" and word == "WHEN":
            end = next(
                (k for k in range(j + 1, len(tokens)) if tokens[k].text.upper() == "EXECUTE"),
                len(tokens),
            )
            shape += ["WHEN", *(_key(t, engine) for t in tokens[j + 1 : end])]
        elif engine == "postgresql" and word == "EXECUTE":
            schema, name, _ = _qualified(tokens, j + 2)
            shape += [
                "EXECUTE",
                _name_of(schema, engine) if schema is not None else "",
                _key(name, engine) if name is not None else "",
            ]
            break
        elif engine == "mysql" and word == "ROW":
            if j + 1 < len(tokens) and tokens[j + 1].text.upper() in ("FOLLOWS", "PRECEDES"):
                shape += (
                    [tokens[j + 1].text.upper(), _key(tokens[j + 2], engine)]
                    if j + 2 < len(tokens)
                    else []
                )
            break
    return tuple(shape)


def _cast_syntax(tokens: Sequence[RoutineToken], i: int) -> bool:
    """Whether ``tokens[i]`` is PostgreSQL's ``CAST`` keyword: written unquoted (the
    tokenizer reads a quoted name as an ``IDENTIFIER``) and not after a dot, where any
    word is a name. ``CAST`` is a reserved word, so nothing else reads that way."""
    tok = tokens[i]
    return (
        tok.kind == "VAR"
        and tok.text.casefold() == "cast"
        and not (i >= 1 and tokens[i - 1].kind == "DOT")
    )


def called_functions(text: str, engine: str) -> frozenset[str] | None:
    """Every function or procedure ``text`` calls (a name before ``(``, or after
    ``CALL``), its body and strings included, as written: ``schema.name`` when qualified,
    else ``name``, lower case. The routine's own header name and its parameter types are
    included too (the caller compares two texts with the same header). On PostgreSQL an
    unquoted, unqualified ``CAST(`` is the cast syntax, the same type cast as ``x::t``
    (#2452), never a call: ``"cast"(x)`` and ``s.cast(x)`` stay calls. None when it does
    not tokenize."""
    levels = _deep_levels(text, engine)
    if levels is None:
        return None
    found: set[str] = set()
    for tokens in levels:
        for i, tok in enumerate(tokens[:-1]):
            nxt = tokens[i + 1]
            called = tok.kind in _NAME and nxt.kind == "L_PAREN"
            if called and engine == "postgresql" and _cast_syntax(tokens, i):
                continue
            if tok.text.upper() == "CALL" and nxt.kind in _NAME and not nxt.command_rest:
                called, tok, i = True, nxt, i + 1
                while i + 2 < len(tokens) and tokens[i + 1].kind == "DOT":
                    tok, i = tokens[i + 2], i + 2
            if not called:
                continue
            name = tok.text.casefold()
            if i >= 2 and tokens[i - 1].kind == "DOT" and tokens[i - 2].kind in _NAME:
                name = f"{tokens[i - 2].text.casefold()}.{name}"
            found.add(name)
    return frozenset(found)


#: The statement kinds every routine may run, as :func:`run_statements` names them: reads,
#: and the procedural language's own control statements (#2373). ``EXECUTE`` and MySQL's
#: ``PREPARE``, ``EXECUTE`` and ``DEALLOCATE`` are here because :func:`dynamic_executions`
#: counts them.
ROUTINE_STATEMENTS = {
    "postgresql": frozenset({
        "SelectStmt",
        "PLpgSQL_stmt_block", "PLpgSQL_stmt_assign", "PLpgSQL_stmt_if", "PLpgSQL_stmt_case",
        "PLpgSQL_stmt_loop", "PLpgSQL_stmt_while", "PLpgSQL_stmt_fori", "PLpgSQL_stmt_fors",
        "PLpgSQL_stmt_forc", "PLpgSQL_stmt_foreach_a", "PLpgSQL_stmt_exit",
        "PLpgSQL_stmt_return", "PLpgSQL_stmt_return_next", "PLpgSQL_stmt_return_query",
        "PLpgSQL_stmt_raise", "PLpgSQL_stmt_assert", "PLpgSQL_stmt_perform",
        "PLpgSQL_stmt_getdiag", "PLpgSQL_stmt_open", "PLpgSQL_stmt_fetch",
        "PLpgSQL_stmt_close", "PLpgSQL_stmt_dynexecute", "PLpgSQL_stmt_dynfors",
    }),
    "mysql": frozenset({
        "SELECT", "VALUES", "TABLE", "SET", "DO", "OPEN", "FETCH", "CLOSE", "SIGNAL",
        "RESIGNAL", "GET", "PREPARE", "EXECUTE", "DEALLOCATE",
    }),
}  # fmt: skip
#: PostgreSQL's built-in operator symbols: every ``oprname`` of ``pg_catalog`` on 14 and 17.
BUILT_IN_OPERATORS = frozenset("""
    !! !~ !~* !~~ !~~* # ## #- #> #>> % & && &< &<| &> * *< *<= *<> *= *> *>= + - -> ->>
    -|- / < <-> << <<= <<| <= <> <@ <^ = > >= >> >>= >^ ? ?# ?& ?- ?-| ?| ?|| @ @-@ @> @?
    @@ @@@ ^ ^@ | |&> |/ |>> || ||/ ~ ~* ~<=~ ~<~ ~= ~>=~ ~>~ ~~ ~~*
""".split())
#: The types of ``pg_catalog`` a routine may name unqualified: every base type on 14 and 17
#: and ``record``, plus the serial column types the parser keeps as written. Pseudo types
#: (``internal``, ``cstring``, ``any*``, the handlers, ``trigger``, ``void``) are left out:
#: no cast a routine needs goes to one, and a header that names one is the routine's own.
CATALOG_TYPES = frozenset("""
    aclitem bit bool box bpchar bytea char cid cidr circle date datemultirange daterange
    float4 float8 gtsvector inet int2 int2vector int4 int4multirange int4range int8
    int8multirange int8range interval json jsonb jsonpath line lseg macaddr macaddr8 money
    name numeric nummultirange numrange oid oidvector path pg_brin_bloom_summary
    pg_brin_minmax_multi_summary pg_dependencies pg_lsn pg_mcv_list pg_ndistinct
    pg_node_tree pg_snapshot point polygon record refcursor regclass regcollation regconfig
    regdictionary regnamespace regoper regoperator regproc regprocedure regrole regtype text
    tid time timestamp timestamptz timetz tsmultirange tsquery tsrange tstzmultirange
    tstzrange tsvector txid_snapshot uuid varbit varchar xid xid8 xml serial serial2 serial4
    serial8 smallserial bigserial
""".split())


def run_statements(
    text: str, engine: str, kind: str, sql_mode: str | None = None
) -> frozenset[str] | None:
    """The kind of every statement ``text`` runs, read in ``engine``'s dialect (#2373): on
    PostgreSQL by its parser, nested statements included (``routine_parts``), on MySQL by
    the body reader (``mysql_checks.statement_kinds``). An empty set for a PostgreSQL body
    in a language the parser does not read; None when the text does not read."""
    if engine == "postgresql":
        parts = routine_parts(text, kind)  # type: ignore[arg-type]
        return None if parts is None else parts.statements
    return statement_kinds(
        MySqlRoutine(
            routine_id="proof.routine",
            kind=kind,  # type: ignore[arg-type]
            create_sql=text,
            sql_mode=sql_mode or "",
        )
    )


def operators_and_types(text: str, engine: str, kind: str) -> RoutineParts | None:
    """What ``text`` uses that can run a function without naming it (#2373), as
    :class:`RoutineParts`: on PostgreSQL the operators it applies, the types it names and
    the attributes it reads through a record (``o.name`` can call ``name(o)``), read from
    the parse tree (``routine_parts``). MySQL has no user-defined operators or types, and
    no function is reached through a column, so a MySQL text has none to read. None when
    the text does not read."""
    if engine != "postgresql":
        return RoutineParts(frozenset(), frozenset(), frozenset())
    return routine_parts(text, kind)  # type: ignore[arg-type]
