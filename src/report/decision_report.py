"""The Decision Report: one self-contained HTML page for decision makers."""

from __future__ import annotations

from typing import Any

from src.shared.engine_names import display_engine
from src.shared.workload_fit_labels import WORKLOAD_FIT_DEFINITION

from . import escaping
from .architecture import (
    SHARED_TABLES_LABEL,
    SHARED_TABLES_NOTE,
    _architecture_engines,
    architecture_svg,
)
from .cache_layer import _cache_note, cache_layer_text
from .engine_choices import _tied_queries_html, _why_engines_were_dropped_html
from .engine_roles import SEARCH_READ_MODEL
from .formatting import (
    DASH,
    _cost_baseline_text,
    _eliminated_engine_costs_text,
    _fmt_usd,
    fallback_withheld_reason,
    fmt_num,
    label_summary_counts,
    plural_noun,
    plural_verb,
)
from .risks import NO_OPEN_RISKS_CAVEAT, _severity_counts_text, filtered_risks, resolved_risks
from .roadmap import _roadmap_html, resolve_migration_waves

_DECISION_CSS = """
:root { --green:#00d563; --green-d:#00a84f; --navy:#1a1a2e; }
* { box-sizing:border-box; }
body { font-family:'Segoe UI',system-ui,-apple-system,BlinkMacSystemFont,Roboto,Helvetica,Arial,sans-serif;
  margin:0; background:#f8f9fa; color:#1f2937; line-height:1.55; }
.wrap { max-width:960px; margin:0 auto; padding:0 1.25rem; }
.hero { background:linear-gradient(135deg,#1a1a2e 0%,#16213e 50%,#0f3460 100%); color:#fff; padding:2.4rem 0; }
.hero h1 { font-size:1.9rem; font-weight:700; margin:0 0 .35rem; }
.hero .sub { opacity:.85; margin:0; }
.pill { display:inline-block; font-size:.8rem; padding:.35em .9em; border-radius:20px;
  background:rgba(255,255,255,.14); border:1px solid rgba(255,255,255,.25); margin:.6rem .5rem 0 0; }
.pill b { color:#7CFFB0; }
.tiles { display:flex; flex-wrap:wrap; gap:1rem; margin:1.5rem 0; }
.tile { flex:1 1 150px; border-radius:12px; padding:1.2rem; color:#fff; text-align:center; display:flex; flex-direction:column-reverse; justify-content:flex-end; }
.tile .val { font-size:1.7rem; font-weight:700; margin:0 0 .2rem; opacity:1; }
.tile p { margin:0; font-size:.85rem; opacity:.92; }
.tile.green { background:linear-gradient(135deg,#00d563,#00a84f); }
.tile.blue { background:linear-gradient(135deg,#0066cc,#0f3460); }
.tile.slate { background:linear-gradient(135deg,#334155,#1a1a2e); }
.tile.amber { background:linear-gradient(135deg,#f59e0b,#b45309); }
.tile.red { background:linear-gradient(135deg,#ef4444,#991b1b); }
.section-title { font-size:1.35rem; font-weight:700; margin:2.2rem 0 1rem; padding-bottom:.4rem;
  border-bottom:3px solid var(--green); }
.card { background:#fff; border:1px solid #e5e7eb; border-radius:12px; margin:1rem 0; overflow:hidden; }
.card-h { background:#1a1a2e; color:#fff; padding:.7rem 1rem; font-weight:600; font-size:.95rem; }
.card-b { padding:1rem 1.1rem; }
.exec { font-size:.98rem; margin:0; }
.arch-box { background:linear-gradient(135deg,#f8f9fa,#eef1f4); border:2px dashed #d1d5db;
  border-radius:12px; padding:1.4rem; text-align:center; margin:1rem 0; }
.arch-box svg { max-width:100%; height:auto; }
table { border-collapse:collapse; width:100%; font-size:.9rem; }
th,td { text-align:left; padding:.55rem .6rem; border-bottom:1px solid #eef0f2; vertical-align:top; }
td.why { font-size:.8rem; color:#4b5563; max-width:26rem; }
thead th { background:#1a1a2e; color:#fff; font-weight:600; }
tbody tr:last-child td { border-top:2px solid #d1d5db; font-weight:600; }
.badge { display:inline-block; font-size:.72rem; padding:.28em .7em; border-radius:20px;
  font-weight:600; color:#fff; }
.tradeoff { background:#fff; border:1px solid #e5e7eb; border-left:4px solid var(--green);
  border-radius:8px; padding:.7rem .9rem; margin:.7rem 0; }
.tradeoff .impact { color:#374151; font-size:.92rem; margin-top:.25rem; }
.tradeoff .aff { color:#6b7280; font-size:.8rem; margin-top:.3rem; }
.risk { background:#fff; border:1px solid #e5e7eb; border-left:4px solid #6b7280;
  border-radius:8px; padding:.6rem .9rem; margin:.55rem 0; }
.risk.HIGH,.risk.CRITICAL { border-left-color:#dc3545; }
.risk.MEDIUM { border-left-color:#ffc107; }
.risk.LOW { border-left-color:#28a745; }
.sev { font-size:.7rem; font-weight:700; padding:.14em .5em; border-radius:5px; margin-right:.5rem; color:#fff; }
.sev.HIGH,.sev.CRITICAL { background:#dc3545; }
.sev.MEDIUM { background:#ffc107; color:#5b4708; }
.sev.LOW { background:#28a745; }
.note { background:#f1f5f9; border:1px solid #e2e8f0; border-radius:8px; padding:.7rem .9rem;
  font-size:.88rem; color:#475569; margin:.8rem 0; }
ul { margin:.4rem 0; padding-left:1.2rem; }
footer { margin-top:2.5rem; padding:1.2rem 0 3rem; border-top:1px solid #e5e7eb; color:#6b7280; font-size:.83rem; }
"""

