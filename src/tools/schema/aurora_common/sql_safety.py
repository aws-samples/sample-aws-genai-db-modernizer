# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

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
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field

from src.contracts.aurora_design_delta import IDENTIFIER

MAX_PREDICATE_LENGTH = 300
MAX_STATEMENT_LENGTH = 1000

_FORBIDDEN_SEQUENCES = ("\n", "\r", "\\", "--", "/*", "*/", ";")


class SqlFragmentError(ValueError):
    """A delta fragment does not match its grammar."""


#: Unicode categories no fragment may hold anywhere, literals included (#2381 review): a
#: control character (NUL cuts a ``psql -f`` line short and leaves a literal open), a
#: format character (zero-width, bidirectional overrides) or a line or paragraph separator.
_UNPRINTABLE_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})


def is_unprintable(character: str) -> bool:
    """Whether ``character`` is one no fragment, and no SQL comment, may carry."""
    return unicodedata.category(character) in _UNPRINTABLE_CATEGORIES


def refuse_unprintable(text: str, what: str) -> None:
    """Raise ``SqlFragmentError`` when ``text`` holds a character ``is_unprintable`` names."""
    if any(is_unprintable(ch) for ch in text):
        raise SqlFragmentError(f"{what}: a control, format or line separator character")


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
    r"|(?P<number>-?[0-9]+(?:\.[0-9]+)?)"
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
    refuse_unprintable(text, "where")
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
    r"::\s*(?P<type>\"[A-Za-z_][A-Za-z0-9_ ]*\"|[A-Za-z_][A-Za-z0-9_]*"
    r"(?:\s+(?:varying|precision|with(?:out)?\s+time\s+zone))?)(?P<arrays>(?:\s*\[\])*)",
    re.IGNORECASE,
)
_STRING = re.compile(r"'(?:[^'\\\n\r]|'')*'")
# The casts pg_get_expr adds to a text column compared with a literal.
_COLUMN_TEXT_CASTS = frozenset({"text", "character varying", "varchar", "bpchar"})
_CAST_OPERAND = re.compile(
    r"(?:\(\s*(?P<pcol>\"?[A-Za-z_][A-Za-z0-9_$]*\"?)\s*\)"
    r"|(?<![A-Za-z0-9_$.\"])(?P<num>-?[0-9]+(?:\.[0-9]+)?)"
    r"|(?<![A-Za-z0-9_$.])(?P<col>\"?[A-Za-z_][A-Za-z0-9_$]*\"?))\s*$"
)


def _strip_casts(segment: str, after_literal: bool, out: list[str], text_columns: set[str]) -> None:
    """Append ``segment`` (text between string literals) to ``out`` without its safe casts."""
    pos = 0
    for match in _CAST.finditer(segment):
        before = "".join(out) + segment[pos : match.start()]
        cast = " ".join(match.group("type").strip('"').lower().split())
        operand = _CAST_OPERAND.search(before)
        on_literal = (after_literal and pos == 0 and not segment[: match.start()].strip()) or (
            operand is not None and operand.group("num") is not None
        )
        name = (operand.group("pcol") or operand.group("col")) if operand else None
        on_text_column = name is not None and name.strip('"').lower() in text_columns
        if on_literal:
            pass  # 'x'::text, 5::integer: the literal takes the column's type anyway
        elif on_text_column and cast in _COLUMN_TEXT_CASTS and not match.group("arrays"):
            pass  # (status)::text on a text column: PostgreSQL's own no-op cast
        else:
            raise SqlFragmentError(
                f"where: the cast {match.group(0).strip()!r} changes the comparison; "
                "only casts on literals and text casts on text columns can be dropped"
            )
        out.append(segment[pos : match.start()])
        pos = match.end()
    out.append(segment[pos:])


