# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The columns a live collector reads from a WHERE clause, in linear time (#1009).

The collectors for MySQL and PostgreSQL read ``filter_columns`` from the query
text with ``WHERE\\s+.*?`?(\\w+)`?\\s*(?:=|<|>|!=|LIKE|IN|BETWEEN|IS)``
(``findall``). The lazy gap is retried from every ``WHERE``: a long text with
many ``WHERE`` and no operator after them rescanned the rest of its line once
per ``WHERE`` (quadratic, the slowest of the query-text detectors in #1009).

:func:`where_columns` answers exactly what that regex did, in one pass:

- the gap ``.*?`` stays on one line (no DOTALL), so each ``WHERE`` looks along
  the line that follows it;
- the group can only start at a word's first character: starting later in a
  word gives a subset of the readings (the operator may also begin inside the
  word, ``abIN`` reads ``ab``), so a word that fails from its start fails from
  every later start;
- the reading of a word does not depend on the ``WHERE`` before it, so once a
  line has been scanned to its end with no reading, a later ``WHERE`` on that
  line has none either.
"""

from __future__ import annotations

import re

_WHERE_RE = re.compile(r"WHERE\s+", re.IGNORECASE)
_WORD_RE = re.compile(r"\w+")
_SPACE_RE = re.compile(r"\s*")
_OPERATOR_RE = re.compile(r"=|<|>|!=|LIKE|IN|BETWEEN|IS", re.IGNORECASE)


def _read_word(sql: str, start: int, end: int, quote: str) -> tuple[str, int] | None:
    """The column and the end of the match for the word ``sql[start:end]``, or ``None``.

    The longest reading first, as the regex backtracks: the whole word, then an
    optional closing quote and whitespace, then an operator; failing that, a
    shorter prefix of the word that is followed by an operator word.
    """
    q = end + len(quote) if sql.startswith(quote, end) else end
    operator = _OPERATOR_RE.match(sql, _SPACE_RE.match(sql, q).end())  # type: ignore[union-attr]
    if operator:
        return sql[start:end], operator.end()
    for cut in range(end - 1, start, -1):
        operator = _OPERATOR_RE.match(sql, cut)
        if operator:
            return sql[start:cut], operator.end()
    return None


def where_columns(sql: str, quote: str) -> list[str]:
    """Each column the WHERE regex found in ``sql``, in order, with ``quote`` as the
    identifier quote (a backtick for MySQL, a double quote for PostgreSQL)."""
    found: list[str] = []
    pos = 0
    scanned_line_end = -1  # the end of a line already scanned to its end with no reading
    while (where := _WHERE_RE.search(sql, pos)) is not None:
        start = where.end()
        newline = sql.find("\n", start)
        line_end = len(sql) if newline < 0 else newline
        pos = start
        if line_end == scanned_line_end:
            continue
        reading = None
        k = start
        while reading is None and (word := _WORD_RE.search(sql, k, line_end)) is not None:
            reading = _read_word(sql, word.start(), word.end(), quote)
            k = word.end()
        if reading is None:
            scanned_line_end = line_end
            continue
        found.append(reading[0])
        pos = reading[1]
    return found
