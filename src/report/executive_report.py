"""Executive report -- the landscape C-suite briefing PDF (#1020).

``executive-report.pdf`` is a short landscape deck for the people deciding
whether to proceed: one idea per page, big numbers, cards and the target
architecture as a picture. It is built straight from the synthesis
``report.json`` (ADR-032): every figure comes from the facts layer
(``src.report.facts.report_facts`` and ``tco_analysis``) and every sentence is a
string the facts layer or another deliverable already words; only the headings
and the short labels are new. This module formats; it never counts, totals or
filters a figure the facts layer owns.

Pages, in order: the cover, the recommendation (target architecture with each
engine's role and workload share), the numbers (projected monthly cost, engines
kept and removed, queries moved), why (each engine's deciding reason, one line
each, and the reality check's decisions), the migration waves (counts and the
reason per wave), the risks, the decisions needed (pending engine choices) and
the next steps, which close with a pointer to the engineering report and the
folder index. There is no traceability appendix and no table list: a table name
never appears (``without_table_names``); the engineering report carries every
table, query and trace.

Layout: reportlab platypus on a 13.33 x 7.5 inch page in the retired deck's
design (its palette, Amazon Ember, dark cards with an accent rule, big-number
tiles). Every block is measured before it is placed: text wraps, a big number
shrinks to fit its tile, and a page that runs out of room continues on the next
one under the same title, marked "(continued)". Nothing is truncated or drawn
outside the page.

Determinism: reportlab runs in ``invariant`` mode (no creation date, no random
document id) and every string drawn comes from the report or is a constant, so
one ``report.json`` yields byte-identical PDFs.

Fonts: Amazon Ember Regular and Bold ship in ``assets/fonts``. If a face is
unavailable the module falls back to Helvetica so a PDF is still produced.
"""

from __future__ import annotations

import datetime as dt
import io
import logging
import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from reportlab.lib.colors import Color, HexColor
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont, TTFontFile
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    BaseDocTemplate,
    Flowable,
    Frame,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from src.shared import catalog
from src.shared.engine_names import display_engine
from src.shared.report_facts import CACHE_LAYER
from src.shared.risk_severity import severity_of
from src.shared.target_databases import engine_scope_statement
from src.shared.workload_fit_labels import CACHE_FIT_DEFINITION, WORKLOAD_FIT_DEFINITION

from .architecture import _architecture_engines
from .cache_layer import cache_layer_text
from .engine_choices import (
    AUTO_DECIDED_NOTE,
    PENDING_CHOICES_NOTE,
    REROUTE_OVERRIDE_TEXT,
    reroute_summary,
)
from .engineering_report import RISK_RULE_NOTE, _engine_role_notes_md
from .facts import report_facts
from .formatting import (
    _cost_baseline_text,
    _fmt_usd,
    _retired_in_use_lines,
    cost_baseline_known,
    fmt_num,
    plural_noun,
    plural_verb,
)
from .query_patterns import query_pattern_count_text
from .relational_gaps import heterogeneous_wave, wave_gap_groups
from .risks import (
    NO_OPEN_RISKS_CAVEAT,
    _repeats,
    _risk_engine_and_body,
    _severity_counts_text,
    filtered_risks,
    resolved_risk_label,
    resolved_risks,
)
from .roadmap import _SUGGESTED_PATH_NOTE, _moves_from_label, _wave_headline_stats
from .roadmap import resolve_migration_waves as _resolve_waves
from .table_owners import OWNER_RULE_SHORT, ownership_facts, ownership_summary_text
from .unreviewed import unreviewed_moves_line
from .unservable import (
    UNSERVABLE_SCOPE_SOME,
    cost_exclusion_text,
    headline_query_total,
    no_engine_clause,
    placed_share_note,
    unservable_intro,
    unservable_kind_text,
    unservable_rerun_lines,
    unservable_title,
)

logger = logging.getLogger(__name__)

FILENAME = "executive-report.pdf"
FONT_DIR = Path(__file__).parent / "assets" / "fonts"

# The deliverable's only product name.
PRODUCT_NAME = "Database Modernizer Assessment Tool"
REPORT_NAME = "Executive Report"
# The copyright line of the repository's LICENSE file, word for word
# (tests/unit/report/test_executive_report.py reads LICENSE and compares).
COPYRIGHT = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
FOOTER = f"{COPYRIGHT} For informational purposes only."
# Where the briefing sends readers for the detail: real files in the job's
# deliverables folder (src.report.deliverables.FOLDER_ENTRIES), by their names.
CLOSING_POINTER = "Full detail: engineering-report.md and index.md"
# The tied-query sentence of the engineering report's "Unresolved engine ties".
TIED_QUERIES_NOTE = (
    "No routing rule decides between the engines listed for each of these queries; each is "
    "listed under a deterministic placeholder pending the customer's own choice."
)

# Page titles, in document order. Every page carries one; a page that continues a
# section repeats its title with CONTINUED appended.
H_RECOMMENDATION = "The recommendation"
H_NUMBERS = "The numbers"
H_WHY = "Why this architecture"
H_WAVES = "Migration waves"
# #1120: where the reasons behind the gap list are (they name target functions).
L_GAP_REASONS = (
    "What to do about each gap, and where it was found: the engineering report's "
    "Migration roadmap (wave 1) and Risk register."
)
H_RISKS = "Risks"
H_DECISIONS = "Decisions needed"
H_NEXT = "Next steps"
SECTION_HEADINGS = (H_RECOMMENDATION, H_NUMBERS, H_WHY, H_WAVES, H_RISKS, H_DECISIONS, H_NEXT)
CONTINUED = " (continued)"

# Labels (new wording is limited to headings and labels like these).
L_COST = "Projected monthly cost"
L_KEPT = "Engines in the target architecture"
L_REMOVED = "Removed by the reality check"
L_TABLE_OWNERS = "Table owners"
L_NONE = "None"
L_MOVED = "move to a new engine"
L_OVERALL = "Overall risk"
L_OPEN = "Open risks"
# A risk whose items the engineering report lists in full (``Risk.details``) and this
# report leaves out (#1149 review: the sentence holds only here).
RISK_DETAILS_POINTER = "The engineering report lists each one in full."
L_RESOLVED = "Resolved by the assignment"
L_NO_DECISIONS = "No pending engine choices."
L_DECISIONS_REALITY = "Reality-check decisions"
L_COST_BREAKDOWN = "Cost by engine"
L_ASSUMPTIONS = "Assumptions"
L_TIED_BETWEEN = "Tied between"
L_PLACED_ON = "Placed on"
L_NOT_REVIEWED = "Not reviewed"

# ---------------------------------------------------------------------------
# design: the retired deck's own values (13.33 x 7.5 in, Amazon Ember, dark theme)
# ---------------------------------------------------------------------------
PAGE_W, PAGE_H = 960.0, 540.0
MARGIN_X = 48.0
FRAME_TOP = PAGE_H - 110.0
FRAME_BOTTOM = 58.0
FRAME_W = PAGE_W - 2 * MARGIN_X
FRAME_H = FRAME_TOP - FRAME_BOTTOM
TITLE_BASELINE = PAGE_H - 70.0
TITLE_SIZES = (30.0, 28.0, 26.0, 24.0, 22.0, 20.0, 18.0)
BIG_SIZES = (26.0, 24.0, 22.0, 20.0, 18.0, 16.0, 14.0, 12.0)

