"""Queries on a fallback engine (#729): where each landed, why, and how to
redesign and re-run it.

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
    "No analyzed engine could take these queries, so a routing rule placed each one on the "
    "engine shown: it runs the query, but no analysis scored it there and no design covers "
    "it. The assessment does not bring back an engine it removed. Keep each query where it "
    "landed, or redesign it for the engines that ruled it out, then re-run."
)

# The existing re-run path: the assignment review (the customer gate). One
# sentence for both reports, the deck and the UI callout
# (src/ui/src/locales/en.json, analysis-results-v2.unservable.rerun); the
# interactive report repeats it (src/ui/src/utils/ExportReport.js).
UNSERVABLE_RERUN_TEXT = (
    "To re-run, open the assignment review: change a query's engine or take it out of scope, "
    "then approve; that writes a new assignment version and redesigns only the engines it "
    "touches. A query rewritten as suggested is picked up when the assessment runs again on "
    "the updated collection."
)

# The deck card's action: the re-run path, short enough for a card.
UNSERVABLE_DECK_ACTION = "Keep them there, or redesign them and re-run from the assignment review."


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


def unservable_title(n: Any) -> str:
    """``Queries on a fallback engine (2 queries)``."""
    return f"Queries on a fallback engine ({n} {plural_noun(n, 'query', 'queries')})"


def _keep_or_redesign(q: dict[str, Any]) -> str:
    """``Keep it on Aurora MySQL, or redesign it. For DynamoDB: ...``."""
    engine = display_engine(str(q.get("engine", "")))
    return f"Keep it on {engine}, or redesign it. {' '.join(q.get('redesign_hints') or [])}"


def unservable_html(report: dict[str, Any]) -> list[str]:
    """Decision Report "Queries on a fallback engine" section."""
    facts = _facts(report)
    if facts is None:
        return []
    esc = escaping.html_text
    items = []
    for q in facts.get("unservable_queries") or []:
        gates = _gates(q)
        ruled_out = f" Ruled out: {esc(gates)}." if gates else ""
        items.append(
            f'<li data-query="{escaping.html_attr(q.get("query_id", ""))}" '
            f'data-kind="{escaping.html_attr(q.get("kind", ""))}">'
            f"<code>{esc(q.get('query_id', '?'))}</code> landed on "
            f"<b>{esc(display_engine(str(q.get('engine', ''))))}</b>: "
            f"{esc(q.get('explanation', ''))}{ruled_out} {esc(_keep_or_redesign(q))}</li>"
        )
    more = facts.get("more_unservable_count") or 0
    if more:
        items.append(
            f"<li>{more} more {esc(plural_noun(more, 'query', 'queries'))}: see the "
            "Engineering Report.</li>"
        )
    return [
        "<section data-figure=unservable-queries>",
        f"<h2 class=section-title>{esc(unservable_title(facts.get('unservable_query_count', 0)))}</h2>",
        "<div class=card><div class=card-b>",
        f"<p>{esc(UNSERVABLE_INTRO)}</p>",
        "<ul>",
        *items,
        "</ul>",
        f"<p>{esc(UNSERVABLE_RERUN_TEXT)}</p>",
        "</div></div>",
        "</section>",
    ]


def unservable_md(report: dict[str, Any]) -> list[str]:
    """Engineering Report "Queries on a fallback engine" section."""
    facts = _facts(report)
    if facts is None:
        return []
    out = [
        f"## {unservable_title(facts.get('unservable_query_count', 0))}",
        "",
        UNSERVABLE_INTRO,
        "",
        "| Query | Landed on | Why | Engines ruled out | Needs | Keep or redesign |",
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
        "The Assessment Data (JSON) lists every one, with its gates, under "
        "`query_traces.unservable`.",
        "",
        UNSERVABLE_RERUN_TEXT,
        "",
    ]
    return out
