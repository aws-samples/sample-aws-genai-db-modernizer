"""The PDF report states how the overall risk level is derived (#248)."""

from __future__ import annotations

from tests.executive_pdf import engineering_text

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
    text = engineering_text(_report([risk]))
    assert "Overall risk: MEDIUM" in text
    assert _NOTE in text


def test_no_rule_note_without_open_risks() -> None:
    assert _NOTE not in engineering_text(_report([]))
