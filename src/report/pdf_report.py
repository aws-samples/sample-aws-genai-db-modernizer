"""Executive Summary Report -- the multi-page PDF deliverable.

``summary-executive-report.pdf`` is laid out as a flowing document with reportlab
platypus: paragraphs, tables that repeat their header row across pages, and a page
template with a header and a "Page N of M" footer. It is built straight from the
synthesis ``report.json`` (ADR-032): every figure comes from the facts layer
(``src.report.facts.report_facts`` and the ``tco_analysis`` block), and the
sections that have an equivalent in the engineering report print that report's own
Markdown, so the two never word the same fact differently. This module formats; it
never counts, totals or filters a figure the facts layer owns. Nothing is clipped:
long text wraps and long tables continue on the next page.

Sections, in order: cover, executive summary, cost, recommended engines and roles,
migration wave plan, risks, then (when there are any) the unresolved engine ties,
the queries moved after approval, the queries without a fitting engine and the
close calls, and a traceability appendix (the per-query "why this engine" record
for the queries that moved or were tied).

Determinism: reportlab runs in ``invariant`` mode (no creation date, no random
document id) and every string drawn comes from the report or is a constant, so one
``report.json`` yields byte-identical PDFs.

Fonts: Amazon Ember Regular and Bold ship in ``assets/fonts``. If a face is
unavailable the module falls back to Helvetica so a PDF is still produced.
"""

from __future__ import annotations

import datetime as dt
import io
import logging
import re
from pathlib import Path
from typing import Any

from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    BaseDocTemplate,
    CondPageBreak,
    Frame,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from src.shared.engine_names import display_engine

from . import escaping, pdf_markdown
from .architecture import _architecture_engines
from .close_calls import close_calls_md
from .engine_choices import _reroutes_md, _tied_queries_md
from .engineering_report import _risk_register_md, _target_engines_md, _trade_offs_md
from .facts import report_facts
from .formatting import (
    _cost_baseline_text,
    _fmt_usd,
    _retired_engine_savings_text,
    fallback_withheld_reason,
    label_summary_counts,
)
from .query_traces import query_traces_md
from .risks import NO_OPEN_RISKS_CAVEAT, _severity_counts_text, filtered_risks
from .roadmap import _roadmap_md
from .sources_of_truth import sources_of_truth_md
from .unservable import unservable_md

logger = logging.getLogger(__name__)

FILENAME = "summary-executive-report.pdf"
FONT_DIR = Path(__file__).parent / "assets" / "fonts"

# The deliverable's only product name.
PRODUCT_NAME = "Database Modernizer Assessment Tool"
REPORT_NAME = "Executive Summary Report"
# The copyright line of the repository's LICENSE file, word for word
# (tests/unit/report/test_pdf_report.py reads LICENSE and compares).
COPYRIGHT = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
FOOTER = f"{COPYRIGHT} For informational purposes only."
# Where the summary sends readers for the detail: real files in the job's
# deliverables folder (src.report.deliverables.FOLDER_ENTRIES), by their names.
SEE_ALSO_NOTE = "Full detail in the deliverables folder: engineering-report.md."
# Stated once, after the summary: the retired decision report's closing words (#987).
NO_MODEL_DECIDES_NOTE = (
    "Engine and query assignments are produced deterministically \u2014 no language model "
    "decides which engine a table or query goes to. The executive summary is written over "
    "already-computed results and cannot change a recommendation."
)
# How the overall risk level is derived (#248; synthesis_report.overall_risk_level).
RISK_RULE_NOTE = (
    "Overall risk is the highest severity among the open risks; risks the assignment "
    "resolved do not count."
)
WITHHELD_UNTRUSTED_NOTE = (
    "The generated narrative was withheld because it referenced schema work this run "
    "did not perform; the figures here are unaffected."
)

# Section headings, in document order. "Close calls and queries without a fitting
# engine" appears only when the report has either; the others always do.
H_SUMMARY = "Executive summary"
H_COST = "Cost"
H_ENGINES = "Recommended engines and roles"
H_WAVES = "Migration wave plan"
H_RISKS = "Risks"
H_CLOSE = "Close calls and unservable queries"
H_APPENDIX = "Traceability appendix"
SECTION_HEADINGS = (H_SUMMARY, H_COST, H_ENGINES, H_WAVES, H_RISKS, H_CLOSE, H_APPENDIX)

_FALLBACK = "Helvetica"
_FALLBACK_BOLD = "Helvetica-Bold"

