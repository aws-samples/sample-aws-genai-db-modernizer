"""One product name: "Database Modernizer Assessment Tool" (#954).

No rendered deliverable may carry "ATX", "AWS Transform" or the standalone word
"transform" in any case (``tests/branding.py``; "transformation" is ordinary
prose). The check runs over every file of each sample's deliverables folder
(#987): the executive report's text and document metadata, the engineering report, the
assessment data JSON and the folder index.
"""

from __future__ import annotations

import io
import re
from pathlib import Path

from pypdf import PdfReader

from src.report.deliverables import FOLDER_FILENAMES
from tests.branding import hits as found
from tests.e2e.pipeline import PipelineResult


def _texts(path: Path) -> list[str]:
    data = path.read_bytes()
    if path.suffix == ".pdf":
        reader = PdfReader(io.BytesIO(data))
        meta = " ".join(str(v) for v in (reader.metadata or {}).values())
        return [meta, *[(p.extract_text() or "") for p in reader.pages]]
    return [data.decode("utf-8", "replace")]


def test_no_deliverable_names_another_product(run: PipelineResult) -> None:
    paths = [Path(p) for p in run.report.get("deliverables", [])]
    assert sorted(p.name for p in paths) == sorted(FOLDER_FILENAMES)
    hits: list[str] = []
    for path in paths:
        for text in _texts(path):
            hits.extend(f"{path.name}: {h}" for h in found(text))
    assert hits == []


def test_the_pdf_and_the_index_name_this_product(run: PipelineResult) -> None:
    assert "Database Modernizer Assessment Tool" in " ".join(
        _texts(run.deliverable("executive-report.pdf"))
    )
    assert "Database Modernizer Assessment Tool" in run.deliverable("index.md").read_text()


RETIRED_PROSE = re.compile(r"\b(?:decision|(?:interactive )?analysis) report\b", re.IGNORECASE)


def test_no_deliverable_points_at_a_retired_report(run: PipelineResult) -> None:
    """#987: no deliverable sends a reader to the retired decision or analysis report."""
    hits = [
        f"{Path(p).name}: {m}"
        for p in run.report.get("deliverables", [])
        for text in _texts(Path(p))
        for m in RETIRED_PROSE.findall(text)
    ]
    assert hits == []
