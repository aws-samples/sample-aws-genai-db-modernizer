"""The executive summary PDF has every slide, its headings, and embedded fonts."""

from __future__ import annotations

import re

from pypdf import PdfReader
from pypdf.generic import ArrayObject, IndirectObject

from tests.e2e.pipeline import PipelineResult

SLIDE_TITLES = [
    "Assessment Summary",
    "Workload Coverage and Engine Assignment",
    "Query Workload Composition",
    "Engine Confidence and Open Decisions",
    "Risk Profile",
    "Migration Sequencing",
]
FORBIDDEN = ("undefined", "NaN", "[object Object]", "{{", "None%")


def _reader(run: PipelineResult) -> PdfReader:
    return PdfReader(str(run.deliverable("summary-executive-report.pdf")))


def _normalize(text: str) -> str:
    """Collapse all whitespace runs to single spaces.

    pypdf's ``extract_text()`` emits one token per line (``"Assessment\\nSummary"``
    rather than ``"Assessment Summary"``) because it walks the content stream's
    individual ``Tj``/``TJ`` show-text operations, not a laid-out line. Phrase
    matching has to be whitespace-insensitive to survive that.
    """
    return " ".join(text.split())


def test_has_seven_pages(run: PipelineResult) -> None:
    assert len(_reader(run).pages) == 7


def test_every_slide_title_present_in_order(run: PipelineResult) -> None:
    texts = [_normalize(p.extract_text() or "") for p in _reader(run).pages]
    for i, title in enumerate(SLIDE_TITLES, start=1):
        assert title in texts[i], f"page {i + 1} lacks {title!r}: {texts[i][:200]!r}"


def test_no_placeholder_text(run: PipelineResult) -> None:
    text = "\n".join(p.extract_text() or "" for p in _reader(run).pages)
    assert [s for s in FORBIDDEN if s in text] == []
    # The deck prints "Source database {database_name}" on the Assessment
    # Summary slide (src/report/pptx_report.py's slide_summary); the deck's
    # title/creator metadata ("Database Modernizer Assessment") is NOT
    # part of any page's drawn content, so pypdf's extract_text() never sees
    # it -- verified against both samples' rendered PDFs, where run.db
    # (exactly, not just case-insensitively) is present in the extracted text.
    assert run.db in text, f"source database name {run.db!r} missing from report text"


def _resolve(obj):
    return obj.get_object() if isinstance(obj, IndirectObject) else obj


def _fonts_shown(content: bytes) -> set[str]:
    """Font resource names (e.g. ``F2+0``) actually used to draw a glyph.

    reportlab's ``Canvas`` always registers the base ``Helvetica`` font as ``/F1``
    in every page's ``/Resources`` and opens each page with a no-op
    ``BT /F1 ... Tf ... ET`` before the real drawing begins — the resource is
    present, but nothing is ever shown with it (verified against this deck's own
    content streams: ``/F1`` is set but never followed by a ``Tj``/``TJ`` while
    it is the active font). Counting unused resources as "used" would flag that
    harmless reportlab artifact as a fallback-font bug on every single page.
    """
    text = content.decode("latin1")
    current: str | None = None
    used: set[str] = set()
    for m in re.finditer(r"/(F\d+(?:\+\d+)?)\s+[\d.]+\s+Tf|(Tj|TJ)", text):
        if m.group(1):
            current = m.group(1)
        elif current:
            used.add(current)
    return used


def _page_content_bytes(page) -> bytes:
    contents = _resolve(page.get("/Contents"))
    if contents is None:
        return b""
    if isinstance(contents, ArrayObject):
        return b"".join(bytes(_resolve(part).get_data()) for part in contents)
    return bytes(contents.get_data())


def test_fonts_are_embedded(run: PipelineResult) -> None:
    missing = []
    checked_pages = 0
    for n, page in enumerate(_reader(run).pages, start=1):
        shown = _fonts_shown(_page_content_bytes(page))
        if not shown:
            continue  # e.g. an image-only page draws no text, so nothing to check
        checked_pages += 1

        fonts = _resolve(_resolve(page.get("/Resources") or {}).get("/Font") or {})
        for key, ref in fonts.items():
            name = str(key).lstrip("/")
            if name not in shown:
                continue  # resource present (reportlab default) but never drawn
            font = _resolve(ref)
            base = str(font.get("/BaseFont"))
            if base.endswith(("Helvetica", "Helvetica-Bold")):
                missing.append(f"p{n}:{base} (fallback font used)")
                continue
            desc = font.get("/FontDescriptor")
            if font.get("/Subtype") == "/Type0":
                desc = _resolve(font["/DescendantFonts"][0]).get("/FontDescriptor")
            desc = _resolve(desc) if desc else None
            if not desc or not any(k in desc for k in ("/FontFile", "/FontFile2", "/FontFile3")):
                missing.append(f"p{n}:{base}")
    # Without this, a page-shown-fonts regression (e.g. every page becoming
    # "image-only") would make `missing` trivially empty and this test
    # vacuously pass without having checked anything.
    assert checked_pages > 0, "no page had any drawn text -- nothing was actually checked"
    assert missing == []


# Branding and internal classification metadata a public sample must not carry
# (issue #181). Moved here from tests/unit/report/test_deck_branding.py: reading
# the PDF needs pypdf, which only the e2e extra installs.
BRANDING = re.compile(r"transform|confidential|pending_classification|msip_label", re.IGNORECASE)
DECK_NAME = "Database Modernizer Assessment"
DISCLAIMER = "Amazon Web Services, Inc. or its affiliates. For informational purposes only."


def test_pdf_metadata_names_the_assessment(run: PipelineResult) -> None:
    meta = _reader(run).metadata
    assert meta is not None
    assert meta.author == DECK_NAME
    assert meta.creator == DECK_NAME
    assert not BRANDING.search(" ".join(str(v) for v in meta.values()))


def test_every_page_carries_the_disclaimer_once(run: PipelineResult) -> None:
    for n, page in enumerate(_reader(run).pages, start=1):
        text = _normalize(page.extract_text() or "")
        # Exactly once: the template used to hide a second footer line behind
        # the full-bleed background, which the text layer still carried.
        assert text.count(DISCLAIMER) == 1, f"page {n}: {text.count(DISCLAIMER)} footers"
        assert re.search(r"© \d{4} " + re.escape(DISCLAIMER), text), f"page {n}"
        assert not BRANDING.search(text), f"page {n}"
