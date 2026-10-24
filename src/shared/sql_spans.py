"""Linear-time scanning of SQL comments, literals and quoted identifiers (#886).

The regexes these replace (``'(?:[^'\\]|\\.|'')*'``, ``\\[[^\\]]*\\]`` and
friends) retry from every opener when the closer is missing, so a long run of
unterminated openers (``'\\'\\'...``, ``[[[...``) rescanned the rest of the text
once per opener: quadratic. :func:`spans` walks the text once, left to right,
and answers exactly what those alternations did: the span that opens first
wins, and an opener with no closer is plain text. Once one kind of opener has
no closer left, every later opener of that kind is skipped without a search
(a closer after a later opener would also be a closer after the earlier one;
for a nested block comment the text is one the engine rejects, and reading the
rest as code keeps the scan linear, as ``sql_comments`` does), so each
character is looked at a bounded number of times.

This is the one span grammar: every pass that blanks comments, string literals
or quoted identifiers reads its spans from here, in the source dialect's own
forms (#1416):

======================  =====  ======  ==========  ======  ======  =========
dialect                 ``#``  nested  ``\\`` esc   ``$$``  ``[]``  `` ` ``
======================  =====  ======  ==========  ======  ======  =========
MySQL, MariaDB          yes    no      yes         no      no      yes
PostgreSQL              no     yes     ``E'..'``   yes     no      no
SQL Server              no     yes     no          no      yes     no
Oracle                  no     no      no          no      no      no
none known              no     no      yes         no      no      no
======================  =====  ======  ==========  ======  ======  =========

Oracle also reads its alternative quoting, ``q'[...]'`` (and the national ``nq'[...]'``):
the opener carries a delimiter, ``[ ( { <`` close with their mirror and any other
printable character with itself, and the string ends at the closing delimiter followed by
a quote (#1451). The delimiter is one of 94 printable characters, so the openers that can
each fail once are a bounded set and the scan stays linear. Other dialects read ``q`` as a name and ``'...'`` as a string, as before.

A doubled quote escapes a quote in every dialect (``''`` and ``""``, a doubled
backtick in MySQL, ``]]`` in SQL Server). MySQL reads ``"..."`` as a string,
every other dialect as an identifier. With no dialect known the grammar is the
common one the old regexes read, unchanged (``test_sql_spans.py`` pins it). An
unterminated nested block comment is plain text, as ``sql_comments`` reads it.

Two grammars per dialect:

- :func:`literals_and_comments_for`: the dialect's comments, strings and its own
  quoted identifiers (MySQL backticks, SQL Server brackets);
- :func:`comments_literals_and_identifiers_for`: the same with ``"..."``,
  `` `...` `` and ``[...]`` identifiers in every dialect, one scan that strips
  comments and literals and unquotes identifiers in source order
  (``query_shape``'s normalized text, #871).

:func:`scan_grammar_for` is the third: the digest scan of ``src/tools/sql/normalize.py``,
which masks every quoted span the same way (backticks in every dialect, ``\\`` only in the
MySQL family, an optional SQL Server ``[...]``) and so reads its forms from here.

:data:`QUOTED_IDENTIFIERS` reads ``"..."``, `` `...` `` and ``[...]`` alone, with no
comment or literal awareness (``query_shape``'s former ``_QUOTED_IDENT_RE``).

:func:`code_text` is the text as the engine runs it; a PostgreSQL ``DO`` block or
``AS $$ ... $$`` routine body is code there, not a string (:func:`opens_routine_body`).
"""

from __future__ import annotations

import re
from bisect import bisect_left
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from functools import cache, lru_cache, partial

from src.shared.sql_comments import block_comment_end, block_comments_nest

__all__ = [
    "COMMENTS_LITERALS_AND_IDENTIFIERS",
    "LITERALS_AND_COMMENTS",
    "MYSQL_COMMENTS_LITERALS_AND_IDENTIFIERS",
    "MYSQL_LITERALS_AND_COMMENTS",
    "POSTGRES_COMMENTS_LITERALS_AND_IDENTIFIERS",
    "POSTGRES_LITERALS_AND_COMMENTS",
    "QUOTED_IDENTIFIERS",
    "ROUTINE_BODY_OPENER_RE",
    "Grammar",
    "code_text",
    "comments_literals_and_identifiers_for",
    "literals_and_comments_for",
    "opens_routine_body",
    "quoted_span_end",
    "scan_grammar_for",
    "span_dialect",
    "spans",
    "sub_spans",
]

_STOPS = {
    (quote, backslash): re.compile(f"[{re.escape(quote)}\\\\]" if backslash else re.escape(quote))
    for quote in ("'", '"', "]")
    for backslash in (True, False)
}


