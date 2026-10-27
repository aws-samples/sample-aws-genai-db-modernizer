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

Text the database already normalised has nothing left to replace and comes back
unchanged, and redacting redacted text changes nothing (``redact(redact(t)) ==
redact(t)``). :func:`src.tools.sql.normalize.literal_shape` is not reused for the
stored text: it upper-cases names, drops comments and collapses ``IN`` lists, which
is right for grouping variants and wrong for a text the parser reads again.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from threading import Lock
from typing import Any

from src.shared.sql_spans import (
    is_alternative_quote,
    literals_and_comments_for,
    opens_routine_body,
    span_dialect,
    spans,
)

__all__ = [
    "collector_output_engine",
    "redact_collector_output",
    "redact_comment",
    "redact_definition",
    "redact_literals",
    "redact_raw_collection",
    "redact_values",
    "scrub_comment",
]

# Anything that could be a literal or a comment: text with none comes back as it is.
_MAYBE_LITERAL_RE = re.compile(r"['\"\d$#]|--|/\*")

_NUMBER_RE = re.compile(
    r"(?<![\w$#:.@])(?:\d+(?:\.\d+)*\.?|\.\d+(?:\.\d+)*)(?:[eE][+-]?\d+)?[fFdD]?(?![\w$#])",
    re.UNICODE,
)
_HEX_RE = re.compile(r"(?<![\w$#:.@])0[xX][0-9A-Fa-f]+(?![\w$#])")

# SQL Server's money literal (``$1984.37``): the ``$`` is part of the value.
_MONEY_RE = re.compile(r"(?<![\w$#:.@])\$\s?[+-]?(?:\d+\.?\d*|\.\d+)(?![\w$#])")
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


def redact_literals(text: str, source_engine: str | None) -> str:
    """Query text with its literal values replaced by placeholders (module docstring)."""
    if not text or not _MAYBE_LITERAL_RE.search(text):
        return text
    return _redacted(text, _engine(source_engine), "query")


def redact_definition(text: str, source_engine: str | None) -> str:
    """A view, procedure or trigger definition with each string literal's value
    replaced by ``?`` inside its own quotes; numbers are kept (module docstring)."""
    if not text or not _MAYBE_LITERAL_RE.search(text):
        return text
    return _redacted(text, _engine(source_engine), "definition")


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
        self._items: OrderedDict[tuple[str, str, str], str] = OrderedDict()
        self._chars = 0
        self._lock = Lock()

    def get(self, key: tuple[str, str, str]) -> str | None:
        with self._lock:
            value = self._items.get(key)
            if value is not None:
                self._items.move_to_end(key)
            return value

    def put(self, key: tuple[str, str, str], value: str) -> None:
        size = len(key[0]) + len(value)
        if size > self.max_chars // 8:
            return
        with self._lock:
            if key in self._items:
                return
            self._items[key] = value
            self._chars += size
            while self._chars > self.max_chars:
                (text, _, _), old = self._items.popitem(last=False)
                self._chars -= len(text) + len(old)

    @property
    def chars(self) -> int:
        return self._chars


_CACHE = _SizeBoundCache(max_chars=8_000_000)


def _redacted(text: str, engine: str, mode: str) -> str:
    """One text redacted, cached (:class:`_SizeBoundCache`): a job reads its collector
    output many times, and every read redacts it (:func:`redact_collector_output`)."""
    key = (text, engine, mode)
    cached = _CACHE.get(key)
    if cached is None:
        cached = _Redactor(engine, mode).redact(text)
        _CACHE.put(key, cached)
    return cached


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

    def __init__(self, engine: str, mode: str) -> None:
        self.engine = engine
        self.dialect = span_dialect(engine)
        self.grammar = literals_and_comments_for(engine)
        self.mode = mode
        self.quoted = mode != "query"
        self.style = {"oracle": ":", "postgresql": "$"}.get(self.dialect, "?")

    def redact(self, text: str) -> str:
        masked = _blanked(text, self.grammar)
        numbering = _Numbering(self.style)
        pattern = _EXISTING_BIND_RE.get(self.style)
        if pattern is not None:
            numbering.next = max((int(m.group(1)) for m in pattern.finditer(masked)), default=0) + 1
        return self._redact(text, numbering, body=False)

    # -- one text (or a body / dynamic SQL inside one), left to right ----------------

    def _redact(self, text: str, numbering: _Numbering, *, body: bool) -> str:
        found = list(spans(text, self.grammar))
        masked = _blanked(text, self.grammar, found)
        # A quote or ``/*`` left in the code has no closer: the text was cut inside a
        # string or a comment, which then runs to the end.
        cut = _unterminated(masked, self.dialect)
        if cut is not None:
            found = [s for s in found if s[1] <= cut[0]] + [cut]
            masked = masked[: cut[0]] + "?" * (len(masked) - cut[0])
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
                out.append(self._comment(span, opener, end == len(text), numbering))
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


def _unterminated(masked: str, dialect: str) -> tuple[int, int, str] | None:
    """Where an unclosed string or block comment starts, as a span to the end."""
    quotes = "'\"" if dialect == "mysql" else "'"
    candidates = [masked.find(q) for q in quotes] + [masked.find("/*")]
    starts = [c for c in candidates if c >= 0]
    if not starts:
        return None
    start = min(starts)
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