# Engine badge colours, following the reference template's palette, darkened where
# needed so white badge text keeps a WCAG AA contrast ratio of >= 4.5:1 (axe
# "color-contrast" flagged the un-darkened "aurora" orange at 2.14:1 and the
# un-darkened "elasticache" red at a razor-thin 4.52:1 that flipped pass/fail
# between browsers).
_ENGINE_BADGE = {
    "dynamodb": "#3b48cc",
    "documentdb": "#0c7838",
    "aurora_postgresql": "#9c5700",
    "aurora_mysql": "#9c5700",
    "elasticache": "#c62f25",
    "memorydb": "#c62f25",
    "opensearch": "#0e7c86",
    "neptune": "#7048e8",
    "keyspaces": "#8250df",
}


def _engine_badge(engine: str) -> str:
    color = _ENGINE_BADGE.get(engine, "#6b7280")
    return (
        f'<span class=badge style="background:{color}">'
        f"{escaping.html_text(display_engine(engine))}</span>"
    )


def _risk_tile_class(level: str) -> str:
    lv = str(level).upper()
    if lv in ("HIGH", "CRITICAL"):
        return "red"
    if lv == "MEDIUM":
        return "amber"
    return "green"


def _meta_tags(prov: dict[str, str] | None) -> str:
    if not prov:
        return ""
    return "".join(
        f'<meta name="x-dbmod-{k.replace("_", "-")}" content="{escaping.html_attr(v)}">'
        for k, v in prov.items()
        if v
    )


def _provenance_footer_html(prov: dict[str, str] | None) -> str:
    if not prov:
        return ""
    bits = f"{escaping.html_text(prov['filename'])}"
    if prov.get("job_id"):
        bits += f" &middot; job {escaping.html_text(prov['job_id'])}"
    if prov.get("generated"):
        bits += f" &middot; generated {escaping.html_text(prov['generated'])}"
    return f"<p class=note>{bits}</p>"


