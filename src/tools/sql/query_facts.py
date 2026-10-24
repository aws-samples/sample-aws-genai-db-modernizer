"""Engine-neutral query facts read from SQL text: one detector per fact (#491).

A detector here reports a fact about one query -- never a score, never an
engine name, never a capability. Engines differ only in how a fact is turned
into support or a score (the capability matrix in
``src.agents.referee.capability_registry``, the exclusion rules in
``src.agents.referee.engine_exclusions``), so the same query reads the same
way whichever engine is being scored against it, and a fact is defined once
instead of once per engine module (#485 traced a routing bug to two copies of
the leading-wildcard check that had drifted apart).

Each fact has one definition, and every reader uses it (#893; the guard is
``tests/unit/tools/sql/test_one_definition_per_fact.py``). Two kinds:

- Parsed facts -- join count, ``GROUP BY``, aggregate functions, the text
  searches (leading-wildcard and parameterised ``LIKE``, full-text,
  regular-expression and similarity matches), the window functions, a
  recursive CTE, ``ORDER BY``, the row limit and top-N shape, a timestamp
  column and JSON access -- are the sqlglot facade's (:class:`src.tools.sql.facade.SqlFacts`),
  read in the source engine's dialect, so a comment, a string literal or a
  column name (``count``, ``rank``) is never read as SQL. When sqlglot can't
  read a statement, each is its regex fallback
  (:mod:`src.tools.sql.regex_facts`). The collectors store some of them on each
  query pattern (``has_joins``, ``join_count``, ``has_aggregations``,
  ``has_subqueries``, #796), the same facts read once.
- Text facts are a regex scan on the text: the ones no parse gives yet (a
  session key, a write statement, a computed join key, ...), each moved here
  from its detector by #491. Each reads the text as the source engine runs it
  (``sql_spans.code_text``, #1415): comments blanked, each string literal a
  ``?``, quoted identifiers kept, so a keyword inside a literal or a comment
  is never SQL.
- The timestamp column (#935) is the one fact that also reads the schema: a
  column is a timestamp when the collection says its type is one
  (:func:`is_timestamp_type`), whatever its name. The facade's columns
  (``SqlFacts.columns``) are resolved to their tables through the collection's
  table names (:class:`ColumnTypes`), so a reader passes the collection's types
  (``facts.timestamp_column(types)``); the name list is read when it has none.

A reader passes the collector's source engine
(``metadata.source_database.engine``, :func:`source_engine_of`); a reader with
none gets sqlglot's generic dialect.

:func:`query_facts` returns one object per text, and each fact on it is
computed the first time a reader asks for it, then kept: a query read by
several detectors is scanned once per fact, and a reader never pays for a fact
it doesn't read (#895 review).

The parsed facts come from the readings the collector stored on each query
pattern (``parsed_facts``, :mod:`src.tools.sql.stored_facts`, #934) when a step
has loaded them (:func:`remember_collected_facts`) and they are this build's;
otherwise from one parse of the text. The access-pattern facts
(:meth:`QueryFacts.access_facts`) come from the same reading, so a step reads
each collected query without parsing it again.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from functools import cached_property, lru_cache
from typing import TYPE_CHECKING

from src.shared.sql_comments import strip_leading_comments
from src.shared.sql_spans import (
    POSTGRES_LITERALS_AND_COMMENTS,
    code_text,
    comments_literals_and_identifiers_for,
    opens_routine_body,
    span_dialect,
    spans,
    sub_spans,
)
from src.shared.unsupported_pattern import is_dedup_only_group_by, strip_comments_and_literals
from src.tools.sql.facade import AccessFacts, ColumnUse, SqlFacts, TableRef, query_readings
from src.tools.sql.regex_facts import (
    EXISTS_CONDITION_RE,
    IN_LIST_RE,
    SELECT_WORD_RE,
    TIMESTAMP_COLUMN_NAME_RE,
    RegexMatch,
    WildcardLike,
    has_match_against,
    output_into_targets,
    regex_range_predicate,
    scan_sql,
)
from src.tools.sql.stored_facts import readings_from_stored

if TYPE_CHECKING:
    from src.agents.referee.table_resolution import TableNameResolver

__all__ = [
    "ColumnTypes",
    "QueryFacts",
    "RegexMatch",
    "TimestampColumnReading",
    "WildcardLike",
    "is_timestamp_type",
    "locate_column",
    "query_facts",
    "scan_sql",
    "source_engine_of",
    "timestamp_column_trace",
]

# ---------------------------------------------------------------------------
# Parsed facts (#893). Each is the sqlglot facade's reading (``SqlFacts``),
# which falls back to its regex fact only when the statement can't be parsed.
# A parse is skipped when the text can't hold the fact at all: each fact's
# hint below is a necessary condition of both readings (a GROUP BY needs the
# word ``group``, a window function the word ``over``, ...), so it never
# changes an answer, it only saves the parse
# (``tests/unit/tools/sql/test_query_facts.py::TestParsedFactHints``).
# ---------------------------------------------------------------------------

# A function-call shape: a name (or a closing quote or bracket of a quoted
# one) followed by ``(``, where the name is not a keyword that takes a
# parenthesised list (``IN (``, ``EXISTS (``, ``VALUES (``, ...). An aggregate
# function is always called this way.
_FUNCTION_CALL_HINT_RE = re.compile(
    r"(?:[`\"\]]|\b(?!(?:in|exists|values|as|on|using|from|join|where|and|or|not|select|"
    r"with|over|by|into|union)\b)[a-z_][\w$]*)\s*\(",
    re.IGNORECASE,
)
# The regex fallback's aggregate words (``src.tools.sql.regex_facts``).
_AGGREGATE_WORD_HINTS = (
    "count",
    "sum",
    "avg",
    "min",
    "max",
    "group_concat",
    "string_agg",
    "array_agg",
)
_JOIN_HINTS = ("join", ",", "apply")
_FULLTEXT_HINTS = ("match", "@@", "tsvector", "tsquery", "ts_rank", "contains", "freetext")
_PATTERN_MATCH_HINTS = ("regexp", "rlike", "~", "similar")
_ROW_LIMIT_HINTS = ("limit", "fetch", "top")
# The timestamp column names (``regex_facts.TIMESTAMP_COLUMN_NAMES``) each hold one;
# a parse is skipped on them only when the collection has no column types.
_TIMESTAMP_HINTS = ("_at", "time", "date", "ttl")
_JSON_HINTS = ("json", "->", "#>", "@>")
# A comparison, or a prefix LIKE (a range on the column).
_RANGE_HINTS = ("between", "<", ">", "like")
# The collectors' name for a full-text match on each source (``text_search_type``).
_FULLTEXT_LABELS = {"postgresql": "tsvector", "postgres": "tsvector", "oracle": "oracle_text"}

# ---------------------------------------------------------------------------
# Aggregation (#338, #336)
#
# An aggregate function call needs real aggregation capability on one table
# just as much as a joined one: DynamoDB has no server-side SUM/COUNT/AVG
# regardless of how many tables feed it. A query with neither GROUP BY nor a
# join (a bare SELECT COUNT(*) FROM t) used to get no capability requirement
# the same way the join/aggregation gap did (#338) -- an aggregate function
# call alone is enough, so this fires independently of GROUP BY or join count.
# FOUND_ROWS()/SQL_CALC_FOUND_ROWS (MySQL's row-count-of-last-query modifier)
# and any window function count too: DynamoDB has no equivalent for any of
# them.
#
# GROUP BY alone, with no aggregate function, is usually a deduplication
# technique (#336) rather than an aggregation. Rather than re-deriving that
# distinction here, ``dedup_only_group_by`` is
# ``src.shared.unsupported_pattern.is_dedup_only_group_by`` (the same
# definition the risk report uses, #371), so the shared function is the single
# thing that decides it: GROUP BY needs aggregation exactly when that function
# says it is not just a dedup.
# ---------------------------------------------------------------------------

_FOUND_ROWS_RE = re.compile(r"\bsql_calc_found_rows\b|`?\bfound_rows\b`?\s*\(", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Computed-expression join (review of #375, finding B2)
#
# A join whose ON condition compares a function call (lower(groups.name) =
# users.username_lower) needs an engine that evaluates expressions as part of
# a join -- a key-value store cannot do that at any stage, v1 or the reality
# check, no matter how few tables are involved. This is a stricter, narrower
# case than "complex_joins" (3+ tables): a 2-table join is otherwise fine for
# DynamoDB (a key-scoped lookup), but not when the join key itself is
# computed.
# ---------------------------------------------------------------------------

# One JOIN ... ON <condition> clause, bounded at the next clause keyword (or
# the end of the statement) so a function call elsewhere in the query (e.g. a
# WHERE clause unrelated to the join) is never mistaken for part of the join
# condition.
_JOIN_WORD_RE = re.compile(r"\bjoin\b", re.IGNORECASE)
_ON_WORD_RE = re.compile(r"\bon\b", re.IGNORECASE)
_SPACE_RE = re.compile(r"\s*")
# Where a join condition ends: the next clause keyword, or the end of the text.
_JOIN_CONDITION_END_RE = re.compile(
    r"\b(?:where|group\s+by|order\s+by|having|left|right|inner|full|cross|join|limit)\b|$",
    re.IGNORECASE,
)


def _join_conditions(text: str) -> Iterator[str]:
    """The condition of each ``JOIN ... ON <condition>`` clause, in order.

    Left to right in linear time (#1009). The regex it replaces,
    ``\\bjoin\\b.*?\\bon\\b\\s*(?P<cond>.*?)(?=<keyword>\\b|$)`` with DOTALL, retried its
    gap from every ``JOIN``, so repeated ``JOIN`` with no ``ON`` was quadratic. A
    ``JOIN`` with no ``ON`` after it means no later ``JOIN`` has one either; and
    each clause ends at its first end keyword, where the next search for a ``JOIN``
    starts.
    """
    pos = 0
    while (join := _JOIN_WORD_RE.search(text, pos)) is not None:
        on = _ON_WORD_RE.search(text, join.end())
        if on is None:
            return
        start = _SPACE_RE.match(text, on.end()).end()  # type: ignore[union-attr]
        end = _JOIN_CONDITION_END_RE.search(text, start)
        stop = end.start() if end else len(text)  # the end of the text always matches
        yield text[start:stop]
        pos = stop


# A column reference -- optionally quoted, optionally table-qualified -- on
# one side of the comparison: "users"."username_lower" or users.username_lower
# or just id. Starting on a letter/underscore (optionally behind a opening
# quote) excludes a bind parameter ("$1", "?"), which starts on "$" or "?":
# a function compared against a parameter ("lower(g.name) = $1") tests a
# computed value against a filter value, not a join on a computed key, so it
# must not match either branch below (a second review of #375 found the
# first version matching this regardless of what followed the operator).
_COLUMN_REF_RE = r'"?[a-z_][a-z0-9_]*"?(?:\."?[a-z_][a-z0-9_]*"?)?'
# A function call: an identifier immediately followed by "(".
_FUNC_CALL_RE = r"[a-z_][a-z0-9_]*\s*\([^()]*\)"
_COMPARISON_OP_RE = r"(?:=|<>|!=|<=|>=|<|>)"
# A function call compared against a column, inside a join condition, on
# either side -- "lower(groups.name) = users.username_lower" (function on
# the left) or "users.username_lower = lower(groups.name)" (function on the
# right, a second review of #375 found this side was never checked). A
# plain "t1.id = t2.id" join condition does not trigger either branch.
_COMPUTED_EXPR_IN_CONDITION_RE = re.compile(
    rf"{_FUNC_CALL_RE}\s*{_COMPARISON_OP_RE}\s*{_COLUMN_REF_RE}\b"
    rf"|{_COLUMN_REF_RE}\s*{_COMPARISON_OP_RE}\s*{_FUNC_CALL_RE}",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

# ``INSERT``/``UPDATE``/``DELETE`` followed later by ``SELECT`` (``SELECT_WORD_RE``):
# two scans, the second from where the first write word ends. One regex
# (``\b(INSERT|UPDATE|DELETE)\b.*\bSELECT\b``, DOTALL) answered the same but retried
# the rest of the text from every write word, quadratic on many of them (#902).
_WRITE_WORD_RE = re.compile(r"\b(?:INSERT|UPDATE|DELETE)\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Relational shape keywords (the Aurora analysis's detectors, moved here from
# ``aurora_common_pattern_catalog``). Case-insensitive checks on the text with its
# comments and literals blanked (``QueryFacts._code``, #1415): they still read column
# names (``limit`` in ``credit_limit``), which is why they are kept apart from the
# parsed facts.
# ---------------------------------------------------------------------------

# EXISTS (subquery) or NOT EXISTS (subquery) is ``regex_facts.EXISTS_CONDITION_RE``, the
# condition the transaction shape blanks too (#930); IN (SELECT ...) is this one.
_IN_SUBQUERY_RE = re.compile(r"\bIN\s*\(\s*SELECT\b", re.IGNORECASE)

# ---------------------------------------------------------------------------
# MySQL syntax (the Aurora MySQL analysis's detectors, moved here from
# ``aurora_mysql_pattern_catalog``): case-insensitive substring checks.
# ---------------------------------------------------------------------------

DUPLICATE_KEY_KEYWORDS: tuple[str, ...] = ("on duplicate key", "replace into")

# A string aggregate, whatever the dialect calls it: MySQL's GROUP_CONCAT, the
# STRING_AGG of PostgreSQL and SQL Server, Oracle's and DB2's LISTAGG (#920: both
# Aurora engines have one).
_STRING_AGGREGATE_RE = re.compile(r"\b(?:group_concat|string_agg|listagg)\s*\(", re.IGNORECASE)

STORED_ROUTINE_KEYWORDS: tuple[str, ...] = (
    "call ",
    "delimiter",
    "create procedure",
    "create function",
)
# SQL Server runs a stored procedure with EXEC or EXECUTE as the statement (with an
# optional ``@rc =`` for its return code); ``EXEC ('...')`` runs a string instead. Only
# for a SQL Server source: PostgreSQL's and MySQL's EXECUTE runs a prepared statement.
_TSQL_EXEC_RE = re.compile(r"^exec(?:ute)?\s+(?:@\w+\s*=\s*)?[\[\w]", re.IGNORECASE)

# ---------------------------------------------------------------------------
# PostgreSQL syntax (the Aurora PostgreSQL analysis's detectors, moved here
# from ``aurora_pg_pattern_catalog``).
# ---------------------------------------------------------------------------

# Array operations:
#   - ANY(ARRAY[...])
#   - array_agg(
#   - unnest(
#   - ARRAY[...]
#   - @> ARRAY[...] (array containment, NOT json)
_ARRAY_OPERATION_RE = re.compile(
    r"""
    \bANY\s*\(\s*ARRAY\b
    |
    \barray_agg\s*\(
    |
    \bunnest\s*\(
    |
    \bARRAY\s*\[
    |
    @>\s*ARRAY\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# LATERAL joins: requires JOIN LATERAL or comma LATERAL or LATERAL( with word boundary
_LATERAL_JOIN_RE = re.compile(
    r"""
    \bJOIN\s+LATERAL\s*\(
    |
    ,\s*LATERAL\s*\(
    |
    \bLATERAL\s*\(
    """,
    re.IGNORECASE | re.VERBOSE,
)
# SQL Server's correlated join: CROSS APPLY / OUTER APPLY of a subquery or a table-valued
# function is the same join as LATERAL (#982), read in the SQL Server dialect only.
_TSQL_APPLY_RE = re.compile(r"\b(?:CROSS|OUTER)\s+APPLY\b", re.IGNORECASE)

# Upsert: ON CONFLICT keyword OR RETURNING used as a SQL clause.
# RETURNING as a SQL clause follows DML (not preceded by AS keyword).
# Matches: RETURNING id, RETURNING *, RETURNING col1, col2
# Does NOT match: AS returning FROM (alias usage)
# An upsert in any dialect (#984): PostgreSQL's ON CONFLICT, MySQL's ON DUPLICATE KEY
# UPDATE and REPLACE INTO, read on the text with comments, literals and quoted identifiers
# (double quotes, backticks, T-SQL brackets) blanked out.
_UPSERT_RE = re.compile(
    r"\bON\s+CONFLICT\b|\bON\s+DUPLICATE\s+KEY\s+UPDATE\b|\bREPLACE\s+INTO\b", re.IGNORECASE
)
_BACKTICK_OR_BRACKET_IDENTIFIER_RE = re.compile(r"`[^`]*`|\[[^\]]*\]")
_ON_CONFLICT_OR_RETURNING_RE = re.compile(
    r"""
    \bON\s+CONFLICT\b
    |
    (?<!AS\s)(?<!\bAS\b)\bRETURNING\s+[\w*]
    """,
    re.IGNORECASE | re.VERBOSE,
)


# ---------------------------------------------------------------------------
# Document shapes (the DocumentDB analysis's detectors, moved here from
# ``documentdb_analysis_tools``).
# ---------------------------------------------------------------------------

_NEGATION_WORD_RE = re.compile(r"\b(not\s+in|not\s+exists|<>|!=)\b", re.I)
# ``( SELECT``, then a later ``WHERE``, then a later ``a.b = c.d``: three scans, each
# from where the one before ended (:func:`_has_correlated_subquery_shape`). One regex
# with two lazy gaps (``re.S``) answered the same but was roughly cubic on many
# uncorrelated subqueries (#902).
_SUBQUERY_SELECT_RE = re.compile(r"\(\s*select\b", re.I)
_SUBQUERY_WHERE_RE = re.compile(r"\bwhere\b", re.I)
_QUALIFIED_EQUALITY_RE = re.compile(r"\b\w+\.\w+\s*=\s*\w+\.\w+", re.I)


def _has_correlated_subquery_shape(text: str) -> bool:
    """``( SELECT ... WHERE ... a.b = c.d`` in linear time: the earliest ``( SELECT``
    and the earliest ``WHERE`` after it leave the most text for the next scan, so
    they decide."""
    select = _SUBQUERY_SELECT_RE.search(text)
    if select is None:
        return False
    where = _SUBQUERY_WHERE_RE.search(text, select.end())
    return where is not None and _QUALIFIED_EQUALITY_RE.search(text, where.end()) is not None


# ---------------------------------------------------------------------------
# Search and time-series keywords (the OpenSearch analysis's detectors, moved
# here from ``opensearch_pattern_catalog``): case-insensitive substring checks.
# ---------------------------------------------------------------------------

TIME_AGG_KEYWORDS: tuple[str, ...] = (
    "date_trunc",
    "date(",
    "hour(",
    "extract(",
)


# ---------------------------------------------------------------------------
# Cache shapes (the ElastiCache analysis's detectors, moved here from
# ``redis_analysis_tools``): case-insensitive substring checks.
# ---------------------------------------------------------------------------

# Spatial SQL functions or coordinate columns.
GEOSPATIAL_KEYWORDS: tuple[str, ...] = (
    "st_distance",
    "st_within",
    "st_contains",
    "st_intersects",
    "st_dwithin",
    "latitude",
    "longitude",
    "geography",
    "geometry",
)


# ---------------------------------------------------------------------------
# Workload signals (triage's detectors, moved here from
# ``src.agents.referee.triage``). Each reads the lower-cased text, as triage did.
# ---------------------------------------------------------------------------

_MIN_MAX_CALL_RE = re.compile(r"\b(max|min)\s*\(")
_STATUS_EQUALS_VALUE_RE = re.compile(r"\bstatus\s*=\s*[\?'\"]")
_IS_NULL_RE = re.compile(r"\bis\s+null\b")
_COUNT_STAR_OR_ONE_RE = re.compile(r"\bcount\s*\(\s*[\*1]\s*\)")


# ---------------------------------------------------------------------------
# Session keys (moved here from ``src.agents.referee.cache_overlay``, #296/#663):
# the one session fact triage, the cache overlay and the DynamoDB and ElastiCache
# analyses read (#893). The top-N shape is the facade's ``top_n`` now.
# ---------------------------------------------------------------------------

# Same keywords for triage's session_store signal and the cache overlay (``session_key``).
_SESSION_RE = re.compile(r"\b(session|token|sess_id|session_id|csrf)\b", re.IGNORECASE)
# The parts of a PascalCase / camelCase name: ``SessionToken`` -> Session, Token;
# ``CSRFToken`` -> CSRF, Token; ``sessionId`` -> session, Id; ``SessionID`` -> Session, ID.
_NAME_PART_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_IDENTIFIER_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
# The two-part session-store key names a PascalCase or camelCase schema uses
# (#663, kept narrow after review: ``session_store`` alone selects analysis
# agents). ``<prefix>Token`` for these prefixes, or ``Session{Id,Key,Token}``.
# Not ``SessionTimeout``, ``TokenCount``, ``TrainingSession``, ``ExpressionToken``
# or ``PaymentTokenVault``: a name part alone is not a session key. The
# prefixes name a login, session or account-access credential: the session and
# auth ones, and the one-time tokens an account flow looks up by value
# (``ResetToken``, ``LoginToken``, ``RememberToken``, ``VerificationToken``,
# ``InviteToken``, ``IdToken``, ..., #742). A token that is not a credential
# (``PaymentToken``, ``DeviceToken``, ``UnsubscribeToken``) is not one.
_SESSION_TOKEN_PREFIXES = frozenset(
    {
        # session and auth
        "session",
        "csrf",
        "xsrf",
        "auth",
        "access",
        "refresh",
        "jwt",
        "api",
        "bearer",
        "id",
        "sso",
        # login and account flows
        "login",
        "remember",
        "reset",
        "verification",
        "verify",
        "confirmation",
        "confirm",
        "activation",
        "invite",
        "invitation",
        "magic",
        "otp",
    }
)
_SESSION_KEY_SUFFIXES = frozenset({"id", "key", "token"})
_WHERE_CLAUSE_RE = re.compile(r"\bwhere\b(.*)", re.IGNORECASE | re.DOTALL)


def _is_session_key_name(name: str) -> bool:
    """Whether a PascalCase, camelCase or snake_case ``name`` is a two-part session or
    token key (``SessionToken``, ``sessionId``, ``session_token``, ``auth_token``)."""
    parts = [p.lower() for chunk in name.split("_") for p in _NAME_PART_RE.findall(chunk)]
    if len(parts) != 2:
        return False
    head, tail = parts
    return (tail == "token" and head in _SESSION_TOKEN_PREFIXES) or (
        head == "session" and tail in _SESSION_KEY_SUFFIXES
    )


def _has_session_key(text: str, source_engine: str | None) -> bool:
    """Whether ``text`` is a session/token statement (``session_store``, #663).

    Either ``_SESSION_RE`` matches a whole word (``session``, ``token``,
    ``session_id``, ...), unchanged from before, or the WHERE clause filters on a
    two-part session key such as T-SQL's ``[SessionToken]``, ``[CSRFToken]``,
    ``sessionId``, or the snake_case ``session_token``/``auth_token`` (#893: the
    session fact the DynamoDB and ElastiCache analyses read too, whose copies read
    ``session_token`` and this one did not; :func:`_is_session_key_name`). The second rule
    is deliberately narrow, because ``session_store`` selects analysis agents on
    its own: a name part such as ``[SessionTimeout]`` or ``[TokenCount]`` does not
    count, and neither does a ``[UserSession]`` query keyed by something else or
    a session key named only in the select list or inside a string literal. Needs
    the SQL in its original case.
    """
    text = text or ""
    if _SESSION_RE.search(text):
        return True
    # Literals blanked (``purpose = 'reset_token'`` names no column); a bracketed or
    # double-quoted identifier keeps its name (``scan_sql``).
    where = _WHERE_CLAUSE_RE.search(scan_sql(text, source_engine))
    if where is None:
        return False
    return any(_is_session_key_name(name) for name in _IDENTIFIER_RE.findall(where.group(1)))


# ---------------------------------------------------------------------------
# Search depth (the reality check's OpenSearch justification floor, moved here
# from ``reality_check_floors``, #326/#375).
# ---------------------------------------------------------------------------

# A string literal ('...' or "...") is replaced with a single placeholder
# before any pattern below runs, so a literal's own characters are never
# mistaken for a SQL operator -- a review of #375 found a leading-wildcard
# LIKE pattern (LIKE 'abc%') being read as the pg_trgm similarity operator
# because the '%' inside the literal matched the same way `title % $1` does.
_STRING_LITERAL_RE = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")


# A search predicate: the query actually filters on a text-search pattern,
# not just an ordinary equality/range WHERE clause. Required before ranking,
# fuzzy matching or facets are even considered -- a bare GROUP BY, or a
# to_tsvector() call with no @@ match, is not a search at all (see below).
# A MySQL ``MATCH (...) AGAINST (`` is a search predicate too
# (``regex_facts.has_match_against``, a linear scan, #902).
_SEARCH_PREDICATE_RE = re.compile(
    r"\b(like|ilike)\b|to_tsquery\s*\(|"
    r"plainto_tsquery\s*\(|@@|similarity\s*\(|word_similarity\s*\(",
    re.IGNORECASE,
)

# Relevance ranking: an actual full-text match operation. to_tsvector() alone
# (document construction, e.g. CREATE EXTENSION's companion index build, or a
# plain SELECT to_tsvector(...) with no @@) does not count -- only a real
# match (the @@ operator, constructing the query side with to_tsquery/
# plainto_tsquery, or a MySQL ``MATCH (...) AGAINST (``) does.
_RELEVANCE_RANKING_RE = re.compile(
    r"to_tsquery\s*\(|plainto_tsquery\s*\(|@@",
    re.IGNORECASE,
)

# Fuzzy matching: an actual similarity computation -- the similarity()/
# word_similarity() functions, the pg_trgm distance/threshold operators
# ("<%", "%>", word-similarity thresholds), or the real pg_trgm "%" operator
# (an identifier or closing paren, then "%", then a parameter placeholder,
# "$N" or "?", not immediately followed by a comparison or arithmetic
# operator). Checked against literal-stripped text only: a leading-wildcard
# LIKE pattern like 'abc%' has no "%" left once its literal is stripped, so
# it never matches this on its own (a review of #375 found the previous
# regex doing exactly that -- matching a LIKE pattern's own wildcard as the
# trigram operator). The negative lookahead after the parameter excludes
# plain modulo arithmetic against a parameter, e.g. "id % $2 = 0" or
# "id % $2 + 1" -- a real trigram "%" is used as a boolean predicate on its
# own, never followed immediately by another comparison or arithmetic
# operator (a second review of #375 found "id % $2 = 0" misread as fuzzy
# matching).
_FUZZY_MATCH_RE = re.compile(
    r"\bsimilarity\s*\(|\bword_similarity\s*\(|<%|%>|"
    r"[\w)]\s*%\s*(?:\$\d+|\?)(?!\s*(?:=|<>|!=|<=|>=|<|>|[+\-*/%]))",
    re.IGNORECASE,
)


def _has_search_depth(query_text: str, group_by: bool) -> bool:
    """Real search depth in one query (#326 finding 4): a search predicate
    plus relevance ranking, fuzzy matching, or a facet (``group_by``, the
    query's ``GROUP BY`` fact, alongside the same predicate) -- never a bare
    GROUP BY, a plain LIKE/prefix filter, a plain join, or to_tsvector()/pg_trgm
    document/extension setup with no match operator.
    """
    text = _STRING_LITERAL_RE.sub("?", query_text)
    # Each regex below needs a literal word or symbol; a substring test on the lower-cased
    # text (C speed) skips the regex on the long queries that have none of them.
    lower = text.lower()
    if any(word in lower for word in ("tsquery", "@@")) and _RELEVANCE_RANKING_RE.search(text):
        return True
    if "against" in lower and has_match_against(text):
        return True
    if ("similarity" in lower or "%" in lower) and _FUZZY_MATCH_RE.search(text):
        return True
    return group_by and bool(_SEARCH_PREDICATE_RE.search(text))


# ---------------------------------------------------------------------------
# Heterogeneous-source features (the Aurora engine choice's detectors, moved
# here from ``aurora_choice``, #381). Each runs on :func:`scan_sql`'s output.
# ---------------------------------------------------------------------------

# Requires MERGE to be followed by an optional TOP(n) clause, then (optionally "INTO"
# then) the start of a target identifier -- the shape an actual MERGE statement takes
# -- not just the bare keyword "merge" (e.g. a column or variable literally named
# ``merge``) coincidentally followed by "using" somewhere within the next 80
# characters. The negative lookahead excludes a handful of SQL keywords that can
# legitimately follow a bare "merge" column reference (``SELECT merge FROM ...
# USING (...)``) but never follow the MERGE statement itself. ``[#\w]`` (#381 review
# round 2) only requires the *first* character of the target identifier to look like
# one -- a word character, or ``#`` for a local temp table (``MERGE #target ...``) --
# rather than the whole identifier, since by the time this runs ``scan_sql`` has
# already turned a bracketed or double-quoted identifier into its bare name (``[dbo].
# [Target]`` -> ``dbo.Target``), and the ``.{0,80}\busing\b`` that follows is what
# actually confirms this is a MERGE statement, not the identifier shape.
_MERGE_RE = re.compile(
    r"\bmerge\s+(?:top\s*\([^)]*\)\s*)?(?:into\s+)?"
    r"(?!(?:from|where|select|group|order|having|join)\b)[#\w]"
    r".{0,80}\busing\b",
    re.IGNORECASE | re.DOTALL,
)


# ---------------------------------------------------------------------------
# Cache overlay shapes and the write check (moved here from
# ``src.agents.referee.cache_overlay``, #296/#303/#663/#825). The UI engine
# picker mirrors the write check (``src/ui/src/utils/assignmentSummary.js``).
# ---------------------------------------------------------------------------

_WHERE_RE = re.compile(r"\bwhere\b", re.IGNORECASE)
_KEYED_RE = re.compile(r"(?<![<>!])=|\bin\s*\(", re.IGNORECASE)

# Leading ``--`` or ``#`` line comments and ``/* ... */`` block comments, as
# many as the statement opens with, are stripped first (review of #303), so a
# leading CALL/TRUNCATE/COPY is seen behind a comment too. The collector types a
# comment-led DML statement by its verb since #1178 (``src.tools.sql.query_type``),
# and a WITH-led write by its final statement since #1287; a WITH-led statement that
# ends in SELECT (a PostgreSQL data-modifying CTE included) is still ``OTHER``. ``src.shared.sql_comments`` strips in linear
# time (#808).
# A DML keyword anywhere in the statement -- not just at the start -- so a
# data-modifying CTE (``WITH w AS (INSERT INTO ... RETURNING id) SELECT ...``)
# is caught too, the same way the collector's own query_type misses it.
# ``UPDATE <table> [[AS] <alias>] SET`` (#1287): the table may carry an alias, with or
# without ``AS``, and PostgreSQL allows ``UPDATE ONLY <table>``.
_DML_KEYWORD_RE = re.compile(
    r"\b(insert\s+into|update\s+(?:only\s+)?\S+(?:\s+(?:as\s+)?\S+)?\s+set"
    r"|delete\s+from|merge\s+into)\b",
    re.IGNORECASE,
)
# CALL/TRUNCATE/COPY are writes but have no DML keyword of their own; anchored
# at the start (after stripping a leading comment), since each is a statement
# in its own right, not a clause that can appear mid-query.
_LEADING_WRITE_VERB_RE = re.compile(r"^\s*(call|truncate|copy)\b", re.IGNORECASE)


def _strip_leading_comments(text: str, nested: bool = False) -> str:
    return strip_leading_comments(
        text, hash_comments=True, strip_following_space=True, nested=nested
    )


# A PostgreSQL ``DO $$ ... $$`` block, or the ``AS $$ ... $$`` body of a ``CREATE FUNCTION`` /
# ``CREATE PROCEDURE``, is code that runs, not a string literal (#1144): the one opener
# check is ``sql_spans.opens_routine_body``, which ``code_text`` reads too (#1416).
_TAIL = 80
# Inside a body ``CALL``, ``TRUNCATE`` and ``COPY`` are statements wherever they stand, not
# only at the start of the text, so a body reads them as the DML they are.
_BODY_VERB_RE = re.compile(r"\b(?:call|truncate|copy)\s+(?:table\s+)?(?=[\w\"])", re.IGNORECASE)


def _postgres_code_reading(text: str, in_body: bool = False) -> str:
    """``text`` with its comments and string literals blanked, except the code a
    PostgreSQL statement runs.

    The scan is the dialect's span grammar, so a ``do`` or ``as`` inside a quoted
    string never opens a body. A ``$tag$`` string that follows ``DO`` (with an optional
    ``LANGUAGE x``) or ``AS`` is code, read again the same way. Inside a body every
    string is read as code too: dynamic SQL (``EXECUTE 'DELETE FROM t'``,
    ``EXECUTE $b$ INSERT ... $b$``) is a write the gate must see. That errs toward
    "write" (a ``RAISE NOTICE 'delete from t'`` counts), the safe direction.
    """
    out: list[str] = []
    tail = ""
    last = 0
    if in_body:
        text = _BODY_VERB_RE.sub(" delete from ", text)
    for start, end, opener in spans(text, POSTGRES_LITERALS_AND_COMMENTS):
        kept = text[last:start]
        out.append(kept)
        tail = (tail + kept)[-_TAIL:]
        span = text[start:end]
        piece = " "
        if opener.startswith("$") and len(span) >= 2 * len(opener):
            if in_body or opens_routine_body(tail):
                piece = f" {_postgres_code_reading(span[len(opener) : -len(opener)], True)} "
        elif opener in ("'", "E'", "e'") and in_body and len(span) >= len(opener) + 1:
            body = span[len(opener) : -1].replace(chr(39) * 2, chr(39))
            piece = f" {_postgres_code_reading(body, True)} "
        out.append(piece)
        tail = (tail + piece)[-_TAIL:]
        last = end
    out.append(text[last:])
    return "".join(out)


def _leading_comment_readings(text: str) -> tuple[str, ...]:
    """``text`` without its leading comments, read both ways (#825).

    PostgreSQL and SQL Server nest block comments, MySQL and Oracle end one at
    its first ``*/``. The write check (:func:`is_write_query`, behind
    :func:`can_own`, the cache overlay's write-heavy tables and schema design's
    write scope) strips the leading comments before the source engine is applied
    (the nesting differs by dialect and a comment can open the statement), so this
    reads both ways. A write under either reading is a write, which errs toward
    "write" rather than missing one. The function reads both ways whatever the source
    engine is; the engine only decides how the rest is stripped afterwards.

    The nested reading counts only when it strips at least as much as the
    first-close reading. A terminated nested comment always ends at or after the
    first ``*/``, so a nested strip that leaves more text means the comment is
    unterminated there (``/* delete from /* x */ SELECT 1``): not valid
    PostgreSQL or T-SQL, and a MySQL read that must stay a read.
    """
    plain = _strip_leading_comments(text)
    nested = _strip_leading_comments(text, nested=True)
    return (plain,) if len(nested) > len(plain) else (plain, nested)


def _contains_any(lower_text: str, keywords: tuple[str, ...]) -> bool:
    """Whether lower-cased text contains any keyword (keywords compared as written)."""
    return any(kw in lower_text for kw in keywords)


class QueryFacts:
    """The text facts of one query (see the module docstring).

    Each fact is computed the first time a reader asks for it, then kept on the
    object (``functools.cached_property``), so a reader pays only for the facts
    it reads: a costly detector one analysis needs (the DocumentDB correlated
    subquery shape) never runs in the steps that don't read it (#895 review).
    """

    def __init__(self, text: str, source_engine: str = "") -> None:
        self._text = text
        self._source_engine = source_engine
        self._timestamp_kept: tuple[ColumnTypes | None, TimestampColumnReading] | None = None

    @cached_property
    def _lower(self) -> str:
        return self._text.lower()

    @cached_property
    def _code(self) -> str:
        """The text as the source engine runs it (``sql_spans.code_text``, #1415): comments
        blanked, each string literal a ``?``, quoted identifiers kept. The text facts read
        this, so a keyword inside a literal or a comment is never SQL."""
        return code_text(self._text, self._source_engine)

    @cached_property
    def _code_lower(self) -> str:
        return self._code.lower()

    # -- Parsed facts (the facade's ``SqlFacts``, #893) ------------------------

    @cached_property
    def _readings(self) -> tuple[SqlFacts, AccessFacts]:
        """The facade's readings of the text in the source engine's dialect: the ones the
        collector stored for it (:func:`remember_collected_facts`, #934), else one parse."""
        stored = _STORED_READINGS.get((self._text, self._source_engine))
        if stored is not None:
            readings = readings_from_stored(stored, self._text, self._source_engine)
            if readings is not None:
                return readings
        return query_readings(self._text, self._source_engine)

    def access_facts(self) -> AccessFacts:
        """The facade's access-pattern facts of the text (``facade.access_facts``), from the
        same reading as the parsed facts."""
        return self._access

    @cached_property
    def _parsed(self) -> SqlFacts:
        return self._readings[0]

    @cached_property
    def _access(self) -> AccessFacts:
        return self._readings[1]

    def _has(self, *hints: str) -> bool:
        return any(hint in self._lower for hint in hints)

    @cached_property
    def group_by(self) -> bool:
        """A ``GROUP BY`` clause (``SqlFacts.group_by``)."""
        return self._has("group") and self._parsed.group_by

    @cached_property
    def grouping_sets(self) -> bool:
        """Several groupings of one row set (``SqlFacts.grouping_sets``, #968)."""
        return self._has("grouping", "rollup", "cube") and self._parsed.grouping_sets

    @cached_property
    def aggregate_function_count(self) -> int:
        """How many aggregate function calls (``SqlFacts.aggregate_function_count``)."""
        if not self._may_aggregate:
            return 0
        return self._parsed.aggregate_function_count

    @cached_property
    def _may_aggregate(self) -> bool:
        return self._has(*_AGGREGATE_WORD_HINTS) or bool(_FUNCTION_CALL_HINT_RE.search(self._text))

    @cached_property
    def aggregation(self) -> bool:
        """A ``GROUP BY`` or an aggregate function, windowed or not: exactly the facade's
        ``has_aggregation``, the collector's ``has_aggregations``. A ranking or offset
        window function alone is not one.
        """
        return self.group_by or self.aggregate_function_count > 0

    @cached_property
    def join_count(self) -> int:
        """Joins to another row source (``SqlFacts.join_count``, the collector's
        ``join_count``): ``FROM a, b`` is one join, a table function is none.
        """
        return self._parsed.join_count if self._has(*_JOIN_HINTS) else 0

    @cached_property
    def leading_wildcard_like(self) -> bool:
        """A ``LIKE``/``ILIKE`` whose pattern starts with ``%`` (``SqlFacts.wildcard_leading``)."""
        return self._has("like") and self._parsed.wildcard_leading

    @cached_property
    def wildcard_likes(self) -> tuple[WildcardLike, ...]:
        """Each leading-wildcard ``LIKE``, with the column it tests and whether it is a
        word search (``SqlFacts.wildcard_likes``, #493)."""
        return self._parsed.wildcard_likes if self._has("like") else ()

    @cached_property
    def like_parameter(self) -> bool:
        """A ``LIKE``/``ILIKE`` whose whole pattern is a bind parameter
        (``SqlFacts.like_parameter``): a MySQL digest's ``LIKE ?``.
        """
        return self._has("like") and self._parsed.like_parameter

    @cached_property
    def fulltext_search(self) -> bool:
        """A full-text match: ``MATCH ... AGAINST``, ``@@`` or a ``tsvector``/``tsquery``
        function, ``CONTAINS``/``FREETEXT`` (``SqlFacts.fulltext_search``).
        """
        return self._has(*_FULLTEXT_HINTS) and self._parsed.fulltext_search

    @cached_property
    def pattern_match(self) -> bool:
        """A regular-expression or ``SIMILAR TO`` match (``SqlFacts.pattern_match``)."""
        return self._has(*_PATTERN_MATCH_HINTS) and self._parsed.pattern_match

    @cached_property
    def prefix_regex(self) -> bool:
        """Every regular expression is a case-sensitive ``^``-anchored literal, the shape
        an index can serve (``SqlFacts.prefix_regex``, #982)."""
        return self.pattern_match and self._parsed.prefix_regex

    @cached_property
    def regex_matches(self) -> tuple[RegexMatch, ...]:
        """Each regular-expression or ``SIMILAR TO`` match, with the column it tests
        (``SqlFacts.regex_matches``, #1098); empty on the regex fallback."""
        return self._parsed.regex_matches if self.pattern_match else ()

    @cached_property
    def similarity_match(self) -> bool:
        """A trigram similarity function (``SqlFacts.similarity_match``)."""
        return self._has("similarity") and self._parsed.similarity_match

    @cached_property
    def text_search(self) -> bool:
        """A text search: a leading-wildcard or parameterised ``LIKE``, a full-text,
        regular-expression or similarity match. A prefix ``LIKE 'abc%'`` is a range,
        not a search (ADR-031 Decision 14 Amendment 5).
        """
        return (
            self.leading_wildcard_like
            or self.like_parameter
            or self.fulltext_search
            or self.pattern_match
            or self.similarity_match
        )

    def text_search_type(self) -> str | None:
        """The collector's ``text_search_type`` label for :attr:`text_search`, from its
        parts (#929): a full-text match is ``tsvector`` on PostgreSQL, ``oracle_text`` on
        Oracle and ``fulltext`` elsewhere; then ``trigram`` (a similarity function),
        ``regex`` (a regular-expression or ``SIMILAR TO`` match) and ``like_wildcard``
        (a leading-wildcard or parameterised ``LIKE``). None when there is no search.
        """
        if self.fulltext_search:
            return _FULLTEXT_LABELS.get(self._source_engine, "fulltext")
        if self.similarity_match:
            return "trigram"
        if self.pattern_match:
            return "regex"
        if self.leading_wildcard_like or self.like_parameter:
            return "like_wildcard"
        return None

    @cached_property
    def window_function(self) -> bool:
        """A function with an ``OVER`` clause (``SqlFacts.window_function``)."""
        return self._has("over") and self._parsed.window_function

    @cached_property
    def ranking_window_function(self) -> bool:
        """``ROW_NUMBER``/``RANK``/``DENSE_RANK`` over a window
        (``SqlFacts.ranking_window_function``).
        """
        return self._has("over") and self._parsed.ranking_window_function

    @cached_property
    def offset_window_function(self) -> bool:
        """``LAG``/``LEAD``/``NTILE`` over a window (``SqlFacts.offset_window_function``)."""
        return self._has("over") and self._parsed.offset_window_function

    @cached_property
    def windowed_aggregate(self) -> bool:
        """An aggregate over a window, ``SUM(...) OVER`` (``SqlFacts.windowed_aggregate``)."""
        return self._has("over") and self._parsed.windowed_aggregate

    @cached_property
    def recursive_cte(self) -> bool:
        """``WITH RECURSIVE``, or a CTE whose body has a ``UNION`` and reads itself
        (``SqlFacts.recursive_cte``; SQL Server and Oracle have no ``RECURSIVE``).
        """
        return self._has("with") and self._parsed.recursive_cte

    @cached_property
    def order_by(self) -> bool:
        """An ``ORDER BY`` that sorts a query's rows, not a window's (``SqlFacts.order_by``)."""
        return self._has("order") and self._parsed.order_by

    @cached_property
    def row_limit(self) -> bool:
        """A row limit: ``LIMIT``, ``TOP``, ``FETCH FIRST``/``NEXT`` (``SqlFacts.row_limit``);
        a ``credit_limit`` column is not one.
        """
        return self._has(*_ROW_LIMIT_HINTS) and self._parsed.row_limit

    @cached_property
    def top_n(self) -> bool:
        """A top-N read: one query with both an ``ORDER BY`` and a row limit
        (``SqlFacts.top_n``; ``TOP`` with no ``ORDER BY`` is not one).
        """
        return self.order_by and self.row_limit and self._parsed.top_n

    def timestamp_column(self, column_types: ColumnTypes | None) -> TimestampColumnReading:
        """A timestamp column: one whose collected type is a timestamp
        (:func:`is_timestamp_type`), resolved to its table from the facade's columns
        (``SqlFacts.columns``) and the collection's tables (#935). With no column
        types (``None``), and for a column whose table can't be told, its name is
        read instead (``regex_facts.TIMESTAMP_COLUMN_NAMES``: ``created_at``,
        ``event_time``, ``*_date``, ``last_*_at``, ...), and the reading says so.
        Kept for the last ``column_types`` asked for; not parsed when the text holds
        neither a timestamp name nor a timestamp-typed column's name.
        """
        kept = self._timestamp_kept
        if kept is not None and kept[0] is column_types:
            return kept[1]
        # A column is read only by a name the text holds: a timestamp name's hint, or
        # the name of one of the collection's timestamp-typed columns.
        if not self._has(*_TIMESTAMP_HINTS) and (
            column_types is None or not self._has(*column_types.timestamp_names)
        ):
            reading = _NO_TIMESTAMP_BY_NAME if column_types is None else _NO_TIMESTAMP
        else:
            reading = read_timestamp_column(self._parsed.columns, column_types)
        self._timestamp_kept = (column_types, reading)
        return reading

    @cached_property
    def multi_table_write(self) -> bool:
        """An UPDATE or DELETE whose rows come from a join with another table, in any
        dialect (``SqlFacts.multi_table_write``).
        """
        return self._has("update", "delete") and self._parsed.multi_table_write

    @cached_property
    def write_targets(self) -> tuple[str, ...]:
        """The tables the statement writes, lower-cased bare names: every ``INSERT``,
        ``REPLACE``, ``UPDATE``, ``DELETE``, ``MERGE`` and ``TRUNCATE`` target, never a
        table the write only reads (``SqlFacts.write_targets``, #992).
        """
        if not self._has("insert", "replace", "update", "delete", "merge", "truncate"):
            return ()
        targets = self._parsed.write_targets
        if self._source_engine == "sqlserver":
            # ``OUTPUT ... INTO <table>`` writes that table too (#1287).
            targets = tuple(
                dict.fromkeys((*targets, *output_into_targets(self._text, self._source_engine)))
            )
        return targets

    @cached_property
    def json_access(self) -> bool:
        """The query reads or builds JSON: a JSON function or operator, a cast to
        ``json``/``jsonb``, a JSON containment (``SqlFacts.json_access``).
        """
        return self._has(*_JSON_HINTS) and self._parsed.json_access

    @cached_property
    def range_predicate(self) -> bool:
        """A range on a column: ``<``, ``<=``, ``>``, ``>=``, ``BETWEEN`` or a prefix
        ``LIKE 'abc%'``, as a top-level ``AND`` term of a ``WHERE`` or ``JOIN`` condition
        (the access facts' ``ranges``, in any scope); the regex fallback's comparison
        when the statement can't be parsed (#933, #728).
        """
        if not self._has(*_RANGE_HINTS):
            return False
        if self._access.parsed_by == "sqlglot":
            return any(scope.ranges for scope in self._access.scopes)
        return regex_range_predicate(self._text)

    # -- Text facts ------------------------------------------------------------

    @cached_property
    def found_rows(self) -> bool:
        """MySQL's ``SQL_CALC_FOUND_ROWS`` modifier or a ``FOUND_ROWS()`` call (the row
        count of the previous query).
        """
        return bool(_FOUND_ROWS_RE.search(self._code))

    @cached_property
    def dedup_only_group_by(self) -> bool:
        """The ``GROUP BY`` only de-duplicates rows
        (``unsupported_pattern.is_dedup_only_group_by``, #336).
        """
        return self.group_by and is_dedup_only_group_by(self._text, self._source_engine)

    @cached_property
    def computed_join_key(self) -> bool:
        """Some ``JOIN ... ON`` condition compares a function call with a column
        (``lower(g.name) = u.username_lower``, either side); a function compared with a
        bind parameter is a filter, not a computed join key.
        """
        return _has_computed_join_key(self._text)

    @cached_property
    def write_with_subquery(self) -> bool:
        """``INSERT``/``UPDATE``/``DELETE`` followed later by ``SELECT``."""
        write = _WRITE_WORD_RE.search(self._code)
        return write is not None and SELECT_WORD_RE.search(self._code, write.end()) is not None

    @cached_property
    def exists_or_in_subquery(self) -> bool:
        """``EXISTS (``, ``NOT EXISTS (`` or ``IN (SELECT``."""
        return bool(EXISTS_CONDITION_RE.search(self._code) or _IN_SUBQUERY_RE.search(self._code))

    @cached_property
    def in_list(self) -> bool:
        """An ``IN (`` list: ``regex_facts.IN_LIST_RE``, the one definition the key
        predicate and the transaction shape read too (#1010)."""
        return bool(IN_LIST_RE.search(self._code))

    @cached_property
    def duplicate_key_upsert(self) -> bool:
        """``on duplicate key`` or ``replace into``."""
        return _contains_any(self._code_lower, DUPLICATE_KEY_KEYWORDS)

    @cached_property
    def upsert(self) -> bool:
        """An upsert, in any dialect: PostgreSQL's ``ON CONFLICT``, MySQL's ``ON DUPLICATE
        KEY UPDATE`` or ``REPLACE INTO``, never inside a comment, a string literal or a quoted
        identifier. The per-query feature fact the upsert patterns rate (#984): the upsert
        statement itself earns the credit, never its table's other queries (ADR-031
        Decision 14)."""
        if not self._has("conflict", "duplicate", "replace"):
            return False
        text = _BACKTICK_OR_BRACKET_IDENTIFIER_RE.sub(
            " ", strip_comments_and_literals(self._text, self._source_engine)
        )
        return bool(_UPSERT_RE.search(text))

    @cached_property
    def string_aggregate_call(self) -> bool:
        """``GROUP_CONCAT(``, ``STRING_AGG(`` or ``LISTAGG(`` outside comments and
        string literals."""
        return self._has("group_concat", "string_agg", "listagg") and bool(
            _STRING_AGGREGATE_RE.search(scan_sql(self._text, self._source_engine))
        )

    @cached_property
    def stored_routine_keyword(self) -> bool:
        """A :data:`STORED_ROUTINE_KEYWORDS` substring (``call ``, ``delimiter``, ``create
        procedure``, ``create function``), or, for a SQL Server source, a statement that
        is an ``EXEC``/``EXECUTE`` of a procedure.
        """
        if _contains_any(self._code_lower, STORED_ROUTINE_KEYWORDS):
            return True
        if self._source_engine.strip().lower() != "sqlserver" or not self._has("exec"):
            return False
        return any(_TSQL_EXEC_RE.match(t) for t in _leading_comment_readings(self._text))

    @cached_property
    def array_operation(self) -> bool:
        """``ANY(ARRAY``, ``array_agg(``, ``unnest(``, ``ARRAY[`` or ``@> ARRAY``."""
        return bool(_ARRAY_OPERATION_RE.search(self._code))

    @cached_property
    def lateral_join(self) -> bool:
        """``JOIN LATERAL (``, ``, LATERAL (`` or ``LATERAL (``, outside comments and string
        literals (the class score reads it too since #985, so a word in a value is not one);
        on a SQL Server source, ``CROSS APPLY`` or ``OUTER APPLY`` too (#982): the same
        correlated join, which both Aurora engines run as ``LATERAL``. The ``APPLY`` reading
        also blanks quoted identifiers, so a bracketed name such as ``[cross apply]`` is
        not one (#1098)."""
        if self._has("lateral") and _LATERAL_JOIN_RE.search(
            strip_comments_and_literals(self._text, self._source_engine)
        ):
            return True
        if self._source_engine.strip().lower() != "sqlserver" or not self._has("apply"):
            return False
        grammar = comments_literals_and_identifiers_for(self._source_engine)
        return bool(_TSQL_APPLY_RE.search(sub_spans(self._text, grammar, lambda _span: " ")))

    @cached_property
    def on_conflict_or_returning(self) -> bool:
        """``ON CONFLICT``, or ``RETURNING <column>`` not preceded by ``AS``."""
        return bool(_ON_CONFLICT_OR_RETURNING_RE.search(self._code))

    @cached_property
    def negation_word(self) -> bool:
        """``NOT IN``, ``NOT EXISTS``, ``<>`` or ``!=`` between word characters."""
        return bool(_NEGATION_WORD_RE.search(self._code))

    @cached_property
    def correlated_subquery_shape(self) -> bool:
        """``( SELECT ... WHERE ... a.b = c.d``."""
        return _has_correlated_subquery_shape(self._text)

    @cached_property
    def time_function_keyword(self) -> bool:
        """``date_trunc``, ``date(``, ``hour(`` or ``extract(``."""
        return _contains_any(self._code_lower, TIME_AGG_KEYWORDS)

    @cached_property
    def geospatial_keyword(self) -> bool:
        """A :data:`GEOSPATIAL_KEYWORDS` substring (``st_distance``, ..., ``latitude``,
        ``geometry``).
        """
        return _contains_any(self._code_lower, GEOSPATIAL_KEYWORDS)

    @cached_property
    def min_max_call(self) -> bool:
        """A ``MAX(`` or ``MIN(`` call."""
        return bool(_MIN_MAX_CALL_RE.search(self._code_lower))

    @cached_property
    def status_equals_value(self) -> bool:
        """``status = ?`` / ``status = '...`` / ``status = "...``."""
        return bool(_STATUS_EQUALS_VALUE_RE.search(self._code_lower))

    @cached_property
    def is_null(self) -> bool:
        """``IS NULL``."""
        return bool(_IS_NULL_RE.search(self._code_lower))

    @cached_property
    def count_star_or_one(self) -> bool:
        """``COUNT(*)`` or ``COUNT(1)``."""
        return bool(_COUNT_STAR_OR_ONE_RE.search(self._code_lower))

    @cached_property
    def _scanned(self) -> str:
        return scan_sql(self._text, self._source_engine)

    @cached_property
    def _scanned_lower(self) -> str:
        return self._scanned.lower()

    @cached_property
    def search_depth(self) -> bool:
        """Real search depth (#326): relevance ranking or fuzzy matching, or a search
        predicate with a ``GROUP BY`` facet, read with string literals blanked.
        """
        return _has_search_depth(self._code, self.group_by)

    @cached_property
    def merge_statement(self) -> bool:
        """A ``MERGE [TOP (n)] [INTO] <target> ... USING`` statement, read on
        :func:`scan_sql`'s output.
        """
        return "merge" in self._scanned_lower and bool(_MERGE_RE.search(self._scanned))

    @cached_property
    def _where_split(self) -> list[str]:
        return _WHERE_RE.split(self._text, maxsplit=1)

    @cached_property
    def where_word(self) -> bool:
        """A ``WHERE`` word anywhere in the text."""
        return len(self._where_split) == 2

    @cached_property
    def keyed_after_where(self) -> bool:
        """An equality (``=`` not after ``<``/``>``/``!``) or an ``IN (`` after the first
        ``WHERE``.
        """
        return len(self._where_split) == 2 and bool(_KEYED_RE.search(self._where_split[1]))

    @cached_property
    def write_statement_text(self) -> bool:
        """After the leading comments (read both nested and first-close, #825), a DML
        keyword anywhere (``INSERT INTO``, ``UPDATE x SET``, ``DELETE FROM``,
        ``MERGE INTO``) or a leading ``CALL``/``TRUNCATE``/``COPY``.
        """
        readings = _leading_comment_readings(self._text)
        if self._source_engine:
            # The dialect's own comments and strings (MySQL ``#``, PostgreSQL ``$$``)
            # hold no statement: a keyword inside one neither makes a write nor, by
            # opening a string the dialect does not have, hides one (#1144).
            if span_dialect(self._source_engine) == "postgresql":
                readings = tuple(_postgres_code_reading(reading) for reading in readings)
            else:
                readings = tuple(
                    strip_comments_and_literals(reading, self._source_engine)
                    for reading in readings
                )
        return any(
            _DML_KEYWORD_RE.search(reading) or _LEADING_WRITE_VERB_RE.match(reading)
            for reading in readings
        )

    @cached_property
    def session_key(self) -> bool:
        """A session/token statement (#663): a session word (``session``, ``token``,
        ``sess_id``, ``session_id``, ``csrf``), or a two-part PascalCase/camelCase session
        key in the WHERE clause (``[SessionToken]``, ``sessionId``). Reads the original case.
        """
        return _has_session_key(self._code, self._source_engine)

    @property
    def needs_aggregation(self) -> bool:
        """A window function, ``FOUND_ROWS``, or an aggregation that is more than a
        de-duplicating ``GROUP BY`` (#338, #336)."""
        if self.window_function or self.found_rows:
            return True
        return self.aggregation and not self.dedup_only_group_by


# ---------------------------------------------------------------------------
# The timestamp column, by collected type (#935)
#
# A DBA tells a timestamp column by its type in the schema, not its name: a
# ``posted`` DATETIME is one, a ``created_at`` VARCHAR is not. Each dialect's
# point-in-time types, after the collector's normalized type when it set one: an
# Oracle DATE holds a time of day too; SQL Server's ``timestamp`` is a row version
# (``rowversion``), never a point in time, whatever an offline normalization said.
# A time of day (``time``), an interval or a year is not a point in time.
# ---------------------------------------------------------------------------

_TIMESTAMP_NORMALIZED_TYPES = frozenset({"date", "datetime", "timestamp"})
_TIMESTAMP_NATIVE_TYPES = frozenset(
    {
        "date",
        "datetime",
        "datetime2",
        "smalldatetime",
        "datetimeoffset",
        "timestamp",
        "timestamptz",
        "timestamp with time zone",
        "timestamp without time zone",
        "timestamp with local time zone",
    }
)
_SQLSERVER_ROW_VERSION_TYPES = frozenset({"timestamp", "rowversion"})
# A type's arguments, ``(6)`` in ``TIMESTAMP(6) WITH TIME ZONE``.
_TYPE_ARGUMENTS_RE = re.compile(r"\s*\([^)]*\)")


def is_timestamp_type(column: Mapping, source_engine: str = "") -> bool:
    """Whether a collected column's type is a point in time (date, datetime or
    timestamp, in any dialect's spelling); its name is not read."""
    native = " ".join(
        _TYPE_ARGUMENTS_RE.sub("", str(column.get("data_type") or "")).lower().split()
    )
    if (source_engine or "").strip().lower() == "sqlserver" and native in (
        _SQLSERVER_ROW_VERSION_TYPES
    ):
        return False
    normalized = column.get("normalized_data_type")
    normalized = str(getattr(normalized, "value", normalized) or "").lower()
    if normalized:
        return normalized in _TIMESTAMP_NORMALIZED_TYPES
    return native in _TIMESTAMP_NATIVE_TYPES


@dataclass(frozen=True, eq=False)
class ColumnTypes:
    """A collection's column types: each collected table's columns (lower-case name ->
    the name as collected, and whether its type is a timestamp), and the resolver from
    a written table name to its ``table_id`` (``table_resolution.TableNameResolver``)."""

    resolver: TableNameResolver
    columns: Mapping[str, Mapping[str, tuple[str, bool]]]
    # The lower-case names of the timestamp-typed columns, in any table.
    timestamp_names: tuple[str, ...] = ()

    @classmethod
    def from_collector(cls, collector_output: Mapping | None) -> ColumnTypes | None:
        """The collection's column types, or None when no collected column has a type
        (the timestamp-column fact then reads the name list)."""
        from src.agents.referee.table_resolution import TableNameResolver  # import cycle

        engine = source_engine_of(collector_output)
        schema = (collector_output or {}).get("database_schema") or {}
        columns: dict[str, dict[str, tuple[str, bool]]] = {}
        typed = False
        for table in schema.get("tables") or []:
            table_id = str(table.get("table_id") or "")
            if not table_id:
                continue
            own = columns.setdefault(table_id, {})
            for column in table.get("columns") or []:
                name = str(column.get("column_name") or "")
                if not name:
                    continue
                typed = typed or bool(column.get("data_type") or column.get("normalized_data_type"))
                own[name.lower()] = (name, is_timestamp_type(column, engine))
        resolver = TableNameResolver.from_collector(collector_output or {})
        if not typed or resolver is None:
            return None
        names = {n for own in columns.values() for n, (_, ts) in own.items() if ts}
        return cls(resolver=resolver, columns=columns, timestamp_names=tuple(sorted(names)))

    def _table_id(self, ref: TableRef) -> str | None:
        return self.resolver.resolve(ref.written) or self.resolver.resolve(ref.name)

    def owner(self, use: ColumnUse) -> tuple[tuple[str, str, bool] | None, str]:
        """``((table_id, column, is_timestamp), "")`` for the collected column ``use``
        names, or ``(None, why)`` when its table can't be told."""
        table_id, why = locate_column(
            use, self._table_id, lambda t, name: name in self.columns.get(t, {})
        )
        if table_id is None:
            return None, why
        written, timestamp = self.columns[table_id][use.name.lower()]
        return (table_id, written, timestamp), ""


def locate_column(
    use: ColumnUse,
    table_id_of: Callable[[TableRef], str | None],
    holds: Callable[[str, str], bool],
) -> tuple[str | None, str]:
    """``(table_id, "")`` for the collected table that holds the column ``use`` names, or
    ``(None, why)`` when it can't be told: the way SQL resolves it, the innermost scope in
    which exactly one collected table holds a column of that name (``table_id_of``: a
    written table's id, None when not collected; ``holds(table_id, lower-case name)``).
    The one resolution the timestamp-column fact and the leading-wildcard ``LIKE`` fact
    (#935, #942) share."""
    if use.from_regex:
        return None, "statement not parsed"
    name = use.name.lower()
    for level in use.scopes:
        holders: set[str] = set()
        unknown: list[str] = []
        for ref in level:
            table_id = table_id_of(ref)
            if table_id is None:
                unknown.append(ref.written)
            elif holds(table_id, name):
                holders.add(table_id)
        if len(holders) == 1:
            return next(iter(holders)), ""
        if holders:
            return None, "ambiguous: " + ", ".join(sorted(holders))
        if unknown:
            return None, "no collected table " + ", ".join(unknown)
    return None, "no collected table holds it"


@dataclass(frozen=True)
class TimestampColumnReading:
    """The timestamp-column fact of one query (``QueryFacts.timestamp_column``), true
    when ``found``. ``by_type``: the timestamp-typed columns (``table_id.column``).
    ``by_name``: the timestamp-named columns read by name. ``fallbacks``: each column
    read by name although the collection has types, with why (``column: ambiguous:
    ...``, ``column: no collected table ...``, ``column: statement not parsed``), when
    it could be a timestamp: a timestamp name, or a timestamp-typed column's name.
    ``no_types``: the collection has no column types, so every name was read."""

    found: bool
    by_type: tuple[str, ...] = ()
    by_name: tuple[str, ...] = ()
    fallbacks: tuple[str, ...] = ()
    no_types: bool = False

    def __bool__(self) -> bool:
        return self.found

    @property
    def read_by_name(self) -> bool:
        """The fact holds only because of a column read by name."""
        return self.found and not self.by_type


_NO_TIMESTAMP_BY_NAME = TimestampColumnReading(found=False, no_types=True)
_NO_TIMESTAMP = TimestampColumnReading(found=False)


def read_timestamp_column(
    columns: Iterable[ColumnUse], column_types: ColumnTypes | None
) -> TimestampColumnReading:
    """The timestamp-column fact of a statement's columns (``SqlFacts.columns``):
    each column's collected type when its table can be told, else its name."""
    by_type: list[str] = []
    by_name: list[str] = []
    fallbacks: list[str] = []
    for use in columns:
        if column_types is not None:
            owner, why = column_types.owner(use)
            if owner is not None:
                if owner[2]:
                    by_type.append(f"{owner[0]}.{owner[1]}")
                continue
        named = bool(TIMESTAMP_COLUMN_NAME_RE.fullmatch(use.name))
        if column_types is not None and (named or use.name.lower() in column_types.timestamp_names):
            fallbacks.append(f"{use.name}: {why}")
        if named:
            by_name.append(use.name)
    return TimestampColumnReading(
        found=bool(by_type or by_name),
        by_type=tuple(dict.fromkeys(by_type)),
        by_name=tuple(dict.fromkeys(by_name)),
        fallbacks=tuple(dict.fromkeys(fallbacks)),
        no_types=column_types is None,
    )


def timestamp_column_trace(collector_output: Mapping) -> dict:
    """How an analysis read the timestamp-column fact, for its decision trace: by
    column type, or by name when the collection has none; and each query on which
    some column was read by name although the collection has types (#935)."""
    types = ColumnTypes.from_collector(collector_output)
    if types is None:
        return {"read_by": "column name (the collection has no column types)", "name_fallbacks": []}
    engine = source_engine_of(collector_output)
    fallbacks = []
    for q in (collector_output.get("queries") or {}).get("query_patterns") or []:
        reading = query_facts(q.get("query_text") or "", engine).timestamp_column(types)
        if reading.fallbacks:
            fallbacks.append(
                {
                    "query_id": q.get("query_id", ""),
                    "timestamp_column": reading.found,
                    "read_by_name": list(reading.fallbacks),
                }
            )
    return {"read_by": "column type", "name_fallbacks": fallbacks}


def _has_computed_join_key(text: str) -> bool:
    return any(_COMPUTED_EXPR_IN_CONDITION_RE.search(cond) for cond in _join_conditions(text))


# Covers the 20,000-query ingestion default (``ingestion_limits``) twice
# over: triage also reads facts of each query's lower-cased text. A collection
# raised past that by ``MODERNIZER_MAX_UPLOAD_QUERIES`` only recomputes what
# its readers ask for, since each fact is lazy.
@lru_cache(maxsize=65536)
def _query_facts(text: str, source_engine: str) -> QueryFacts:
    return QueryFacts(text, source_engine)


def query_facts(query_text: str | None, source_engine: str | None = None) -> QueryFacts:
    """The facts of ``query_text`` (``None`` reads as empty text), one object per
    text and source engine: computed facts are kept for every later reader. The
    source engine (the collector's ``metadata.source_database.engine``) picks the
    dialect the parsed facts are read in; none reads them in sqlglot's generic one.
    """
    return _query_facts(query_text or "", (source_engine or "").strip().lower())


# The readings each collected query pattern carries (``parsed_facts``, #934), by query
# text and source engine, for the QueryFacts that reads that text. Bounded like the
# QueryFacts cache: past the bound, the oldest are forgotten and parsed when read.
_STORED_READINGS: dict[tuple[str, str], Mapping] = {}
_STORED_READINGS_MAX = 65536
_STORED_READINGS_LOCK = threading.Lock()


def remember_stored_facts(query_text: str, source_engine: str | None, stored: object) -> None:
    """Use ``stored`` (a query pattern's ``parsed_facts``) as the reading of ``query_text``
    read for ``source_engine``. Facts it can't vouch for (another version, engine or
    text) are ignored when read, and the text is parsed instead."""
    if not isinstance(stored, Mapping) or not query_text:
        return
    key = (query_text, (source_engine or "").strip().lower())
    with _STORED_READINGS_LOCK:  # the analyses load the collection in parallel threads
        _STORED_READINGS.pop(key, None)
        _STORED_READINGS[key] = stored
        while len(_STORED_READINGS) > _STORED_READINGS_MAX:
            _STORED_READINGS.pop(next(iter(_STORED_READINGS)))


def remember_collected_facts(collector_output: Mapping | None) -> None:
    """Use every query pattern's stored facts (``parsed_facts``, #934) as the reading of
    its text, so the steps that read the collection's query facts parse none of the
    queries the collector already parsed. A step calls it when it loads the collection."""
    engine = source_engine_of(collector_output)
    queries = (collector_output or {}).get("queries") or {}
    patterns = queries.get("query_patterns") if isinstance(queries, Mapping) else queries
    for pattern in patterns or ():
        if isinstance(pattern, Mapping):
            remember_stored_facts(
                str(pattern.get("query_text") or ""), engine, pattern.get("parsed_facts")
            )


def source_engine_of(collector_output: Mapping | None) -> str:
    """The collection's source engine (``metadata.source_database.engine``), lower-cased."""
    source = ((collector_output or {}).get("metadata") or {}).get("source_database") or {}
    return str(source.get("engine") or "").strip().lower()
