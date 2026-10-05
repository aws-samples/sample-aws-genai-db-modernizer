"""Executive Summary Report — the Database Modernizer Assessment PPTX deliverable.

Renders ``summary-executive-report.pptx`` from the synthesis report plus the
report export data, using ``assets/template.pptx`` (a neutral dark deck: its own
theme, aurora layout backgrounds, the AWS logo, and on every slide the footer
"© <year> Amazon Web Services, Inc. or its affiliates. For informational
purposes only."). Published alongside the Decision Report HTML by
``src.report.deliverables.render_deliverables``, from the same inputs, so the two
never disagree.

Three rules this module exists to honour:

1. **No cost comparison.** Cost figures are not used as an argument anywhere:
   no per-engine cost table, no "% of spend", no savings claim. Cost sentences
   are stripped out of the deterministic summary before it is rendered.
2. **Slide 1 mirrors the HTML Decision Report.** It reuses that renderer's own
   ``_architecture_engines`` helper so the deck and the HTML cannot drift.
3. **Deterministic content.** Every string is either a fixed constant or derived
   from the artifacts by rule in ``derive()``. No LLM narrative, no ``now()``, and
   every iteration order is explicitly sorted, so the same job renders the same
   deck on every agent execution. The clock-derived values a deliverable
   normally carries — the generation date on the title slide and the footer's
   copyright year — are read from the report's own ``timestamp``. Only a report
   without one falls back to the current year for the footer.

The public entry point is ``render_executive_summary_pptx(report, export_data)``,
which returns bytes; nothing here touches S3 or the platform.
"""

from __future__ import annotations

import datetime as dt
import io
import logging
import math
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
# recommended_architecture.databases and schema_designs). ``filtered_risks`` is
# reused for the same reason the risk count must agree with the decision and
# engineering reports (issue #201): one filter, one count, everywhere.
# ``plural_noun`` is the one place every count+noun string in this deck and in
# the decision/engineering reports agrees on English count agreement
# (issue #206).
from src.shared.engine_names import ENGINE_DISPLAY_NAMES
from src.shared.migration_wave_engines import RELATIONAL_ENGINES, SEARCH_ENGINES
from src.shared.ranking import confidence_text, engine_confidence, is_signal_only

from .renderers import (
    SHARED_TABLES_LABEL,
    SHARED_TABLES_NOTE,
    _architecture_engines,
    _mapping_split,
    filtered_risks,
    fmt_num,
    label_summary_counts,
    plural_noun,
    plural_verb,
    resolve_migration_waves,
)

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
# Alias, not a copy: display names are the shared source of truth in
# src.shared.engine_names (#221 follow-up). Restricted to the six engines this deck
# colours (ENGINE_COLOR, above) rather than re-exporting ENGINE_DISPLAY_NAMES whole,
# so prettify_engines/name_target_tables below -- which build their match set from
# this dict's keys -- keep matching exactly the engine ids they always have, not
# the three engines (neptune, keyspaces, aurora) the deck has no colour for. Kept
# under this name because other modules and tests still import ENGINE_LABEL.
ENGINE_LABEL = {engine: ENGINE_DISPLAY_NAMES[engine] for engine in ENGINE_COLOR}

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
# The Risk Profile shows each listed risk's own mitigation in a table cell; free
# text from the report, so it is clipped rather than allowed to overflow the row.
MITIGATION_MAX_CHARS = 66
# A migration target the assessment is at least this confident in is sequenced before
# the ones it is not. Stated as a constant so the wave split is reproducible.
CONFIDENCE_FLOOR = 50
# Roles that need no data migration. They always form Wave 1 (reversible), at
# any confidence; CONFIDENCE_FLOOR orders only the migration targets.
NO_MIGRATION_ROLES = ("Retained", "Cache layer")

# Accent lookup for a resolved ``migration_waves`` entry (#225): the source-compatible
# relational engine, carried over 1:1, gets the same "no further migration" blue as
# the cache; a search read model is green; everything else is a migration target.
# Imported from src/shared (#225) so this can't drift from the builder.
_WAVE_RETAINED_ENGINES = RELATIONAL_ENGINES
_WAVE_SEARCH_ENGINES = SEARCH_ENGINES

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

    Slide 1 of the template *is* the intro title slide (the deck name, a
    subtitle, a small-print line, and the AWS logo that comes from the layout),
    so it is retained rather than rebuilt — only its subtitle and small print
    are filled in by ``retitle_intro``. New slides append after it, which is the
    order we want.
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


# The deliverable's name: the title slide's title (authored in the template),
# the deck/PDF title and the author/creator metadata.
DECK_NAME = "Database Modernizer Assessment"

# Every slide's footer, drawn by the slide layouts. The template carries it with
# a sample year; ``set_footer_year`` stamps the generation year at render time.
FOOTER = "© {year} Amazon Web Services, Inc. or its affiliates. For informational purposes only."
_FOOTER_RE = re.compile(r"^©\s*\d{4}\b.*Amazon Web Services")

# Template sample text the intro slide's runs are found by.
_INTRO_SUBTITLE_SAMPLE = "Source database"
_INTRO_SMALL_PRINT_SAMPLE = "Job · Generated"
_INTRO_SUBTITLE_PT = 32.0


def retitle_intro(slide, f: dict[str, Any]) -> None:
    """Fill in the retained intro slide in place.

    Two runs are rewritten: the subtitle (the source database) and the small
    print (job ID and generation date, so a printed PDF can be traced back to
    its run). Everything else on the slide — the title, the AWS logo, the
    footer — is left as authored.
    """
    small = f"Job {f['job_id'] or 'unknown'}"
    if f["generated"]:
        small += f"  ·  Generated {f['generated']}"
    for shape in slide.shapes:
        if not shape.has_text_frame:
            continue
        for p in shape.text_frame.paragraphs:
            for r in p.runs:
                if r.text == _INTRO_SUBTITLE_SAMPLE:
                    r.text = f"Source database {f['database']}" if f["database"] else ""
                    # One line at the template's 32pt, shrunk for long names
                    # (the frame does not autofit).
                    fit = (shape.width / 914400) * 72 / max(_text_em(r.text), 1.0)
                    if fit < _INTRO_SUBTITLE_PT:
                        r.font.size = Pt(int(fit * 2) / 2)
                elif r.text == _INTRO_SMALL_PRINT_SAMPLE:
                    r.text = small


