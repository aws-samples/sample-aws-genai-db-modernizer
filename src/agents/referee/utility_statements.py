"""Utility and metadata statements are excluded from engine routing (#327).

``SHOW``, ``SET``, ``DESCRIBE``/``DESC``, ``EXPLAIN``, DDL (``CREATE``/``ALTER``/
``DROP`` — including ``CREATE EXTENSION``), transaction control (``BEGIN``/
``COMMIT``/``ROLLBACK``/``SAVEPOINT``/``RELEASE``), ``TRUNCATE``, and catalog
lookups (``information_schema``, Postgres ``pg_catalog``) are
database-administration traffic, not an application access pattern a target
engine needs to serve. A purpose-built engine like DynamoDB has no concept of
a column list to ``SHOW`` or a session variable to ``SET``, so routing these
through the normal confidence-scoring path can hand them to an engine that can
never run them -- exactly what happened with ``SHOW FULL FIELDS FROM
wp_options`` and ``SET SESSION SQL_BIG_SELECTS = ?`` landing on DynamoDB, and
``CREATE EXTENSION IF NOT EXISTS pg_trgm`` landing on OpenSearch. A statement
can open with a comment (``/* ... */`` or ``-- ...``) before the verb that
would otherwise match, so leading comments are stripped first.

A tableless catalog call is the same kind of statement with no leading verb at
all: ``SELECT obj_description($1::regclass::oid, $2)``,
``SELECT pg_get_serial_sequence($1, $2)``, ``SELECT setval($1, $2, $3)`` and
``SELECT $1::regtype::oid`` are Postgres catalog introspection and sequence
housekeeping, not an application read or write -- the collector itself can't
even name a source table for them (``tables_accessed`` comes back
``["unknown"]``, :data:`src.agents.referee.table_resolution.PSEUDO_TABLES`).
A catalog function or ``::reg*`` cast only counts as utility when there is no
real source table, since a legitimate application query could in principle
call one of these alongside a real ``FROM``. A query whose ``tables_accessed``
are *all* system catalog tables (``pg_class``, ``pg_type``, ``pg_proc``,
``pg_matviews``, ...) is a utility statement even without the
``pg_catalog.`` prefix or a catalog function call -- these are introspection
queries written straight against the catalog's own tables (seen in
ActiveRecord/Rails schema dumping on the discourse sample).

Rather than dropping these from the assignment (which would change every
downstream artifact that counts "every collected query"), they are kept on the
source-compatible relational engine: the one engine that already understands
DDL and session semantics, and the one an operator would run them against by
hand anyway.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from src.agents.referee.table_resolution import PSEUDO_TABLES

# A leading ``/* block */`` or ``-- line`` comment, repeated, before the verb
# that actually marks the statement. Stripped before any verb match so a
# commented-out utility statement (``/* schema dump */ SHOW TABLES``) is still
# caught.
_LEADING_COMMENT_RE = re.compile(r"^(?:\s*(?:/\*.*?\*/|--[^\n]*\n))+", re.DOTALL)

# A leading SQL verb that marks a statement as DDL, transaction control or
# session/administration traffic rather than application read/write workload.
# Anchored to the start of the (comment-stripped) statement so this never
# matches, say, a column named "show".
_UTILITY_VERB_RE = re.compile(
    r"^\s*(SHOW|SET|DESCRIBE|DESC|EXPLAIN|CREATE|ALTER|DROP|TRUNCATE|"
    r"BEGIN|COMMIT|ROLLBACK|SAVEPOINT|RELEASE)\b",
    re.IGNORECASE,
)

# Catalog/metadata lookups anywhere in the statement — information_schema and
# the Postgres system catalog are schema introspection, not workload traffic,
# regardless of the leading verb (e.g. a SELECT against information_schema).
_CATALOG_RE = re.compile(r"\b(information_schema|pg_catalog)\b", re.IGNORECASE)

# Postgres catalog/introspection functions and sequence housekeeping calls
# (ActiveRecord/Rails schema dumping uses these heavily). Only treated as a
# utility statement when the query has no real source table (see below).
_CATALOG_FUNCTION_RE = re.compile(
    r"\b(obj_description|pg_get_serial_sequence|pg_get_expr|pg_get_indexdef|"
    r"pg_get_constraintdef|format_type|generate_subscripts|pg_typeof|"
    r"setval|nextval|currval)\s*\(",
    re.IGNORECASE,
)

# A cast to a Postgres object-identifier type (regclass, regtype, regproc, ...)
# -- resolving a catalog OID, not application data.
_CATALOG_CAST_RE = re.compile(
    r"::\s*reg(class|type|proc|procedure|oper|operator|namespace|role|config|dictionary)\b",
    re.IGNORECASE,
)

# System catalog tables/schemas (Postgres pg_catalog contents, information_schema)
# a query can name directly in FROM without the pg_catalog. prefix.
_SYSTEM_CATALOG_TABLE_RE = re.compile(r"^(pg_[a-z_]+|information_schema(\..+)?)$", re.IGNORECASE)


def _strip_leading_comments(text: str) -> str:
    return _LEADING_COMMENT_RE.sub("", text)


def _has_no_real_source_table(tables_accessed: Iterable[str] | None) -> bool:
    """True when every table ``tables_accessed`` names is a pseudo table (or there are none)."""
    tables = list(tables_accessed or [])
    return all(t in PSEUDO_TABLES for t in tables)


def _all_tables_are_system_catalog(tables_accessed: Iterable[str] | None) -> bool:
    """True when ``tables_accessed`` is non-empty and every entry is a system catalog table."""
    tables = [str(t).strip() for t in (tables_accessed or []) if t]
    if not tables:
        return False
    return all(_SYSTEM_CATALOG_TABLE_RE.match(t) for t in tables)


def is_utility_statement(
    query_text: str | None, tables_accessed: Iterable[str] | None = None
) -> bool:
    """Whether ``query_text`` is a utility/metadata/DDL statement, not workload traffic.

    True for ``SHOW``/``SET``/``DESCRIBE``/``EXPLAIN``/``CREATE``/``ALTER``/``DROP``/
    ``TRUNCATE``/``BEGIN``/``COMMIT``/``ROLLBACK``/``SAVEPOINT``/``RELEASE``
    statements (a leading comment is stripped first), for any statement that
    reads ``information_schema`` or ``pg_catalog`` regardless of its leading
    verb, for a query whose ``tables_accessed`` are all system catalog tables,
    and for a catalog function call or ``::reg*`` cast when ``tables_accessed``
    names no real application table.
    """
    text = _strip_leading_comments(query_text or "")
    if _UTILITY_VERB_RE.search(text) or _CATALOG_RE.search(text):
        return True
    if _all_tables_are_system_catalog(tables_accessed):
        return True
    if _CATALOG_FUNCTION_RE.search(text) or _CATALOG_CAST_RE.search(text):
        return _has_no_real_source_table(tables_accessed)
    return False
