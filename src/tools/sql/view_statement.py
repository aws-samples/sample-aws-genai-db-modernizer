# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Is a view's ``CREATE`` exactly one view statement? (#2082, ADR-034 Decision 5).

A view's text is collection text: an offline collection file is the customer's to edit,
so its definition is untrusted until it is checked. The views deploy script holds each
carried view's ``CREATE`` as it is, and the customer runs it with ``psql`` or the ``mysql``
client, so a definition that ends its statement early (``SELECT 1; DROP TABLE ...``),
closes a literal early, carries a client command (a ``psql`` ``\\!`` or ``\\i`` line, a
``mysql`` ``system`` or ``source`` line, a ``DELIMITER`` change) or leaves its statement
open (a trailing line comment that swallows the ``;``) would run more than the view.
The rule is the one routines follow (a multi-statement ``CREATE`` is refused, ADR-034
Decision 5): :func:`statement_problem` passes only

- on PostgreSQL, a text the server's own parser (``pgparse``) reads as exactly one
  ``CREATE OR REPLACE VIEW`` of the expected schema and name, that also ends its statement
  (followed by ``SELECT 1;`` it reads as two), with no backslash and no ``psql`` variable
  (``:name``) outside literals, identifiers and dollar quotes;
- on MySQL, a text whose only ``;`` outside quotes is its last character (where the
  ``mysql`` client ends the statement; comments are refused), that starts with the expected
  ``CREATE OR REPLACE SQL SECURITY ... VIEW `db`.`name` (`` header, whose query sqlglot
  reads in the MySQL dialect as exactly one query, with no backslash outside quotes and no
  line that starts with a ``mysql`` client command.

Both engines can read a backslash two ways, by a setting an Aurora parameter group may
change: PostgreSQL's ``standard_conforming_strings`` (off: a backslash escapes inside
``'...'``) and MySQL's ``NO_BACKSLASH_ESCAPES`` (on: a backslash is a plain character).
The client follows the server, so the same text could end its statement elsewhere. The
check reads the text both ways and refuses it (:data:`AMBIGUOUS`) unless both readings
find the same quotes, and the deploy script pins the reading the check assumes.

Anything else is a refusal code, never the text. Pure; linear in the text.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

__all__ = ["AMBIGUOUS", "CLIENT_COMMAND", "NOT_ONE_STATEMENT", "statement_problem"]

#: The refusal of a text that is not exactly one view statement.
NOT_ONE_STATEMENT = "not_one_view_statement"
#: The refusal of a text that holds a psql or mysql client command.
CLIENT_COMMAND = "client_command_in_text"
#: The refusal of a text whose statement boundary depends on how the target reads
#: backslashes (PostgreSQL standard_conforming_strings, MySQL NO_BACKSLASH_ESCAPES).
AMBIGUOUS = "statement_depends_on_backslashes"

#: The mysql client's long-form commands (``help`` lists them): one at a line's start runs.
_MYSQL_COMMANDS = frozenset({
    "?", "charset", "clear", "connect", "delimiter", "edit", "ego", "exit", "go", "help",
    "nopager", "notee", "nowarning", "pager", "print", "prompt", "query_attributes", "quit",
    "rehash", "resetconnection", "source", "ssl_session_data_print", "status", "system", "tee",
    "use", "warnings",
})  # fmt: skip
_FIRST_WORD = re.compile(r"[ \t]*([A-Za-z_?]+)")  # matched at a line start
_DOLLAR_TAG = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")


def _pg_outside(text: str, standard: bool = True) -> Iterator[tuple[int, str]]:
    """Each character of ``text`` outside PostgreSQL literals, quoted identifiers and dollar
    quotes (comments count as outside), with its offset."""
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in "'\"":
            escapes = c == "'" and (not standard or (i > 0 and text[i - 1] in "eE"))
            i += 1
            while i < n:
                if escapes and text[i] == "\\":
                    i += 2
                    continue
                if text[i] == c:
                    if i + 1 < n and text[i + 1] == c:
                        i += 2
                        continue
                    break
                i += 1
            i += 1
            continue
        if c == "$":
            tag = _DOLLAR_TAG.match(text, i)
            if tag:
                end = text.find(tag.group(0), tag.end())
                i = n if end < 0 else end + len(tag.group(0))
                continue
        yield i, c
        i += 1


def _mysql_outside(text: str) -> Iterator[str]:
    """Each character of ``text`` outside MySQL quotes (backslash escapes read)."""
    for _, c in _mysql_positions(text):
        yield c


def _mysql_positions(text: str, backslashes: bool = True) -> Iterator[tuple[int, str]]:
    """Each character of ``text`` outside MySQL quotes, with its offset; an unterminated
    quote runs to the end. ``backslashes`` False reads it as ``NO_BACKSLASH_ESCAPES`` does."""
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in "'\"`":
            i += 1
            while i < n:
                if backslashes and text[i] == "\\" and c != "`":
                    i += 2
                    continue
                if text[i] == c:
                    if i + 1 < n and text[i + 1] == c:
                        i += 2
                        continue
                    break
                i += 1
            i += 1
            continue
        yield i, c
        i += 1