def set_footer_year(prs: PresentationPart, year: int) -> None:
    """Stamp ``year`` into the copyright footer on the master and every layout.

    The footer is static template text, so without this every deck would carry
    the year the template was last edited. A footer paragraph is collapsed into
    its first run (keeping that run's formatting).
    """
    master = prs.slide_masters[0]
    for part in (master, *master.slide_layouts):
        for shape in part.shapes:
            if not shape.has_text_frame:
                continue
            for p in shape.text_frame.paragraphs:
                if not p.runs or not _FOOTER_RE.match(p.text):
                    continue
                first, *rest = p.runs
                first.text = FOOTER.format(year=year)
                for r in rest:
                    r._r.getparent().remove(r._r)


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


def _estimated_text_height_in(
    text: str, size_pt: float, width_in: float, line_height: float = 1.22
) -> float:
    """Rough estimate of the rendered height of ``text`` word-wrapped to
    ``width_in`` at ``size_pt``, reusing ``_text_em``'s advance-width table.

    Not exact (PowerPoint's own layout differs slightly by font), but good
    enough to catch a card whose body text clearly will not fit (#225): the
    Migration Sequencing slide's rationale/gate text is long enough, at real
    wordpress/discourse scale, to run past a fixed-height card otherwise.
    """
    if not text:
        return 0.0
    width_in_at_size = _text_em(text) * (size_pt / 72.0)
    lines = max(1, math.ceil(width_in_at_size / width_in))
    return lines * (size_pt * line_height / 72.0)


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


def name_target_tables(text: str) -> str:
    """``aurora_mysql: 9 tables`` -> ``aurora_mysql: 9 target tables``.

    The deterministic summary breaks the schema design down per engine as
    "<engine>: N tables" -- the design's target tables -- while the Scope column
    on the same slide counts the source tables mapped to the engine. Naming both
    keeps "9 tables" and "1 table" from reading as one disagreeing count (#219).

    Synthesis now writes "N target tables" itself; this only rewords a
    ``report.json`` produced before that, and is idempotent ("9 target tables"
    has no digit directly before "tables", so it is never matched again).
    """
    engines = "|".join(re.escape(k) for k in sorted(ENGINE_LABEL, key=len, reverse=True))
    return re.sub(rf"\b({engines}): (\d+) (tables?)\b", r"\1: \2 target \3", text)


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


_REATTRIBUTED_LEAD = re.compile(
    r"^Flagged by the [^:]+? analysis for \d+ quer(?:y|ies) now on [^:]+?:\s*"
)


SEVERITY_ORDER = ("CRITICAL", "HIGH", "MEDIUM", "LOW")
# Risks whose severity is none of SEVERITY_ORDER are shown, not dropped (#249 review).
UNRATED = "UNRATED"


def _severity(risk: dict[str, Any]) -> str:
    level = str(risk.get("severity") or "").upper()
    return level if level in SEVERITY_ORDER else UNRATED


def _risk_query_count(risk: dict[str, Any]) -> int:
    """Affected queries: the "(N remaining)" count in the description, else ``query_ids``."""
    _, n_q = clean_risk_text(str(risk.get("description") or ""))
    if n_q:
        return int(n_q)
    ids = risk.get("query_ids")
    return len(ids) if isinstance(ids, list) else 0


def _risk_caption(level: str, n_shown: int) -> str:
    """The Risk Profile caption for the severity its table shows (#249).

    The fixed "Each HIGH risk carries ..." sentence sat above an empty table on a
    run whose risks were all MEDIUM; the caption now names what the table lists.
    It sits below the table, so it does not point up or down.
    """
    if level in ("CRITICAL", "HIGH"):
        return f"Each {level} risk carries an affected-query count and a documented mitigation."
    if not level:
        return "No open risks remain."
    risks_word = plural_noun(n_shown, "risk")
    if level == UNRATED:
        return f"No risk has a recognised severity; the table shows the top unrated {risks_word}."
    above = " or ".join(
        k for k in ("HIGH", "MEDIUM") if SEVERITY_ORDER.index(k) < SEVERITY_ORDER.index(level)
    )
    return f"No {above} risks remain; the table shows the top {level} {risks_word}."


def split_risk_text(description: str, mitigation: str) -> tuple[str, str]:
    """(what it is, mitigation) for a Risk Profile row, without saying either twice.

    Several analysis risks are described as "<type>: <mitigation>" (``aggregation:
    COUNT(*) on postmeta ...: Query the META# prefix ...``) or end with their
    mitigation, so the full description contains the mitigation. That text is
    removed from the description, leaving the type or the problem ("Aggregation").
    A mitigation that is the whole description is shown once, under "What it is".
    Matched case-insensitively with whitespace collapsed, against the *full*
    description -- the clipped cell text would hide most repeats (#249 review).
    """
    desc = " ".join(str(description or "").split())
    mit = " ".join(str(mitigation or "").split())
    if mit:
        at = desc.casefold().find(mit.casefold())
        if at >= 0:
            rest = (desc[:at] + " " + desc[at + len(mit) :]).strip(" :;,.-\u2014")
            rest = " ".join(rest.split())
            if not rest:
                return _capitalize(desc), ""
            return _capitalize(rest), mit
    return _capitalize(desc), mit


def _capitalize(text: str) -> str:
    return text[:1].upper() + text[1:]


def clean_risk_text(description: str) -> tuple[str, str]:
    """Return (prose, query_count) for a risk description.

    Risks carry ``affected_tables`` but no query count; the count only exists
    inside the description as "(0% of queries resolved by schema design, N
    remaining)", so it is lifted out and the parenthetical removed.

    A risk re-attributed to the engine now serving its queries opens with
    "Flagged by the <Engine> analysis for N queries now on <Engine>: ". The
    deck's Engine column already names the engine, so that lead-in is dropped
    and the clipped row text shows the risk itself (#222).
    """
    desc = re.sub(r"^\[[^\]]+\]\s*", "", str(description or "")).strip()
    desc = _REATTRIBUTED_LEAD.sub("", desc, count=1)
    if desc.lower().startswith("unknown:"):
        desc = desc[len("unknown:") :].strip()
    m = re.search(r"\((?:[^()]*?)(\d+)\s+remaining\)", desc)
    n_q = m.group(1) if m else ""
    if m:
        desc = (desc[: m.start()] + desc[m.end() :]).strip()
    return " ".join(desc.split()), n_q


