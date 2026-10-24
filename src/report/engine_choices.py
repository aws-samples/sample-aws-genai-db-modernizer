"""Which queries tie between engines, and which moved after the customer's approval."""

from __future__ import annotations

import re
from typing import Any

from src.shared.engine_names import display_engine
from src.shared.flag_reasons import flag_reason_text
from src.shared.report_facts import (
    CONSOLIDATED_FROM_TO_RE,
    ENGINE_BY_NAME,
    FULLY_ELIMINATED_RE,
    engine_id_from_name,
)

from . import escaping
from .facts import report_facts
from .formatting import plural_noun

# The engine-name maps and the reality-check line patterns live in the facts
# layer (#615), which reads the reality check's text once; re-exported here.
_ENGINE_BY_NAME = ENGINE_BY_NAME
_ENGINE_NAME_ALT = "|".join(re.escape(n) for n in sorted(ENGINE_BY_NAME, key=len, reverse=True))
_FULLY_ELIMINATED_RE = FULLY_ELIMINATED_RE
_CONSOLIDATED_FROM_TO_RE = CONSOLIDATED_FROM_TO_RE
_engine_from_reason = engine_id_from_name


# The pending engine choices' lead paragraph (#1003), in the engineering report and
# the executive report's "Decisions needed" card.
PENDING_CHOICES_NOTE = (
    "These queries tie between engines at the whole-point score routing compares, so no "
    "routing rule picks one. Each table is one choice: a pick applies to every query listed "
    "under it, and until you choose, they sit on a deterministic placeholder. Choose in the "
    "assignment review; a run with --auto decides for you, by the score before rounding, then "
    "by the engine the plan already uses more."
)
# The auto-decided choices' lead paragraph (#1003).
AUTO_DECIDED_NOTE = (
    "These engine choices were ties the decide-for-me rule settled for you: decided "
    "automatically, review if needed. Change any of them in the assignment review; that "
    "writes a new assignment version and redesigns only the engines it touches."
)


def _score_pair(engine: dict[str, Any]) -> str:
    whole = engine.get("score")
    exact = engine.get("unrounded_score")
    if not isinstance(whole, (int, float)) or not isinstance(exact, (int, float)):
        return f"{display_engine(str(engine.get('engine', '')))} (n/a)"
    return f"{display_engine(str(engine.get('engine', '')))} ({whole:g}; {exact:.2f})"


def _bare_table(table: Any) -> str:
    return str(table or "?").rsplit(".", 1)[-1]


