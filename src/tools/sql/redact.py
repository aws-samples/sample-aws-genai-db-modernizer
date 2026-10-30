"""Literal redaction of collected SQL text (#1625).

MySQL (``DIGEST_TEXT``) and PostgreSQL (``pg_stat_statements``) hand the collector
query text the database has already normalised: ``?`` and ``$1`` stand where the
values were. SQL Server keeps the text of one real execution per ``query_hash``,
Oracle's ``SQL_FULLTEXT`` is the text as sent, and a Db2 collection, a Performance
Insights text or a hand-edited file can hold literals too. So every query text is
redacted here, deterministically and in the source dialect, before anything stores
it: the same privacy rule as #387, which stopped the collectors reading table rows.

:func:`redact_literals` (query text) replaces each value with the engine's own kind
of placeholder: ``?`` (MySQL, MariaDB, SQL Server, Db2, an unknown engine), ``:n``
(Oracle) or ``$n`` (PostgreSQL), numbered after the highest the text already holds.
What is a value:

- string literals in the dialect's forms (:mod:`src.shared.sql_spans`: doubled quotes,
  ``N'...'``, ``X'...'`` and the other prefixes, ``E'...'``, Oracle ``q'[...]'``,
  a dollar-quoted constant such as ``$q$Jane$q$``), and the typed date literals ``DATE '...'``, ``TIME '...'``,
  ``TIMESTAMP '...'`` as a whole;
- numbers, and SQL Server's binary (``0x...``) and money (``$19.99``) literals.

A string written with a prefix keeps its form, so it reads as the literal it was: an
``X'...'`` or ``B'...'`` binary or bit string is zeroed (``X'00'``; sqlglot reads no
``X'...'``), and any other (``N'...'``, Db2's ``G'...'``, ``U&'...'``, a MySQL charset
introducer) and every other quote form (MySQL's ``"..."``, ``E'...'``, ``q'[...]'``)
becomes ``'?'`` in its own quotes (``N'?'``).

What is kept, because it is structure the routing parser reads and holds no value:

- a number that is a row limit or offset (``TOP``, ``LIMIT``, ``OFFSET``, ``FETCH
  FIRST``/``NEXT``, ``ROWNUM <=``, ``n ROWS``, ``n PERCENT``), a lock wait (``FOR
  UPDATE WAIT 5``), a sample size (``SAMPLE (10)``, ``SEED (42)``), an ``NTILE`` count,
  a window frame bound, an ``ORDER BY``/``GROUP BY`` position, a type's size
  (``varchar(40)``, ``decimal(10, 2)``), ``COUNT(1)``, a lone ``SELECT 1`` and a query
  hint's number. A kept number is a size, never a value: an integer of at most nine
  digits (a type size at most five; ``SELECT n`` and ``COUNT(n)`` only 0 or 1), so a
  card number in any of those places is redacted;
- a string with no letter or digit (``'%'``, ``''``, ``','``), a column alias
  (``AS 'Total'``), a JSON path (``'$.a.b'``) and a date function's date part or
  format (``date_trunc('day', ...)``, ``TO_CHAR(d, 'YYYY-MM')``);
- the shape of a pattern: a ``LIKE``/``ILIKE``/``SIMILAR TO``/regular-expression
  operand, a string with a ``%`` wildcard and a ``REGEXP_*`` argument keep their
  quotes, wildcards and punctuation, with each run of letters and digits written
  ``x`` and an e-mail address ``?`` (``'%Jane Doe%'`` becomes ``'%x x%'``,
  ``'%jane@example.com%'`` becomes ``'%?%'``), so a leading wildcard, a prefix or a
  word search reads as it did;
- code: a PostgreSQL dollar-quoted ``DO`` block or routine body (and, inside one, a
  dollar-quoted statement) and the dynamic SQL string of ``EXEC``,
  ``EXECUTE [IMMEDIATE]`` or ``sp_executesql`` are redacted as SQL in their own right,
  and keep their quotes (the write gate reads them as code); inside a PostgreSQL body,
  a string that is itself a statement is code too, and any other becomes ``'?'``;
- names: quoted identifiers are never touched; neither are bind variables
  (``@p1``, ``:id``, ``$1``, ``?``).

Comments are not values, but they can hold one (:func:`redact_comment`, which also
redacts each query tag read from a comment): an e-mail address (``%40``-encoded too)
and a run of nine or more digits (a card, account or phone number) become ``?``
(:func:`scrub_comment`), and in a comment that is not a sqlcommenter tag every run
of text between two quotes becomes ``?``, whether or not the quotes pair as a string
(an apostrophe, ``don't``, would otherwise pair with a value's opening quote), so
sqlcommenter keys and values and endpoint names stay readable. Plain words are kept: a name written in a comment is not found. A line
comment that runs to the end of the text is the rest of the statement on the SQL
Server and Oracle collections, which put each statement on one line, so its content
is redacted as SQL. An unterminated string (a text cut at a length cap) keeps its
opening quote and loses the rest.

:func:`redact_definition` does the same for a view, procedure or trigger definition,
with two differences: a string keeps its own quotes and becomes ``'?'`` (a gap rule
reads the quote form, ``relational_gaps``), and numbers are kept: in code they are
sizes, flags and loop bounds far more often than values.

Redaction fails closed on the source's string settings (#1482). Whether a ``\\``
escapes a quote depends on a setting (MySQL's ``NO_BACKSLASH_ESCAPES``, PostgreSQL's
``standard_conforming_strings``), and the one a statement ran under can differ from the
one the collection reports: an application's session sets its own ``sql_mode``, a
routine keeps the mode it was created with. So a text is read both ways, and every
character either reading puts inside a string or a comment is masked: where the two
readings agree on a span, it is redacted as that span; where they disagree, the whole
run they disagree on is one value (``?``, or ``'?'`` in a definition). The reported
settings (``sql_spans.SpanSettings``, the collection's own for a whole collection) only
choose which reading names a span both readings agree on; they never shrink what is
masked.

Text the database already normalised has nothing left to replace and comes back
unchanged, and redacting redacted text changes nothing (``redact(redact(t)) ==
redact(t)``). :func:`src.tools.sql.normalize.literal_shape` is not reused for the
stored text: it upper-cases names, drops comments and collapses ``IN`` lists, which
is right for grouping variants and wrong for a text the parser reads again.
"""

from __future__ import annotations

import re
import string
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, replace
from threading import Lock
from typing import Any

from src.shared.sql_comments import block_comments_nest
from src.shared.sql_spans import (
    Grammar,
    SpanSettings,
    active_span_settings,
    is_alternative_quote,
    literals_and_comments_for,
    opens_routine_body,
    span_dialect,
    span_settings,
    span_settings_of,
    spans,
    with_span_settings,
)

__all__ = [
    "collector_output_engine",
    "redact_collector_output",
    "redact_comment",
    "redact_definition",
    "redact_literals",
    "redact_raw_collection",
    "raw_collection_settings",
    "redact_values",
    "scrub_comment",
]

