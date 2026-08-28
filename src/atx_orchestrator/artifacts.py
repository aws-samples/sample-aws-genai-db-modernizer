"""Publish phase outputs to the AWS Transform Artifacts panel.

The pipeline's system of record is S3, written through ``ArtifactStore``
(``S3ArtifactStore`` is a raw ``put_object``). That makes an object durable but
gives the platform nothing to show: no artifact record, no download link, no
entry in the Artifacts panel. Registering a platform artifact is a separate call,
and until 2026-08-21 nothing in ``src/atx_orchestrator/`` made it — which is why
a successful synthesis run produced a report the customer could not reach.

This is a port of the shape already working in
``docdb-mig-exp-atx/src/atx_orchestrator/sizing.py`` (lines 159-183). Two
properties of that implementation are deliberate and preserved here:

**The S3 copy is written first and independently.** Callers persist through the
store, then publish. If publishing fails the data is already safe.

**Publishing never raises.** A failure to register an artifact must not fail a
phase whose real work succeeded. That mistake was made once already, in the
synthesis guard, where an exception after the report was durable turned a
successful run into a reported failure and left the agent telling the customer no
report existed. Here a failure logs a warning and returns an empty mapping.

Not yet implemented: worklogs and ``plan_step_id``. An artifact carries a
download link in the Artifacts panel; the *worklog* is a second index that makes
it appear in the step narrative. The reference implementation does not do this
either, so it is unproven and left as follow-up.
"""

from __future__ import annotations

import html as _html
import logging
from typing import Any, Literal

logger = logging.getLogger(__name__)

# The SDK types these as Literals, so they are the authoritative enums — more
# reliable than copying whatever the reference implementation happened to pass.
# Surfaced by mypy on 2026-08-21; note TXT, not TEXT.
CategoryType = Literal[
    "AGENT_INPUT",
    "AGENT_OUTPUT",
    "CUSTOMER_INPUT",
    "CUSTOMER_OUTPUT",
    "HITL_FROM_AGENT",
    "HITL_FROM_USER",
    "INTERNAL",
    "PLAN_STEP_OUTPUT",
    "PLAN_STEP_SUMMARY",
    "STATE",
]
FileType = Literal["CSV", "HTML", "JSON", "MARKDOWN", "OTHER", "PDF", "PPTX", "TXT", "XLSX", "ZIP"]


def publish(items: list[tuple[bytes, FileType, str, CategoryType]]) -> dict[str, str]:
    """Register content with the platform so it appears in the Artifacts panel.

    Args:
        items: ``(content, file_type, label, category_type)`` tuples. ``label`` is
            what the customer sees in the panel, so write it for them.

    Returns:
        ``{label: artifact_id}`` for whatever uploaded. Empty when running outside
        the ATX runtime, or when publishing failed. **Never raises** — the caller's
        S3 copy is the system of record and its phase must not fail over this.

    Each item is uploaded independently so one rejection does not lose the rest.
    That matters because ``category_type`` is caller-role-scoped: a category valid
    from the agent side may be refused from the operator side, and the warning names
    the category so the cause is visible rather than silent.
    """
    published: dict[str, str] = {}
    try:
        from agent_builder_sdk.agentic_framework.artifact_store import ArtifactStore
        from agent_builder_sdk.agentic_framework.client_factory import get_agentic_api_client
        from agent_builder_sdk.agentic_framework.common import calculate_digest
        from agent_builder_sdk.env_var import get_agent_context_from_env

        ctx = get_agent_context_from_env()
        store = ArtifactStore(
            workspace_id=ctx.workspace_id,
            job_id=ctx.job_id,
            agent_instance_id=ctx.agent_instance_id,
            client=get_agentic_api_client(),
        )
        for content, file_type, label, category in items:
            try:
                artifact_id = store.upload_artifact(
                    content,
                    calculate_digest(content),
                    category_type=category,
                    file_type=file_type,
                    label=label,
                )
                published[label] = artifact_id
                logger.info(
                    "Published artifact: label=%r type=%s category=%s bytes=%d id=%s",
                    label,
                    file_type,
                    category,
                    len(content),
                    artifact_id,
                )
            except Exception as exc:  # noqa: BLE001
                # One artifact failing must not lose the others.
                logger.warning(
                    "Artifact upload failed for %r (type=%s category=%s): %s",
                    label,
                    file_type,
                    category,
                    exc,
                )
    except Exception as exc:  # noqa: BLE001
        # Expected outside the ATX runtime (no agent context env vars), e.g. local
        # runs and tests. Also catches SDK or client construction failure.
        logger.warning("Artifact publishing unavailable, S3 copies still written: %s", exc)
    return published


