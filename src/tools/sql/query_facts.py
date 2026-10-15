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
_RECURSIVE_CTE_RE = re.compile(r"WITH\s+RECURSIVE\b", re.IGNORECASE)

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
    )
