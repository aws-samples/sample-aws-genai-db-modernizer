"""ci/llm/judge.py reads a real PDF: page text and the slide title (the
largest-font text). Lives under tests/e2e because pypdf ships in the e2e
extra, which the unit-test job doesn't install."""

from __future__ import annotations

from pathlib import Path

import pypdf

from ci.llm import judge


def test_extract_pdf_pages_reads_title_from_largest_font(tmp_path: Path) -> None:
    content = b"BT /F1 24 Tf 20 150 Td (Big Title) Tj ET BT /F1 10 Tf 20 100 Td (Body text.) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
    ]
    pdf, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(pdf)
    pdf += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    pdf += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    pdf += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    path = tmp_path / "deck.pdf"
    path.write_bytes(pdf)

    pages = judge.extract_pdf_pages(path, pypdf)
    assert len(pages) == 1
    title, raw = pages[0]
    assert title == "Big Title"
    assert judge.format_pdf_pages(pages).splitlines() == ["[page 1: Big Title]", "Body text."]
