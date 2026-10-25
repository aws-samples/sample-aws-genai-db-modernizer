"""Per-query routing decisions (#339): the appendix of the engineering report
(the executive report has none, #1020).

It formats ``report.json``'s ``query_traces`` (the full trace of every query
whose routing has a story, from ``src.shared.query_trace``) and its figures,
``facts.query_trace`` (ADR-032): which queries are listed and why, how many
were moved, tied or overridden, and how many stayed where their first
assignment put them. Nothing here counts or filters a trace; every sentence
is the reason renderer's (``src.shared.move_reasons``), escaped for its
context like any other string.
"""

from __future__ import annotations

from collections.abc import Set as AbstractSet
from typing import Any

from src.shared.engine_names import display_engine
from src.shared.move_reasons import consolidation_reason
from src.shared.workload_fit_labels import pinned_label

from . import escaping
from .facts import report_facts
from .formatting import fmt_num, plural_noun, plural_verb


def _trace_facts(report: dict[str, Any]) -> dict[str, Any] | None:
    facts = report_facts(report).get("query_trace")
    if not isinstance(facts, dict) or not facts.get("has_ledger"):
        return None
    return facts


def _score(value: Any) -> str:
    return fmt_num(value, 1) if isinstance(value, (int, float)) else "n/a"


def _kinds(row: dict[str, Any], group: dict[str, Any] | None = None) -> str:
    """Why the query is listed beyond its move, from the facts layer's row: a tie
    still open is an "unresolved tie" (as in "Unresolved engine ties"), a settled
    one a "settled tie". A group's entry (#1108) states its stored open-tie count
    instead ("26 unresolved ties"), so the headings add up to "Unresolved engine
    ties". The move itself is the heading's path (#1170)."""
    tie = "unresolved tie" if row.get("open_tie") else "settled tie"
    if group is not None and row.get("open_tie"):
        open_ties = int(group.get("open_tie_count") or 0)
        tie = f"{open_ties} {plural_noun(open_ties, 'unresolved tie', 'unresolved ties')}"
    labels = (
        ("tied", tie),
        ("overridden", "overridden"),
        ("pinned", "pinned by a listed rule"),
    )
    return ", ".join(label for key, label in labels if row.get(key))


def _heading_tail(path: str, kinds: str) -> str:
    """``moved from DocumentDB to Aurora MySQL`` or ``stays on DynamoDB``, then
    ``; settled tie`` and the other reasons it is listed (#1170: no bare labels)."""
    return f"{path}; {kinds}" if kinds else path


def _tables_md(tables: list[Any]) -> str:
    """The query's tables as code spans: the first three, then how many more."""
    shown = ", ".join(f"`{escaping.md_code(t)}`" for t in tables[:3])
    more = f" (+{len(tables) - 3} more)" if len(tables) > 3 else ""
    return shown + more


def _candidates_md(step: dict[str, Any]) -> str:
    """Every engine's score or gate at the step, in one line."""
    parts = []
    for c in step.get("candidates") or []:
        name = escaping.md_text(c.get("engine_name") or c.get("engine") or "?")
        score = c.get("score")
        gate = c.get("gate_text")
        if gate and score is None:
            parts.append(f"{name}: {escaping.md_text(gate)}")
        elif gate:
            parts.append(f"{name} {_score(score)} ({escaping.md_text(gate)})")
        else:
            parts.append(f"{name} {_score(score)}")
    return "; ".join(parts)


# The reason code of a move the reality check made because the engine it left
# adds too little (``ReasonCode.CONSOLIDATE_NO_UNIQUE_VALUE``).
_CONSOLIDATED = "consolidate.no_unique_value"


def _consolidated_engines(report: dict[str, Any]) -> set[str]:
    """The engines whose consolidation line "Migration trade-offs" still lists
    (``facts.reality_check_decisions``, ADR-032 Decision 7)."""
    return {
        str(d.get("engine"))
        for d in report_facts(report).get("reality_check_decisions") or []
        if isinstance(d, dict) and d.get("kind") == "consolidation" and d.get("engine")
    }