def quoted_span_end(text: str, start: int, quote: str, backslash: bool) -> int:
    """End (exclusive) of the ``quote``-quoted span opening at ``start``, or -1.

    ``quote`` is the closing character (``]`` for a ``[`` span). A doubled quote is
    an escaped quote, and with ``backslash`` so is ``\\`` followed by any character
    (newline included). Same answer as the regex ``'(?:[^'\\]|\\.|'')*'`` (without
    the backslash parts when ``backslash`` is false): when the text ends inside the
    span, the span closes at the first quote of its last doubled pair, as the regex
    does when it backtracks; with no pair it is unterminated.
    """
    stops = _STOPS[quote, backslash]
    k = start + 1
    last_pair = -1
    while True:
        stop = stops.search(text, k)
        if stop is None:
            return last_pair + 1 if last_pair >= 0 else -1
        pos = stop.start()
        if text[pos] == "\\":
            if pos + 1 >= len(text):
                return last_pair + 1 if last_pair >= 0 else -1
            k = pos + 2
        elif text.startswith(quote * 2, pos):
            last_pair = pos
            k = pos + 2
        else:
            return pos + 1


@dataclass(frozen=True)
class Grammar:
    """The openers a scan looks for, how each one's span ends, and which openers
    start a quoted identifier rather than a string (``code_text`` keeps those)."""

    opener: re.Pattern[str]
    end: Callable[[str, int, str], int]
    identifiers: frozenset[str] = field(default=frozenset())


# A PostgreSQL dollar-quoted string opens with ``$$`` or ``$tag$`` (a tag cannot start with
# a digit, so a ``$1`` parameter is not one) and closes at the same ``$tag$``. Not an opener
# inside an identifier (``a$b$``), which PostgreSQL reads as one name.
_DOLLAR_OPENER = r"(?<![\w$])\$(?:[^\W\d]\w*)?\$"
_DOLLAR_TOKEN_RE = re.compile(r"(?=(\$(?:[^\W\d]\w*)?\$))")
# PostgreSQL's escape string, ``E'...'``: the one string there that reads ``\`` as an
# escape. The ``E`` stands alone (``note'x'`` is a name and a string).
_ESCAPE_STRING_OPENER = r"(?<![\w$])[Ee]'"
# Oracle's alternative quoting, ``q'[...]'`` (and the national ``nq'[...]'``): the opener
# carries its delimiter, a printable non-space ASCII character, and the string runs to
# the closing delimiter followed by a quote. ``[ ( { <`` close with their mirror, every
# other delimiter with itself. The ``q`` stands alone (``seq'x'`` is a name and a string).
_Q_QUOTE_OPENER = r"(?<![\w$#])[nN]?[qQ]'[!-~]"
_Q_CLOSERS = {"[": "]", "(": ")", "{": "}", "<": ">"}


@lru_cache(maxsize=2)
def _dollar_tokens(text: str) -> dict[str, list[int]]:
    """Every ``$tag$`` token in ``text`` (overlaps included) with its start offsets,
    built in one pass so that finding a closer is a bisect, never a rescan per tag."""
    found: dict[str, list[int]] = {}
    for match in _DOLLAR_TOKEN_RE.finditer(text):
        found.setdefault(match.group(1), []).append(match.start())
    return found


def _dollar_end(text: str, start: int, opener: str) -> int:
    positions = _dollar_tokens(text).get(opener, [])
    at = bisect_left(positions, start + len(opener))
    return positions[at] + len(opener) if at < len(positions) else -1


# The source engine names each dialect is known by (``metadata.source_database.engine``,
# and the sqlglot dialect names ``facade`` passes).
_DIALECTS = {
    "mysql": "mysql",
    "mariadb": "mysql",
    "postgresql": "postgresql",
    "postgres": "postgresql",
    "sqlserver": "sqlserver",
    "mssql": "sqlserver",
    "tsql": "sqlserver",
    "oracle": "oracle",
}


def span_dialect(source_engine: str | None) -> str:
    """The span dialect of a source engine: ``"mysql"`` (MySQL, MariaDB),
    ``"postgresql"``, ``"sqlserver"``, ``"oracle"``, or ``""`` when none is known."""
    return _DIALECTS.get((source_engine or "").strip().lower(), "")


def _line_end(text: str, start: int) -> int:
    newline = text.find("\n", start)
    return len(text) if newline < 0 else newline


@dataclass(frozen=True)
class _Forms:
    """How one reading of a dialect ends its spans (hashable, so a grammar is cached)."""

    nests: bool  # a block comment nests
    backslash: bool  # ``\\`` escapes in a ``'...'`` or ``"..."`` string
    bracket_escape: bool  # ``]]`` is a ``]`` inside ``[...]``