BG = HexColor("#262626")  # the template's dk1 at lumMod 85% / lumOff 15%
INK = HexColor("#232F3E")  # card fill
PAPER = HexColor("#F1F3F3")
WHITE = HexColor("#FFFFFF")
MUTED = HexColor("#9BA7B6")
TBL_HEAD = HexColor("#7C59ED")
BLUE = HexColor("#41B3FF")
PURPLE = HexColor("#AD5CFF")
GREEN = HexColor("#00E500")
PINK = HexColor("#FF5C85")
ORANGE = HexColor("#FF693C")
YELLOW = HexColor("#FBD332")

# Each engine's colour, the rule catalog's one palette (``colors.ui``): the UI draws the
# engine in the same colour (#1246). Grey for an id the catalog does not know. Some of
# these colours are too dark to read as text on the report's dark cards, so the report
# uses them for marks only (bar segments, a card's accent rule, the rule beside an
# engine's table row) and writes engine names in white.
ENGINE_COLOR: dict[str, Color] = {e.id: HexColor(e.colors.ui) for e in catalog.engines()}
SEVERITY_COLOR: dict[str, Color] = {
    "CRITICAL": ORANGE,
    "HIGH": PINK,
    "MEDIUM": YELLOW,
    "LOW": GREEN,
}

_FALLBACK = "Helvetica"
_FALLBACK_BOLD = "Helvetica-Bold"
_FONTS: dict[bool, str] = {}
_FONT_FILES = {False: "AmazonEmber_Rg.ttf", True: "AmazonEmber_Bd.ttf"}
FAMILY = "Ember"


def register_fonts() -> None:
    """Register the bundled TTFs once. Missing files degrade to Helvetica."""
    if _FONTS:
        return
    for bold, filename in _FONT_FILES.items():
        path = FONT_DIR / filename
        name = f"Ember-{'Bd' if bold else 'Rg'}"
        if not path.exists():
            logger.warning("Font %s missing; falling back to %s", path.name, _FALLBACK)
            _FONTS[bold] = _FALLBACK_BOLD if bold else _FALLBACK
            continue
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, str(path)))
        _FONTS[bold] = name
    pdfmetrics.registerFontFamily(
        FAMILY,
        normal=_FONTS[False],
        bold=_FONTS[True],
        italic=_FONTS[False],
        boldItalic=_FONTS[True],
    )


def font_for(bold: bool = False) -> str:
    """The registered reportlab font for a weight (Ember, else Helvetica)."""
    register_fonts()
    return _FONTS[bold]


_GLYPHS: list[set[int] | None] = []


def _glyphs() -> set[int] | None:
    """Code points the regular face can draw; None when running on the fallback font."""
    if not _GLYPHS:
        path = FONT_DIR / _FONT_FILES[False]
        _GLYPHS.append(set(TTFontFile(str(path)).charToGlyph) if path.exists() else None)
    return _GLYPHS[0]


_ARROW = re.compile(r"\s*→\s*")


def printable(text: Any) -> str:
    """``text`` with what the bundled font cannot draw replaced, never dropped.

    Ember has no arrow (U+2192), which would render as an empty box; the readable
    form of "A -> B" is "A to B". Any other character without a glyph becomes "?"
    so the reader sees that something stood there.
    """
    s = _ARROW.sub(" to ", str(text))
    glyphs = _glyphs()
    if glyphs is None:
        return s
    return "".join(ch if (ord(ch) in glyphs or ch in "\n\t ") else "?" for ch in s)


