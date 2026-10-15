"""The regex table-reference extractor for offline collections.

This is the scan ``offline_parser._transform_queries`` ran before #553, moved
here unchanged so the sqlglot facade (:mod:`src.tools.sql.facade`) can fall
back to exactly the same answer when it can't parse a statement (#557), and
so the cross-check (:mod:`src.tools.sql.shadow`) can compare the two. It
returns references as written (``[database.]schema.table``); resolving them
to collection table ids stays with the caller.

What it matches:

- ``FROM``/``JOIN``/``INTO``/``UPDATE`` followed by a bare, backtick-,
  double-quote- or bracket-quoted name, with up to two qualifiers
  (``schema.table``, T-SQL's ``database.schema.table``);
- ``REFRESH MATERIALIZED VIEW [CONCURRENTLY] <name>`` (PostgreSQL);
- ``MERGE [INTO] <target> USING <source>`` (SQL Server, Oracle, PostgreSQL
  15+; #663). ``USING`` names a table only in a MERGE: elsewhere it is
  ``JOIN ... USING (col)`` or ``CREATE INDEX ... USING gin``.

A keyword must start a word and not be a parameter or variable name: T-SQL's
``@From`` or a column such as ``valid_from`` followed by ``AND`` is not a
FROM clause (#663). Upsert clauses that use ``UPDATE`` for a column
assignment are skipped (#552), and ``FROM``/``JOIN`` references to a
``WITH``-defined CTE name are dropped (#682).
"""

from __future__ import annotations

import re

from src.tools.database.sql_cte import drop_cte_references

# Leading ``--`` line and ``/* */`` block comments before a statement's verb.
_LEADING_COMMENTS_RE = re.compile(r"^(?:\s*(?:--[^\n]*(?:\n|$)|/\*.*?\*/))+", re.DOTALL)


def is_merge_statement(query_text: str) -> bool:
    """Whether ``query_text`` is a MERGE (#663): its first verb, after any leading
    comments, is MERGE, or it is a ``WITH ... AS (...) MERGE`` (a CTE feeding it)."""
    text = _LEADING_COMMENTS_RE.sub("", query_text).lstrip()
    if re.match(r"MERGE\b", text, re.I):
        return True
    return bool(re.match(r"WITH\b", text, re.I) and re.search(r"\)\s*MERGE\b", text, re.I))


def regex_table_refs(query_text: str) -> list[tuple[list[str], str]]:
    """Every table reference in ``query_text`` as ``(qualifiers, table)``, in order.

    ``qualifiers`` holds zero, one (``schema``) or two (``database``,
    ``schema``) names. Duplicates are kept: the caller resolves and dedupes.
    """
    is_merge = is_merge_statement(query_text)
    using = r"|USING" if is_merge else ""
    table_re = re.compile(
        r"(?<![\w@$#])"
        r"(?:FROM|JOIN|INTO|UPDATE|MERGE(?:\s+INTO)?"
        r"|REFRESH\s+MATERIALIZED\s+VIEW(?:\s+CONCURRENTLY)?" + using + r")\s+"
        r'(?:[`"\[]?(?P<qual1>\w+)[`"\]]?\s*\.\s*)?'
        r'(?:[`"\[]?(?P<qual2>\w+)[`"\]]?\s*\.\s*)?'
        r'[`"\[]?(?P<table>\w+)[`"\]]?',
        re.I,
    )
    # Upsert clauses use UPDATE for a column assignment, not a table reference:
    # MySQL's ON DUPLICATE KEY UPDATE, PostgreSQL's DO UPDATE, and, in a MERGE
    # only, WHEN MATCHED THEN UPDATE SET (#663). Outside a MERGE, a procedural
    # ``IF ... THEN UPDATE orders SET ...`` is a real update of ``orders``.
    upsert_update = r"\bON\s+DUPLICATE\s+KEY\s+UPDATE\b|\bDO\s+UPDATE\b"
    if is_merge:
        upsert_update += r"|\bTHEN\s+UPDATE\b"
    scanned_query = re.sub(upsert_update, " ", query_text, flags=re.I)
    scanned_query = drop_cte_references(scanned_query)  # query-local names (#682)
    refs: list[tuple[list[str], str]] = []
    for m in table_re.finditer(scanned_query):
        table = m.group("table")
        if not table:
            continue
        qualifiers = [q for q in (m.group("qual1"), m.group("qual2")) if q]
        refs.append((qualifiers, table))
    return refs