# Triage signal -> singular modifier for "N <modifier> queries" in a sentence.
# The SIGNAL_LABEL forms are plural nouns ("Key-value lookups"), which read as
# "key-value lookups queries" when a count + "queries" is appended (#220).
SIGNAL_MODIFIER = {
    "aggregations": "aggregation",
    "complex_joins": "complex-join",
    "eav_pattern": "entity-attribute-value",
    "high_frequency_reads": "high-frequency read",
    "json_columns": "JSON-column",
    "junction_tables": "junction-table",
    "key_value_lookups": "key-value lookup",
    "leaderboard_pattern": "leaderboard / top-n",
    "low_frequency_reads": "low-frequency read",
    "low_frequency_writes": "low-frequency write",
    "metadata_config": "metadata / config",
    "range_queries": "range",
    "session_store": "session store",
    "status_filters": "status-filter",
    "subqueries": "correlated-subquery",
    "text_search": "full-text search",
    "time_series": "time-series",
    "write_heavy": "write-heavy",
}


def signal_modifier(name: str) -> str:
    """``key_value_lookups`` -> ``key-value lookup``, for "14 key-value lookup queries"."""
    return SIGNAL_MODIFIER.get(name, name.replace("_", " ").lower())


def join_names(names: list[str]) -> str:
    """``["A"]`` -> ``A``; ``["A", "B"]`` -> ``A and B``; ``["A", "B", "C"]`` -> ``A, B and C``."""
    if len(names) <= 2:
        return " and ".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


def _evidence_text(signal: dict[str, Any] | None, engine: str | None = None) -> str:
    """The evidence sentence for the signal ``_evidence_signal`` picked.

    With ``engine`` (and the signal's ``served`` count, from the effective
    assignment): "14 of 15 leaderboard / top-n queries routed to ElastiCache", or
    "15 ... routed to ElastiCache" when every query of the signal went there. Without
    it (no query journeys to count from): "N <signal> queries in the whole
    workload".

    Extracted so the exact #206 regression ("1 session store queries") has a
    direct unit test independent of building a full ``derive()`` input.
    """
    if not signal:
        return "limited supporting evidence"
    if engine is not None and "served" in signal:
        n = signal["served"]
        # The workload slide lists the signal's whole-workload count; when only part
        # of it landed here, name both so the two slides reconcile (#256).
        total = signal.get("count")
        of_total = f" of {total}" if isinstance(total, int) and total > n else ""
        noun = plural_noun(total if of_total else n, "query", "queries")
        return (
            f"{n}{of_total} {signal_modifier(signal['name'])} {noun} "
            f"routed to {ENGINE_LABEL.get(engine, engine)}"
        )
    n = signal["count"]
    return (
        f"{n} {signal_modifier(signal['name'])} {plural_noun(n, 'query', 'queries')} "
        f"in the whole workload"
    )


# "signal override: leaderboard_pattern → elasticache" in a ranking entry's
# assignment_reason_summary: the triage signal that routed queries to the engine.
_SIGNAL_OVERRIDE = re.compile(r"signal override:\s*([A-Za-z0-9_]+)\s*(?:→|->)\s*([A-Za-z0-9_]+)")


def _triage_family(engine: str) -> str:
    """Triage targets name the Aurora family (``aurora``); rankings name the
    flavour (``aurora_mysql``, ``aurora_postgresql``)."""
    return "aurora" if engine.startswith("aurora") else engine


