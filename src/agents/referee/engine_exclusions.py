"""
Engine Exclusions — Hard rules that prevent query→engine assignment.

Unlike anti-pattern penalties (which reduce confidence scores), exclusions are
binary: if a query matches an exclusion rule for an engine, that engine CANNOT
serve it. The first assignment's own scoring pass (``assignment_resolver.
resolve``) zeroes that engine for the query instead of picking it.

This also handles customer override validation: if a customer manually
assigns a query to an engine excluded for it, the override is rejected
(``assignment_overrides.ExcludedEngineOverride``, #548) with the plain,
customer-facing ``customer_message`` below -- never ``description``, which is
the technical, log-only reason -- so the customer can pick a different engine
instead of having their choice silently moved somewhere else.

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
    """A single exclusion rule for an engine.

    ``description`` is the technical reason, for logs and the decision trace
    only. ``customer_message`` is the plain-language fragment a customer-facing
    rejection quotes (#548): what is wrong with the query for this engine, as
    a verb phrase with no leading subject ("uses a leading-wildcard search
    ...", not "it uses ..." or "This query uses ...") -- the caller's own
    sentence supplies the subject ("the query", or a named group-mate), and
    the fragment is appended straight after it. No engine name in it either
    (the caller names the engine with ``display_engine``), and no mention of
    where the query goes instead -- rejecting an override never implies a
    move.
    """

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
#   - customer_message: plain-language verb-phrase fragment for a
#     customer-facing rejection (#548); no leading subject, no engine name,
#     no "auto-reassigned" wording
#   - check: function(query_text) -> bool
# ---------------------------------------------------------------------------

EXCLUSION_RULES: list[ExclusionRule] = [
    # DynamoDB exclusions
    ExclusionRule(
        engine="dynamodb",
        rule_id="ddb-no-leading-wildcard",
        description="Leading wildcard LIKE '%term' requires full scan; search engine territory",
        # round 3 review: "(LIKE '%...')", not a specific literal like
        # "(LIKE '%term%')" -- the fixed example text could otherwise read
        # like a quote of the customer's own SQL even when their actual
        # pattern is something else, e.g. '%a%'.
        customer_message="uses a leading-wildcard search (LIKE '%...')",
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
            "performs a conditional write with a subquery, which needs transactional "
            "guarantees it can't provide"
        ),
        check=_has_write_with_subquery,
    ),
    # ElastiCache exclusions
    ExclusionRule(
        engine="elasticache",
        rule_id="ec-no-complex-join",
        description="ElastiCache cannot perform multi-table JOINs; it's a key-value/data structure store",
        customer_message="joins more than one table, and a key-value store can't serve joins",
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
    engine is excluded for that query. Returns a list of override violations
    with the plain, customer-facing rejection reason (#548: the override is
    rejected, never silently moved to a different engine).

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
