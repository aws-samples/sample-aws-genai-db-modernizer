# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

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
is just adding ``[[exclusions]]`` entries to ``src/catalog/gates.toml`` (#921).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from src.shared import catalog
from src.tools.sql.query_facts import query_facts


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
    check: Callable[[str, str], bool]


# Each check reads one engine-neutral query fact (#491,
# ``src.tools.sql.query_facts``), in the source engine's dialect (#893); the
# detectors live there, once each.


def _fact_check(fact: str, minimum: int | None) -> Callable[[str, str], bool]:
    """The check for a rule on ``fact``: truthy, or at least ``minimum`` for a count."""

    def check(query_text: str, source_engine: str) -> bool:
        value = getattr(query_facts(query_text, source_engine), fact)
        return value >= minimum if minimum is not None else bool(value)

    return check


# ---------------------------------------------------------------------------
# Exclusion Registry: ``src/catalog/gates.toml``'s ``[[exclusions]]`` (ADR-031
# Decision 8, #921), in file order. Each rule names the engine it excludes, a
# stable rule_id, the query fact (and ``min`` for a count), the technical
# description (logs) and the plain customer_message fragment (#548): no
# leading subject, no engine name, no "auto-reassigned" wording.
# ---------------------------------------------------------------------------

EXCLUSION_RULES: list[ExclusionRule] = [
    ExclusionRule(
        engine=rule.engine,
        rule_id=rule.rule_id,
        description=rule.description,
        customer_message=rule.customer_message,
        check=_fact_check(rule.fact, rule.min),
    )
    for rule in catalog.exclusions()
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


def check_exclusions(
    query_id: str, query_text: str, engine: str, source_engine: str | None = None
) -> ExclusionResult | None:
    """Check if a query is excluded from a specific engine.

    ``source_engine`` (the collector's) picks the dialect its query facts are read
    in. Returns the first matching ExclusionResult, or None if no exclusion applies.
    """
    for rule in _RULES_BY_ENGINE.get(engine, []):
        if rule.check(query_text, source_engine or ""):
            return ExclusionResult(
                query_id=query_id,
                excluded_engine=engine,
                rule_id=rule.rule_id,
                description=rule.description,
                customer_message=rule.customer_message,
            )
    return None


def check_all_exclusions(
    query_id: str, query_text: str, source_engine: str | None = None
) -> list[ExclusionResult]:
    """Check a query against ALL engines' exclusion rules.

    Returns a list of all exclusions that apply (can be empty).
    """
    results = []
    for engine in _RULES_BY_ENGINE:
        result = check_exclusions(query_id, query_text, engine, source_engine)
        if result:
            results.append(result)
    return results


def validate_customer_overrides(
    query_assignments: list[dict],
    queries: list[dict],
    source_engine: str | None = None,
) -> list[dict]:
    """Validate customer-assigned queries against exclusion rules.

    For each assignment where customer_override=True, checks if the assigned
    engine is excluded for that query. Returns a list of override violations
    with the plain, customer-facing rejection reason (#548: the override is
    rejected, never silently moved to a different engine).

    Args:
        query_assignments: list of assignment dicts with query_id, assigned_engine, customer_override
        queries: list of query dicts with query_id and query_text
        source_engine: the collector's source engine (the query facts' dialect)

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

        exclusion = check_exclusions(qid, query_text, engine, source_engine)
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