# Registered reportlab font names, keyed by (typeface, bold).
_FONTS: dict[tuple[str, bool], str] = {}
_FONT_FILES = {
    ("Amazon Ember", False): "AmazonEmber_Rg.ttf",
    ("Amazon Ember", True): "AmazonEmber_Bd.ttf",
}
FAMILY = "Ember"

INK = HexColor("#232F3E")
ACCENT = HexColor("#FF9900")
MUTED = HexColor("#5F6B7A")
RULE = HexColor("#C5CCD3")
HEAD_BG = HexColor("#E8EDF2")
ZEBRA = HexColor("#F6F8FA")


def register_fonts() -> None:
    """Register the bundled TTFs once. Missing files degrade to Helvetica."""
    if _FONTS:
        return
    for (face, bold), filename in _FONT_FILES.items():
        path = FONT_DIR / filename
        name = f"Ember-{'Bd' if bold else 'Rg'}"
        if not path.exists():
            logger.warning("Font %s missing; falling back to %s", path.name, _FALLBACK)
            _FONTS[(face, bold)] = _FALLBACK_BOLD if bold else _FALLBACK
            continue
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, str(path)))
        _FONTS[(face, bold)] = name
    pdfmetrics.registerFontFamily(
        FAMILY,
        normal=font_for(None, False),
        bold=font_for(None, True),
        italic=font_for(None, False),
        boldItalic=font_for(None, True),
    )


def font_for(typeface: str | None, bold: bool) -> str:
    """The registered reportlab font for a typeface and weight (Ember by default)."""
    if not _FONTS:
        register_fonts()
    return _FONTS.get((typeface or "Amazon Ember", bold)) or (_FALLBACK_BOLD if bold else _FALLBACK)


# ---------------------------------------------------------------------------
# styles
# ---------------------------------------------------------------------------
def _styles() -> dict[str, ParagraphStyle]:
    rg, bd = font_for(None, False), font_for(None, True)
    body = ParagraphStyle(
        "body",
        fontName=rg,
        bulletFontName=rg,
        fontSize=9.5,
        leading=13,
        textColor=INK,
        spaceAfter=5,
        alignment=TA_LEFT,
    )
    return {
        "body": body,
        "h1": ParagraphStyle(
            "h1", parent=body, fontName=bd, fontSize=19, leading=23, spaceBefore=6, spaceAfter=8
        ),
        "h2": ParagraphStyle(
            "h2", parent=body, fontName=bd, fontSize=13, leading=17, spaceBefore=10, spaceAfter=5
        ),
        "h3": ParagraphStyle(
            "h3",
            parent=body,
            fontName=bd,
            fontSize=10.5,
            leading=14,
            spaceBefore=8,
            spaceAfter=3,
            keepWithNext=1,
        ),
        "cell": ParagraphStyle(
            "cell", parent=body, fontSize=8.2, leading=10.4, spaceAfter=0, textColor=INK
        ),
        "head_cell": ParagraphStyle(
            "head_cell", parent=body, fontName=bd, fontSize=8.2, leading=10.4, spaceAfter=0
        ),
        "note": ParagraphStyle("note", parent=body, fontSize=8.5, leading=11.5, textColor=MUTED),
        "cover_title": ParagraphStyle(
            "cover_title", parent=body, fontName=bd, fontSize=30, leading=36, spaceAfter=6
        ),
        "cover_sub": ParagraphStyle(
            "cover_sub", parent=body, fontSize=15, leading=20, textColor=MUTED, spaceAfter=24
        ),
        "label": ParagraphStyle(
            "label", parent=body, fontName=bd, fontSize=9.5, leading=13, textColor=MUTED
        ),
    }


def _md_styles(st: dict[str, ParagraphStyle]) -> pdf_markdown.MdStyles:
    bullets = tuple(
        ParagraphStyle(
            f"bullet{lvl}",
            parent=st["body"],
            leftIndent=14 + 14 * lvl,
            bulletIndent=4 + 14 * lvl,
            spaceAfter=3,
        )
        for lvl in range(4)
    )
    return pdf_markdown.MdStyles(
        body=st["body"],
        h2=st["h2"],
        h3=st["h3"],
        cell=st["cell"],
        head_cell=st["head_cell"],
        bullets=bullets,
        code_font=font_for(None, False),
        table_head_bg=HEAD_BG,
        table_grid=RULE,
        table_zebra=ZEBRA,
    )


