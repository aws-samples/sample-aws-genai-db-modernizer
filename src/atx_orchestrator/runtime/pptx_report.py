"""Executive Summary Report — the AWS Transform PPTX deliverable.

Renders ``summary-executive-report.pptx`` from the synthesis report plus the
report export data, using ``assets/template.pptx`` (the AWS Transform deck: its
own theme, aurora layout backgrounds, AWS logo and Transform hexagon). Published
alongside the Decision Report HTML by ``tools._publish_synthesis_deliverables``,
from the same inputs, so the two never disagree.

Three rules this module exists to honour:

1. **No cost comparison.** Cost figures are not used as an argument anywhere:
   no per-engine cost table, no "% of spend", no savings claim. Cost sentences
   are stripped out of the deterministic summary before it is rendered.
2. **Slide 1 mirrors the HTML Decision Report.** It reuses that renderer's own
   ``_architecture_engines`` helper so the deck and the HTML cannot drift.
3. **Deterministic content.** Every string is either a fixed constant or derived
   from the artifacts by rule in ``derive()``. No LLM narrative, no ``now()``, and
   every iteration order is explicitly sorted, so the same job renders the same
   deck on every agent execution. The one clock-derived value a deliverable
   normally carries — the date — is read from the report's own ``timestamp``.

The public entry point is ``render_executive_summary_pptx(report, export_data)``,
which returns bytes; nothing here touches S3 or the platform.
"""

from __future__ import annotations

import io
import logging
import re
from pathlib import Path
from typing import Any

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN

# ``pptx.Presentation`` is a factory function, not the class it returns, so the
# deck type has to be imported separately to annotate anything with it.
from pptx.presentation import Presentation as PresentationPart
from pptx.util import Inches, Pt

# Reused rather than reimplemented: slide 1 must show the same architecture the
# HTML Decision Report shows, and that view is non-trivial (ranking joined with
# recommended_architecture.databases and schema_designs).
from .artifacts import _architecture_engines

logger = logging.getLogger(__name__)

# Fixed, customer-facing deliverable name. Deliberately not built from
# ``artifact_stem``: this file is the executive summary of the assessment and is
# named the same in every job, so it is recognisable in the Artifacts panel and
# across engagements.
FILENAME = "summary-executive-report.pptx"

TEMPLATE = Path(__file__).parent / "assets" / "template.pptx"

# ---------------------------------------------------------------------------
# Design system — every value read out of business_case.pptx, not invented.
# ---------------------------------------------------------------------------
FONT_HEAD = "Amazon Ember Display"
FONT_BODY = "Amazon Ember"

INK = RGBColor(0x23, 0x2F, 0x3E)  # table body fill (AWS Squid Ink)
PAPER = RGBColor(0xF1, 0xF3, 0xF3)  # table text
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
MUTED = RGBColor(0x9B, 0xA7, 0xB6)
TBL_HEAD = RGBColor(0x7C, 0x59, 0xED)  # header fill used on slide 26
BLUE = RGBColor(0x41, 0xB3, 0xFF)  # accent1
PURPLE = RGBColor(0xAD, 0x5C, 0xFF)  # accent2
GREEN = RGBColor(0x00, 0xE5, 0x00)  # accent3
PINK = RGBColor(0xFF, 0x5C, 0x85)  # accent4
ORANGE = RGBColor(0xFF, 0x69, 0x3C)  # accent5
YELLOW = RGBColor(0xFB, 0xD3, 0x32)  # accent6

ENGINE_COLOR = {
    "aurora_postgresql": BLUE,
    "elasticache": PURPLE,
    "documentdb": GREEN,
    "dynamodb": YELLOW,
    "opensearch": ORANGE,
    "aurora_mysql": PINK,
}
ENGINE_LABEL = {
    "aurora_postgresql": "Aurora PostgreSQL",
    "aurora_mysql": "Aurora MySQL",
    "elasticache": "ElastiCache",
    "documentdb": "DocumentDB",
    "dynamodb": "DynamoDB",
    "opensearch": "OpenSearch",
}

# Triage signal -> customer-facing label. Static, so the same signal always
# renders the same words; unknown signals fall back to a prettified name.
SIGNAL_LABEL = {
    "aggregations": "Aggregations (SUM/COUNT/AVG + GROUP BY)",
    "complex_joins": "Complex joins (3+ tables)",
    "eav_pattern": "Entity-attribute-value tables",
    "json_columns": "JSON columns",
    "junction_tables": "Junction tables",
    "key_value_lookups": "Key-value lookups (PK read, ≤5 rows)",
    "leaderboard_pattern": "Leaderboard / top-N (ORDER BY + LIMIT)",
    "low_frequency_reads": "Low-frequency reads (admin, reporting, health checks)",
    "low_frequency_writes": "Low-frequency writes (<5 cps)",
    "metadata_config": "Metadata / config store",
    "range_queries": "Range queries (PK + sort key)",
    "session_store": "Session store",
    "status_filters": "Status filters (WHERE status=?, EXISTS, IS NULL)",
    "subqueries": "Correlated subqueries",
    "text_search": "Full-text search (LIKE, MATCH, tsvector)",
    "time_series": "Time-series / event log",
}
MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
# An engine the assessment is at least this confident in is sequenced before the
# ones it is not. Stated as a constant so the wave split is reproducible.
CONFIDENCE_FLOOR = 50

LAYOUT_HERO = "Default 32"  # aurora full-bleed background + 48pt title
LAYOUT_CONTENT = "Default 5"  # title + subtitle, plain dark background

# Both TITLE and BODY carry idx=0 in this template, so placeholders cannot be
# addressed by idx — only by document order. Date/footer/slide-number copies are
# dropped because the layout already draws its own.
_DROP_PLACEHOLDER_TYPES = (16, 15, 13)  # DATE, FOOTER, SLIDE_NUMBER