def _number_edits(masked: str, dialect: str) -> list[tuple[int, int]]:
    """The numbers (and binary and money literals) of the code to replace, in order."""
    keep: list[tuple[int, int]] = [m.span() for m in _ORDERING_RE.finditer(masked)]

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

    edits = [m.span() for m in _HEX_RE.finditer(masked)]
    if dialect == "sqlserver":
        edits += [m.span() for m in _MONEY_RE.finditer(masked)]
    edits += [m.span() for m in _NUMBER_RE.finditer(masked) if not kept(*m.span())]
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


def _redacted_definitions(items: object, engine: str) -> object:
    if not isinstance(items, list):
        return items
    out: list[Any] = []
    for item in items:
        if isinstance(item, Mapping) and isinstance(item.get("definition"), str):
            text = redact_definition(item["definition"], engine)
            if text != item["definition"]:
                item = {**item, "definition": text}
        out.append(item)
    return out if any(a is not b for a, b in zip(out, items, strict=True)) else items


def redact_collector_output(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """A collector output (a dict) with every query text, query tag and view,
    procedure and trigger definition redacted in its source engine's dialect.

    The same object when nothing changes (a collector output written since #1625),
    else a new dict; the input is never changed. A shape it does not know (no
    ``queries.query_patterns`` list) is returned as it is.
    """
    engine = collector_output_engine(payload)
    out: dict[str, Any] | None = None
    queries = payload.get("queries")
    patterns = queries.get("query_patterns") if isinstance(queries, Mapping) else None
    if isinstance(patterns, list):
        rows: list[Any] = []
        changed = False
        for row in patterns:
            if isinstance(row, Mapping):
                fixed = _redacted_pattern(row, engine)
                changed = changed or fixed is not row
                row = fixed
            rows.append(row)
        if changed:
            out = {**payload, "queries": {**queries, "query_patterns": rows}}  # type: ignore[dict-item]
    schema = payload.get("database_schema")
    if isinstance(schema, Mapping):
        fixed_schema = {k: _redacted_definitions(schema.get(k), engine) for k in _DEFINITIONS}
        if any(fixed_schema[k] is not schema.get(k) for k in _DEFINITIONS if k in schema):
            out = dict(out if out is not None else payload)
            out["database_schema"] = {
                **schema,
                **{k: v for k, v in fixed_schema.items() if k in schema},
            }
    return out if out is not None else payload


def _redacted_pattern(row: Mapping[str, Any], engine: str) -> Mapping[str, Any]:
    text = row.get("query_text")
    tags = row.get("query_tags")
    new_text = redact_literals(text, engine) if isinstance(text, str) else text
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
    the source engine the metadata names (``offline_parser.detect_source_engine``).

    A query row with no ``digest`` gets the one the parser gives it (the first 16 hex
    digits of the SHA-256 of its text as collected), so the copy keeps its
    ``query_id``s when it is read again. The same object when nothing changes.
    """
    import hashlib

    from src.tools.database.offline_parser import detect_source_engine

    metadata = data.get("metadata")
    engine = detect_source_engine(metadata if isinstance(metadata, dict) else None)[0]
    out: dict[str, Any] | None = None
    queries = data.get("queries")
    if isinstance(queries, list):
        rows: list[Any] = []
        changed = False
        for row in queries:
            text = row.get("query_text") if isinstance(row, Mapping) else None
            if isinstance(text, str):
                redacted = redact_literals(text, engine)
                if redacted != text:
                    digest = row.get("digest") or hashlib.sha256(text.encode()).hexdigest()[:16]
                    row = {**row, "query_text": redacted, "digest": digest}
                    changed = True
            rows.append(row)
        if changed:
            out = {**data, "queries": rows}
    for key in _DEFINITIONS:
        if key in data:
            fixed = _redacted_definitions(data[key], engine)
            if fixed is not data[key]:
                out = dict(out if out is not None else data)
                out[key] = fixed
    return out if out is not None else data


_TEXT_KEYS = {"query_text": redact_literals, "definition": redact_definition}


def redact_values(data: Any, source_engine: str | None) -> Any:
    """``data`` (any JSON value: a live collector's stage, a collector output) with every
    ``query_text`` and ``definition`` string and every ``query_tags`` entry redacted,
    however deep. A new value; the input is not changed. Idempotent."""
    if isinstance(data, Mapping):
        out: dict[Any, Any] = {}
        for key, value in data.items():
            redact = _TEXT_KEYS.get(key) if isinstance(key, str) else None
            if redact is not None and isinstance(value, str):
                out[key] = redact(value, source_engine)
            elif key == "query_tags" and isinstance(value, list):
                tags = [
                    redact_comment(t, source_engine) if isinstance(t, str) else t for t in value
                ]
                out[key] = (
                    list(dict.fromkeys(tags)) if all(isinstance(t, str) for t in tags) else tags
                )
            else:
                out[key] = redact_values(value, source_engine)
        return out
    if isinstance(data, list):
        return [redact_values(item, source_engine) for item in data]
    return data
