"""Risks the assignment resolved are visible in the deliverables (#221).

``risk_assessment.resolved_risks`` records analysis risks whose queries the effective
assignment moved to an engine that serves them. The engineering report lists them in a
"Resolved by the assignment" section and the decision report's risk line counts them,
so a HIGH risk never vanishes from a deliverable without a trace.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from src.report import renderers

FIXTURE = Path(__file__).parent / "fixtures" / "wordpress_report.json"

RESOLVED = [
    {
        "engine": "aurora_mysql",
        "severity": "HIGH",
        "description": "[aurora_mysql] Single-row SELECT by primary key at very high frequency.",
        "affected_tables": ["wordpress.wp_options"],
        "query_ids": ["q1"],
        "resolved_on": "dynamodb",
        "reason": "the Aurora MySQL analysis recommended DynamoDB for these queries and the "
        "assignment moved them there",
    },
    {
        "engine": "aurora_mysql",
        "severity": "MEDIUM",
        "description": "[aurora_mysql] Table accessed by at most 2 query patterns.",
        "affected_tables": [],
        "query_ids": ["q2"],
        "resolved_on": "dynamodb",
        "reason": "the queries moved to DynamoDB, whose schema design serves all of them",
    },
]


@pytest.fixture
def report() -> dict:
    data: dict = json.loads(FIXTURE.read_text())
    data = copy.deepcopy(data)
    data["risk_assessment"]["resolved_risks"] = RESOLVED
    return data


def test_engineering_report_lists_resolved_risks(report: dict) -> None:
    md = renderers.render_engineering_report_md(report)
    assert "## Resolved by the assignment (2)" in md
    section = md.split("## Resolved by the assignment (2)", 1)[1]
    assert "HIGH · aurora\\_mysql → dynamodb — Single-row SELECT" in section
    assert (
        "Resolved because the Aurora MySQL analysis recommended DynamoDB for these queries "
        "and the assignment moved them there."
    ) in section


def test_decision_report_counts_resolved_risks(report: dict) -> None:
    html = renderers.render_decision_report_html(report)
    assert "; 2 resolved by the assignment)" in html


def test_no_resolved_risks_no_section(report: dict) -> None:
    report["risk_assessment"].pop("resolved_risks")
    assert "Resolved by the assignment" not in renderers.render_engineering_report_md(report)
    assert "resolved by the assignment" not in renderers.render_decision_report_html(report)