def _xml(text: Any) -> str:
    return printable(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_MD_ESCAPE = re.compile(r"\\(.)")
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")


def _md_markup(text: str) -> str:
    """Paragraph markup for the inline Markdown ``escaping.md_text`` and the
    engineering report's notes emit: backslash escapes and ``**bold**``."""
    parts = _MD_BOLD.split(text)
    out = []
    for i, part in enumerate(parts):
        plain = _xml(_MD_ESCAPE.sub(r"\1", part))
        out.append(f"<b>{plain}</b>" if i % 2 else plain)
    return "".join(out)


# ---------------------------------------------------------------------------
# no identifiers, no SQL (#1020): the briefing never names a table, view, routine,
# trigger or column, and never quotes SQL
# ---------------------------------------------------------------------------
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
# A full stop that ends an abbreviation, not a sentence ("e.g.", "incl.", "Dr.").
_ABBREVIATION = re.compile(
    r"(?:\b(?:e\.g|i\.e|etc|vs|cf|approx|incl|esp|Mr|Mrs|Ms|Dr|No|Nos|St|Inc|Ltd|Jr|Sr)"
    r"|(?<![\w.])[A-Z])\.$"
)
_PLACEHOLDER_END = re.compile(r"(?:[=,(<>]|\bin|\blike)\s*\?$", re.IGNORECASE)
_PARENTHETICAL = re.compile(r"\s*\([^()]*\)")
# "function: api_key_auth_check — ..." in a model-written risk: the kind label and the
# name go, the rest of the sentence stays.
_KIND_PREFIX = re.compile(
    r"\b(?:tables?|views?|functions?|procedures?|triggers?|index(?:es)?|columns?)\s*:\s*"
    r"[`\"\[]?[\w.$]+[`\"\]]?\s*(?:[—–]|-{1,2})\s*",
    re.IGNORECASE,
)
# SQL, in any case: a statement's shape, a predicate, a starred call, a grouping.
_SQL_SHAPE = re.compile(
    r"\bselect\b[^.;]*?\bfrom\b|\binsert\s+into\b|\bdelete\s+from\b|\bupdate\s+\S+\s+set\b"
    r"|\bwhere\s+\S+\s*(?:=|<>|!=|<=|>=|<|>|\bin\b|\blike\b|\bis\b)|\b\w+\s*\(\s*\*\s*\)"
    r"|\bis\s+(?:not\s+)?null\b|\b(?:group|order|partition)\s+by\b"
    r"|[\w.$]+\s*(?:=|<>|!=|<=|>=)\s*(?:\?|\$\d+|:\w+|'[^']*'|\d+(?![\w.]))",
    re.IGNORECASE,
)
# SQL as it is quoted: a backtick, a bracketed or double-quoted identifier, a keyword
# in capitals (in lower case these are ordinary words: "moves from", "where").
_SQL_QUOTED = re.compile(
    r"`|\[\w[^\]]*\]|\"[\w.$]+\"|\b(?:SELECT|FROM|WHERE|JOIN|HAVING|LIMIT|OFFSET|DISTINCT|VALUES)\b"
)
# An identifier whatever list it is on: a word with an inner underscore (snake_case),
# as a schema, table, view, routine or column name is written ("revoked_at").
_SNAKE = r"[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]*[A-Za-z0-9]"
# A context that makes any word an identifier: quoted, schema-qualified, after a SQL
# keyword in capitals, or after a kind label ("table: users").
_CONTEXT = (
    r"[`\"\[]\s*(?:{n})\s*[`\"\]]|[\w$]+\.(?:{n})(?![\w$])"
    r"|\b(?-i:FROM|JOIN|INTO|UPDATE)\s+(?:{n})(?![\w$])"
    r"|\b(?:table|view|function|procedure|trigger|column)\s*:\s*(?:{n})(?![\w$])"
)
_DISTINCTIVE = re.compile(r"[_\d$.]|[a-z][A-Z]")


def _add_name(names: set[str], value: Any) -> None:
    if isinstance(value, str) and len(value.strip()) > 1:
        names.add(value.strip())
        names.add(value.strip().rsplit(".", 1)[-1])


def table_names(report: dict[str, Any]) -> set[str]:
    """Every table and view name the report itself names: each wave's tables
    (wave 1 carries the whole collected schema), their owners and groups, the table
    mappings and the risks' qualified affected tables; each qualified and bare."""
    names: set[str] = set()
    for w in _resolve_waves(report) or []:
        for t in w.get("tables") or []:
            _add_name(names, t)
        for o in w.get("table_owners") or []:
            if isinstance(o, dict):
                _add_name(names, o.get("table"))
        for g in w.get("table_groups") or []:
            for t in (g.get("tables") or []) if isinstance(g, dict) else []:
                _add_name(names, t)
    for m in report.get("table_mappings") or []:
        if isinstance(m, dict):
            _add_name(names, m.get("source_table"))
    # A risk's affected tables, qualified ones only: an older report listed the SQL
    # parser's CTE aliases and keywords there too ("cte", "the", "SET").
    risk = report.get("risk_assessment") or {}
    for key in ("risks", "resolved_risks"):
        for r in risk.get(key) or []:
            for t in (r.get("affected_tables") or []) if isinstance(r, dict) else []:
                if isinstance(t, str) and "." in t:
                    _add_name(names, t)
    return {n for n in names if len(n) > 1}


def collected_identifiers(collector_output: dict[str, Any] | None) -> set[str]:
    """Every name the collector found: tables, views, procedures and functions,
    triggers and columns, by id, bare and schema-qualified name."""
    schema = (collector_output or {}).get("database_schema") or {}
    names: set[str] = set()
    kinds = (("tables", "table"), ("views", "view"), ("procedures", "procedure"))
    for key, kind in (*kinds, ("triggers", "trigger")):
        for obj in schema.get(key) or []:
            if not isinstance(obj, dict):
                continue
            _add_name(names, obj.get(f"{kind}_id"))
            _add_name(names, obj.get(f"{kind}_name"))
            if obj.get("schema_name") and obj.get(f"{kind}_name"):
                _add_name(names, f"{obj['schema_name']}.{obj[f'{kind}_name']}")
            for col in obj.get("columns") or []:
                if isinstance(col, dict):
                    _add_name(names, col.get("column_name"))
    return names


def _names_pattern(names: Iterable[str]) -> re.Pattern[str]:
    """One case-insensitive matcher for every identifier in ``names`` and every
    snake_case word: a distinctive name (an underscore, a digit, a dot or an inner
    capital) wherever it stands, with or without a schema prefix; a plain word
    (``users``, ``Product``, ``group``) only where it reads as an identifier (quoted,
    schema-qualified, after FROM or JOIN in capitals, or after "table:" and the like),
    since as a word it is ordinary prose ("1 table group")."""
    distinct = sorted({re.escape(n) for n in names if _DISTINCTIVE.search(n)}, key=len)[::-1]
    plain = sorted({re.escape(n) for n in names if not _DISTINCTIVE.search(n)}, key=len)[::-1]
    alts = [_SNAKE, *distinct]
    parts = [r"(?<![\w$])(?:" + "|".join(alts) + r")(?![\w$])"]
    if plain:
        parts.append(_CONTEXT.format(n="|".join(plain)))
    return re.compile("|".join(parts), re.IGNORECASE)


def _sentences(s: str) -> list[str]:
    out: list[str] = []
    for piece in _SENTENCE_END.split(s):
        # Not a sentence end: an abbreviation's full stop, a "?" placeholder in SQL.
        if out and (_ABBREVIATION.search(out[-1]) or _PLACEHOLDER_END.search(out[-1])):
            out[-1] += " " + piece
        else:
            out.append(piece)
    return out


def without_table_names(text: Any, names: Iterable[str] | re.Pattern[str] | None) -> str:
    """``text`` as written, less what names an identifier or quotes SQL.

    A kind label and its name ("function: api_key_auth_check — ") go first, then a
    parenthetical that names one of ``names`` (see ``_names_pattern``) or quotes SQL
    ("15 tables (a, b, ...) stay in sync" reads "15 tables stay in sync"). Then each
    sentence (an abbreviation's full stop ends none) that still does is cut back to
    the clause before the colon that introduces the list ("Schema design gap: 18
    queries have no access pattern: SELECT ..." keeps "Schema design gap: 18 queries
    have no access pattern."), when that clause has at least four words; otherwise
    the sentence is left out. Empty when nothing is left.
    """
    s = " ".join(str(text or "").split())
    if not s:
        return ""
    pattern = names if isinstance(names, re.Pattern) else _names_pattern(names or [])

    def first_hit(piece: str) -> int | None:
        found = (_SQL_SHAPE.search(piece), _SQL_QUOTED.search(piece), pattern.search(piece))
        hits = [m.start() for m in found if m]
        return min(hits) if hits else None

    s = _KIND_PREFIX.sub("", s)
    s = _PARENTHETICAL.sub(lambda m: "" if first_hit(m.group(0)) is not None else m.group(0), s)
    kept: list[str] = []
    for sentence in _sentences(s):
        hit = first_hit(sentence)
        if hit is None:
            kept.append(sentence)
            continue
        cut = sentence.rfind(": ", 0, hit)
        clause = sentence[:cut].rstrip() if cut > 0 else ""
        if len(clause.split()) >= 4 and first_hit(clause) is None:
            kept.append(clause + ".")
    return " ".join(kept).strip()


# ---------------------------------------------------------------------------
# flowables
# ---------------------------------------------------------------------------
def _style(name: str, **kw: Any) -> ParagraphStyle:
    base = {
        "fontName": font_for(False),
        "fontSize": 10.5,
        "leading": 14,
        "textColor": PAPER,
        "spaceAfter": 0,
        "splitLongWords": 1,
    }
    base.update(kw)
    return ParagraphStyle(name, **base)


def _styles() -> dict[str, ParagraphStyle]:
    bd = font_for(True)
    return {
        "sub": _style("sub", fontSize=14, leading=18, textColor=BLUE),
        "body": _style("body"),
        "small": _style("small", fontSize=8.5, leading=11.5, textColor=MUTED),
        "card_title": _style("card_title", fontName=bd, fontSize=12, leading=15, textColor=WHITE),
        "kicker": _style("kicker", fontName=bd, fontSize=8.5, leading=11, textColor=MUTED),
        "label": _style("label", fontSize=10, leading=13, textColor=PAPER),
        "cell": _style("cell", fontSize=9.5, leading=12.5),
        "head": _style("head", fontName=bd, fontSize=9.5, leading=12.5, textColor=WHITE),
        "pointer": _style("pointer", fontName=bd, fontSize=13, leading=17, textColor=WHITE),
    }


def _para(text: Any, style: ParagraphStyle, color: Color | None = None) -> Paragraph:
    markup = _xml(text)
    if color is not None:
        markup = f'<font color="{color.hexval()}">{markup}</font>'
    return Paragraph(markup, style)


def _labelled(label: str, text: Any, style: ParagraphStyle) -> Paragraph:
    return Paragraph(f"<b>{_xml(label)}:</b> {_xml(text)}", style)


class _Section(Flowable):
    """Zero-size marker at the top of a section's first page: it draws the page
    title in the title band above the frame, first on the page, so the title leads
    the page's text. A page that continues the section gets its title at page end
    (``_page_end``)."""

    def __init__(self, title: str) -> None:
        super().__init__()
        self.title = title

    def wrap(self, availWidth: float, availHeight: float) -> tuple[float, float]:  # noqa: N803
        return 0.0, 0.0

    def drawOn(self, canvas: Canvas, x: float, y: float, _sW: float = 0) -> None:  # noqa: N802
        _draw_title(canvas, self.title)

    def draw(self) -> None:
        return None


class _Panel(Flowable):
    """A dark card with a coloured left rule, holding a column of flowables.

    Measured from its content; split between two of its children (or inside a
    paragraph) when a page runs out of room, so a long card continues on the next
    page instead of being cut.
    """

    PAD = 12.0
    RULE = 4.0
    GAP = 4.0

    def __init__(
        self, content: Sequence[Flowable], accent: Color | None = None, fill: Color = INK
    ) -> None:
        super().__init__()
        self.content = [c for c in content if c is not None]
        self.accent = accent
        self.fill = fill
        self.min_height = 0.0
        self._sizes: list[float] = []

    def _inner(self, width: float) -> float:
        return max(width - 2 * self.PAD - self.RULE, 1.0)

    def wrap(self, availWidth: float, availHeight: float) -> tuple[float, float]:  # noqa: N803
        inner = self._inner(availWidth)
        self._sizes = [c.wrap(inner, 1e6)[1] for c in self.content]
        gaps = self.GAP * max(len(self.content) - 1, 0)
        self.width = availWidth
        self.height = max(2 * self.PAD + sum(self._sizes) + gaps, self.min_height)
        return self.width, self.height

    def draw(self) -> None:
        c = self.canv
        c.saveState()
        c.setFillColor(self.fill)
        c.roundRect(0, 0, self.width, self.height, 5, stroke=0, fill=1)
        if self.accent is not None:
            c.setFillColor(self.accent)
            c.rect(0, 0, self.RULE, self.height, stroke=0, fill=1)
        c.restoreState()
        y = self.height - self.PAD
        x = self.PAD + self.RULE
        for child, h in zip(self.content, self._sizes, strict=True):
            y -= h
            child.drawOn(c, x, y)
            y -= self.GAP

    def split(self, availWidth: float, availHeight: float) -> list[Flowable]:  # noqa: N803
        self.wrap(availWidth, availHeight)
        if self.height <= availHeight:
            return [self]
        if self.height <= FRAME_H and availHeight < FRAME_H - 1:
            return []  # it fits a page of its own: move it there whole
        inner = self._inner(availWidth)
        room = availHeight - 2 * self.PAD
        head: list[Flowable] = []
        used = 0.0
        for i, (child, h) in enumerate(zip(self.content, self._sizes, strict=True)):
            step = h + (self.GAP if head else 0.0)
            if used + step <= room:
                head.append(child)
                used += step
                continue
            rest = list(self.content[i + 1 :])
            left = room - used - (self.GAP if head else 0.0)
            parts: list[Flowable] = []
            if left > 0:
                child.wrap(inner, left)
                parts = list(child.split(inner, left))
            if parts:
                head.append(parts[0])
                rest = parts[1:] + rest
            else:
                rest = [child, *rest]
            if not head:
                return []
            return [_Panel(head, self.accent, self.fill), _Panel(rest, self.accent, self.fill)]
        return [self]


class _Columns(Flowable):
    """Cards side by side, all as tall as the tallest. Too tall for a whole page,
    they stack one under the other instead."""

    GAP = 14.0

    def __init__(self, panels: Sequence[_Panel], weights: Sequence[float] | None = None) -> None:
        super().__init__()
        self.panels = list(panels)
        self.weights = list(weights or [1.0] * len(self.panels))
        self._widths: list[float] = []

    def wrap(self, availWidth: float, availHeight: float) -> tuple[float, float]:  # noqa: N803
        n = len(self.panels)
        free = availWidth - self.GAP * max(n - 1, 0)
        total = sum(self.weights) or 1.0
        self._widths = [free * w / total for w in self.weights]
        for p in self.panels:
            p.min_height = 0.0
        heights = [
            p.wrap(w, availHeight)[1] for p, w in zip(self.panels, self._widths, strict=True)
        ]
        self.width = availWidth
        self.height = max(heights or [0.0])
        for p, w in zip(self.panels, self._widths, strict=True):
            p.min_height = self.height
            p.wrap(w, availHeight)
        return self.width, self.height

    def draw(self) -> None:
        x = 0.0
        for p, w in zip(self.panels, self._widths, strict=True):
            p.drawOn(self.canv, x, 0)
            x += w + self.GAP

    def split(self, availWidth: float, availHeight: float) -> list[Flowable]:  # noqa: N803
        self.wrap(availWidth, availHeight)
        if self.height <= availHeight:
            return [self]
        if availHeight >= FRAME_H - 1:
            for p in self.panels:
                p.min_height = 0.0
            out: list[Flowable] = []
            for p in self.panels:
                out += [p, Spacer(1, self.GAP)]
            return out[:-1]
        return []


class _Bar(Flowable):
    """The target architecture as one stacked bar: each owner engine's share of the
    workload, in its colour, labelled when the label fits inside its segment. Adjacent
    segments are split by a thin gap in the page colour (#1246), so two neighbours stay
    apart even when a narrow segment drops its label."""

    HEIGHT = 26.0
    SEPARATOR = 1.5

    def __init__(self, segments: Sequence[tuple[Color, float, str]]) -> None:
        super().__init__()
        self.segments = list(segments)

    def wrap(self, availWidth: float, availHeight: float) -> tuple[float, float]:  # noqa: N803
        self.width = availWidth
        self.height = self.HEIGHT
        return self.width, self.height

    def draw(self) -> None:
        c = self.canv
        x = 0.0
        size = 9.5
        for i, (color, share, label) in enumerate(self.segments):
            w = self.width * share / 100.0  # facts: presentation (chart geometry)
            c.setFillColor(color)
            c.rect(x, 0, w, self.HEIGHT, stroke=0, fill=1)
            if i:
                c.setFillColor(BG)
                c.rect(x, 0, self.SEPARATOR, self.HEIGHT, stroke=0, fill=1)
            text = printable(label)
            if pdfmetrics.stringWidth(text, font_for(True), size) <= w - 12:
                c.setFillColor(_label_ink(color))
                c.setFont(font_for(True), size)
                c.drawString(x + 6, (self.HEIGHT - size) / 2 + 2, text)
            x += w


def _fit_size(text: str, bold: bool, width: float, sizes: Sequence[float]) -> float:
    """The largest of ``sizes`` at which ``text`` fits ``width`` on one line, else the
    smallest (the paragraph then wraps)."""
    font = font_for(bold)
    s = printable(text)
    for size in sizes:
        if pdfmetrics.stringWidth(s, font, size) <= width:
            return size
    return sizes[-1]


class _Big(Flowable):
    """A big-number paragraph sized to its tile: the largest step at which the
    value fits on one line, wrapping at the smallest step when it never does."""

    def __init__(self, text: str, color: Color) -> None:
        super().__init__()
        self.text = text
        self.color = color
        self._para: Paragraph | None = None

    def wrap(self, availWidth: float, availHeight: float) -> tuple[float, float]:  # noqa: N803
        size = _fit_size(self.text, True, availWidth, BIG_SIZES)
        style = _style(
            "big", fontName=font_for(True), fontSize=size, leading=size * 1.18, textColor=self.color
        )
        self._para = _para(self.text, style)
        self.width, self.height = self._para.wrap(availWidth, availHeight)
        return self.width, self.height

    def draw(self) -> None:
        if self._para is not None:  # wrap() always runs first
            self._para.drawOn(self.canv, 0, 0)


def _tile(
    st: dict[str, ParagraphStyle], value: str, label: str, accent: Color, lines: Sequence[str] = ()
) -> _Panel:
    content: list[Flowable] = [_Big(value, accent), _para(label, st["label"])]
    content += [_para(line, st["small"]) for line in lines if line]
    return _Panel(content, accent)


def _table(
    st: dict[str, ParagraphStyle],
    header: Sequence[str],
    rows: Sequence[Sequence[Any]],
    widths: Sequence[float],
    *,
    total: Sequence[Any] | None = None,
    row_accents: Sequence[Color] | None = None,
) -> Table:
    """A dark table in the deck's design; a cell is a string or a ready paragraph.
    ``row_accents``: one colour per row, drawn as a rule left of its first cell (#1246)."""

    def cell(v: Any, style: ParagraphStyle) -> Flowable:
        return v if isinstance(v, Flowable) else _para(v, style)

    data = [[cell(h, st["head"]) for h in header]]
    data += [[cell(v, st["cell"]) for v in row] for row in rows]
    if total is not None:
        data.append([cell(v, st["head"]) for v in total])
    scale = FRAME_W / sum(widths)
    t = Table(data, colWidths=[w * scale for w in widths], repeatRows=1, hAlign="LEFT")
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), TBL_HEAD),
                ("BACKGROUND", (0, 1), (-1, -1), INK),
                ("LINEBELOW", (0, 1), (-1, -1), 0.5, BG),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
            + [
                ("LINEBEFORE", (0, i), (0, i), ROW_ACCENT_WIDTH, color)
                for i, color in enumerate(row_accents or (), start=1)
            ]
        )
    )
    return t


