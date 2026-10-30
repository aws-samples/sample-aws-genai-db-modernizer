# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The risks the assignment resolved, by severity, as a report fact (#1020, ADR-032).

"Low risk, and the assignment already resolved 2 high and 4 medium risks" is the
risk story the executive report tells, so the per-severity figure is computed once,
next to ``resolved_risk_count``, and every deliverable prints the stored map.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.contracts.synthesis_output import ReportFacts
from src.report.facts import report_facts
from src.shared.report_facts import compute_report_facts, resolved_risk_severity_facts
from tests.executive_pdf import engineering_text, executive_text

FIXTURES = Path(__file__).resolve().parents[1] / "report" / "fixtures"


def _resolved(*severities: str) -> dict[str, Any]:
    return {
        "risk_assessment": {
            "overall_risk_level": "LOW",
            "risks": [],
            "resolved_risks": [
                {
                    "severity": s,
                    "engine": "aurora_mysql",
                    "resolved_on": "dynamodb",
                    "description": f"[aurora_mysql] A {s.lower()} risk.",
                    "reason": "the Aurora MySQL analysis recommended DynamoDB for these queries",
                }
                for s in severities
            ],
        }
    }


class TestResolvedRiskSeverityFacts:
    def test_counts_each_severity_in_order_and_skips_zero(self) -> None:
        report = _resolved("MEDIUM", "HIGH", "medium", "MEDIUM", "HIGH", "MEDIUM")
        assert resolved_risk_severity_facts(report) == {
            "resolved_risks_by_severity": {"HIGH": 2, "MEDIUM": 4}
        }

    def test_an_unknown_severity_is_unrated_and_nothing_is_lost(self) -> None:
        facts = resolved_risk_severity_facts(_resolved("HIGH", "SEVERE"))
        assert facts["resolved_risks_by_severity"] == {"HIGH": 1, "UNRATED": 1}

    def test_none_resolved_is_an_empty_map(self) -> None:
        assert resolved_risk_severity_facts({}) == {"resolved_risks_by_severity": {}}

    def test_in_the_facts_block_and_the_contract(self) -> None:
        report = json.loads((FIXTURES / "wordpress_report.json").read_text())
        facts = compute_report_facts(report)
        assert sum(facts["resolved_risks_by_severity"].values()) == facts["resolved_risk_count"]
        ReportFacts.model_validate(facts)


class TestOldReports:
    def test_a_saved_1_26_report_gains_the_map_when_read(self) -> None:
        report = json.loads((FIXTURES / "wordpress_1_26_report.json").read_text())
        assert "resolved_risks_by_severity" not in report["facts"]
        facts = report_facts(report)
        assert facts["resolved_risks_by_severity"] == {"HIGH": 2, "MEDIUM": 4}
        assert facts["resolved_risk_count"] == report["facts"]["resolved_risk_count"] == 6

    def test_a_stored_map_is_read_as_written(self) -> None:
        report = json.loads((FIXTURES / "wordpress_1_26_report.json").read_text())
        report["facts"]["resolved_risks_by_severity"] = {"CRITICAL": 9}
        assert report_facts(report)["resolved_risks_by_severity"] == {"CRITICAL": 9}


class TestBothReportsSayIt:
    def _report(self) -> dict[str, Any]:
        report: dict[str, Any] = json.loads((FIXTURES / "wordpress_1_26_report.json").read_text())
        report["risk_assessment"]["risks"] = []
        report["risk_assessment"]["overall_risk_level"] = "LOW"
        report.pop("facts")
        return report

    def test_the_executive_report_tells_the_low_risk_story(self) -> None:
        text = executive_text(self._report())
        assert "LOW Overall risk 0 Open risks 6 Resolved by the assignment 2 high, 4 medium" in text
        assert "No open migration risks; 6 resolved by the assignment (2 high, 4 medium)." in text
        assert "Resolved by the assignment: 2 high, 4 medium" in text

    def test_one_line_per_resolved_high_saying_what_resolved_it(self) -> None:
        report = self._report()
        text = executive_text(report)
        highs = [r for r in report["risk_assessment"]["resolved_risks"] if r["severity"] == "HIGH"]
        assert len(highs) == 2
        for r in highs:
            assert f"resolved because {r['reason']}." in text
        mediums = [
            r for r in report["risk_assessment"]["resolved_risks"] if r["severity"] == "MEDIUM"
        ]
        reasons = {r["reason"] for r in mediums} - {r["reason"] for r in highs}
        assert reasons and not any(f"resolved because {x}" in text for x in reasons)

    def test_the_engineering_report_says_the_same(self) -> None:
        text = engineering_text(self._report())
        assert "No open migration risks; 6 resolved by the assignment (2 high, 4 medium)." in text

    def test_both_print_the_stored_map(self) -> None:
        report = self._report()
        report["facts"] = compute_report_facts(report)
        report["facts"]["resolved_risks_by_severity"] = {"CRITICAL": 1, "LOW": 5}
        assert "(1 critical, 5 low)" in engineering_text(report)
        assert "Resolved by the assignment 1 critical, 5 low" in executive_text(report)


def test_two_resolved_highs_that_read_the_same_give_one_line() -> None:
    """The merged-wave discourse fixture resolves two high risks the same way; the card
    states it once (its label as the PDF prints it: the font has no arrow)."""
    report = json.loads((FIXTURES / "discourse_merged_wave_report.json").read_text())
    line = (
        "DocumentDB to Aurora PostgreSQL: resolved because the queries run as SQL on "
        "Aurora PostgreSQL."
    )
    highs = [
        r
        for r in report["risk_assessment"]["resolved_risks"]
        if r["severity"] == "HIGH"
        and r.get("reason") == "the queries run as SQL on Aurora PostgreSQL"
    ]
    assert len(highs) >= 2
    assert executive_text(report).count(line) == 1
