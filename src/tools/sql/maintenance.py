"""The tables a maintenance statement names, for the statements sqlglot cannot read (#483).

sqlglot reads ``ALTER TABLE``, ``DROP TABLE``, ``TRUNCATE``, ``ANALYZE``,
``DESCRIBE`` and ``COMMENT ON TABLE`` itself (:mod:`src.tools.sql.facade`). It reads
the maintenance statements below only as a generic command, or not at all, and the
regex scan finds no ``FROM``/``JOIN``/``INTO`` in them, so their tables used to come
out as ``["unknown"]`` (or, for ``UPDATE STATISTICS t``, as a table named
``STATISTICS``):

- T-SQL ``ALTER INDEX {index | ALL} ON <table> ...`` and ``UPDATE STATISTICS <table>``;
- MySQL/MariaDB ``OPTIMIZE | CHECK | REPAIR | CHECKSUM [LOCAL] TABLE <t>[, <t>...]``
  and ``LOCK TABLES <t> {READ | WRITE}[, ...]``;
- PostgreSQL ``VACUUM [(options)] [FULL] [FREEZE] [VERBOSE] [ANALYZE] [<table>]``,
  ``REINDEX [(options)] TABLE [CONCURRENTLY] <table>``, ``CLUSTER [VERBOSE] <table>
  [USING <index>]`` and ``LOCK [TABLE] <t>[, ...] IN <mode> MODE`` (Oracle has the
  same ``LOCK TABLE``).

The match is on the leading verb only, after leading comments (the shared comment
rule, :mod:`src.shared.sql_comments`), so a column named ``cluster`` or a quoted
``'VACUUM t'`` literal is not a maintenance statement. A bare ``VACUUM`` or
``CLUSTER`` names no table. A name is bare or quoted with backticks, double quotes
or brackets, with up to two qualifiers, as in :mod:`src.tools.sql.bulk_load`.
"""

from __future__ import annotations

import re

from src.tools.sql.bulk_load import _IDENT, _NAME, _statement, _target

__all__ = ["maintenance_tables"]

_OPTIONS = r"\([^()]*\)"
_END = r"(?:\s+|$|;)"

# Statements naming one table: the verb, then the name.
_ONE_TABLE = (
    re.compile(r"ALTER\s+INDEX\s+(?:ALL|" + _IDENT + r")\s+ON\s+" + _NAME, re.I),
    re.compile(r"UPDATE\s+STATISTICS\s+" + _NAME, re.I),
    re.compile(r"REINDEX\s+(?:" + _OPTIONS + r"\s*)?TABLE\s+(?:CONCURRENTLY\s+)?" + _NAME, re.I),
    re.compile(
        r"VACUUM\s+(?:" + _OPTIONS + r"\s*|(?:FULL|FREEZE|VERBOSE|ANALYZE|ANALYSE)" + _END + r")*"
        r"(?!(?:FULL|FREEZE|VERBOSE|ANALYZE|ANALYSE)\b)" + _NAME,
        re.I,
    ),
    re.compile(
        r"CLUSTER\s+(?:" + _OPTIONS + r"\s*|VERBOSE" + _END + r")*"
        r"(?!VERBOSE\b)" + _NAME + r"\s*(?:USING\b|;|$)",
        re.I,
    ),
)

# Statements naming a list of tables: the verb, then comma-separated entries that each
# start with the table (a lock type, an alias or a lock mode may follow it).
_TABLE_LIST = (
    re.compile(
        r"(?:OPTIMIZE|CHECK|REPAIR|CHECKSUM)\s+(?:(?:NO_WRITE_TO_BINLOG|LOCAL)\s+)?TABLE\s+", re.I
    ),
    re.compile(r"LOCK\s+(?:TABLES?\s+|(?!INSTANCE\b))(?:ONLY\s+)?", re.I),
)
_FIRST_NAME = re.compile(_NAME, re.I)


def _table_list(text: str, verb: re.Match[str]) -> list[tuple[list[str], str]]:
    refs: list[tuple[list[str], str]] = []
    pos = verb.end()
    while True:
        name = _FIRST_NAME.match(text, pos)
        if name is None:
            break
        refs.append(_target(name))
        # Skip the entry's alias or lock type up to the next top-level comma.
        depth = 0
        pos = name.end()
        while pos < len(text) and not (text[pos] == "," and depth == 0) and text[pos] != ";":
            depth += (text[pos] == "(") - (text[pos] == ")")
            pos += 1
        if pos >= len(text) or text[pos] != ",":
            break
        pos += 1
        while pos < len(text) and text[pos].isspace():
            pos += 1
    return refs


def maintenance_tables(sql: str, source_engine: str = "") -> list[tuple[list[str], str]] | None:
    """The ``(qualifiers, table)`` references of a maintenance statement, or None.

    None when ``sql`` is not one of the statements in the module docstring; an empty
    list is not returned (a bare ``VACUUM`` is None too).
    """
    text = _statement(sql, source_engine)
    for pattern in _ONE_TABLE:
        match = pattern.match(text)
        if match:
            return [_target(match)]
    for pattern in _TABLE_LIST:
        match = pattern.match(text)
        if match:
            return _table_list(text, match) or None
    return None
