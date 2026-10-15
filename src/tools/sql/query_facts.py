"""Engine-neutral query facts read from SQL text: one detector per fact (#491).

A detector here reports a fact about one query -- never a score, never an
engine name, never a capability. Engines differ only in how a fact is turned
into support or a score (the capability matrix in
``src.agents.referee.capability_registry``, the exclusion rules in
``src.agents.referee.engine_exclusions``), so the same query reads the same
way whichever engine is being scored against it, and a fact is defined once
instead of once per engine module (#485 traced a routing bug to two copies of
the leading-wildcard check that had drifted apart).

Two kinds of fact, from two readers:

- Parse-derived facts -- table references, join count, aggregation,
  subquery, leading-wildcard ``LIKE`` -- come from the sqlglot facade
  (:func:`src.tools.sql.facade.read_sql`), which needs the source engine's
  dialect. The collectors store them on each query pattern (``has_joins``,
  ``join_count``, ``has_aggregations``, ``has_subqueries``, #796).
- Text facts (:class:`QueryFacts`, this module) need no dialect: each is the
  regex scan its detector ran before #491, moved here unchanged, so every
  routing decision that reads one is byte-identical. A detector called with
  the query text alone -- the exclusion gate inside
  ``query_scoring.score_query_for_engine`` -- can read them. The
  leading-wildcard fact here is the facade's own regex fallback
  (:func:`src.tools.sql.regex_facts.regex_wildcard_leading`); it moves to the
  facade's parsed reading once that gate receives the source engine.

:func:`query_facts` returns one object per text, and each fact on it is
computed the first time a reader asks for it, then kept: a query read by
several detectors is scanned once per fact, and a reader never pays for a fact
it doesn't read (#895 review).
"""

from __future__ import annotations

import re
from functools import cached_property, lru_cache

from src.shared.unsupported_pattern import is_dedup_only_group_by
from src.tools.sql.regex_facts import regex_has_subquery, regex_join_count, regex_wildcard_leading

__all__ = ["QueryFacts", "query_facts"]

# ---------------------------------------------------------------------------
# Text search (the "inverted_index" capability's detectors)
# ---------------------------------------------------------------------------