def _step_explanation(step: dict[str, Any], consolidated: AbstractSet[str]) -> str:
    """The step's sentence. A consolidation whose reason "Migration trade-offs" states
    (``consolidated``, the appendix says so once above the entries) keeps its move and
    scores, not that reason again (#1220)."""
    text = str(step.get("explanation") or "")
    engine = str(step.get("from_engine") or "")
    if step.get("reason_code") == _CONSOLIDATED and engine in consolidated:
        text = text.replace(f": {consolidation_reason(display_engine(engine))}", "", 1)
    return text


def _step_md(
    step: dict[str, Any],
    first: bool,
    previous: dict[str, Any] | None,
    consolidated: AbstractSet[str] = frozenset(),
) -> list[str]:
    """One step: its version, rule and reason, and every engine's score or
    gate when they differ from the step before (the first step always)."""
    what = "first assignment, " if first else ""
    out = [
        f"- Step {escaping.md_text(step.get('step', '?'))}, version "
        f"{escaping.md_text(step.get('version', '?'))} ({what}"
        f"{escaping.md_text(step.get('rule') or '')}): "
        f"{escaping.md_text(_step_explanation(step, consolidated))}"
    ]
    same = previous is not None and previous.get("candidates") == step.get("candidates")
    scores = "" if same else _candidates_md(step)
    if scores:
        out.append(f"  - Scores: {scores}")
    return out


def _said_by_a_step(better: dict[str, Any], steps: list[dict[str, Any]]) -> bool:
    """Whether a step shown above already says why ``better`` lost the query: the
    step that took the query off it was a consolidation, and the engine is not in
    the final plan (#1170). Its line would repeat that step."""
    if better.get("in_plan") or not better.get("left_it"):
        return False
    return any(
        s.get("step") == better.get("decided_by_step")
        and s.get("from_engine") == better.get("engine")
        and s.get("reason_code") == _CONSOLIDATED
        for s in steps
    )


def _pending_tables(report: dict[str, Any], query_ids: set[str]) -> list[str]:
    """The tables "Pending engine choices" lists any of ``query_ids`` under, in its order
    (as its headings name them, #1220)."""
    return [
        str(c.get("table") or "?").rsplit(".", 1)[-1]
        for c in report.get("pending_engine_choices") or []
        if isinstance(c, dict) and query_ids & {str(q) for q in c.get("query_ids") or []}
    ]


def _restates_open_tie(trace: dict[str, Any], pending: list[str]) -> bool:
    """Whether the runner-up line would only restate the open tie: the runner-up is a
    tie partner at the final score, and "Pending engine choices" holds the tie (#1220)."""
    runner = trace.get("runner_up")
    return (
        bool(pending)
        and isinstance(runner, dict)
        and runner.get("engine") in (trace.get("tied_engines") or [])
        and runner.get("gap") == 0
    )


