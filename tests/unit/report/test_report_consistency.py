"""Cross-deliverable risk-count consistency (issue #201).

The decision report, the engineering report and the executive summary deck
(PDF/PPTX) must all report the same risk totals for the same ``report.json``.
Before this fix, the decision/engineering reports filtered out risks with no
body text (``renderers._risk_has_content``) before counting, while
``pptx_report.derive`` counted every risk in ``risk_assessment.risks``
unfiltered -- so a report with malformed empty risks produced a decision
report that said "9 migration risks identified" while the PDF said 12.

Fixture: ``wordpress_report.json`` is a trimmed copy of the real synthesis
report from the headless wordpress e2e run that triggered this bug report
(GitHub issue #201). Its ``risk_assessment.risks`` has 12 entries, 3 of which
are the malformed ``[elasticache] unknown:`` risks with no body text.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from src.report import pptx_report, renderers

FIXTURE = Path(__file__).parent / "fixtures" / "wordpress_report.json"


@pytest.fixture(scope="module")
def report() -> dict:
    data: dict = json.loads(FIXTURE.read_text())
    return data


def test_fixture_has_three_empty_risks(report: dict) -> None:
    """Sanity check on the fixture itself: 12 risks, 3 with no body."""
    risks = report["risk_assessment"]["risks"]
    assert len(risks) == 12
    empty = [r for r in risks if not renderers._risk_has_content(r.get("description"))]
    assert len(empty) == 3


class TestSharedRiskFilter:
    def test_filtered_risks_drops_the_empty_ones(self, report: dict) -> None:
        kept = renderers.filtered_risks(report)
        assert len(kept) == 9
        assert all(renderers._risk_has_content(r.get("description")) for r in kept)

    def test_decision_report_count_matches_filtered_risks(self, report: dict) -> None:
        n = len(renderers.filtered_risks(report))
        html = renderers.render_decision_report_html(report)
        assert f"{n} migration risks identified" in html

    def test_engineering_report_count_matches_filtered_risks(self, report: dict) -> None:
        n = len(renderers.filtered_risks(report))
        md = renderers.render_engineering_report_md(report)
        assert f"## Risk register ({n})" in md

    def test_pptx_derive_risk_count_matches_filtered_risks(self, report: dict) -> None:
        """The executive summary deck (and, by construction, its PDF rendering)
        must count the same filtered risk list as the two text reports."""
        n = len(renderers.filtered_risks(report))
        facts = pptx_report.derive(report, {})
        assert len(facts["risks"]) == n

    def test_all_three_deliverables_agree(self, report: dict) -> None:
        """The actual regression: before the fix this was 9 / 9 / 12."""
        html_n = int(
            re.search(
                r"(\d+) migration risks identified", renderers.render_decision_report_html(report)
            ).group(1)
        )
        md_n = int(
            re.search(
                r"## Risk register \((\d+)\)", renderers.render_engineering_report_md(report)
            ).group(1)
        )
        pptx_n = len(pptx_report.derive(report, {})["risks"])
        assert html_n == md_n == pptx_n == len(renderers.filtered_risks(report))
