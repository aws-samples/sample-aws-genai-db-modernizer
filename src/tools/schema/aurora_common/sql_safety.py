"""Strict parsing of the SQL fragments a design delta may carry (issue #273).

Delta content is LLM output produced from untrusted collector text (query
text, defaults, object names), so nothing from it is pasted into DDL as
written. This module parses the two fragments that can reach DDL, a partial
index predicate and a legacy ``CREATE INDEX`` string, under strict grammars,
and returns structured parts that the DDL generator re-renders with quoted
identifiers. Anything outside the grammar is rejected with a message the
model can act on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.contracts.aurora_design_delta import IDENTIFIER

MAX_PREDICATE_LENGTH = 300
MAX_STATEMENT_LENGTH = 1000

_FORBIDDEN_SEQUENCES = ("\n", "\r", "\\", "--", "/*", "*/", ";")


class SqlFragmentError(ValueError):
    """A delta fragment does not match its grammar."""


def resolve_column(columns: list[str], name: str) -> str | None:
    """The table's spelling of ``name`` (exact, else a unique case-insensitive match)."""
    if name in columns:
        return name
    matches = [c for c in columns if c.lower() == name.lower()]
    return matches[0] if len(matches) == 1 else None


# ---------------------------------------------------------------------------
# Partial-index predicate
# ---------------------------------------------------------------------------

_PREDICATE_TOKENS = re.compile(
    r"\s*(?:"
    r"(?P<string>'(?:[^'\\\n\r]|'')*')"
    r"|(?P<number>-?\d+(?:\.\d+)?)"
    r"|(?P<quoted>\"[A-Za-z_][A-Za-z0-9_$]*\")"
    r"|(?P<word>[A-Za-z_][A-Za-z0-9_$]*)"
    r"|(?P<op><>|!=|<=|>=|=|<|>)"
    r"|(?P<punct>[(),])"
    r")"
)
_PREDICATE_KEYWORDS = frozenset({"AND", "OR", "NOT", "IS", "NULL", "TRUE", "FALSE", "IN"})


def render_predicate(text: str, columns: list[str], quote) -> str:
    """Validate a partial-index predicate and re-render it with quoted columns.

    Allowed: column names, ``= <> != < <= > >=``, ``IS [NOT] NULL``, ``IN (...)``,
    ``AND``/``OR``/``NOT``, parentheses, numbers, single-quoted strings and
    ``TRUE``/``FALSE``. Every identifier must be a column of the table.
    """
    text = text.strip()
    if not text:
        raise SqlFragmentError("where must not be blank")
    if len(text) > MAX_PREDICATE_LENGTH:
        raise SqlFragmentError(f"where is longer than {MAX_PREDICATE_LENGTH} characters")
    if any(seq in text for seq in _FORBIDDEN_SEQUENCES):
        raise SqlFragmentError("where must not contain newlines, backslashes, comments or ';'")
    out: list[str] = []
    depth = 0
    pos = 0
    while pos < len(text):
        match = _PREDICATE_TOKENS.match(text, pos)
        if match is None or match.end() == pos:
            raise SqlFragmentError(f"where: unexpected input at {text[pos : pos + 20]!r}")
        pos = match.end()
        kind = match.lastgroup
        token = match.group(kind or 0)
        if kind in ("word", "quoted"):
            name = token.strip('"')
            if kind == "word" and name.upper() in _PREDICATE_KEYWORDS:
                out.append(name.upper())
                continue
            column = resolve_column(columns, name)
            if column is None:
                raise SqlFragmentError(f"where: {name!r} is not a column of this table")
            out.append(quote(column))
        elif kind == "punct":
            depth += {"(": 1, ")": -1}.get(token, 0)
            if depth < 0:
                raise SqlFragmentError("where: unbalanced parentheses")
            out.append(token)
        else:
            out.append(token)
    if depth != 0:
        raise SqlFragmentError("where: unbalanced parentheses")
    return " ".join(out)


_CAST = re.compile(
    r"::\s*(?:\"[A-Za-z_][A-Za-z0-9_ ]*\"|[A-Za-z_][A-Za-z0-9_]*"
    r"(?:\s+(?:varying|precision|with(?:out)?\s+time\s+zone))?)(?:\s*\[\])*",
    re.IGNORECASE,
)
_STRING = re.compile(r"'(?:[^'\\\n\r]|'')*'")


