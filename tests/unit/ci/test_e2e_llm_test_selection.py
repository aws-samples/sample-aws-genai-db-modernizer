"""ci/e2e-llm.sh checks the run's deliverables, not the help site (#353)."""

from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / "ci" / "e2e-llm.sh"


def test_report_tests_leave_out_the_docs_site_suite():
    text = SCRIPT.read_text()
    start = text.index("run_report_tests() {")
    block = text[start : text.index("}", start)]
    assert "--ignore=tests/e2e/test_docs_site.py" in block
    assert "--ignore=tests/e2e/test_ui.py" in block