def render_source_predicate(
    text: str, columns: list[str], quote, text_columns: Iterable[str] = ()
) -> str:
    """A collector's partial-index predicate, re-rendered under the ``render_predicate`` grammar.

    PostgreSQL reports predicates as ``pg_get_expr`` text, which carries type
    casts (``((status)::text = 'active'::text)``). Only casts that do not
    change the comparison are dropped: a cast on a literal (``'x'::text``,
    ``5::integer``; the literal is then typed by the column it is compared
    with) and the text casts PostgreSQL puts on a column that is itself text
    (``::text``, ``::character varying``, ``::bpchar``; ``text_columns`` names
    the table's text-typed columns). Any other cast, e.g. ``(created_at)::date``,
    ``(amount)::integer`` or ``(views)::text`` on an integer column (which
    compares as text: '10' < '9'), changes the meaning and
    raises ``SqlFragmentError``; the draft then falls back to a full index and
    notes it. The rest must pass the strict predicate grammar, so collector
    text never reaches DDL unchecked.
    """
    refuse_unprintable(text, "where")
    texts = {c.lower() for c in text_columns}
    out: list[str] = []
    pos = 0
    after_literal = False
    for match in _STRING.finditer(text):
        _strip_casts(text[pos : match.start()], after_literal, out, texts)
        out.append(match.group(0))
        pos = match.end()
        after_literal = True
    _strip_casts(text[pos:], after_literal, out, texts)
    return render_predicate("".join(out), columns, quote)


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


# ---------------------------------------------------------------------------
# PostgreSQL expression index keys (#2381)
# ---------------------------------------------------------------------------

MAX_EXPRESSION_LENGTH = 300
_MAX_EXPRESSION_DEPTH = 6

_EXPRESSION_TOKENS = re.compile(
    r"\s*(?:"
    r"(?P<string>'(?:[^'\\\n\r]|'')*')"
    r"|(?P<number>[0-9]+)"
    r"|(?P<quoted>\"[A-Za-z_][A-Za-z0-9_$]*\")"
    r"|(?P<word>[A-Za-z_][A-Za-z0-9_$]*)"
    r"|(?P<cast>::)"
    r"|(?P<punct>[(),])"
    r")"
)
#: The functions an expression key may call, as (fewest, most) arguments and how the DDL
#: spells the name: pg_get_expr's own spelling, ``"left"`` quoted since LEFT is a keyword.
#: Every one is a built-in, immutable function of Aurora PostgreSQL.
_EXPRESSION_FUNCTIONS: dict[str, tuple[int, int, str]] = {
    "lower": (1, 1, "lower"),
    "upper": (1, 1, "upper"),
    "md5": (1, 1, "md5"),
    "left": (2, 2, '"left"'),
    "coalesce": (2, 8, "COALESCE"),
}
#: The casts a literal may keep (``'-1'::integer``, ``'none'::character varying``), as the DDL
#: spells them.
_LITERAL_CASTS = frozenset({"integer", "bigint", "smallint", "text", "character varying"})


@dataclass
class _Node:
    kind: str  # "column", "literal", "call"
    sql: str
    has_column: bool
    column: str | None = None  # the table's spelling, for a bare column