def _fmt_usd(x: Any) -> str:
    return f"${x:,.2f}" if isinstance(x, (int, float)) else "-"


def _engine_costs(report: dict[str, Any]) -> dict[str, float]:
    """engine name -> monthly USD, from tco_analysis.cost_breakdown."""
    out: dict[str, float] = {}
    for row in (report.get("tco_analysis") or {}).get("cost_breakdown") or []:
        db, c = row.get("database"), row.get("monthly_cost_usd")
        if db is not None and isinstance(c, (int, float)):
            out[db] = c
    return out


def _completed_designs(report: dict[str, Any]) -> dict[str, dict]:
    """engine -> design summary, only for engines whose design completed."""
    sd = report.get("schema_designs") or {}
    return {e: v for e, v in sd.items() if isinstance(v, dict) and v.get("status") == "completed"}


def _risk_engine_and_body(desc: Any) -> tuple[str, str]:
    """Split a risk description into its ``[engine]`` prefix and the remaining text.

    Risks carry no explicit engine field; the engine is encoded as a leading
    ``[documentdb]`` / ``[elasticache]`` tag in the description. Returns
    ``(engine, body)`` with ``engine`` defaulting to ``"(general)"``.
    """
    s = str(desc or "").strip()
    engine = "(general)"
    if s.startswith("["):
        j = s.find("]")
        if j != -1:
            engine = s[1:j].strip() or "(general)"
            s = s[j + 1 :].strip()
    return engine, s


def _risk_has_content(desc: Any) -> bool:
    """False for the malformed empty risks synthesis emits as ``[engine] unknown:``
    with nothing after — noise that should not reach either report."""
    _, body = _risk_engine_and_body(desc)
    if body.lower().startswith("unknown:"):
        body = body[len("unknown:") :].strip()
    return bool(body)


_CACHE_ENGINES = {"elasticache", "memorydb"}

_ROLE_STROKE = {
    "Retained": "#1F7A3D",
    "Migration target": "#146EB4",
    "Cache layer": "#8B5CF6",
    "Assessed": "#6B7280",
}


def _engine_role(engine: str, recommended: set, schema_designs: dict[str, Any]) -> str:
    """Role of an engine in the target architecture.

    ``recommended_architecture.databases`` lists only net-new migration targets —
    it drops the retained relational core and cache engines because it filters on
    a ``schema_design_available`` flag that is False for both (defect (d) for the
    cache, which has a completed design the flag does not count; by design for a
    retained engine, which has no migration design). Roles are therefore derived
    from the engine kind and its design status, not from that list alone.
    """
    if engine in _CACHE_ENGINES:
        return "Cache layer"
    if engine in recommended:
        return "Migration target"
    status = (schema_designs.get(engine) or {}).get("status")
    if status in ("not_available", "skipped"):
        return "Retained"
    if status == "completed":
        return "Migration target"
    return "Assessed"