# ---------------------------------------------------------------------------
# page furniture
# ---------------------------------------------------------------------------
class _NumberedCanvas(Canvas):
    """Two-pass canvas so the footer can say "Page N of M"."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._saved: list[dict[str, Any]] = []

    def showPage(self) -> None:  # noqa: N802 -- reportlab's name
        self._saved.append(dict(self.__dict__))
        self._startPage()

    def save(self) -> None:
        total = len(self._saved)
        for state in self._saved:
            self.__dict__.update(state)
            self._draw_page_number(total)
            super().showPage()
        super().save()

    def _draw_page_number(self, total: int) -> None:
        if self._pageNumber == 1:  # the cover carries no furniture
            return
        self.setFont(font_for(None, False), 8)
        self.setFillColor(MUTED)
        self.drawRightString(
            letter[0] - 0.75 * inch, 0.5 * inch, f"Page {self._pageNumber} of {total}"
        )


def _furniture(db: str, footer: str):
    def draw(canvas: Canvas, doc: Any) -> None:
        w, h = letter
        canvas.saveState()
        canvas.setFont(font_for(None, True), 8)
        canvas.setFillColor(MUTED)
        canvas.drawString(0.75 * inch, h - 0.5 * inch, PRODUCT_NAME)
        canvas.setFont(font_for(None, False), 8)
        canvas.drawRightString(w - 0.75 * inch, h - 0.5 * inch, pdf_markdown.printable(db, None))
        canvas.setStrokeColor(RULE)
        canvas.setLineWidth(0.5)
        canvas.line(0.75 * inch, h - 0.58 * inch, w - 0.75 * inch, h - 0.58 * inch)
        canvas.line(0.75 * inch, 0.62 * inch, w - 0.75 * inch, 0.62 * inch)
        canvas.drawString(0.75 * inch, 0.5 * inch, footer)
        canvas.restoreState()

    return draw


def _cover_page(canvas: Canvas, doc: Any) -> None:
    w, h = letter
    canvas.saveState()
    canvas.setFillColor(INK)
    canvas.rect(0, h - 0.55 * inch, w, 0.55 * inch, stroke=0, fill=1)
    canvas.setFillColor(ACCENT)
    canvas.rect(0, h - 0.6 * inch, w, 0.05 * inch, stroke=0, fill=1)
    canvas.restoreState()


class _Heading(Paragraph):
    """A section heading that also writes a PDF outline entry."""

    outline = True


class _Doc(BaseDocTemplate):
    _outlines = 0

    def afterFlowable(self, flowable: Any) -> None:  # noqa: N802 -- reportlab's name
        if isinstance(flowable, _Heading):
            text = re.sub(r"<[^>]+>", "", flowable.text)
            self._outlines += 1
            key = f"section-{self._outlines}"
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(text, key, 0, 0)


# ---------------------------------------------------------------------------
# content
# ---------------------------------------------------------------------------
def _date(report: dict[str, Any]) -> dt.date | None:
    """The report's own generation date; None when it carries no valid one."""
    m = re.match(r"\d{4}-\d{2}-\d{2}", str(report.get("timestamp") or ""))
    try:
        return dt.date.fromisoformat(m.group(0)) if m else None
    except ValueError:
        return None


def _trust(report: dict[str, Any]) -> bool:
    """Whether the generated summary may be shown (as ``deliverables`` decides it)."""
    return any(r.get("schema_design_available") for r in (report.get("ranking") or []))


def _withheld_note(report: dict[str, Any], trust: bool, summary: Any) -> str | None:
    """Why the summary shown is not the generated one, worded as the retired decision
    report worded it (#987): withheld because no schema design ran, or replaced by the
    deterministic fallback for the reason synthesis recorded. A run that never generated
    a narrative (``--llm-mode none``: the summary is the deterministic one) withheld
    nothing, so it gets no note."""
    if not summary:
        return None
    if not trust:
        generated = report.get("summary")
        if generated and generated != report.get("summary_deterministic"):
            return WITHHELD_UNTRUSTED_NOTE
        return None
    if report.get("summary_source") == "deterministic_fallback":
        return (
            f"The generated narrative was withheld because it "
            f"{fallback_withheld_reason(report)}; this summary is built from the "
            "effective assignment."
        )
    return None


