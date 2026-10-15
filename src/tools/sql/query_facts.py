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

:func:`query_facts` caches its answer per text, so a query read by several
detectors (the capability gate, the exclusion gate, the shape checks) is
scanned once.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from src.shared.unsupported_pattern import is_dedup_only_group_by
from src.tools.sql.regex_facts import regex_join_count, regex_wildcard_leading

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


def _contains_any(lower_text: str, keywords: tuple[str, ...]) -> bool:
    """Whether lower-cased text contains any keyword (keywords compared as written)."""
    return any(kw in lower_text for kw in keywords)


@dataclass(frozen=True)
class QueryFacts:
    """The text facts of one query (see the module docstring).

    Attributes:
        leading_wildcard_like: ``LIKE`` followed by a quote and ``%``
            (``ILIKE`` and ``NOT LIKE`` included).
        fulltext_match: MySQL ``MATCH (...) AGAINST (``.
        text_search_operator: PostgreSQL ``to_tsvector``, ``to_tsquery`` or ``@@``.
        ranking_window_function: ``ROW_NUMBER``/``RANK``/``DENSE_RANK``/
            ``NTILE``/``LAG``/``LEAD`` with an empty argument list and ``OVER (``.
        recursive_cte: ``WITH RECURSIVE``.
        aggregate_function_call: A call to ``SUM``/``COUNT``/``AVG``/``MIN``/
            ``MAX``/``FOUND_ROWS``/``GROUP_CONCAT``/``STRING_AGG``/``ARRAY_AGG``
            (backtick-quoted names included).
        sql_calc_found_rows: MySQL's ``SQL_CALC_FOUND_ROWS`` modifier.
        window_function: ``)`` followed by ``OVER (`` -- any window function.
        group_by: A ``GROUP BY`` anywhere in the text.
        dedup_only_group_by: The ``GROUP BY`` only de-duplicates rows
            (``unsupported_pattern.is_dedup_only_group_by``, #336).
        computed_join_key: Some ``JOIN ... ON`` condition compares a function
            call with a column (``lower(g.name) = u.username_lower``, either
            side); a function compared with a bind parameter is a filter, not
            a computed join key.
        write_with_subquery: ``INSERT``/``UPDATE``/``DELETE`` followed later
            by ``SELECT``.
        join_keyword_count: How many times the word ``join`` appears (the
            regex join count, :func:`src.tools.sql.regex_facts.regex_join_count`).
        aggregation_keyword: An :data:`AGGREGATION_KEYWORDS` substring
            (``group by``, ``having``, ``count(``, ...).
        exists_or_in_subquery: ``EXISTS (``, ``NOT EXISTS (`` or ``IN (SELECT``.
        pagination_keyword: A :data:`PAGINATION_KEYWORDS` substring (``limit``,
            ``offset``, ``fetch first``, ``fetch next``).
        in_list: `` in (`` or `` in(``.
        like_percent_literal: The lower-cased text contains ``like '%`` (one
            space, a single quote; ``ilike '%`` included).
        text_search_keyword: A :data:`TEXT_SEARCH_KEYWORDS` substring.
        json_function_keyword: A :data:`JSON_FUNCTION_KEYWORDS` substring
            (``json_extract(``, ..., ``->``).
        match_against_keyword: ``match(``, ``match (``, ``against(`` or ``against (``.
        duplicate_key_upsert: ``on duplicate key`` or ``replace into``.
        group_concat_call: ``group_concat(``.
        stored_routine_keyword: A :data:`STORED_ROUTINE_KEYWORDS` substring
            (``call ``, ``delimiter``, ``create procedure``, ``create function``).
        window_function_keyword: A named window function call (``ROW_NUMBER(``,
            ``RANK(``, ... ``PERCENT_RANK(``), ``) OVER (``, or ``OVER (PARTITION
            BY``/``OVER (ORDER BY``.
        jsonb_operation: ``->>``, a ``jsonb_*(`` function, ``::jsonb`` or ``@> '``.
        array_operation: ``ANY(ARRAY``, ``array_agg(``, ``unnest(``,
            ``ARRAY[`` or ``@> ARRAY``.
        tsvector_search: A ``to_tsvector``/``*to_tsquery`` call, ``tsvector``,
            ``ts_rank(`` or ``@@``.
        lateral_join: ``JOIN LATERAL (``, ``, LATERAL (`` or ``LATERAL (``.
        on_conflict_or_returning: ``ON CONFLICT``, or ``RETURNING <column>``
            not preceded by ``AS``.
    """

    leading_wildcard_like: bool
    fulltext_match: bool
    text_search_operator: bool
    ranking_window_function: bool
    recursive_cte: bool
    aggregate_function_call: bool
    sql_calc_found_rows: bool
    window_function: bool
    group_by: bool
    dedup_only_group_by: bool
    computed_join_key: bool
    write_with_subquery: bool
    join_keyword_count: int
    aggregation_keyword: bool
    exists_or_in_subquery: bool
    pagination_keyword: bool
    in_list: bool
    like_percent_literal: bool
    text_search_keyword: bool
    json_function_keyword: bool
    match_against_keyword: bool
    duplicate_key_upsert: bool
    group_concat_call: bool
    stored_routine_keyword: bool
    window_function_keyword: bool
    jsonb_operation: bool
    array_operation: bool
    tsvector_search: bool
    lateral_join: bool
    on_conflict_or_returning: bool

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


