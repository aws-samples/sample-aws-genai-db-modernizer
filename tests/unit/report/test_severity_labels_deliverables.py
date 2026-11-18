# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#2442: the deliverables word a severity the way the results page does.

The stored ids stay upper case in ``report.json``; the engineering report, the
executive report PDF and the printed summary show "High" and "Overall risk: high".
"""

from __future__ import annotations

import copy
from typing import Any

from src.report import renderers
from src.report.executive_report import render_executive_report_pdf
from src.shared.severity_labels import SEVERITY_LABELS, severity_label, severity_word


def test_labels_are_sentence_case_and_unknown_is_unrated() -> None:
    assert [severity_label(s) for s in ("CRITICAL", "HIGH", "MEDIUM", "LOW")] == [
        "Critical",
        "High",
        "Medium",
        "Low",
    ]
    assert severity_label("high") == "High"
    assert severity_label(None) == "Unrated"
    assert severity_label("bogus") == "Unrated"
    assert severity_word("HIGH") == "high"
    assert set(SEVERITY_LABELS) == {"CRITICAL", "HIGH", "MEDIUM", "LOW"}


def _risk(severity: str) -> dict[str, Any]:
    return {
        "risk_id": "RISK-001",
        "risk_type": "MIGRATION_COMPLEXITY",
        "severity": severity,
        "description": "[dynamodb] RISK-001 needs a mitigation",
        "mitigation": "Batch the writes.",
    }


def _report(level: str = "HIGH") -> dict[str, Any]:
    return {
        "database_name": "wordpress",
        "risk_assessment": {
            "overall_risk_level": level,
            "risks": [_risk(level)],
            "mitigation_strategies": [],
        },
    }


def test_the_engineering_report_words_severities_like_the_results_page() -> None:
    report = _report("HIGH")
    before = copy.deepcopy(report)
    md = renderers.render_engineering_report_md(report)
    assert "- Overall risk: high" in md
    line = next(line for line in md.splitlines() if "**RISK-001**" in line)
    assert " \u00b7 High \u00b7 " in line
    assert "HIGH" not in md
    assert report == before  # the stored ids are untouched


def test_the_executive_pdf_words_severities_like_the_results_page() -> None:
    from tests.executive_pdf import body_text

    report = _report("HIGH")
    text = body_text(render_executive_report_pdf(report), "wordpress")
    assert "High Overall risk" in text
    assert "RISK-001 \u00b7 High" in text
    assert "HIGH" not in text


def test_the_printed_summary_words_severities_like_the_results_page() -> None:
    from tests.unit.report.test_printed_summary import _final
    from tests.unit.report.test_printed_summary import _report as full_report

    text = _final(full_report())
    assert "Overall risk: high" in text
    assert "- High, " in text
    assert "HIGH" not in text and "MEDIUM" not in text


def test_the_words_equal_the_results_pages_labels() -> None:
    import json
    from pathlib import Path

    en = json.loads(
        (Path(__file__).resolve().parents[3] / "src/ui/src/locales/en.json").read_text(
            encoding="utf-8"
        )
    )
    prefix = "analysis-results.risk-assessment.severity."
    for level in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "UNRATED"):
        assert severity_label(level) == en[prefix + level.lower()], level