def _trace_md(
    trace: dict[str, Any],
    row: dict[str, Any],
    group: dict[str, Any] | None = None,
    report: dict[str, Any] | None = None,
    consolidated: AbstractSet[str] = frozenset(),
) -> list[str]:
    """One entry: a query's trace, or (``group``, #1108) the one trace the queries
    of a facts-layer group share, headed by their count and short ids.

    An open tie that "Pending engine choices" holds names that section, not the
    tie a third time (#1220)."""
    first = trace.get("first_assignment") or {}
    start = first.get("to_engine_name")
    end = trace.get("final_engine_name") or "?"
    # #1170: a tie still open is only listed on its engine, pending the customer.
    stays = "listed on" if row.get("open_tie") else "stays on"
    path = f"moved from {start} to {end}" if start and start != end else f"{stays} {end}"
    tables = [t for t in trace.get("source_tables") or [] if t]
    on = f" on {_tables_md(tables)}" if tables else ""
    if group is not None:
        count = int(group.get("query_count") or 0)
        out = [
            f"### {count} {plural_noun(count, 'query', 'queries')}: "
            f"{escaping.md_text(_heading_tail(path, _kinds(row, group)))}",
            "",
            "Queries: "
            + ", ".join(f"`{escaping.md_code(i)}`" for i in group.get("short_ids") or []),
            "",
            "Each of them has this trace; their full ids, tables and SQL are in the "
            "Assessment Data (JSON) and the UI's query history.",
            "",
        ]
    else:
        out = [
            f"### `{escaping.md_code(trace.get('query_id', '?'))}`{on}: "
            f"{escaping.md_text(_heading_tail(path, _kinds(row)))}",
            "",
        ]
        if trace.get("query_excerpt"):
            out += [f"`{escaping.md_code(trace['query_excerpt'])}`", ""]
    previous: dict[str, Any] | None = None
    steps = ([first] if first else []) + list(trace.get("moves") or [])
    for step in steps:
        out += _step_md(step, first=step is first, previous=previous, consolidated=consolidated)
        previous = step
    ids = {str(q) for q in (group or {}).get("query_ids") or []} or {str(trace.get("query_id"))}
    pending = _pending_tables(report or {}, ids) if row.get("open_tie") else []
    final_score = trace.get("final_score")
    pin = pinned_label(trace.get("pinned"))
    # A pinned query shows its pin, not its score (#1373).
    scored = (
        f", {pin}"
        if pin
        else f" ({_score(final_score)})" if isinstance(final_score, (int, float)) else ""
    )
    group_mean = trace.get("final_group_mean")
    if isinstance(group_mean, (int, float)):
        scored += f"; its co-dependency group's mean score there is {_score(group_mean)}"
    effective = trace.get("effective_version")
    as_of = (
        f", as of version {escaping.md_text(trace.get('final_version'))}"
        if trace.get("final_version") is not None
        else ""
    )
    if effective is not None and effective != trace.get("final_version"):
        as_of += f" (the effective assignment is version {escaping.md_text(effective)})"
    out.append(f"- Final engine: {escaping.md_text(end)}{scored}{as_of}.")
    runner = trace.get("runner_up")
    if (
        isinstance(runner, dict)
        and runner.get("explanation")
        and not _restates_open_tie(trace, pending)
    ):
        # Only a runner-up with a score was compared; a gated one was not.
        basis = (
            " Compared on the co-dependency group's mean scores."
            if runner.get("basis") == "group_mean" and runner.get("score") is not None
            else ""
        )
        out.append(f"- Runner-up: {escaping.md_text(runner['explanation'])}{basis}")
    for better in trace.get("outscored_by") or []:
        if isinstance(better, dict) and _said_by_a_step(better, steps):
            continue
        if isinstance(better, dict) and better.get("explanation"):
            # #951: the explanation starts "<engine> scored higher", so no label
            # repeats it.
            out.append(f"- {escaping.md_text(better['explanation'])}")
    for key in trace.get("key_candidates") or []:
        # #1361: each column the redesign considered as a key, and why it was
        # accepted or refused (the sentence is the redesign model's).
        if isinstance(key, dict) and key.get("column") and key.get("explanation"):
            out.append(
                f"- Redesign key `{escaping.md_code(key['column'])}`: "
                f"{escaping.md_text(key['explanation'])}."
            )
    tied = trace.get("tied_engine_names") or []
    if tied and pending:
        at = f" at {_score(final_score)}" if isinstance(final_score, (int, float)) else ""
        where = ", ".join(f"`{escaping.md_code(t)}`" for t in pending)
        out.append(
            f"- Unresolved tie: {escaping.md_text(', '.join(str(n) for n in tied))}{at}; "
            f"the choice is under Pending engine choices ({where})."
        )
    elif tied:
        who = "each query" if group is not None else "the query"
        out.append(
            f"- Unresolved tie: {escaping.md_text(', '.join(str(n) for n in tied))}; {who} "
            "is listed under the final engine pending the customer's choice."
        )
    out.append("")
    return out


# Why a query is listed, as the appendix and the pointer word it.
_LISTED_WHY = (
    "changed engine after its first assignment, was tied at some step, or was placed by "
    "an override or a listed rule"
)


