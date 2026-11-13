# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The one ``query_type`` rule for every collector (#1178).

A collected statement's ``query_type`` (``SELECT``, ``INSERT``, ``UPDATE``,
``DELETE``, ``MERGE``, ``REPLACE`` or ``OTHER``) is its first verb once the comments
it opens with are gone. Before #1178 six copies decided it -- the offline parser's
regex and ``_extract_query_type`` in the MySQL, PostgreSQL, SQL Server, Oracle and
RDS tools -- and none skipped a leading comment, so a tagged statement
(``-- endpoint: CartController.Add\\nSELECT ...``) was ``OTHER`` although the SQL
facade read it as a ``select``, and lost every signal that reads ``query_type``
(key access, the cache overlay, access-pattern classes, triage).

Leading comments follow the shared comment rule (:mod:`src.shared.sql_comments`),
in the source dialect's own way: block comments nest for PostgreSQL and SQL Server
(:func:`~src.shared.sql_comments.block_comments_nest`), and MySQL and MariaDB also
have ``#`` line comments and executable comments (``/*! ... */``, which are code, so
a statement that opens with one is not stripped past it). ``REPLACE`` is a statement
only in the MySQL family (an engine the facade has no dialect for keeps it). A bulk load (``COPY ... FROM``,
``LOAD DATA``) writes rows into its table, so it is an ``INSERT`` (#777).

A statement that opens with a verb is typed by that verb. A statement that opens with
parentheses around a ``SELECT`` -- a parenthesized query expression, or a set operation
over them (``(SELECT ...) UNION (SELECT ...)``, nested or followed by ``ORDER BY`` /
``LIMIT``) -- is a ``SELECT``, the same as the unparenthesized statement (#2159), when
the facade reads it as a query in the source dialect (``(SELECT 1) = 1`` is a bare
expression, not one). Comments between the parentheses follow the same dialect rule.
Only a query expression is ever wrapped, so a wrapped ``WITH`` or DML verb stays
``OTHER``. A ``WITH ...`` statement is
typed by the facade's statement kind (:class:`src.tools.sql.facade.AccessFacts`, read
in the source dialect) when that kind is a write -- ``WITH ... INSERT/UPDATE/DELETE/
MERGE`` (#1287). Otherwise it stays ``OTHER``: the facade calls a PostgreSQL
data-modifying CTE that ends in ``SELECT`` (``WITH w AS (INSERT ...) SELECT ...``) a
``select``, which a write check must never read as a read, so ``is_write_query``
re-reads the text of an ``OTHER`` statement. For a statement that opens with a verb
the two agree (``tests/unit/tools/sql/test_query_type.py``).
"""

from __future__ import annotations

import re

from src.shared.sql_comments import block_comments_nest, strip_leading_comments
from src.tools.sql.bulk_load import is_bulk_load
from src.tools.sql.facade import access_facts, sqlglot_dialect

__all__ = ["statement_query_type"]

_VERB_RE = re.compile(r"(SELECT|INSERT|UPDATE|DELETE|MERGE|REPLACE)\b", re.IGNORECASE)
_WITH_RE = re.compile(r"WITH\b", re.IGNORECASE)
_SELECT_RE = re.compile(r"SELECT\b", re.IGNORECASE)
_WRITE_KINDS = frozenset({"insert", "update", "delete", "merge"})


def _strip_comments(text: str, engine: str, dialect: str) -> str:
    mysql_family = dialect == "mysql"
    return strip_leading_comments(
        text,
        hash_comments=mysql_family,
        strip_following_space=True,
        nested=block_comments_nest(engine),
        mysql_executable=mysql_family,
        mysql_dashes=mysql_family,
    ).lstrip()


def _opens_a_parenthesized_select(text: str, engine: str, dialect: str) -> bool:
    """Whether ``text`` opens with parentheses whose innermost one opens a ``SELECT``,
    and the facade reads the whole text as a query (#2159): an expression such as
    ``(SELECT 1) = 1`` is no statement. Text the facade cannot parse (a digest cut
    short) is read as a ``select``, as the unparenthesized statement is."""
    inner = text
    while inner.startswith("("):
        inner = _strip_comments(inner[1:], engine, dialect)
    if inner is text or not _SELECT_RE.match(inner):
        return False
    facts = access_facts(text, engine)
    return facts.kind == "select" and facts.fallback_reason != "not_a_statement"


def statement_query_type(sql: str, source_engine: str) -> str:
    """``sql``'s ``query_type``, read in ``source_engine``'s dialect (see the module
    docstring). Never raises; empty or comment-only text is ``OTHER``."""
    engine = (source_engine or "").strip().lower()
    # The facade's dialect for the engine: "mysql" for MySQL and MariaDB, "" for an
    # engine it has no dialect for.
    dialect = sqlglot_dialect(engine)
    text = _strip_comments(sql or "", engine, dialect)
    match = _VERB_RE.match(text)
    if match:
        verb = match.group(1).upper()
        # REPLACE is a MySQL-family statement; an engine with no dialect keeps it, as
        # the RDS tools' copy did before #1178.
        if verb != "REPLACE" or dialect in ("mysql", ""):
            return verb
    # SQL Server needs the statement before a CTE ended with ``;``, so a script
    # writes ``;WITH ...``: the leading semicolons are not part of the statement.
    cte_text = text.lstrip("; \t\r\n") if dialect == "tsql" else text
    if _WITH_RE.match(cte_text):
        kind = access_facts(cte_text, engine).kind
        if kind in _WRITE_KINDS:
            return kind.upper()
        return "OTHER"
    if _opens_a_parenthesized_select(text, engine, dialect):
        return "SELECT"
    return "INSERT" if is_bulk_load(sql or "", engine) else "OTHER"
