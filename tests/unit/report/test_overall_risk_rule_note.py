"""The decision report states how the overall risk level is derived (#248)."""

from __future__ import annotations

from src.report import renderers

_NOTE = (
    "Overall risk is the highest severity among the open risks; risks the assignment "
    "resolved do not count."
)


def _report(risks: list[dict]) -> dict:
    return {
        "database_name": "wordpress",
        "risk_assessment": {
            "overall_risk_level": "MEDIUM" if risks else "LOW",
            "risks": risks,
            "mitigation_strategies": [],
        },
    }


def test_rule_is_shown_next_to_the_level() -> None:
    risk = {
        "risk_id": "RISK-001",
        "risk_type": "MIGRATION_COMPLEXITY",
        "severity": "MEDIUM",
        "description": "[dynamodb] aggregation: DynamoDB has no native equivalent.",
    }
    html = renderers.render_decision_report_html(_report([risk]))
    assert "Overall risk <b>MEDIUM</b>." in html
    assert _NOTE in html


def test_no_risk_posture_section_without_risks() -> None:
    assert _NOTE not in renderers.render_decision_report_html(_report([]))