# Anything that could be a literal or a comment: text with none comes back as it is.
_MAYBE_LITERAL_RE = re.compile(r"['\"\d$#]|--|/\*")

# What can follow a number token: ``.digits`` fragments, each with its own exponent.
# The token takes all of them, so no digit of a literal survives beside a placeholder
# (``0x1F.1``, ``1.5e3.2``); a placeholder never starts a second number (#1715).
_NUMBER_TAIL = r"(?:\.\d+(?:[eE][+-]?\d+)?[fFdD]?)*"
_START = r"(?<![\w$#:.@])"


def _digits(digit: str, *, separators: bool) -> str:
    return rf"{digit}+(?:_{digit}+)*" if separators else rf"{digit}+"


def _token(body: str, *, strict: bool) -> re.Pattern[str]:
    """``body`` as a token. Strict (MySQL): it ends at a non-word. Otherwise the whole
    token is taken first, and a glued word is left to the text after it; where a ``$`` or
    ``#`` follows, the shorter strict reading main always had applies (the number before
    the ``.``), so no digit that was redacted before is kept."""
    if strict:
        return re.compile(_START + body + r"(?![\w$#])", re.UNICODE)
    return re.compile(_START + "(?:(?>" + body + r")(?![$#])|" + body + r"(?![\w$#]))", re.UNICODE)


def _number_patterns(dialect: str) -> tuple[re.Pattern[str], ...]:
    """The number, hex, binary and octal token patterns of one dialect.

    MySQL keeps the strict reading: a number glued to a word (``1_000``, ``4abc``) is a
    name there, since an identifier may start with digits, and ``0o17`` is no literal.

    Every other engine reads a number glued to a word as a number plus an alias
    (SQL Server, Db2, Oracle and PostgreSQL 14 and earlier), so the numeric prefix is
    the literal and the digits of the alias after it would leak: the token ends where
    the number does (``4111111111111111abc`` is a placeholder and ``abc``) and takes ``_`` digit groups
    (``4111_1111_1111_1111``). PostgreSQL 16 reads those groups, ``0o17``, ``0b101`` and
    ``0xFFFF_FFFF`` as one literal. Redacting them on every PostgreSQL version, and on
    the engines with no such literal, only drops an alias, so it is safe (#1715).
    """
    strict = dialect == "mysql"
    sep = not strict
    dec = _digits(r"\d", separators=sep)
    num = (
        rf"(?:{dec}(?:\.{dec})*\.?|\.{dec}(?:\.{dec})*)(?:[eE][+-]?{dec})?" r"(?:[fFdD](?!\w))?"
        if sep
        else r"(?:\d+(?:\.\d+)*\.?|\.\d+(?:\.\d+)*)(?:[eE][+-]?\d+)?[fFdD]?"
    )
    tail = rf"(?:\.{dec}(?:[eE][+-]?{dec})?(?:[fFdD](?!\w))?)*" if sep else _NUMBER_TAIL

    def token(body: str) -> re.Pattern[str]:
        return _token(body, strict=strict)

    hexa = rf"0[xX]{_digits('[0-9A-Fa-f]', separators=sep)}{tail}"
    # Any digit, not only the valid ones: ``0b102`` reads as ``0 AS b102`` on the engines
    # that glue, and a digit left behind next to the placeholder would be redacted by a
    # second pass.
    # (MySQL's ``0b`` is lower case only: ``0B101`` is a name there.)
    binary = rf"0[bB]{_digits(r'\d', separators=True)}{tail}" if sep else rf"0b[01]+{tail}"
    octal = rf"0[oO]{_digits(r'\d', separators=sep)}{tail}"
    patterns = [token(num + tail), token(hexa), token(binary)]
    if not strict:
        patterns.append(token(octal))
    return tuple(patterns)


_NUMBER_PATTERNS = {
    dialect: _number_patterns(dialect)
    for dialect in ("mysql", "postgresql", "sqlserver", "oracle", "db2")
}

# SQL Server's money literal (``$1984.37``): the ``$`` is part of the value. It ends like a
# number token (``$1.5x`` is ``?`` and a word; ``$4111_1111`` takes its groups).
_MONEY_RE = _token(
    r"\$\s?[+-]?(?:\d+(?:_\d+)*(?:\.(?:\d+(?:_\d+)*)?)?|\.\d+(?:_\d+)*)(?:[eE][+-]?\d+(?:_\d+)*)?"
    + r"(?:\.\d+(?:_\d+)*(?:[eE][+-]?\d+(?:_\d+)*)?(?:[fFdD](?!\w))?)*",
    strict=False,
)
# A kept number is a size, never a value: at most this many digits (a card, account
# or phone number is longer), a type size at most five, ``SELECT n`` and ``COUNT(n)``
# only 0 or 1.
_MAX_KEPT_DIGITS = 9
_MAX_TYPE_SIZE_DIGITS = 5
_SMALL_INTEGER_RE = re.compile(r"\d+")
# A number kept for what comes before it (read on the code with literals blanked):
# a row limit, offset, lock wait, sample size, bucket count or hint.
_KEEP_BEFORE_RE = re.compile(
    r"(?:\b(?:TOP|LIMIT|OFFSET|FETCH\s+(?:FIRST|NEXT)|MAXDOP|MAXRECURSION|FAST"
    r"|QUERYTRACEON|WAIT|ROWNUM\s*(?:<=?|=))\s*\(?"
    r"|\b(?:SAMPLE(?:\s+BLOCK)?|SEED|NTILE)\s*\("
    r"|\bLIMIT\s+\d+\s*,)\s*$",
    re.IGNORECASE,
)
# ... a type's size.
_TYPE_SIZE_BEFORE_RE = re.compile(
    r"\b(?:N?VARCHAR2?|N?CHAR|VARGRAPHIC|GRAPHIC|VARBINARY|BINARY|DEC(?:IMAL)?|NUMERIC"
    r"|NUMBER|DECFLOAT|FLOAT|REAL|DATETIME2|DATETIMEOFFSET|TIME|TIMESTAMP|RAW|BIT|N?CLOB"
    r"|BLOB|DBCLOB|UROWID|INTERVAL\s+\w+|TO\s+SECOND)\s*\(\s*(?:\d+\s*,\s*)?$",
    re.IGNORECASE,
)
# ... and for what comes after it.
_KEEP_AFTER_RE = re.compile(r"\s*\)?\s*(?:ROWS?|PERCENT|PRECEDING|FOLLOWING)\b", re.IGNORECASE)
# ``COUNT(1)`` and ``SELECT 1``: the number must stand alone there.
_ALONE_BEFORE_RE = re.compile(r"(?:\bCOUNT\s*\(|\bSELECT\s+(?:DISTINCT\s+)?)\s*$", re.IGNORECASE)
_COUNT_ALONE_AFTER_RE = re.compile(r"\s*\)")
_SELECT_ALONE_AFTER_RE = re.compile(r"\s*(?:\bFROM\b|\bUNION\b|\)|;|$)", re.IGNORECASE)
_COUNT_BEFORE_RE = re.compile(r"\bCOUNT\s*\(\s*$", re.IGNORECASE)
# ``ORDER BY 1, 2`` / ``GROUP BY 2``: positions, structure (the rule ``literal_shape`` keeps).
_ORDER_ITEM = r"[\w.\"$#\[\]]+(?:\s+(?:ASC|DESC))?(?:\s+NULLS\s+(?:FIRST|LAST))?"
_ORDERING_RE = re.compile(
    r"\b(?:ORDER|GROUP)\s+(?:SIBLINGS\s+)?BY\s+"
    + _ORDER_ITEM
    + r"(?:\s*,\s*"
    + _ORDER_ITEM
    + r")*",
    re.IGNORECASE,
)

