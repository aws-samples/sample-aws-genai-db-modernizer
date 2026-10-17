"""Nothing in the repository points at a retired deliverable (#987).

The decision report HTML, the interactive analysis report HTML and the UI's
"Export to HTML" were retired: the executive report and the UI results page are the
primary deliverables, the engineering report and the assessment data JSON the
supporting ones. Their renderers, templates and the template sync script are
gone, so no tracked file (code, tests, docs, commands, CI) may name them, and
none of their files may come back.
"""

from __future__ import annotations

import json
import re
import subprocess  # nosec B404 -- runs git ls-files with fixed argv
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
THIS = Path(__file__).resolve().relative_to(REPO).as_posix()

# Files that must not exist any more.
REMOVED_FILES = (
    "src/report/decision_report.py",
    "src/report/analysis_report.py",
    "src/report/templates/analysis_report.css",
    "src/report/templates/analysis_report.js",
    "src/report/templates/analysis_report.html.tpl",
    "scripts/sync_report_template.py",
    "src/ui/src/utils/ExportReport.js",
)

# Names a reference to a retired file or entry point would use.
RETIRED_NAMES = re.compile(
    r"decision[-_]report(?!s)|analysis[-_]report|ExportReport|generateHTMLReport|"
    r"sync_report_template|report_template_sync|render_decision_report_html|"
    r"render_analysis_report_html|build_export_data|export-to-html|Export to HTML"
)

# pytest-split's generated timing file is checked by its own staleness rule
# (ci/README.md); every other tracked text file is scanned.
SKIPPED = {THIS, ".test_durations"}

# The one place that may still spell a retired file's name: the list the render
# step deletes from an older job's folder (``RETIRED_FOLDER_FILENAMES``).
ALLOWED = {
    "src/report/deliverables.py": [
        '    "decision-report.html",',
        '    "analysis-report.html",',
        '    "assessment.pdf",',
    ],
}


def _tracked_files() -> list[str]:
    out = subprocess.run(  # nosec B603 B607 -- fixed argv
        ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, check=True
    ).stdout
    return [p for p in out.decode().split("\0") if p]


def test_the_retired_files_are_gone() -> None:
    assert [p for p in REMOVED_FILES if (REPO / p).exists()] == []


def test_no_tracked_file_names_a_retired_deliverable() -> None:
    hits: list[str] = []
    for rel in _tracked_files():
        if rel in SKIPPED or RETIRED_NAMES.search(rel):
            if rel not in SKIPPED:
                hits.append(f"{rel}: file name")
            continue
        path = REPO / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        allowed = list(ALLOWED.get(rel, []))
        for n, line in enumerate(text.splitlines(), start=1):
            m = RETIRED_NAMES.search(line)
            if m and line in allowed:
                allowed.remove(line)
            elif m:
                hits.append(f"{rel}:{n}: {m.group(0)}")
    assert hits == [], "\n".join(hits[:80])


def test_the_allowed_names_are_exactly_the_retired_folder_files() -> None:
    from src.report.deliverables import RETIRED_FOLDER_FILENAMES

    names = {line.strip().strip('",') for line in ALLOWED["src/report/deliverables.py"]}
    assert names == set(RETIRED_FOLDER_FILENAMES)


# Prose in what the render step writes: no deliverable may send a reader to a
# retired report by name ("the Decision Report", "the interactive analysis report").
RETIRED_PROSE = re.compile(r"\b(?:decision|(?:interactive )?analysis) report\b", re.IGNORECASE)
FIXTURES = REPO / "tests" / "unit" / "report" / "fixtures"


@pytest.mark.parametrize("name", sorted(p.name for p in FIXTURES.glob("*.json")))
def test_no_rendered_deliverable_points_at_a_retired_report(name: str, tmp_path: Path) -> None:
    from src.report import deliverables as dl
    from src.storage.local_store import LocalArtifactStore
    from tests.executive_pdf import executive_text

    report = json.loads((FIXTURES / name).read_text())
    store = LocalArtifactStore(base_dir=str(tmp_path))
    key = "db/job-1/synthesis/v1/report.json"
    store.write_json(key, report)
    rendered = dl.render_deliverables(store, "job-1", "db", key)
    files = {e.filename: d.content for e, d in rendered.folder_files()}
    assert set(files) == {e.filename for e in dl.FOLDER_ENTRIES}
    texts = {
        f: (executive_text(report) if f.endswith(".pdf") else data.decode("utf-8"))
        for f, data in files.items()
    }
    texts[dl.INDEX_FILENAME] = dl.render_folder_index(
        "db", "job-1", 1, [e for e, _ in rendered.folder_files()]
    )
    hits = {f: RETIRED_PROSE.findall(t) for f, t in texts.items() if RETIRED_PROSE.search(t)}
    assert hits == {}