def _span_end(forms: _Forms, identifiers: bool, text: str, start: int, opener: str) -> int:
    """Where the span ``opener`` opens at ``start`` ends, read the ``forms`` way."""
    if opener in ("--", "#"):
        return _line_end(text, start)
    if opener == "/*":
        return block_comment_end(text, start, forms.nests)
    if opener[0] == "$":
        return _dollar_end(text, start, opener)
    if opener == "[":
        if forms.bracket_escape:
            return quoted_span_end(text, start, "]", backslash=False)
        found = text.find("]", start + 1)
        return found + 1 if found >= 0 else -1
    if opener == "`":
        # A doubled backtick closes one span and opens the next: the same spans.
        found = text.find("`", start + 1)
        return found + 1 if found >= 0 else -1
    if opener in ("E'", "e'"):
        return quoted_span_end(text, start + 1, "'", backslash=True)
    if is_alternative_quote(opener):
        delimiter = opener[-1]
        found = text.find(_Q_CLOSERS.get(delimiter, delimiter) + "'", start + len(opener))
        return found + 2 if found >= 0 else -1
    backslash = forms.backslash
    if opener == '"' and identifiers:
        backslash = False
    return quoted_span_end(text, start, opener, backslash)


def is_alternative_quote(opener: str) -> bool:
    """Whether a span opener is an Oracle ``q'<delimiter>`` (or ``nq'``) opener."""
    return len(opener) >= 3 and opener[-2] == "'"


@cache
def _grammar(dialect: str, identifiers: bool) -> Grammar:
    """The span grammar of a span ``dialect``; with ``identifiers``, ``"..."``,
    `` `...` `` and ``[...]`` identifiers in every dialect too."""
    parts = [r"/\*", "--"]
    if dialect == "mysql":
        parts.append("#")
    if dialect == "oracle":
        parts.append(_Q_QUOTE_OPENER)
    if dialect == "postgresql":
        parts.append(_ESCAPE_STRING_OPENER)
    quotes = "'\""
    if identifiers or dialect == "mysql":
        quotes += "`"
    if identifiers or dialect == "sqlserver":
        quotes += r"\["
    parts.append(f"[{quotes}]")
    if dialect == "postgresql":
        parts.append(_DOLLAR_OPENER)
    named = {"`", "["}
    if identifiers or dialect != "mysql":
        named.add('"')
    forms = _Forms(
        nests=block_comments_nest(dialect),
        backslash=dialect in ("mysql", ""),
        bracket_escape=dialect == "sqlserver",
    )
    return Grammar(
        re.compile("|".join(parts)),
        partial(_span_end, forms, identifiers),
        frozenset(named),
    )


@cache
def scan_grammar_for(source_engine: str | None, brackets: bool = False) -> Grammar:
    """The grammar of ``normalize``'s digest scan, which masks every quoted span.

    It reads the same forms as :func:`literals_and_comments_for` with the digest
    scan's own settings: a backtick quotes in every dialect, ``\\`` escapes only in
    MySQL and MariaDB (never in an unknown dialect), ``[...]`` is a name only with
    ``brackets`` (and ``]]`` escapes a ``]``), a dollar quote opens in every dialect but
    MySQL's, and a block comment nests as :func:`block_comments_nest` says for the raw
    engine name (``aurora_postgresql`` included).
    """
    engine = (source_engine or "").strip().lower()
    dialect = span_dialect(engine)
    mysql = dialect == "mysql"
    parts = [r"/\*", "--"]
    if mysql:
        parts.append("#")
    if dialect == "oracle":
        parts.append(_Q_QUOTE_OPENER)
    if dialect == "postgresql":
        parts.append(_ESCAPE_STRING_OPENER)
    parts.append(r"['\"`\[]" if brackets else "['\"`]")
    if not mysql:
        parts.append(_DOLLAR_OPENER)
    forms = _Forms(nests=block_comments_nest(engine), backslash=mysql, bracket_escape=True)
    return Grammar(
        re.compile("|".join(parts)),
        partial(_span_end, forms, False),
        frozenset({"`", "[", '"'} if not mysql else {"`", "["}),
    )


def literals_and_comments_for(source_engine: str | None) -> Grammar:
    """The comment and literal grammar of a source engine (``metadata.source_database.engine``),
    with its own quoted identifiers (MySQL backticks, SQL Server brackets); see the module
    docstring for each dialect's forms. None known reads the forms the old regexes read."""
    return _grammar(span_dialect(source_engine), False)


def comments_literals_and_identifiers_for(source_engine: str | None) -> Grammar:
    """The one-scan comment, literal and quoted-identifier grammar of a source engine:
    :func:`literals_and_comments_for`'s, with ``"..."``, `` `...` `` and ``[...]``
    identifiers whatever the dialect."""
    return _grammar(span_dialect(source_engine), True)


