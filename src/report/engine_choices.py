"""Why engines were dropped, and which queries tie between engines."""

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


def _tied_queries_html(report: dict[str, Any]) -> list[str]:
    """Decision Report "Pending engine choices" callout (#503): the same
    ``tied_queries`` the engineering report lists and the approval gate
    already shows per query, so a reader of either report sees the same
    unresolved ties, not just a bare count.
    """
    esc = escaping.html_text
    tied = [t for t in (report.get("tied_queries") or []) if isinstance(t, dict)]
    if not tied:
        return []
    n = int(report_facts(report).get("tied_query_count") or 0)
    items = []
    for t in sorted(tied, key=lambda t: str(t.get("query_id", ""))):
        tied_names = ", ".join(esc(display_engine(e)) for e in sorted(t.get("tied_engines") or []))
        under = esc(display_engine(t.get("assigned_engine", "")))
        items.append(
            f"<li><code>{esc(str(t.get('query_id', '?')))}</code>: scores tie between "
            f"{tied_names}; no routing rule decides between them; listed under "
            f"<b>{under}</b> pending your choice.</li>"
        )
    return [
        "<section data-figure=tied-queries>",
        f"<h2 class=section-title>Pending engine choices "
        f"({n} {esc(plural_noun(n, 'query', 'queries'))})</h2>",
        "<div class=card><div class=card-b><ul>",
        *items,
        "</ul></div></div>",
        "</section>",
    ]


def _dropped_engines(report: dict[str, Any]) -> list[str]:
    """Engines the reality check removed from the architecture (#547), from the
    facts layer: never one a later assignment edit moved queries back onto (#546)."""
    return [e["engine"] for e in report_facts(report).get("removed_engines") or []]


def _why_engines_were_dropped_html(report: dict[str, Any]) -> list[str]:
    """Decision Report "Engines not used" note (#326, #167): state why.

    A purpose-built engine must justify its place with traffic, data,
    capability and cost (#340); when it does not, the deliverables must say
    why it was dropped, not just show the final architecture without it. The
    facts layer lists every removed engine once, with every reality-check
    line that moved its queries and where they went (#547); this formats it.
    """
    esc = escaping.html_text
    items: list[str] = []
    for removed in report_facts(report).get("removed_engines") or []:
        engine = removed["engine"]
        reason = " ".join(removed.get("reasons") or []) or (
            "The reality check removed it from the architecture."
        )
        targets = removed.get("moved_to") or []
        arrow = " → " + ", ".join(esc(display_engine(x)) for x in targets) if targets else ""
        items.append(
            f'<li data-engine="{escaping.html_attr(engine)}"><b>{esc(display_engine(engine))}</b>'
            f"{arrow}: {esc(reason)}</li>"
        )
    if not items:
        return []
    return [
        "<h2 class=section-title>Engines not used</h2>",
        "<div class=card data-figure=engines-not-used><div class=card-b><ul>",
        *items,
        "</ul></div></div>",
    ]


def _tied_queries_md(report: dict[str, Any]) -> list[str]:
    """Engineering Report section for unresolved query ties (#503); empty
    without any.

    ``report["tied_queries"]`` is the structured list (``TiedQuery``):
    every query an exact score tie left unresolved, each with the engines
    it ties between and the deterministic placeholder it currently sits
    under -- the same per-query fact the approval gate shows from
    ``QueryAssignment.tied_engines`` directly, so the two never disagree.
    """
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
# sentence, shared by the decision report, the engineering report and the deck.
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
    return (
        f"After you approved the assignment{version}, schema design review found that "
        f"{n} {plural_noun(n, 'query', 'queries')} cannot run on the "
        f"approved engine, so each moved to the best-scoring engine you kept."
    )


def _reroutes_html(report: dict[str, Any]) -> list[str]:
    """Decision Report "Moved after your approval" section (#629)."""
    esc = escaping.html_text
    rows = _reroutes(report)
    if not rows:
        return []
    n = _n_reroutes(report)
    items = [
        f"<li><code>{esc(str(r.get('query_id', '?')))}</code>: "
        f"{esc(display_engine(str(r.get('from_engine', ''))))} to "
        f"<b>{esc(display_engine(str(r.get('to_engine', ''))))}</b> "
        f"({esc(flag_reason_text(r.get('reason_category')))}; score "
        f"{esc(_fmt_score(r.get('score_after')))} against "
        f"{esc(_fmt_score(r.get('score_before')))})</li>"
        for r in rows
    ]
    return [
        "<section data-figure=post-schema-reroutes>",
        f"<h2 class=section-title>Moved after your approval "
        f"({n} {esc(plural_noun(n, 'query', 'queries'))})</h2>",
        "<div class=card><div class=card-b>",
        f"<p>{esc(reroute_summary(report) or '')}</p>",
        "<ul>",
        *items,
        "</ul>",
        f"<p>{esc(REROUTE_OVERRIDE_TEXT)}</p>",
        "</div></div>",
        "</section>",
    ]


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