def _has_comment(chars: list[str]) -> bool:
    """A comment outside quotes: a server's view text has none, and one can hide the end of
    the statement from the client (a ``--`` that swallows the ``;``)."""
    joined = "".join(chars)
    return "--" in joined or "/*" in joined


def _pg_problem(text: str, schema: str, name: str) -> str | None:
    import pgparse

    outside = list(_pg_outside(text))
    if outside != list(_pg_outside(text, standard=False)):
        # Where the statement ends would depend on standard_conforming_strings: refused
        # whatever the target's setting (the script pins it on as well).
        return AMBIGUOUS
    if _has_comment([c for _, c in outside]):
        return NOT_ONE_STATEMENT
    if any(c == "\\" for _, c in outside):
        return CLIENT_COMMAND  # a psql meta-command (\! \i \o ...), or text psql reads as one
    for i, c in outside:
        if c != ":" or i + 1 >= len(text):
            continue
        if text[i + 1] == ":" or (i > 0 and text[i - 1] == ":"):
            continue  # a :: cast
        if text[i + 1].isalpha() or text[i + 1] in "_'\"{":
            return CLIENT_COMMAND  # a psql variable (:name, :'name', :"name", :{?name})
    try:
        statements = pgparse.parse(text + "\nSELECT 1;")
    except (pgparse.PGQueryError, UnicodeError, ValueError):
        return NOT_ONE_STATEMENT
    if len(statements) != 2:
        return NOT_ONE_STATEMENT
    view = (statements[0].get("stmt") or {}).get("ViewStmt")
    tail = (statements[1].get("stmt") or {}).get("SelectStmt")
    if not isinstance(view, dict) or not isinstance(tail, dict) or not view.get("replace"):
        return NOT_ONE_STATEMENT
    relation = view.get("view") or {}
    if (relation.get("schemaname"), relation.get("relname")) != (schema, name):
        return NOT_ONE_STATEMENT
    return None


def _mysql_problem(text: str, schema: str, name: str) -> str | None:
    from src.tools.sql.facade import one_query

    positions = list(_mysql_positions(text))
    if positions != list(_mysql_positions(text, backslashes=False)):
        # Where the statement ends would depend on NO_BACKSLASH_ESCAPES: refused whatever the
        # target's sql_mode (the script pins a mode without it as well).
        return AMBIGUOUS
    outside = [c for _, c in positions]
    if any(c == "\\" for c in outside):
        return CLIENT_COMMAND  # a mysql client command (\! \. \G ...)
    if _has_comment(outside) or "#" in outside:
        return NOT_ONE_STATEMENT
    # A line the client reads as a command starts outside quotes; a line inside a string
    # literal (a multi-line value) is data.
    # (only a line break outside quotes starts one).
    starts = [0] + [i + 1 for i, c in positions if c == "\n"]
    for start in starts:
        word = _FIRST_WORD.match(text, start)
        if word and word.group(1).lower() in _MYSQL_COMMANDS:
            return CLIENT_COMMAND
    # The client ends a statement at a ';' outside quotes (comments are refused above): the
    # text must hold exactly one, as its last character, so it ends where it should.
    body_text = text.rstrip()
    semis = [i for i, c in _mysql_positions(body_text) if c == ";"]
    if semis != [len(body_text) - 1]:
        return NOT_ONE_STATEMENT
    statement = body_text[:-1]
    head = re.match(
        r"CREATE OR REPLACE SQL SECURITY (?:DEFINER|INVOKER) VIEW (`(?:[^`]|``)+`)\.(`(?:[^`]|``)+`) \(",
        text,
    )
    quoted = ("`" + schema.replace("`", "``") + "`", "`" + name.replace("`", "``") + "`")
    if head is None or (head.group(1), head.group(2)) != quoted:
        return NOT_ONE_STATEMENT
    body = statement.split(") AS\n", 1)[1] if ") AS\n" in statement else ""
    body = re.sub(r"\s+WITH (?:LOCAL |CASCADED )?CHECK OPTION\s*$", "", body.strip())
    return None if body and one_query(body, "mysql") else NOT_ONE_STATEMENT


def statement_problem(engine: str, text: str, schema: str, name: str) -> str | None:
    """None when ``text`` is exactly one view statement for ``schema.name`` on ``engine``
    (``postgresql``/``aurora_postgresql`` or ``mysql``/``aurora_mysql``); else
    :data:`NOT_ONE_STATEMENT` or :data:`CLIENT_COMMAND` (module docstring)."""
    if "\x00" in text:
        return NOT_ONE_STATEMENT
    if engine in ("postgresql", "aurora_postgresql"):
        return _pg_problem(text, schema, name)
    if engine in ("mysql", "aurora_mysql"):
        return _mysql_problem(text, schema, name)
    return NOT_ONE_STATEMENT
