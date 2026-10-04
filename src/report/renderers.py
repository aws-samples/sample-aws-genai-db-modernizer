"""Pure renderers for the synthesis deliverables (decision report HTML, engineering
report Markdown, architecture SVG, provenance). Split out of
``src/atx_orchestrator/runtime/artifacts.py``, which now holds only platform
publishing."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from src.shared.engine_names import display_engine
from src.shared.unsupported_pattern import (
    unsupported_pattern_ids,
    unsupported_pattern_label,
    unsupported_pattern_text,
)

from . import escaping


def artifact_stem(
    database_name: str,
    artifact: str,
    job_id: str,
    generated: datetime | None = None,
) -> str:
    """Canonical filename stem: ``{database}_{artifact}_{job8}_{YYYYMMDD}``.

    The customer downloads ``1e8ec29e-0227-4ae4-9523-306d4a622c47.html`` and cannot
    tell which database, job or report it is. That is not an S3 problem — the keys
    written here have been descriptive all along — it is that
    ``ArtifactStore.upload_artifact(content, digest, category_type=, file_type=,
    label=)`` takes no filename, so the panel names the download after the artifact
    id. Two things follow: the S3 keys use this stem, and each rendered file states
    its own identity internally (HTML ``<title>`` + ``x-dbmod-*`` meta, Markdown
    front matter, JSON ``_artifact`` envelope) so a UUID download is still traceable.

    Underscore separates fields, hyphen lives inside one, so the stem splits cleanly
    on ``_``. ``job_id[:8]`` rather than the full UUID: 32 bits is ample within one
    database, and 36 characters of UUID would crowd out the two fields that actually
    distinguish the file. The full job id is in the S3 key and inside the file.

    If a future SDK's ``upload_artifact`` accepts a filename, pass this stem to it
    and the panel problem disappears; the SDK is container-only, so that is untested.
    """
    db = re.sub(r"[^a-z0-9]+", "-", database_name.lower()).strip("-") or "database"
    day = (generated or datetime.now(UTC)).strftime("%Y%m%d")
    return f"{db}_{artifact}_{job_id[:8]}_{day}"


def plural_noun(n: Any, singular: str, plural: str | None = None) -> str:
    """The correctly agreed noun for a count, e.g. ``f"{n} {plural_noun(n, 'risk')}"``.

    Every count+noun string across the decision report, engineering report and
    executive summary deck must agree on the English singular/plural rule.
    Before this helper existed, each call site wrote its own — most just hardcoded
    the plural form, which is how the deck printed "1 session store queries"
    (issue #206). Composing the count and the noun is left to the caller (who may
    need ``:,`` thousands separators or other formatting around the number); this
    only decides which form of the word to use.
    """
    try:
        is_one = float(n) == 1
    except (TypeError, ValueError):
        is_one = False
    return singular if is_one else (plural or f"{singular}s")


def plural_verb(n: Any, singular: str, plural: str) -> str:
    """The correctly agreed verb form for a count, e.g.
    ``f"{n} risk {plural_verb(n, 'sits', 'sit')} on dynamodb alone."``.

    English verb agreement is the *opposite* polarity of noun pluralisation: a
    singular subject (``n == 1``) takes the "-s" form ("sits", "is",
    "migrates"), a plural subject takes the bare form ("sit", "are",
    "migrate") -- and irregularly enough (``is``/``are``, ``touches``/
    ``touch``) that there is no safe default to derive one form from the
    other, unlike ``plural_noun``'s "append s". Both forms are always given
    explicitly.

    A dedicated helper rather than reusing ``plural_noun`` for verbs: a couple
    of #206 call sites did exactly that (``plural_noun(n, "sits", "sit")``),
    which happened to produce the right string but reads backwards --
    ``plural_noun``'s positional args mean "singular noun, optional plural
    noun", not "verb form for one, verb form for many".
    """
    try:
        is_one = float(n) == 1
    except (TypeError, ValueError):
        is_one = False
    return singular if is_one else plural


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
    """Defensive guard against a risk description with no real text after its
    ``[engine]`` prefix.

    This is no longer the primary line of defense it once was: before #210,
    synthesis could emit ``[engine] unknown:`` with nothing after it for an
    unsupported-pattern risk on an engine whose contract has neither
    ``pattern_type`` nor ``recommendation`` (documentdb/elasticache carry
    ``reason``/``workaround`` instead, which that code didn't read).
    ``build_risk_assessment`` now reads whichever fields a contract actually
    has (``src.shared.unsupported_pattern``), so a genuinely contentless
    risk should no longer occur there in practice. This filter remains as a
    general-purpose guard -- against any other producer emitting a risk with
    no body, and against the historical ``unknown:`` pattern surviving in
    already-generated ``report.json`` files.
    """
    _, body = _risk_engine_and_body(desc)
    if body.lower().startswith("unknown:"):
        body = body[len("unknown:") :].strip()
    return bool(body)


def filtered_risks(report: dict[str, Any]) -> list[dict[str, Any]]:
    """The one risk list every deliverable renders and counts from.

    ``risk_assessment.risks`` can in principle contain contentless entries
    (see ``_risk_has_content``); the decision report and engineering report
    dropped those before counting while the PDF/PPTX deck counted the raw
    list, so a report with any such entries produced disagreeing risk totals
    across deliverables (#201: "9 migration risks identified" in the decision
    report vs. 12 in ``report.json`` and the PDF, because 3 of the 12 were
    the contentless unsupported-pattern risks #210 also fixes at the source).
    Every deliverable must build its risk count and its risk list from this
    one function instead of re-deriving the filter.
    """
    risk = report.get("risk_assessment") or {}
    return [
        r
        for r in (risk.get("risks") or [])
        if isinstance(r, dict) and _risk_has_content(r.get("description"))
    ]


def _repeats(mitigation: Any, description: Any) -> bool:
    """True when ``mitigation`` is already contained in ``description`` (#222).

    Some risks (e.g. DynamoDB unsupported patterns carrying only ``recommendation``)
    use the same text for both; the engineering report then states it once.
    Compared case-insensitively with whitespace collapsed.
    """
    m = " ".join(str(mitigation or "").split()).casefold()
    d = " ".join(str(description or "").split()).casefold()
    return bool(m) and m in d


def resolved_risks(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Analysis risks the effective assignment resolved (``risk_assessment.resolved_risks``)."""
    risk = report.get("risk_assessment") or {}
    return [
        r
        for r in (risk.get("resolved_risks") or [])
        if isinstance(r, dict) and _risk_has_content(r.get("description"))
    ]


_CACHE_ENGINES = {"elasticache", "memorydb"}

_ROLE_STROKE = {
    "Retained": "#1F7A3D",
    "Migration target": "#146EB4",
    "Cache layer": "#8B5CF6",
    # Muted grey: an engine that was evaluated but serves nothing is not an active
    # component of the target architecture, and must not read as one in the diagram.
    "Evaluated": "#6B7280",
    "Assessed": "#6B7280",
}


def _engine_role(
    engine: str,
    recommended: set,
    schema_designs: dict[str, Any],
    workload: float | None = None,
) -> str:
    """Role of an engine in the target architecture.

    ``recommended_architecture.databases`` lists only net-new migration targets —
    it drops the retained relational core and cache engines because it filters on
    a ``schema_design_available`` flag that is False for both (defect (d) for the
    cache, which has a completed design the flag does not count; by design for a
    retained engine, which has no migration design). Roles are therefore derived
    from the engine kind and its design status, not from that list alone.

    An engine carrying **no workload** is ``Evaluated``: triage selected it, analysis
    scored it, and the assignment then routed nothing to it because another engine won
    every query. It is *not* ``Retained`` — retained means the engine still serves
    queries, which is true of the source relational core and false of an engine that
    won none. Calling it Retained told the customer to provision an engine holding
    nothing, and (via ``no_move`` in ``pptx_report``) pulled the Wave 1 confidence down
    to a floor set by an engine doing no work.

    Judged on workload rather than on the engine's name because the synthesis report
    carries no ``source_engine`` field. If every query migrated off the source engine it
    would read ``Evaluated`` rather than "retired" — rare, and Evaluated still claims
    nothing false.
    """
    if engine in _CACHE_ENGINES:
        return "Cache layer"
    if engine in recommended:
        return "Migration target"
    # Ordered deliberately: a cache engine or a real migration target keeps its role
    # whatever its workload, and this test precedes the status checks because the
    # ``skipped``/``not_available`` branch below is the one being corrected.
    if not workload:
        return "Evaluated"
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
        role = _engine_role(eng, recommended, schema_designs, r.get("workload_percent"))
        objs = (schema_designs.get(eng) or {}).get("tables")
        objs = len(objs) if isinstance(objs, list) else None
        if role == "Migration target":
            n = src_tables.get(eng)
            scope = (
                # Source tables mapped to the engine -- named as such, because the
                # summary also counts the schema design's *target* tables (#219).
                f"{n} source {plural_noun(n, 'table')}"
                if n is not None
                else (f"{objs} {plural_noun(objs, 'target object')}" if objs else "\u2014")
            )
        elif role == "Cache layer":
            scope = f"{objs} key {plural_noun(objs, 'design')}" if objs else "cache"
        elif role == "Retained":
            scope = "source schema retained"
        elif role == "Evaluated":
            scope = "no queries assigned"
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
        return escaping.svg_text(s)

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
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '  # nosemgrep: string-concat-in-list -- intentional multi-line string
        f'viewBox="0 0 {width} {height}" font-family="-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif">',
        '<defs><marker id="arr" markerWidth="9" markerHeight="9" refX="7" refY="3" '  # nosemgrep: string-concat-in-list -- intentional multi-line string
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
    return f'<span class=badge style="background:{color}">' f"{escaping.html_text(engine)}</span>"


def _risk_tile_class(level: str) -> str:
    lv = str(level).upper()
    if lv in ("HIGH", "CRITICAL"):
        return "red"
    if lv == "MEDIUM":
        return "amber"
    return "green"


def provenance(
    report: dict[str, Any],
    artifact: str,
    ext: str,
    job_id: str = "",
    source_artifact: str = "",
) -> dict[str, str]:
    """Identity block for one rendered deliverable.

    Carried inside the file (meta tags / front matter / ``_artifact`` envelope) so a
    download named after a UUID is still traceable to a database and a job. See
    ``artifact_stem`` for why the filename itself cannot be set.
    """
    db = str(report.get("database_name") or "database")
    jid = job_id or str(report.get("job_id") or "")
    now = datetime.now(UTC)
    return {
        "artifact": artifact,
        "database": db,
        "job_id": jid,
        "generated": now.isoformat(timespec="seconds"),
        "filename": f"{artifact_stem(db, artifact, jid, now)}.{ext}",
        "source_artifact": source_artifact,
    }


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
        f"<title>Decision Report \u2014 {esc(db)}"
        + (f" \u2014 {esc(prov['job_id'][:8])}" if prov and prov.get("job_id") else "")
        + "</title>",
        _meta_tags(prov),
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
                "<p class=note>The generated narrative was withheld because it referenced "  # nosemgrep: string-concat-in-list -- intentional multi-line string
                "schema work this run did not perform; the figures here are unaffected.</p>"
            ]
        elif report.get("summary_source") == "deterministic_fallback":
            out += [
                "<p class=note>The generated narrative was withheld because it named a table "  # nosemgrep: string-concat-in-list -- intentional multi-line string
                "under an engine that does not serve it; this summary is built from the "
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
            f"<td>{migrated} {plural_noun(migrated, 'table')} "
            f"{plural_verb(migrated, 'migrates', 'migrate')}</td>"
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
                f"The migration moves the {migrated} {plural_noun(migrated, 'table')} assigned to "
                f"{', '.join(esc(x) for x in migr)}; the per-engine costs above reconcile to the "
                "projected total."
            )
        if note_bits:
            out.append("<p class=note>" + " ".join(note_bits) + "</p>")

    risks = filtered_risks(report)
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
            n_resolved = len(resolved_risks(report))
            resolved_txt = f"; {n_resolved} resolved by the assignment" if n_resolved else ""
            out.append(
                f"<p>Overall risk <b>{esc(risk_level)}</b>. {len(risks)} migration "
                f"{plural_noun(len(risks), 'risk')} identified "
                f"({hi} high, {med} medium{resolved_txt}) across {esc(types_txt)}. The full "
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
        "<footer>Engine and query assignments are produced deterministically \u2014 no language "  # nosemgrep: string-concat-in-list -- intentional multi-line string
        "model decides which engine a table or query goes to. The executive summary is written "
        "over already-computed results and cannot change a recommendation. The complete "
        "machine-readable assessment is available as the Assessment Data (JSON) artifact.</footer>",
        _provenance_footer_html(prov),
        "</div></body></html>",
    ]
    return "\n".join(o for o in out if o)


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
        lines.append(f'    {tnode}["{escaping.mermaid_label(tgt)}"]')
        for s in t.get("source_tables") or []:
            if s not in seen_src:
                seen_src[s] = f"S{sid}"
                lines.append(f'    {seen_src[s]}[("{escaping.mermaid_label(s)}")]')
                sid += 1
            lines.append(f"    {seen_src[s]} --> {tnode}")
    lines.append("```")
    return "\n".join(lines)


def _unsupported_pattern_md(u: dict[str, Any]) -> str:
    """One readable Markdown line for a schema-design ``unsupported_patterns`` entry.

    The four engine contracts (``*_model_output.py``) disagree on field names
    -- see ``src.shared.unsupported_pattern`` for the full picture, and for
    why that module (not this one) owns reading them: the same field-reading
    is also needed by ``synthesis_report.build_risk_assessment`` (#210), in a
    package this one must not depend on. Printing ``str(u)`` for whichever
    shape showed up produced a raw Python dict/list repr in the engineering
    report (#204); this reads the shared helpers' output into one sentence
    instead, escaped as Markdown flowing text via ``escaping.md_text``/
    ``escaping.md_code``. ``source_query`` (opensearch's original SQL text)
    is never shown here -- see ``unsupported_pattern_text``.

    Query ids are long hashes; showing every one of them wrecked readability,
    so only the first three are shown with a "+N more" count, each truncated
    to an 8-character prefix (enough to recognise, not to collide visibly).
    """
    ids = unsupported_pattern_ids(u)
    id_bits = ", ".join(f"`{escaping.md_code(i[:8])}`" for i in ids[:3])
    if len(ids) > 3:
        id_bits += f" (+{len(ids) - 3} more)"

    label = unsupported_pattern_label(u)
    head_bits = [f"**{escaping.md_text(label)}**"]
    if id_bits:
        head_bits.append(f"({id_bits})")
    head = " ".join(head_bits)

    body = escaping.md_text(unsupported_pattern_text(u))

    if head and body:
        return f"{head} \u2014 {body}"
    return head or body or "(no detail provided)"


def _migration_note_md(mn: dict[str, Any]) -> str:
    """One readable Markdown bullet for a schema-design ``migration_notes`` entry.

    ``migration_notes`` is a list of dicts (``object_name``, ``object_type``,
    ``source_table`` optional, ``application_logic_required``) -- the same
    four field names on every engine contract, so there is no shape to
    reconcile here the way ``unsupported_patterns`` needs. The report used to
    interpolate the whole list with ``escaping.md_text(notes)``, which prints
    the Python list/dict repr verbatim (e.g.
    ``[{'object_name': 'x', 'object_type': 'trigger', ...}]``); this renders
    one bullet per note instead, tolerant of any of the fields being absent.
    """
    object_type = str(mn.get("object_type") or "").strip()
    object_name = str(mn.get("object_name") or "").strip()
    logic = str(mn.get("application_logic_required") or "").strip()

    head_bits = []
    if object_type:
        head_bits.append(f"**{escaping.md_text(object_type)}**")
    if object_name:
        head_bits.append(escaping.md_text(object_name))
    head = " ".join(head_bits)
    body = escaping.md_text(logic)

    if head and body:
        return f"{head} \u2014 {body}"
    return head or body or "(no detail provided)"


def render_engineering_report_md(report: dict[str, Any], prov: dict[str, str] | None = None) -> str:
    """Build-team-facing document: migration map, per-engine target schemas,
    query groups. Markdown with mermaid fences, which render in the tooling
    engineers open it in (VS Code, GitHub, GitLab).
    """
    db = report.get("database_name", "?")
    out: list[str] = []
    if prov:
        # Front-matter values are quoted so a value containing ``:`` or ``#`` cannot
        # be misread as YAML structure, and the double quotes inside are escaped so
        # the value cannot close its own quoting. ``db`` here is customer-derived.
        out += ["---"]
        out += [f'{k}: "{escaping.md_yaml_value(v)}"' for k, v in prov.items() if v]
        out += ["---", ""]
    out += [
        "# Database Modernization \u2014 Engineering Report",
        "",
        f"Source database: `{escaping.md_code(db)}`. This is the build companion to the "  # nosemgrep: string-concat-in-list -- intentional multi-line string
        "Decision Report: "
        "the source-to-target mapping, the per-engine target schemas, and the query "
        "co-dependency groups.",
        "",
    ]

    mappings = [m for m in (report.get("table_mappings") or []) if isinstance(m, dict)]
    if mappings:
        out += [
            f"## Migration map ({len(mappings)} {plural_noun(len(mappings), 'table')})",
            "",
            "| Source table | Target engine | Target | Pattern | Confidence |",
            "|---|---|---|---|---|",
        ]
        for m in mappings:
            out.append(
                f"| `{escaping.md_code(m.get('source_table', '?'))}` "
                f"| {escaping.md_cell(m.get('recommended_database', '?'))} "
                f"| `{escaping.md_code(m.get('target_table', '-'))}` "
                f"| {escaping.md_cell(m.get('aggregate_pattern', '-'))} "
                f"| {escaping.md_cell(m.get('confidence_score', '-'))} |"
            )
        out.append("")

    designs = _completed_designs(report)
    if designs:
        out += ["## Target schemas by engine", ""]
        for eng, dz in designs.items():
            tables = [t for t in (dz.get("tables") or []) if isinstance(t, dict)]
            n_aps = dz.get("access_pattern_count", 0)
            out += [
                f"### {escaping.md_text(eng)} ({len(tables)} target "
                f"{plural_noun(len(tables), 'object')}, "
                f"{n_aps} access {plural_noun(n_aps, 'pattern')})",
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
                        f"`{escaping.md_code(t.get('table_name', '?'))}`",
                        escaping.md_cell(t.get("aggregate_pattern", "-")),
                        ", ".join(
                            f"`{escaping.md_code(s)}`" for s in (t.get("source_tables") or [])
                        )
                        or "-",
                        escaping.md_cell(t.get("gsi_count", "-")),
                    ]
                    if has_ttl:
                        row.append(escaping.md_cell(t.get("ttl_seconds", "-")))
                    if has_shards:
                        row += [
                            escaping.md_cell(t.get("shards", "-")),
                            escaping.md_cell(t.get("replicas", "-")),
                            escaping.md_cell(t.get("field_count", "-")),
                        ]
                    out.append("| " + " | ".join(row) + " |")
                out.append("")
            unsupported = dz.get("unsupported_patterns") or []
            if isinstance(unsupported, str):
                # Same malformed-shape guard as migration_notes below: a bare
                # string must be one entry, not one character per entry.
                unsupported = [unsupported]
            elif not isinstance(unsupported, list):
                unsupported = []
            if unsupported:
                out += [f"**Unsupported patterns ({len(unsupported)}):**", ""]
                out += [
                    f"- {_unsupported_pattern_md(u) if isinstance(u, dict) else escaping.md_text(u)}"
                    for u in unsupported
                ]
                out.append("")
            notes = dz.get("migration_notes") or []
            if isinstance(notes, str):
                # Contractually a list of dicts; a bare string is a malformed
                # shape that `for mn in notes` would otherwise iterate one
                # character at a time, rather than treat as one note.
                notes = [notes]
            elif not isinstance(notes, list):
                notes = []
            if notes:
                out += ["**Migration notes:**", ""]
                out += [
                    f"- {_migration_note_md(mn) if isinstance(mn, dict) else escaping.md_text(mn)}"
                    for mn in notes
                ]
                out.append("")
            er = _mermaid_er(eng, dz)
            if er:
                out += [er, ""]
            elif tables:
                out += [
                    f"_ER diagram omitted ({len(tables)} target "
                    f"{plural_noun(len(tables), 'object')}); see the table above._",
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
                f"| {escaping.md_cell(g.get('group_name', '?'))} | {escaping.md_cell(engines)} "
                f"| {len(aps) if isinstance(aps, list) else escaping.md_cell(aps or '-')} "
                f"| {len(sqs) if isinstance(sqs, list) else escaping.md_cell(sqs or '-')} "
                f"| {escaping.md_cell(g.get('total_design_rps', '-'))} |"
            )
        out.append("")

    risks = filtered_risks(report)
    if risks:
        by_eng: dict[str, list] = {}
        for r in risks:
            eng, _ = _risk_engine_and_body(r.get("description"))
            by_eng.setdefault(eng, []).append(r)
        out += [f"## Risk register ({len(risks)})", ""]
        for eng, items in by_eng.items():
            out += [f"### {escaping.md_text(eng)}", ""]
            for r in items:
                _, body = _risk_engine_and_body(r.get("description"))
                sev = r.get("severity", "-")
                rtype = str(r.get("risk_type", "")).replace("_", " ").lower()
                rid = r.get("risk_id", "-")
                out.append(
                    f"- **{escaping.md_text(rid)}** \u00b7 {escaping.md_text(sev)} \u00b7 "
                    f"{escaping.md_text(rtype)} \u2014 {escaping.md_text(body)}"
                )
                if r.get("mitigation") and not _repeats(r.get("mitigation"), body):
                    out.append(f"  - Mitigation: {escaping.md_text(r.get('mitigation'))}")
                aff = list(r.get("affected_tables") or [])
                if aff:
                    shown = ", ".join(f"`{escaping.md_code(a)}`" for a in aff[:8])
                    more = f" (+{len(aff) - 8} more)" if len(aff) > 8 else ""
                    out.append(f"  - Affects: {shown}{more}")
            out.append("")

    resolved = resolved_risks(report)
    if resolved:
        # Analysis risks the effective assignment resolved (#221): listed so none
        # disappears without a record.
        out += [f"## Resolved by the assignment ({len(resolved)})", ""]
        for r in resolved:
            _, body = _risk_engine_and_body(r.get("description"))
            engine_name = display_engine(r.get("engine", "-"))
            where = (
                f"{engine_name} \u2192 {display_engine(r['resolved_on'])}"
                if r.get("resolved_on")
                else engine_name
            )
            out.append(
                f"- {escaping.md_text(r.get('severity', '-'))} \u00b7 "
                f"{escaping.md_text(where)} \u2014 {escaping.md_text(body)}"
            )
            if r.get("reason"):
                out.append(f"  - Resolved because {escaping.md_text(r['reason'])}.")
        out.append("")

    tradeoffs = [t for t in (report.get("trade_offs") or []) if isinstance(t, dict)]
    if tradeoffs:
        by_engine: dict[str, list] = {}
        for t in tradeoffs:
            by_engine.setdefault(t.get("engine") or "(general)", []).append(t)
        out += [f"## Migration trade-offs ({len(tradeoffs)})", ""]
        for eng, items in by_engine.items():
            out += [f"### {escaping.md_text(eng)}", ""]
            for t in items:
                desc = str(t.get("description", "")).strip()
                impact = str(t.get("impact", "")).strip()
                line = f"- **{escaping.md_text(desc)}**"
                if impact and impact != desc:  # reality-check items repeat their title
                    line += f" \u2014 {escaping.md_text(impact)}"
                out.append(line)
                aff = list(t.get("source_tables") or [])
                if aff:
                    shown = ", ".join(f"`{escaping.md_code(s)}`" for s in aff[:8])
                    more = f" (+{len(aff) - 8} more)" if len(aff) > 8 else ""
                    out.append(f"  - Affects: {shown}{more}")
            out.append("")

    out += [
        "---",
        "",
        "Assignments are deterministic. The complete machine-readable assessment is the "  # nosemgrep: string-concat-in-list -- intentional multi-line string
        "Assessment Data (JSON) artifact.",
        "",
    ]
    return "\n".join(out)