# A string's prefix, written right against its quote: national, hex, bit, Db2 graphic
# and Unicode forms, a MySQL charset introducer.
_PREFIX_RE = re.compile(r"(?<![\w$#@])(?:N|X|B|BX|GX|UX|G|U&|_[A-Za-z0-9]+)$", re.IGNORECASE)
# How many output pieces back a typed literal's keyword is looked for (it sits in the
# code piece just before the literal, or a comment or two before that).
_TYPED_TAIL_PIECES = 4
_TYPED_LITERAL_RE = re.compile(r"\b(?:DATE|TIME|TIMESTAMP)\s*$", re.IGNORECASE)
_ALIAS_RE = re.compile(r"\bAS\s*$", re.IGNORECASE)
_DYNAMIC_SQL_RE = re.compile(
    r"\b(?:EXEC(?:UTE)?(?:\s+IMMEDIATE)?|SP_EXECUTESQL)\s*\(?\s*$", re.IGNORECASE
)
# A pattern operand: right after a pattern operator, or after a concatenation that
# started there (``LIKE '%' + @t + 'abc'``).
_STOP_WORDS = (
    "AND|OR|NOT|WHEN|THEN|ELSE|END|ON|WHERE|AS|IS|IN|BETWEEN|ESCAPE|SELECT|FROM|SET|"
    "VALUES|HAVING|GROUP|ORDER|LIMIT|UNION|JOIN|CASE"
)
_PATTERN_CONTEXT_RE = re.compile(
    r"(?:\b(?:NOT\s+)?(?:I?LIKE|SIMILAR\s+TO|REGEXP|RLIKE)|!?~\*?)"
    r"(?>\s+|\+|\|\||\(|,|\?|(?!(?:" + _STOP_WORDS + r")\b)[@:$#]?[\w.]++)*+$",
    re.IGNORECASE,
)
_PATTERN_FUNCTIONS = frozenset(
    {
        "regexp_like",
        "regexp_substr",
        "regexp_instr",
        "regexp_replace",
        "regexp_count",
        "regexp_matches",
        "regexp_split_to_table",
        "regexp_split_to_array",
        "patindex",
    }
)
# A JSON path (``'$.a.b'``, ``'lax $[0]'``): where a value sits, never the value.
_JSON_PATH_RE = re.compile(
    r"^\s*(?:(?:lax|strict)\s+)?\$(?:[.\[][\w.\[\]*\"]*)?\s*$", re.IGNORECASE
)
# A date part or a date format of a date function (``date_trunc('day', ...)``,
# ``TO_CHAR(d, 'YYYY-MM')``): how a date is cut or written, never a value.
_DATE_FUNCTIONS = frozenset(
    {
        "date_trunc",
        "date_part",
        "date_bin",
        "trunc",
        "round",
        "to_char",
        "to_date",
        "to_timestamp",
        "date_format",
        "str_to_date",
        "strftime",
        "format",
        "datename",
        "datepart",
        "dateadd",
        "datediff",
        "timestampadd",
        "timestampdiff",
        "extract",
    }
)
_DATE_PART = (
    r"(?:MICROSECONDS?|MILLISECONDS?|SECONDS?|MINUTES?|HOURS?|DAYS?|WEEKS?|MONTHS?|QUARTERS?"
    r"|YEARS?|DECADES?|CENTURY|CENTURIES|MILLENNIUM|MILLENNIA|DOW|DOY|EPOCH|ISODOW|ISOYEAR"
    r"|YYYY|YYY|YY|Y|IYYY|IY|RRRR|RR|MONTH|MON|MM|DDD|DD|DY|D|HH24|HH12|HH|MI|SS|SSSSS|FF\d?"
    r"|AM|PM|A\.M\.|P\.M\.|Q|WW|IW|W|J|TZH|TZM|TZR|TZD|SYYYY|FM|FX|TH|SP|%[A-Za-z])"
)
_DATE_FORMAT_RE = re.compile(
    r"^\s*" + _DATE_PART + r"(?:[\s\-/:.,_T]*" + _DATE_PART + r")*[\s\-/:.,]*$", re.IGNORECASE
)
# A ``%``-token format (MySQL's ``DATE_FORMAT``, ``strftime``): its digits are text.
_PERCENT_FORMAT_RE = re.compile(r"^(?=.*%[A-Za-z])(?:%[A-Za-z]|[\s\-/:.,_T0-9])+$")
_FUNCTION_NAME_RE = re.compile(r"([A-Za-z_][\w$#]*)\s*$")
_WORD_RUN_RE = re.compile(r"[^\W_]+")
# The quote's context is read from at most this many characters before it.
_CONTEXT = 200

# Comment values: an e-mail address (sqlcommenter URL-encodes the ``@``) and a run of
# nine or more digits, single spaces or dashes allowed between them.
# Both are linear in the text: every quantifier is bounded or possessive, and a digit
# run is found once, as a whole, then cut into its single-separator parts.
_EMAIL_RE = re.compile(r"[\w.+-]{1,64}(?:@|%40)[\w-]{1,63}(?:\.[\w-]{1,63}){1,8}", re.UNICODE)
_DIGIT_RUN_RE = re.compile(r"(?<![\w])\d[\d -]*+")
_DIGIT_PART_RE = re.compile(r"\d(?:[ -]?\d)*+")


def _long_digit_runs(text: str) -> str:
    out: list[str] = []
    last = 0
    for run in _DIGIT_RUN_RE.finditer(text):
        for part in _DIGIT_PART_RE.finditer(text, run.start(), run.end()):
            end = part.end()
            digits = sum(ch.isdigit() for ch in part.group())
            # The run must not go on as a word (``123456789abc`` is a name).
            if digits >= 9 and not (end < len(text) and (text[end].isalnum() or text[end] == "_")):
                out.append(text[last : part.start()])
                out.append("?")
                last = end
    out.append(text[last:])
    return "".join(out)


