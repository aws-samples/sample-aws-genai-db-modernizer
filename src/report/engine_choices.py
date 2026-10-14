"""Why engines were dropped, and which queries tie between engines."""

from __future__ import annotations

import re
from typing import Any

from src.shared.engine_names import ENGINE_DISPLAY_NAMES, display_engine
from src.shared.flag_reasons import flag_reason_text

from . import escaping
from .formatting import plural_noun

# A "Consolidated N queries from <engine> -> <absorber>: ... no in-scope
# <engine> query remains ..." trade-off line (built by
# reality_check_patterns._build_recommendations / reality_check_records.
# reconcile_consolidations) is a full elimination -- the "no in-scope ... remains"
# clause is reconcile_consolidations' own marker for it (its "full" action), so it is matched
# directly rather than re-deriving "eliminated" from the assignment here.
# Reason text names engines by display name ("Aurora MySQL") or, in reports
# written before #572, by raw id ("aurora_mysql"); both map back to the id.
_ENGINE_BY_NAME = {
    **{v: k for k, v in ENGINE_DISPLAY_NAMES.items()},
    **{k: k for k in ENGINE_DISPLAY_NAMES},
}
_ENGINE_NAME_ALT = "|".join(re.escape(n) for n in sorted(_ENGINE_BY_NAME, key=len, reverse=True))
_FULLY_ELIMINATED_RE = re.compile(rf"no in-scope ({_ENGINE_NAME_ALT}) query remains")
_CONSOLIDATED_FROM_TO_RE = re.compile(
    rf"^Consolidated \d+ quer(?:y|ies) from ({_ENGINE_NAME_ALT}) → ({_ENGINE_NAME_ALT})"
)


def _engine_from_reason(name: str) -> str:
    return _ENGINE_BY_NAME.get(name, name)


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
        f"({len(tied)} {esc(plural_noun(len(tied), 'query', 'queries'))})</h2>",
        "<div class=card><div class=card-b><ul>",
        *items,
        "</ul></div></div>",
        "</section>",
    ]


def _dropped_engines(report: dict[str, Any]) -> list[str]:
    """Engines the reality check removed from the architecture, in a stable order.

    #547: an engine counts as dropped when it owned queries before the reality
    check (``before_distribution``), a full consolidation moved it out, or the
    TCO lists its cost as already removed (the "Already removed" note's own
    source) -- and it owns nothing afterwards. A trade-off line carrying
    ``reconcile_consolidations``' "no in-scope X query remains" marker also
    counts, for reports written before the distributions were recorded. A
    partial consolidation leaves its engine in the architecture, so it is
    never listed.
    """
    rc = report.get("reality_check") or {}
    after = rc.get("after_distribution") or {}
    cache = (report.get("cache_overlay") or {}).get("engine")
    candidates: list[str] = []
    for t in report.get("trade_offs") or []:
        desc = str((t or {}).get("description") or "")
        elim = _FULLY_ELIMINATED_RE.search(desc)
        m = _CONSOLIDATED_FROM_TO_RE.match(desc)
        candidates += [_engine_from_reason(x.group(1)) for x in (elim, m) if x]
    candidates += [
        str(c.get("from_engine") or "")
        for c in rc.get("consolidations") or []
        if isinstance(c, dict) and c.get("action") == "full"
    ]
    candidates += sorted(e for e, n in (rc.get("before_distribution") or {}).items() if n)
    eliminated = [
        str(row.get("database") or "")
        for row in (report.get("tco_analysis") or {}).get("eliminated_engine_costs") or []
        if isinstance(row, dict)
    ]
    candidates += eliminated
    marked = set(eliminated) | {
        _engine_from_reason(m.group(1))
        for t in report.get("trade_offs") or []
        if (m := _FULLY_ELIMINATED_RE.search(str((t or {}).get("description") or "")))
    }
    out: list[str] = []
    for e in candidates:
        if not e or e in out or e == cache or after.get(e):
            continue
        if not after and e not in marked:
            # No distributions recorded (an older report): only the marker or
            # an eliminated cost says an engine left; a bare line does not.
            continue
        out.append(e)
    return out


def _why_engines_were_dropped_html(report: dict[str, Any]) -> list[str]:
    """Decision Report "Engines not used" note (#326, #167): state why.

    A purpose-built engine must justify its place with traffic, data,
    capability and cost (#340); when it does not, the deliverables must say
    why it was dropped, not just show the final architecture without it. The
    trade-off line for a fully eliminated engine already carries the
    OpenSearch justification floor's reason (#326) or the generalized
    cost-share floor's reason (#167), appended by ``reconcile_consolidations``.

    #547: every dropped engine (``_dropped_engines``) is listed, once, with
    every consolidation line that moved its queries -- not only an engine
    whose line carries the "no in-scope X query remains" marker, so the
    "see Engines not used below" pointer always has this section to point at.
    """
    esc = escaping.html_text
    rc = report.get("reality_check") or {}
    items: list[str] = []
    for engine in _dropped_engines(report):
        lines: list[str] = []
        targets: list[str] = []
        for t in report.get("trade_offs") or []:
            desc = str((t or {}).get("description") or "")
            m = _CONSOLIDATED_FROM_TO_RE.match(desc)
            elim = _FULLY_ELIMINATED_RE.search(desc)
            if (m and _engine_from_reason(m.group(1)) == engine) or (
                elim and _engine_from_reason(elim.group(1)) == engine
            ):
                if desc not in lines:
                    lines.append(desc)
                if m and _engine_from_reason(m.group(2)) not in targets:
                    targets.append(_engine_from_reason(m.group(2)))
        for c in rc.get("consolidations") or []:
            if isinstance(c, dict) and c.get("from_engine") == engine:
                to = str(c.get("to_engine") or "")
                if to and to not in targets:
                    targets.append(to)
                if not lines and c.get("reason"):
                    lines.append(str(c["reason"]))
        reason = " ".join(lines) or "The reality check removed it from the architecture."
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
    out = [
        f"## Unresolved engine ties ({len(tied)} {plural_noun(len(tied), 'query', 'queries')})",
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


def reroute_summary(report: dict[str, Any]) -> str | None:
    """One sentence stating how many queries moved after approval, or None."""
    rows = _reroutes(report)
    if not rows:
        return None
    approved = rows[0].get("approved_version")
    version = f" (version {approved})" if approved else ""
    return (
        f"After you approved the assignment{version}, schema design review found that "
        f"{len(rows)} {plural_noun(len(rows), 'query', 'queries')} cannot run on the "
        f"approved engine, so each moved to the best-scoring engine you kept."
    )


def _reroutes_html(report: dict[str, Any]) -> list[str]:
    """Decision Report "Moved after your approval" section (#629)."""
    esc = escaping.html_text
    rows = _reroutes(report)
    if not rows:
        return []
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
        f"({len(rows)} {esc(plural_noun(len(rows), 'query', 'queries'))})</h2>",
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
    out = [
        f"## Moved after approval ({len(rows)} {plural_noun(len(rows), 'query', 'queries')})",
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