# ---------------------------------------------------------------------------
# deck plumbing
# ---------------------------------------------------------------------------
def open_deck(keep: int = 1) -> PresentationPart:
    """Template with all sample slides after the first ``keep`` removed.

    Slide 1 of the template *is* the intro title slide (AWS Transform wordmark,
    "Migration Assessment Business Case", date, and the hexagon logo that comes
    from the layout), so it is retained verbatim rather than rebuilt — only its
    date is re-derived. New slides append after it, which is the order we want.
    """
    prs = Presentation(str(TEMPLATE))
    lst = prs.slides._sldIdLst
    rels = prs.part.rels
    for sid in list(lst)[keep:]:
        rid = sid.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
        lst.remove(sid)
        if rid in rels:
            rels.pop(rid)
    return prs


INTRO_SUBTITLE = "Database Modernization Assessment"


def retitle_intro(slide, date_text: str) -> None:
    """Retitle the retained intro slide in place.

    Two runs are rewritten: the template's sample subtitle and its hardcoded
    month. Everything else on the slide — the AWS logo, the AWS Transform
    wordmark and hexagon, the copyright line — is left exactly as authored.
    """
    for shape in slide.shapes:
        if not shape.has_text_frame:
            continue
        for p in shape.text_frame.paragraphs:
            for r in p.runs:
                if re.fullmatch(r"[A-Z][a-z]+ \d{4}", r.text.strip()):
                    r.text = date_text
                elif r.text.strip() == "Migration Assessment Business Case":
                    r.text = INTRO_SUBTITLE


def add_slide(prs: PresentationPart, layout_name: str):
    layout = next(lay for lay in prs.slide_masters[0].slide_layouts if lay.name == layout_name)
    slide = prs.slides.add_slide(layout)
    for shp in list(slide.placeholders):
        if int(shp.placeholder_format.type) in _DROP_PLACEHOLDER_TYPES:
            shp._element.getparent().remove(shp._element)
    return slide


def phs(slide) -> list:
    """Placeholders in document order — 0 is the title, 1 the subtitle/body."""
    return list(slide.placeholders)


def drop_ph(slide, i: int) -> None:
    shp = phs(slide)[i]
    shp._element.getparent().remove(shp._element)


def _style(run, size, *, bold=False, color=WHITE, font=FONT_BODY):
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.color.rgb = color
    run.font.name = font


# Consistent vertical band for every content slide, in inches.
TITLE_BOX = (0.67, 0.55, 11.50, 0.72)
SUB_BOX = (0.67, 1.29, 11.50, 0.42)
BODY_TOP = 1.92


def _place(shape, box) -> None:
    shape.left, shape.top, shape.width, shape.height = (Inches(v) for v in box)


def clip(text: str, n: int) -> str:
    """Truncate on a word boundary — contract descriptions are long and cutting
    mid-word ("...DocumentDB versi") reads like a rendering bug on a slide."""
    text = " ".join(text.split())
    if len(text) <= n:
        return text
    cut = text[:n].rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:.") + "…"


# Approximate advance widths in em for a bold sans headline face, used to keep
# titles on one line. A character count is not enough: two headlines 6
# characters apart can differ by 1.5in of rendered width.
_EM_NARROW = frozenset("iijlItfr.,:;'|!()[]-·")
_EM_WIDE = frozenset("mMW@%")
_TITLE_MAX_IN = 11.15  # TITLE_BOX width less the text-frame's own insets


def _text_em(text: str) -> float:
    total = 0.0
    for ch in text:
        if ch == " ":
            total += 0.28
        elif ch in _EM_NARROW:
            total += 0.31
        elif ch in _EM_WIDE:
            total += 0.86
        elif ch.isdigit():
            total += 0.57
        elif ch.isupper():
            total += 0.72
        else:
            total += 0.57
    return total


def title_size(text: str) -> float:
    """Largest step size that keeps the headline on one line inside TITLE_BOX.

    PowerPoint's autofit is not applied to programmatically set text, so a
    headline that overflows silently wraps down over the subtitle band.
    """
    em = max(_text_em(line) for line in text.split("\n"))
    for size in (40.0, 36.0, 32.0, 29.0, 26.0):
        if em * size / 72 <= _TITLE_MAX_IN:
            return size
    return 24.0


def set_title(slide, text: str, *, size=None, box=TITLE_BOX):
    """Geometry is set explicitly: TITLE and BODY both carry ``idx=0`` in this
    template, so both inherit the *same* layout placeholder position and would
    otherwise render stacked on top of each other."""
    size = title_size(text) if size is None else size
    ph = phs(slide)[0]
    _place(ph, box)
    ph.text_frame.word_wrap = True
    ph.text_frame.text = text
    for p in ph.text_frame.paragraphs:
        for r in p.runs:
            _style(r, size, bold=True, font=FONT_HEAD)
    return ph


def set_subtitle(slide, text: str, *, size=16.0, color=BLUE, box=SUB_BOX):
    ph = phs(slide)[1]
    _place(ph, box)
    ph.text_frame.word_wrap = True
    ph.text_frame.text = text
    _style(ph.text_frame.paragraphs[0].runs[0], size, bold=False, color=color)
    return ph


def textbox(slide, x, t, w, h, *, anchor=MSO_ANCHOR.TOP):
    box = slide.shapes.add_textbox(Inches(x), Inches(t), Inches(w), Inches(h))
    tf = box.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    return tf


def para(
    tf,
    text,
    *,
    size=12.0,
    bold=False,
    color=WHITE,
    first=False,
    space_after=4,
    font=FONT_BODY,
    align=PP_ALIGN.LEFT,
):
    p = tf.paragraphs[0] if first else tf.add_paragraph()
    p.alignment = align
    p.space_after = Pt(space_after)
    r = p.add_run()
    r.text = text
    _style(r, size, bold=bold, color=color, font=font)
    return p


def bar(slide, x, t, w, h, color):
    shp = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(x), Inches(t), Inches(w), Inches(h))
    shp.fill.solid()
    shp.fill.fore_color.rgb = color
    shp.line.fill.background()
    shp.shadow.inherit = False
    return shp


def card(slide, x, t, w, h, accent):
    """Dark tile with a coloured left rule — the template's content-card idiom."""
    box = slide.shapes.add_shape(
        MSO_SHAPE.ROUNDED_RECTANGLE, Inches(x), Inches(t), Inches(w), Inches(h)
    )
    box.fill.solid()
    box.fill.fore_color.rgb = INK
    box.line.fill.background()
    box.shadow.inherit = False
    box.adjustments[0] = 0.06
    rule = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE, Inches(x), Inches(t), Inches(0.05), Inches(h)
    )
    rule.fill.solid()
    rule.fill.fore_color.rgb = accent
    rule.line.fill.background()
    rule.shadow.inherit = False
    return box