def scrub_comment(text: str) -> str:
    """``text`` (a comment, or a tag read from one) with each e-mail address and each
    run of nine or more digits replaced by ``?``. Idempotent, and linear in the text."""
    if "@" not in text and "%40" not in text and not any(c.isdigit() for c in text):
        return text
    if "@" in text or "%40" in text:
        text = _EMAIL_RE.sub("?", text)
    return _long_digit_runs(text)


def redact_literals(
    text: str, source_engine: str | None, settings: SpanSettings | None = None
) -> str:
    """Query text with its literal values replaced by placeholders (module docstring).
    ``settings`` are the source's string settings, by default the active ones."""
    if not text or not _MAYBE_LITERAL_RE.search(text):
        return text
    return _redacted(text, _engine(source_engine), "query", settings)


def redact_definition(
    text: str, source_engine: str | None, settings: SpanSettings | None = None
) -> str:
    """A view, procedure or trigger definition with each string literal's value
    replaced by ``?`` inside its own quotes; numbers are kept (module docstring).
    ``settings`` are the source's string settings, by default the active ones."""
    if not text or not _MAYBE_LITERAL_RE.search(text):
        return text
    return _redacted(text, _engine(source_engine), "definition", settings)


def redact_comment(text: str, source_engine: str | None) -> str:
    """A comment's text, or a query tag read from one (``QueryPattern.query_tags``).

    A sqlcommenter tag, a comment that is nothing but comma-separated ``key='value'``
    pairs with URL-encoded keys (the sqlcommenter specification), keeps its keys and values, which name code, less any e-mail address or long digit
    run (:func:`scrub_comment`); in any other comment, every run of text between two
    quotes is written ``?`` too (``don't 'Jane'`` becomes ``don'?'?'``). Idempotent.
    """
    if not text:
        return text
    if _SQLCOMMENTER_RE.fullmatch(text.strip()):
        return scrub_comment(text)
    return scrub_comment(_quoted_runs_redacted(text))


_QUOTE_RE = re.compile(r"['\"]")
# A sqlcommenter comment: only ``key='value'`` pairs, comma-separated, each key
# URL-encoded (no space, quote or ``:``) and each ``'`` in a value escaped as ``\'``.
_SQLCOMMENTER_PAIR = r"[A-Za-z0-9_.~%-]+='(?:[^'\\]|\\.)*+'"
_SQLCOMMENTER_RE = re.compile(_SQLCOMMENTER_PAIR + r"(?:," + _SQLCOMMENTER_PAIR + r")*+")


def _quoted_runs_redacted(text: str) -> str:
    """``text`` with every run between two quotes that holds a letter or digit written
    ``?``. Every run, not only a well-formed string: an apostrophe (``don't``) would
    pair with a value's opening quote and leave the value out of the string."""
    quotes = [m.start() for m in _QUOTE_RE.finditer(text)]
    if len(quotes) < 2:
        return text
    out = [text[: quotes[0] + 1]]
    for here, there in zip(quotes, quotes[1:], strict=False):
        run = text[here + 1 : there]
        out.append("?" if any(ch.isalnum() for ch in run) else run)
        out.append(text[there])
    out.append(text[quotes[-1] + 1 :])
    return "".join(out)


def _engine(source_engine: str | None) -> str:
    return (source_engine or "").strip().lower()


class _SizeBoundCache:
    """The last redactions, by text, bounded by the characters they hold (collected
    text included), not by their count: a long-lived process keeps at most
    ``max_chars`` of it. A text longer than ``max_chars // 8`` is not kept."""

    def __init__(self, max_chars: int) -> None:
        self.max_chars = max_chars
        # (text, engine, mode, settings): the text first, the size counted on it.
        self._items: OrderedDict[tuple[Any, ...], str] = OrderedDict()
        self._chars = 0
        self._lock = Lock()

    def get(self, key: tuple[Any, ...]) -> str | None:
        with self._lock:
            value = self._items.get(key)
            if value is not None:
                self._items.move_to_end(key)
            return value

    def put(self, key: tuple[Any, ...], value: str) -> None:
        size = len(key[0]) + len(value)
        if size > self.max_chars // 8:
            return
        with self._lock:
            if key in self._items:
                return
            self._items[key] = value
            self._chars += size
            while self._chars > self.max_chars:
                (text, *_), old = self._items.popitem(last=False)
                self._chars -= len(text) + len(old)

    @property
    def chars(self) -> int:
        return self._chars


_CACHE = _SizeBoundCache(max_chars=8_000_000)
# The most passes a text is redacted again to reach a fixed point (``_redacted``).
_MAX_PASSES = 4


def _redacted(text: str, engine: str, mode: str, settings: SpanSettings | None) -> str:
    """One text redacted, cached (:class:`_SizeBoundCache`): a job reads its collector
    output many times, and every read redacts it (:func:`redact_collector_output`)."""
    settings = settings or active_span_settings()
    key = (text, engine, mode, settings)
    cached = _CACHE.get(key)
    if cached is None:
        cached = _redacted_uncached(text, engine, mode, settings)
        _CACHE.put(key, cached)
    return cached


def _redacted_uncached(text: str, engine: str, mode: str, settings: SpanSettings) -> str:
    """One text redacted. Where the readings disagreed, a tail was cut or a comment
    rewritten, the text is redacted again until nothing changes, so redacting stays
    idempotent even where masking one run exposes another (a text cut inside nested
    comments). Each pass only masks more; the bound is a guard. Otherwise the
    redaction is the one reading's, which is idempotent, so no pass confirms it."""
    redactor = _Redactor(engine, mode, settings)
    out = redactor.redact(text)
    for _ in range(_MAX_PASSES):
        if not redactor.disagreed:
            break
        again = redactor.redact(out)
        if again == out:
            break
        out = again
    return out


@dataclass
class _Numbering:
    """The next ``:n`` / ``$n`` placeholder of one text (Oracle, PostgreSQL)."""

    style: str  # "?", ":" or "$"
    next: int = 1

    def take(self) -> str:
        if self.style == "?":
            return "?"
        out = f"{self.style}{self.next}"
        self.next += 1
        return out


_EXISTING_BIND_RE = {
    ":": re.compile(r"(?<![\w:]):(\d+)\b"),
    "$": re.compile(r"(?<![\w$])\$(\d+)\b"),
}