def _cost_md(report: dict[str, Any]) -> list[str]:
    """Cost section lines: the baseline text and projected total as the engineering
    report words them, then one row per engine and the total, all stored figures."""
    tco = report.get("tco_analysis") or {}
    facts = report_facts(report)
    current_text, savings_text = _cost_baseline_text(tco)
    out = [
        f"- Current monthly (source): {escaping.md_text(current_text)}",
        f"- Projected monthly (target): {escaping.md_text(_fmt_usd(tco.get('projected_monthly_cost')))}",
        f"- Savings: {escaping.md_text(savings_text)}",
    ]
    # #949: only an engine the source runs today; ``eliminated_engine_costs`` is not read.
    retired_text = _retired_engine_savings_text(tco)
    if retired_text:
        out.append(
            f"- Retired by this plan: {escaping.md_text(retired_text)}, "
            "running today and not in this projection (see Migration trade-offs below "
            "for why)."
        )
    out.append("")
    engines = _architecture_engines(report)
    if engines:
        out += ["| Engine | Role | Est. monthly |", "|---|---|---|"]
        for e in engines:
            out.append(
                f"| {escaping.md_cell(display_engine(e['engine']))} "
                f"| {escaping.md_cell(e['role'])} | {escaping.md_cell(_fmt_usd(e.get('cost')))} |"
            )
        out.append(
            f"| Total | | {escaping.md_cell(_fmt_usd(facts.get('engine_cost_total_usd')))} |"
        )
        out.append("")
    return out


def _risks_md(report: dict[str, Any]) -> list[str]:
    """Risks section: the posture, worded as the retired decision report worded it, then
    the register."""
    facts = report_facts(report)
    risk = report.get("risk_assessment") or {}
    open_count = int(facts.get("open_risk_count") or 0)
    resolved_count = int(facts.get("resolved_risk_count") or 0)
    out = [f"- Overall risk: {escaping.md_text(risk.get('overall_risk_level', 'not assessed'))}"]
    if filtered_risks(report):
        sev = _severity_counts_text(facts.get("open_risks_by_severity") or {})
        out.append(f"- Open risks: {open_count} ({escaping.md_text(sev)})")
        out += ["", f"*{escaping.md_text(RISK_RULE_NOTE)}*", ""]
    else:
        out.append(
            f"- No open migration risks; {resolved_count} resolved by the assignment."
            if resolved_count
            else "- No risks were identified in this analysis."
        )
        out += ["", f"*{escaping.md_text(NO_OPEN_RISKS_CAVEAT)}*"]
    if resolved_count and filtered_risks(report):
        out.append(f"- Resolved by the assignment: {resolved_count}")
    out.append("")
    out += _risk_register_md(report)
    out += _trade_offs_md(report, facts)
    strategies = [s for s in (risk.get("mitigation_strategies") or []) if s]
    if strategies:
        out += ["## Mitigation strategies", ""]
        out += [f"- {escaping.md_text(s)}" for s in strategies]
        out.append("")
    return out