def _evidence_signal(
    engine: str,
    ranking_row: dict[str, Any],
    q_signals: list[dict[str, Any]],
    signals: list[dict[str, Any]],
    assigned: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """The triage signal that best explains why ``engine`` was chosen.

    ``assigned`` maps query_id -> the engine the *effective* assignment gave it
    (from the query journeys). When present, each signal is scored by how many
    of its queries actually landed on ``engine`` (returned as ``served``); a
    signal whose queries all went elsewhere is not evidence for this engine,
    whatever triage targeted. Preference: a signal the ranking names as a
    "signal override" for the engine (traceable to ``report.json``), else the
    one serving the most queries. Without journeys, triage targets stand in
    (Aurora flavours matched to the ``aurora`` family) and the largest
    targeting signal is used.

    Several signals usually point at one engine, so the *smallest* of them --
    what this used to pick (#220) -- is the one least likely to be the reason.
    """
    reasons = ranking_row.get("assignment_reason_summary") or []
    if isinstance(reasons, str):
        reasons = [reasons]
    cited = {
        m.group(1)
        for r in reasons
        for m in _SIGNAL_OVERRIDE.finditer(str(r))
        if m.group(2) == engine
    }
    by_name = {str(x.get("signal")): x for x in signals}
    if assigned:
        candidates = []
        for s in q_signals:
            ids = by_name.get(s["name"], {}).get("query_ids") or []
            served = sum(1 for q in ids if assigned.get(str(q)) == engine)
            if served:
                candidates.append({**s, "served": served})
        candidates.sort(key=lambda s: (-s["served"], s["name"]))
    else:
        family = _triage_family(engine)
        candidates = [
            s
            for s in q_signals
            if family
            in {_triage_family(str(t)) for t in by_name.get(s["name"], {}).get("targets") or []}
        ]
    if not candidates:
        return None
    pool = [s for s in candidates if s["name"] in cited] or candidates
    return pool[0]


def _sequencing_rule_text(
    engines: list[dict[str, Any]], conf: dict[str, float], routed: bool = False
) -> str:
    """The Engine Confidence slide's statement of the wave rule ``derive()`` applies.

    No-migration steps (cache layer, retained engine) always form Wave 1 because
    they are reversible; ``CONFIDENCE_FLOOR`` only orders the migration targets.
    The slide used to state the floor as a universal rule ("anything under 50% is
    sequenced last") while Wave 1 held a 48% cache layer (#220). Every
    no-migration engine under the floor is named, so the exception is explicit;
    the migration-target clause is dropped when there are none.

    With ``routed`` (the report carries ``routed_confidence``, #152) the slide says
    what the figure is: the fit of the work each engine was given.
    """
    no_move = [e for e in engines if e["role"] in NO_MIGRATION_ROLES]
    has_targets = any(e["role"] == "Migration target" for e in engines)
    text = (
        "Confidence is the mean fit of the queries routed to each engine (for the cache "
        "layer, of the reads it fronts), not a forecast. "
        if routed
        else "Confidence is the assessment's own measure of evidence strength, not a forecast. "
    )
    if no_move:
        low = [e for e in no_move if conf.get(e["engine"], 0) < CONFIDENCE_FLOOR]
        named = (
            " ("
            + ", ".join(
                f"{ENGINE_LABEL.get(e['engine'], e['engine'])} at {conf.get(e['engine'], 0):.0f}%"
                for e in low
            )
            + ")"
            if low
            else ""
        )
        text += (
            f"Steps that need no data migration{named} go first at any confidence: "
            f"the source database stays authoritative, so they are reversible."
        )
    if has_targets:
        text += (
            f" Migration targets under {CONFIDENCE_FLOOR}% are sequenced last so they can be "
            f"re-scoped once the earlier waves have produced real measurements."
        )
    return text.strip()


def _confidence_phrase(text: str) -> str:
    """``93%`` -> ``93% confidence``; ``60% (signal only)`` -> ``60% confidence (signal only)``."""
    pct, _, note = text.partition(" (")
    return f"{pct} confidence" + (f" ({note}" if note else "")


def derive(rep: dict[str, Any], exp: dict[str, Any]) -> dict[str, Any]:
    """All deck content, derived from the two artifacts."""
    arch = rep.get("recommended_architecture") or {}
    risk = rep.get("risk_assessment") or {}
    assignment = rep.get("assignment_summary") or {}

    # ---- generation date: the report's own timestamp, not the wall clock -----
    # Only a report without a valid timestamp falls back to the current year, so
    # the footer's copyright year is never blank; an invalid date ("2026-02-30")
    # is not printed on the title slide.
    stamp = str(rep.get("timestamp") or "")
    m = re.match(r"\d{4}-\d{2}-\d{2}", stamp)
    try:
        gen_date: dt.date | None = dt.date.fromisoformat(m.group(0)) if m else None
    except ValueError:
        gen_date = None
    generated = gen_date.isoformat() if gen_date else ""
    year = gen_date.year if gen_date else dt.datetime.now(dt.UTC).year

    # ---- architecture, exactly as the HTML Decision Report computes it -------
    engines = _architecture_engines(rep)
    # The one wave source (#225): every deliverable, including
    # this deck, calls resolve_migration_waves -- never a second, on-the-fly split.
    stored_waves = resolve_migration_waves(rep)
    # #225: "0 source tables migrate" could contradict a wave that
    # clearly moves real tables, when no schema design exists yet to drive the
    # per-engine ``migrates`` count below. Prefer that richer, schema-design-based
    # figure when it has one (keeps every other count label, #257/#258,
    # unchanged); fall back to summing the tables a wave actually migrates
    # (moves_from non-empty — never the cache, which moves nothing, or the
    # search/retained waves, which don't move data either) only when it is 0.
    migrated = sum(e["migrates"] for e in engines if e["role"] == "Migration target") or sum(
        w.get("table_count", 0) for w in stored_waves if w.get("moves_from")
    )
    # The cache layer owns no query (#296): it is shown by the reads it fronts and
    # their share of calls, never by a share of the workload.
    cache = {
        e["engine"]: (e["cached_queries"], e["cached_call_share"])
        for e in engines
        if e.get("cached_queries") is not None
    }

    def share_text(eng: str) -> str:
        if eng in cache:
            return f"cache layer for {fmt_num(cache[eng][1], 1)}% of calls"
        return f"{workload.get(eng, 0):.1f}% of workload"

    ranking = [r for r in (rep.get("ranking") or []) if isinstance(r, dict)]
    # Routed confidence (#152): the fit of the queries routed to each engine, not
    # the average over every table it analyzed; a legacy report keeps the latter
    conf = {r.get("target"): engine_confidence(r) for r in ranking}
    # A signal-only fit is never shown as solid (#152): every confidence the deck
    # prints carries its evidence note ("60% (signal only — no table-level evidence)")
    by_target = {r.get("target"): r for r in ranking}

    def conf_text(eng: Any, short: bool = False) -> str:
        entry = by_target.get(eng)
        return confidence_text(entry, short=short) if entry else f"{conf.get(eng, 0):.0f}%"

    routed = any(r.get("routed_confidence") is not None for r in ranking)
    workload = {r.get("target"): float(r.get("workload_percent") or 0) for r in ranking}
    by_workload = sorted(
        ranking, key=lambda r: (-(r.get("workload_percent") or 0), str(r.get("target")))
    )

    # ---- risks --------------------------------------------------------------
    risks = sorted(filtered_risks(rep), key=lambda r: str(r.get("risk_id")))
    sev: dict[str, int] = {}
    for r in risks:
        k = _severity(r)
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
    # The Risk Profile table lists the highest severity present, so a run with no
    # HIGH risks still shows its top MEDIUM (or LOW) risks rather than an empty
    # table (#249). Within that severity, the risks touching the most queries first.
    shown_level = next((k for k in (*SEVERITY_ORDER, UNRATED) if sev.get(k)), "")
    shown_risks = sorted(
        (r for r in risks if _severity(r) == shown_level),
        key=lambda r: (-_risk_query_count(r), str(r.get("risk_id"))),
    )
    if shown_level == "HIGH":
        shown_risks = high  # unchanged: HIGH rows stay in risk-id order
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
    ranked_conf = sorted(ranking, key=lambda r: (engine_confidence(r), str(r.get("target"))))
    # The Confirm card goes to the weakest engine under the floor and, whatever its
    # number, to an engine whose routed fit has no table-level evidence (#152). One
    # card: the first candidate by confidence; the others are named on it.
    confirm = [
        r for r in ranked_conf if engine_confidence(r) < CONFIDENCE_FLOOR or is_signal_only(r)
    ]
    if confirm:
        weakest = confirm[0]
        eng = str(weakest.get("target") or "")
        # A truncated journey list holds only part of the workload, so counting
        # "routed to" from it would undercount: treat it as missing and let the
        # triage targets stand in (#220).
        journeys = exp.get("queryJourneys") or {}
        assigned = (
            {}
            if journeys.get("truncated")
            else {
                str(j.get("query_id")): str(
                    (j.get("assignment") or {}).get("assigned_engine") or ""
                )
                for j in (journeys.get("items") or [])
                if isinstance(j, dict)
            }
        )
        evidence = _evidence_text(
            _evidence_signal(eng, weakest, q_signals, signals, assigned),
            eng if assigned else None,
        )
        lead = (
            "No table-level evidence; confirm the requirement"
            if is_signal_only(weakest)
            else f"Lowest confidence of the {len(ranking)} {plural_noun(len(ranking), 'engine')}"
        )
        also = [ENGINE_LABEL.get(str(r.get("target")), str(r.get("target"))) for r in confirm[1:]]
        decisions.append(
            {
                "question": f"Confirm {ENGINE_LABEL.get(eng, eng)}?",
                "badge": (
                    f"{conf_text(eng)} confidence"
                    if not is_signal_only(weakest)
                    else conf_text(eng)
                ),
                "accent": ENGINE_COLOR.get(eng, ORANGE),
                "against": (
                    f"{lead} · {evidence} · {share_text(eng)}"
                    + (f" · also confirm {join_names(also)}" if also else "")
                ),
                "action": "Requirement validation pending.",
            }
        )
    if high_by_engine:
        eng = sorted(high_by_engine.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        decisions.append(
            {
                "question": f"Scope {ENGINE_LABEL.get(eng, eng)}?",
                "badge": f"{high_by_engine[eng]} of {len(high)} HIGH {plural_noun(len(high), 'risk')}",
                "accent": ENGINE_COLOR.get(eng, GREEN),
                "against": (
                    f"Carries the most HIGH risks · {conf_text(eng)} confidence · "
                    f"{share_text(eng)}"
                ),
                "action": "Scope defined as the matching table subset.",
            }
        )
    no_move = [e for e in engines if e["role"] in NO_MIGRATION_ROLES]
    if no_move:
        names = " + ".join(ENGINE_LABEL.get(e["engine"], e["engine"]) for e in no_move)
        pct = sum(workload.get(e["engine"], 0) for e in no_move if e["engine"] not in cache)
        no_move_high = sum(high_by_engine.get(e["engine"], 0) for e in no_move)
        cached_share = sum(cache[e["engine"]][1] for e in no_move if e["engine"] in cache)
        badge = (
            f"{pct:.0f}% of workload"
            if not cached_share
            else (
                f"{pct:.0f}% of workload + cache" if pct else f"{cached_share:.0f}% of calls cached"
            )
        )
        decisions.append(
            {
                "question": "Start with the no-migration step?",
                "badge": badge,
                "accent": BLUE,
                "against": (
                    f"{names} · no data migration · "
                    f"{no_move_high} HIGH {plural_noun(no_move_high, 'risk')}"
                ),
                "action": "Source database remains authoritative; step is reversible.",
            }
        )

    # ---- waves ----------------------------------------------------------------
    # Synthesis writes the incremental roadmap deterministically from the
    # assignment (#225) -- one sequencing rule: cache, then key-value/point
    # lookups, then any other direct target, then search/analytics read models
    # and document data, then whatever is retained on the source-compatible
    # relational engine. The deck only maps each resolved wave onto its own
    # slide fields; it never derives a second, on-the-fly split (#225 review
    # finding 10 — this used to disagree with the decision/engineering reports
    # for a report synthesized before #225; resolve_migration_waves already
    # falls back to the same shape they use, so the deck calls it too, like
    # every other deliverable).
    waves: list[dict[str, Any]] = []
    for sw in stored_waves:
        wave_keys = [str(e) for e in (sw.get("engines") or []) if e]
        if not wave_keys:
            continue
        keys = set(wave_keys)
        if sw.get("share_basis") == "calls" or keys & _WAVE_RETAINED_ENGINES:
            accent = BLUE
        elif keys & _WAVE_SEARCH_ENGINES:
            accent = GREEN
        else:
            accent = YELLOW
        is_cache_share = sw.get("share_basis") == "calls"
        share = float(sw.get("workload_share_percent") or 0.0)
        waves.append(
            {
                "engines": wave_keys,
                "accent": accent,
                "note": sw.get("rationale", ""),
                "gate": sw.get("gate", ""),
                # A wave moves no data only when it has nothing to move away from
                # (the cache, which only fronts reads; the search read model and
                # retained wave, which sync/stay rather than migrate).
                "no_migration": not sw.get("moves_from"),
                "workload": 0.0 if is_cache_share else share,
                "cached_share": share if is_cache_share else 0.0,
                "cached_queries": int(sw.get("query_count") or 0) if is_cache_share else 0,
            }
        )
    for w in waves:
        w["names"] = " + ".join(ENGINE_LABEL.get(e, e) for e in w["engines"])
        w["high"] = sum(high_by_engine.get(e, 0) for e in w["engines"])
        w["conf_text"] = conf_text(
            min(w["engines"], key=lambda e: (conf.get(e, 0), e)),
            short=True,
        )

    top = q_signals[0] if q_signals else None
    second = q_signals[1] if len(q_signals) > 1 else None
    return {
        "generated": generated,
        "year": year,
        "database": rep.get("database_name"),
        "job_id": rep.get("job_id"),
        "timestamp": stamp,
        "arch_type": str(arch.get("architecture_type") or "not determined").replace("_", " "),
        "risk_level": str(risk.get("overall_risk_level") or "not assessed"),
        "engines": engines,
        "migrated": migrated,
        "ranking": by_workload,
        "conf": conf,
        "conf_text": {t: conf_text(t, short=True) for t in conf},
        "routed_confidence": routed,
        "workload": workload,
        "cache": cache,
        "summary": prettify_engines(
            label_summary_counts(
                name_target_tables(strip_cost(str(rep.get("summary_deterministic") or ""))),
                rep,
            )
        ),
        "n_tables": len(rep.get("table_mappings") or []),
        # ": 20 migrate, 1 to the cache layer" (#258); says where the rest go (#296)
        "mapping_split": _mapping_split(
            rep, [m for m in rep.get("table_mappings") or [] if isinstance(m, dict)]
        ),
        "n_tradeoffs": len(rep.get("trade_offs") or []),
        "assignment": assignment,
        "risks": risks,
        "sev": sev,
        "high": high,
        "high_by_engine": high_by_engine,
        "shown_level": shown_level,
        "shown_risks": shown_risks,
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

    # Present the migration as WAVES, not a raw target-engine count: a headline
    # like "5 target engines" invites the "why would I move to 5 databases?"
    # objection, whereas the wave framing reads as a phased, lower-risk plan
    # (ADR-029 waves alignment). The wave breakdown is on the sequencing slide.
    n_waves = len(f.get("waves") or [])
    tiles = [
        (f"{f['n_patterns']:,}", f"query {plural_noun(f['n_patterns'], 'pattern')} analyzed", BLUE),
        (f"{n_waves}", f"migration {plural_noun(n_waves, 'wave')}", PURPLE),
        (
            f"{f['migrated']}",
            f"source {plural_noun(f['migrated'], 'table')} "
            f"{plural_verb(f['migrated'], 'migrates', 'migrate')}",
            GREEN,
        ),
        (
            f["risk_level"],
            f"overall risk, {len(f['risks'])} {plural_noun(len(f['risks']), 'item')}",
            _risk_accent(f["risk_level"]),
        ),
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
                (
                    f"{fmt_num(e['cached_call_share'], 1)}% calls"
                    if e.get("cached_queries") is not None
                    else f"{wl:.1f}%" if isinstance(wl, (int, float)) else "—"
                ),
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
    # Only engines that actually keep serving workload. Tested positively against the
    # two roles that do, not negatively against "Migration target": an "Evaluated"
    # engine (assignment routed it nothing) also fails that negative test, and naming
    # it here claimed it keeps a share of the workload it does not carry.
    kept = [
        e
        for e in f["engines"]
        if e["role"] in NO_MIGRATION_ROLES and e.get("cached_queries") is None
    ]
    kept_pct = sum(e.get("workload") or 0 for e in kept)
    caches = [e for e in f["engines"] if e.get("cached_queries")]
    # The summary beside this footer says "22 source tables mapped"; when some of
    # them map to the cache layer, the footer says how 21 relates to 22 (#258).
    n_mig = f["migrated"]
    split = str(f.get("mapping_split") or "").lstrip(": ")
    subject = (
        f"{n_mig} of the {f['n_tables']} mapped source tables" + (f" ({split})" if split else "")
        if f["n_tables"] > n_mig > 0
        else f"{n_mig} source {plural_noun(n_mig, 'table')}"
    )
    footer_note(
        s,
        f"{subject} {plural_verb(n_mig, 'moves', 'move')} to a purpose-built engine; "
        + (
            f"{join_names([ENGINE_LABEL.get(e['engine'], e['engine']) for e in kept])} "
            f"{plural_verb(len(kept), 'keeps', 'keep')} {kept_pct:.1f}% of the workload "
            f"with no data migration. "
            if kept
            else ""
        )
        + "".join(
            f"{ENGINE_LABEL.get(e['engine'], e['engine'])} caches {e['cached_queries']} hot "
            f"{plural_noun(e['cached_queries'], 'read')} "
            f"({fmt_num(e['cached_call_share'], 1)}% of calls) and owns none of the workload. "
            for e in caches
        )
        + "Full detail in the Decision and Engineering reports."
        + (
            f" {SHARED_TABLES_NOTE}"
            if any(SHARED_TABLES_LABEL in str(e["scope"]) for e in f["engines"])
            else ""
        ),
        t=5.92,
        color=MUTED,
        size=9.5,
    )
    return s


def slide_evidence(prs, f):
    s = add_slide(prs, LAYOUT_CONTENT)
    a = f["assignment"]
    set_title(s, "Workload Coverage and Engine Assignment")
    query_count = a.get("query_count", 0)
    set_subtitle(
        s,
        f"{query_count:,} production query {plural_noun(query_count, 'pattern')} analyzed, "
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
        assigned = r.get("assigned_queries", 0)
        if eng in f["cache"]:
            n_c, share_c = f["cache"][eng]
            line = (
                f"{ENGINE_LABEL.get(eng, eng)}   cache layer   ·   {n_c:,} cached "
                f"{plural_noun(n_c, 'read')}   ·   {fmt_num(share_c, 1)}% of calls"
            )
        else:
            line = (
                f"{ENGINE_LABEL.get(eng, eng)}   {f['workload'].get(eng, 0):.1f}%   ·   "
                f"{assigned:,} {plural_noun(assigned, 'query', 'queries')}   ·   "
                + _confidence_phrase(f["conf_text"].get(eng, "0%"))
            )
        para(tf, line, size=11.5, first=True)

    card(s, 6.60, 2.72, 5.5, 2.10, BLUE)
    tf = textbox(s, 6.85, 2.86, 5.05, 1.85)
    para(tf, "Basis of the analysis", size=11.0, bold=True, color=BLUE, first=True)
    co_dep = a.get("co_dependency_groups", 0)
    for line in (
        f"{a.get('in_scope_count', 0):,} of {query_count:,} query "
        f"{plural_noun(query_count, 'pattern')} in scope",
        f"{f['n_tables']} {plural_noun(f['n_tables'], 'table')} mapped to a named target, each "
        "with its own confidence score",
        f"{co_dep} co-dependency {plural_noun(co_dep, 'group')} — table sets that must "
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
    para(
        tf,
        f"OPERATION MIX  ({n:,} {plural_noun(n, 'pattern')})",
        size=9.5,
        bold=True,
        color=MUTED,
        first=True,
    )
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
    # Triage signals count the whole workload, before assignment; the Engine
    # Confidence slide counts what was routed to one engine (#256).
    para(
        tf,
        "WHAT THOSE QUERIES ARE DOING  ·  WHOLE WORKLOAD",
        size=9.5,
        bold=True,
        color=MUTED,
        first=True,
    )
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
        f"{f['fan1']:,} {plural_noun(f['fan1'], 'query', 'queries')} "
        f"{plural_verb(f['fan1'], 'touches', 'touch')} a single table "
        f"({f['fan1'] / f['n_patterns'] * 100:.1f}%). Only {f['fan3']:,} touch three or more "
        f"({f['fan3'] / f['n_patterns'] * 100:.1f}%), and just {f['n_max_fan']} "
        f"{plural_verb(f['n_max_fan'], 'reaches', 'reach')} {f['max_fan']}.",
        size=11.0,
        color=PAPER,
    )

    card(s, 6.45, 4.85, 5.65, 1.22, PURPLE)
    tf = textbox(s, 6.68, 4.94, 5.3, 1.1)
    para(tf, "Throughput distribution", size=10.5, bold=True, color=PURPLE, first=True)
    para(
        tf,
        f"{f['total_cps']:,.1f} queries/sec across the whole database, "
        f"{f['read_share']:.1f}% reads. The top 10 patterns carry "
        f"{f['top10_share']:.1f}% of throughput; the busiest single query runs at "
        f"{f['busiest']:,.1f} rps.",
        size=11.0,
        color=PAPER,
    )
    return s


def _worst_engine_sentence(worst: tuple[str, int], n_high: int) -> str:
    """ "{worst[1]} of the {n_high} HIGH risk(s) sit(s)/sits on {engine} alone."

    Extracted so the verb-agreement regression has a direct unit test: the
    verb must agree with ``worst[1]`` (the count sitting on *this* engine --
    the sentence's subject), not with ``n_high`` (the sentence's other
    number, the total HIGH risk count across every engine).
    """
    eng, count = worst
    return (
        f"{count} of the {n_high} HIGH {plural_noun(n_high, 'risk')} "
        f"{plural_verb(count, 'sits', 'sit')} on {ENGINE_LABEL.get(eng, eng)} alone."
    )


def _root_cause_sentence(n_high: int, high_type: str) -> str:
    """ "{the|all} {n_high} HIGH risk(s) is/are {high_type}".

    "all 1 HIGH risk is ..." reads oddly -- "all" implies more than one;
    "the 1 HIGH risk is ..." is the natural singular phrasing.
    """
    determiner = "the" if n_high == 1 else "all"
    return (
        f"{determiner} {n_high} HIGH {plural_noun(n_high, 'risk')} "
        f"{plural_verb(n_high, 'is', 'are')} {high_type}"
    )


def slide_decisions(prs, f):
    s = add_slide(prs, LAYOUT_CONTENT)
    d = f["decisions"]
    set_title(s, "Engine Confidence and Open Decisions")
    weak = min(f["conf"].items(), key=lambda kv: (kv[1], kv[0])) if f["conf"] else None
    n_ranking = len(f["ranking"])
    engines_word = plural_noun(n_ranking, "engine")
    set_subtitle(
        s,
        (
            f"{n_ranking} recommended {engines_word}  ·  lowest confidence "
            f"{ENGINE_LABEL.get(weak[0], weak[0])} at "
            f"{f['conf_text'].get(weak[0], f'{weak[1]:.0f}%')}"
            if weak
            else f"{n_ranking} recommended {engines_word}"
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
                (
                    f"{fmt_num(f['cache'][eng][1], 1)}% calls"
                    if eng in f["cache"]
                    else f"{f['workload'].get(eng, 0):.1f}%"
                ),
                f["conf_text"].get(eng, f"{c:.0f}%"),
                str(f["high_by_engine"].get(eng, 0)),
                role,
            )
        )
        emph[(i, 0)] = ENGINE_COLOR.get(eng, PAPER)
        if c < CONFIDENCE_FLOOR or "(" in f["conf_text"].get(eng, ""):
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
        _sequencing_rule_text(f["engines"], f["conf"], routed=f["routed_confidence"]),
        size=11.0,
        color=WHITE,
        first=True,
        space_after=8,
    )
    if f["high_by_engine"]:
        worst = sorted(f["high_by_engine"].items(), key=lambda kv: (-kv[1], kv[0]))[0]
        n_high = len(f["high"])
        para(
            tf,
            _worst_engine_sentence(worst, n_high),
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
        f"{f['sev'][k]} {k.lower() if k == UNRATED else k}"
        for k in (*SEVERITY_ORDER, UNRATED)
        if f["sev"].get(k)
    )
    n_risks = len(f["risks"])
    sub = f"{n_risks} {plural_noun(n_risks, 'risk')}: {mix}"
    if f["one_root_cause"]:
        sub += f"  ·  {_root_cause_sentence(n_high, f['high_type'])}"
    set_subtitle(s, sub)

    # Same colours as the table's Engine column (_risk_accent). Up to three rows
    # keep the old spacing; more are packed so they stay clear of the BY TYPE card.
    present = [(k, _risk_accent(k)) for k in (*SEVERITY_ORDER, UNRATED) if f["sev"].get(k)]
    step = 0.50 if len(present) <= 3 else 1.20 / len(present)
    top_n = max((f["sev"].get(k, 0) for k, _ in present), default=1) or 1
    for i, (k, colour) in enumerate(present):
        cnt = f["sev"][k]
        y = BODY_TOP + 0.10 + i * step
        tf = textbox(s, 0.67, y - 0.04, 1.05, 0.32)
        para(tf, k, size=11.0, bold=True, color=colour, first=True)
        bar(s, 1.75, y, max(2.9 * cnt / top_n, 0.05), 0.24, colour)
        tf = textbox(s, 4.78, y - 0.05, 0.9, 0.32)
        para(tf, str(cnt), size=12.0, bold=True, color=PAPER, first=True)

    card(s, 0.67, 3.30, 5.05, 1.05, MUTED)
    tf = textbox(s, 0.90, 3.41, 4.7, 0.9)
    para(tf, "BY TYPE", size=9.5, bold=True, color=MUTED, first=True)
    para(tf, "  ·  ".join(f"{c} {t}" for t, c in f["by_type"][:3]), size=11.5, color=PAPER)

    # Each row carries its own mitigation (#249), clipped; a mitigation that only
    # restates the risk is not repeated.
    rows = [("#", "Engine", "What it is", "Mitigation", "Queries")]
    for r in f["shown_risks"][:4]:
        desc, _ = clean_risk_text(str(r.get("description") or ""))
        n_q = _risk_query_count(r)
        eng = risk_engine(str(r.get("description") or "")) or "(general)"
        what, mit = split_risk_text(desc, str(r.get("mitigation") or ""))
        rows.append(
            (
                str(r.get("risk_id") or ""),
                ENGINE_LABEL.get(eng, eng),
                clip(what, 66),
                clip(mit, MITIGATION_MAX_CHARS) if mit else "—",
                str(n_q) if n_q else "",
            )
        )
    table(
        s,
        6.05,
        BODY_TOP,
        6.05,
        rows,
        col_w=[0.80, 0.95, 1.80, 1.75, 0.75],
        head_size=10.0,
        body_size=9.0,
        row_h=0.56,
        head_h=0.32,
        emphasis={(i, 1): _risk_accent(f["shown_level"]) for i in range(1, len(rows))},
    )

    card(s, 0.67, 5.05, 11.43, 1.05, GREEN)
    tf = textbox(s, 0.90, 5.17, 11.0, 0.9)
    para(
        tf,
        _risk_caption(f["shown_level"], min(len(f["shown_risks"]), 4))
        + (
            " The full mitigation of every risk is in the Engineering Report."
            if f["shown_risks"]
            else ""
        ),
        size=12.0,
        color=WHITE,
        first=True,
    )
    return s


def _wave_card_text(rationale: str, gate: str, box_w: float, box_h: float) -> tuple[str, str]:
    """The rationale/gate text that fits a Migration Sequencing card's text box.

    Clips the rationale to about one sentence and the gate to about one line
    (#225); if both together still estimate taller than ``box_h``, drops the
    rationale and keeps only the gate (the alternative the review offered),
    shrinking the gate further as a last resort. ``_estimated_text_height_in``
    is an approximation, not exact PowerPoint layout, but catches the case
    that actually happened: real wordpress/discourse rationale/gate text
    (270-370 characters) overflowing a fixed-height card.
    """
    rationale_text = clip(rationale, 110) if rationale else ""
    gate_text = f"Gate: {clip(gate, 90)}" if gate else ""
    r_h = _estimated_text_height_in(rationale_text, 10.0, box_w)
    g_h = _estimated_text_height_in(gate_text, 9.0, box_w)
    if rationale_text and gate_text and r_h + g_h > box_h:
        rationale_text = ""
    if gate_text:
        budget = 90
        while _estimated_text_height_in(gate_text, 9.0, box_w) > box_h and budget > 20:
            budget -= 20
            gate_text = f"Gate: {clip(gate, budget)}"
    return rationale_text, gate_text


def slide_sequencing(prs, f):
    s = add_slide(prs, LAYOUT_CONTENT)
    waves = f["waves"]
    set_title(s, "Migration Sequencing")
    first_pct = waves[0]["workload"] if waves else 0.0
    first_cache = waves[0].get("cached_share", 0.0) if waves else 0.0
    # #225: "no data migration" is only ever true of the resolved
    # wave 1 itself (the cache, or — without one — whatever is retained); when
    # wave 1 is a real migration target (e.g. DynamoDB with no cache overlay),
    # say so instead of repeating the no-migration claim unconditionally.
    first_no_migration = bool(waves[0].get("no_migration")) if waves else False
    if first_cache and not first_pct:
        n_c = waves[0]["cached_queries"]
        subtitle = (
            f"Wave 1 puts the cache layer for {n_c} hot {plural_noun(n_c, 'read')} "
            f"({fmt_num(first_cache, 1)}% of calls) in front of the current source database, "
            "with no data migration"
        )
    elif first_cache:
        subtitle = (
            f"Wave 1 covers {first_pct:.1f}% of the workload, plus the cache layer for "
            f"{fmt_num(first_cache, 1)}% of calls, with no data migration"
        )
    elif first_no_migration:
        subtitle = f"Wave 1 covers {first_pct:.1f}% of the workload with no data migration"
    else:
        subtitle = f"Wave 1 covers {first_pct:.1f}% of the workload, the first data migration"
    set_subtitle(s, subtitle)

    # #225: up to 6 waves (cache, KV, other, search, document,
    # retained) must still fit above the constraints card on one slide. 4 waves
    # keep the original, unshrunk sizing; more waves shrink the step so the
    # constraints card never runs off the 7.5" slide.
    n = len(waves) or 1
    constraints_block = 1.06  # constraints card height (0.98) + its own margin (0.08)
    bottom_margin = 0.18
    y0 = BODY_TOP - 0.02
    step = 1.00 if n <= 4 else max(0.62, (7.50 - y0 - constraints_block - bottom_margin) / n)
    card_h = min(0.92, step - 0.08)

    y = y0
    for i, w in enumerate(waves):
        accent = w["accent"]
        card(s, 0.67, y, 11.43, card_h, accent)
        tf = textbox(s, 0.92, y + 0.06, 1.3, 0.35)
        para(tf, f"WAVE {i + 1}", size=13.0, bold=True, color=accent, first=True, font=FONT_HEAD)
        tf = textbox(s, 2.25, y + 0.05, 4.4, 0.35)
        para(tf, w["names"], size=13.0, bold=True, color=WHITE, first=True)
        stats = (
            (
                f"{fmt_num(w['cached_share'], 1)}% calls cached"
                if w.get("cached_share") and not w["workload"]
                else f"{w['workload']:.1f}% query patterns"
            ),
            # A labelled figure ("60% (signal only)") fills the cell on its own
            w["conf_text"] if "(" in w["conf_text"] else f"{w['conf_text']} confidence",
            f"{w['high']} HIGH {plural_noun(w['high'], 'risk')}",
        )
        for j, val in enumerate(stats):
            tf = textbox(s, 6.75 + j * 1.80, y + 0.06, 1.75, 0.33)
            para(tf, val, size=11.5, bold=(j == 1), color=PAPER if j != 1 else accent, first=True)
        # #225: every wave shows its own gate, and the text is clipped (or the
        # rationale dropped) to fit the card.
        box_w, box_h = 9.6, card_h - 0.44
        rationale_text, gate_text = _wave_card_text(w["note"], w.get("gate", ""), box_w, box_h)
        tf = textbox(s, 2.25, y + 0.44, box_w, max(box_h, 0.01))
        # No top/bottom inset: the text area is the full box _wave_card_text budgets for.
        tf.margin_top = tf.margin_bottom = 0
        if rationale_text:
            para(tf, rationale_text, size=10.0, color=MUTED, first=True, space_after=1)
        if gate_text:
            para(tf, gate_text, size=9.0, bold=True, color=PINK, first=not rationale_text)
        y += step

    card(s, 0.67, y + 0.08, 11.43, 0.98, GREEN)
    tf = textbox(s, 0.90, y + 0.17, 11.0, 0.85)
    para(tf, "Sequencing constraints", size=10.5, bold=True, color=GREEN, first=True, space_after=2)
    co_dep = f["assignment"].get("co_dependency_groups", 0)
    n_tradeoffs = f["n_tradeoffs"]
    para(
        tf,
        f"{co_dep} co-dependency {plural_noun(co_dep, 'group')} "
        f"{plural_verb(co_dep, 'constrains', 'constrain')} wave boundaries; "
        f"{n_tradeoffs} {plural_noun(n_tradeoffs, 'trade-off')} "
        f"{plural_verb(n_tradeoffs, 'is', 'are')} documented in the Engineering "
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
        publishes best-effort and logs; see
        ``src.report.deliverables.render_deliverables``.
    """
    f = derive(report, export_data or {})
    prs = open_deck(keep=1)
    retitle_intro(prs.slides[0], f)
    set_footer_year(prs, f["year"])
    for build in SLIDES:
        build(prs, f)
    cp = prs.core_properties
    cp.title = f"{DECK_NAME} — {f['database']}"
    cp.author = DECK_NAME
    cp.last_modified_by = DECK_NAME
    # The report's timestamp, not the wall clock: re-rendering the same job must
    # not change the file's metadata either.
    cp.comments = f"{DECK_NAME} — job {f['job_id']} — {f['timestamp']}"
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
