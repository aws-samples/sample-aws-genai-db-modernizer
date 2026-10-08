"""The Risk Profile slide (and the PDF, which replays it) with no open risks (#434).

A job can end with ``risk_assessment.risks`` empty while ``resolved_risks`` is not --
every risk the analysis raised was moved onto an engine that resolves it. Before this
fix the Risk Profile slide rendered an empty shell for that case: a "0 risks:"
subtitle, a table with a header row and no data rows, and an empty "BY TYPE" box --
only a fixed "No open risks remain." sentence at the bottom carried any content.

The fix mirrors the empty-state the interactive Analysis Report already uses
(``src/report/templates/analysis_report.js``: an "N resolved by the assignment"
disclosure listing each resolved risk with its reason) and the Engineering Report's
"Resolved by the assignment" section (``src/report/renderers.py``): list what the
assignment resolved, by engine, with its reason, and carry
``renderers.NO_OPEN_RISKS_CAVEAT`` -- the same reminder every deliverable's
no-open-risk state carries -- that an empty register is not the same as "no
migration risk" (the analysis never modeled traffic bursts, peak load or
compliance).

Round-1 review (PR #456) found the deck's engine arrow (U+2192) has no glyph in the
PDF's embedded font, so it rendered as a blank; ``TestPdfFontCoverage`` below checks
every character the slide can print against both embedded font files directly, the
way the PDF renderer (``pdf_report.py``) actually draws them.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from reportlab.pdfbase.ttfonts import TTFontFile

from src.report import pdf_report, pptx_report, renderers

FIXTURE = Path(__file__).parent / "fixtures" / "wordpress_report.json"
FONT_DIR = Path(pptx_report.__file__).parent / "assets" / "fonts"
_FONT_RG = TTFontFile(str(FONT_DIR / "AmazonEmber_Rg.ttf"))
_FONT_BD = TTFontFile(str(FONT_DIR / "AmazonEmber_Bd.ttf"))

RESOLVED: list[dict[str, Any]] = [
    {
        "engine": "aurora_mysql",
        "severity": "HIGH",
        "description": "[aurora_mysql] Single-row SELECT by primary key at very high frequency.",
        "affected_tables": ["wordpress.wp_options"],
        "query_ids": ["q1"],
        "resolved_on": "dynamodb",
        "reason": "the Aurora MySQL analysis recommended DynamoDB for these queries and the "
        "assignment moved them there",
    },
    {
        "engine": "aurora_mysql",
        "severity": "MEDIUM",
        "description": "[aurora_mysql] Table accessed by at most 2 query patterns.",
        "affected_tables": [],
        "query_ids": ["q2"],
        "resolved_on": "dynamodb",
        "reason": "the queries moved to DynamoDB, whose schema design serves all of them",
    },
    {
        "engine": "aurora_mysql",
        "severity": "MEDIUM",
        "description": "[aurora_mysql] Join across 4 tables on every page load.",
        "affected_tables": [],
        "query_ids": ["q3"],
        "resolved_on": "opensearch",
        "reason": "the join moved to OpenSearch, which serves the whole access pattern in one call",
    },
    {
        "engine": "aurora_mysql",
        "severity": "LOW",
        "description": "[aurora_mysql] Full table scan on a reporting query.",
        "affected_tables": [],
        "query_ids": ["q4"],
        "resolved_on": "opensearch",
        "reason": "reporting queries moved to OpenSearch, which indexes the fields they filter on",
    },
    {
        "engine": "aurora_mysql",
        "severity": "LOW",
        "description": "[aurora_mysql] Unbounded result set on a list endpoint.",
        "affected_tables": [],
        "query_ids": ["q5"],
        "resolved_on": "dynamodb",
        "reason": "the endpoint moved to DynamoDB with a bounded Query",
    },
    {
        "engine": "aurora_mysql",
        "severity": "LOW",
        "description": "[aurora_mysql] Session data stored in a relational table.",
        "affected_tables": [],
        "query_ids": ["q6"],
        "resolved_on": "elasticache",
        "reason": "session reads and writes moved to the ElastiCache cache layer",
    },
]

# A risk resolved without the queries moving anywhere (#434 review item 2).
KEPT_RESOLVED: list[dict[str, Any]] = [
    {
        "engine": "aurora_mysql",
        "severity": "MEDIUM",
        "description": "[aurora_mysql] Kept on Aurora MySQL because of a utility script.",
        "affected_tables": ["wordpress.wp_options", "wordpress.wp_postmeta", "wordpress.wp_users"],
        "query_ids": ["q10"],
        "resolved_on": "aurora_mysql",
        "reason": "Aurora MySQL is also required for a utility script on this table",
    },
]


def _long_resolved(n: int) -> list[dict[str, Any]]:
    """``n`` resolved risks whose description/reason are each well past any
    reasonable table-cell truncation point (#434 review item 5: 20 long rows
    must still fit six rows on the slide without overflowing into the
    reminder card)."""
    long_desc = (
        "A long description of a resolved risk that keeps going on past any "
        "reasonable truncation point for a slide table cell, repeating itself "
        "a little just to be sure it is long enough to need clipping"
    )
    long_reason = (
        "Because the assignment moved these queries through several intermediate "
        "steps that are individually unremarkable but add up to a genuinely long "
        "sentence once they are all strung together end to end"
    )
    return [
        {
            "engine": "aurora_mysql",
            "severity": "LOW",
            "description": f"[aurora_mysql] {long_desc} (case {i}).",
            "affected_tables": [],
            "query_ids": [f"q{i}"],
            "resolved_on": "dynamodb",
            "reason": f"{long_reason} (case {i}).",
        }
        for i in range(n)
    ]


def _all_text(prs) -> str:
    out = []
    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                out.append(shape.text_frame.text)
            if shape.has_table:
                for row in shape.table.rows:
                    for cell in row.cells:
                        out.append(cell.text_frame.text)
    return "\n".join(out)


def _missing_glyphs(prs) -> set[str]:
    """Characters with no glyph in whichever embedded font (regular or bold)
    the run that prints them would use. ``pdf_report.py``'s own docstring:
    the PDF walks this exact shape tree and draws each run with
    ``font_for(run.font.name, run.font.bold)`` -- regular or bold Amazon
    Ember -- so a character missing from that font renders as a silent blank,
    not an error (reportlab's ``drawString`` does not raise on it)."""
    missing: set[str] = set()
    for slide in prs.slides:
        for shape in slide.shapes:
            frames = []
            if shape.has_text_frame:
                frames.append(shape.text_frame)
            if shape.has_table:
                for row in shape.table.rows:
                    for cell in row.cells:
                        frames.append(cell.text_frame)
            for tf in frames:
                for para in tf.paragraphs:
                    for run in para.runs:
                        font = _FONT_BD if run.font.bold else _FONT_RG
                        for ch in run.text:
                            if ch.isspace():
                                continue
                            if ord(ch) not in font.charToGlyph:
                                missing.add(ch)
    return missing


def _report(*, resolved: list[dict[str, Any]] | None) -> dict[str, Any]:
    data: dict = json.loads(FIXTURE.read_text())
    data = copy.deepcopy(data)
    data["risk_assessment"]["risks"] = []
    data["risk_assessment"]["overall_risk_level"] = "LOW"
    if resolved is None:
        data["risk_assessment"].pop("resolved_risks", None)
    else:
        data["risk_assessment"]["resolved_risks"] = resolved
    return data


def _render_risk_slide(rep: dict[str, Any]):
    f = pptx_report.derive(rep, {})
    prs = pptx_report.open_deck(keep=1)
    pptx_report.slide_risk(prs, f)
    return prs


def _render_risk_slide_text(rep: dict[str, Any]) -> str:
    return _all_text(_render_risk_slide(rep))


class TestNoOpenRisksWithResolved:
    def test_subtitle_states_the_resolved_count_not_a_zero_count(self) -> None:
        text = _render_risk_slide_text(_report(resolved=RESOLVED))
        assert "0 risks:" not in text
        assert "No open risks. 6 risks resolved by the assignment" in text

    def test_lists_resolved_risks_with_their_reasons(self) -> None:
        text = _render_risk_slide_text(_report(resolved=RESOLVED))
        assert "Single-row SELECT by primary key" in text
        assert "the Aurora MySQL analysis recommended DynamoDB for these queries" in text
        assert "session reads and writes moved to the ElastiCache cache layer" in text

    def test_resolved_engine_label_spells_out_to_not_an_arrow(self) -> None:
        """#434 round-1 review (MUST): U+2192 has no glyph in the embedded PDF
        font and rendered as a blank; spell out "to" instead."""
        text = _render_risk_slide_text(_report(resolved=RESOLVED))
        assert "Aurora MySQL to DynamoDB" in text
        assert "aurora_mysql" not in text
        assert "→" not in text

    def test_kept_row_says_kept_and_names_up_to_two_affected_tables(self) -> None:
        text = _render_risk_slide_text(_report(resolved=KEPT_RESOLVED))
        assert "Aurora MySQL (kept)" in text
        assert "wordpress.wp_options" in text
        assert "wordpress.wp_postmeta" in text
        assert "wordpress.wp_users" not in text  # affected_tables[:2] only

    def test_no_empty_by_type_box(self) -> None:
        """The "BY TYPE" box has nothing to count when there are no open risks."""
        text = _render_risk_slide_text(_report(resolved=RESOLVED))
        assert "BY TYPE" not in text

    def test_overflow_resolved_risks_point_to_the_engineering_report(self) -> None:
        """Past whatever cap the table shows per slide, the rest must still be
        reachable, not silently dropped."""
        overflowing = RESOLVED + [
            {
                "engine": "aurora_mysql",
                "severity": "LOW",
                "description": "[aurora_mysql] Yet another resolved risk past the table cap.",
                "affected_tables": [],
                "query_ids": ["q7"],
                "resolved_on": "dynamodb",
                "reason": "it also moved to DynamoDB",
            }
        ]
        text = _render_risk_slide_text(_report(resolved=overflowing))
        assert "Engineering Report" in text

    def test_reminds_that_an_empty_register_is_not_zero_migration_risk(self) -> None:
        text = _render_risk_slide_text(_report(resolved=RESOLVED))
        assert "traffic" in text.lower()
        assert "peak load" in text.lower()
        assert "compliance" in text.lower()
        assert renderers.NO_OPEN_RISKS_CAVEAT in text

    def test_resolved_rows_sort_by_severity_only_ties_keep_report_json_order(self) -> None:
        """#434 round-1 review (should 3): sorting on severity, engine and
        description disagreed with the Engineering/Analysis reports, which keep
        ``resolved_risks``' own order for same-severity risks. ``sorted`` is
        stable, so ties must come out in list order, not re-sorted by engine."""
        f = pptx_report.derive(_report(resolved=RESOLVED), {})
        medium = [r for r in f["resolved_risks"] if r["severity"] == "MEDIUM"]
        # RESOLVED lists its two MEDIUM risks resolved_on=dynamodb then
        # resolved_on=opensearch; alphabetical-by-description sorting would
        # have reordered them ("Join..." < "Table...").
        assert [r["resolved_on"] for r in medium] == ["dynamodb", "opensearch"]


class TestNoOpenAndNoResolvedRisks:
    def test_states_no_risks_at_all_rather_than_an_empty_table(self) -> None:
        text = _render_risk_slide_text(_report(resolved=None))
        assert "0 risks:" not in text
        assert "No open or resolved risks" in text
        assert "traffic" in text.lower()

    def test_reminder_is_not_stranded_alone_at_the_bottom(self) -> None:
        """#434 round-1 review (suggestion): move the caveat up into the lead
        card instead of leaving it isolated at the bottom of an otherwise
        empty slide."""
        prs = _render_risk_slide(_report(resolved=None))
        slide = prs.slides[-1]
        caveat_tops = [
            shape.top
            for shape in slide.shapes
            if shape.has_text_frame and renderers.NO_OPEN_RISKS_CAVEAT in shape.text_frame.text
        ]
        assert caveat_tops
        # The lead card sits at BODY_TOP (1.92in); the caveat must be in it, not
        # down near the old bottom-banner position (5.05in).
        assert all(top < pptx_report.Inches(3.0) for top in caveat_tops)


class TestPdfFontCoverage:
    """#434 round-1 review (MUST): every character the slide can print must have
    a glyph in whichever embedded font (regular or bold) actually draws it."""

    def test_resolved_risk_table_has_no_missing_glyphs(self) -> None:
        prs = _render_risk_slide(_report(resolved=RESOLVED))
        assert _missing_glyphs(prs) == set()

    def test_kept_row_has_no_missing_glyphs(self) -> None:
        prs = _render_risk_slide(_report(resolved=KEPT_RESOLVED))
        assert _missing_glyphs(prs) == set()

    def test_no_risk_at_all_slide_has_no_missing_glyphs(self) -> None:
        prs = _render_risk_slide(_report(resolved=None))
        assert _missing_glyphs(prs) == set()

    def test_twenty_long_resolved_risks_have_no_missing_glyphs(self) -> None:
        prs = _render_risk_slide(_report(resolved=_long_resolved(20)))
        assert _missing_glyphs(prs) == set()


class TestTwentyLongResolvedRisksFitTheSlide:
    """#434 round-1 review (should 5): a run with far more resolved risks than
    the table can show must still render a well-formed slide -- the table caps
    at MAX_RESOLVED_ROWS, sits entirely above the reminder card, and the
    reminder (plus its overflow sentence) still fits its own text box."""

    def test_table_caps_at_max_resolved_rows(self) -> None:
        f = pptx_report.derive(_report(resolved=_long_resolved(20)), {})
        prs = pptx_report.open_deck(keep=1)
        s = pptx_report.slide_risk(prs, f)
        tables = [sh for sh in s.shapes if sh.has_table]
        assert len(tables) == 1
        assert len(tables[0].table.rows) == pptx_report.MAX_RESOLVED_ROWS + 1  # + header

    def test_table_bottom_sits_above_the_reminder_card(self) -> None:
        f = pptx_report.derive(_report(resolved=_long_resolved(20)), {})
        prs = pptx_report.open_deck(keep=1)
        s = pptx_report.slide_risk(prs, f)
        tbl_shape = next(sh for sh in s.shapes if sh.has_table)
        table_bottom_in = (tbl_shape.top + tbl_shape.height) / pptx_report.Inches(1)
        reminder_card_top_in = 5.05
        assert table_bottom_in <= reminder_card_top_in, (
            f"table bottom {table_bottom_in:.3f}in overlaps the reminder card at "
            f"{reminder_card_top_in}in"
        )

    def test_reminder_text_fits_its_box_with_the_overflow_sentence(self) -> None:
        f = pptx_report.derive(_report(resolved=_long_resolved(20)), {})
        n_resolved = len(f["resolved_risks"])
        more = n_resolved - pptx_report.MAX_RESOLVED_ROWS
        reminder = (
            renderers.NO_OPEN_RISKS_CAVEAT
            + f" The remaining {more} resolved risks are in the Engineering Report."
        )
        height = pptx_report._estimated_text_height_in(reminder, 12.0, 11.0)
        # textbox(s, 0.90, 5.17, 11.0, 0.9) -- the reminder card's text box height.
        assert height <= 0.9, f"estimated reminder height {height:.3f}in exceeds the 0.9in box"

    def test_resolved_rows_wrap_within_two_lines_at_the_table_row_height(self) -> None:
        """#434 round-1 review (should 1 and 5): verify word-wrap with the PDF
        renderer's own layout helpers (``pdf_report._tokens``/``_wrap``), not an
        estimate -- the row height (0.44in) only has room for two lines at the
        table's 9.5pt body size."""
        f = pptx_report.derive(_report(resolved=_long_resolved(20)), {})
        prs = pptx_report.open_deck(keep=1)
        s = pptx_report.slide_risk(prs, f)
        tbl = next(sh for sh in s.shapes if sh.has_table).table
        col_widths_in = {2: 4.00, 3: 4.08}  # "What it is", "Reason"
        for row in list(tbl.rows)[1:]:
            for col_idx, width_in in col_widths_in.items():
                cell = row.cells[col_idx]
                for para_ in cell.text_frame.paragraphs:
                    toks = pdf_report._tokens(para_, {}, 9.5, (241, 243, 243))
                    usable_pt = (width_in - 0.12) * 72  # minus the cell's 0.06in L/R margins
                    lines = pdf_report._wrap(toks, usable_pt)
                    assert len(lines) <= 2, (
                        f"cell {cell.text_frame.text!r} wraps to {len(lines)} lines, "
                        f"past the row's two-line budget"
                    )


@pytest.mark.parametrize("resolved", [RESOLVED, KEPT_RESOLVED, None])
def test_pdf_renders_without_missing_glyphs_or_box_overflow(resolved) -> None:
    """The PDF walks the shape tree of the same deck (pdf_report.py's own
    docstring: "there is one layout implementation, in pptx_report"), so the
    glyph-coverage and geometry checks above are the PDF's checks too; this
    only confirms the PDF renderer itself still runs end to end over the fixed
    shapes without raising."""
    rep = _report(resolved=resolved)
    _, pdf_bytes = pdf_report.render_executive_summary_pdf(rep, {})
    assert pdf_bytes.startswith(b"%PDF")
