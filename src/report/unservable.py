"""Queries that cannot be served as designed: the ones on a fallback engine
(#729), where each landed, why, and how to redesign and re-run it, and the
flagged queries no kept engine can take (#888): a review flagged them and they
stay where they are, and the flagged queries a customer pin holds on their
engine (#1033).

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
    "These queries cannot be served as designed on the engine they are on. The assessment "
    "does not bring back an engine it removed: keep each query where it is, or redesign it, "
    "then re-run."
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
# The held kind (#1033): flagged like the above, but another engine was never tried
# because a pin the customer set keeps the table's writes on this one.
UNSERVABLE_HELD_ALL = (
    "These were scored and designed on their engine, then a design review found that engine "
    "cannot serve them. A write on each query's table is pinned to that engine in the "
    "assignment review, and a table has one writer, so they stay there until the pin changes."
)
UNSERVABLE_HELD_SOME = UNSERVABLE_HELD_ALL.replace("These", "Some", 1)
# How to move a held query: the pin, not the engine pick (the row's lead sentence).
UNSERVABLE_HELD_LEAD = "To move it, change the pin in the assignment review, or redesign it."
# The no-engine-in-scope kind (#1329): no engine in the customer's engine scope can
# serve it, so it has no engine at all. A section that lists only this kind opens with
# its own intro and closes with its own way to serve them (an assessment's scope is
# fixed, so a new assessment widens it), never the assignment review, which refuses to
# edit these queries. The same sentences as the UI callout.
UNSERVABLE_SCOPE_INTRO = (
    "These queries have no engine in the customer's engine scope: no engine the scope lists "
    "can serve them, so they are in no migration wave and no schema design, and none is "
    "forced onto an engine outside the scope."
)
UNSERVABLE_SCOPE_SOME = (
    "Some have no engine in the customer's engine scope: no engine the scope lists can serve "
    "them, so they are in no migration wave and no schema design, and none is forced onto an "
    "engine outside the scope."
)
# ``{engines}`` are the engines they would fall back to, which the scope leaves out.
UNSERVABLE_SCOPE_RERUN = (
    "To serve them, start a new assessment whose scope adds {engines}, or keep them on the "
    "source database."
)
UNSERVABLE_SCOPE_RERUN_WIDER = (
    "To serve them, start a new assessment with a wider scope, or keep them on the source "
    "database."
)
# The row's lead sentence; ``{engine}`` is the engine it would fall back to.
UNSERVABLE_SCOPE_LEAD = (
    "Start a new assessment whose scope adds {engine}, or keep it on the source database."
)
UNSERVABLE_SCOPE_LEAD_NO_ENGINE = (
    "Start a new assessment with a wider scope, or keep it on the source database."
)


def _scope_only(facts: dict[str, Any]) -> bool:
    """Whether every query the section lists has no engine in the scope (#1329)."""
    return bool(facts.get("no_engine_in_scope_query_count")) and not any(
        facts.get(k) for k in ("no_fit_query_count", "flagged_query_count", "held_query_count")
    )


def unservable_intro(facts: dict[str, Any]) -> str:
    """The section's opening sentence: the no-engine-in-scope one when that is the only
    kind listed (#1329), else the one for queries that sit on an engine."""
    return UNSERVABLE_SCOPE_INTRO if _scope_only(facts) else UNSERVABLE_INTRO


def unservable_kind_text(facts: dict[str, Any]) -> list[str]:
    """The sentence for each kind the section lists (``facts.unservable``'s counts); none
    when the only kind is no-engine-in-scope, which the intro already states."""
    if _scope_only(facts):
        return []
    fallback = facts.get("no_fit_query_count") or 0
    flagged = facts.get("flagged_query_count") or 0
    held = facts.get("held_query_count") or 0
    scoped = facts.get("no_engine_in_scope_query_count") or 0
    several = bool(
        (fallback and (flagged or held or scoped))
        or (flagged and (held or scoped))
        or (held and scoped)
    )
    out = []
    if fallback:
        out.append(UNSERVABLE_FALLBACK_SOME if several else UNSERVABLE_FALLBACK_ALL)
    if flagged:
        out.append(UNSERVABLE_FLAGGED_SOME if several else UNSERVABLE_FLAGGED_ALL)
    if held:
        out.append(UNSERVABLE_HELD_SOME if several else UNSERVABLE_HELD_ALL)
    if scoped:
        out.append(UNSERVABLE_SCOPE_SOME)
    return out


def _engine_list(engines: list[str]) -> str:
    names = [display_engine(str(e)) for e in engines]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + f" and {names[-1]}"


def unservable_rerun_lines(facts: dict[str, Any]) -> list[str]:
    """How to act on the listed queries: the assignment review for the queries that sit
    on an engine, and a new assessment with a wider scope for the ones with no engine in
    the scope (#1329), which the assignment review refuses to edit."""
    out = [] if _scope_only(facts) else [UNSERVABLE_RERUN_TEXT]
    if facts.get("no_engine_in_scope_query_count"):
        engines = [str(e) for e in facts.get("no_engine_in_scope_closest_engines") or [] if e]
        out.append(
            UNSERVABLE_SCOPE_RERUN.format(engines=_engine_list(engines))
            if engines
            else UNSERVABLE_SCOPE_RERUN_WIDER
        )
    return out


def unservable_engine_text(q: dict[str, Any]) -> str:
    """The Engine cell: the engine a query is on, or, for a query with no engine in the
    scope (#1329), "No engine (Aurora MySQL is outside the scope)"."""
    if q.get("kind") == "no_engine_in_scope":
        closest = str(q.get("closest_engine") or "")
        if closest:
            return f"No engine ({display_engine(closest)} is outside the scope)"
        return "No engine in the scope"
    return display_engine(str(q.get("engine", "")))


def unservable_lists_text(facts: dict[str, Any]) -> str:
    """Where the Assessment Data (JSON) lists every one: the list of each kind present."""
    lists = []
    if facts.get("no_fit_query_count"):
        lists.append("`query_traces.unservable`")
    if facts.get("flagged_query_count") or facts.get("held_query_count"):
        lists.append("`flagged_unservable`")
    if facts.get("no_engine_in_scope_query_count"):
        lists.append("`scope_unservable`")
    where = (
        ", ".join(lists[:-1]) + f" and {lists[-1]}"
        if len(lists) > 1
        else (lists or ["`query_traces.unservable`"])[0]
    )
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
    """``Keep it on Aurora MySQL, or redesign it. For DynamoDB: ...``; a held query
    names the pin as the way to move it (#1033)."""
    hints = " ".join(q.get("redesign_hints") or [])
    if q.get("kind") == "held":
        return f"{UNSERVABLE_HELD_LEAD} {hints}"
    if q.get("kind") == "no_engine_in_scope":
        closest = str(q.get("closest_engine") or "")
        lead = (
            UNSERVABLE_SCOPE_LEAD.format(engine=display_engine(closest))
            if closest
            else UNSERVABLE_SCOPE_LEAD_NO_ENGINE
        )
        return f"{lead} {hints}".strip()
    engine = display_engine(str(q.get("engine", "")))
    return f"Keep it on {engine}, or redesign it. {hints}"


def unservable_md(report: dict[str, Any]) -> list[str]:
    """Engineering Report "Queries that cannot be served as designed" section."""
    facts = _facts(report)
    if facts is None:
        return []
    out = [
        f"## {unservable_title(facts.get('unservable_query_count', 0))}",
        "",
        unservable_intro(facts),
        "",
        *([" ".join(unservable_kind_text(facts)), ""] if unservable_kind_text(facts) else []),
        "| Query | Engine | Why | Engines ruled out | Needs | Keep or redesign |",
        "|---|---|---|---|---|---|",
    ]
    for q in facts.get("unservable_queries") or []:
        out.append(
            f"| `{escaping.md_cell(escaping.md_code(q.get('query_id', '?')))}` "
            f"| {escaping.md_cell(unservable_engine_text(q))} "
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
    out += [unservable_lists_text(facts), ""]
    for line in unservable_rerun_lines(facts):
        out += [line, ""]
    return out


# ---------------------------------------------------------------------------
# The headline totals with the queries that have no engine in the scope (#1329)
# ---------------------------------------------------------------------------


def _no_engine(facts: dict[str, Any]) -> int:
    return int(facts.get("no_engine_in_scope_query_count") or 0)


def no_engine_clause(facts: dict[str, Any]) -> str:
    """``42 have no engine in the customer's engine scope``, or "" when none has."""
    n = _no_engine(facts)
    if not n:
        return ""
    return f"{n} {plural_verb(n, 'has', 'have')} no engine in the customer's engine scope"


def headline_query_total(facts: dict[str, Any]) -> int:
    """The headline's "of N queries": every collected query when some have no engine
    in the scope (``query_total_count``), else the in-scope queries, as before."""
    total = facts.get("query_total_count")
    if _no_engine(facts) and isinstance(total, int):
        return total
    return int(facts.get("in_scope_query_count") or 0)


def placed_share_note(facts: dict[str, Any]) -> str:
    """What the engine shares are of, when some queries have no engine (#1329)."""
    if not _no_engine(facts):
        return ""
    placed = int(facts.get("in_scope_query_count") or 0)
    return (
        f"Workload shares are of the {placed} {plural_noun(placed, 'query', 'queries')} "
        f"placed on an engine; {no_engine_clause(facts)}."
    )


def cost_exclusion_text(facts: dict[str, Any]) -> str:
    """The projected cost leaves out the queries with no engine in the scope (#1329)."""
    n = _no_engine(facts)
    if not n:
        return ""
    return (
        f"Excludes the {n} {plural_noun(n, 'query', 'queries')} with no engine in the "
        "customer's engine scope, which keep running on the source database."
    )
