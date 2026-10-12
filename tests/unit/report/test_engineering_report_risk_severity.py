"""The engineering report's Risk register keeps CRITICAL and LOW intact (#270).

#270 found the decision report's risk posture line folding CRITICAL into
"high" and dropping LOW entirely. The engineering report never summarised
severities into a count sentence -- each risk already prints its own
``severity`` field verbatim -- so this is a regression test pinning that a
CRITICAL risk is never relabelled HIGH and a LOW risk is never dropped from
the register (or its header count).
"""

from __future__ import annotations

from src.report import renderers


def _risk(risk_id: str, severity: str) -> dict:
    return {
        "risk_id": risk_id,
        "risk_type": "MIGRATION_COMPLEXITY",
        "severity": severity,
        "description": f"[dynamodb] {risk_id} needs a mitigation",
    }


def _report(risks: list[dict]) -> dict:
    return {
        "database_name": "wordpress",
        "risk_assessment": {
            "overall_risk_level": "CRITICAL" if risks else "LOW",
            "risks": risks,
            "mitigation_strategies": [],
        },
    }


def test_a_critical_risk_prints_as_critical_not_high() -> None:
    md = renderers.render_engineering_report_md(_report([_risk("RISK-001", "CRITICAL")]))
    assert "RISK-001" in md
    assert "CRITICAL" in md
    # The bullet carries the risk's own severity, never relabelled HIGH.
    line = next(line for line in md.splitlines() if "RISK-001" in line)
    assert "CRITICAL" in line
    assert "HIGH" not in line


def test_a_mix_of_medium_and_low_risks_are_both_kept_and_counted() -> None:
    risks = [_risk("RISK-001", "MEDIUM"), _risk("RISK-002", "LOW"), _risk("RISK-003", "LOW")]
    md = renderers.render_engineering_report_md(_report(risks))
    assert "## Risk register (3)" in md
    for rid in ("RISK-001", "RISK-002", "RISK-003"):
        assert rid in md
    medium_line = next(line for line in md.splitlines() if "RISK-001" in line)
    assert "MEDIUM" in medium_line
    low_lines = [line for line in md.splitlines() if "RISK-002" in line or "RISK-003" in line]
    assert all("LOW" in line for line in low_lines)