_FULLTEXT_MATCH_RE = re.compile(r"MATCH\s*\(.+?\)\s*AGAINST\s*\(", re.IGNORECASE)
_TEXT_SEARCH_OPERATOR_RE = re.compile(r"to_tsvector|to_tsquery|@@", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Scan shapes (the "scan_engine" capability's detectors)
# ---------------------------------------------------------------------------

_RANKING_WINDOW_FUNCTION_RE = re.compile(
    r"\b(ROW_NUMBER|RANK|DENSE_RANK|NTILE|LAG|LEAD)\s*\(\s*\)\s*OVER\s*\(", re.IGNORECASE
)
# ``WITH RECURSIVE`` (the capability gate and the Aurora PostgreSQL
# recursive-CTE pattern read this one fact). The leading ``\b`` came from the
# PostgreSQL pattern; the gate's copy had none, which only differs when WITH is
# glued to a preceding word character, as in no SQL statement.
_RECURSIVE_CTE_RE = re.compile(r"\bWITH\s+RECURSIVE\b", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Aggregation (#338, #336)
#
# An aggregate function call needs real aggregation capability on one table
# just as much as a joined one: DynamoDB has no server-side SUM/COUNT/AVG
# regardless of how many tables feed it. A query with neither GROUP BY nor a
# join (a bare SELECT COUNT(*) FROM t) used to get no capability requirement
# the same way the join/aggregation gap did (#338) -- an aggregate function
# call alone is enough, so this fires independently of GROUP BY or join count.
# FOUND_ROWS()/SQL_CALC_FOUND_ROWS (MySQL's row-count-of-last-query modifier),
# GROUP_CONCAT/STRING_AGG/ARRAY_AGG (string and array aggregation) and any
# window function (anything immediately followed by ``OVER (...)``) all count
# too: DynamoDB has no equivalent for any of them.
#
# GROUP BY alone, with no aggregate function, is usually a deduplication
# technique (#336) rather than an aggregation. Rather than re-deriving that
# distinction here, ``dedup_only_group_by`` is
# ``src.shared.unsupported_pattern.is_dedup_only_group_by`` (the same
# definition the risk report uses, #371), so the shared function is the single
# thing that decides it: GROUP BY needs aggregation exactly when that function
# says it is not just a dedup.
# ---------------------------------------------------------------------------

_AGGREGATE_FUNCTION_CALL_RE = re.compile(
    r"`?\b(sum|count|avg|min|max|found_rows|group_concat|string_agg|array_agg)\b`?\s*\(",
    re.IGNORECASE,
)
_SQL_CALC_FOUND_ROWS_RE = re.compile(r"\bsql_calc_found_rows\b", re.IGNORECASE)
# Any function call immediately followed by OVER (...) is a window function
# (ROW_NUMBER() OVER (...), SUM(x) OVER (...), ...) -- an aggregate computed
# per-row over a window.
_WINDOW_FUNCTION_RE = re.compile(r"\)\s*OVER\s*\(", re.IGNORECASE)
_GROUP_BY_RE = re.compile(r"\bGROUP\s+BY\b", re.IGNORECASE)

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
_JOIN_ON_CLAUSE_RE = re.compile(
    r"\bjoin\b.*?\bon\b\s*(?P<cond>.*?)(?=\b(?:where|group\s+by|order\s+by|having|"
    r"left|right|inner|full|cross|join|limit)\b|$)",
    re.IGNORECASE | re.DOTALL,
)
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

_WRITE_WITH_SUBQUERY_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE)\b.*\bSELECT\b", re.IGNORECASE | re.DOTALL
)


# ---------------------------------------------------------------------------
# Relational shape keywords (the Aurora analysis's detectors, moved here from
# ``aurora_common_pattern_catalog``). Case-insensitive substring checks on the
# raw text: they read comments, literals and column names too (``limit`` in
# ``credit_limit``), which is why they are kept apart from the parsed facts.
# ---------------------------------------------------------------------------

AGGREGATION_KEYWORDS: tuple[str, ...] = (
    "group by",
    "having",
    "sum(",
    "count(",
    "avg(",
    "min(",
    "max(",
)

# EXISTS (subquery), NOT EXISTS (subquery), IN (SELECT ...)
_EXISTS_OR_IN_SUBQUERY_RE = re.compile(
    r"""
    \bEXISTS\s*\(               # EXISTS (subquery)
    |
    \bNOT\s+EXISTS\s*\(         # NOT EXISTS (subquery)
    |
    \bIN\s*\(\s*SELECT\b        # IN (SELECT ...)
    """,
    re.IGNORECASE | re.VERBOSE,
)

PAGINATION_KEYWORDS: tuple[str, ...] = (
    "limit",
    "offset",
    "fetch first",
    "fetch next",
)

IN_LIST_KEYWORDS: tuple[str, ...] = (
    " in (",
    " in(",
)

# Text search indicators (for the volume-based high-volume-text-search check).
# The ``like N'%`` entry is compared with lower-cased text, so it never
# matches; it is kept as found (#491 moves detectors, it does not change them).
TEXT_SEARCH_KEYWORDS: tuple[str, ...] = (
    "like '%",
    "like N'%",
    "ilike '%",
    "to_tsvector",
    "plainto_tsquery",
    "match(",
    "match (",
)

# ---------------------------------------------------------------------------
# MySQL syntax (the Aurora MySQL analysis's detectors, moved here from
# ``aurora_mysql_pattern_catalog``): case-insensitive substring checks.
# ---------------------------------------------------------------------------

JSON_FUNCTION_KEYWORDS: tuple[str, ...] = (
    "json_extract(",
    "json_contains(",
    "json_value(",
    "json_set(",
    "json_array(",
    "json_object(",
    "->>",
    "->",
)

MATCH_AGAINST_KEYWORDS: tuple[str, ...] = (
    "match(",
    "match (",
    "against(",
    "against (",
)

DUPLICATE_KEY_KEYWORDS: tuple[str, ...] = ("on duplicate key", "replace into")

GROUP_CONCAT_KEYWORDS: tuple[str, ...] = ("group_concat(",)

STORED_ROUTINE_KEYWORDS: tuple[str, ...] = (
    "call ",
    "delimiter",
    "create procedure",
    "create function",
)

# ---------------------------------------------------------------------------
# PostgreSQL syntax (the Aurora PostgreSQL analysis's detectors, moved here
# from ``aurora_pg_pattern_catalog``).
# ---------------------------------------------------------------------------

# Window functions: named window functions OR ) OVER ( OR OVER (PARTITION|ORDER BY
_WINDOW_FUNCTION_KEYWORD_RE = re.compile(
    r"""
    \b(?:ROW_NUMBER|RANK|DENSE_RANK|LAG|LEAD|NTILE|FIRST_VALUE|LAST_VALUE|CUME_DIST|PERCENT_RANK)\s*\(
    |
    \)\s*OVER\s*\(
    |
    \bOVER\s*\(\s*(?:PARTITION\s+BY|ORDER\s+BY)
    """,
    re.IGNORECASE | re.VERBOSE,
)

# JSONB operations:
#   - ->> operator
#   - jsonb_* function names
#   - ::jsonb cast
#   - @> followed by a JSON object/string literal (single-quoted, NOT ARRAY)
_JSONB_OPERATION_RE = re.compile(
    r"""
    ->>
    |
    \bjsonb_(?:agg|build_object|build_array|each|each_text|array_elements|array_elements_text|
               populate_record|typeof|strip|pretty|set|insert|delete|path_exists|path_query)\s*\(
    |
    ::jsonb\b
    |
    @>\s*'
    """,
    re.IGNORECASE | re.VERBOSE,
)

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

# Full-text search: tsvector/tsquery functions and @@ operator in tsquery context
_TSVECTOR_SEARCH_RE = re.compile(
    r"""
    \b(?:to_tsvector|to_tsquery|plainto_tsquery|phraseto_tsquery|websearch_to_tsquery)\s*\(
    |
    \btsvector\b
    |
    \bts_rank\s*\(
    |
    @@
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

# Upsert: ON CONFLICT keyword OR RETURNING used as a SQL clause.
# RETURNING as a SQL clause follows DML (not preceded by AS keyword).
# Matches: RETURNING id, RETURNING *, RETURNING col1, col2
# Does NOT match: AS returning FROM (alias usage)
_ON_CONFLICT_OR_RETURNING_RE = re.compile(
    r"""
    \bON\s+CONFLICT\b
    |
    (?<!AS\s)(?<!\bAS\b)\bRETURNING\s+[\w*]
    """,
    re.IGNORECASE | re.VERBOSE,
)


# ---------------------------------------------------------------------------
# Key-value access shape (the DynamoDB analysis's detectors, moved here from
# ``dynamodb_analysis_tools``).
# ---------------------------------------------------------------------------

# A column name that looks like a timestamp (created_at, ttl, last_seen_at, ...).
_TIMESTAMP_COLUMN_RE = re.compile(
    r"\b(created_at|updated_at|timestamp|date|time|expires?_at|ttl|last_\w+_at)\b", re.I
)
# A word that looks like session or auth data.
_SESSION_WORD_RE = re.compile(r"\b(session|token|sess_id|session_id|login|auth)\b", re.I)
# GROUP BY, HAVING or COUNT/SUM/AVG followed by "(" -- the trailing \b needs a
# word character right after the "(", so ``count(*)`` does not match it but
# ``count(id)`` does (kept as found, #491).
_AGGREGATION_CALL_WORD_RE = re.compile(
    r"\b(group\s+by|having|count\s*\(|sum\s*\(|avg\s*\()\b", re.I
)
# BETWEEN, or < / <= / > / >= with a word character before them (``a>=1``,
# not ``a >= 1``), read on the lower-cased text.
_RANGE_OPERATOR_WORD_RE = re.compile(r"\b(between|>=?|<=?)\b")


# ---------------------------------------------------------------------------
# Document shapes (the DocumentDB analysis's detectors, moved here from
# ``documentdb_analysis_tools``).
# ---------------------------------------------------------------------------

# GROUP BY, HAVING or COUNT/SUM/AVG/MIN/MAX followed by "(" and a word
# character (the DynamoDB copy, ``_AGGREGATION_CALL_WORD_RE``, has no MIN/MAX).
_AGGREGATE_CALL_WORD_RE = re.compile(
    r"\b(group\s+by|having|count\s*\(|sum\s*\(|avg\s*\(|min\s*\(|max\s*\()\b", re.I
)
_WINDOW_FUNCTION_WORD_RE = re.compile(
    r"\b(row_number|rank|dense_rank|ntile|lag|lead|over\s*\()\b", re.I
)
# The window functions DocumentDB 8.0.2+ serves through $setWindowFields
# (ROW_NUMBER/RANK/DENSE_RANK as $documentNumber/$rank/$denseRank, and
# aggregates such as SUM(...) OVER for running totals), and the ones it has no
# operator for on any version (LAG/LEAD need $shift; NTILE has none), per the
# supported-APIs page.
_SET_WINDOW_FIELDS_RE = re.compile(
    r"\b(row_number|rank|dense_rank)\s*\("
    r"|\b(sum|avg|min|max|count)\s*\((?:[^()]|\([^()]*\))*\)\s*over\b",
    re.I,
)
_OFFSET_OR_BUCKET_WINDOW_CALL_RE = re.compile(r"\b(lag|lead|ntile)\s*\(", re.I)
_NEGATION_WORD_RE = re.compile(r"\b(not\s+in|not\s+exists|<>|!=)\b", re.I)
_CORRELATED_SUBQUERY_SHAPE_RE = re.compile(
    r"\(\s*select\b.*?\bwhere\b.*?\b\w+\.\w+\s*=\s*\w+\.\w+", re.I | re.S
)
_JSON_EXTRACT_WORD_RE = re.compile(r"\b(json_extract|json_unquote|json_value|->>|->)\b", re.I)


# ---------------------------------------------------------------------------
# Search and time-series keywords (the OpenSearch analysis's detectors, moved
# here from ``opensearch_pattern_catalog``): case-insensitive substring checks.
# ---------------------------------------------------------------------------

FULLTEXT_KEYWORDS: tuple[str, ...] = (
    "tsvector",
    "tsquery",
    "to_tsvector",
    "plainto_tsquery",
    "@@",
    "match against",
)

WILDCARD_KEYWORDS: tuple[str, ...] = (
    "like ",
    "ilike ",
)

REGEX_KEYWORDS: tuple[str, ...] = (
    "regexp",
    "rlike",
    "~ ",
    "~* ",
)

FUZZY_KEYWORDS: tuple[str, ...] = (
    "pg_trgm",
    "similarity(",
)

TIMESTAMP_KEYWORDS: tuple[str, ...] = (
    "timestamp",
    "created_at",
    "updated_at",
    "event_time",
    "log_time",
    "ingested_at",
)

TIME_RANGE_KEYWORDS: tuple[str, ...] = (
    "between",
    ">=",
    "<=",
)

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

SESSION_OR_USER_KEYWORDS: tuple[str, ...] = ("session", "user_id")
TIMESTAMP_OR_DATE_KEYWORDS: tuple[str, ...] = ("timestamp", "created_at", "updated_at", "date")
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
_EXISTS_CALL_RE = re.compile(r"\b(exists|not\s+exists)\s*\(")
_IS_NULL_RE = re.compile(r"\bis\s+null\b")
_COUNT_STAR_OR_ONE_RE = re.compile(r"\bcount\s*\(\s*[\*1]\s*\)")
_EVENT_TIMESTAMP_WORD_RE = re.compile(
    r"\b(created_at|updated_at|timestamp|event_time|open_date|close_date|"
    r"created_date|modified_date|log_time|event_date)\b",
    re.IGNORECASE,
)
_LIKE_PATTERN_VALUE_RE = re.compile(r"\b(like|ilike)\s+['\"\?%]")
_FULLTEXT_WORD_RE = re.compile(
    r"\b(tsvector|tsquery|to_tsvector|plainto_tsquery|match\s+against)\b"
)
_REGEXP_WORD_RE = re.compile(r"\b(regexp|rlike)\b")
_SIMILARITY_OR_TRIGRAM_RE = re.compile(r"\b(similarity\s*\(|pg_trgm)\b")


# ---------------------------------------------------------------------------
# Top-N and session-key shapes (moved here from ``src.agents.referee.cache_overlay``,
# #296/#663). Triage and the cache overlay read the same facts.
# ---------------------------------------------------------------------------

# A top-N read: ``ORDER BY ... LIMIT n``, or its T-SQL and ANSI spellings (#663),
# ``SELECT TOP (n) ... ORDER BY`` and ``ORDER BY ... [OFFSET ...] FETCH {FIRST|NEXT}``.
# TOP takes a number, a ``(expression)`` or a variable, so a column named ``top``
# is not one; TOP with no ORDER BY returns arbitrary rows and is not a top-N.
_TOP_N_RE = re.compile(
    r"order\s+by\b.*\b(?:limit|fetch\s+(?:first|next))\b|\btop\s*(?:\(|\d|@).*\border\s+by\b",
    re.IGNORECASE | re.DOTALL,
)
# Same keywords for triage's session_store signal and the cache overlay (``session_key``).
_SESSION_RE = re.compile(r"\b(session|token|sess_id|session_id|csrf)\b", re.IGNORECASE)
# The parts of a PascalCase / camelCase name: ``SessionToken`` -> Session, Token;
# ``CSRFToken`` -> CSRF, Token; ``sessionId`` -> session, Id; ``SessionID`` -> Session, ID.
_NAME_PART_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_IDENTIFIER_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*")
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
    """Whether a PascalCase/camelCase ``name`` is a two-part session or token key."""
    parts = [p.lower() for p in _NAME_PART_RE.findall(name)]
    if len(parts) != 2:
        return False
    head, tail = parts
    return (tail == "token" and head in _SESSION_TOKEN_PREFIXES) or (
        head == "session" and tail in _SESSION_KEY_SUFFIXES
    )


def _has_session_key(text: str) -> bool:
    """Whether ``text`` is a session/token statement (``session_store``, #663).

    Either ``_SESSION_RE`` matches a whole word (``session``, ``token``,
    ``session_id``, ...), unchanged from before, or the WHERE clause filters on a
    PascalCase/camelCase session key such as T-SQL's ``[SessionToken]``,
    ``[CSRFToken]``, ``sessionId`` (:func:`_is_session_key_name`). The second rule
    is deliberately narrow, because ``session_store`` selects analysis agents on
    its own: a name part such as ``[SessionTimeout]`` or ``[TokenCount]`` does not
    count, and neither does a ``[UserSession]`` query keyed by something else or
    a session key named only in the select list. Needs the SQL in its original
    case.
    """
    text = text or ""
    if _SESSION_RE.search(text):
        return True
    where = _WHERE_CLAUSE_RE.search(text)
    if where is None:
        return False
    return any(_is_session_key_name(name) for name in _IDENTIFIER_RE.findall(where.group(1)))


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

    def __init__(self, text: str) -> None:
        self._text = text

    @cached_property
    def _lower(self) -> str:
        return self._text.lower()

    @cached_property
    def leading_wildcard_like(self) -> bool:
        """``LIKE`` followed by a quote and ``%`` (``ILIKE`` and ``NOT LIKE`` included)."""
        return regex_wildcard_leading(self._text)

    @cached_property
    def fulltext_match(self) -> bool:
        """MySQL ``MATCH (...) AGAINST (``."""
        return bool(_FULLTEXT_MATCH_RE.search(self._text))

    @cached_property
    def text_search_operator(self) -> bool:
        """PostgreSQL ``to_tsvector``, ``to_tsquery`` or ``@@``."""
        return bool(_TEXT_SEARCH_OPERATOR_RE.search(self._text))

    @cached_property
    def ranking_window_function(self) -> bool:
        """``ROW_NUMBER``/``RANK``/``DENSE_RANK``/ ``NTILE``/``LAG``/``LEAD`` with an empty
        argument list and ``OVER (``.
        """
        return bool(_RANKING_WINDOW_FUNCTION_RE.search(self._text))

    @cached_property
    def recursive_cte(self) -> bool:
        """``WITH RECURSIVE``."""
        return bool(_RECURSIVE_CTE_RE.search(self._text))

    @cached_property
    def aggregate_function_call(self) -> bool:
        """A call to ``SUM``/``COUNT``/``AVG``/``MIN``/
        ``MAX``/``FOUND_ROWS``/``GROUP_CONCAT``/``STRING_AGG``/``ARRAY_AGG``
        (backtick-quoted names included).
        """
        return bool(_AGGREGATE_FUNCTION_CALL_RE.search(self._text))

    @cached_property
    def sql_calc_found_rows(self) -> bool:
        """MySQL's ``SQL_CALC_FOUND_ROWS`` modifier."""
        return bool(_SQL_CALC_FOUND_ROWS_RE.search(self._text))

    @cached_property
    def window_function(self) -> bool:
        """``)`` followed by ``OVER (`` -- any window function."""
        return bool(_WINDOW_FUNCTION_RE.search(self._text))

    @cached_property
    def group_by(self) -> bool:
        """A ``GROUP BY`` anywhere in the text."""
        return bool(_GROUP_BY_RE.search(self._text))

    @cached_property
    def dedup_only_group_by(self) -> bool:
        """The ``GROUP BY`` only de-duplicates rows
        (``unsupported_pattern.is_dedup_only_group_by``, #336).
        """
        return self.group_by and is_dedup_only_group_by(self._text)

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
        return bool(_WRITE_WITH_SUBQUERY_RE.search(self._text))

    @cached_property
    def join_keyword_count(self) -> int:
        """How many times the word ``join`` appears (the regex join count,
        :func:`src.tools.sql.regex_facts.regex_join_count`).
        """
        return regex_join_count(self._text)

    @cached_property
    def aggregation_keyword(self) -> bool:
        """An :data:`AGGREGATION_KEYWORDS` substring (``group by``, ``having``, ``count(``,
        ...).
        """
        return _contains_any(self._lower, AGGREGATION_KEYWORDS)

    @cached_property
    def exists_or_in_subquery(self) -> bool:
        """``EXISTS (``, ``NOT EXISTS (`` or ``IN (SELECT``."""
        return bool(_EXISTS_OR_IN_SUBQUERY_RE.search(self._text))

    @cached_property
    def pagination_keyword(self) -> bool:
        """A :data:`PAGINATION_KEYWORDS` substring (``limit``, ``offset``, ``fetch first``,
        ``fetch next``).
        """
        return _contains_any(self._lower, PAGINATION_KEYWORDS)

    @cached_property
    def in_list(self) -> bool:
        """`` in (`` or `` in(``."""
        return _contains_any(self._lower, IN_LIST_KEYWORDS)

    @cached_property
    def like_percent_literal(self) -> bool:
        """The lower-cased text contains ``like '%`` (one space, a single quote; ``ilike
        '%`` included).
        """
        return "like '%" in self._lower

    @cached_property
    def text_search_keyword(self) -> bool:
        """A :data:`TEXT_SEARCH_KEYWORDS` substring."""
        return _contains_any(self._lower, TEXT_SEARCH_KEYWORDS)

    @cached_property
    def json_function_keyword(self) -> bool:
        """A :data:`JSON_FUNCTION_KEYWORDS` substring (``json_extract(``, ..., ``->``)."""
        return _contains_any(self._lower, JSON_FUNCTION_KEYWORDS)

    @cached_property
    def match_against_keyword(self) -> bool:
        """``match(``, ``match (``, ``against(`` or ``against (``."""
        return _contains_any(self._lower, MATCH_AGAINST_KEYWORDS)

    @cached_property
    def duplicate_key_upsert(self) -> bool:
        """``on duplicate key`` or ``replace into``."""
        return _contains_any(self._lower, DUPLICATE_KEY_KEYWORDS)

    @cached_property
    def group_concat_call(self) -> bool:
        """``group_concat(``."""
        return _contains_any(self._lower, GROUP_CONCAT_KEYWORDS)

    @cached_property
    def stored_routine_keyword(self) -> bool:
        """A :data:`STORED_ROUTINE_KEYWORDS` substring (``call ``, ``delimiter``, ``create
        procedure``, ``create function``).
        """
        return _contains_any(self._lower, STORED_ROUTINE_KEYWORDS)

    @cached_property
    def window_function_keyword(self) -> bool:
        """A named window function call (``ROW_NUMBER(``, ``RANK(``, ...
        ``PERCENT_RANK(``), ``) OVER (``, or ``OVER (PARTITION BY``/``OVER (ORDER BY``.
        """
        return bool(_WINDOW_FUNCTION_KEYWORD_RE.search(self._text))

    @cached_property
    def jsonb_operation(self) -> bool:
        """``->>``, a ``jsonb_*(`` function, ``::jsonb`` or ``@> '``."""
        return bool(_JSONB_OPERATION_RE.search(self._text))

    @cached_property
    def array_operation(self) -> bool:
        """``ANY(ARRAY``, ``array_agg(``, ``unnest(``, ``ARRAY[`` or ``@> ARRAY``."""
        return bool(_ARRAY_OPERATION_RE.search(self._text))

    @cached_property
    def tsvector_search(self) -> bool:
        """A ``to_tsvector``/``*to_tsquery`` call, ``tsvector``, ``ts_rank(`` or ``@@``."""
        return bool(_TSVECTOR_SEARCH_RE.search(self._text))

    @cached_property
    def lateral_join(self) -> bool:
        """``JOIN LATERAL (``, ``, LATERAL (`` or ``LATERAL (``."""
        return bool(_LATERAL_JOIN_RE.search(self._text))

    @cached_property
    def on_conflict_or_returning(self) -> bool:
        """``ON CONFLICT``, or ``RETURNING <column>`` not preceded by ``AS``."""
        return bool(_ON_CONFLICT_OR_RETURNING_RE.search(self._text))

    @cached_property
    def timestamp_column_word(self) -> bool:
        """A timestamp-like word (``created_at``, ``updated_at``, ``timestamp``, ``date``,
        ``time``, ``expires_at``, ``ttl``, ``last_*_at``).
        """
        return bool(_TIMESTAMP_COLUMN_RE.search(self._text))

    @cached_property
    def session_word(self) -> bool:
        """``session``, ``token``, ``sess_id``, ``session_id``, ``login`` or ``auth`` as a
        word.
        """
        return bool(_SESSION_WORD_RE.search(self._text))

    @cached_property
    def aggregation_call_word(self) -> bool:
        """``GROUP BY``, ``HAVING``, or ``COUNT(``/``SUM(``/ ``AVG(`` followed by a word
        character (``count(*)`` is not one).
        """
        return bool(_AGGREGATION_CALL_WORD_RE.search(self._text))

    @cached_property
    def range_with_order_by(self) -> bool:
        """``BETWEEN`` or a word character followed by ``<``/``<=``/``>``/``>=``, and
        ``order by`` somewhere in the text.
        """
        return bool(_RANGE_OPERATOR_WORD_RE.search(self._lower)) and self.order_by_substring

    @cached_property
    def limit_word(self) -> bool:
        """The lower-cased text contains ``limit``."""
        return "limit" in self._lower

    @cached_property
    def aggregate_call_word_count(self) -> int:
        """How many ``GROUP BY``, ``HAVING`` or
        ``COUNT(``/``SUM(``/``AVG(``/``MIN(``/``MAX(`` followed by a word character the
        text has (``aggregation_call_word`` without ``MIN``/``MAX``, counted).
        """
        return len(_AGGREGATE_CALL_WORD_RE.findall(self._text))

    @cached_property
    def window_function_word(self) -> bool:
        """``row_number``, ``rank``, ``dense_rank``, ``ntile``, ``lag``, ``lead`` as a
        word, or ``over (`` followed by a word character.
        """
        return bool(_WINDOW_FUNCTION_WORD_RE.search(self._text))

    @cached_property
    def set_window_fields_shape(self) -> bool:
        """A ``ROW_NUMBER(``/``RANK(``/``DENSE_RANK(`` call, or
        ``SUM``/``AVG``/``MIN``/``MAX``/``COUNT(...) OVER``.
        """
        return bool(_SET_WINDOW_FIELDS_RE.search(self._text))

    @cached_property
    def offset_or_bucket_window_call(self) -> bool:
        """A ``LAG(``, ``LEAD(`` or ``NTILE(`` call."""
        return bool(_OFFSET_OR_BUCKET_WINDOW_CALL_RE.search(self._text))

    @cached_property
    def negation_word(self) -> bool:
        """``NOT IN``, ``NOT EXISTS``, ``<>`` or ``!=`` between word characters."""
        return bool(_NEGATION_WORD_RE.search(self._text))

    @cached_property
    def correlated_subquery_shape(self) -> bool:
        """``( SELECT ... WHERE ... a.b = c.d``."""
        return bool(_CORRELATED_SUBQUERY_SHAPE_RE.search(self._text))

    @cached_property
    def json_extract_word(self) -> bool:
        """``json_extract``, ``json_unquote``, ``json_value``, ``->>`` or ``->`` between
        word characters.
        """
        return bool(_JSON_EXTRACT_WORD_RE.search(self._text))

    @cached_property
    def group_by_substring(self) -> bool:
        """The lower-cased text contains ``group by`` (one space)."""
        return "group by" in self._lower

    @cached_property
    def comma_count(self) -> int:
        """How many commas the text has."""
        return self._lower.count(",")

    @cached_property
    def fulltext_keyword(self) -> bool:
        """A :data:`FULLTEXT_KEYWORDS` substring (``tsvector``, ``tsquery``, ``@@``,
        ``match against``, ...).
        """
        return _contains_any(self._lower, FULLTEXT_KEYWORDS)

    @cached_property
    def like_keyword(self) -> bool:
        """``like `` or ``ilike `` (followed by a space)."""
        return _contains_any(self._lower, WILDCARD_KEYWORDS)

    @cached_property
    def regex_keyword(self) -> bool:
        """``regexp``, ``rlike``, ``~ `` or ``~* ``."""
        return _contains_any(self._lower, REGEX_KEYWORDS)

    @cached_property
    def fuzzy_keyword(self) -> bool:
        """``pg_trgm`` or ``similarity(``."""
        return _contains_any(self._lower, FUZZY_KEYWORDS)

    @cached_property
    def timestamp_keyword(self) -> bool:
        """A :data:`TIMESTAMP_KEYWORDS` substring (``timestamp``, ``created_at``,
        ``updated_at``, ``event_time``, ``log_time``, ``ingested_at``).
        """
        return _contains_any(self._lower, TIMESTAMP_KEYWORDS)

    @cached_property
    def time_range_operator(self) -> bool:
        """``between``, ``>=`` or ``<=``."""
        return _contains_any(self._lower, TIME_RANGE_KEYWORDS)

    @cached_property
    def time_function_keyword(self) -> bool:
        """``date_trunc``, ``date(``, ``hour(`` or ``extract(``."""
        return _contains_any(self._lower, TIME_AGG_KEYWORDS)

    @cached_property
    def session_or_user_keyword(self) -> bool:
        """``session`` or ``user_id`` anywhere."""
        return _contains_any(self._lower, SESSION_OR_USER_KEYWORDS)

    @cached_property
    def order_by_substring(self) -> bool:
        """The lower-cased text contains ``order by`` (one space)."""
        return "order by" in self._lower

    @cached_property
    def timestamp_or_date_keyword(self) -> bool:
        """``timestamp``, ``created_at``, ``updated_at`` or ``date`` anywhere."""
        return _contains_any(self._lower, TIMESTAMP_OR_DATE_KEYWORDS)

    @cached_property
    def geospatial_keyword(self) -> bool:
        """A :data:`GEOSPATIAL_KEYWORDS` substring (``st_distance``, ..., ``latitude``,
        ``geometry``).
        """
        return _contains_any(self._lower, GEOSPATIAL_KEYWORDS)

    @cached_property
    def min_max_call(self) -> bool:
        """A ``MAX(`` or ``MIN(`` call."""
        return bool(_MIN_MAX_CALL_RE.search(self._lower))

    @cached_property
    def status_equals_value(self) -> bool:
        """``status = ?`` / ``status = '...`` / ``status = "...``."""
        return bool(_STATUS_EQUALS_VALUE_RE.search(self._lower))

    @cached_property
    def exists_call(self) -> bool:
        """``EXISTS (`` or ``NOT EXISTS (``."""
        return bool(_EXISTS_CALL_RE.search(self._lower))

    @cached_property
    def is_null(self) -> bool:
        """``IS NULL``."""
        return bool(_IS_NULL_RE.search(self._lower))

    @cached_property
    def join_with_spaces_count(self) -> int:
        """How many times `` join `` (a space on each side) appears in the lower-cased text."""
        return self._lower.count(" join ")

    @cached_property
    def count_star_or_one(self) -> bool:
        """``COUNT(*)`` or ``COUNT(1)``."""
        return bool(_COUNT_STAR_OR_ONE_RE.search(self._lower))

    @cached_property
    def event_timestamp_word(self) -> bool:
        """An event-time column word (``created_at``, ``updated_at``, ``timestamp``,
        ``event_time``, ``open_date``, ``close_date``, ``created_date``,
        ``modified_date``, ``log_time``, ``event_date``).
        """
        return bool(_EVENT_TIMESTAMP_WORD_RE.search(self._lower))

    @cached_property
    def like_pattern_value(self) -> bool:
        """``LIKE``/``ILIKE`` followed by a quote, ``?`` or ``%``."""
        return bool(_LIKE_PATTERN_VALUE_RE.search(self._lower))

    @cached_property
    def fulltext_word(self) -> bool:
        """``tsvector``, ``tsquery``, ``to_tsvector``, ``plainto_tsquery`` or
        ``match against`` as a word.
        """
        return bool(_FULLTEXT_WORD_RE.search(self._lower))

    @cached_property
    def regexp_word(self) -> bool:
        """``regexp`` or ``rlike`` as a word."""
        return bool(_REGEXP_WORD_RE.search(self._lower))

    @cached_property
    def similarity_or_trigram(self) -> bool:
        """``similarity(`` followed by a word character, or ``pg_trgm``."""
        return bool(_SIMILARITY_OR_TRIGRAM_RE.search(self._lower))

    @cached_property
    def select_word_repeated(self) -> bool:
        """More than one ``select`` substring (the regex subquery test,
        :func:`src.tools.sql.regex_facts.regex_has_subquery`).
        """
        return regex_has_subquery(self._text)

    @cached_property
    def top_n_shape(self) -> bool:
        """A top-N read: ``ORDER BY ... LIMIT``/``FETCH FIRST|NEXT``, or
        ``TOP (n)``/``TOP n``/``TOP @v`` ... ``ORDER BY``.
        """
        return bool(_TOP_N_RE.search(self._text))

    @cached_property
    def session_key(self) -> bool:
        """A session/token statement (#663): a session word (``session``, ``token``,
        ``sess_id``, ``session_id``, ``csrf``), or a two-part PascalCase/camelCase session
        key in the WHERE clause (``[SessionToken]``, ``sessionId``). Reads the original case.
        """
        return _has_session_key(self._text)

    @property
    def needs_aggregation(self) -> bool:
        """An aggregate call, ``SQL_CALC_FOUND_ROWS`` or a window function, or a
        ``GROUP BY`` that does more than de-duplicate (#338, #336)."""
        if self.aggregate_function_call or self.sql_calc_found_rows or self.window_function:
            return True
        return self.group_by and not self.dedup_only_group_by


def _has_computed_join_key(text: str) -> bool:
    return any(
        _COMPUTED_EXPR_IN_CONDITION_RE.search(m.group("cond"))
        for m in _JOIN_ON_CLAUSE_RE.finditer(text)
    )


# Covers the 20,000-query ingestion default (``ingestion_limits``) twice
# over: triage also reads facts of each query's lower-cased text. A collection
# raised past that by ``MODERNIZER_MAX_UPLOAD_QUERIES`` only recomputes what
# its readers ask for, since each fact is lazy.
@lru_cache(maxsize=65536)
def _query_facts(text: str) -> QueryFacts:
    return QueryFacts(text)


def query_facts(query_text: str | None) -> QueryFacts:
    """The text facts of ``query_text`` (``None`` reads as empty text), one
    object per text: computed facts are kept for every later reader."""
    return _query_facts(query_text or "")