def render_decision_report_html(
    report: dict[str, Any],
    trust_generated_summary: bool = True,
    prov: dict[str, str] | None = None,
) -> str:
    """Stakeholder-facing decision document: why / what / cost / risk.

    Self-contained HTML (offline-safe: no CDN, no external CSS/JS/fonts) with an
    inline SVG architecture diagram. The visual language — dark hero, green
    accent, metric tiles, cards, engine-colour badges — mimics the reference
    template while inlining every style so a stakeholder opening the download
    offline or behind a strict CSP still gets the full layout.

    ``trust_generated_summary``: when False, the caller has determined that no
    schema design ran, so the generated narrative summary (which on such runs can
    claim schema work that never happened) is withheld and the deterministic
    summary is used instead.
    """

    def esc(s: Any) -> str:
        return escaping.html_text(s)

    db = report.get("database_name", "?")
    arch = report.get("recommended_architecture") or {}
    risk = report.get("risk_assessment") or {}
    tco = report.get("tco_analysis") or {}
    engines = _architecture_engines(report)
    # #225: prefer the schema-design-based figure (#257/#258)
    # when it has one; fall back to the wave-table count (resolve_migration_waves,
    # the same fallback the deck tile uses) only when that figure is 0, so this
    # sentence can't disagree with the roadmap just below it on the same page.
    #
    # #321 review (finding 4): wave 1 (Aurora) now also carries ``moves_from``,
    # but its ``table_count`` is the *whole* collected schema, not a
    # purpose-built-engine migration -- summing it in here would both double
    # count against the later waves it's a superset of, and relabel "the
    # whole schema moves to Aurora" as "tables migrate to a purpose-built
    # engine". This figure keeps its pre-#321 meaning: tables that leave
    # Aurora for a purpose-built engine, so wave 1 is excluded from the sum
    # (see ``pptx_report.derive``'s identical fix for the deck tile).
    migrated = sum(e["migrates"] for e in engines if e["role"] == "Migration target") or sum(
        w.get("table_count", 0)
        for w in resolve_migration_waves(report)
        if w.get("moves_from") and w.get("homogeneity") is None
    )
    risk_level = risk.get("overall_risk_level", "not assessed")

    out = [
        "<!doctype html><html lang=en><head><meta charset=utf-8>",
        '<meta name=viewport content="width=device-width,initial-scale=1">',
        f"<title>Decision Report \u2014 {esc(db)}"
        + (f" \u2014 {esc(prov['job_id'][:8])}" if prov and prov.get("job_id") else "")
        + "</title>",
        _meta_tags(prov),
        f"<style>{_DECISION_CSS}</style></head><body>",
        # Landmarks for assistive tech (#184): the hero is the banner, the body is
        # the one main region, and the footer stays outside <main> so it keeps its
        # contentinfo role.
        "<header class=hero><div class=wrap>",
        "<h1>Database Modernization \u2014 Decision Report</h1>",
        f"<p class=sub>Source database: <b>{esc(db)}</b></p>",
        f'<span class=pill>Architecture <b>{esc(arch.get("architecture_type", "not determined"))}</b></span>',
        f"<span class=pill>Overall risk <b>{esc(risk_level)}</b></span>",
        "</div></header>",
        "<div class=wrap><main>",
        "<div class=tiles>",
    ]
    current_cost_text, savings_text = _cost_baseline_text(tco)
    # Tile figures are text, not <h3>: a heading that reads "$582.76" is noise when
    # navigating by heading, and it skipped a level after the h1 (#184).
    out += [
        f'<div class="tile slate"><p>Current monthly</p><p class=val>{esc(current_cost_text)}</p></div>',
        f'<div class="tile green"><p>Projected monthly</p><p class=val>{_fmt_usd(tco.get("projected_monthly_cost"))}</p></div>',
        f'<div class="tile green"><p>Savings</p><p class=val>{esc(savings_text)}</p></div>',
        f'<div class="tile blue"><p>Engines</p><p class=val>{len(engines)}</p></div>',
        f'<div class="tile slate"><p>Tables migrate</p><p class=val>{migrated}</p></div>',
        f'<div class="tile {_risk_tile_class(risk_level)}"><p>Overall risk</p><p class=val>{esc(risk_level)}</p></div>',
        "</div>",
    ]
    eliminated_cost_text = _eliminated_engine_costs_text(tco)
    if eliminated_cost_text:
        out.append(
            "<p class=note data-figure=eliminated-engines>Already removed from this comparison: "
            f"{esc(eliminated_cost_text)}, "
            "retired before this projection; see Engines not used below.</p>"
        )

    summary = (
        report.get("summary") if trust_generated_summary else report.get("summary_deterministic")
    )
    summary = summary or report.get("summary_deterministic")
    if summary and summary == report.get("summary_deterministic"):
        summary = label_summary_counts(summary, report)
    if summary:
        out += [
            "<h2 class=section-title>Executive summary</h2>",
            f"<div class=card><div class=card-b><p class=exec>{esc(summary.strip())}</p></div></div>",
        ]
        if not trust_generated_summary:
            out += [
                "<p class=note>The generated narrative was withheld because it referenced "  # nosemgrep: string-concat-in-list -- intentional multi-line string
                "schema work this run did not perform; the figures here are unaffected.</p>"
            ]
        elif report.get("summary_source") == "deterministic_fallback":
            out += [
                "<p class=note>The generated narrative was withheld because it "
                f"{esc(fallback_withheld_reason(report))}; this summary is built from the "
                "effective assignment.</p>"
            ]

    out += [
        "<h2 class=section-title>Recommended architecture</h2>",
        f"<div class=arch-box>{architecture_svg(report)}</div>",
    ]

    if engines:
        out += [
            "<div class=card><div class=card-b>",
            "<table><thead><tr><th>Engine</th><th>Role</th><th>Workload</th>"  # nosemgrep: string-concat-in-list -- intentional multi-line string
            "<th>Scope</th><th>Est. monthly</th><th>Why</th></tr></thead><tbody>",
        ]
        total_cost = 0.0
        total_wl = 0.0
        for e in engines:
            wl = e.get("workload")
            c = e.get("cost")
            if isinstance(c, (int, float)):
                total_cost += c
            if isinstance(wl, (int, float)):
                total_wl += wl
            # data-engine/data-figure: stable hooks for the deliverable-figures
            # e2e test (#573), which reads each figure back from the page.
            out.append(
                f"<tr data-engine=\"{escaping.html_attr(e['engine'])}\">"
                f"<td>{_engine_badge(e['engine'])}</td>"
                f"<td class=role>{esc(e['role'])}</td>"
                f"<td data-figure=workload>{_workload_cell(e)}</td>"
                f"<td data-figure=scope>{esc(e['scope'])}</td>"
                f"<td data-figure=monthly-cost>{_fmt_usd(c)}</td>"
                f"<td class=why>{esc(e['rationale'] or DASH)}</td></tr>"
            )
        out.append(
            f"<tr data-figure=total><td colspan=2>Total</td>"
            f"<td>{total_wl:.0f}%</td>"
            f"<td data-figure=scope>{migrated} {plural_noun(migrated, 'table')} "
            f"{plural_verb(migrated, 'migrates', 'migrate')}</td>"
            f"<td data-figure=monthly-cost>{_fmt_usd(total_cost)}</td><td></td></tr>"
        )
        out.append("</tbody></table></div></div>")
        # First appearance of a fit figure in this report (inside "Why", e.g. "93%
        # mean fit"): the plain-language definition every deliverable states once,
        # shared with the engineering report, the deck and the UI (bug: fit score
        # presented as a confidence percentage).
        out.append(f"<p class=note>{esc(WORKLOAD_FIT_DEFINITION)}</p>")

        retained = [e["engine"] for e in engines if e["role"] == "Retained"]
        caches = [e["engine"] for e in engines if e["role"] == "Cache layer"]
        migr = [e["engine"] for e in engines if e["role"] == "Migration target"]
        note_bits = []
        if retained:
            note_bits.append(
                f"<b>{', '.join(esc(display_engine(x)) for x in retained)}</b> is retained as "
                "the relational core (source-compatible, no migration)."
            )
        search = [e for e in engines if e["role"] == SEARCH_READ_MODEL]
        if search:
            names = ", ".join(esc(display_engine(e["engine"])) for e in search)
            note_bits.append(
                f"<b>{names}</b> is a search read model: it serves its queries from data "
                "synced from the engines that own those tables, holds no system-of-record "
                "data, and is built after its owners."
            )
        if caches:
            note_bits.append(_cache_note(engines, report))
        else:
            # #459 round 2: a full safety-net drop (every cached read dropped)
            # removes the cache engine from `ranking` entirely (it owns no
            # workload, #296) -- so it is never in `engines` and the `if
            # caches:` branch above never fires. Without this, the note that
            # explains the drop (and that none remain) would silently vanish
            # from the one deliverable that otherwise never mentions the cache
            # layer at all once it owns nothing -- exactly the shape #424's
            # report described.
            fallback_notes = (report.get("cache_overlay") or {}).get("safety_net_notes") or []
            if fallback_notes:
                note_bits.append(" ".join(esc(x) for x in fallback_notes))
        if migr:
            note_bits.append(
                f"The migration moves the {migrated} {plural_noun(migrated, 'table')} assigned to "
                f"{', '.join(esc(display_engine(x)) for x in migr)}; the per-engine costs above "
                "reconcile to the projected total."
            )
        if any(SHARED_TABLES_LABEL in str(e["scope"]) for e in engines):
            note_bits.append(SHARED_TABLES_NOTE)
        if note_bits:
            out.append("<p class=note>" + " ".join(note_bits) + "</p>")

    out += _tied_queries_html(report)
    out += _why_engines_were_dropped_html(report)
    out += _roadmap_html(report)

    risks = filtered_risks(report)
    strategies = [s for s in (risk.get("mitigation_strategies") or []) if s]
    resolved_count = len(resolved_risks(report))
    if risks or strategies or resolved_count:
        out += ["<h2 class=section-title>Risk posture</h2>", "<div class=card><div class=card-b>"]
        if not risks and resolved_count:
            # #434: with no open risks this section used to disappear entirely
            # when there were no mitigation strategies either, saying nothing
            # about the resolved risks the Engineering Report still lists --
            # "overall risk LOW" with no section at all reads as "no migration
            # risk", which the caveat below exists to rule out.
            out.append(
                f"<p>No open migration risks; {resolved_count} "
                f"{plural_verb(resolved_count, 'was', 'were')} resolved by the assignment "
                "(see the Engineering Report).</p>"
            )
            out.append(f"<p class=note>{esc(NO_OPEN_RISKS_CAVEAT)}</p>")
        if risks:
            sev_txt = _severity_counts_text(risks)
            types = sorted(
                {
                    str(r.get("risk_type", "")).replace("_", " ").lower()
                    for r in risks
                    if r.get("risk_type")
                }
            )
            types_txt = ", ".join(types) if types else "several areas"
            n_resolved = len(resolved_risks(report))
            # The resolved risks are not among the open ones; saying "(…; 4 resolved)"
            # inside the open count read as 4 of them being resolved (#258).
            resolved_txt = (
                f" {n_resolved} more {plural_verb(n_resolved, 'was', 'were')} resolved by the "
                "assignment."
                if n_resolved
                else ""
            )
            out.append(
                f"<p data-figure=risk-summary>Overall risk <b>{esc(risk_level)}</b>. "
                f"{len(risks)} open migration "
                f"{plural_noun(len(risks), 'risk')} "
                f"({sev_txt}) across {esc(types_txt)}.{resolved_txt} The full "
                "risk register, with "
                "per-engine detail and mitigations, and the migration trade-offs are in the "
                "Engineering Report.</p>"
            )
            # How the level is derived (#248; synthesis_report.overall_risk_level).
            out.append(
                "<p class=note>Overall risk is the highest severity among the open risks; "
                "risks the assignment resolved do not count.</p>"
            )
        if strategies:
            out.append("<p><b>Mitigation strategies</b></p><ul>")
            out += [f"<li>{esc(s)}</li>" for s in strategies]
            out.append("</ul>")
        out.append("</div></div>")

    out += [
        "</main>",
        "<footer>Engine and query assignments are produced deterministically \u2014 no language "  # nosemgrep: string-concat-in-list -- intentional multi-line string
        "model decides which engine a table or query goes to. The executive summary is written "
        "over already-computed results and cannot change a recommendation. The complete "
        "machine-readable assessment is available as the Assessment Data (JSON) artifact."
        + _provenance_footer_html(prov)
        + "</footer>",
        "</div></body></html>",
    ]
    return "\n".join(o for o in out if o)


def _workload_cell(e: dict[str, Any]) -> str:
    """Workload column of the recommendation table (owner share, or cache counts)."""
    esc = escaping.html_text
    if e.get("cached_queries") is not None:
        return esc(cache_layer_text(e["cached_queries"], e["cached_call_share"]))
    wl = e.get("workload")
    return esc(fmt_num(wl, 1) + "%") if isinstance(wl, (int, float)) else "-"