def _gap(h: float = 10.0) -> Spacer:
    return Spacer(1, h)


ROW_ACCENT_WIDTH = 4.0


def _luminance(color: Color) -> float:
    """WCAG relative luminance. The UI computes the same for its badges
    (``contrastRatio`` in ``src/ui/src/utils/engineColors.js``); keep the two in step."""

    def channel(v: float) -> float:
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    return (
        0.2126 * channel(color.red) + 0.7152 * channel(color.green) + 0.0722 * channel(color.blue)
    )


def contrast_ratio(a: Color, b: Color) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _label_ink(fill: Color) -> Color:
    """White or the card ink, whichever reads better on ``fill`` (#1246: the catalog's
    darker engine colours, Aurora's tan among them, need white)."""
    return WHITE if contrast_ratio(fill, WHITE) >= contrast_ratio(fill, INK) else INK


def _engine_color(engine: str) -> Color:
    return ENGINE_COLOR.get(engine, MUTED)


def _severity_color(level: Any) -> Color:
    return SEVERITY_COLOR.get(str(level or "").upper(), MUTED)


# ---------------------------------------------------------------------------
# sections
# ---------------------------------------------------------------------------
def _share_text(e: dict[str, Any]) -> str:
    cached = e.get("cached_queries")
    if cached is not None:
        return cache_layer_text(int(cached), float(e.get("cached_call_share") or 0))
    w = e.get("workload")
    return f"{fmt_num(w, 1)}%" if isinstance(w, (int, float)) else "-"