class _Redactor:
    """One redaction. ``mode`` is ``"query"`` (placeholders, numbers too) or
    ``"definition"`` (strings become ``'?'`` in their own quotes, numbers are kept)."""

    def __init__(self, engine: str, mode: str, settings: SpanSettings | None = None) -> None:
        self.engine = engine
        self.dialect = span_dialect(engine)
        # ANSI_QUOTES only ever makes a ``"..."`` a name, which is kept: never trusted,
        # so a MySQL ``"..."`` is a string here whatever the source reports.
        self.settings = replace(settings or active_span_settings(), ansi_quotes=False)
        self.grammar = literals_and_comments_for(engine, self.settings)
        # The other reading of the one setting that moves a string's end (module
        # docstring): masked too, whatever the collection reports.
        other = _other_reading(self.dialect, self.settings)
        self.other = literals_and_comments_for(engine, other) if other is not None else None
        # Whether the last redaction masked a run the readings disagree on, or a cut
        # text's tail: then ``_redacted`` confirms it with another pass.
        self.disagreed = False
        self.mode = mode
        self.quoted = mode != "query"
        self.style = {"oracle": ":", "postgresql": "$"}.get(self.dialect, "?")

    def redact(self, text: str) -> str:
        self.disagreed = False
        masked = _blanked(text, self.grammar)
        numbering = _Numbering(self.style)
        pattern = _EXISTING_BIND_RE.get(self.style)
        if pattern is not None:
            numbering.next = max((int(m.group(1)) for m in pattern.finditer(masked)), default=0) + 1
        # A body's statement test reads the text with the same settings.
        with with_span_settings(self.settings):
            return self._redact(text, numbering, body=False)

    # -- one text (or a body / dynamic SQL inside one), left to right ----------------

    def _found(self, text: str, grammar: Grammar) -> list[tuple[int, int, str]]:
        """The spans of ``text`` one reading finds. A quote or ``/*`` left in the code
        has no closer: the text was cut inside a string or a comment, which then runs to
        the end."""
        found = list(spans(text, grammar))
        cut = _unterminated(_blanked(text, grammar, found), self.dialect, text, found)
        if cut is not None:
            # A cut text is confirmed by another pass (``_redacted``): masking its tail
            # can leave a form the next reading cuts again.
            self.disagreed = True
            if cut[2] == "/*" and self._ambiguous_cut(text, cut[0], found, grammar):
                cut = (cut[0], cut[1], _DISAGREED)
            found = [s for s in found if s[1] <= cut[0]] + [cut]
        return found

    def _ambiguous_cut(
        self, text: str, start: int, found: list[tuple[int, int, str]], grammar: Grammar
    ) -> bool:
        """Whether a text cut inside a nested block comment reads two ways: as a comment
        to the end (whose rest is redacted as SQL, where a nested ``*/`` can close it),
        and with that ``/*`` as text, where a string follows it. Then the rest is one
        value, masked whole (fails closed, #1482)."""
        if not block_comments_nest(self.engine) or "*/" not in text[start + 2 :]:
            return False
        return any(
            s > start and o not in ("--", "#", "/*") and o not in grammar.identifiers
            for s, _e, o in found
        )

    def _redact(self, text: str, numbering: _Numbering, *, body: bool) -> str:
        found = self._found(text, self.grammar)
        # The readings differ only on a backslash: without one there is one reading.
        if self.other is not None and "\\" in text:
            other = self._found(text, self.other)
            if other != found:
                self.disagreed = True
                found = _both_readings(found, other, self.grammar.identifiers)
        masked = _blanked(text, self.grammar, found)
        numbers = _number_edits(masked, self.dialect) if self.mode == "query" else []
        out: list[str] = []
        last = 0
        number_at = 0
        stack: list[str] = []  # the function name of each open parenthesis
        for start, end, opener in found:
            code_end = start
            replaced_from = start
            if _is_string(opener, self.grammar):
                replaced_from = _prefix_start(text, start, opener)
                code_end = replaced_from
            number_at = self._code(text, last, code_end, numbers, number_at, numbering, out)
            _track_parens(masked, last, code_end, stack)
            span = text[start:end]
            if opener in ("--", "#", "/*") or opener.startswith("/*"):
                comment = self._comment(span, opener, end == len(text), numbering)
                if comment != span:
                    # A rewritten comment can lose a ``*/`` (a nested reading changes):
                    # confirmed by another pass (``_redacted``).
                    self.disagreed = True
                out.append(comment)
            elif not _is_string(opener, self.grammar):
                out.append(span)  # a quoted identifier
            else:
                before = masked[max(0, replaced_from - _CONTEXT) : replaced_from]
                piece, typed = self._string(
                    text[replaced_from:start], span, opener, before, stack, numbering, body=body
                )
                if typed:
                    # ``DATE '...'``: the keyword goes with its literal.
                    # Only the last few pieces are read, so a text with many typed
                    # literals takes time in proportion to its length.
                    for back in range(1, min(_TYPED_TAIL_PIECES, len(out)) + 1):
                        keyword = _TYPED_LITERAL_RE.search(out[-back])
                        if keyword is not None:
                            # The keyword's piece is cut there, and what stood between
                            # it and the literal (space, a comment) goes with it.
                            out[-back] = out[-back][: keyword.start()]
                            del out[len(out) - back + 1 :]
                            break
                        if out[-back].strip() and not out[-back].lstrip().startswith(
                            ("/*", "--", "#")
                        ):
                            break
                if piece != text[replaced_from:end]:
                    # A replacement never runs into a word or a quote beside it, so it
                    # reads the same when it is redacted again (``q'?'`` would open an
                    # Oracle q-quote).
                    if out and out[-1][-1:] and (out[-1][-1].isalnum() or out[-1][-1] in "_$'\""):
                        piece = " " + piece
                    if (
                        piece[-1:] in "?0123456789"
                        and end < len(text)
                        and (text[end].isalnum() or text[end] in "_$'\"")
                    ):
                        piece = piece + " "
                out.append(piece)
            last = end
        self._code(text, last, len(text), numbers, number_at, numbering, out)
        return "".join(out)

    def _code(
        self,
        text: str,
        start: int,
        end: int,
        numbers: list[tuple[int, int]],
        at: int,
        numbering: _Numbering,
        out: list[str],
    ) -> int:
        pos = start
        while at < len(numbers) and numbers[at][0] < start:
            at += 1
        while at < len(numbers) and numbers[at][1] <= end:
            s, e = numbers[at]
            out.append(text[pos:s])
            mark = numbering.take()
            if s > 0 and (text[s - 1].isalnum() or text[s - 1] in "_$"):
                mark = " " + mark
            if e < len(text) and (text[e].isalnum() or text[e] in "_$'\""):
                mark = mark + " "
                if text[e].isalnum() or text[e] == "_":
                    # A number glued to a word: the word is a word of its own now, which
                    # a keyword rule can read differently (``1ORDER BY``), so the redacted
                    # text is confirmed with another pass (``_redacted``).
                    self.disagreed = True
            out.append(mark)
            pos = e
            at += 1
        out.append(text[pos:end])
        return at

    def _comment(self, span: str, opener: str, at_end: bool, numbering: _Numbering) -> str:
        code = True
        if code and opener in ("--", "#") and at_end and "\n" not in span:
            # The rest of a one-line statement: SQL, redacted as such.
            rest = scrub_comment(span[len(opener) :])
            return opener + scrub_comment(self._redact(rest, numbering, body=False))
        if code and span.startswith("/*!") and span.endswith("*/") and len(span) >= 5:
            # MySQL's executable comment is code.
            return "/*!" + self._redact(span[3:-2], numbering, body=False) + "*/"
        if code and opener == "/*" and (len(span) < 4 or not span.endswith("*/")):
            # Cut inside a comment: what follows may be the statement itself.
            return "/*" + scrub_comment(
                self._redact(scrub_comment(span[2:]), numbering, body=False)
            )
        if opener == "/*" and len(span) >= 4:
            return "/*" + redact_comment(span[2:-2], self.engine) + "*/"
        return opener + redact_comment(span[len(opener) :], self.engine)

    def _is_statement(self, text: str) -> bool:
        """Whether a string inside a body is itself a statement (dynamic SQL): it has a
        ``query_type`` (:func:`src.tools.sql.query_type.statement_query_type`)."""
        from src.tools.sql.query_type import statement_query_type

        return statement_query_type(_unescape(text, "'"), self.engine) != "OTHER"

    def _string(
        self,
        prefix: str,
        span: str,
        opener: str,
        before: str,
        stack: list[str],
        numbering: _Numbering,
        *,
        body: bool,
    ) -> tuple[str, bool]:
        """A string literal's replacement, and whether it took a typed literal's keyword."""
        if opener == _DISAGREED:
            # A run the two readings split differently: one value, nothing of it kept.
            return ("'?'" if self.quoted or body else numbering.take()), False
        closer_len, inner = _inner(span, opener)
        head = prefix + span[: len(span) - len(inner) - closer_len]
        tail = span[len(span) - closer_len :] if closer_len else ""
        unterminated = closer_len == 0 and not opener.startswith("$")
        if unterminated:
            return head, False
        function = stack[-1] if stack else ""
        is_code = opens_routine_body(before) or (body and self._is_statement(inner))
        if opener.startswith("$") and not is_code:
            # A dollar-quoted constant (``$q$Jane$q$``), not a ``DO`` block or a routine
            # body: a string literal.
            if not any(ch.isalnum() for ch in inner):
                return prefix + span, False
            if self.quoted:
                return head + "?" + tail, False
            return numbering.take(), False
        if opener.startswith("$"):
            body_text = self._redact(inner, numbering, body=True)
            if body_text != inner:
                # The body's first or last placeholder never runs into the ``$tag$``.
                body_text = (" " if body_text[:1] in "?$:" else "") + body_text
                body_text += " " if body_text[-1:] in "?$:0123456789" else ""
            return head + body_text + tail, False
        if _DYNAMIC_SQL_RE.search(before) or (body and self._is_statement(inner)):
            code = _unescape(inner, opener)
            return head + _escape(self._redact(code, numbering, body=body), opener) + tail, False
        if not any(ch.isalnum() for ch in inner):
            return prefix + span, False
        if _ALIAS_RE.search(before):
            return prefix + span, False
        if _JSON_PATH_RE.match(inner):
            return prefix + span, False
        if function in _DATE_FUNCTIONS and (
            _DATE_FORMAT_RE.match(inner) or _PERCENT_FORMAT_RE.match(inner)
        ):
            return prefix + span, False
        if "%" in inner or function in _PATTERN_FUNCTIONS or _PATTERN_CONTEXT_RE.search(before):
            return head + scrub_comment(_shape(inner)) + tail, False
        kind = prefix.upper()
        if kind in ("X", "BX", "B"):
            # A binary or bit string: zeroed, in its own form, which not every parser
            # reads (sqlglot reads no ``X'...'``), so it reads as it did.
            return head + ("0" if kind == "B" else "00") + tail, False
        if self.quoted or body or opener != "'" or kind:
            return head + "?" + tail, False
        return numbering.take(), bool(_TYPED_LITERAL_RE.search(before))