def _architecture_engines(report: dict[str, Any]) -> list[dict[str, Any]]:
    """The full target architecture, one entry per engine, ordered by workload.

    Derived from ``ranking`` (every engine that carries workload) joined with
    ``tco_analysis.cost_breakdown``, ``recommended_architecture.databases`` (source
    table counts for migration targets) and ``schema_designs`` (design status and
    cache-object counts). This is the picture synthesis actually computed;
    ``recommended_architecture.databases`` alone is a lossy projection of it that
    neither reconciles to the cost total nor matches the executive summary.
    """
    ranking = [r for r in (report.get("ranking") or []) if isinstance(r, dict)]
    arch = report.get("recommended_architecture") or {}
    dbs = [d for d in (arch.get("databases") or []) if isinstance(d, dict)]
    recommended = {d.get("service") for d in dbs}
    src_tables = {d.get("service"): d.get("table_count") for d in dbs}
    schema_designs = report.get("schema_designs") or {}
    costs = _engine_costs(report)

    rows: list[dict[str, Any]] = []
    for r in ranking:
        eng = r.get("target")
        if not eng:
            continue
        role = _engine_role(eng, recommended, schema_designs)
        objs = (schema_designs.get(eng) or {}).get("tables")
        objs = len(objs) if isinstance(objs, list) else None
        if role == "Migration target":
            n = src_tables.get(eng)
            scope = (
                f"{n} tables" if n is not None else (f"{objs} target objects" if objs else "\u2014")
            )
        elif role == "Cache layer":
            scope = f"{objs} key designs" if objs else "cache"
        elif role == "Retained":
            scope = "source schema retained"
        else:
            scope = "\u2014"
        rows.append(
            {
                "engine": eng,
                "role": role,
                "workload": r.get("workload_percent"),
                "scope": scope,
                "cost": costs.get(eng),
                "rationale": next(
                    (d.get("rationale") for d in dbs if d.get("service") == eng),
                    r.get("assignment_reason_summary") or "",
                ),
            }
        )
    rows.sort(
        key=lambda x: (x["workload"] if isinstance(x["workload"], (int, float)) else -1),
        reverse=True,
    )
    return rows