def _recommendation(report: dict[str, Any], st: dict[str, ParagraphStyle], names: Any) -> list:
    facts = report_facts(report)
    engines = _architecture_engines(report)
    flow: list[Flowable] = [_Section(H_RECOMMENDATION)]
    arch = report.get("recommended_architecture") or {}
    rationale = without_table_names(arch.get("rationale"), names)
    if rationale:
        flow += [_para(rationale, st["sub"]), _gap(12)]
    if facts.get("engine_scope"):
        # #1328: the customer's engine scope, stated with the recommendation.
        flow += [_para(engine_scope_statement(facts["engine_scope"]) + ".", st["small"]), _gap(8)]
    if placed_share_note(facts):
        # #1329: what the engine shares below are of.
        flow += [_para(placed_share_note(facts), st["small"]), _gap(8)]
    segments = [
        (_engine_color(e["engine"]), float(e["workload"]), display_engine(e["engine"]))
        for e in engines
        if e.get("cached_queries") is None
        and isinstance(e.get("workload"), (int, float))
        and e["workload"] > 0
    ]
    if segments:
        flow += [_Bar(segments), _gap(12)]
    if engines:
        migrated = int(facts.get("tables_migrate") or 0)
        rows = [
            [
                _para(display_engine(e["engine"]), st["head"]),
                e["role"],
                _share_text(e),
                e["scope"],
                _fmt_usd(e.get("cost")),
            ]
            for e in engines
        ]
        total = [
            "Total",
            "",
            f"{fmt_num(facts.get('workload_total_percent') or 0, 0)}%",
            f"{migrated} {plural_noun(migrated, 'table')} "
            f"{plural_verb(migrated, 'migrates', 'migrate')}",
            _fmt_usd(facts.get("engine_cost_total_usd")),
        ]
        flow.append(
            _table(
                st,
                ("Engine", "Role", "Workload", "Scope", "Est. monthly"),
                rows,
                (2.2, 1.8, 2.6, 2.6, 1.4),
                total=total,
                row_accents=[_engine_color(e["engine"]) for e in engines],
            )
        )
        notes = " ".join(
            _engine_role_notes_md(engines, migrated, wave_one=heterogeneous_wave(report))
        )
        if notes:
            flow += [_gap(8), Paragraph(_md_markup(notes), st["small"])]
    ownership = ownership_facts(report)
    if ownership:
        # #212: who owns the mapped tables, in counts (this report names no table).
        flow += [
            _gap(8),
            _labelled(L_TABLE_OWNERS, ownership_summary_text(ownership), st["small"]),
            _para(OWNER_RULE_SHORT, st["small"]),
        ]
    return flow


def _numbers(report: dict[str, Any], st: dict[str, ParagraphStyle], names: Any) -> list:
    facts = report_facts(report)
    tco = report.get("tco_analysis") or {}
    current, savings = _cost_baseline_text(tco)
    retired = _retired_in_use_lines(facts)
    # #949 (#1028): with no source baseline the two lines are left out, not "not provided".
    cost_lines = (
        [f"Current monthly (source): {current}", f"Savings: {savings}"]
        if cost_baseline_known(tco)
        else []
    )
    cost_lines += [f"Retired by this plan: {statement}" for statement, _, _ in retired]
    # #1329: the projection leaves out the queries with no engine in the scope.
    cost_lines += [cost_exclusion_text(facts)] if cost_exclusion_text(facts) else []
    engines = facts.get("engines") or []
    removed = facts.get("removed_engines") or []
    removed_names = ", ".join(display_engine(r["engine"]) for r in removed)
    removed_lines = [
        f"{display_engine(r['engine'])}: queries moved to "
        + ", ".join(display_engine(m) for m in r.get("moved_to") or [])
        for r in removed
        if r.get("moved_to")
    ]
    n_in = headline_query_total(facts)
    migrated = int(facts.get("tables_migrate") or 0)
    tiles = [
        _tile(st, _fmt_usd(facts.get("projected_monthly_cost_usd")), L_COST, BLUE, cost_lines),
        _tile(
            st,
            str(facts.get("engine_count") or 0),
            L_KEPT,
            PURPLE,
            [", ".join(display_engine(e["engine"]) for e in engines)],
        ),
        _tile(st, removed_names or L_NONE, L_REMOVED, ORANGE, removed_lines),
        _tile(
            st,
            str(facts.get("moving_query_count") or 0),
            f"of {n_in} {plural_noun(n_in, 'query', 'queries')} {L_MOVED}",
            GREEN,
            [
                # #1329: "65 of 107 queries move; 42 have no engine in scope".
                *([f"{no_engine_clause(facts)}."] if no_engine_clause(facts) else []),
                f"{migrated} {plural_noun(migrated, 'table')} "
                f"{plural_verb(migrated, 'migrates', 'migrate')}",
                # #1146: the in-scope queries as the query patterns they make.
                *([patterns] if (patterns := query_pattern_count_text(report)) else []),
            ],
        ),
    ]
    flow: list[Flowable] = [_Section(H_NUMBERS), _Columns(tiles), _gap(16)]
    # #1076: why each engine the customer runs today is retired, in the consolidation's words.
    reasons = [f"{name}: {reason}." for _, reason, name in retired if reason]
    if reasons:
        flow += [_para(without_table_names(" ".join(reasons), names), st["small"]), _gap(10)]
    arch = _architecture_engines(report)
    if arch:
        flow.append(_para(L_COST_BREAKDOWN.upper(), st["kicker"]))
        flow.append(_gap(4))
        flow.append(
            _table(
                st,
                ("Engine", "Role", "Est. monthly"),
                [
                    [
                        _para(display_engine(e["engine"]), st["head"]),
                        e["role"],
                        _fmt_usd(e.get("cost")),
                    ]
                    for e in arch
                ],
                (3, 3, 2),
                total=["Total", "", _fmt_usd(facts.get("engine_cost_total_usd"))],
                row_accents=[_engine_color(e["engine"]) for e in arch],
            )
        )
    assumptions = [a for a in (tco.get("assumptions") or []) if isinstance(a, str) and a]
    if assumptions:
        flow += [
            _gap(8),
            _para(f"{L_ASSUMPTIONS}: " + "; ".join(assumptions) + ".", st["small"]),
        ]
    return flow


