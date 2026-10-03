"""Cross-deliverable risk-count consistency (issue #201), and the fixed source
of those risks (issue #210).

The decision report, the engineering report and the executive summary deck
(PDF/PPTX) must all report the same risk totals for the same ``report.json``.
Before #201, the decision/engineering reports filtered out risks with no body
text (``renderers._risk_has_content``) before counting, while
``pptx_report.derive`` counted every risk in ``risk_assessment.risks``
unfiltered. On the wordpress evidence run that combination showed "9
migration risks identified" in the decision report and 12 in the PDF.

The 3 "empty" risks were never meant to be empty: #210 found that
``synthesis_report.build_risk_assessment`` read only ``pattern_type``/
``recommendation`` for schema-design unsupported patterns, fields only the
dynamodb contract has. ElastiCache's unsupported patterns (``reason``/
``workaround`` instead) became risks with the literal text
``"[elasticache] unknown: "``. With #210 fixed, synthesis never produces an
empty risk for this fixture, so the correct reconciled total is 12 -- not 9 --
and ``filtered_risks`` drops nothing.

Fixture: ``wordpress_report.json`` is a trimmed copy of the real synthesis
report from the headless wordpress e2e run that triggered #201 and #210.
Its three previously-empty risks (RISK-007/008/009) have been regenerated
from the fixture's own ``schema_designs.elasticache.unsupported_patterns``
(the real evidence data) via the fixed ``build_risk_assessment`` field
reading, so the fixture reflects what fixed synthesis actually produces.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from src.agents.referee.synthesis_data import EngineArtifacts, SynthesisData
from src.agents.referee.synthesis_report import build_risk_assessment
from src.report import pptx_report, renderers

FIXTURE = Path(__file__).parent / "fixtures" / "wordpress_report.json"


@pytest.fixture(scope="module")
def report() -> dict:
    data: dict = json.loads(FIXTURE.read_text())
    return data


class TestFixedSynthesisProducesRealText:
    """Re-running build_risk_assessment on the real evidence inputs (#210)."""

    def test_elasticache_unsupported_patterns_from_the_fixture_yield_real_text(
        self, report: dict
    ) -> None:
        """The exact evidence data that used to produce 3 contentless risks."""
        elasticache_patterns = report["schema_designs"]["elasticache"]["unsupported_patterns"]
        assert len(elasticache_patterns) == 3

        data = SynthesisData(job_id="job-x", database_name="wordpress")
        data.triage = {"selected_agents": [{"agent_type": "elasticache"}]}
        data.engines["elasticache"] = EngineArtifacts(
            engine="elasticache",
            analysis={},
            schema_design={"unsupported_patterns": elasticache_patterns},
        )

        out = build_risk_assessment(data)
        assert len(out["risks"]) == 3
        for risk in out["risks"]:
            assert "unknown:" not in risk["description"].lower()
            assert risk["mitigation"]

    def test_fixture_risks_all_carry_real_text(self, report: dict) -> None:
        """The committed fixture reflects fixed synthesis: 12 risks, all non-empty."""
        risks = report["risk_assessment"]["risks"]
        assert len(risks) == 12
        for risk in risks:
            assert renderers._risk_has_content(risk.get("description")), risk


class TestEmptyRiskGuard:
    """``filtered_risks`` / ``_risk_has_content`` remain a guard against a
    genuinely contentless entry, independent of the #210 fix at the source."""

    def test_synthetic_empty_description_is_dropped(self) -> None:
        assert renderers._risk_has_content("[elasticache] unknown: ") is False
        assert renderers._risk_has_content("[elasticache] unknown:") is False
        assert renderers._risk_has_content("") is False
        assert renderers._risk_has_content(None) is False

    def test_filtered_risks_drops_a_synthetic_empty_entry(self, report: dict) -> None:
        rigged = json.loads(json.dumps(report))
        rigged["risk_assessment"]["risks"].append(
            {
                "risk_id": "RISK-999",
                "risk_type": "MIGRATION_COMPLEXITY",
                "severity": "MEDIUM",
                "description": "[elasticache] unknown: ",
                "affected_tables": [],
                "mitigation": None,
            }
        )
        kept = renderers.filtered_risks(rigged)
        assert len(kept) == len(report["risk_assessment"]["risks"])
        assert all(r["risk_id"] != "RISK-999" for r in kept)


class TestSharedRiskFilter:
    def test_filtered_risks_keeps_all_twelve(self, report: dict) -> None:
        kept = renderers.filtered_risks(report)
        assert len(kept) == 12
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
        """The regression this guards: before #201/#210 this was 9 / 9 / 12."""
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
        assert html_n == md_n == pptx_n == len(renderers.filtered_risks(report)) == 12
