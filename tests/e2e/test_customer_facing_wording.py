"""No internal wording reaches a customer-facing artifact (#572, extends #406).

#406 fixed one specific leak: a bare issue tag (``#167``) surviving in a
reality-check consolidation's ``reason``. This guard is the general case,
run against the real deterministic wordpress/discourse pipeline output (the
same ``run`` fixture every other ``tests/e2e/`` module shares, so this does
not run the pipeline a second time): ``report.json``,
``assignment/v*/assignment.json``'s per-query reasons, the reality-check
output, and the rendered deliverables: the engineering Markdown report and
the executive report (the decision report HTML #572 also named was retired in #987).

The scanner (``src.tools.validation.customer_text_guard``) flags a raw engine
id where a display name belongs, an ``ADR-NNN`` reference, a snake_case
reason code, and a known log-only phrase -- excluding id-holding fields by
schema path (``ID_FIELDS``), never by string matching. See that module's
docstring for the full rationale.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.tools.validation.customer_text_guard import Finding, scan_json, scan_markdown, scan_text
from tests.e2e.pipeline import PipelineResult

# Accepted hits this run does not fix, each tracked by an open issue -- see that
# issue for why it is not a small, contained renderer/reason-builder fix. Keyed by
# (artifact, finding.kind, finding.text) so an unrelated new hit on the same
# artifact still fails the test. Empty today: every hit the #572 scan actually
# found in the bundled wordpress/discourse samples was fixed at its source (see
# the #572 PR description for the list); #584 (the signal-override reason's raw
# signal name and engine id) is fixed at its source too: the reason names the
# signal and the engine by their readable forms.
ALLOWLIST: set[tuple[str, str, str]] = set()


def _version_sort_key(path: Path) -> int:
    digits = path.parent.name[1:]
    return int(digits) if digits.isdigit() else -1


def _effective_report_json(run: PipelineResult) -> Path:
    cands = sorted((run.job_dir() / "synthesis").glob("v*/report.json"), key=_version_sort_key)
    assert cands, f"no synthesis/v*/report.json under {run.job_dir()}"
    return cands[-1]


def _assignment_jsons(run: PipelineResult) -> list[Path]:
    cands = sorted((run.job_dir() / "assignment").glob("v*/assignment.json"))
    assert cands, f"no assignment/v*/assignment.json under {run.job_dir()}"
    return cands


def _filtered(findings: list[Finding], artifact: str) -> list[Finding]:
    return [f for f in findings if (artifact, f.kind, f.text) not in ALLOWLIST]


def _describe(findings: list[Finding]) -> list[tuple[str, str, str]]:
    return [(f.kind, f.path, f.excerpt) for f in findings]


def test_report_json_has_no_internal_wording(run: PipelineResult) -> None:
    findings = scan_json(json.loads(_effective_report_json(run).read_text()))
    findings = _filtered(findings, "report.json")
    assert findings == [], _describe(findings)


def test_assignment_reasons_have_no_internal_wording(run: PipelineResult) -> None:
    for path in _assignment_jsons(run):
        findings = scan_json(json.loads(path.read_text()))
        findings = _filtered(findings, "assignment.json")
        assert findings == [], (path.relative_to(run.job_dir()), _describe(findings))


def test_reality_check_output_has_no_internal_wording(run: PipelineResult) -> None:
    path = run.job_dir() / "reality-check" / "output.json"
    if not path.is_file():
        # Nothing to scan: the run must then have no reality-check file at all.
        assert not list((run.job_dir() / "reality-check").glob("*.json"))
        return
    findings = scan_json(json.loads(path.read_text()))
    findings = _filtered(findings, "reality-check/output.json")
    assert findings == [], _describe(findings)


def test_engineering_report_markdown_has_no_internal_wording(run: PipelineResult) -> None:
    findings = scan_markdown(run.deliverable("engineering-report.md").read_text())
    findings = _filtered(findings, "engineering-report.md")
    assert findings == [], _describe(findings)


def test_executive_summary_pdf_has_no_internal_wording(run: PipelineResult) -> None:
    from pypdf import PdfReader

    reader = PdfReader(str(run.deliverable("executive-report.pdf")))
    text = "\n".join(p.extract_text() or "" for p in reader.pages)
    findings = _filtered(scan_text(text), "executive-report.pdf")
    assert findings == [], _describe(findings)


@pytest.mark.deterministic
def test_designed_report_json_has_no_internal_wording(designed_run: PipelineResult) -> None:
    """The samples' own jobs run no schema design, so their report.json has no
    ``table_mappings``; the designed job does (#212: each row's owner line holds engine
    ids, data the guard must treat as ids). Caught here, without an e2e-llm run."""
    report = json.loads(designed_run.report_json().read_text())
    assert report["table_mappings"] and report["table_mappings"][0]["owner"]
    assert report["facts"]["table_owner_lines"]["table_lines"]
    findings = _filtered(scan_json(report), "report.json")
    assert findings == [], _describe(findings)
    md = scan_markdown(designed_run.deliverable("engineering-report.md").read_text())
    md = _filtered(md, "engineering-report.md")
    assert md == [], _describe(md)