# The opener of a run the two readings of a setting split differently (``_both_readings``).
_DISAGREED = "\x00"


def _other_reading(dialect: str, settings: SpanSettings) -> SpanSettings | None:
    """The other value of the setting that moves a string's end in ``dialect``, or
    ``None`` when no setting does (SQL Server, Oracle, an unknown engine)."""
    if dialect == "mysql":
        return replace(settings, no_backslash_escapes=not settings.no_backslash_escapes)
    if dialect == "postgresql":
        return replace(
            settings, standard_conforming_strings=not settings.standard_conforming_strings
        )
    return None


def _both_readings(
    found: list[tuple[int, int, str]],
    other: list[tuple[int, int, str]],
    identifiers: frozenset[str],
) -> list[tuple[int, int, str]]:
    """The spans masking every character either reading hides (a string or a comment).

    Each maximal run of hidden characters is one span: the reading's own when both
    readings hide exactly that span, the same way, else a ``_DISAGREED`` value. A quoted
    identifier is kept where no run covers it."""
    hidden = sorted(
        (start, end) for start, end, opener in found + other if opener not in identifiers
    )
    runs: list[list[int]] = []
    for start, end in hidden:
        if runs and start < runs[-1][1]:
            runs[-1][1] = max(runs[-1][1], end)
        else:
            runs.append([start, end])
    mine = {(s, e): o for s, e, o in found if o not in identifiers}
    theirs = {(s, e): o for s, e, o in other if o not in identifiers}
    out: list[tuple[int, int, str]] = []
    for start, end in runs:
        opener = mine.get((start, end))
        agreed = opener is not None and theirs.get((start, end)) == opener
        out.append((start, end, opener if agreed and opener is not None else _DISAGREED))
    for start, end, opener in found:
        if opener in identifiers and not any(s < end and start < e for s, e in runs):
            out.append((start, end, opener))
    return sorted(out)


def _is_string(opener: str, grammar) -> bool:
    return opener not in ("--", "#", "/*") and opener not in grammar.identifiers


def _blanked(text: str, grammar, found: list[tuple[int, int, str]] | None = None) -> str:
    """``text`` with every comment, literal and quoted identifier blanked (same length):
    a comment as spaces, a quoted name as ``x``s (a name, as it reads), a string as
    ``?``s (the placeholder it becomes), so a rule reads around a string exactly as it
    reads around its redacted form, and redacting twice decides the same."""
    chars = list(text)
    for start, end, opener in found if found is not None else spans(text, grammar):
        if opener in ("--", "#", "/*"):
            fill = " "
        elif opener in grammar.identifiers:
            fill = "x"
        else:
            fill = "?"
        chars[start:end] = fill * (end - start)
    return "".join(chars)


# A PostgreSQL dollar quote's opener (``$$``, ``$tag$``), as ``sql_spans`` reads one.
_DOLLAR_OPENER_RE = re.compile(r"(?<![\w$])\$(?:[^\W\d]\w*)?\$")


