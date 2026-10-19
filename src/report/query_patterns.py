"""The queries and the distinct query patterns they make (#1146): one line, from the facts.

Two queries are one query pattern when they read or write the same rows the same way:
the same tables, filters, sort order, row limit and operation (an insert, an update, a
delete and an upsert are four), and for a count or total the same grouping, whatever
columns they return (``src.tools.sql.query_pattern_groups``).
It is not the designers' "access patterns", which count what a schema design serves.
Every deliverable states "N queries in M distinct query patterns" from
``facts.query_pattern_counts`` (ADR-032); nothing here counts. A report written before
#1146 has no grouping, and the line is left out.
"""

from __future__ import annotations

from typing import Any

from .facts import report_facts
from .formatting import plural_noun, plural_verb

# What makes two queries one query pattern, in the customer's words; the engineering
# report states it once, the executive report and the UI print the figure only.
QUERY_PATTERN_DEFINITION = (
    "Queries that read or write the same rows the same way (the same tables, filters, "
    "sort order, row limit and operation, where an insert, an update, a delete and an "
    "upsert are four operations, and for a count or total the same grouping) are one "
    "query pattern, whatever columns they return."
)


def _figures(report: dict[str, Any]) -> dict[str, Any] | None:
    facts = report_facts(report).get("query_pattern_counts")
    return facts if isinstance(facts, dict) else None


def query_pattern_count_text(report: dict[str, Any]) -> str | None:
    """``1,654 queries in 1,045 distinct query patterns``, or None with no grouping."""
    facts = _figures(report)
    if facts is None:
        return None
    n = int(facts.get("query_count") or 0)
    m = int(facts.get("pattern_count") or 0)
    return (
        f"{n:,} {plural_noun(n, 'query', 'queries')} in {m:,} distinct query "
        f"{plural_noun(m, 'pattern')}"
    )


def query_patterns_md(report: dict[str, Any]) -> list[str]:
    """The figures and the definition as their own paragraph (engineering report)."""
    text = query_pattern_count_text(report)
    facts = _figures(report)
    if text is None or facts is None:
        return []
    k = int(facts.get("repeated_pattern_count") or 0)
    q = int(facts.get("repeated_query_count") or 0)
    repeated = (
        f" {k:,} of the patterns {plural_verb(k, 'is', 'are')} made by more than one query "
        f"({q:,} {plural_noun(q, 'query', 'queries')} in all)."
        if k
        else " Every query is a pattern of its own."
    )
    return [f"**Distinct query patterns:** {text}. {QUERY_PATTERN_DEFINITION}{repeated}", ""]