def table(
    slide,
    x,
    t,
    w,
    rows,
    *,
    col_w,
    head_fill=TBL_HEAD,
    head_size=11.0,
    body_size=10.0,
    row_h=0.30,
    head_h=0.34,
    emphasis=None,
):
    """rows[0] is the header. ``emphasis`` = {(row, col): RGBColor} for bold cells."""
    n_rows, n_cols = len(rows), len(rows[0])
    h = head_h + row_h * (n_rows - 1)
    gf = slide.shapes.add_table(n_rows, n_cols, Inches(x), Inches(t), Inches(w), Inches(h))
    tbl = gf.table
    tbl.first_row = False
    tbl.horz_banding = False
    for i, cw in enumerate(col_w):
        tbl.columns[i].width = Inches(cw)
    tbl.rows[0].height = Inches(head_h)
    for r in range(1, n_rows):
        tbl.rows[r].height = Inches(row_h)
    for r, row in enumerate(rows):
        for c, val in enumerate(row):
            cell = tbl.cell(r, c)
            cell.fill.solid()
            cell.fill.fore_color.rgb = head_fill if r == 0 else INK
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            cell.margin_left = Inches(0.06)
            cell.margin_right = Inches(0.06)
            cell.margin_top = Inches(0.02)
            cell.margin_bottom = Inches(0.02)
            tf = cell.text_frame
            tf.word_wrap = True
            p = tf.paragraphs[0]
            p.alignment = (
                PP_ALIGN.RIGHT
                if (c and isinstance(val, str) and val[:1].isdigit())
                else PP_ALIGN.LEFT
            )
            run = p.add_run()
            run.text = str(val)
            colour = (emphasis or {}).get((r, c), PAPER)
            _style(run, head_size if r == 0 else body_size, bold=(r == 0), color=colour)
    return tbl


def footer_note(slide, text, *, t=6.55, color=MUTED, size=9.0):
    tf = textbox(slide, 0.55, t, 11.9, 0.5)
    para(tf, text, size=size, color=color, first=True)


# ---------------------------------------------------------------------------
# deterministic content layer
#
# Everything the slides say is produced here, from the artifacts, by rule. No
# language model, no wall-clock, and every iteration order explicitly sorted so
# repeated agent executions over the same job produce identical content.
# ---------------------------------------------------------------------------
_COST_WORDS = re.compile(r"\$|\bcost\b|\bspend\b|\bpricing\b|\bsavings?\b|/month\b", re.I)


def strip_cost(text: str) -> str:
    """Drop whole sentences that talk about money.

    The deck must not argue from cost, but ``summary_deterministic`` mixes a cost
    sentence in with the workload facts. Removing the sentence rather than the
    figure keeps the remaining prose grammatical.
    """
    kept = [s for s in re.split(r"(?<=\.)\s+", text.strip()) if s and not _COST_WORDS.search(s)]
    return " ".join(kept)


def prettify_engines(text: str) -> str:
    """``aurora_postgresql`` -> ``Aurora PostgreSQL`` in generated prose.

    Longest key first so ``aurora_postgresql`` is not partially matched.
    """
    for raw in sorted(ENGINE_LABEL, key=len, reverse=True):
        text = text.replace(raw, ENGINE_LABEL[raw])
    return text


def signal_label(name: str) -> str:
    return SIGNAL_LABEL.get(name, name.replace("_", " ").capitalize())


def short_label(name: str) -> str:
    """Label without its parenthetical, for use inside a sentence."""
    return signal_label(name).split(" (")[0].lower()


def risk_engine(description: str) -> str:
    """Risks carry their engine as a ``[engine]`` prefix on the description."""
    m = re.match(r"\[([^\]]+)\]", description or "")
    return m.group(1) if m else ""


def clean_risk_text(description: str) -> tuple[str, str]:
    """Return (prose, query_count) for a risk description.

    Risks carry ``affected_tables`` but no query count; the count only exists
    inside the description as "(0% of queries resolved by schema design, N
    remaining)", so it is lifted out and the parenthetical removed.
    """
    desc = re.sub(r"^\[[^\]]+\]\s*", "", str(description or "")).strip()
    if desc.lower().startswith("unknown:"):
        desc = desc[len("unknown:") :].strip()
    m = re.search(r"\((?:[^()]*?)(\d+)\s+remaining\)", desc)
    n_q = m.group(1) if m else ""
    if m:
        desc = (desc[: m.start()] + desc[m.end() :]).strip()
    return " ".join(desc.split()), n_q