def build_story(report: dict[str, Any], *, trust_generated_summary: bool | None = None):
    """The flowables of the whole document, in section order, and the footer text."""
    register_fonts()
    st = _styles()
    ms = _md_styles(st)
    glyphs = _glyphs()
    width = letter[0] - 1.5 * inch

    def md(lines: list[str]) -> list[Any]:
        return pdf_markdown.to_flowables(lines, ms, width=width, glyphs=glyphs)

    def h1(text: str) -> list[Any]:
        return [CondPageBreak(2.2 * inch), _Heading(text, st["h1"])]

    def txt(s: Any) -> str:
        return pdf_markdown.inline(escaping.md_text(s), ms, glyphs)

    db = str(report.get("database_name") or "?")
    arch = report.get("recommended_architecture") or {}
    risk = report.get("risk_assessment") or {}
    tco = report.get("tco_analysis") or {}
    trust = _trust(report) if trust_generated_summary is None else trust_generated_summary
    generated = _date(report)
    footer = FOOTER

    # -- cover ---------------------------------------------------------------
    rows = [
        ("Source database", db),
        ("Architecture", str(arch.get("architecture_type") or "not determined").replace("_", " ")),
        ("Overall risk", str(risk.get("overall_risk_level") or "not assessed")),
        ("Projected monthly cost", _fmt_usd(tco.get("projected_monthly_cost"))),
        ("Job", str(report.get("job_id") or "unknown")),
    ]
    if generated:
        rows.append(("Generated", generated.isoformat()))
    cover_tbl = Table(
        [[Paragraph(txt(k), st["label"]), Paragraph(txt(v), st["body"])] for k, v in rows],
        colWidths=[1.9 * inch, width - 1.9 * inch],
        hAlign="LEFT",
    )
    cover_tbl.setStyle(
        TableStyle(
            [
                ("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    story: list[Any] = [
        Spacer(1, 2.2 * inch),
        Paragraph(PRODUCT_NAME, st["cover_title"]),
        Paragraph(REPORT_NAME, st["cover_sub"]),
        cover_tbl,
        NextPageTemplate("body"),
        PageBreak(),
    ]

    # -- executive summary ---------------------------------------------------
    summary = report.get("summary") if trust else report.get("summary_deterministic")
    summary = summary or report.get("summary_deterministic")
    if summary and summary == report.get("summary_deterministic"):
        summary = label_summary_counts(summary, report)
    story += h1(H_SUMMARY)
    if summary:
        for chunk in str(summary).strip().split("\n"):
            if chunk.strip():
                story.append(Paragraph(txt(chunk.strip()), st["body"]))
    else:
        story.append(Paragraph(txt("No executive summary was generated for this run."), st["body"]))
    withheld = _withheld_note(report, trust, summary)
    if withheld:
        story.append(Paragraph(txt(withheld), st["note"]))
    story.append(Paragraph(txt(SEE_ALSO_NOTE), st["note"]))
    story.append(Paragraph(txt(NO_MODEL_DECIDES_NOTE), st["note"]))

    # -- cost ----------------------------------------------------------------
    story += h1(H_COST) + md(_cost_md(report))

    # -- engines -------------------------------------------------------------
    story += h1(H_ENGINES) + md(_target_engines_md(report))

    # -- wave plan -----------------------------------------------------------
    story += h1(H_WAVES) + md(_roadmap_md(report) + sources_of_truth_md(report))

    # -- risks ---------------------------------------------------------------
    story += h1(H_RISKS) + md(_risks_md(report))

    # -- open ties, moves after approval, unservable queries, close calls -----
    # The open ties and the moves after approval are the engineering report's own
    # sections: the decision makers who read the retired decision report saw both
    # there (#987).
    close = (
        _tied_queries_md(report)
        + _reroutes_md(report)
        + unservable_md(report)
        + close_calls_md(report)
    )
    if close:
        story += h1(H_CLOSE) + md(close)

    # -- traceability appendix -----------------------------------------------
    traces = query_traces_md(report)
    if traces:
        story += h1(H_APPENDIX) + md(traces)

    return story, footer, db


_GLYPH_CACHE: list[set[int] | None] = []


def _glyphs() -> set[int] | None:
    """Code points the regular face can draw; None when running on the fallback font."""
    if not _GLYPH_CACHE:
        path = FONT_DIR / _FONT_FILES[("Amazon Ember", False)]
        _GLYPH_CACHE.append(pdf_markdown.known_glyphs(str(path)) if path.exists() else None)
    return _GLYPH_CACHE[0]


def render_executive_summary_pdf(
    report: dict[str, Any],
    *,
    trust_generated_summary: bool | None = None,
) -> bytes:
    """Render the PDF report from ``report.json`` and return its bytes.

    Every figure comes from the report's facts layer. Deterministic for a given report.
    """
    story, footer, db = build_story(report, trust_generated_summary=trust_generated_summary)
    buf = io.BytesIO()
    w, h = letter
    margin = 0.75 * inch
    doc = _Doc(
        buf,
        pagesize=letter,
        leftMargin=margin,
        rightMargin=margin,
        topMargin=0.85 * inch,
        bottomMargin=0.85 * inch,
        title=f"{PRODUCT_NAME} — {db}",
        author=PRODUCT_NAME,
        creator=PRODUCT_NAME,
        subject=f"{REPORT_NAME} — job {report.get('job_id') or 'unknown'}",
        invariant=1,
        pageCompression=1,
    )
    cover = Frame(margin, 0.85 * inch, w - 2 * margin, h - 1.7 * inch, id="cover")
    body = Frame(margin, 0.85 * inch, w - 2 * margin, h - 1.7 * inch, id="body")
    doc.addPageTemplates(
        [
            PageTemplate(id="cover", frames=[cover], onPage=_cover_page),
            PageTemplate(id="body", frames=[body], onPage=_furniture(db, footer)),
        ]
    )
    doc.build(story, canvasmaker=_NumberedCanvas)
    data = buf.getvalue()
    logger.info("Rendered %s: %d bytes, database=%s", FILENAME, len(data), db)
    return data


__all__ = [
    "FILENAME",
    "H_APPENDIX",
    "H_CLOSE",
    "H_COST",
    "H_ENGINES",
    "H_RISKS",
    "H_SUMMARY",
    "H_WAVES",
    "PRODUCT_NAME",
    "SECTION_HEADINGS",
    "render_executive_summary_pdf",
]
