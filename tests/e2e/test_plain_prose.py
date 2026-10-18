"""Customer deliverables carry no item of the plain-prose style list (#1034).

The style list (``src.shared.prose_style``) holds the machine-writing tells kept
out of customer text: em/en dashes used as punctuation, filler words, hedges,
"not just ... but" and similar constructions, and all-caps severity labels in
prose. Deterministic templates are fixed at their source; model text is cleaned
at its LLM seam. This guard runs on every sample's deliverables (and, in the
external-job mode, on a model run's): the engineering report, the PDF report's
text and every customer-facing string of ``report.json`` and
``assessment-data.json``. Renderers only format (ADR-032), so a hit here is
fixed where the text is produced, never filtered in a renderer.
"""

from __future__ import annotations

import json

from pypdf import PdfReader

from src.shared.prose_style import collector_identifiers, find_style_hits
from src.tools.validation.customer_text_guard import scan_json_style
from tests.e2e.pipeline import PipelineResult


def _identifiers(run: PipelineResult) -> set[str]:
    """The run's collected table and column names: data, not words."""
    path = run.job_dir() / "collector" / "output.json"
    return collector_identifiers(json.loads(path.read_text())) if path.is_file() else set()


def _describe(hits: list) -> list[tuple[str, str, str]]:
    return [(h.kind, h.term, h.excerpt) for h in hits]


def test_engineering_report_has_no_style_hits(run: PipelineResult) -> None:
    hits = find_style_hits(run.deliverable("engineering-report.md").read_text(), _identifiers(run))
    assert hits == [], _describe(hits)


def test_executive_report_pdf_has_no_style_hits(run: PipelineResult) -> None:
    reader = PdfReader(str(run.deliverable("executive-report.pdf")))
    text = "\n".join(p.extract_text() or "" for p in reader.pages)
    hits = find_style_hits(text, _identifiers(run))
    assert hits == [], _describe(hits)


def test_assessment_data_has_no_style_hits(run: PipelineResult) -> None:
    findings = scan_json_style(
        json.loads(run.deliverable("assessment-data.json").read_text()),
        identifiers=_identifiers(run),
    )
    assert findings == [], [(f.path, f.kind, f.term, f.excerpt) for f in findings]


def test_report_json_has_no_style_hits(run: PipelineResult) -> None:
    findings = scan_json_style(
        json.loads(run.report_json().read_text()), identifiers=_identifiers(run)
    )
    assert findings == [], [(f.path, f.kind, f.term, f.excerpt) for f in findings]