def _unterminated(
    masked: str,
    dialect: str,
    text: str = "",
    found: list[tuple[int, int, str]] | None = None,
) -> tuple[int, int, str] | None:
    """Where an unclosed string or block comment starts, as a span to the end: a quote
    (every prefixed form, ``N'``, ``E'``, ``q'[``, leaves its quote), a ``/*`` or, in
    PostgreSQL, a dollar quote (``$$x`` cut at a length cap holds a value too). A dollar
    opener is looked for with every span of ``found`` blanked to spaces, so one right
    after a closed span (``$$a$$$$x``, ``"n"$$x``) counts: PostgreSQL opens a string
    there, and erring that way masks more, never less."""
    quotes = "'\"" if dialect == "mysql" else "'"
    candidates = [masked.find(q) for q in quotes] + [masked.find("/*")]
    dollar = None
    if dialect == "postgresql":
        chars = list(text or masked)
        for start, end, _opener in found or ():
            chars[start:end] = " " * (end - start)
        dollar = _DOLLAR_OPENER_RE.search("".join(chars))
    if dollar is not None:
        candidates.append(dollar.start())
    starts = [c for c in candidates if c >= 0]
    if not starts:
        return None
    start = min(starts)
    if dollar is not None and start == dollar.start():
        return start, len(masked), dollar.group()
    opener = "/*" if masked.startswith("/*", start) else masked[start]
    return start, len(masked), opener


def _prefix_start(text: str, start: int, opener: str) -> int:
    """Where a plain string's prefix (``N``, ``X``, ``_utf8mb4``, ...) starts."""
    if opener not in ("'", '"'):
        return start
    match = _PREFIX_RE.search(text[max(0, start - 32) : start])
    return start - len(match.group()) if match else start


def _inner(span: str, opener: str) -> tuple[int, str]:
    """The length of a string span's closing delimiter (0 when it has none) and the text
    between its delimiters."""
    if opener.startswith("$"):
        if len(span) >= 2 * len(opener) and span.endswith(opener):
            return len(opener), span[len(opener) : -len(opener)]
        return 0, span[len(opener) :]
    if is_alternative_quote(opener):
        if len(span) >= len(opener) + 2 and span.endswith("'"):
            return 2, span[len(opener) : -2]
        return 0, span[len(opener) :]
    quote = opener[-1]
    if len(span) >= len(opener) + 1 and span.endswith(quote):
        return 1, span[len(opener) : -1]
    return 0, span[len(opener) :]


def _unescape(inner: str, opener: str) -> str:
    quote = opener[-1]
    if opener.startswith("$") or is_alternative_quote(opener) or quote not in "'\"":
        return inner
    return inner.replace(quote * 2, quote)


def _escape(inner: str, opener: str) -> str:
    quote = opener[-1]
    if opener.startswith("$") or is_alternative_quote(opener) or quote not in "'\"":
        return inner
    return inner.replace(quote, quote * 2)


def _shape(inner: str) -> str:
    """A pattern's shape: each run of letters and digits written ``x`` (two when a
    regular-expression quantifier follows, so the run's own prefix stays one)."""

    def repl(match: re.Match[str]) -> str:
        following = inner[match.end() : match.end() + 1]
        return "xx" if following in ("?", "*", "+", "{") and len(match.group()) > 1 else "x"

    return _WORD_RUN_RE.sub(repl, inner)


def _track_parens(masked: str, start: int, end: int, stack: list[str]) -> None:
    """Follow ``masked[start:end]``'s parentheses: push each one's function name."""
    i = masked.find("(", start, end)
    j = masked.find(")", start, end)
    while i >= 0 or j >= 0:
        if j < 0 or (0 <= i < j):
            name = _FUNCTION_NAME_RE.search(masked[max(0, i - 64) : i])
            stack.append(name.group(1).lower() if name else "")
            i = masked.find("(", i + 1, end)
        else:
            if stack:
                stack.pop()
            j = masked.find(")", j + 1, end)