LITERALS_AND_COMMENTS = literals_and_comments_for(None)
MYSQL_LITERALS_AND_COMMENTS = literals_and_comments_for("mysql")
POSTGRES_LITERALS_AND_COMMENTS = literals_and_comments_for("postgresql")
COMMENTS_LITERALS_AND_IDENTIFIERS = comments_literals_and_identifiers_for(None)
MYSQL_COMMENTS_LITERALS_AND_IDENTIFIERS = comments_literals_and_identifiers_for("mysql")
POSTGRES_COMMENTS_LITERALS_AND_IDENTIFIERS = comments_literals_and_identifiers_for("postgresql")


def _end_quoted_identifiers(text: str, start: int, opener: str) -> int:
    if opener == '"':
        return quoted_span_end(text, start, opener, backslash=False)
    closer = "]" if opener == "[" else "`"
    found = text.find(closer, start + 1)
    return found + 1 if found >= 0 else -1


QUOTED_IDENTIFIERS = Grammar(
    re.compile(r"[\"`\[]"), _end_quoted_identifiers, frozenset({'"', "`", "["})
)


def spans(text: str, grammar: Grammar) -> Iterator[tuple[int, int, str]]:
    """Every ``(start, end, opener)`` span of ``text``, left to right, in linear time."""
    unclosed: set[str] = set()
    i = 0
    while (match := grammar.opener.search(text, i)) is not None:
        start, opener = match.start(), match.group()
        end = -1
        if opener not in unclosed:
            end = grammar.end(text, start, opener)
            if end < 0:
                unclosed.add(opener)
        if end < 0:
            i = start + 1
            continue
        yield start, end, opener
        i = end


def sub_spans(text: str, grammar: Grammar, repl: Callable[[str], str]) -> str:
    """``text`` with each span replaced by ``repl(span_text)``."""
    out: list[str] = []
    last = 0
    for start, end, _ in spans(text, grammar):
        out.append(text[last:start])
        out.append(repl(text[start:end]))
        last = end
    out.append(text[last:])
    return "".join(out)


# A PostgreSQL ``DO $$ ... $$`` block (with an optional ``LANGUAGE x``), or the ``AS $$ ... $$``
# body of a ``CREATE FUNCTION`` / ``CREATE PROCEDURE``, is code that runs, not a string
# literal (#1144, #1416). Matched on the code before the dollar quote.
ROUTINE_BODY_OPENER_RE = re.compile(r"\b(?:do|as)\s+(?:language\s+\w+\s+)?$", re.IGNORECASE)


def opens_routine_body(code_before: str) -> bool:
    """Whether a dollar-quoted string after ``code_before`` (the code just before it,
    comments and strings already blanked) is a ``DO`` block or an ``AS`` routine body."""
    return bool(ROUTINE_BODY_OPENER_RE.search(code_before))


def code_text(text: str, source_engine: str | None) -> str:
    """``text`` as the source engine runs it (#1415): each comment a space, each string
    literal a ``?`` placeholder, each quoted identifier as written.

    The spans are the dialect's (:func:`literals_and_comments_for`): MySQL's and MariaDB's
    ``#`` comments and ``"..."`` strings, PostgreSQL's dollar-quoted and ``E'...'``
    strings, SQL Server's ``[...]`` names, nested block comments in PostgreSQL and SQL
    Server. A literal keeps its place as a value (``status = 'x'`` reads ``status = ?``),
    so a fact that needs one after an operator still sees it, and a keyword inside it is
    never SQL. ``"..."`` names an identifier in every other dialect, and a backtick or
    bracket span always does, so the name is kept (``"latitude"``, `` `session_id` ``).

    A PostgreSQL ``DO`` block or ``AS $$ ... $$`` routine body is SQL the engine runs, so
    it is read as code, the same way, between two spaces (#1416): a ``CALL`` or an
    ``IN (SELECT`` in it is one. Every other dollar-quoted string is a literal, and a
    string inside a body is still a string (``RAISE NOTICE 'call x()'`` calls nothing).
    """
    text = text or ""
    grammar = literals_and_comments_for(source_engine)
    out: list[str] = []
    last = 0
    for start, end, opener in spans(text, grammar):
        out.append(text[last:start])
        last = end
        if opener in ("--", "/*", "#"):
            out.append(" ")
        elif opener in grammar.identifiers:
            out.append(text[start:end])
        elif (
            opener[0] == "$"
            and end - start >= 2 * len(opener)
            # The few pieces before the quote: the code it follows, a comment or two.
            and opens_routine_body("".join(out[-4:]))
        ):
            body = text[start + len(opener) : end - len(opener)]
            out.append(f" {code_text(body, source_engine)} ")
        else:
            out.append("?")
    out.append(text[last:])
    return "".join(out)