def render_source_predicate(text: str, columns: list[str], quote) -> str:
    """A collector's partial-index predicate, re-rendered under the ``render_predicate`` grammar.

    PostgreSQL reports predicates as ``pg_get_expr`` text, which carries type
    casts (``((status)::text = 'active'::text)``). Casts outside string
    literals are dropped (the comparison is the same without them); the rest
    must pass the strict predicate grammar, so collector text never reaches
    DDL unchecked.
    """
    parts: list[str] = []
    pos = 0
    for match in _STRING.finditer(text):
        parts.append(_CAST.sub("", text[pos : match.start()]))
        parts.append(match.group(0))
        pos = match.end()
    parts.append(_CAST.sub("", text[pos:]))
    return render_predicate("".join(parts), columns, quote)


# ---------------------------------------------------------------------------
# Legacy CREATE INDEX strings
# ---------------------------------------------------------------------------

_ID = r'(?:"[A-Za-z_][A-Za-z0-9_$]*"|`[A-Za-z_][A-Za-z0-9_$]*`|[A-Za-z_][A-Za-z0-9_$]*)'
_STATEMENT = re.compile(
    r"\s*CREATE\s+(?P<unique>UNIQUE\s+)?INDEX\s+"
    r"(?P<concurrently>CONCURRENTLY\s+)?(?P<ifne>IF\s+NOT\s+EXISTS\s+)?"
    rf"(?P<name>{_ID})\s+ON\s+(?P<only>ONLY\s+)?"
    rf"(?:(?P<schema>{_ID})\s*\.\s*)?(?P<table>{_ID})\s*"
    r"(?:USING\s+(?P<method>[A-Za-z]+)\s*)?"
    rf"\(\s*(?P<cols>{_ID}(?:\s*,\s*{_ID})*)\s*\)\s*;?\s*",
    re.IGNORECASE,
)


@dataclass
class ParsedIndex:
    index_name: str
    table: str
    schema: str | None
    columns: list[str]
    unique: bool
    method: str | None
    pg_only: list[str] = field(default_factory=list)  # PostgreSQL-only syntax used
    quotes: set[str] = field(default_factory=set)  # quote characters used


def _unquote(identifier: str) -> str:
    return identifier.strip('"`')


def parse_index_statement(statement: str) -> ParsedIndex:
    """Parse a single plain ``CREATE [UNIQUE] INDEX name ON table (col, ...)`` statement.

    The whole string must match: no newlines, backslashes, comments, second
    statements, expressions, ``INCLUDE`` or ``WHERE`` (use the structured form
    for those).
    """
    if len(statement) > MAX_STATEMENT_LENGTH:
        raise SqlFragmentError("index statement is too long")
    body = statement.strip()
    if body.endswith(";"):
        body = body[:-1]
    if any(seq in body for seq in _FORBIDDEN_SEQUENCES):
        raise SqlFragmentError(
            "index statement must be one line with no backslashes, comments or second "
            "statement; use the structured form {index_name, columns, unique}"
        )
    match = _STATEMENT.fullmatch(body)
    if match is None:
        raise SqlFragmentError(
            "not a plain 'CREATE [UNIQUE] INDEX name ON table (col, ...)' statement; "
            "use the structured form {index_name, columns, unique, method, include, where}"
        )
    raw_ids = [match.group("name"), match.group("table")] + re.findall(_ID, match.group("cols"))
    if match.group("schema"):
        raw_ids.append(match.group("schema"))
    quotes = {r[0] for r in raw_ids if r[0] in '"`'}
    pg_only = [
        label
        for label, group in (
            ("CONCURRENTLY", "concurrently"),
            ("IF NOT EXISTS", "ifne"),
            ("ONLY", "only"),
            ("USING", "method"),
        )
        if match.group(group)
    ]
    name = _unquote(match.group("name"))
    if not IDENTIFIER.fullmatch(name):
        raise SqlFragmentError(f"index name {name!r} is not a plain identifier")
    return ParsedIndex(
        index_name=name,
        table=_unquote(match.group("table")),
        schema=_unquote(match.group("schema")) if match.group("schema") else None,
        columns=[_unquote(c) for c in re.findall(_ID, match.group("cols"))],
        unique=bool(match.group("unique")),
        method=match.group("method").lower() if match.group("method") else None,
        pg_only=pg_only,
        quotes=quotes,
    )