def _why(report: dict[str, Any], st: dict[str, ParagraphStyle], names: Any) -> list:
    facts = report_facts(report)
    flow: list[Flowable] = [_Section(H_WHY)]
    rows = []
    for e in _architecture_engines(report):
        reason = without_table_names(e.get("rationale"), names)
        rows.append(
            [
                _para(display_engine(e["engine"]), st["head"]),
                e["role"],
                reason or "-",
            ]
        )
    if rows:
        accents = [_engine_color(e["engine"]) for e in _architecture_engines(report)]
        flow.append(_table(st, ("Engine", "Role", "Why"), rows, (2, 1.6, 8.4), row_accents=accents))
        flow += [_gap(6), _para(WORKLOAD_FIT_DEFINITION, st["small"])]
        if any(e.get("role") == CACHE_LAYER for e in _architecture_engines(report)):
            flow += [_gap(2), _para(CACHE_FIT_DEFINITION, st["small"])]
    decisions = []
    for d in facts.get("reality_check_decisions") or []:
        # The reality check's own line, "<title>: <detail>", as every deliverable shows it.
        title = without_table_names(d.get("title"), names)
        detail = without_table_names(d.get("detail"), names)
        if title and detail:
            decisions.append([Paragraph(f"<b>{_xml(title)}:</b> {_xml(detail)}", st["cell"])])
        elif title or detail:
            decisions.append([title or detail])
    if decisions:
        flow += [_gap(14), _table(st, (L_DECISIONS_REALITY,), decisions, (1,))]
    return flow


def _waves(report: dict[str, Any], st: dict[str, ParagraphStyle], names: Any) -> list:
    waves = _resolve_waves(report) or []
    flow: list[Flowable] = [_Section(H_WAVES)]
    flow += [
        _para(_SUGGESTED_PATH_NOTE.format(pointer="the target architecture"), st["sub"]),
        _gap(12),
    ]
    facts_by_wave = {int(w.get("wave") or 0): w for w in report_facts(report).get("waves") or []}
    for w in waves:
        n = int(w.get("wave") or 0)
        engines = [str(e) for e in w.get("engines") or []]
        accent = _engine_color(engines[0]) if engines else MUTED
        wf = facts_by_wave.get(n)
        count_phrase, table_phrase = _wave_headline_stats(w, wf)
        names_text = ", ".join(display_engine(e) for e in engines)
        content: list[Flowable] = [
            _para(f"WAVE {n}", st["kicker"]),
            _para(without_table_names(w.get("title"), names) or names_text, st["card_title"]),
            _para(f"{count_phrase}{table_phrase}", st["body"]),
            _labelled("Engines", names_text, st["small"]),
        ]
        if w.get("moves_from"):
            content.append(
                _labelled(
                    "Moves from",
                    ", ".join(_moves_from_label(str(e)) for e in w["moves_from"]),
                    st["small"],
                )
            )
        if w.get("fronts"):
            content.append(_labelled("Fronts", _moves_from_label(str(w["fronts"])), st["small"]))
        why = without_table_names(w.get("rationale"), names)
        if why:
            content.append(_labelled("Why", why, st["body"]))
        gate = without_table_names(w.get("gate"), names)
        if gate:
            content.append(_labelled("Gate before the next wave", gate, st["small"]))
        if wf is not None and wf.get("heterogeneous_note"):
            # #1120: the note and the detected gaps by feature and impact. The reasons
            # name target functions (jsonb_to_recordset, gen_random_uuid): they stay in
            # the engineering report, like every identifier.
            content.append(_para(wf["heterogeneous_note"], st["body"]))
            # #1330: grouped by impact ("22 need a rewrite: A; B; ..."), so a long
            # conversion's list stays readable; the counts are the facts layer's.
            groups = wave_gap_groups(report, n)
            if groups:
                content.append(
                    _labelled(
                        "Relational gaps",
                        " ".join(
                            f"{phrase}: "
                            + "; ".join(str(g.get("feature") or g.get("rule_id")) for g in members)
                            + "."
                            for phrase, members in groups
                        ),
                        st["small"],
                    )
                )
                content.append(_para(L_GAP_REASONS, st["small"]))
        if wf is not None:
            content.append(
                _labelled(
                    "Open critical or high risks", wf.get("critical_high_risks", 0), st["small"]
                )
            )
        flow += [_Panel(content, accent), _gap(10)]
    return flow[:-1] if waves else flow


def _risks(report: dict[str, Any], st: dict[str, ParagraphStyle], names: Any) -> list:
    facts = report_facts(report)
    risk = report.get("risk_assessment") or {}
    level = str(risk.get("overall_risk_level") or "not assessed")
    open_count = int(facts.get("open_risk_count") or 0)
    resolved_count = int(facts.get("resolved_risk_count") or 0)
    risks = filtered_risks(report)
    sev = _severity_counts_text(facts.get("open_risks_by_severity") or {})
    resolved_sev = _severity_counts_text(facts.get("resolved_risks_by_severity") or {})
    tiles = [
        _tile(st, level, L_OVERALL, _severity_color(level)),
        _tile(st, str(open_count), L_OPEN, BLUE, [sev]),
        _tile(st, str(resolved_count), L_RESOLVED, GREEN, [resolved_sev]),
    ]
    flow: list[Flowable] = [_Section(H_RISKS), _Columns(tiles), _gap(8)]
    if risks:
        flow.append(_para(RISK_RULE_NOTE, st["small"]))
    else:
        flow.append(
            _para(
                (
                    f"No open migration risks; {resolved_count} resolved by the assignment"
                    + (f" ({resolved_sev})." if resolved_sev else ".")
                    if resolved_count
                    else "No risks were identified in this analysis."
                ),
                st["body"],
            )
        )
        flow.append(_para(NO_OPEN_RISKS_CAVEAT, st["small"]))
    for r in risks:
        eng, body = _risk_engine_and_body(r.get("description"))
        level_r = str(r.get("severity") or "-")
        rtype = str(r.get("risk_type") or "").replace("_", " ").lower()
        head = " · ".join(
            x
            for x in (
                str(r.get("risk_id") or "-"),
                level_r,
                display_engine(eng) if eng != "(general)" else eng,
                rtype,
            )
            if x
        )
        content: list[Flowable] = [_para(head, st["card_title"], _severity_color(level_r))]
        what = without_table_names(body, names)
        if what:
            content.append(_para(what, st["body"]))
        mitigation = r.get("mitigation")
        if mitigation and not _repeats(mitigation, body):
            m = without_table_names(mitigation, names)
            if m:
                content.append(_labelled("Mitigation", m, st["small"]))
        if r.get("details") and "engineering report" not in body:
            content.append(_para(RISK_DETAILS_POINTER, st["small"]))
        flow += [_gap(10), _Panel(content, _severity_color(level_r))]
    resolved = _resolved_high_lines(report, names)
    if resolved:
        content = [_para(f"{L_RESOLVED}: {resolved_sev}", st["card_title"], GREEN)]
        content += [_para(line, st["body"]) for line in resolved]
        flow += [_gap(10), _Panel(content, GREEN)]
    return flow


