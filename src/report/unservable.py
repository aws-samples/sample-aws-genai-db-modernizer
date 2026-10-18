"""Queries that cannot be served as designed: the ones on a fallback engine
(#729), where each landed, why, and how to redesign and re-run it, and the
flagged queries no kept engine can take (#888): a review flagged them and they
stay where they are.

A query whose engine a fallback rule decided (no analyzed engine could take
it), or whose every candidate engine is gated, lands on the fallback engine:
it runs the query, but no analysis scored it there and no design covers it.
The assessment does not bring back an engine the reality check removed
(maintainer decision, 2026-10-14); these sections say where each query
landed and why, the gates that ruled out each engine, a redesign hint for
those engines and the existing re-run path. Both
format ``facts.unservable`` (``src.shared.report_facts.unservable_query_facts``,
ADR-032); nothing here counts a query.
"""

from __future__ import annotations

from typing import Any

from src.shared.engine_names import display_engine

from . import escaping
from .facts import report_facts
from .formatting import plural_noun, plural_verb

UNSERVABLE_INTRO = (
    "No engine the plan keeps can serve these queries as written. The assessment does not "
    "bring back an engine it removed: keep each query where it is, or redesign it, then re-run."
)

# Why each kind cannot be served, for the kinds the section lists: "These" when
# every row is of that kind, "Some" when both kinds are listed. The same
# sentences as the UI callout (analysis-results.unservable.fallback_all and so on).
UNSERVABLE_FALLBACK_ALL = (
    "These landed on a fallback engine because no analyzed engine could take them: it runs "
    "the query, but no analysis scored it there and no design covers it."
)
UNSERVABLE_FALLBACK_SOME = UNSERVABLE_FALLBACK_ALL.replace("These", "Some", 1)
# The flagged kind (#888): scored and designed on its engine, then a review found
# that engine cannot serve it and no other kept engine can take it.
UNSERVABLE_FLAGGED_ALL = (
    "These were scored and designed on their engine, then a review found that engine cannot "
    "serve them, and no other engine the plan keeps can take them, so they stay there."
)
UNSERVABLE_FLAGGED_SOME = UNSERVABLE_FLAGGED_ALL.replace("These", "Some", 1)


def unservable_kind_text(facts: dict[str, Any]) -> list[str]:
    """The sentence for each kind the section lists (``facts.unservable``'s counts)."""
    fallback = facts.get("no_fit_query_count") or 0
    flagged = facts.get("flagged_query_count") or 0
    both = bool(fallback and flagged)
    out = []
    if fallback:
        out.append(UNSERVABLE_FALLBACK_SOME if both else UNSERVABLE_FALLBACK_ALL)
    if flagged:
        out.append(UNSERVABLE_FLAGGED_SOME if both else UNSERVABLE_FLAGGED_ALL)
    return out


def unservable_lists_text(facts: dict[str, Any]) -> str:
    """Where the Assessment Data (JSON) lists every one: the list of each kind present."""
    lists = []
    if facts.get("no_fit_query_count"):
        lists.append("`query_traces.unservable`")
    if facts.get("flagged_query_count"):
        lists.append("`flagged_unservable`")
    where = " and ".join(lists or ["`query_traces.unservable`"])
    return f"The Assessment Data (JSON) lists every one, with its gates, under {where}."


# The existing re-run path: the assignment review (the customer gate). One
# sentence for both reports, the PDF and the UI callout
# (src/ui/src/locales/en.json, analysis-results.unservable.rerun).
UNSERVABLE_RERUN_TEXT = (
    "To re-run, open the assignment review: change a query's engine or take it out of scope, "
    "then approve; that writes a new assignment version and redesigns only the engines it "
    "touches. A query rewritten as suggested is picked up when the assessment runs again on "
    "the updated collection."
)


def _facts(report: dict[str, Any]) -> dict[str, Any] | None:
    facts = report_facts(report).get("unservable")
    if not isinstance(facts, dict) or not facts.get("unservable_query_count"):
        return None
    return facts


def _gates(q: dict[str, Any]) -> str:
    """``DynamoDB (missing a capability the query needs); OpenSearch (...)``."""
    return "; ".join(
        f"{display_engine(str(g.get('engine', '')))} ({g.get('gate_text') or g.get('gate')})"
        for g in q.get("blocking_gates") or []
    )


UNSERVABLE_TITLE = "Queries that cannot be served as designed"


def unservable_title(n: Any) -> str:
    """``Queries that cannot be served as designed (2 queries)``."""
    return f"{UNSERVABLE_TITLE} ({n} {plural_noun(n, 'query', 'queries')})"


def _keep_or_redesign(q: dict[str, Any]) -> str:
    """``Keep it on Aurora MySQL, or redesign it. For DynamoDB: ...``."""
    engine = display_engine(str(q.get("engine", "")))
    return f"Keep it on {engine}, or redesign it. {' '.join(q.get('redesign_hints') or [])}"


def unservable_md(report: dict[str, Any]) -> list[str]:
    """Engineering Report "Queries that cannot be served as designed" section."""
    facts = _facts(report)
    if facts is None:
        return []
    out = [
        f"## {unservable_title(facts.get('unservable_query_count', 0))}",
        "",
        UNSERVABLE_INTRO,
        "",
        " ".join(unservable_kind_text(facts)),
        "",
        "| Query | Engine | Why | Engines ruled out | Needs | Keep or redesign |",
        "|---|---|---|---|---|---|",
    ]
    for q in facts.get("unservable_queries") or []:
        out.append(
            f"| `{escaping.md_cell(escaping.md_code(q.get('query_id', '?')))}` "
            f"| {escaping.md_cell(display_engine(str(q.get('engine', ''))))} "
            f"| {escaping.md_cell(q.get('explanation', ''))} "
            f"| {escaping.md_cell(_gates(q) or '-')} "
            f"| {escaping.md_cell(', '.join(q.get('capability_labels') or []) or '-')} "
            f"| {escaping.md_cell(_keep_or_redesign(q))} |"
        )
    out.append("")
    more = facts.get("more_unservable_count") or 0
    if more:
        out += [
            f"{more} more {plural_noun(more, 'query', 'queries')} "
            f"{plural_verb(more, 'is', 'are')} not shown here.",
            "",
        ]
    out += [
        unservable_lists_text(facts),
        "",
        UNSERVABLE_RERUN_TEXT,
        "",
    ]
    return out
