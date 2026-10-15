"""The regex query facts: the facade's fallback when sqlglot can't read a statement.

These are the scans the collectors ran on raw query text before #796, moved
here unchanged so the facade (:func:`src.tools.sql.facade.sql_facts`) falls
back to exactly the same answer when it can't parse a statement, the way
:mod:`src.tools.sql.regex_tables` backs its table references. They read
comments, string literals and column names as SQL (a column named ``count``
is an aggregate, ``user_selectable`` is a second ``SELECT``, a ``-- join``
comment is a join), which is why the facade reads the syntax tree first.

- ``has_joins``: the text contains `` join `` (surrounded by spaces);
- ``join_count``: the number of ``join`` words;
- ``has_aggregation``: a ``count``/``sum``/``avg``/``min``/``max`` word or
  ``GROUP BY`` anywhere;
- ``has_subquery``: more than one ``select`` substring;
- ``wildcard_leading``: ``LIKE`` followed by a quote and ``%`` (the
  capability gate's and the DynamoDB exclusion's own detector, which reads
  ``ILIKE`` and ``NOT LIKE`` too).
"""

from __future__ import annotations

import re

_JOIN_WORD_RE = re.compile(r"\bjoin\b", re.IGNORECASE)
_AGGREGATION_WORD_RE = re.compile(r"\b(count|sum|avg|min|max|group\s+by)\b", re.IGNORECASE)
_LEADING_WILDCARD_RE = re.compile(r"LIKE\s+['\"]%", re.IGNORECASE)


def regex_has_joins(query_text: str) -> bool:
    """`` join `` with a space on each side, case-insensitive."""
    return " join " in (query_text or "").lower()


def regex_join_count(query_text: str) -> int:
    """How many times the word ``join`` appears."""
    return len(_JOIN_WORD_RE.findall(query_text or ""))


def regex_has_aggregation(query_text: str) -> bool:
    """An aggregate function name or ``GROUP BY`` anywhere in the text."""
    return bool(_AGGREGATION_WORD_RE.search(query_text or ""))


def regex_has_subquery(query_text: str) -> bool:
    """More than one ``select`` substring, case-insensitive."""
    return (query_text or "").lower().count("select") > 1


def regex_wildcard_leading(query_text: str) -> bool:
    """``LIKE '%...`` / ``LIKE "%...`` (``ILIKE``, ``NOT LIKE`` included)."""
    return bool(_LEADING_WILDCARD_RE.search(query_text or ""))
