# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Query-text facts for the live collectors, read through the SQL facade (#795).

The offline parser reads every query's table references and its
``has_joins``/``join_count``/``has_aggregations``/``has_subqueries`` facts from
:func:`src.tools.sql.facade.read_sql` (#553, #796). The live collectors
(``mysql_tools``, also used by ``mariadb_tools``, ``postgres_tools``,
``sqlserver_tools``, ``oracle_tools`` and the two ``rds_tools`` call sites)
read them here, from the same facade in their own engine's dialect, so a live
and an offline collection of the same query text agree. Their ``has_text_search``
and ``text_search_type`` are the ``text_search`` query fact and its parts
(:mod:`src.tools.sql.query_facts`, #929), the definition triage and the cache
overlay read next to them. The facade's regex
fallback is the only regex scan left; a collection logs how many of its
queries used it, as the offline parser does.

Each module keeps its own spelling of a table name (the shape it returned
before), so its downstream consumers see no format change:

- a reference is spelled as written (``[catalog.][schema.]name``, quotes
  removed), the way the offline parser keeps a name it can't resolve;
- PostgreSQL drops a ``public.`` qualifier (``public.topics`` is ``topics``);
- Oracle lower-cases the name;
- a system catalog table (``information_schema.columns``, ``sys.objects``) is a
  table the query reads and is named like any other, the offline parser's rule
  (#483); only a bare ``sys`` or ``information_schema``, a schema read as a
  table, is skipped.

Resolving a name to a collection table id (``<database>.<table>``) stays with
the pipeline (``TableNameResolver``); a live collector reads its queries
before it has a table list to resolve against.

A collector's own filter, sort and time-range parsing reads the text the engine
runs (#1420): :func:`column_text` for the columns, :func:`time_range_text` for the
time range, so a keyword or a column name inside a comment or a string literal
(a query tag, a note) is never read as SQL, and a quoted identifier is one name whose
words are never SQL either (#1737).
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from src.shared.sql_spans import (
    NEUTRAL_NAME,
    literals_and_comments_for,
    opens_routine_body,
    spans,
)
from src.tools.sql import facade
from src.tools.sql.facade import SqlReading, TableRef, TableReferences
from src.tools.sql.query_facts import query_facts, remember_stored_facts
from src.tools.sql.query_tags import QueryTags
from src.tools.sql.stored_facts import stored_reading

logger = logging.getLogger(__name__)

# A literal that reads as a date, a time, a timestamp or an interval's amount, in the forms
# MySQL, PostgreSQL, SQL Server and Oracle accept (with a string prefix such as N or E):
# digits and separators ('2026-01-01 10:00:00+02', '7'), or an ISO date with an optional
# T or space time, fraction, a Z or an offset, and a zone name ('2026-01-01T10:00:00Z',
# '2026-01-01 10:00:00 UTC', '2026-01-01 10:00:00 -0800 PST', Go's
# '2026-01-01 10:00:00.000000 +0000 UTC').
_TIME_LITERAL_RE = re.compile(
    r"[A-Za-z]{0,2}'(?:[\d\s:.+\-/]*"
    r"|\d{4}-\d{2}-\d{2}(?:[T ]\d{1,2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?)?"
    r"\s*(?:Z|[+-]\d{2}(?::?\d{2})?)?\s*(?:[A-Za-z][\w/+\-]*)?)'",
    re.IGNORECASE,
)

_SYSTEM_CATALOGS = frozenset({"sys", "information_schema"})
_POSTGRES_ENGINES = frozenset({"postgresql", "postgres"})


def read(sql: str, source_engine: str, query_id: str = "", *, log_trace: bool = True) -> SqlReading:
    """The facade's reading of ``sql``; the regex reading if the facade raises.

    The facade never raises by contract. If it does anyway the regex answers
    are used, labelled ``fallback_reason="facade_error"``, so a parser bug
    can't fail a collection. A warning with the stack trace is logged when
    ``log_trace`` (a :class:`LiveQueryReader` logs it once per collection and
    counts the rest in its summary line).
    """
    try:
        return facade.read_sql(sql or "", source_engine)
    except Exception:  # noqa: BLE001 -- the regex answer keeps the collection going
        if log_trace:
            logger.warning(
                "live collector: sql facade failed on query %s, using the regex scans",
                query_id or "?",
                exc_info=True,
            )
        return facade.regex_reading(sql or "", source_engine, "facade_error")


def table_names(refs: TableReferences, source_engine: str) -> list[str]:
    """The live spelling of each table reference, duplicates collapsed in
    first-seen order."""
    names: list[str] = []
    for ref in refs.tables:
        if _is_system_catalog(ref):
            continue
        names.append(_live_name(ref, source_engine))
    return list(dict.fromkeys(names))


def extract_tables(
    sql: str, source_engine: str, empty: tuple[str, ...] = ("unknown",)
) -> list[str]:
    """The live ``tables_accessed`` for ``sql``: :func:`table_names`, or
    ``empty`` when the text names no table."""
    return table_names(read(sql, source_engine).tables, source_engine) or list(empty)


def _is_system_catalog(ref: TableRef) -> bool:
    """A bare ``sys`` or ``information_schema``: a schema name read as a table."""
    return not ref.qualifiers and ref.name.lower() in _SYSTEM_CATALOGS


def _live_name(ref: TableRef, source_engine: str) -> str:
    engine = (source_engine or "").strip().lower()
    if engine in _POSTGRES_ENGINES and not ref.catalog and (ref.schema or "").lower() == "public":
        return ref.name
    if engine == "oracle":
        return ref.written.lower()
    return ref.written


class LiveQueryReader:
    """Reads one live collection's query texts through the facade.

    ``fields`` returns the query-text keys of one query pattern (the text-search ones
    included, #929, and the leading comment tags, #1180); ``log_fallbacks``
    logs, once per collection, how many queries used the regex fallback. A
    facade error logs its stack trace for the first query it hits only.
    """

    def __init__(self, source_engine: str, collector: str, empty: tuple[str, ...] = ("unknown",)):
        self.source_engine = source_engine
        self.collector = collector
        self.empty = empty
        self._read = 0
        self._fallbacks: Counter[str] = Counter()

    def fields(self, query_id: str, sql: str) -> dict[str, Any]:
        first_error = self._fallbacks["facade_error"] == 0
        reading = read(sql, self.source_engine, query_id, log_trace=first_error)
        self._read += 1
        if reading.tables.fallback_reason:
            self._fallbacks[reading.tables.fallback_reason] += 1
        facts = reading.facts
        stored = stored_reading(sql, self.source_engine, reading)
        # The text-search fact from this same reading, not a second parse (#934).
        remember_stored_facts(sql, self.source_engine, stored)
        searches = query_facts(sql, self.source_engine)
        return {
            "tables_accessed": table_names(reading.tables, self.source_engine) or list(self.empty),
            "has_joins": facts.has_joins,
            "join_count": facts.join_count,
            "has_aggregations": facts.has_aggregation,
            "has_subqueries": facts.has_subquery,
            "has_text_search": searches.text_search,
            "text_search_type": searches.text_search_type(),
            "parsed_facts": stored,
            # The leading comment tags, read as the offline parser reads them (#1180).
            **QueryTags.of([sql], self.source_engine).fields(),
        }

    def log_fallbacks(self) -> None:
        if not self._fallbacks:
            return
        logger.info(
            "%s: table references: %d/%d queries used the regex fallback (%s)",
            self.collector,
            sum(self._fallbacks.values()),
            self._read,
            ", ".join(f"{reason}={n}" for reason, n in sorted(self._fallbacks.items())),
        )


def time_range_text(sql: str, source_engine: str) -> str:
    """``sql`` for a collector's time-range check (#1420), read in ``source_engine``'s
    forms: each comment a space, each date, time or number literal as written
    (:data:`_TIME_LITERAL_RE`) and every other literal empty (``''``). A date literal
    after ``BETWEEN`` or a comparison is the signal the check reads; a word inside any
    other literal is text, never a function or a column.

    A PostgreSQL ``DO`` block or ``AS $$ ... $$`` routine body is SQL the engine runs, so
    it is read the same way, as :func:`src.shared.sql_spans.code_text` reads it (#1416):
    ``now() - interval '30 days'`` in it is a time range.
    """
    text = sql or ""
    grammar = literals_and_comments_for(source_engine)
    out: list[str] = []
    last = 0
    for start, end, opener in spans(text, grammar):
        out.append(text[last:start])
        last = end
        span = text[start:end]
        if opener in ("--", "/*", "#"):
            out.append(" ")
        elif opener in grammar.identifiers:
            out.append(span)
        elif (
            opener[0] == "$"
            and end - start >= 2 * len(opener)
            and opens_routine_body("".join(out[-4:]))
        ):
            body = text[start + len(opener) : end - len(opener)]
            out.append(" " + time_range_text(body, source_engine) + " ")
        else:
            out.append(span if _TIME_LITERAL_RE.fullmatch(span) else "''")
    out.append(text[last:])
    return "".join(out)


# The quote that closes each quoted-identifier opener; a doubled closer inside the name is
# one quote of the name.
_IDENTIFIER_CLOSERS = {"`": "`", '"': '"', "[": "]"}


def _is_word_char(char: str) -> bool:
    return char.isalnum() or char == "_"


@dataclass(frozen=True)
class ColumnText:
    """A query's text for a collector's filter and sort parsing (:func:`column_text`):
    ``text`` with each quoted identifier one placeholder word, and the name each
    placeholder stands for."""

    text: str
    names: Mapping[str, str]

    def is_quoted(self, word: str) -> bool:
        """Whether ``word`` is a quoted identifier's placeholder."""
        return word in self.names

    def name(self, word: str, fold: Callable[[str], str] = str) -> str:
        """The column ``word`` names: each dot-separated part that is a placeholder is its
        quoted name as written (``"t"."My Col"`` reads ``t.My Col``), and every other part
        is ``fold(part)`` (PostgreSQL folds an unquoted name to lower case, never a quoted
        one)."""
        return ".".join(
            self.names[part] if part in self.names else fold(part) for part in word.split(".")
        )


def column_text(sql: str, source_engine: str) -> ColumnText:
    """``sql`` for a collector's filter and sort parsing (#1420, #1737), read in
    ``source_engine``'s forms (:func:`src.shared.sql_spans.literals_and_comments_for`):
    each comment a space, each string literal a ``?`` placeholder, and each quoted
    identifier (MySQL and MariaDB backticks, ``"..."`` in every other dialect, SQL Server
    brackets) one placeholder word that no keyword or operator is, with the name it
    stands for (:meth:`ColumnText.name`). A quoted name is one column: its words are never
    SQL (`` `a = 1 AND b IN (2)` `` is no filter), and a name that is not one word keeps
    its spaces and escaped quotes.

    A PostgreSQL ``DO`` block or ``AS $$ ... $$`` routine body is SQL the engine runs, so
    it is read the same way, as :func:`src.shared.sql_spans.code_text` reads it (#1416).
    """
    text = sql or ""
    grammar = literals_and_comments_for(source_engine)
    # A placeholder prefix the text does not hold, so a bare name is never read as one.
    prefix = NEUTRAL_NAME
    while prefix in text.lower():
        prefix += "x"
    names: dict[str, str] = {}

    def read(part: str) -> str:
        out: list[str] = []
        last = 0
        previous_name_end = -1  # where the last quoted identifier ended
        for start, end, opener in spans(part, grammar):
            out.append(part[last:start])
            last = end
            if opener in ("--", "/*", "#"):
                out.append(" ")
            elif opener in grammar.identifiers:
                closer = _IDENTIFIER_CLOSERS[opener]
                name = part[start + 1 : end - 1].replace(closer * 2, closer)
                if opener == "`" and start == previous_name_end and names:
                    # A doubled backtick ends one span and opens the next: one name.
                    token = next(reversed(names))
                    names[token] += "`" + name
                else:
                    token = f"{prefix}_{len(names)}"
                    names[token] = name
                    before = next((piece[-1] for piece in reversed(out) if piece), "")
                    after = part[end : end + 1]
                    left = " " if before and _is_word_char(before) else ""
                    right = " " if after and _is_word_char(after) else ""
                    out.append(f"{left}{token}{right}")
                previous_name_end = end
            elif (
                opener[0] == "$"
                and end - start >= 2 * len(opener)
                and opens_routine_body("".join(out[-4:]))
            ):
                body = part[start + len(opener) : end - len(opener)]
                out.append(" " + read(body) + " ")
            else:
                out.append("?")
        out.append(part[last:])
        return "".join(out)

    return ColumnText(read(text), names)


# The escapes a live query writes in a statement's text so it stays on one line of the
# client's output (#1874, #1880): each backslash doubled first, then CR, LF and tab.
_ESCAPED = {"\\": "\\", "r": "\r", "n": "\n", "t": "\t"}
_ESCAPE_RE = re.compile(r"\\(.)", re.DOTALL)


def decode_query_text(text: str) -> str:
    """The statement a live query wrote with escapes (``sqlserver_tools``, ``oracle_tools``).

    The query writes each backslash as ``\\\\`` first, then CR, LF and tab as ``\\r``,
    ``\\n`` and ``\\t``, so reading the escapes left to right gives the text back. A
    backslash before any other character, or a last one with nothing after it, is kept as
    it is. Call it once, on the live row's text only: an offline collection's text is
    JSON and was never encoded.
    """
    return _ESCAPE_RE.sub(lambda m: _ESCAPED.get(m.group(1), m.group(0)), text)


def flattened(text: str) -> str:
    """``text`` with CR, LF and tab as spaces: the form the live SQL Server and Oracle
    queries read before #1874 and #1880, which their text-hash fallback ids hash."""
    return text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
