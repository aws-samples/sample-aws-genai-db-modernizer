# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

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

The table holds each engine's default settings, which a source can change (#1482); the
grammar takes the source's own as :class:`SpanSettings`, read from the collection
(:func:`span_settings_of`): MySQL and MariaDB with ``NO_BACKSLASH_ESCAPES`` in their
``sql_mode`` read ``\\`` as an ordinary character, and PostgreSQL with
``standard_conforming_strings = off`` reads it as an escape in a plain ``'...'`` string
too. With no settings given, a scan reads the settings of the collection being read
(:func:`use_span_settings`), and with none of those each engine's defaults, as before.

MySQL and MariaDB read ``--`` as a comment only when a space or a control character (or
the end of the text) follows it: ``5--1`` is ``5 - -1``. That needs no setting.

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
:func:`keyword_text` is the same text with each quoted identifier's name a neutral one
(:data:`NEUTRAL_NAME`), for a scan that looks for keywords: a name is never one (#1478).
"""

from __future__ import annotations

import re
from bisect import bisect_left
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import cache, lru_cache, partial

from src.shared.sql_comments import block_comment_end, block_comments_nest

__all__ = [
    "COMMENTS_LITERALS_AND_IDENTIFIERS",
    "LITERALS_AND_COMMENTS",
    "NEUTRAL_NAME",
    "MYSQL_COMMENTS_LITERALS_AND_IDENTIFIERS",
    "MYSQL_LITERALS_AND_COMMENTS",
    "POSTGRES_COMMENTS_LITERALS_AND_IDENTIFIERS",
    "POSTGRES_LITERALS_AND_COMMENTS",
    "DEFAULT_SPAN_SETTINGS",
    "QUOTED_IDENTIFIERS",
    "ROUTINE_BODY_OPENER_RE",
    "Grammar",
    "SpanSettings",
    "active_span_settings",
    "backslash_quotes",
    "code_text",
    "comments_literals_and_identifiers_for",
    "keyword_text",
    "literals_and_comments_for",
    "opens_routine_body",
    "quoted_span_end",
    "replace_strings_and_comments",
    "scan_grammar_for",
    "span_dialect",
    "span_settings",
    "span_settings_of",
    "spans",
    "sub_spans",
    "use_span_settings",
    "with_span_settings",
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


@dataclass(frozen=True)
class SpanSettings:
    """The source settings that change how a string is read (#1482). The defaults are
    each engine's own, so ``SpanSettings()`` reads every dialect as before.

    ``no_backslash_escapes``: MySQL's or MariaDB's ``sql_mode`` holds
    ``NO_BACKSLASH_ESCAPES``, so ``\\`` is an ordinary character in a string.
    ``standard_conforming_strings``: PostgreSQL's setting; ``False`` (``off``) makes
    ``\\`` an escape in a plain ``'...'`` string as well as in ``E'...'``.
    ``ansi_quotes``: MySQL's or MariaDB's ``sql_mode`` holds ``ANSI_QUOTES`` (or
    ``ANSI``), so ``"..."`` quotes a name, not a string. Each is read only for its own
    dialect. Redaction never trusts the settings to mask less (``redact``).
    """

    no_backslash_escapes: bool = False
    standard_conforming_strings: bool = True
    ansi_quotes: bool = False


DEFAULT_SPAN_SETTINGS = SpanSettings()

# The settings of the collection being read, for a scan given none (:func:`use_span_settings`).
_ACTIVE: ContextVar[SpanSettings] = ContextVar("span_settings", default=DEFAULT_SPAN_SETTINGS)


def active_span_settings() -> SpanSettings:
    """The settings a scan given none reads: the collection's (:func:`use_span_settings`),
    else each engine's defaults."""
    return _ACTIVE.get()


def use_span_settings(settings: SpanSettings | None) -> None:
    """Read every later scan in this context (thread, task) with ``settings``, ``None``
    being each engine's defaults. A step calls it when it loads a collection
    (``query_facts.remember_collected_facts``), so a scan reads the source's settings."""
    _ACTIVE.set(settings or DEFAULT_SPAN_SETTINGS)


@contextmanager
def with_span_settings(settings: SpanSettings | None) -> Iterator[SpanSettings]:
    """Read every scan inside the block with ``settings`` (``None``: the ones already
    active), then restore the settings that were active before it."""
    token = _ACTIVE.set(_settings(settings))
    try:
        yield _ACTIVE.get()
    finally:
        _ACTIVE.reset(token)


def _settings(settings: SpanSettings | None) -> SpanSettings:
    return _ACTIVE.get() if settings is None else settings


def _setting_on(value: object) -> bool | None:
    """A boolean setting as a server reports it (``on``, ``off``, ``1``, ``true``), or
    ``None`` when it is not one."""
    if isinstance(value, bool):
        return value
    text = str(value if value is not None else "").strip().lower()
    if text in ("on", "true", "1", "yes"):
        return True
    if text in ("off", "false", "0", "no"):
        return False
    return None


def span_settings(source_engine: str | None, settings: Mapping[str, object] | None) -> SpanSettings:
    """The span settings of a source's reported settings (``sql_mode`` for MySQL and
    MariaDB, ``standard_conforming_strings`` for PostgreSQL): each one missing or not
    understood is the engine's default, and another dialect's is not read."""
    if not isinstance(settings, Mapping):
        return DEFAULT_SPAN_SETTINGS
    dialect = span_dialect(source_engine)
    if dialect == "mysql":
        modes = {m.strip().upper() for m in str(settings.get("sql_mode") or "").split(",")}
        return SpanSettings(
            no_backslash_escapes="NO_BACKSLASH_ESCAPES" in modes,
            ansi_quotes=bool(modes & {"ANSI_QUOTES", "ANSI"}),
        )
    if dialect == "postgresql":
        on = _setting_on(settings.get("standard_conforming_strings"))
        return SpanSettings(standard_conforming_strings=on is not False)
    return DEFAULT_SPAN_SETTINGS


def span_settings_of(collector_output: Mapping | None) -> SpanSettings:
    """The span settings of a collection: its ``metadata.source_database.settings``
    read for its source engine. A collection without them (any written before #1482)
    reads each engine's defaults, as before."""
    metadata = (
        (collector_output or {}).get("metadata") if isinstance(collector_output, Mapping) else None
    )
    source = metadata.get("source_database") if isinstance(metadata, Mapping) else None
    if not isinstance(source, Mapping):
        return DEFAULT_SPAN_SETTINGS
    engine = source.get("engine")
    return span_settings(str(getattr(engine, "value", engine) or ""), source.get("settings"))


def _line_end(text: str, start: int) -> int:
    newline = text.find("\n", start)
    return len(text) if newline < 0 else newline


@dataclass(frozen=True)
class _Forms:
    """How one reading of a dialect ends its spans (hashable, so a grammar is cached)."""

    nests: bool  # a block comment nests
    backslash: frozenset[str]  # the quotes (``'``, ``"``) a ``\\`` escapes in
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
    backslash = opener in forms.backslash
    if opener == '"' and identifiers:
        backslash = False
    return quoted_span_end(text, start, opener, backslash)


def is_alternative_quote(opener: str) -> bool:
    """Whether a span opener is an Oracle ``q'<delimiter>`` (or ``nq'``) opener."""
    return len(opener) >= 3 and opener[-2] == "'"


# MySQL and MariaDB read ``--`` as a comment only before a space or a control character
# (or the end of the text, which their lexer reads as one): ``5--1`` is arithmetic.
_MYSQL_LINE_COMMENT = r"--(?=[\x00-\x20\x7f]|\Z)"


def _line_comment(dialect: str) -> str:
    return _MYSQL_LINE_COMMENT if dialect == "mysql" else "--"


def _backslash_quotes(dialect: str, settings: SpanSettings) -> frozenset[str]:
    """The quotes a ``\\`` escapes in, in ``dialect`` read with ``settings``."""
    if dialect == "":
        return frozenset({"'", '"'})
    if dialect == "mysql" and not settings.no_backslash_escapes:
        # With ANSI_QUOTES a ``"..."`` is a name, where a backslash escapes nothing.
        return frozenset({"'"} if settings.ansi_quotes else {"'", '"'})
    if dialect == "postgresql" and not settings.standard_conforming_strings:
        return frozenset({"'"})
    return frozenset()


def backslash_quotes(source_engine: str | None, settings: SpanSettings | None = None) -> str:
    """The quotes (``'``, ``"``) a ``\\`` escapes in, for a known source engine read with
    ``settings`` (by default the active ones): MySQL's and MariaDB's two unless
    ``NO_BACKSLASH_ESCAPES``, PostgreSQL's ``'`` with ``standard_conforming_strings = off``,
    none otherwise (an unknown engine included). For the scanners outside this module
    (``sql_comments``), so they read a string as the grammar does (#1482)."""
    dialect = span_dialect(source_engine)
    if not dialect:
        return ""
    return "".join(sorted(_backslash_quotes(dialect, _settings(settings))))


@cache
def _grammar(dialect: str, identifiers: bool, settings: SpanSettings) -> Grammar:
    """The span grammar of a span ``dialect``; with ``identifiers``, ``"..."``,
    `` `...` `` and ``[...]`` identifiers in every dialect too."""
    parts = [r"/\*", _line_comment(dialect)]
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
    if identifiers or dialect != "mysql" or settings.ansi_quotes:
        named.add('"')
    forms = _Forms(
        nests=block_comments_nest(dialect),
        backslash=_backslash_quotes(dialect, settings),
        bracket_escape=dialect == "sqlserver",
    )
    return Grammar(
        re.compile("|".join(parts)),
        partial(_span_end, forms, identifiers),
        frozenset(named),
    )


def scan_grammar_for(
    source_engine: str | None, brackets: bool = False, settings: SpanSettings | None = None
) -> Grammar:
    """The grammar of ``normalize``'s digest scan, which masks every quoted span.

    It reads the same forms as :func:`literals_and_comments_for` with the digest
    scan's own settings: a backtick quotes in every dialect, ``\\`` escapes only in
    MySQL and MariaDB (never in an unknown dialect) unless ``settings`` say otherwise
    (``NO_BACKSLASH_ESCAPES``, PostgreSQL's ``standard_conforming_strings = off``),
    ``[...]`` is a name only with ``brackets`` (and ``]]`` escapes a ``]``), a dollar
    quote opens in every dialect but MySQL's, and a block comment nests as
    :func:`block_comments_nest` says for the raw engine name (``aurora_postgresql``
    included). ``settings`` default to the collection's (:func:`active_span_settings`).
    """
    return _scan_grammar((source_engine or "").strip().lower(), brackets, _settings(settings))


@cache
def _scan_grammar(engine: str, brackets: bool, settings: SpanSettings) -> Grammar:
    dialect = span_dialect(engine)
    mysql = dialect == "mysql"
    parts = [r"/\*", _line_comment(dialect)]
    if mysql:
        parts.append("#")
    if dialect == "oracle":
        parts.append(_Q_QUOTE_OPENER)
    if dialect == "postgresql":
        parts.append(_ESCAPE_STRING_OPENER)
    parts.append(r"['\"`\[]" if brackets else "['\"`]")
    if not mysql:
        parts.append(_DOLLAR_OPENER)
    backslash = _backslash_quotes(dialect, settings) if dialect else frozenset()
    forms = _Forms(nests=block_comments_nest(engine), backslash=backslash, bracket_escape=True)
    return Grammar(
        re.compile("|".join(parts)),
        partial(_span_end, forms, False),
        frozenset({"`", "[", '"'} if not mysql or settings.ansi_quotes else {"`", "["}),
    )


def literals_and_comments_for(
    source_engine: str | None, settings: SpanSettings | None = None
) -> Grammar:
    """The comment and literal grammar of a source engine (``metadata.source_database.engine``),
    with its own quoted identifiers (MySQL backticks, SQL Server brackets); see the module
    docstring for each dialect's forms. None known reads the forms the old regexes read.
    ``settings`` are the source's (#1482), by default the collection's
    (:func:`active_span_settings`)."""
    return _grammar(span_dialect(source_engine), False, _settings(settings))


def comments_literals_and_identifiers_for(
    source_engine: str | None, settings: SpanSettings | None = None
) -> Grammar:
    """The one-scan comment, literal and quoted-identifier grammar of a source engine:
    :func:`literals_and_comments_for`'s, with ``"..."``, `` `...` `` and ``[...]``
    identifiers whatever the dialect."""
    return _grammar(span_dialect(source_engine), True, _settings(settings))


# Each engine's default settings, whatever the collection's.
LITERALS_AND_COMMENTS = literals_and_comments_for(None, DEFAULT_SPAN_SETTINGS)
MYSQL_LITERALS_AND_COMMENTS = literals_and_comments_for("mysql", DEFAULT_SPAN_SETTINGS)
POSTGRES_LITERALS_AND_COMMENTS = literals_and_comments_for("postgresql", DEFAULT_SPAN_SETTINGS)
COMMENTS_LITERALS_AND_IDENTIFIERS = comments_literals_and_identifiers_for(
    None, DEFAULT_SPAN_SETTINGS
)
MYSQL_COMMENTS_LITERALS_AND_IDENTIFIERS = comments_literals_and_identifiers_for(
    "mysql", DEFAULT_SPAN_SETTINGS
)
POSTGRES_COMMENTS_LITERALS_AND_IDENTIFIERS = comments_literals_and_identifiers_for(
    "postgresql", DEFAULT_SPAN_SETTINGS
)


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


def replace_strings_and_comments(
    text: str,
    source_engine: str | None,
    *,
    string: Callable[[str], str],
    comment: Callable[[str], str],
    settings: SpanSettings | None = None,
) -> str:
    """``text`` with each string literal written ``string(span)`` and each comment
    ``comment(span)``, read in the source engine's forms and settings
    (:func:`literals_and_comments_for`); quoted identifiers are kept. For a scan that
    blanks literals and comments by its own rule, so it reads them as the grammar does
    (#1482)."""
    grammar = literals_and_comments_for(source_engine, settings)
    out: list[str] = []
    last = 0
    for start, end, opener in spans(text, grammar):
        out.append(text[last:start])
        span = text[start:end]
        if opener in ("--", "#", "/*"):
            out.append(comment(span))
        elif opener in grammar.identifiers:
            out.append(span)
        else:
            out.append(string(span))
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


# What :func:`keyword_text` writes inside a quoted identifier: a name that is no keyword in
# any dialect. The quotes stay, so a scan reads a quoted name where one stood, as before.
NEUTRAL_NAME = "quoted_name"
_CLOSERS = {"[": "]"}
# A ``(`` after a quoted name: the name is a function's, and the call is SQL.
_CALL_PAREN_RE = re.compile(r"\s*\(")
_WORD_RE = re.compile(r"\w+")


def _quoted(opener: str, name: str) -> str:
    return f"{opener}{name}{_CLOSERS.get(opener, opener)}"


def keyword_text(
    text: str,
    source_engine: str | None,
    *,
    unquote: bool = False,
    settings: SpanSettings | None = None,
) -> str:
    """:func:`code_text` with each quoted identifier's name :data:`NEUTRAL_NAME`, in its
    quotes (``"where"`` reads ``"quoted_name"``, #1478).

    A keyword used as a quoted name (`` `where` `` in MySQL, ``"where"`` in PostgreSQL or
    Oracle, ``[where]`` in SQL Server) is a name, never SQL, so a fact that looks for a
    keyword reads this text; a fact that needs the names (a table, a ``latitude``
    column, a session key) reads :func:`code_text`. A quoted name followed by ``(`` names
    the function a call runs, so it is kept (`` `FOUND_ROWS`() `` is that call).

    With ``unquote``, each neutral name is written bare (``RETURNING "id"`` reads
    ``RETURNING quoted_name``), for a fact whose pattern expects a bare name (#1547).
    """
    return code_text(
        text, source_engine, neutral_name=NEUTRAL_NAME, unquote=unquote, settings=settings
    )


def code_text(
    text: str,
    source_engine: str | None,
    *,
    neutral_name: str | None = None,
    unquote: bool = False,
    settings: SpanSettings | None = None,
) -> str:
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

    With ``neutral_name``, each quoted identifier holds that token instead of its name
    (:func:`keyword_text`). With ``unquote``, a quoted identifier that is one word is
    written without its quotes (``"status"`` reads ``status``, ``"a"."b"`` reads ``a.b``,
    #1547), for a fact whose pattern expects a bare name next to a keyword; a name that
    is not one word (``"my status"``) keeps its quotes.

    ``settings`` are the source's string settings (:class:`SpanSettings`, #1482), by
    default the collection's.
    """
    text = text or ""
    settings = _settings(settings)
    grammar = literals_and_comments_for(source_engine, settings)
    out: list[str] = []
    last = 0
    for start, end, opener in spans(text, grammar):
        out.append(text[last:start])
        last = end
        if opener in ("--", "/*", "#"):
            out.append(" ")
        elif opener in grammar.identifiers:
            # A quoted name before ``(`` is the function called: kept, as a call.
            if neutral_name is None or _CALL_PAREN_RE.match(text, end) is not None:
                name = text[start + 1 : end - 1]
                is_call = _CALL_PAREN_RE.match(text, end) is not None
                if unquote and not is_call and _WORD_RE.fullmatch(name):
                    out.append(name)
                else:
                    out.append(text[start:end])
            elif unquote:
                out.append(neutral_name)
            else:
                out.append(_quoted(opener, neutral_name))
        elif (
            opener[0] == "$"
            and end - start >= 2 * len(opener)
            # The few pieces before the quote: the code it follows, a comment or two.
            and opens_routine_body("".join(out[-4:]))
        ):
            body = text[start + len(opener) : end - len(opener)]
            out.append(
                " "
                + code_text(
                    body,
                    source_engine,
                    neutral_name=neutral_name,
                    unquote=unquote,
                    settings=settings,
                )
                + " "
            )
        else:
            out.append("?")
    out.append(text[last:])
    return "".join(out)