def architecture_svg(report: dict[str, Any]) -> str:
    """Inline SVG of the full target architecture.

    Source on the left, every engine that carries workload on the right, colored
    by role (retained / migration target / cache) with the cache layer dashed.
    Driven by ``_architecture_engines`` so the diagram, the recommendation table
    and the cost total all reflect the same 5-engine picture rather than the
    3-engine ``recommended_architecture.databases`` projection.

    Light theme, self-contained, no external fetch, renders offline.
    """
    src = report.get("database_name", "source")
    engines = _architecture_engines(report)

    def esc(s: Any) -> str:
        return _html.escape(str(s))

    box_w, box_h, gap = 230, 54, 20
    left_x, right_x = 24, 380
    n = max(len(engines), 1)
    body_top = 20
    stack_h = n * (box_h + gap) - gap
    height = max(body_top + stack_h + 20, 140)
    width = right_x + box_w + 24
    src_y = body_top + stack_h // 2 - box_h // 2

    def box(x, y, w, h, title, sub, stroke, dashed=False):
        dash = ' stroke-dasharray="6 4"' if dashed else ""
        ty = y + (h / 2 - 5 if sub else h / 2 + 4)
        title_t = (
            f'<text x="{x + w / 2}" y="{ty}" text-anchor="middle" font-size="14" '
            f'font-weight="600" fill="#111">{esc(title)}</text>'
        )
        sub_t = (
            f'<text x="{x + w / 2}" y="{y + h / 2 + 13}" text-anchor="middle" '
            f'font-size="10.5" fill="#555">{esc(sub)}</text>'
            if sub
            else ""
        )
        return (
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="7" fill="#fff" '
            f'stroke="{stroke}" stroke-width="2"{dash}/>{title_t}{sub_t}'
        )

    p = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif">',
        '<defs><marker id="arr" markerWidth="9" markerHeight="9" refX="7" refY="3" '
        'orient="auto"><path d="M0,0 L7,3 L0,6 Z" fill="#146EB4"/></marker></defs>',
        f'<rect width="{width}" height="{height}" fill="#fff"/>',
        box(left_x, src_y, box_w, box_h, str(src), "source database", "#232F3E"),
    ]
    for i, e in enumerate(engines):
        y = body_top + i * (box_h + gap)
        role = e["role"]
        stroke = _ROLE_STROKE.get(role, "#6B7280")
        dashed = role == "Cache layer"
        sub_bits = [role]
        if isinstance(e.get("workload"), (int, float)):
            sub_bits.append(f"{e['workload']}%")
        if e.get("cost") is not None:
            sub_bits.append(f"{_fmt_usd(e['cost'])}/mo")
        sub = "  \u00b7  ".join(sub_bits)
        p.append(box(right_x, y, box_w, box_h, str(e["engine"]), sub, stroke, dashed=dashed))
        dash_line = ' stroke-dasharray="5 4"' if dashed else ""
        marker = "" if dashed else ' marker-end="url(#arr)"'
        p.append(
            f'<line x1="{left_x + box_w}" y1="{src_y + box_h / 2}" x2="{right_x}" '
            f'y2="{y + box_h / 2}" stroke="{stroke}" stroke-width="1.5"{dash_line}{marker}/>'
        )
    p.append("</svg>")
    return "".join(p)


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
.tile { flex:1 1 150px; border-radius:12px; padding:1.2rem; color:#fff; text-align:center; }
.tile h3 { font-size:1.7rem; font-weight:700; margin:0 0 .2rem; }
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

# Engine badge colours, following the reference template's palette.
_ENGINE_BADGE = {
    "dynamodb": "#3b48cc",
    "documentdb": "#13aa52",
    "aurora_postgresql": "#ff9900",
    "aurora_mysql": "#ff9900",
    "elasticache": "#dc382d",
    "memorydb": "#dc382d",
    "opensearch": "#0e7c86",
    "neptune": "#7048e8",
    "keyspaces": "#8250df",
}


def _engine_badge(engine: str) -> str:
    color = _ENGINE_BADGE.get(engine, "#6b7280")
    return f'<span class=badge style="background:{color}">{_html.escape(str(engine))}</span>'


def _risk_tile_class(level: str) -> str:
    lv = str(level).upper()
    if lv in ("HIGH", "CRITICAL"):
        return "red"
    if lv == "MEDIUM":
        return "amber"
    return "green"


def render_decision_report_html(
    report: dict[str, Any], trust_generated_summary: bool = True
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
        return _html.escape(str(s))

    db = report.get("database_name", "?")
    arch = report.get("recommended_architecture") or {}
    risk = report.get("risk_assessment") or {}
    tco = report.get("tco_analysis") or {}
    engines = _architecture_engines(report)
    migrated = 0
    for e in engines:
        if e["role"] == "Migration target":
            digits = "".join(ch for ch in str(e["scope"]) if ch.isdigit())
            if digits:
                migrated += int(digits)
    risk_level = risk.get("overall_risk_level", "not assessed")

    out = [
        "<!doctype html><html lang=en><head><meta charset=utf-8>",
        '<meta name=viewport content="width=device-width,initial-scale=1">',
        f"<title>Decision Report \u2014 {esc(db)}</title>",
        f"<style>{_DECISION_CSS}</style></head><body>",
        "<div class=hero><div class=wrap>",
        "<h1>Database Modernization \u2014 Decision Report</h1>",
        f"<p class=sub>Source database: <b>{esc(db)}</b></p>",
        f'<span class=pill>Architecture <b>{esc(arch.get("architecture_type", "not determined"))}</b></span>',
        f"<span class=pill>Overall risk <b>{esc(risk_level)}</b></span>",
        "</div></div>",
        "<div class=wrap>",
        "<div class=tiles>",
        f'<div class="tile green"><h3>{_fmt_usd(tco.get("projected_monthly_cost"))}</h3><p>Projected monthly</p></div>',
        f'<div class="tile blue"><h3>{len(engines)}</h3><p>Engines</p></div>',
        f'<div class="tile slate"><h3>{migrated}</h3><p>Tables migrate</p></div>',
        f'<div class="tile {_risk_tile_class(risk_level)}"><h3>{esc(risk_level)}</h3><p>Overall risk</p></div>',
        "</div>",
    ]

    summary = (
        report.get("summary") if trust_generated_summary else report.get("summary_deterministic")
    )
    summary = summary or report.get("summary_deterministic")
    if summary:
        out += [
            "<h2 class=section-title>Executive summary</h2>",
            f"<div class=card><div class=card-b><p class=exec>{esc(summary.strip())}</p></div></div>",
        ]
        if not trust_generated_summary:
            out += [
                "<p class=note>The generated narrative was withheld because it referenced "
                "schema work this run did not perform; the figures here are unaffected.</p>"
            ]

    out += [
        "<h2 class=section-title>Recommended architecture</h2>",
        f"<div class=arch-box>{architecture_svg(report)}</div>",
    ]

    if engines:
        out += [
            "<div class=card><div class=card-b>",
            "<table><thead><tr><th>Engine</th><th>Role</th><th>Workload</th>"
            "<th>Scope</th><th>Est. monthly</th></tr></thead><tbody>",
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
            out.append(
                f"<tr><td>{_engine_badge(e['engine'])}</td>"
                f"<td class=role>{esc(e['role'])}</td>"
                f"<td>{esc(f'{wl}%') if isinstance(wl, (int, float)) else '-'}</td>"
                f"<td>{esc(e['scope'])}</td>"
                f"<td>{_fmt_usd(c)}</td></tr>"
            )
        out.append(
            f"<tr><td colspan=2>Total</td>"
            f"<td>{total_wl:.0f}%</td>"
            f"<td>{migrated} tables migrate</td>"
            f"<td>{_fmt_usd(total_cost)}</td></tr>"
        )
        out.append("</tbody></table></div></div>")

        retained = [e["engine"] for e in engines if e["role"] == "Retained"]
        caches = [e["engine"] for e in engines if e["role"] == "Cache layer"]
        migr = [e["engine"] for e in engines if e["role"] == "Migration target"]
        note_bits = []
        if retained:
            note_bits.append(
                f"<b>{', '.join(esc(x) for x in retained)}</b> is retained as the relational "
                "core (source-compatible, no migration)."
            )
        if caches:
            note_bits.append(
                f"<b>{', '.join(esc(x) for x in caches)}</b> is an additive cache layer."
            )
        if migr:
            note_bits.append(
                f"The migration moves the {migrated} tables assigned to "
                f"{', '.join(esc(x) for x in migr)}; the per-engine costs above reconcile to the "
                "projected total."
            )
        if note_bits:
            out.append("<p class=note>" + " ".join(note_bits) + "</p>")

    risks_all = [r for r in (risk.get("risks") or []) if isinstance(r, dict)]
    risks = [r for r in risks_all if _risk_has_content(r.get("description"))]
    strategies = [s for s in (risk.get("mitigation_strategies") or []) if s]
    if risks or strategies:
        out += ["<h2 class=section-title>Risk posture</h2>", "<div class=card><div class=card-b>"]
        if risks:
            hi = sum(1 for r in risks if str(r.get("severity", "")).upper() in ("HIGH", "CRITICAL"))
            med = sum(1 for r in risks if str(r.get("severity", "")).upper() == "MEDIUM")
            types = sorted(
                {
                    str(r.get("risk_type", "")).replace("_", " ").lower()
                    for r in risks
                    if r.get("risk_type")
                }
            )
            types_txt = ", ".join(types) if types else "several areas"
            out.append(
                f"<p>Overall risk <b>{esc(risk_level)}</b>. {len(risks)} migration risks identified "
                f"({hi} high, {med} medium) across {esc(types_txt)}. The full risk register, with "
                "per-engine detail and mitigations, and the migration trade-offs are in the "
                "Engineering Report.</p>"
            )
        if strategies:
            out.append("<p><b>Mitigation strategies</b></p><ul>")
            out += [f"<li>{esc(s)}</li>" for s in strategies]
            out.append("</ul>")
        out.append("</div></div>")

    out += [
        "<footer>Engine and query assignments are produced deterministically \u2014 no language "
        "model decides which engine a table or query goes to. The executive summary is written "
        "over already-computed results and cannot change a recommendation. The complete "
        "machine-readable assessment is available as the Assessment Data (JSON) artifact.</footer>",
        "</div></body></html>",
    ]
    return "\n".join(out)


def _mermaid_er(engine: str, design: dict, max_nodes: int = 15) -> str | None:
    """A small mermaid flowchart of source tables -> target tables for one engine.

    Returns None when the design has more target tables than ``max_nodes`` — a
    diagram past that point is an unreadable wall, and the target-table table
    already lists them completely.
    """
    tables = [t for t in (design.get("tables") or []) if isinstance(t, dict)]
    if not tables or len(tables) > max_nodes:
        return None
    lines = ["```mermaid", "flowchart LR"]
    seen_src: dict[str, str] = {}
    sid = 0
    for i, t in enumerate(tables):
        tgt = t.get("table_name", f"t{i}")
        tnode = f"T{i}"
        lines.append(f'    {tnode}["{tgt}"]')
        for s in t.get("source_tables") or []:
            if s not in seen_src:
                seen_src[s] = f"S{sid}"
                lines.append(f'    {seen_src[s]}[("{s}")]')
                sid += 1
            lines.append(f"    {seen_src[s]} --> {tnode}")
    lines.append("```")
    return "\n".join(lines)


def render_engineering_report_md(report: dict[str, Any]) -> str:
    """Build-team-facing document: migration map, per-engine target schemas,
    query groups. Markdown with mermaid fences, which render in the tooling
    engineers open it in (VS Code, GitHub, GitLab).
    """
    db = report.get("database_name", "?")
    out = [
        "# Database Modernization \u2014 Engineering Report",
        "",
        f"Source database: `{db}`. This is the build companion to the Decision Report: "
        "the source-to-target mapping, the per-engine target schemas, and the query "
        "co-dependency groups.",
        "",
    ]

    mappings = [m for m in (report.get("table_mappings") or []) if isinstance(m, dict)]
    if mappings:
        out += [
            f"## Migration map ({len(mappings)} tables)",
            "",
            "| Source table | Target engine | Target | Pattern | Confidence |",
            "|---|---|---|---|---|",
        ]
        for m in mappings:
            out.append(
                f"| `{m.get('source_table', '?')}` | {m.get('recommended_database', '?')} "
                f"| `{m.get('target_table', '-')}` | {m.get('aggregate_pattern', '-')} "
                f"| {m.get('confidence_score', '-')} |"
            )
        out.append("")

    designs = _completed_designs(report)
    if designs:
        out += ["## Target schemas by engine", ""]
        for eng, dz in designs.items():
            tables = [t for t in (dz.get("tables") or []) if isinstance(t, dict)]
            out += [
                f"### {eng} ({len(tables)} target objects, {dz.get('access_pattern_count', 0)} access patterns)",
                "",
            ]
            if tables:
                # engine-specific columns surface when present
                has_ttl = any("ttl_seconds" in t for t in tables)
                has_shards = any("shards" in t for t in tables)
                cols = ["Target", "Pattern", "Source tables", "GSIs"]
                if has_ttl:
                    cols.append("TTL(s)")
                if has_shards:
                    cols += ["Shards", "Replicas", "Fields"]
                out += ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
                for t in tables:
                    row = [
                        f"`{t.get('table_name', '?')}`",
                        str(t.get("aggregate_pattern", "-")),
                        ", ".join(f"`{s}`" for s in (t.get("source_tables") or [])) or "-",
                        str(t.get("gsi_count", "-")),
                    ]
                    if has_ttl:
                        row.append(str(t.get("ttl_seconds", "-")))
                    if has_shards:
                        row += [
                            str(t.get("shards", "-")),
                            str(t.get("replicas", "-")),
                            str(t.get("field_count", "-")),
                        ]
                    out.append("| " + " | ".join(row) + " |")
                out.append("")
            unsupported = dz.get("unsupported_patterns") or []
            if unsupported:
                out += [f"**Unsupported patterns ({len(unsupported)}):**", ""]
                out += [f"- {u}" for u in unsupported]
                out.append("")
            notes = dz.get("migration_notes")
            if notes:
                out += [
                    "**Migration notes:**",
                    "",
                    (notes if isinstance(notes, str) else str(notes)),
                    "",
                ]
            er = _mermaid_er(eng, dz)
            if er:
                out += [er, ""]
            elif tables:
                out += [
                    f"_ER diagram omitted ({len(tables)} target objects); see the table above._",
                    "",
                ]

    groups = [g for g in (report.get("query_groups") or []) if isinstance(g, dict)]
    if groups:
        out += [
            f"## Query co-dependency groups ({len(groups)})",
            "",
            "| Group | Engines | Access patterns | Source queries | Design RPS |",
            "|---|---|---|---|---|",
        ]
        for g in groups:
            engines = (
                ", ".join(g.get("engines") or [])
                if isinstance(g.get("engines"), list)
                else str(g.get("engines", "-"))
            )
            aps = g.get("access_patterns")
            sqs = g.get("source_queries")
            out.append(
                f"| {g.get('group_name', '?')} | {engines} "
                f"| {len(aps) if isinstance(aps, list) else (aps or '-')} "
                f"| {len(sqs) if isinstance(sqs, list) else (sqs or '-')} "
                f"| {g.get('total_design_rps', '-')} |"
            )
        out.append("")

    risks = [
        r
        for r in (report.get("risk_assessment") or {}).get("risks", [])
        if isinstance(r, dict) and _risk_has_content(r.get("description"))
    ]
    if risks:
        by_eng: dict[str, list] = {}
        for r in risks:
            eng, _ = _risk_engine_and_body(r.get("description"))
            by_eng.setdefault(eng, []).append(r)
        out += [f"## Risk register ({len(risks)})", ""]
        for eng, items in by_eng.items():
            out += [f"### {eng}", ""]
            for r in items:
                _, body = _risk_engine_and_body(r.get("description"))
                sev = r.get("severity", "-")
                rtype = str(r.get("risk_type", "")).replace("_", " ").lower()
                rid = r.get("risk_id", "-")
                out.append(f"- **{rid}** \u00b7 {sev} \u00b7 {rtype} \u2014 {body}")
                if r.get("mitigation"):
                    out.append(f"  - Mitigation: {r.get('mitigation')}")
                aff = list(r.get("affected_tables") or [])
                if aff:
                    shown = ", ".join(f"`{a}`" for a in aff[:8])
                    more = f" (+{len(aff) - 8} more)" if len(aff) > 8 else ""
                    out.append(f"  - Affects: {shown}{more}")
            out.append("")

    tradeoffs = [t for t in (report.get("trade_offs") or []) if isinstance(t, dict)]
    if tradeoffs:
        by_engine: dict[str, list] = {}
        for t in tradeoffs:
            by_engine.setdefault(t.get("engine") or "(general)", []).append(t)
        out += [f"## Migration trade-offs ({len(tradeoffs)})", ""]
        for eng, items in by_engine.items():
            out += [f"### {eng}", ""]
            for t in items:
                desc = str(t.get("description", "")).strip()
                impact = str(t.get("impact", "")).strip()
                line = f"- **{desc}**"
                if impact:
                    line += f" \u2014 {impact}"
                out.append(line)
                aff = list(t.get("source_tables") or [])
                if aff:
                    shown = ", ".join(f"`{s}`" for s in aff[:8])
                    more = f" (+{len(aff) - 8} more)" if len(aff) > 8 else ""
                    out.append(f"  - Affects: {shown}{more}")
            out.append("")

    out += [
        "---",
        "",
        "Assignments are deterministic. The complete machine-readable assessment is the "
        "Assessment Data (JSON) artifact.",
        "",
    ]
    return "\n".join(out)
