"""
Engine Exclusions — Hard rules that prevent query→engine assignment.

Unlike anti-pattern penalties (which reduce confidence scores), exclusions are
binary: if a query matches an exclusion rule for an engine, that engine CANNOT
serve it. The query goes to the next-best engine instead.

This also handles customer override validation: if a customer manually assigns
a query to an excluded engine, the system overrides it back with a clear note.

Design: registry-based, so adding new engines (Neptune, ElastiCache, etc.)
is just adding entries to EXCLUSION_RULES.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

# Pre-compiled regex patterns for SQL detection
_RE_GROUP_BY = re.compile(r"\bGROUP\s+BY\b", re.IGNORECASE)
_RE_HAVING = re.compile(r"\bHAVING\b", re.IGNORECASE)
_RE_AGG_FUNCS = re.compile(r"\b(SUM|COUNT|AVG|MIN|MAX)\s*\(", re.IGNORECASE)
_RE_LEADING_WILDCARD = re.compile(r"LIKE\s+['\"]%", re.IGNORECASE)
_RE_WRITE_WITH_SUBQUERY = re.compile(
    r"\b(INSERT|UPDATE|DELETE)\b.*\bSELECT\b", re.IGNORECASE | re.DOTALL
)


@dataclass(frozen=True)
class ExclusionRule:
    """A single exclusion rule for an engine."""

    engine: str
    rule_id: str
    description: str
    customer_message: str
    check: Callable[[str], bool]


def _has_aggregation(query_text: str) -> bool:
    """Detect GROUP BY, HAVING, or aggregate functions."""
    return bool(
        _RE_GROUP_BY.search(query_text)
        or _RE_HAVING.search(query_text)
        or _RE_AGG_FUNCS.search(query_text)
    )


def _has_leading_wildcard(query_text: str) -> bool:
    """Detect LIKE '%...' patterns (leading wildcard)."""
    return bool(_RE_LEADING_WILDCARD.search(query_text))


def _has_write_with_subquery(query_text: str) -> bool:
    """Detect INSERT/UPDATE/DELETE containing a subquery."""
    return bool(_RE_WRITE_WITH_SUBQUERY.search(query_text))


# ---------------------------------------------------------------------------
# Exclusion Registry
#
# Add new engines/rules here. Each rule must have:
#   - engine: which engine this excludes
#   - rule_id: stable identifier for tracking
#   - description: technical reason (for logs)
#   - customer_message: human-readable note shown when overriding customer choice
#   - check: function(query_text) -> bool
# ---------------------------------------------------------------------------

EXCLUSION_RULES: list[ExclusionRule] = [
    # DynamoDB exclusions
    ExclusionRule(
        engine="dynamodb",
        rule_id="ddb-no-leading-wildcard",
        description="Leading wildcard LIKE '%term' requires full scan; search engine territory",
        customer_message=(  # nosemgrep: string-concat-in-list
            "This query uses a leading wildcard pattern (LIKE '%...') which requires "
            "a full table scan on DynamoDB. Auto-reassigned to a search engine."
        ),
        check=_has_leading_wildcard,
    ),
    # OpenSearch exclusions
    ExclusionRule(
        engine="opensearch",
        rule_id="os-no-write-subquery",
        description=(  # nosemgrep: string-concat-in-list
            "OpenSearch cannot perform transactional writes with subquery logic. "
            "The application layer must handle conditional insert/update logic before indexing."
        ),
        customer_message=(  # nosemgrep: string-concat-in-list
            "This query performs a conditional write with a subquery which requires "
            "transactional semantics OpenSearch cannot guarantee. Auto-reassigned to "
            "the next best engine. Note: in a future migration phase, the application "
            "layer can handle the conditional logic and index the result in OpenSearch."
        ),
        check=_has_write_with_subquery,
    ),
    # ElastiCache exclusions
    ExclusionRule(
        engine="elasticache",
        rule_id="ec-no-complex-join",
        description="ElastiCache cannot perform multi-table JOINs; it's a key-value/data structure store",
        customer_message="This query uses multi-table JOINs which ElastiCache cannot serve. Auto-reassigned to a relational or document engine.",
        check=lambda q: bool(re.search(r"\bJOIN\b.*\bJOIN\b", q, re.IGNORECASE | re.DOTALL)),
    ),
]

# Build lookup: engine → list of rules
_RULES_BY_ENGINE: dict[str, list[ExclusionRule]] = {}
for _rule in EXCLUSION_RULES:
    _RULES_BY_ENGINE.setdefault(_rule.engine, []).append(_rule)


@dataclass
class ExclusionResult:
    """Result of checking a query against exclusion rules."""

    query_id: str
    excluded_engine: str
    rule_id: str
    description: str
    customer_message: str


def check_exclusions(query_id: str, query_text: str, engine: str) -> ExclusionResult | None:
    """Check if a query is excluded from a specific engine.

    Returns the first matching ExclusionResult, or None if no exclusion applies.
    """
    for rule in _RULES_BY_ENGINE.get(engine, []):
        if rule.check(query_text):
            return ExclusionResult(
                query_id=query_id,
                excluded_engine=engine,
                rule_id=rule.rule_id,
                description=rule.description,
                customer_message=rule.customer_message,
            )
    return None


def check_all_exclusions(query_id: str, query_text: str) -> list[ExclusionResult]:
    """Check a query against ALL engines' exclusion rules.

    Returns a list of all exclusions that apply (can be empty).
    """
    results = []
    for engine in _RULES_BY_ENGINE:
        result = check_exclusions(query_id, query_text, engine)
        if result:
            results.append(result)
    return results


def validate_customer_overrides(
    query_assignments: list[dict],
    queries: list[dict],
) -> list[dict]:
    """Validate customer-assigned queries against exclusion rules.

    For each assignment where customer_override=True, checks if the assigned
    engine is excluded for that query. Returns a list of override notifications
    with the reassignment reason.

    Args:
        query_assignments: list of assignment dicts with query_id, assigned_engine, customer_override
        queries: list of query dicts with query_id and query_text

    Returns:
        List of dicts: {query_id, original_engine, rule_id, customer_message}
        for each violated override.
    """
    query_text_map = {q["query_id"]: q.get("query_text", "") for q in queries}
    violations = []

    for assignment in query_assignments:
        if not assignment.get("customer_override", False):
            continue

        qid = assignment["query_id"]
        engine = assignment["assigned_engine"]
        query_text = query_text_map.get(qid, "")

        if not query_text:
            continue

        exclusion = check_exclusions(qid, query_text, engine)
        if exclusion:
            violations.append(
                {
                    "query_id": qid,
                    "original_engine": engine,
                    "rule_id": exclusion.rule_id,
                    "customer_message": exclusion.customer_message,
                }
            )

    return violations