def _resolved_high_lines(report: dict[str, Any], names: Any) -> list[str]:
    """One line per resolved CRITICAL or HIGH risk: where it went and what resolved it,
    in the engineering report's own words ("Resolved because ..."), less any
    identifier. Two risks that read the same give one line."""
    out: list[str] = []
    for r in resolved_risks(report):
        if severity_of(r) not in ("CRITICAL", "HIGH"):
            continue
        reason = r.get("reason")
        if reason:
            text = without_table_names(f"resolved because {reason}.", names)
        else:
            text = without_table_names(_risk_engine_and_body(r.get("description"))[1], names)
        line = f"{resolved_risk_label(r)}: {text}"
        if text and line not in out:
            out.append(line)
    return out


def _decisions(report: dict[str, Any], st: dict[str, ParagraphStyle], names: Any) -> list:
    facts = report_facts(report)
    flow: list[Flowable] = [_Section(H_DECISIONS)]
    cards: list[_Panel] = []
    unreviewed = unreviewed_moves_line(report)
    if unreviewed:
        cards.append(
            _Panel([_para(L_NOT_REVIEWED, st["card_title"]), _para(unreviewed, st["body"])], YELLOW)
        )
    cards += _engine_choice_cards(report, facts, st)
    tied = [t for t in (report.get("tied_queries") or []) if isinstance(t, dict)]
    if tied and report.get("pending_engine_choices") is None:
        n = int(facts.get("tied_query_count") or 0)
        pairs = sorted(
            {
                " and ".join(display_engine(e) for e in sorted(t.get("tied_engines") or []))
                for t in tied
            }
        )
        cards.append(
            _Panel(
                [
                    _para(
                        f"Unresolved engine ties ({n} {plural_noun(n, 'query', 'queries')})",
                        st["card_title"],
                    ),
                    _para(TIED_QUERIES_NOTE, st["body"]),
                    *[_labelled(L_TIED_BETWEEN, p, st["small"]) for p in pairs],
                ],
                YELLOW,
            )
        )
    moved = reroute_summary(report)
    if moved:
        n = int(facts.get("post_schema_reroute_count") or 0)
        cards.append(
            _Panel(
                [
                    _para(
                        f"Moved after approval ({n} {plural_noun(n, 'query', 'queries')})",
                        st["card_title"],
                    ),
                    _para(moved, st["body"]),
                    _para(REROUTE_OVERRIDE_TEXT, st["small"]),
                ],
                PINK,
            )
        )
    unservable = facts.get("unservable") or {}
    if unservable.get("unservable_query_count"):
        cards.append(
            _Panel(
                [
                    _para(unservable_title(unservable["unservable_query_count"]), st["card_title"]),
                    _para(unservable_intro(unservable), st["body"]),
                    # #1329: the queries with no engine in the customer's scope, next to
                    # the other kinds (the intro states them when they are the only kind).
                    *(
                        [_para(UNSERVABLE_SCOPE_SOME, st["body"])]
                        if UNSERVABLE_SCOPE_SOME in unservable_kind_text(unservable)
                        else []
                    ),
                    *(_para(line, st["small"]) for line in unservable_rerun_lines(unservable)),
                ],
                ORANGE,
            )
        )
    if not cards:
        cards.append(_Panel([_para(L_NO_DECISIONS, st["card_title"])], GREEN))
    for card in cards:
        flow += [card, _gap(10)]
    return flow[:-1]


def _choice_count_text(group: dict[str, Any]) -> str:
    tables = int(group.get("choice_count") or 0)
    queries = int(group.get("query_count") or 0)
    return f"{tables} {plural_noun(tables, 'table')}, {queries} {plural_noun(queries, 'query', 'queries')}"


def _engine_choice_cards(
    report: dict[str, Any], facts: dict[str, Any], st: dict[str, ParagraphStyle]
) -> list:
    """The pending engine choices (one per table) and the choices ``--auto``
    decided (#1003), each a "Decisions needed" card; none for an older report."""
    cards: list = []
    n = int(facts.get("pending_choice_count") or 0)
    if n and report.get("pending_engine_choices") is not None:
        q = int(facts.get("pending_choice_query_count") or 0)
        pairs = [
            _labelled(
                L_TIED_BETWEEN,
                f"{' and '.join(display_engine(e) for e in p.get('engines') or [])}: "
                f"{_choice_count_text(p)}",
                st["small"],
            )
            for p in facts.get("pending_choice_pairs") or []
            if isinstance(p, dict)
        ]
        cards.append(
            _Panel(
                [
                    _para(
                        f"Pending engine choices ({n} {plural_noun(n, 'table')}, "
                        f"{q} {plural_noun(q, 'query', 'queries')})",
                        st["card_title"],
                    ),
                    _para(PENDING_CHOICES_NOTE, st["body"]),
                    *pairs,
                ],
                YELLOW,
            )
        )
    n = int(facts.get("auto_decided_choice_count") or 0)
    if n:
        q = int(facts.get("auto_decided_query_count") or 0)
        placed = [
            _labelled(
                f"{L_PLACED_ON} {display_engine(str(g.get('engine') or ''))}",
                _choice_count_text(g),
                st["small"],
            )
            for g in facts.get("auto_decided_by_engine") or []
            if isinstance(g, dict)
        ]
        cards.append(
            _Panel(
                [
                    _para(
                        f"Decided automatically, review if needed ({n} {plural_noun(n, 'table')}, "
                        f"{q} {plural_noun(q, 'query', 'queries')})",
                        st["card_title"],
                    ),
                    _para(AUTO_DECIDED_NOTE, st["body"]),
                    *placed,
                ],
                YELLOW,
            )
        )
    return cards


def _next_steps(report: dict[str, Any], st: dict[str, ParagraphStyle], names: Any) -> list:
    risk = report.get("risk_assessment") or {}
    flow: list[Flowable] = [_Section(H_NEXT)]
    steps = [
        s
        for s in (
            without_table_names(x, names)
            for x in (risk.get("mitigation_strategies") or [])
            if isinstance(x, str)
        )
        if s
    ]
    for i, step in enumerate(steps, start=1):
        number = f'<font name="{font_for(True)}" color="{BLUE.hexval()}">{i}.</font>'
        flow += [_Panel([Paragraph(f"{number} {_xml(step)}", st["body"])], BLUE), _gap(8)]
    flow += [_gap(6), _Panel([_para(CLOSING_POINTER, st["pointer"])], GREEN)]
    return flow


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------
def _date(report: dict[str, Any]) -> dt.date | None:
    """The report's own generation date; None when it carries no valid one."""
    m = re.match(r"\d{4}-\d{2}-\d{2}", str(report.get("timestamp") or ""))
    try:
        return dt.date.fromisoformat(m.group(0)) if m else None
    except ValueError:
        return None


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
            if self._pageNumber > 1:  # the cover carries no page number
                self.setFont(font_for(False), 7)
                self.setFillColor(MUTED)
                self.drawRightString(PAGE_W - MARGIN_X, 18, f"Page {self._pageNumber} of {total}")
            super().showPage()
        super().save()