@lru_cache(maxsize=16384)
def query_facts(query_text: str | None) -> QueryFacts:
    """The text facts of ``query_text`` (``None`` reads as empty text)."""
    text = query_text or ""
    lower = text.lower()
    group_by = bool(_GROUP_BY_RE.search(text))
    return QueryFacts(
        leading_wildcard_like=regex_wildcard_leading(text),
        fulltext_match=bool(_FULLTEXT_MATCH_RE.search(text)),
        text_search_operator=bool(_TEXT_SEARCH_OPERATOR_RE.search(text)),
        ranking_window_function=bool(_RANKING_WINDOW_FUNCTION_RE.search(text)),
        recursive_cte=bool(_RECURSIVE_CTE_RE.search(text)),
        aggregate_function_call=bool(_AGGREGATE_FUNCTION_CALL_RE.search(text)),
        sql_calc_found_rows=bool(_SQL_CALC_FOUND_ROWS_RE.search(text)),
        window_function=bool(_WINDOW_FUNCTION_RE.search(text)),
        group_by=group_by,
        dedup_only_group_by=group_by and is_dedup_only_group_by(text),
        computed_join_key=_has_computed_join_key(text),
        write_with_subquery=bool(_WRITE_WITH_SUBQUERY_RE.search(text)),
        join_keyword_count=regex_join_count(text),
        aggregation_keyword=_contains_any(lower, AGGREGATION_KEYWORDS),
        exists_or_in_subquery=bool(_EXISTS_OR_IN_SUBQUERY_RE.search(text)),
        pagination_keyword=_contains_any(lower, PAGINATION_KEYWORDS),
        in_list=_contains_any(lower, IN_LIST_KEYWORDS),
        like_percent_literal="like '%" in lower,
        text_search_keyword=_contains_any(lower, TEXT_SEARCH_KEYWORDS),
        json_function_keyword=_contains_any(lower, JSON_FUNCTION_KEYWORDS),
        match_against_keyword=_contains_any(lower, MATCH_AGAINST_KEYWORDS),
        duplicate_key_upsert=_contains_any(lower, DUPLICATE_KEY_KEYWORDS),
        group_concat_call=_contains_any(lower, GROUP_CONCAT_KEYWORDS),
        stored_routine_keyword=_contains_any(lower, STORED_ROUTINE_KEYWORDS),
        window_function_keyword=bool(_WINDOW_FUNCTION_KEYWORD_RE.search(text)),
        jsonb_operation=bool(_JSONB_OPERATION_RE.search(text)),
        array_operation=bool(_ARRAY_OPERATION_RE.search(text)),
        tsvector_search=bool(_TSVECTOR_SEARCH_RE.search(text)),
        lateral_join=bool(_LATERAL_JOIN_RE.search(text)),
        on_conflict_or_returning=bool(_ON_CONFLICT_OR_RETURNING_RE.search(text)),
    )
