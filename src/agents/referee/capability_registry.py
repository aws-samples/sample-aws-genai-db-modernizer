"""Capability Registry — Engine capabilities and serviceability detection.

Maps each engine's fundamental capabilities (what it CAN do at an
architectural level) and provides detection logic for query requirements.

Used by the reality check serviceability gate to prevent query absorption
when the target engine fundamentally cannot serve the access pattern.

This is distinct from the existing ENGINE_CAPABILITIES in reality_check.py
which tracks workload-pattern fit scoring. This registry tracks hard
architectural constraints: things an engine structurally cannot do.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from src.agents.referee.utility_statements import is_utility_statement
from src.shared.unsupported_pattern import is_dedup_only_group_by

__all__ = [
    "ENGINE_CAPABILITIES",
    "CAPABILITY_DETECTORS",
    "LIGHTWEIGHT_ALTERNATIVES",
    "SIGNAL_TO_CAPABILITY",
    "detect_required_capabilities",
    "can_engine_serve_capability",
    "suggest_lightweight_alternative",
    "is_dedup_only_group_by",
    "requires_aggregation_capability",
    "requires_computed_join_capability",
]

# ---------------------------------------------------------------------------
# Hard capability matrix — architectural constraints, not fit preferences.
#
# A capability here means the engine CAN serve this pattern at all,
# even if poorly. Absence means "structurally impossible."
# ---------------------------------------------------------------------------

ENGINE_CAPABILITIES: dict[str, set[str]] = {
    "dynamodb": {"strong_consistency"},
    "documentdb": {"multi_doc_acid", "strong_consistency", "aggregation"},
    "opensearch": {"inverted_index", "scan_engine", "aggregation"},
    "aurora_postgresql": {
        "multi_doc_acid",
        "strong_consistency",
        "scan_engine",
        "inverted_index",
        "aggregation",
        "complex_joins",
        "sql_admin",
        "computed_join",
    },
    # InnoDB FULLTEXT indexes (MATCH ... AGAINST) since MySQL 5.6
    "aurora_mysql": {
        "multi_doc_acid",
        "strong_consistency",
        "scan_engine",
        "inverted_index",
        "aggregation",
        "complex_joins",
        "sql_admin",
        "computed_join",
    },
}

# ---------------------------------------------------------------------------
# Capability detectors — regex patterns that indicate a query requires
# a specific hard capability.
# ---------------------------------------------------------------------------

CAPABILITY_DETECTORS: dict[str, list[str]] = {
    "inverted_index": [
        r"LIKE\s+['\"]%",
        r"MATCH\s*\(.+?\)\s*AGAINST\s*\(",
        r"to_tsvector|to_tsquery|@@",
    ],
    "scan_engine": [
        r"\b(ROW_NUMBER|RANK|DENSE_RANK|NTILE|LAG|LEAD)\s*\(\s*\)\s*OVER\s*\(",
        r"WITH\s+RECURSIVE\b",
    ],
    "multi_doc_acid": [],  # Structural detection, not regex-based
    # "aggregation" is handled by requires_aggregation_capability() below, not
    # a plain regex list: it needs the "GROUP BY but not is_dedup_only_group_by"
    # branch, which a flat OR-of-patterns list cannot express.
}

# Compiled regex cache (built on first use)
_COMPILED_DETECTORS: dict[str, list[re.Pattern]] | None = None


def _get_compiled_detectors() -> dict[str, list[re.Pattern]]:
    global _COMPILED_DETECTORS
    if _COMPILED_DETECTORS is None:
        _COMPILED_DETECTORS = {
            cap: [re.compile(pat, re.IGNORECASE) for pat in patterns]
            for cap, patterns in CAPABILITY_DETECTORS.items()
        }
    return _COMPILED_DETECTORS


# ---------------------------------------------------------------------------
# Aggregation capability (#338, #336)
#
# An aggregate function call needs real aggregation capability on one table
# just as much as a joined one: DynamoDB has no server-side SUM/COUNT/AVG
# regardless of how many tables feed it. A query with neither GROUP BY nor a
# join (a bare SELECT COUNT(*) FROM t) used to get required_caps = [] from
# this registry the same way the join/aggregation gap did (#338) -- an
# aggregate function call alone is enough, so this fires independently of
# GROUP BY or join count. FOUND_ROWS()/SQL_CALC_FOUND_ROWS (MySQL's
# row-count-of-last-query modifier), GROUP_CONCAT/STRING_AGG/ARRAY_AGG (string
# and array aggregation) and any window function (anything immediately
# followed by ``OVER (...)``) all count too: DynamoDB has no equivalent for
# any of them.
#
# GROUP BY alone, with no aggregate function, is usually a deduplication
# technique (#336) rather than an aggregation. Rather than re-deriving that
# distinction here, this calls is_dedup_only_group_by directly (from
# src.shared.unsupported_pattern, the same definition the risk report uses,
# #371) so the shared function is the single thing that decides it: GROUP BY
# requires aggregation capability exactly when is_dedup_only_group_by says it
# is not just a dedup. Any aggregate function #371 learns to reject
# (GROUP_CONCAT, STRING_AGG, window functions, ROLLUP, ...) is therefore
# caught here too, even before this module's own function list is updated to
# match.
# ---------------------------------------------------------------------------

_AGGREGATE_FUNCTION_CALL_RE = re.compile(
    r"`?\b(sum|count|avg|min|max|found_rows|group_concat|string_agg|array_agg)\b`?\s*\(",
    re.IGNORECASE,
)
_SQL_CALC_FOUND_ROWS_RE = re.compile(r"\bsql_calc_found_rows\b", re.IGNORECASE)
# Any function call immediately followed by OVER (...) is a window function
# (ROW_NUMBER() OVER (...), SUM(x) OVER (...), ...) -- an aggregate computed
# per-row over a window, which DynamoDB cannot do server-side either way.
_WINDOW_FUNCTION_RE = re.compile(r"\)\s*OVER\s*\(", re.IGNORECASE)
_GROUP_BY_RE = re.compile(r"\bGROUP\s+BY\b", re.IGNORECASE)


def requires_aggregation_capability(query_text: str | None) -> bool:
    """Whether ``query_text`` needs the "aggregation" hard capability (#338, #336).

    True for any aggregate function call (SUM/COUNT/AVG/MIN/MAX/FOUND_ROWS/
    GROUP_CONCAT/STRING_AGG/ARRAY_AGG), MySQL's SQL_CALC_FOUND_ROWS modifier, or
    a window function, regardless of GROUP BY or join count. For a plain
    GROUP BY with none of those, defers to is_dedup_only_group_by (#336):
    true unless that call says the GROUP BY is only de-duplicating rows.
    """
    text = query_text or ""
    if (
        _AGGREGATE_FUNCTION_CALL_RE.search(text)
        or _SQL_CALC_FOUND_ROWS_RE.search(text)
        or _WINDOW_FUNCTION_RE.search(text)
    ):
        return True
    if _GROUP_BY_RE.search(text):
        return not is_dedup_only_group_by(text)
    return False


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


def requires_computed_join_capability(query_text: str | None) -> bool:
    """Whether ``query_text`` joins on a computed expression (review finding B2).

    True when some ``JOIN ... ON`` clause's condition applies a function call
    to either side of a comparison against a column (``lower(groups.name) =
    users.username_lower``, or the reverse), which a key-value store cannot
    evaluate as part of a key lookup -- a plain ``t1.id = t2.id`` join
    condition does not trigger this, and neither does a function compared
    against a bind parameter (``lower(g.name) = $1``), which is a filter, not
    a computed join key.
    """
    text = query_text or ""
    return any(
        _COMPUTED_EXPR_IN_CONDITION_RE.search(m.group("cond"))
        for m in _JOIN_ON_CLAUSE_RE.finditer(text)
    )


# ---------------------------------------------------------------------------
# Lightweight managed-service alternatives for small orphan sets
# ---------------------------------------------------------------------------

LIGHTWEIGHT_ALTERNATIVES: dict[str, dict[str, str]] = {
    "scan_engine": {
        "service": "Amazon Athena + S3",
        "use_when": "Infrequent analytics (< daily), reporting, ad-hoc exploration",
        "pattern": "DynamoDB export to S3 -> Athena queries on schedule or on-demand",
        "cost_profile": "Pay-per-query ($5/TB scanned), zero idle cost",
        "limitations": "Not real-time, seconds-to-minutes latency, read-only",
    },
    "inverted_index": {
        "service": "OpenSearch Serverless",
        "use_when": "Low-volume full-text search (< 5 queries, low RPS)",
        "pattern": "DynamoDB Streams -> OpenSearch Serverless collection -> search API",
        "cost_profile": "Serverless pricing, scales to zero when idle",
        "limitations": "Higher per-request cost at scale vs provisioned OpenSearch",
    },
    "multi_doc_acid": {
        "service": "Application-layer saga pattern",
        "use_when": "Infrequent cross-entity transactions, eventual consistency acceptable",
        "pattern": "Step Functions orchestrated writes with compensating transactions",
        "cost_profile": "No additional database, Step Functions execution cost only",
        "limitations": "Eventual consistency, requires idempotent operations",
    },
}

# ---------------------------------------------------------------------------
# Triage signal → hard capability mapping
#
# When a triage signal implies a hard architectural requirement, map it here.
# Not all signals imply hard capabilities — most are just workload preferences.
# ---------------------------------------------------------------------------

SIGNAL_TO_CAPABILITY: dict[str, str] = {
    "text_search": "inverted_index",
    # SUM/COUNT/AVG with GROUP BY, or an aggregate combined with a JOIN, is a
    # hard requirement: an engine with no aggregation capability (DynamoDB)
    # cannot run it at all, even on a single query, not just "poorly" (#338).
    "aggregations": "aggregation",
    # 3+ table joins are the same kind of hard requirement -- DynamoDB and
    # OpenSearch have no join capability regardless of how the data is keyed.
    "complex_joins": "complex_joins",
}


def detect_required_capabilities(
    query_text: str, signals: list[str], tables_accessed: Iterable[str] | None = None
) -> list[str]:
    """Detect hard capability requirements from query text and triage signals.

    Returns a list of capability names that the query structurally requires.
    An empty list means any engine can potentially serve this query.
    ``tables_accessed`` is used only to detect a utility/metadata statement
    (#327): those need "sql_admin", which only the Aurora engines have, so
    the reality check's serviceability gate keeps them there instead of
    letting them follow the normal confidence-scoring path onto DynamoDB.
    """
    required: set[str] = set()

    # 1. Signal-based detection (fast path)
    for sig in signals:
        cap = SIGNAL_TO_CAPABILITY.get(sig)
        if cap:
            required.add(cap)

    # 2. Regex-based detection from SQL text
    if query_text:
        detectors = _get_compiled_detectors()
        for cap, patterns in detectors.items():
            for pattern in patterns:
                if pattern.search(query_text):
                    required.add(cap)
                    break
        if requires_aggregation_capability(query_text):
            required.add("aggregation")
        if requires_computed_join_capability(query_text):
            required.add("computed_join")

    # 3. Utility/metadata statements (#327) -- only Aurora understands DDL,
    # session variables and transaction control.
    if is_utility_statement(query_text, tables_accessed):
        required.add("sql_admin")

    return sorted(required)


def can_engine_serve_capability(engine: str, required: list[str]) -> bool:
    """Check if an engine can serve ALL required capabilities.

    Returns True if:
    - The query has no required capabilities (any engine can serve it), OR
    - The engine has ALL required capabilities in its capability set.
    """
    if not required:
        return True

    engine_caps = ENGINE_CAPABILITIES.get(engine, set())
    return all(cap in engine_caps for cap in required)


def suggest_lightweight_alternative(capability: str) -> dict[str, str] | None:
    """Get a lightweight managed-service alternative for a capability.

    Returns None if no alternative exists for the given capability.
    """
    return LIGHTWEIGHT_ALTERNATIVES.get(capability)
