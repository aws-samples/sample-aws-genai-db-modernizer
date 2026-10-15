"""Per-query routing decisions (#339): the engineering report's appendix and
the decision report's one-line pointer to it.

Both format ``report.json``'s ``query_traces`` (the full trace of every query
whose routing has a story, from ``src.shared.query_trace``) and its figures,
``facts.query_trace`` (ADR-032): which queries are listed and why, how many
were moved, tied or overridden, and how many stayed where their first
assignment put them. Nothing here counts or filters a trace; every sentence
is the reason renderer's (``src.shared.move_reasons``), escaped for its
context like any other string.
"""

from __future__ import annotations

from typing import Any

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


def _kinds(row: dict[str, Any]) -> str:
    """Why the query is listed, from the facts layer's row: a tie still open is an
    "unresolved tie" (as in "Unresolved engine ties"), a settled one "was tied"."""
    tie = "unresolved tie" if row.get("open_tie") else "was tied"
    labels = (
        ("moved", "moved"),
        ("tied", tie),
        ("overridden", "overridden"),
        ("pinned", "pinned by a listed rule"),
    )
    return ", ".join(label for key, label in labels if row.get(key))


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


def _step_md(step: dict[str, Any], first: bool, previous: dict[str, Any] | None) -> list[str]:
    """One step: its version, rule and reason, and every engine's score or
    gate when they differ from the step before (the first step always)."""
    what = "first assignment, " if first else ""
    out = [
        f"- Step {escaping.md_text(step.get('step', '?'))}, version "
        f"{escaping.md_text(step.get('version', '?'))} ({what}"
        f"{escaping.md_text(step.get('rule') or '')}): "
        f"{escaping.md_text(step.get('explanation') or '')}"
    ]
    same = previous is not None and previous.get("candidates") == step.get("candidates")
    scores = "" if same else _candidates_md(step)
    if scores:
        out.append(f"  - Scores: {scores}")
    return out


def _trace_md(trace: dict[str, Any], row: dict[str, Any]) -> list[str]:
    first = trace.get("first_assignment") or {}
    start = first.get("to_engine_name")
    end = trace.get("final_engine_name") or "?"
    path = f"{start} to {end}" if start and start != end else f"on {end}"
    tables = [t for t in trace.get("source_tables") or [] if t]
    on = f" on {_tables_md(tables)}" if tables else ""
    out = [
        f"### `{escaping.md_code(trace.get('query_id', '?'))}`{on} "
        f"({escaping.md_text(_kinds(row))}): {escaping.md_text(path)}",
        "",
    ]
    if trace.get("query_excerpt"):
        out += [f"`{escaping.md_code(trace['query_excerpt'])}`", ""]
    previous: dict[str, Any] | None = None
    for step in ([first] if first else []) + list(trace.get("moves") or []):
        out += _step_md(step, first=step is first, previous=previous)
        previous = step
    final_score = trace.get("final_score")
    scored = f" ({_score(final_score)})" if isinstance(final_score, (int, float)) else ""
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
    if isinstance(runner, dict) and runner.get("explanation"):
        # Only a runner-up with a score was compared; a gated one was not.
        basis = (
            " Compared on the co-dependency group's mean scores."
            if runner.get("basis") == "group_mean" and runner.get("score") is not None
            else ""
        )
        out.append(f"- Runner-up: {escaping.md_text(runner['explanation'])}{basis}")
    for better in trace.get("outscored_by") or []:
        if isinstance(better, dict) and better.get("explanation"):
            out.append(f"- Scored higher: {escaping.md_text(better['explanation'])}")
    tied = trace.get("tied_engine_names") or []
    if tied:
        out.append(
            f"- Unresolved tie: {escaping.md_text(', '.join(str(n) for n in tied))}; the query "
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
            "`query_traces.more_listed`, with why each is listed; the UI's query journey "
            "(Why this engine) shows any query's full trace.",
            "",
        ]
    for row in facts.get("listed_queries") or []:
        trace = by_id.get(str(row.get("query_id")))
        if row.get("in_full") and trace is not None:
            out += _trace_md(trace, row)
    return out


def query_traces_pointer_html(report: dict[str, Any]) -> list[str]:
    """Decision Report: one line pointing at the engineering report's appendix (#339)."""
    facts = _trace_facts(report)
    if facts is None or not facts.get("listed_query_count"):
        return []
    listed = int(facts.get("listed_query_count") or 0)
    total = int(facts.get("traced_query_count") or 0)
    return [
        f"<p class=note data-figure=query-traces>{listed} of the {total} "
        f"{escaping.html_text(plural_noun(total, 'query', 'queries'))} changed engine after "
        "their first assignment, were tied at some step, or were placed by an override or a "
        "listed rule; the Engineering Report's appendix gives every step and the reason for "
        "each one.</p>"
    ]