def pending_choices_md(report: dict[str, Any]) -> list[str]:
    """Engineering Report section for the pending engine choices, one per table
    (#1003); empty without any."""
    choices = [c for c in report.get("pending_engine_choices") or [] if isinstance(c, dict)]
    if not choices:
        return []
    facts = report_facts(report)
    n = int(facts.get("pending_choice_count") or 0)
    q = int(facts.get("pending_choice_query_count") or 0)
    out = [
        f"## Pending engine choices ({n} {plural_noun(n, 'table')}, "
        f"{q} {plural_noun(q, 'query', 'queries')})",
        "",
        PENDING_CHOICES_NOTE,
        "",
        "| Table | Queries | Tied engines (score; before rounding) | Listed under "
        "| Decide for me picks |",
        "|---|---|---|---|---|",
    ]
    for c in choices:
        engines = ", ".join(_score_pair(e) for e in c.get("options") or [] if isinstance(e, dict))
        listed = c.get("current_engines") or [c.get("current_engine", "")]
        out.append(
            f"| `{escaping.md_code(_bare_table(c.get('table')))}` "
            f"| {len(c.get('query_ids') or [])} "
            f"| {escaping.md_cell(engines)} "
            f"| {escaping.md_cell(', '.join(display_engine(str(e)) for e in listed))} "
            f"| {escaping.md_cell(display_engine(str(c.get('decide_for_me_engine', ''))))} |"
        )
    out.append("")
    # #1410: a table whose tie and options read word for word as an earlier table's
    # refers to it instead of printing them again.
    stated: dict[tuple[str, ...], str] = {}
    option_stated: dict[str, str] = {}
    why_stated: dict[str, str] = {}
    for c in choices:
        ids = c.get("query_ids") or []
        table = f"`{escaping.md_code(_bare_table(c.get('table')))}`"
        why = escaping.md_text(c.get("why_tied") or "")
        options = [
            (
                escaping.md_text(display_engine(str(e.get("engine", "")))),
                escaping.md_text(e.get("trade_off") or ""),
            )
            for e in c.get("options") or []
            if isinstance(e, dict)
        ]
        decide = f"- Decide for me: {escaping.md_text(c.get('decide_for_me_reason') or '')}"
        out += [f"### {table} ({len(ids)} {plural_noun(len(ids), 'query', 'queries')})", ""]
        key = (why, *(f"{name}: {text}" for name, text in options), decide)
        if key in stated:
            out += [f"The same tie and options as {stated[key]}.", ""]
        else:
            stated[key] = table
            # #1454: tables tied at the same score and engines share the tie sentence;
            # a later one refers to the first and keeps its own figures.
            lead, _, rest = why.partition(". ")
            if rest and lead in why_stated:
                why = f"The tie is the same as for {why_stated[lead]}. {rest}"
            elif rest:
                why_stated[lead] = table
            out += [why, ""]
            for name, text in options:
                line = f"{name}: {text}"
                # #1454: an option whose first sentence reads as an earlier table's
                # refers to it and keeps only what is its own (the score delta).
                lead, _, rest = text.partition(". ")
                head = f"{name}: {lead}"
                if text and line in option_stated:
                    out.append(f"- {name}: as for {option_stated[line]}.")
                elif rest and head in option_stated:
                    out.append(f"- {name}: as for {option_stated[head]}. {rest}")
                else:
                    option_stated.setdefault(line, table)
                    if rest:
                        option_stated.setdefault(head, table)
                    out.append(f"- {line}")
            out.append(decide)
        out.append("- Queries: " + ", ".join(f"`{escaping.md_code(str(qid))}`" for qid in ids))
        out.append("")
    return out


def auto_decided_md(report: dict[str, Any]) -> list[str]:
    """Engineering Report section for the choices ``--auto`` decided (#1003)."""
    decided = [d for d in report.get("auto_decided_choices") or [] if isinstance(d, dict)]
    if not decided:
        return []
    facts = report_facts(report)
    n = int(facts.get("auto_decided_choice_count") or 0)
    q = int(facts.get("auto_decided_query_count") or 0)
    out = [
        f"## Decided automatically, review if needed ({n} {plural_noun(n, 'table')}, "
        f"{q} {plural_noun(q, 'query', 'queries')})",
        "",
        AUTO_DECIDED_NOTE,
        "",
        "| Table | Queries | Placed on | Alternative | Why |",
        "|---|---|---|---|---|",
    ]
    for d in decided:
        alternatives = ", ".join(display_engine(e) for e in d.get("alternatives") or [])
        out.append(
            f"| `{escaping.md_code(_bare_table(d.get('table')))}` "
            f"| {len(d.get('query_ids') or [])} "
            f"| {escaping.md_cell(display_engine(str(d.get('engine', ''))))} "
            f"| {escaping.md_cell(alternatives)} "
            f"| {escaping.md_cell(d.get('reason') or '')} |"
        )
    out.append("")
    return out


