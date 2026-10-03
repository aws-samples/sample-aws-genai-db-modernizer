"""Neutral title slide, metadata and disclaimer for the executive deck (issue #181).

The deck is generated locally from this public sample, so it must not carry the
AWS Transform wordmark/mark, an "Amazon Confidential" footer, or AWS Transform
metadata. The title slide names the assessment, keeps the AWS logo, and prints the
job ID and generation date so a printed PDF traces back to its run. Every slide's
footer reads "(c) <year> Amazon Web Services, Inc. or its affiliates. For
informational purposes only." with the year the report was generated.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import re
import zipfile
from pathlib import Path
from typing import Any

import pytest
from pptx import Presentation
from pypdf import PdfReader

from src.report import pdf_report, pptx_report

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "wordpress_report.json"
# Branding, plus Microsoft sensitivity-label properties (docProps/custom.xml): a
# public sample must not carry internal classification metadata.
FORBIDDEN = re.compile(r"transform|confidential|pending_classification|msip_label", re.IGNORECASE)


def _report(timestamp: str | None = "2031-04-05T06:07:08Z") -> dict[str, Any]:
    rep: dict[str, Any] = json.loads(FIXTURE.read_text())
    rep["job_id"] = "job-181"
    if timestamp is None:
        rep.pop("timestamp", None)
    else:
        rep["timestamp"] = timestamp
    return rep


def _footer(year: int) -> str:
    return f"© {year} Amazon Web Services, Inc. or its affiliates. For informational purposes only."


def _forbidden_hits(blob: bytes) -> list[str]:
    hits = []
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for name in z.namelist():
            if name.endswith((".xml", ".rels")):
                text = z.read(name).decode("utf-8")
                hits += [f"{name}: {m.group(0)}" for m in FORBIDDEN.finditer(text)]
    return hits


def _layout_texts(prs) -> list[str]:
    """Every text frame's text on every slide's layout (where the footer lives)."""
    out = []
    for slide in prs.slides:
        for shape in slide.slide_layout.shapes:
            if shape.has_text_frame:
                out.append(shape.text_frame.text)
    return out


@pytest.fixture(scope="module")
def rendered() -> tuple[bytes, bytes]:
    return pdf_report.render_executive_summary_pdf(_report(), {})


class TestTemplate:
    def test_template_parts_carry_no_branding_or_classification_metadata(self) -> None:
        assert _forbidden_hits(pptx_report.TEMPLATE.read_bytes()) == []


class TestRenderedDeck:
    def test_no_branding_or_classification_metadata_in_any_part(self, rendered) -> None:
        deck, _ = rendered
        assert _forbidden_hits(deck) == []

    def test_every_slide_footer_is_the_neutral_disclaimer_with_generation_year(
        self, rendered
    ) -> None:
        prs = Presentation(io.BytesIO(rendered[0]))
        footers = [t for t in _layout_texts(prs) if t.startswith("©")]
        # Each of the seven slides inherits at least one visible footer line.
        assert len(footers) >= len(prs.slides)
        assert set(footers) == {_footer(2031)}

    def test_title_slide(self, rendered) -> None:
        prs = Presentation(io.BytesIO(rendered[0]))
        title = prs.slides[0]
        texts = [s.text_frame.text for s in title.shapes if s.has_text_frame]
        assert "Database Modernizer Assessment" in texts
        small = next(t for t in texts if "job-181" in t)
        assert "2031-04-05" in small
        assert any("wordpress" in t for t in texts)
        # The hexagon mark is gone; nothing on the title slide is custom geometry.
        assert not any(
            s._element.find(f".//{pdf_report.A}custGeom") is not None for s in title.shapes
        )

    def test_title_slide_keeps_the_aws_logo(self, rendered) -> None:
        prs = Presentation(io.BytesIO(rendered[0]))
        pictures = [s for s in prs.slides[0].slide_layout.shapes if s.shape_type == 13]
        # Full-bleed background, the small footer logo, and the AWS logo top-left.
        w, h = int(prs.slide_width or 0), int(prs.slide_height or 0)
        logo = [p for p in pictures if p.top < h // 4 and p.width < w // 4]
        assert len(logo) == 1
        # The plain AWS logo (the same part as the footer logo), uncropped -- not
        # the "aws migrations" lockup cropped down to its "aws" half.
        blip = logo[0]._element.blipFill
        assert logo[0].part.related_part(blip.blip.rEmbed).partname == "/ppt/media/image1.png"
        assert blip.find(f"{pdf_report.A}srcRect") is None
        # Same aspect ratio as the image, so nothing is stretched.
        assert abs(logo[0].width / logo[0].height - 1254 / 750) < 0.01

    def test_unused_wordmark_image_is_not_packaged(self, rendered) -> None:
        with zipfile.ZipFile(io.BytesIO(rendered[0])) as z:
            assert "ppt/media/image3.png" not in z.namelist()

    def test_core_properties_name_the_assessment(self, rendered) -> None:
        cp = Presentation(io.BytesIO(rendered[0])).core_properties
        assert cp.title == "Database Modernizer Assessment — wordpress"
        assert cp.comments == "Database Modernizer Assessment — job job-181 — 2031-04-05T06:07:08Z"
        assert cp.author == "Database Modernizer Assessment"
        assert cp.last_modified_by == "Database Modernizer Assessment"

    def test_pdf_metadata_names_the_assessment(self, rendered) -> None:
        meta = PdfReader(io.BytesIO(rendered[1])).metadata
        assert meta is not None
        assert meta.author == "Database Modernizer Assessment"
        assert meta.creator == "Database Modernizer Assessment"
        assert not FORBIDDEN.search(" ".join(str(v) for v in meta.values()))

    def test_pdf_pages_carry_the_disclaimer(self, rendered) -> None:
        pages = PdfReader(io.BytesIO(rendered[1])).pages
        for page in pages:
            text = " ".join((page.extract_text() or "").split())
            assert "For informational purposes only." in text
            assert not FORBIDDEN.search(text)


def test_footer_year_falls_back_to_the_current_year_without_a_timestamp() -> None:
    deck = pptx_report.render_executive_summary_pptx(_report(timestamp=None), {})
    prs = Presentation(io.BytesIO(deck))
    footers = {t for t in _layout_texts(prs) if t.startswith("©")}
    assert footers == {_footer(dt.datetime.now(dt.UTC).year)}


def test_long_database_name_subtitle_shrinks_to_fit_one_line() -> None:
    rep = _report()
    rep["database_name"] = "customer_orders_and_fulfilment_production_primary"
    prs = Presentation(io.BytesIO(pptx_report.render_executive_summary_pptx(rep, {})))
    sub = next(
        s
        for s in prs.slides[0].shapes
        if s.has_text_frame and "customer_orders" in s.text_frame.text
    )
    run = sub.text_frame.paragraphs[0].runs[0]
    size = run.font.size.pt
    assert size < 32
    width_in = sub.width / 914400
    assert pptx_report._text_em(run.text) * size / 72 <= width_in