class _ExpressionParser:
    """Recursive descent over one ``pg_get_expr`` key list; see ``render_index_expressions``."""

    def __init__(self, text: str, columns: list[str], quote, text_columns: set[str]):
        self.tokens = self._tokenize(text)
        self.pos = 0
        self.columns = columns
        self.quote = quote
        self.text_columns = text_columns

    @staticmethod
    def _tokenize(text: str) -> list[tuple[str, str]]:
        tokens: list[tuple[str, str]] = []
        pos = 0
        while pos < len(text):
            if not text[pos:].strip():
                break
            match = _EXPRESSION_TOKENS.match(text, pos)
            if match is None or match.end() == pos:
                raise SqlFragmentError("expression: a character outside the supported grammar")
            kind = match.lastgroup or ""
            tokens.append((kind, match.group(kind)))
            pos = match.end()
        return tokens

    def _peek(self) -> tuple[str, str] | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _take(self, value: str | None = None) -> tuple[str, str]:
        token = self._peek()
        if token is None or (value is not None and token[1] != value):
            raise SqlFragmentError("expression: incomplete or unbalanced")
        self.pos += 1
        return token

    def keys(self) -> list[str]:
        keys = [self._key()]
        while self._peek() is not None:
            self._take(",")
            keys.append(self._key())
        return keys

    def _key(self) -> str:
        node = self._expr(0)
        if not node.has_column or node.kind == "column":
            # A bare column is a plain key, which the collector names itself; a key with no
            # column is a constant.
            raise SqlFragmentError("expression: each key must call a function on a column")
        return node.sql

    def _expr(self, depth: int) -> _Node:
        if depth > _MAX_EXPRESSION_DEPTH:
            raise SqlFragmentError("expression: nested too deeply")
        node = self._primary(depth)
        while (token := self._peek()) is not None and token[0] == "cast":
            self._take()
            node = self._cast(node)
        return node

    def _primary(self, depth: int) -> _Node:
        kind, value = self._take()
        if value == "(":
            node = self._expr(depth + 1)
            self._take(")")
            return node
        if kind == "string":
            return _Node("literal", value, False)
        if kind == "number":
            return _Node("literal", value, False)
        if kind in ("word", "quoted"):
            name = value.strip('"')
            nxt = self._peek()
            if nxt is not None and nxt[1] == "(":
                if kind == "quoted" and name != name.lower():
                    # A quoted name is case-sensitive: "LOWER" is another function than the
                    # built-in lower; "left" (pg_get_expr's spelling, LEFT is a keyword) is it.
                    raise SqlFragmentError("expression: a quoted function name not in lower case")
                return self._call(name.lower(), depth)
            column = resolve_column(self.columns, name)
            if column is None:
                raise SqlFragmentError("expression: a name that is not a column of this table")
            return _Node("column", self.quote(column), True, column)
        raise SqlFragmentError("expression: unexpected punctuation")

    def _call(self, name: str, depth: int) -> _Node:
        if name not in _EXPRESSION_FUNCTIONS:
            raise SqlFragmentError("expression: a function outside the supported set")
        fewest, most, spelling = _EXPRESSION_FUNCTIONS[name]
        self._take("(")
        args = [self._expr(depth + 1)]
        while (token := self._peek()) is not None and token[1] == ",":
            self._take()
            args.append(self._expr(depth + 1))
        self._take(")")
        if not fewest <= len(args) <= most:
            raise SqlFragmentError("expression: a function called with the wrong arguments")
        if name == "left" and (
            args[1].kind != "literal" or not re.fullmatch("[0-9]+", args[1].sql)
        ):
            raise SqlFragmentError("expression: left() takes a length")
        return _Node(
            "call",
            f"{spelling}({', '.join(a.sql for a in args)})",
            any(a.has_column for a in args),
        )

    def _cast(self, node: _Node) -> _Node:
        kind, value = self._take()
        if kind != "word":
            raise SqlFragmentError("expression: a cast to a type outside the supported set")
        cast = value.lower()
        if cast == "character":
            self._take("varying")
            cast = "character varying"
        if node.kind == "column":
            if cast in _COLUMN_TEXT_CASTS and str(node.column).lower() in self.text_columns:
                return node  # (email)::text on a text column: PostgreSQL's own no-op cast
            raise SqlFragmentError("expression: a cast that changes a column's type")
        if node.kind == "literal" and cast in _LITERAL_CASTS:
            return _Node("literal", f"{node.sql}::{cast}", False)
        raise SqlFragmentError("expression: a cast outside the supported set")


def render_index_expressions(
    text: str, columns: list[str], quote, text_columns: Iterable[str] = ()
) -> list[str]:
    """A PostgreSQL index's expression keys (``pg_get_expr`` of ``pg_index.indexprs``),
    each validated and re-rendered on its own (#2381).

    Collector text, so nothing of it is pasted: it is tokenized and parsed under a strict
    grammar, and each key is written again from the parse, columns quoted with ``quote``.
    Allowed: a call of ``lower``, ``upper``, ``md5``, ``left`` (a column and a length) or
    ``COALESCE`` on columns of the table and literals; the no-op text casts PostgreSQL puts
    on a text column (``(email)::text``, dropped, ``text_columns`` names those columns);
    and ``integer``, ``bigint``, ``smallint``, ``text`` or ``character varying`` casts on a literal. Every key
    must call a function on a column. Anything else (operators, other functions or casts,
    JSON paths, comments, a second statement) raises ``SqlFragmentError`` with a fixed
    message that quotes none of the text.
    """
    refuse_unprintable(text, "expression")
    text = text.strip()
    if not text:
        raise SqlFragmentError("expression: blank")
    if len(text) > MAX_EXPRESSION_LENGTH:
        raise SqlFragmentError(f"expression: longer than {MAX_EXPRESSION_LENGTH} characters")
    if any(seq in text for seq in _FORBIDDEN_SEQUENCES):
        raise SqlFragmentError("expression: newlines, backslashes, comments or ';'")
    texts = {c.lower() for c in text_columns}
    return _ExpressionParser(text, columns, quote, texts).keys()