def _tied_queries_md(report: dict[str, Any]) -> list[str]:
    """Engineering Report section for unresolved query ties (#503); empty
    without any.

    A report synthesized since #1003 carries ``pending_engine_choices``: the
    same ties grouped per table, rendered by :func:`pending_choices_md`. Only an
    older report (no such field) falls back to the per-query table below.

    ``report["tied_queries"]`` is the structured list (``TiedQuery``):
    every query an exact score tie left unresolved, each with the engines
    it ties between and the deterministic placeholder it currently sits
    under -- the same per-query fact the approval gate shows from
    ``QueryAssignment.tied_engines`` directly, so the two never disagree.
    """
    if report.get("pending_engine_choices") is not None:
        return pending_choices_md(report)
    tied = [t for t in (report.get("tied_queries") or []) if isinstance(t, dict)]
    if not tied:
        return []
    n = int(report_facts(report).get("tied_query_count") or 0)
    out = [
        f"## Unresolved engine ties ({n} {plural_noun(n, 'query', 'queries')})",
        "",
        "No routing rule decides between the engines listed for each of these "
        "queries; each is listed under a deterministic placeholder pending the "
        "customer's own choice.",
        "",
        "| Query | Tied engines | Currently listed under |",
        "|---|---|---|",
    ]
    for t in sorted(tied, key=lambda t: str(t.get("query_id", ""))):
        tied_names = ", ".join(display_engine(e) for e in sorted(t.get("tied_engines") or []))
        out.append(
            f"| `{escaping.md_code(str(t.get('query_id', '?')))}` "
            f"| {escaping.md_cell(tied_names)} "
            f"| {escaping.md_cell(display_engine(t.get('assigned_engine', '')))} |"
        )
    out.append("")
    return out


# How a customer keeps a rerouted query where they approved it (#629). One
# sentence, in the engineering report's section.
REROUTE_OVERRIDE_TEXT = (
    "To keep any of these on the engine you approved, change its engine in the "
    "assignment review; that writes a new assignment version and redesigns only "
    "the engines it touches."
)


def _reroutes(report: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [r for r in (report.get("post_schema_reroutes") or []) if isinstance(r, dict)]
    return sorted(rows, key=lambda r: str(r.get("query_id", "")))


def _fmt_score(value: Any) -> str:
    return "n/a" if not isinstance(value, (int, float)) else f"{value:g}"


def _n_reroutes(report: dict[str, Any]) -> int:
    """How many queries moved after approval: the facts layer's count (#615)."""
    return int(report_facts(report).get("post_schema_reroute_count") or 0)


def reroute_summary(report: dict[str, Any]) -> str | None:
    """One sentence stating how many queries moved after approval, or None."""
    rows = _reroutes(report)
    if not rows:
        return None
    n = _n_reroutes(report)
    approved = rows[0].get("approved_version")
    version = f" (version {approved})" if approved else ""
    facts = report_facts(report)
    sot = int(facts.get("source_of_truth_reroute_count") or 0)
    design = int(facts.get("design_review_reroute_count") or 0) if sot else n
    parts: list[str] = []
    if design:
        parts.append(
            f"schema design review found that {design} "
            f"{plural_noun(design, 'query', 'queries')} cannot run on the approved engine, "
            "so each moved to the best-scoring engine you kept"
        )
    if sot:
        # #992: a saved routing that split a table's writes, re-routed on resume.
        parts.append(
            f"{sot} {plural_noun(sot, 'write')} moved so that each table's writes land "
            "on one engine, its source of truth"
        )
    return f"After you approved the assignment{version}, " + "; and ".join(parts) + "."


def _reroutes_md(report: dict[str, Any]) -> list[str]:
    """Engineering Report section for queries moved after approval (#629)."""
    rows = _reroutes(report)
    if not rows:
        return []
    n = _n_reroutes(report)
    out = [
        f"## Moved after approval ({n} {plural_noun(n, 'query', 'queries')})",
        "",
        reroute_summary(report) or "",
        "",
        "| Query | Approved on | Moved to | Why | Score after / before |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        out.append(
            f"| `{escaping.md_code(str(r.get('query_id', '?')))}` "
            f"| {escaping.md_cell(display_engine(str(r.get('from_engine', ''))))} "
            f"| {escaping.md_cell(display_engine(str(r.get('to_engine', ''))))} "
            f"| {escaping.md_cell(flag_reason_text(r.get('reason_category')))} "
            f"| {_fmt_score(r.get('score_after'))} / {_fmt_score(r.get('score_before'))} |"
        )
    out += ["", REROUTE_OVERRIDE_TEXT, ""]
    return out
