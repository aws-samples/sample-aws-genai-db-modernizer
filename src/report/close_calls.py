"""Close calls (#730): queries an engine won by a small margin over another
engine the plan keeps, and how the customer moves them.

The reality check keeps deciding for the customer (ADR-031 Decision 7); these
section says where that decision was close. It formats ``facts.close_calls``
(``src.shared.report_facts.close_call_facts``, ADR-032) as the engineering
report's table; the executive report leaves close calls out (#1020). Nothing
here counts a query.
"""

from __future__ import annotations

from typing import Any

from src.shared.engine_names import display_engine

from . import escaping
from .facts import report_facts
from .formatting import fmt_num, plural_noun

# How a customer moves a close call: the engineering report. The UI
# callout says the same, with its last sentence worded for the page it is on
# (src/ui/src/locales/en.json, analysis-results.close-calls.override).
CLOSE_CALL_OVERRIDE_TEXT = (
    "To move any of them, change its engine in the assignment review (a query in a "
    "co-dependency group moves with its group); that writes a new assignment version "
    "and redesigns only the engines it touches. The results page's Why this engine "
    "trace shows both scores for each query."
)


def _facts(report: dict[str, Any]) -> dict[str, Any] | None:
    facts = report_facts(report).get("close_calls")
    if not isinstance(facts, dict):
        return None
    if not facts.get("close_call_query_count") and not facts.get("settled_tie_count"):
        return None
    return facts


def close_calls_title(facts: dict[str, Any]) -> str:
    """``Close calls (550 queries), plus 214 exact ties a rule settled``."""
    n = facts.get("close_call_query_count", 0)
    ties = facts.get("settled_tie_count", 0)
    out = f"Close calls ({n} {plural_noun(n, 'query', 'queries')})"
    if ties:
        out += f", plus {ties} exact {plural_noun(ties, 'tie')} a rule settled"
    return out


def _points(value: Any) -> str:
    return f"{fmt_num(value, 1)} {plural_noun(value, 'point')}"


def _intro(margin: Any) -> str:
    return (
        f"These queries went to an engine that scores less than {_points(margin)} above "
        "another engine the plan keeps, which could serve them too. The assessment "
        "decides for you; you can move them yourself."
    )


def close_calls_md(report: dict[str, Any]) -> list[str]:
    """Engineering Report "Close calls" section: one table row per engine pair."""
    facts = _facts(report)
    if facts is None:
        return []
    margin = facts.get("margin_points")
    out = [
        f"## {close_calls_title(facts)}",
        "",
        _intro(margin),
        "",
        "| Went to | Could run on | Close calls | Gap (points) | Settled ties | Group mean | "
        "First queries |",
        "|---|---|---|---|---|---|---|",
    ]
    for pair in facts.get("engine_pairs") or []:
        low, high = pair.get("min_gap"), pair.get("max_gap")
        gap = fmt_num(low, 1) if low == high else f"{fmt_num(low, 1)} to {fmt_num(high, 1)}"
        gap = gap if pair.get("pair_query_count") else "-"
        ids = ", ".join(f"`{escaping.md_code(q)}`" for q in pair.get("query_ids") or [])
        more = pair.get("more_query_count") or 0
        more_txt = f" (+{more} more)" if more else ""
        out.append(
            f"| {escaping.md_cell(display_engine(str(pair.get('engine', ''))))} "
            f"| {escaping.md_cell(display_engine(str(pair.get('alternative', ''))))} "
            f"| {escaping.md_cell(pair.get('pair_query_count', 0))} "
            f"| {escaping.md_cell(gap)} "
            f"| {escaping.md_cell(pair.get('exact_tie_count', 0))} "
            f"| {escaping.md_cell(pair.get('group_mean_count', 0))} "
            f"| {escaping.md_cell(f"{ids}{more_txt}")} |"
        )
    out += [
        "",
        "A query compared on its co-dependency group's mean score moves with its group. The "
        "Assessment Data (JSON) lists every close call, with both scores, under "
        "`query_traces.close_calls`.",
        "",
        CLOSE_CALL_OVERRIDE_TEXT,
        "",
    ]
    return out