def query_traces_md(report: dict[str, Any]) -> list[str]:
    """Engineering Report "Appendix: per-query routing decisions" (#339).

    The queries the facts layer lists (moved after their first assignment,
    tied at some step, overridden, pinned by a listed rule) get their whole
    trace, up to the facts layer's cap: the tables and the start of the SQL
    that identify the query, every step with its number, version, rule and
    reason, every engine's score or gate, the final engine, the runner-up
    with its gap or gate, and the engines that scored higher with the step
    that decided against each. The listed queries beyond the cap and the
    queries not listed are summarised, so the appendix stays short on a
    large workload. Empty for a report with no traces or one written before
    the move ledger.
    """
    facts = _trace_facts(report)
    if facts is None:
        return []
    digest = report.get("query_traces") or {}
    by_id = {str(t.get("query_id")): t for t in digest.get("queries") or [] if isinstance(t, dict)}
    listed = int(facts.get("listed_query_count") or 0)
    total = int(facts.get("traced_query_count") or 0)
    rest = int(facts.get("unlisted_query_count") or 0)
    out = [
        f"## Appendix: per-query routing decisions ({listed} "
        f"{plural_noun(listed, 'query', 'queries')})",
        "",
    ]
    if listed:
        tied = int(facts.get("ever_tied_query_count") or 0)
        still_open = int(facts.get("open_tie_query_count") or 0)
        counts = ", ".join(
            text
            for key, text in (
                (
                    "moved_query_count",
                    f"{int(facts.get('moved_query_count') or 0)} changed engine after the "
                    "first assignment",
                ),
                ("ever_tied_query_count", f"{tied} tied at some step ({still_open} still open)"),
                (
                    "overridden_query_count",
                    f"{int(facts.get('overridden_query_count') or 0)} placed by the customer "
                    "or a design review",
                ),
                (
                    "pinned_query_count",
                    f"{int(facts.get('pinned_query_count') or 0)} pinned by a listed rule",
                ),
            )
            if facts.get(key)
        )
        out += [
            f"The routing record traces all {total} {plural_noun(total, 'query', 'queries')} "
            f"from their first assignment to their final engine. Each query below {_LISTED_WHY} "
            f"({counts}).",
            "",
        ]
    else:
        out += [
            f"The routing record traces all {total} {plural_noun(total, 'query', 'queries')}. "
            f"No query {_LISTED_WHY}.",
            "",
        ]
    if rest:
        other = "other " if listed else ""
        out += [
            f"The {other}{rest} {plural_noun(rest, 'query', 'queries')} stayed on the engine "
            "their first assignment chose, placed by:",
            "",
        ]
        out += [
            f"- {escaping.md_text(r.get('label') or r.get('rule') or '?')}: "
            f"{escaping.md_text(r.get('rule_query_count', 0))} "
            f"{plural_noun(r.get('rule_query_count', 0), 'query', 'queries')}"
            for r in facts.get("unlisted_rules") or []
        ]
        out.append("")
    not_in_full = int(facts.get("not_in_full_count") or 0)
    if not_in_full:
        out += [
            f"Only the first {int(facts.get('full_trace_count') or 0)} are shown in full. "
            f"{not_in_full} more listed {plural_noun(not_in_full, 'query', 'queries')} "
            f"{plural_verb(not_in_full, 'is', 'are')} in the Assessment Data (JSON) under "
            "`query_traces.more_listed`, with why each is listed; the UI's query history "
            "(Why this engine) shows any query's full trace.",
            "",
        ]
    entries = int(facts.get("trace_group_count") or 0)
    in_full = int(facts.get("full_trace_count") or 0)
    if entries and entries < in_full:
        out += [
            f"Queries whose trace reads the same (same path, steps, scores and reason) share one "
            f"entry: {entries} {plural_noun(entries, 'entry', 'entries')} below cover "
            f"{in_full} {plural_noun(in_full, 'query', 'queries')}.",
            "",
        ]
    rows = {str(r.get("query_id")): r for r in facts.get("listed_queries") or []}
    consolidated = _consolidated_engines(report)
    said_once = any(
        m.get("reason_code") == _CONSOLIDATED and m.get("from_engine") in consolidated
        for t in by_id.values()
        for m in t.get("moves") or []
        if isinstance(m, dict)
    )
    if said_once:
        # #1170: the consolidation's reason is stated once, under Migration trade-offs.
        out += [
            'A step marked "engine consolidated away" moved its query for the reason given '
            'under "Migration trade-offs" (Reality check).',
            "",
        ]
    # A report written before 1.41 gets its groups from report_facts (#1108).
    for group in facts.get("trace_groups") or []:
        rep = str(group.get("representative_id"))
        trace = by_id.get(rep)
        if trace is None or not rows.get(rep, {}).get("in_full"):
            continue
        out += _trace_md(
            trace,
            rows[rep],
            group if group.get("query_count", 1) > 1 else None,
            report,
            consolidated if said_once else set(),
        )
    return out