def _positions(
    masked: str,
    lists: list[tuple[int, int]],
    fixed: list[tuple[int, int]],
    numbers: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    """The part of each ``ORDER BY``/``GROUP BY`` list in which a number is a position.

    A list is read as positions only up to its first item that is replaced: a hex, binary
    or money literal, or a number that is not a plain integer position (a longer one, a
    decimal, one glued to a word). After it the numbers are redacted. The redacted text
    has a placeholder there, which ends the list for the next pass, so both passes agree
    (``ORDER BY 0x1F, 2`` is ``ORDER BY ?, ?`` at once)."""
    out: list[tuple[int, int]] = []
    for start, end in lists:
        cut = end
        for a, _b in fixed:
            if start <= a < cut:
                cut = a
        for a, b in numbers:
            if start <= a < cut:
                number = masked[a:b]
                position = _SMALL_INTEGER_RE.fullmatch(number) and len(number) <= _MAX_KEPT_DIGITS
                if not position or masked[b : b + 1] in _POSITION_STOP:
                    cut = a
        out.append((start, cut))
    return out


_POSITION_STOP = frozenset("._$#") | frozenset(string.ascii_letters + string.digits)


def _number_edits(masked: str, dialect: str) -> list[tuple[int, int]]:
    """The numbers (and binary and money literals) of the code to replace, in order."""
    number_re, *literal_res = _NUMBER_PATTERNS.get(dialect, _NUMBER_PATTERNS["db2"])
    fixed = [m.span() for pattern in literal_res for m in pattern.finditer(masked)]
    if dialect == "sqlserver":
        fixed += [m.span() for m in _MONEY_RE.finditer(masked)]
    numbers = [m.span() for m in number_re.finditer(masked)]
    keep = _positions(masked, [m.span() for m in _ORDERING_RE.finditer(masked)], fixed, numbers)

    def kept(start: int, end: int) -> bool:
        number = masked[start:end]
        if not _SMALL_INTEGER_RE.fullmatch(number) or len(number) > _MAX_KEPT_DIGITS:
            return False
        before = masked[max(0, start - 80) : start]
        after = masked[end : end + 40]
        if _ALONE_BEFORE_RE.search(before):
            if number not in ("0", "1"):
                return False
            if _COUNT_BEFORE_RE.search(before):
                return bool(_COUNT_ALONE_AFTER_RE.match(after))
            return bool(_SELECT_ALONE_AFTER_RE.match(after))
        if _TYPE_SIZE_BEFORE_RE.search(before):
            return len(number) <= _MAX_TYPE_SIZE_DIGITS
        if any(s <= start and end <= e for s, e in keep):
            return True
        return bool(_KEEP_AFTER_RE.match(after) or _KEEP_BEFORE_RE.search(before))

    edits = fixed + [span for span in numbers if not kept(*span)]
    edits.sort()
    # A money literal holds a number (``$ 1984.37``): one value, one placeholder.
    merged: list[tuple[int, int]] = []
    for start, end in edits:
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


# -- whole collections ------------------------------------------------------------

_DEFINITIONS = ("views", "procedures", "triggers")


def collector_output_engine(payload: Mapping[str, Any]) -> str:
    """A collector output's source engine (``metadata.source_database.engine``)."""
    meta = payload.get("metadata")
    source = meta.get("source_database") if isinstance(meta, Mapping) else None
    engine = source.get("engine") if isinstance(source, Mapping) else None
    return str(getattr(engine, "value", engine) or "")


def _redacted_definitions(items: object, engine: str, settings: SpanSettings) -> object:
    if not isinstance(items, list):
        return items
    out: list[Any] = []
    for item in items:
        if isinstance(item, Mapping) and isinstance(item.get("definition"), str):
            text = redact_definition(item["definition"], engine, settings)
            if text != item["definition"]:
                item = {**item, "definition": text}
        out.append(item)
    return out if any(a is not b for a, b in zip(out, items, strict=True)) else items


def redact_collector_output(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """A collector output (a dict) with every query text, query tag and view,
    procedure and trigger definition redacted in its source engine's dialect, read with
    its source settings (``metadata.source_database.settings``; none: the defaults).

    The same object when nothing changes (a collector output written since #1625),
    else a new dict; the input is never changed. A shape it does not know (no
    ``queries.query_patterns`` list) is returned as it is.
    """
    engine = collector_output_engine(payload)
    settings = span_settings_of(payload)
    out: dict[str, Any] | None = None
    queries = payload.get("queries")
    patterns = queries.get("query_patterns") if isinstance(queries, Mapping) else None
    if isinstance(patterns, list):
        rows: list[Any] = []
        changed = False
        for row in patterns:
            if isinstance(row, Mapping):
                fixed = _redacted_pattern(row, engine, settings)
                changed = changed or fixed is not row
                row = fixed
            rows.append(row)
        if changed:
            out = {**payload, "queries": {**queries, "query_patterns": rows}}  # type: ignore[dict-item]
    schema = payload.get("database_schema")
    if isinstance(schema, Mapping):
        fixed_schema = {
            k: _redacted_definitions(schema.get(k), engine, settings) for k in _DEFINITIONS
        }
        if any(fixed_schema[k] is not schema.get(k) for k in _DEFINITIONS if k in schema):
            out = dict(out if out is not None else payload)
            out["database_schema"] = {
                **schema,
                **{k: v for k, v in fixed_schema.items() if k in schema},
            }
    return out if out is not None else payload


def _redacted_pattern(
    row: Mapping[str, Any], engine: str, settings: SpanSettings
) -> Mapping[str, Any]:
    text = row.get("query_text")
    tags = row.get("query_tags")
    new_text = redact_literals(text, engine, settings) if isinstance(text, str) else text
    new_tags = tags
    if isinstance(tags, list) and tags:
        fixed = [redact_comment(t, engine) if isinstance(t, str) else t for t in tags]
        if fixed != tags:
            new_tags = list(dict.fromkeys(fixed))
    if new_text == text and new_tags is tags:
        return row
    return {**row, "query_text": new_text, "query_tags": new_tags}


def redact_raw_collection(data: Mapping[str, Any]) -> Mapping[str, Any]:
    """A raw collection-script file with every query text and definition redacted, in
    the source engine the metadata names (``offline_parser.detect_source_engine``), read
    with the settings it reports (``metadata.settings``, :func:`raw_collection_settings`).

    A query row with no ``digest`` gets the one the parser gives it (the first 16 hex
    digits of the SHA-256 of its text as collected), so the copy keeps its
    ``query_id``s when it is read again. The same object when nothing changes.
    """
    import hashlib

    from src.tools.database.offline_parser import detect_source_engine

    metadata = data.get("metadata")
    engine = detect_source_engine(metadata if isinstance(metadata, dict) else None)[0]
    settings = raw_collection_settings(engine, metadata)
    out: dict[str, Any] | None = None
    queries = data.get("queries")
    if isinstance(queries, list):
        rows: list[Any] = []
        changed = False
        for row in queries:
            text = row.get("query_text") if isinstance(row, Mapping) else None
            if isinstance(text, str):
                redacted = redact_literals(text, engine, settings)
                if redacted != text:
                    digest = row.get("digest") or hashlib.sha256(text.encode()).hexdigest()[:16]
                    row = {**row, "query_text": redacted, "digest": digest}
                    changed = True
            rows.append(row)
        if changed:
            out = {**data, "queries": rows}
    for key in _DEFINITIONS:
        if key in data:
            fixed = _redacted_definitions(data[key], engine, settings)
            if fixed is not data[key]:
                out = dict(out if out is not None else data)
                out[key] = fixed
    return out if out is not None else data


def raw_collection_settings(source_engine: str | None, metadata: object) -> SpanSettings:
    """The span settings a raw collection-script file reports in its ``metadata.settings``
    (``sql_mode``, ``standard_conforming_strings``, #1482); none: the engine's defaults."""
    if isinstance(metadata, str):
        import json

        try:
            metadata = json.loads(metadata)
        except ValueError:
            metadata = None
    reported = metadata.get("settings") if isinstance(metadata, Mapping) else None
    return span_settings(source_engine, reported if isinstance(reported, Mapping) else None)


def _reported_settings(data: Any, source_engine: str | None) -> SpanSettings | None:
    """The source settings a collector output or a parsed collection reports, or ``None``."""
    metadata = data.get("metadata") if isinstance(data, Mapping) else None
    if isinstance(metadata, str):
        return raw_collection_settings(source_engine, metadata)
    if not isinstance(metadata, Mapping):
        return None
    source = metadata.get("source_database")
    if isinstance(source, Mapping) and isinstance(source.get("settings"), Mapping):
        return span_settings_of(data)
    if isinstance(metadata.get("settings"), Mapping):
        return raw_collection_settings(source_engine, metadata)
    return None


_TEXT_KEYS = {"query_text": redact_literals, "definition": redact_definition}


def redact_values(
    data: Any, source_engine: str | None, settings: SpanSettings | None = None
) -> Any:
    """``data`` (any JSON value: a live collector's stage, a collector output) with every
    ``query_text`` and ``definition`` string and every ``query_tags`` entry redacted,
    however deep, read with ``settings``: by default the ones ``data`` reports (a
    collector output's ``metadata.source_database.settings``, a parsed collection's
    ``metadata.settings``), else the active ones. A new value; the input is not changed.
    Idempotent."""
    if settings is None:
        settings = _reported_settings(data, source_engine)
    if isinstance(data, Mapping):
        out: dict[Any, Any] = {}
        for key, value in data.items():
            redact = _TEXT_KEYS.get(key) if isinstance(key, str) else None
            if redact is not None and isinstance(value, str):
                out[key] = redact(value, source_engine, settings)
            elif key == "query_tags" and isinstance(value, list):
                tags = [
                    redact_comment(t, source_engine) if isinstance(t, str) else t for t in value
                ]
                out[key] = (
                    list(dict.fromkeys(tags)) if all(isinstance(t, str) for t in tags) else tags
                )
            else:
                out[key] = redact_values(value, source_engine, settings)
        return out
    if isinstance(data, list):
        return [redact_values(item, source_engine, settings) for item in data]
    return data