def _footer(c: Canvas, db: str) -> None:
    c.setFont(font_for(True), 7.5)
    c.setFillColor(MUTED)
    c.drawString(MARGIN_X, 30, printable(f"{PRODUCT_NAME} · {db}"))
    c.setFont(font_for(False), 6.5)
    c.drawString(MARGIN_X, 18, FOOTER)


class _Doc(BaseDocTemplate):
    def __init__(self, *args: Any, db: str, report: dict[str, Any], **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.db = db
        self.report = report
        self.section: str | None = None
        self.section_here = False

    def afterFlowable(self, flowable: Any) -> None:  # noqa: N802 -- reportlab's name
        if isinstance(flowable, _Section):
            self.section = flowable.title
            self.section_here = True
            key = f"section-{len(self.seen_sections)}"
            self.seen_sections.append(flowable.title)
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(flowable.title, key, 0, 0)

    seen_sections: list[str]


def _content_background(c: Canvas, doc: _Doc) -> None:
    c.saveState()
    c.setFillColor(BG)
    c.rect(0, 0, PAGE_W, PAGE_H, stroke=0, fill=1)
    c.setFillColor(BLUE)
    c.rect(MARGIN_X, PAGE_H - 26, 36, 3, stroke=0, fill=1)
    c.restoreState()


def _draw_title(c: Canvas, title: str) -> None:
    size = _fit_size(title, True, FRAME_W, TITLE_SIZES)
    style = _style(
        "title", fontName=font_for(True), fontSize=size, leading=size * 1.15, textColor=WHITE
    )
    p = Paragraph(_xml(title), style)
    _, h = p.wrap(FRAME_W, 100)
    c.saveState()
    p.drawOn(c, MARGIN_X, TITLE_BASELINE - h + size)
    c.restoreState()


def _page_end(c: Canvas, doc: _Doc) -> None:
    """A page that continues a section gets its title, marked continued; every page
    its footer."""
    if doc.section is not None and not doc.section_here:
        _draw_title(c, doc.section + CONTINUED)
    doc.section_here = False
    c.saveState()
    _footer(c, doc.db)
    c.restoreState()


def _cover(c: Canvas, doc: _Doc) -> None:
    """The cover: the deck's aurora as a vector gradient, the product, the report and
    the source database, each wrapped and sized to fit."""
    report = doc.report
    c.saveState()
    c.setFillColor(HexColor("#0B2230"))
    c.rect(0, 0, PAGE_W, PAGE_H, stroke=0, fill=1)
    c.radialGradient(
        PAGE_W * 1.02,
        PAGE_H * 0.78,
        PAGE_W * 0.95,
        (HexColor("#B26BFF"), HexColor("#4F5BEA"), HexColor("#123A6B"), HexColor("#0B2230")),
        (0.0, 0.22, 0.55, 1.0),
        extend=True,
    )
    width = PAGE_W * 0.62
    y = PAGE_H - 150

    def block(text: str, bold: bool, sizes: Sequence[float], color: Color, gap: float) -> None:
        nonlocal y
        size = _fit_size(text, bold, width, sizes)
        style = _style(
            "cover", fontName=font_for(bold), fontSize=size, leading=size * 1.15, textColor=color
        )
        p = Paragraph(_xml(text), style)
        _, h = p.wrap(width, PAGE_H)
        p.drawOn(c, 64, y - h)
        y -= h + gap

    block(PRODUCT_NAME, True, (40, 36, 32, 28, 24), WHITE, 10)
    block(REPORT_NAME, False, (26, 24, 22, 20), PAPER, 26)
    block(f"Source database {doc.db}", True, (24, 22, 20, 18, 16, 14), BLUE, 8)
    generated = _date(report)
    small = f"Job {report.get('job_id') or 'unknown'}" + (
        f" · Generated {generated.isoformat()}" if generated else ""
    )
    style = _style("cover_small", fontSize=10, leading=13, textColor=PAPER)
    p = Paragraph(_xml(small), style)
    _, h = p.wrap(width, PAGE_H)
    p.drawOn(c, 64, 70)
    c.setFont(font_for(False), 6.5)
    c.setFillColor(PAPER)
    c.drawString(64, 30, FOOTER)
    c.restoreState()


SECTION_BUILDERS = (_recommendation, _numbers, _why, _waves, _risks, _decisions, _next_steps)


def build_story(report: dict[str, Any], identifiers: Iterable[str] = ()) -> list[Flowable]:
    """Every page's flowables, in section order (the cover is drawn by its template)."""
    register_fonts()
    st = _styles()
    names = _names_pattern(table_names(report) | set(identifiers))
    story: list[Flowable] = [Spacer(1, 1), NextPageTemplate("content")]
    for build in SECTION_BUILDERS:
        story += [PageBreak(), *build(report, st, names)]
    return story


def render_executive_report_pdf(report: dict[str, Any], identifiers: Iterable[str] = ()) -> bytes:
    """Render the executive report from ``report.json`` and return its bytes.

    ``identifiers`` are the collector's names (``collected_identifiers``), kept out of
    the text with the ones the report names itself. Every figure comes from the
    report's facts layer. Deterministic for a given report and identifier set.
    """
    db = str(report.get("database_name") or "?")
    story = build_story(report, identifiers)
    buf = io.BytesIO()
    doc = _Doc(
        buf,
        db=db,
        report=report,
        pagesize=(PAGE_W, PAGE_H),
        leftMargin=MARGIN_X,
        rightMargin=MARGIN_X,
        topMargin=PAGE_H - FRAME_TOP,
        bottomMargin=FRAME_BOTTOM,
        title=f"{PRODUCT_NAME}: {REPORT_NAME}, {db}",
        author=PRODUCT_NAME,
        creator=PRODUCT_NAME,
        subject=f"{REPORT_NAME}, job {report.get('job_id') or 'unknown'}",
        invariant=1,
        pageCompression=1,
    )
    doc.seen_sections = []

    def frame(fid: str) -> Frame:
        return Frame(
            MARGIN_X,
            FRAME_BOTTOM,
            FRAME_W,
            FRAME_H,
            id=fid,
            leftPadding=0,
            rightPadding=0,
            topPadding=0,
            bottomPadding=0,
        )

    doc.addPageTemplates(
        [
            PageTemplate(id="cover", frames=[frame("cover")], onPage=_cover),
            PageTemplate(
                id="content",
                frames=[frame("content")],
                onPage=_content_background,
                onPageEnd=_page_end,
            ),
        ]
    )
    doc.build(story, canvasmaker=_NumberedCanvas)
    data = buf.getvalue()
    logger.info("Rendered %s: %d bytes, database=%s", FILENAME, len(data), db)
    return data


__all__ = [
    "CLOSING_POINTER",
    "COPYRIGHT",
    "FILENAME",
    "FOOTER",
    "H_DECISIONS",
    "H_NEXT",
    "H_NUMBERS",
    "H_RECOMMENDATION",
    "H_RISKS",
    "H_WAVES",
    "H_WHY",
    "PRODUCT_NAME",
    "REPORT_NAME",
    "SECTION_HEADINGS",
    "printable",
    "render_executive_report_pdf",
    "collected_identifiers",
    "table_names",
    "without_table_names",
]