def derive(rep: dict[str, Any], exp: dict[str, Any]) -> dict[str, Any]:
    """All deck content, derived from the two artifacts."""
    arch = rep.get("recommended_architecture") or {}
    risk = rep.get("risk_assessment") or {}
    assignment = rep.get("assignment_summary") or {}

    # ---- intro date: the report's own timestamp, never the wall clock --------
    stamp = str(rep.get("timestamp") or "")
    m = re.match(r"(\d{4})-(\d{2})", stamp)
    date_text = f"{MONTHS[int(m.group(2)) - 1]} {m.group(1)}" if m else ""

    # ---- architecture, exactly as the HTML Decision Report computes it -------
    engines = _architecture_engines(rep)
    migrated = 0
    for e in engines:
        if e["role"] == "Migration target":
            digits = "".join(ch for ch in str(e["scope"]) if ch.isdigit())
            if digits:
                migrated += int(digits)

    ranking = [r for r in (rep.get("ranking") or []) if isinstance(r, dict)]
    conf = {r.get("target"): float(r.get("confidence_score") or 0) for r in ranking}
    workload = {r.get("target"): float(r.get("workload_percent") or 0) for r in ranking}
    by_workload = sorted(
        ranking, key=lambda r: (-(r.get("workload_percent") or 0), str(r.get("target")))
    )

    # ---- risks --------------------------------------------------------------
    risks = sorted(
        (r for r in (risk.get("risks") or []) if isinstance(r, dict)),
        key=lambda r: str(r.get("risk_id")),
    )
    sev: dict[str, int] = {}
    for r in risks:
        k = str(r.get("severity", "")).upper()
        sev[k] = sev.get(k, 0) + 1
    high = [r for r in risks if str(r.get("severity", "")).upper() == "HIGH"]
    high_by_engine: dict[str, int] = {}
    for r in high:
        eng = risk_engine(str(r.get("description") or ""))
        if eng:
            high_by_engine[eng] = high_by_engine.get(eng, 0) + 1
    by_type: dict[str, int] = {}
    for r in risks:
        t = str(r.get("risk_type") or "other").replace("_", " ").lower()
        by_type[t] = by_type.get(t, 0) + 1
    # "all the same root cause" is only claimed when the HIGH risks really do
    # share one risk_type.
    high_types = sorted({str(r.get("risk_type") or "") for r in high})
    one_root_cause = len(high_types) == 1 and bool(high)

    # ---- workload shape -----------------------------------------------------
    ops: dict[str, int] = {}
    for row in exp.get("flowAggregate") or []:
        qt = str(row.get("query_type") or "OTHER").upper()
        ops[qt] = ops.get(qt, 0) + int(row.get("count") or 0)
    ops_sorted = sorted(ops.items(), key=lambda kv: (-kv[1], kv[0]))
    n_ops = sum(ops.values())

    patterns = ((exp.get("collector") or {}).get("queries") or {}).get("query_patterns") or []
    n_patterns = len(patterns)
    cps = sorted((float(p.get("calls_per_second") or 0.0) for p in patterns), reverse=True)
    total_cps = sum(cps)
    read_cps = sum(
        float(p.get("calls_per_second") or 0.0)
        for p in patterns
        if str(p.get("query_type", "")).upper() == "SELECT"
    )
    fan = [len(p.get("tables_accessed") or []) for p in patterns]
    fan1 = sum(1 for n in fan if n <= 1)
    fan3 = sum(1 for n in fan if n >= 3)
    max_fan = max(fan) if fan else 0
    n_max_fan = sum(1 for n in fan if n == max_fan) if fan else 0

    # ---- triage signals: the deterministic pattern mix -----------------------
    signals = [
        s
        for s in ((exp.get("results") or {}).get("triage_summary") or {}).get("signals") or []
        if isinstance(s, dict)
    ]
    q_signals: list[dict[str, Any]] = sorted(
        (
            {"name": str(s.get("signal")), "count": int(s.get("query_count") or 0)}
            for s in signals
            if int(s.get("query_count") or 0) > 0
        ),
        key=lambda s: (-s["count"], s["name"]),
    )
    for s in q_signals:
        s["share"] = (s["count"] / n_patterns * 100) if n_patterns else 0.0
        s["label"] = signal_label(s["name"])

    # ---- decisions, by rule -------------------------------------------------
    # 1. the engine the assessment is least sure of; 2. the engine carrying the
    # most HIGH risks; 3. the engines that need no data migration at all.
    decisions: list[dict[str, Any]] = []
    ranked_conf = sorted(
        ranking, key=lambda r: (float(r.get("confidence_score") or 0), str(r.get("target")))
    )
    weakest = ranked_conf[0] if ranked_conf else None
    if weakest and float(weakest.get("confidence_score") or 0) < CONFIDENCE_FLOOR:
        eng = str(weakest.get("target") or "")
        thin = [
            s
            for s in q_signals
            if eng
            in (next((x.get("targets") or [] for x in signals if x.get("signal") == s["name"]), []))
        ]
        thinnest = min(thin, key=lambda s: (s["count"], s["name"])) if thin else None
        evidence = (
            f"{thinnest['count']} {short_label(thinnest['name'])} queries in the whole " f"workload"
            if thinnest
            else "limited supporting evidence"
        )
        decisions.append(
            {
                "question": f"Confirm {ENGINE_LABEL.get(eng, eng)}?",
                "badge": f"{conf.get(eng, 0):.0f}% confidence",
                "accent": ENGINE_COLOR.get(eng, ORANGE),
                "against": (
                    f"Lowest confidence of the {len(ranking)} engines · {evidence} · "
                    f"{workload.get(eng, 0):.1f}% of workload"
                ),
                "action": "Requirement validation pending.",
            }
        )
    if high_by_engine:
        eng = sorted(high_by_engine.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        decisions.append(
            {
                "question": f"Scope {ENGINE_LABEL.get(eng, eng)}?",
                "badge": f"{high_by_engine[eng]} of {len(high)} HIGH risks",
                "accent": ENGINE_COLOR.get(eng, GREEN),
                "against": (
                    f"Carries the most HIGH risks · {conf.get(eng, 0):.0f}% confidence · "
                    f"{workload.get(eng, 0):.1f}% of workload"
                ),
                "action": "Scope defined as the matching table subset.",
            }
        )
    no_move = [e for e in engines if e["role"] in ("Retained", "Cache layer")]
    if no_move:
        names = " + ".join(ENGINE_LABEL.get(e["engine"], e["engine"]) for e in no_move)
        pct = sum(workload.get(e["engine"], 0) for e in no_move)
        decisions.append(
            {
                "question": "Start with the no-migration step?",
                "badge": f"{pct:.0f}% of workload",
                "accent": BLUE,
                "against": (
                    f"{names} · no data migration · "
                    f"{sum(high_by_engine.get(e['engine'], 0) for e in no_move)} HIGH risks"
                ),
                "action": "Source database remains authoritative; step is reversible.",
            }
        )

    # ---- waves, by rule -----------------------------------------------------
    # Wave 1 is whatever needs no data migration. The migration targets then
    # split on CONFIDENCE_FLOOR: the ones the assessment is confident in go
    # before the ones it is not.
    targets = sorted(
        (e for e in engines if e["role"] == "Migration target"),
        key=lambda e: (-conf.get(e["engine"], 0), e["engine"]),
    )
    waves: list[dict[str, Any]] = []
    if no_move:
        waves.append(
            {
                "engines": no_move,
                "accent": BLUE,
                "note": (
                    "No data migration — source-compatible or additive, so the source database "
                    "stays authoritative and the step is reversible."
                ),
            }
        )
    confident = [e for e in targets if conf.get(e["engine"], 0) >= CONFIDENCE_FLOOR]
    unsure = [e for e in targets if conf.get(e["engine"], 0) < CONFIDENCE_FLOOR]
    for group, accent in ((confident, YELLOW), (unsure, ORANGE)):
        if not group:
            continue
        lo = min(conf.get(e["engine"], 0) for e in group)
        n_t = sum(int("".join(ch for ch in str(e["scope"]) if ch.isdigit()) or 0) for e in group)
        note = (
            f"{n_t} source tables migrate · confidence from {lo:.0f}%"
            if group is confident
            else f"{n_t} source tables · confidence {lo:.0f}% — re-scope after the gate"
        )
        waves.append({"engines": group, "accent": accent, "note": note})
    for w in waves:
        w["names"] = " + ".join(ENGINE_LABEL.get(e["engine"], e["engine"]) for e in w["engines"])
        w["workload"] = sum(workload.get(e["engine"], 0) for e in w["engines"])
        w["high"] = sum(high_by_engine.get(e["engine"], 0) for e in w["engines"])

    top = q_signals[0] if q_signals else None
    second = q_signals[1] if len(q_signals) > 1 else None
    return {
        "date_text": date_text,
        "database": rep.get("database_name"),
        "job_id": rep.get("job_id"),
        "timestamp": stamp,
        "arch_type": str(arch.get("architecture_type") or "not determined").replace("_", " "),
        "risk_level": str(risk.get("overall_risk_level") or "not assessed"),
        "engines": engines,
        "migrated": migrated,
        "ranking": by_workload,
        "conf": conf,
        "workload": workload,
        "summary": prettify_engines(strip_cost(str(rep.get("summary_deterministic") or ""))),
        "n_tables": len(rep.get("table_mappings") or []),
        "n_tradeoffs": len(rep.get("trade_offs") or []),
        "assignment": assignment,
        "risks": risks,
        "sev": sev,
        "high": high,
        "high_by_engine": high_by_engine,
        "by_type": sorted(by_type.items(), key=lambda kv: (-kv[1], kv[0])),
        "one_root_cause": one_root_cause,
        "high_type": high_types[0].replace("_", " ").lower() if one_root_cause else "",
        "mitigations": [str(s) for s in (risk.get("mitigation_strategies") or []) if s],
        "ops": ops_sorted,
        "n_ops": n_ops,
        "n_patterns": n_patterns,
        "total_cps": total_cps,
        "read_share": (read_cps / total_cps * 100) if total_cps else 0.0,
        "fan1": fan1,
        "fan3": fan3,
        "max_fan": max_fan,
        "n_max_fan": n_max_fan,
        "busiest": cps[0] if cps else 0.0,
        "top10_share": (sum(cps[:10]) / total_cps * 100) if total_cps else 0.0,
        "signals": q_signals,
        "top_signal": top,
        "second_signal": second,
        "decisions": decisions,
        "waves": waves,
    }


# ---------------------------------------------------------------------------
# slides
# ---------------------------------------------------------------------------
def _risk_accent(level: str):
    return {"HIGH": PINK, "CRITICAL": PINK, "MEDIUM": YELLOW, "LOW": GREEN}.get(
        level.upper(), MUTED
    )


def slide_summary(prs, f):
    """Mirrors the HTML Decision Report: hero line, metric tiles, the
    deterministic executive summary, and the recommended-architecture table.
    The HTML's cost tile and 'Est. monthly' column are deliberately absent."""
    s = add_slide(prs, LAYOUT_HERO)
    drop_ph(s, 1)
    # Not "Database Modernization — Assessment Summary": the intro slide already
    # carries the deck title, so repeating it here just costs a line.
    set_title(s, "Assessment Summary", size=32.0, box=(0.67, 0.58, 11.5, 0.66))
    tf = textbox(s, 0.70, 1.28, 11.4, 0.4)
    para(
        tf,
        f"Source database {f['database']}  ·  Architecture {f['arch_type']}  ·  "
        f"Overall risk {f['risk_level']}",
        size=14.5,
        color=BLUE,
        first=True,
    )

    tiles = [
        (f"{f['n_patterns']:,}", "query patterns analyzed", BLUE),
        (f"{len(f['engines'])}", "target engines", PURPLE),
        (f"{f['migrated']}", "source tables migrate", GREEN),
        (f["risk_level"], f"overall risk, {len(f['risks'])} items", _risk_accent(f["risk_level"])),
    ]
    for i, (big, small, accent) in enumerate(tiles):
        x = 0.67 + i * 2.62
        card(s, x, 1.85, 2.42, 1.15, accent)
        tf = textbox(s, x + 0.20, 1.96, 2.10, 0.95)
        para(tf, big, size=26.0, bold=True, color=accent, first=True, space_after=1, font=FONT_HEAD)
        para(tf, small, size=9.5, color=PAPER)

    card(s, 0.67, 3.20, 5.75, 2.62, PURPLE)
    tf = textbox(s, 0.90, 3.32, 5.30, 2.45)
    para(tf, "EXECUTIVE SUMMARY", size=9.5, bold=True, color=PURPLE, first=True, space_after=5)
    para(tf, f["summary"], size=10.5, color=PAPER)

    rows = [("Engine", "Role", "Workload", "Scope")]
    emph: dict[tuple[int, int], Any] = {}
    for i, e in enumerate(f["engines"], start=1):
        wl = e.get("workload")
        rows.append(
            (
                ENGINE_LABEL.get(e["engine"], e["engine"]),
                e["role"],
                f"{wl:.1f}%" if isinstance(wl, (int, float)) else "—",
                str(e["scope"]),
            )
        )
        emph[(i, 0)] = ENGINE_COLOR.get(e["engine"], PAPER)
    table(
        s,
        6.60,
        3.20,
        5.50,
        rows,
        col_w=[1.55, 1.45, 0.90, 1.60],
        head_size=10.0,
        body_size=9.5,
        row_h=0.34,
        head_h=0.32,
        emphasis=emph,
    )
    kept = [e for e in f["engines"] if e["role"] != "Migration target"]
    kept_pct = sum(e.get("workload") or 0 for e in kept)
    footer_note(
        s,
        f"{f['migrated']} source tables move to a purpose-built engine; "
        + (
            f"{' and '.join(ENGINE_LABEL.get(e['engine'], e['engine']) for e in kept)} "
            f"keep {kept_pct:.1f}% of the workload with no data migration. "
            if kept
            else ""
        )
        + "Full detail in the Decision and Engineering reports.",
        t=5.92,
        color=MUTED,
        size=9.5,
    )
    return s


def slide_evidence(prs, f):
    s = add_slide(prs, LAYOUT_CONTENT)
    a = f["assignment"]
    set_title(s, "Workload Coverage and Engine Assignment")
    set_subtitle(
        s,
        f"{a.get('query_count', 0):,} production query patterns analyzed, "
        f"{a.get('in_scope_count', 0):,} in scope",
    )

    left, width, top = 0.67, 9.4, BODY_TOP + 0.08
    total_pct = sum(f["workload"].values()) or 100.0
    x = left
    for r in f["ranking"]:
        eng = r.get("target")
        w = width * (f["workload"].get(eng, 0) / total_pct)
        bar(s, x, top, w, 0.52, ENGINE_COLOR.get(eng, BLUE))
        x += w
    for i, r in enumerate(f["ranking"]):
        eng = r.get("target")
        y = 2.78 + i * 0.42
        bar(s, left, y + 0.05, 0.16, 0.16, ENGINE_COLOR.get(eng, BLUE))
        tf = textbox(s, left + 0.28, y, 5.6, 0.34)
        para(
            tf,
            f"{ENGINE_LABEL.get(eng, eng)}   {f['workload'].get(eng, 0):.1f}%   ·   "
            f"{r.get('assigned_queries', 0):,} queries   ·   "
            f"{f['conf'].get(eng, 0):.0f}% confidence",
            size=11.5,
            first=True,
        )

    card(s, 6.60, 2.72, 5.5, 2.10, BLUE)
    tf = textbox(s, 6.85, 2.86, 5.05, 1.85)
    para(tf, "Basis of the analysis", size=11.0, bold=True, color=BLUE, first=True)
    for line in (
        f"{a.get('in_scope_count', 0):,} of {a.get('query_count', 0):,} query patterns in scope",
        f"{f['n_tables']} tables mapped to a named target, each with its own confidence score",
        f"{a.get('co_dependency_groups', 0)} co-dependency groups — table sets that must "
        "move together",
    ):
        para(tf, "•  " + line, size=11.5, color=PAPER)

    card(s, 0.67, 5.15, 11.43, 0.95, PURPLE)
    tf = textbox(s, 0.90, 5.28, 11.0, 0.75)
    para(
        tf,
        "Pattern detection, scoring and assignment run with no language model. This deck is "
        "rendered from the stored results of that run.",
        size=12.0,
        color=WHITE,
        first=True,
    )
    return s


def slide_workload(prs, f):
    s = add_slide(prs, LAYOUT_CONTENT)
    top = f["top_signal"]
    set_title(s, "Query Workload Composition")
    if not f["n_patterns"]:
        # No export data: the pattern cache is the only source for this slide.
        # State the gap rather than dropping the slide or dividing by zero — the
        # other five slides come from the report and are unaffected.
        set_subtitle(s, "Query pattern data unavailable for this job")
        card(s, 0.67, BODY_TOP, 11.43, 1.05, MUTED)
        tf = textbox(s, 0.90, BODY_TOP + 0.12, 11.0, 0.9)
        para(
            tf,
            "The collector query patterns and triage signals this slide is built from "
            "could not be read. Engine assignment, risk and sequencing are unaffected — "
            "they come from the synthesis report.",
            size=12.0,
            color=PAPER,
            first=True,
        )
        return s
    reads = dict(f["ops"]).get("SELECT", 0)
    n = f["n_ops"] or 1
    # The dominant pattern leads the subtitle now that the title is a fixed label.
    bits = [f"{top['share']:.0f}% {short_label(top['name'])}"] if top else []
    bits += [
        f"{reads / n * 100:.0f}% reads",
        f"{f['fan1'] / f['n_patterns'] * 100:.0f}% touch a single table",
    ]
    set_subtitle(s, "  ·  ".join(bits))

    tf = textbox(s, 0.67, BODY_TOP, 4.6, 0.3)
    para(tf, f"OPERATION MIX  ({n:,} patterns)", size=9.5, bold=True, color=MUTED, first=True)
    op_colors = {"SELECT": BLUE, "UPDATE": PURPLE, "DELETE": PINK, "OTHER": MUTED, "INSERT": GREEN}
    biggest = f["ops"][0][1] if f["ops"] else 1
    for i, (op, cnt) in enumerate(f["ops"][:5]):
        y = BODY_TOP + 0.38 + i * 0.40
        tf = textbox(s, 0.67, y - 0.04, 0.85, 0.3)
        para(tf, op, size=10.5, bold=True, color=PAPER, first=True)
        bar(s, 1.52, y, max(3.05 * cnt / biggest, 0.03), 0.20, op_colors.get(op, BLUE))
        tf = textbox(s, 4.62, y - 0.05, 1.5, 0.3)
        para(tf, f"{cnt:,}  ·  {cnt / n * 100:.1f}%", size=10.0, color=MUTED, first=True)

    tf = textbox(s, 6.45, BODY_TOP, 5.6, 0.3)
    para(tf, "WHAT THOSE QUERIES ARE DOING", size=9.5, bold=True, color=MUTED, first=True)
    shown = f["signals"][:9]
    rows = [("Pattern", "Queries", "Share")]
    rows += [(clip(sg["label"], 52), f"{sg['count']:,}", f"{sg['share']:.1f}%") for sg in shown]
    # The thinnest evidence in the table is the one worth looking at, so it is
    # the row that gets colour.
    emph = {}
    if shown:
        thin = min(range(len(shown)), key=lambda i: (shown[i]["count"], shown[i]["name"]))
        emph = {(thin + 1, c): YELLOW for c in range(3)}
    # 0.28 head + 9 x 0.235 = 2.395, ending at 4.72 — clear of the cards at 4.85.
    table(
        s,
        6.45,
        BODY_TOP + 0.33,
        5.65,
        rows,
        col_w=[3.65, 1.05, 0.95],
        head_size=10.0,
        body_size=9.5,
        row_h=0.235,
        head_h=0.28,
        emphasis=emph,
    )

    card(s, 0.67, 4.85, 5.35, 1.22, GREEN)
    tf = textbox(s, 0.90, 4.94, 5.0, 1.1)
    para(tf, "Table fan-out", size=10.5, bold=True, color=GREEN, first=True)
    para(
        tf,
        f"{f['fan1']:,} queries touch a single table "
        f"({f['fan1'] / f['n_patterns'] * 100:.1f}%). Only {f['fan3']:,} touch three or more "
        f"({f['fan3'] / f['n_patterns'] * 100:.1f}%), and just {f['n_max_fan']} reach "
        f"{f['max_fan']}.",
        size=11.0,
        color=PAPER,
    )

    card(s, 6.45, 4.85, 5.65, 1.22, PURPLE)
    tf = textbox(s, 6.68, 4.94, 5.3, 1.1)
    para(tf, "Throughput distribution", size=10.5, bold=True, color=PURPLE, first=True)
    para(
        tf,
        f"{f['total_cps']:.1f} queries/sec across the whole database, "
        f"{f['read_share']:.1f}% reads. The top 10 patterns carry "
        f"{f['top10_share']:.1f}% of throughput; the busiest single query runs at "
        f"{f['busiest']:.1f} rps.",
        size=11.0,
        color=PAPER,
    )
    return s


def slide_decisions(prs, f):
    s = add_slide(prs, LAYOUT_CONTENT)
    d = f["decisions"]
    set_title(s, "Engine Confidence and Open Decisions")
    weak = min(f["conf"].items(), key=lambda kv: (kv[1], kv[0])) if f["conf"] else None
    set_subtitle(
        s,
        (
            f"{len(f['ranking'])} recommended engines  ·  lowest confidence "
            f"{ENGINE_LABEL.get(weak[0], weak[0])} at {weak[1]:.0f}%"
            if weak
            else f"{len(f['ranking'])} recommended engines"
        ),
    )

    rows = [("Engine", "Workload", "Confidence", "HIGH risks", "Role")]
    emph: dict[tuple[int, int], Any] = {}
    for i, r in enumerate(f["ranking"], start=1):
        eng = r.get("target")
        role = next((e["role"] for e in f["engines"] if e["engine"] == eng), "—")
        c = f["conf"].get(eng, 0)
        rows.append(
            (
                ENGINE_LABEL.get(eng, eng),
                f"{f['workload'].get(eng, 0):.1f}%",
                f"{c:.0f}%",
                str(f["high_by_engine"].get(eng, 0)),
                role,
            )
        )
        emph[(i, 0)] = ENGINE_COLOR.get(eng, PAPER)
        if c < CONFIDENCE_FLOOR:
            emph[(i, 2)] = ORANGE
        if f["high_by_engine"].get(eng, 0):
            emph[(i, 3)] = PINK
    table(
        s,
        0.67,
        BODY_TOP,
        5.55,
        rows,
        col_w=[1.50, 0.85, 1.00, 0.90, 1.30],
        head_size=9.5,
        body_size=9.5,
        row_h=0.30,
        head_h=0.34,
        emphasis=emph,
    )

    for i, dec in enumerate(d):
        y = BODY_TOP + i * 1.30
        accent = dec["accent"]
        card(s, 6.45, y, 5.65, 1.18, accent)
        tf = textbox(s, 6.68, y + 0.08, 5.25, 1.05)
        p = tf.paragraphs[0]
        p.space_after = Pt(2)
        r1 = p.add_run()
        r1.text = dec["question"] + "   "
        _style(r1, 12.0, bold=True, color=WHITE)
        r2 = p.add_run()
        r2.text = dec["badge"]
        _style(r2, 12.0, bold=True, color=accent)
        para(tf, dec["against"], size=9.0, color=MUTED, space_after=2)
        para(tf, dec["action"], size=10.0, bold=True, color=accent)

    tf = textbox(s, 0.67, 4.05, 5.55, 2.00)
    para(
        tf,
        f"Confidence is the assessment's own measure of evidence strength, not a forecast. "
        f"Anything under {CONFIDENCE_FLOOR}% is sequenced last so it can be re-scoped once "
        f"the earlier waves have produced real measurements.",
        size=11.0,
        color=WHITE,
        first=True,
        space_after=8,
    )
    if f["high_by_engine"]:
        worst = sorted(f["high_by_engine"].items(), key=lambda kv: (-kv[1], kv[0]))[0]
        para(
            tf,
            f"{worst[1]} of the {len(f['high'])} HIGH risks sit on "
            f"{ENGINE_LABEL.get(worst[0], worst[0])} alone.",
            size=11.0,
            bold=True,
            color=PINK,
        )
    return s


def slide_risk(prs, f):
    s = add_slide(prs, LAYOUT_CONTENT)
    n_high = f["sev"].get("HIGH", 0)
    set_title(s, "Risk Profile")
    mix = ", ".join(
        f"{f['sev'][k]} {k}" for k in ("CRITICAL", "HIGH", "MEDIUM", "LOW") if f["sev"].get(k)
    )
    sub = f"{len(f['risks'])} risks: {mix}"
    if f["one_root_cause"]:
        sub += f"  ·  all {n_high} HIGH risks are {f['high_type']}"
    set_subtitle(s, sub)

    present = [
        (k, c) for k, c in (("HIGH", PINK), ("MEDIUM", YELLOW), ("LOW", BLUE)) if f["sev"].get(k)
    ]
    top_n = max((f["sev"].get(k, 0) for k, _ in present), default=1) or 1
    for i, (k, colour) in enumerate(present):
        cnt = f["sev"][k]
        y = BODY_TOP + 0.10 + i * 0.50
        tf = textbox(s, 0.67, y - 0.04, 1.05, 0.32)
        para(tf, k, size=11.0, bold=True, color=colour, first=True)
        bar(s, 1.75, y, max(2.9 * cnt / top_n, 0.05), 0.24, colour)
        tf = textbox(s, 4.78, y - 0.05, 0.9, 0.32)
        para(tf, str(cnt), size=12.0, bold=True, color=PAPER, first=True)

    card(s, 0.67, 3.30, 5.05, 1.05, MUTED)
    tf = textbox(s, 0.90, 3.41, 4.7, 0.9)
    para(tf, "BY TYPE", size=9.5, bold=True, color=MUTED, first=True)
    para(tf, "  ·  ".join(f"{c} {t}" for t, c in f["by_type"][:3]), size=11.5, color=PAPER)

    rows = [("#", "Engine", "What it is", "Queries")]
    for r in f["high"][:4]:
        desc, n_q = clean_risk_text(str(r.get("description") or ""))
        eng = risk_engine(str(r.get("description") or "")) or "(general)"
        rows.append((str(r.get("risk_id") or ""), ENGINE_LABEL.get(eng, eng), clip(desc, 78), n_q))
    table(
        s,
        6.05,
        BODY_TOP,
        6.05,
        rows,
        col_w=[1.05, 1.55, 2.55, 0.90],
        head_size=10.0,
        body_size=9.0,
        row_h=0.56,
        head_h=0.32,
        emphasis={(i, 1): PINK for i in range(1, len(rows))},
    )

    card(s, 0.67, 5.05, 11.43, 1.05, GREEN)
    tf = textbox(s, 0.90, 5.17, 11.0, 0.9)
    mit = f["mitigations"][0] if f["mitigations"] else ""
    para(
        tf,
        "Each HIGH risk carries an affected-query count and a documented mitigation. "
        + (
            f"Specified mitigation: {mit}"
            if mit
            else "Mitigations are listed per risk in the Engineering Report."
        ),
        size=12.0,
        color=WHITE,
        first=True,
    )
    return s


def slide_sequencing(prs, f):
    s = add_slide(prs, LAYOUT_CONTENT)
    waves = f["waves"]
    set_title(s, "Migration Sequencing")
    first_pct = waves[0]["workload"] if waves else 0.0
    set_subtitle(s, f"Wave 1 covers {first_pct:.1f}% of the workload with no data migration")

    y = BODY_TOP - 0.02
    for i, w in enumerate(waves):
        accent = w["accent"]
        card(s, 0.67, y, 11.43, 0.92, accent)
        tf = textbox(s, 0.92, y + 0.06, 1.3, 0.35)
        para(tf, f"WAVE {i + 1}", size=13.0, bold=True, color=accent, first=True, font=FONT_HEAD)
        tf = textbox(s, 2.25, y + 0.05, 4.4, 0.35)
        para(tf, w["names"], size=13.0, bold=True, color=WHITE, first=True)
        stats = (
            f"{w['workload']:.1f}% workload",
            f"{min(f['conf'].get(e['engine'], 0) for e in w['engines']):.0f}% confidence",
            f"{w['high']} HIGH risk" + ("" if w["high"] == 1 else "s"),
        )
        for j, val in enumerate(stats):
            tf = textbox(s, 6.75 + j * 1.80, y + 0.06, 1.75, 0.33)
            para(tf, val, size=11.5, bold=(j == 1), color=PAPER if j != 1 else accent, first=True)
        tf = textbox(s, 2.25, y + 0.44, 9.6, 0.4)
        para(tf, w["note"], size=10.0, color=MUTED, first=True)
        y += 1.00
        if i == 0:
            gate = s.shapes.add_shape(
                MSO_SHAPE.ROUNDED_RECTANGLE,
                Inches(3.55),
                Inches(y - 0.02),
                Inches(5.6),
                Inches(0.36),
            )
            gate.fill.solid()
            gate.fill.fore_color.rgb = PINK
            gate.line.fill.background()
            gate.shadow.inherit = False
            gate.text_frame.text = "VALIDATION GATE  —  load test at production scale"
            p = gate.text_frame.paragraphs[0]
            p.alignment = PP_ALIGN.CENTER
            _style(p.runs[0], 11.0, bold=True, color=RGBColor(0x16, 0x1D, 0x26))
            y += 0.46

    card(s, 0.67, y + 0.08, 11.43, 0.98, GREEN)
    tf = textbox(s, 0.90, y + 0.17, 11.0, 0.85)
    para(tf, "Sequencing constraints", size=10.5, bold=True, color=GREEN, first=True, space_after=2)
    para(
        tf,
        f"{f['assignment'].get('co_dependency_groups', 0)} co-dependency groups constrain "
        f"wave boundaries; {f['n_tradeoffs']} trade-offs are documented in the Engineering "
        f"Report. Later waves are re-scoped from wave 1's measurements.",
        size=11.5,
        color=WHITE,
    )
    return s


SLIDES = (
    slide_summary,
    slide_evidence,
    slide_workload,
    slide_decisions,
    slide_risk,
    slide_sequencing,
)


def render_executive_summary_pptx(
    report: dict[str, Any],
    export_data: dict[str, Any] | None = None,
) -> bytes:
    """Render the executive summary deck and return it as bytes.

    Args:
        report: the synthesis ``report.json`` — the same dict the Decision Report
            HTML is rendered from.
        export_data: the report export data (``analysis_report.build_export_data``),
            which carries the collector query patterns and triage signals slide 3
            is built from. Optional: without it slide 3 falls back to whatever the
            report itself states, because a missing pattern cache is not a reason
            to withhold the other five slides.

    Returns:
        The ``.pptx`` file. Deterministic for a given ``(report, export_data)``:
        the same job renders byte-comparable content on every execution.

    Raises:
        Whatever ``python-pptx`` raises on a malformed template. The caller
        publishes best-effort and logs; see ``tools._publish_synthesis_deliverables``.
    """
    f = derive(report, export_data or {})
    prs = open_deck(keep=1)
    retitle_intro(prs.slides[0], f["date_text"])
    for build in SLIDES:
        build(prs, f)
    prs.core_properties.title = f"Database Modernization Assessment — {f['database']}"
    # The report's timestamp, not the wall clock: re-rendering the same job must
    # not change the file's metadata either.
    prs.core_properties.comments = f"AWS Transform — job {f['job_id']} — {f['timestamp']}"
    buf = io.BytesIO()
    prs.save(buf)
    data = buf.getvalue()
    logger.info(
        "Rendered %s: %d slides, %d bytes, database=%s job=%s",
        FILENAME,
        len(SLIDES) + 1,
        len(data),
        f["database"],
        f["job_id"],
    )
    return data
