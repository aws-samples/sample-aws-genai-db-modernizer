"""Bulk-load statements: the table they load and the fact that they write (#777).

Two statements load rows from a file or the client stream straight into one
table:

- PostgreSQL ``COPY <table> [(<columns>)] FROM {STDIN | '<file>' | PROGRAM '<cmd>'} ...``;
- MySQL/MariaDB ``LOAD {DATA | XML} [LOW_PRIORITY | CONCURRENT] [LOCAL] INFILE
  '<file>' [REPLACE | IGNORE] INTO TABLE <table> ...`` (a digest writes the
  file name as ``?``).

Both are application bulk I/O, not utility statements (#774): the collectors
type them as an ``INSERT`` (as a MySQL ``REPLACE`` already is) and attribute
them to that table, on every dialect. ``COPY <table> TO ...`` and
``COPY (<query>) TO ...`` export rows; they are reads, not bulk loads.

The match is on the leading verb only, after leading comments (the shared
comment rule, :mod:`src.shared.sql_comments`), so a column named ``copy_id``
or a quoted ``'COPY ...'`` literal is not a bulk load. The table name is
bare or quoted with backticks, double quotes or brackets, with up to two
qualifiers; a quoted name is read whole, so it may hold spaces, hyphens or
dots. Block comments between the ``LOAD DATA`` keywords (an optimizer hint)
are skipped. Only the first statement of the text is read: in ``COPY t FROM
'/x'; SELECT 1 FROM z`` the answer is ``t`` alone.
"""

from __future__ import annotations

import re

from src.shared.sql_comments import block_comments_nest, strip_leading_comments

__all__ = ["bulk_load_table", "copy_table", "is_bulk_load"]

# One identifier: bare, or a whole quoted name (double quotes with ``""``
# escapes, backticks, brackets), so ``"my table"`` and `` `my-table` `` are one
# name and a dot inside quotes is not a qualifier.
_IDENT = r'(?:"(?:[^"]|"")+"|`[^`]+`|\[[^\]]+\]|\w+)'
_NAME = (
    r"(?:(?P<qual1>" + _IDENT + r")\s*\.\s*)?"
    r"(?:(?P<qual2>" + _IDENT + r")\s*\.\s*)?"
    r"(?P<table>" + _IDENT + r")"
)

# COPY <name> [(<columns>)] {FROM | TO}. ``COPY (<query>) TO`` starts with a
# parenthesis, so it never matches.
_COPY_RE = re.compile(r"COPY\s+" + _NAME + r"\s*(?:\([^()]*\)\s*)?(?P<direction>FROM|TO)\b", re.I)

# A string literal (either quote, doubled or backslash-escaped quotes) or a
# digest placeholder, so a file name can never be read as SQL.
_FILE = r"""(?:'(?:[^'\\]|\\.|'')*'|"(?:[^"\\]|\\.|"")*"|\?)"""
# Whitespace and block comments (an optimizer hint such as ``/*+ x */``)
# between the keywords. The comment body is the standard unrolled C-comment
# pattern, so it never backtracks.
_GAP = r"(?:\s|/\*[^*]*\*+(?:[^/*][^*]*\*+)*/)+"
_OPT_GAP = r"(?:\s|/\*[^*]*\*+(?:[^/*][^*]*\*+)*/)*"
_LOAD_RE = re.compile(
    r"LOAD"
    + _GAP
    + r"(?:DATA|XML)"
    + _GAP
    + r"(?:(?:LOW_PRIORITY|CONCURRENT)"
    + _GAP
    + r")?(?:LOCAL"
    + _GAP
    + r")?"
    r"INFILE"
    + _OPT_GAP
    + _FILE
    + _OPT_GAP
    + r"(?:(?:REPLACE|IGNORE)"
    + _GAP
    + r")?INTO"
    + _GAP
    + r"TABLE"
    + _GAP
    + _NAME,
    re.I,
)


def _statement(sql: str, source_engine: str) -> str:
    return strip_leading_comments(
        sql or "",
        hash_comments=True,
        strip_following_space=True,
        nested=block_comments_nest(source_engine),
    ).lstrip()


def _unquote(name: str) -> str:
    if name[:1] == '"':
        return name[1:-1].replace('""', '"')
    if name[:1] in "`[":
        return name[1:-1]
    return name


def _target(match: re.Match[str]) -> tuple[list[str], str]:
    qualifiers = [_unquote(q) for q in (match.group("qual1"), match.group("qual2")) if q]
    return qualifiers, _unquote(match.group("table"))


def copy_table(sql: str, source_engine: str = "") -> tuple[list[str], str] | None:
    """The table of a ``COPY <table> FROM|TO`` statement as ``(qualifiers, table)``.

    None for anything else, ``COPY (<query>) TO`` included (its query names
    the tables).
    """
    match = _COPY_RE.match(_statement(sql, source_engine))
    return _target(match) if match else None


def bulk_load_table(sql: str, source_engine: str = "") -> tuple[list[str], str] | None:
    """The table a bulk load writes, as ``(qualifiers, table)``; None if ``sql``
    is not a bulk load (``COPY ... FROM`` or ``LOAD DATA``/``LOAD XML``)."""
    text = _statement(sql, source_engine)
    copy = _COPY_RE.match(text)
    if copy:
        return _target(copy) if copy.group("direction").upper() == "FROM" else None
    load = _LOAD_RE.match(text)
    return _target(load) if load else None


def is_bulk_load(sql: str, source_engine: str = "") -> bool:
    """Whether ``sql`` loads rows into a table (``COPY ... FROM``, ``LOAD DATA``,
    ``LOAD XML``): a write to :func:`bulk_load_table`."""
    return bulk_load_table(sql, source_engine) is not None
