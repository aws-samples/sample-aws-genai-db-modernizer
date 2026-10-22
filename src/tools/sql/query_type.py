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

A statement that opens with a verb is typed by that verb. A ``WITH ...`` statement is
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
_WRITE_KINDS = frozenset({"insert", "update", "delete", "merge"})


def statement_query_type(sql: str, source_engine: str) -> str:
    """``sql``'s ``query_type``, read in ``source_engine``'s dialect (see the module
    docstring). Never raises; empty or comment-only text is ``OTHER``."""
    engine = (source_engine or "").strip().lower()
    # The facade's dialect for the engine: "mysql" for MySQL and MariaDB, "" for an
    # engine it has no dialect for.
    dialect = sqlglot_dialect(engine)
    mysql_family = dialect == "mysql"
    text = strip_leading_comments(
        sql or "",
        hash_comments=mysql_family,
        strip_following_space=True,
        nested=block_comments_nest(engine),
        mysql_executable=mysql_family,
    ).lstrip()
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
    return "INSERT" if is_bulk_load(sql or "", engine) else "OTHER"
