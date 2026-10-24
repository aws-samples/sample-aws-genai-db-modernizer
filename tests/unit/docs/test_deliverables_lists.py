"""Every place that lists the customer deliverables names the same set.

The set is the one ``scripts/run_report.py`` writes to
``artifacts/<db>/<job>/deliverables/`` (``src.report.deliverables.FOLDER_FILENAMES``).
Each surface marks its list with ``<!-- deliverables-list -->`` ...
``<!-- /deliverables-list -->``; inside the markers it must name exactly that set
(plus any extra this test allows for that surface) and point at ``deliverables/``.
Outside the markers, no surface may point readers at the old names or at
``synthesis/v<N>/`` for the deliverables.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.report import deliverables
from tests.branding import hits

REPO = Path(__file__).resolve().parents[3]

# Spelled out on purpose (see tests/unit/scripts/test_run_report.py).
DOCUMENTED_FILES = {
    "executive-report.pdf",
    "engineering-report.md",
    "assessment-data.json",
    "engineering-report-detailed.md",
    "index.md",
}

# surface -> filenames it may name in its list beyond the documented set
SURFACES: dict[str, set[str]] = {
    ".claude/commands/modernize.md": set(),
    ".claude/commands/synthesize.md": set(),
    "README.md": set(),
    "docs/understand-the-results.md": set(),
    "docs/other-ways-to-run-it.md": set(),
    # The help site also renders the engineering report to HTML for the browser.
    "docs/sample.md": {"engineering-report.html"},
}

# Every published page, the repo guides and every command: none may send readers
# to the old file names or to the versioned folder for the deliverables.
SCANNED = sorted(
    {
        *SURFACES,
        "AGENTS.md",
        "CONTRIBUTING.md",
        "mkdocs.yml",
        *(str(p.relative_to(REPO)) for p in (REPO / "docs").glob("*.md")),
        *(str(p.relative_to(REPO)) for p in (REPO / ".claude" / "commands").glob("*.md")),
    }
)

_BLOCK_RE = re.compile(r"<!-- deliverables-list -->(.*?)<!-- /deliverables-list -->", re.DOTALL)
_FILE_RE = re.compile(r"(?<![\w/.-])([A-Za-z0-9_-]+\.(?:pdf|pptx|md|json|html))\b")
_LEGACY = (
    "summary-executive-report",
    "executive-summary.pdf",
    "executive-summary.pptx",
    "assessment.pdf",  # the multi-page PDF report the executive report replaced (#1020)
)
_VERSIONED_POINTER_RE = re.compile(r"deliverables[^.\n]*synthesis/v|synthesis/v[^.\n]*deliverables")


def test_the_documented_set_is_the_set_the_code_writes() -> None:
    assert set(deliverables.FOLDER_FILENAMES) == DOCUMENTED_FILES


@pytest.mark.parametrize("surface", sorted(SURFACES))
def test_each_list_names_exactly_the_documented_set(surface: str) -> None:
    text = (REPO / surface).read_text(encoding="utf-8")
    blocks = _BLOCK_RE.findall(text)
    assert blocks, f"{surface}: no <!-- deliverables-list --> block"
    for block in blocks:
        named = set(_FILE_RE.findall(block))
        assert named == DOCUMENTED_FILES | SURFACES[surface], (
            surface,
            sorted(named ^ (DOCUMENTED_FILES | SURFACES[surface])),
        )
        assert "deliverables/" in block, f"{surface}: list does not point at deliverables/"
        # Branding: "Database Modernizer Assessment Tool" is the only product name.
        assert hits(block) == [], surface


@pytest.mark.parametrize("path", SCANNED)
def test_no_surface_points_at_old_names_or_the_versioned_folder(path: str) -> None:
    text = (REPO / path).read_text(encoding="utf-8")
    for legacy in _LEGACY:
        assert legacy not in text, (path, legacy)
    assert not _VERSIONED_POINTER_RE.search(text), (path, _VERSIONED_POINTER_RE.search(text))


def test_the_pdf_points_at_real_files_in_the_deliverables_folder() -> None:
    from src.report import executive_report

    named = set(_FILE_RE.findall(executive_report.CLOSING_POINTER))
    assert named == {"engineering-report.md", "index.md"}
    assert named <= DOCUMENTED_FILES, named


def test_the_assessment_data_title_matches_what_the_reports_call_it() -> None:
    """The reports point at "the Assessment Data (JSON)"; the index must use
    that name for assessment-data.json, so the pointer resolves."""
    entry = next(e for e in deliverables.FOLDER_ENTRIES if e.filename == "assessment-data.json")
    assert entry.title == "Assessment Data (JSON)"
    for module in ("engineering_report", "close_calls", "unservable"):
        src = (REPO / "src" / "report" / f"{module}.py").read_text(encoding="utf-8")
        assert "Assessment Data (JSON)" in src, module
