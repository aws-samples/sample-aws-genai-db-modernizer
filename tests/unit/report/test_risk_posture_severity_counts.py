"""The decision report's risk posture line counts every severity (#270).

With the overall-risk rule from #248 (the highest open severity),
``overall_risk_level`` can be CRITICAL, and the open risk register can hold
LOW risks. The posture line used to fold CRITICAL into "high" and never
counted LOW at all, so its parenthetical count did not add up to the
open-risk total whenever either was present.
"""

from __future__ import annotations

import re

from src.report import renderers
from src.report.risks import _severity_counts_text
from src.shared.report_facts import risk_facts


def _risk(risk_id: str, severity: str) -> dict:
    return {
        "risk_id": risk_id,
        "risk_type": "MIGRATION_COMPLEXITY",
        "severity": severity,
        "description": f"[dynamodb] {risk_id} needs a mitigation",
    }


def _report(risks: list[dict], overall: str) -> dict:
    return {
        "database_name": "wordpress",
        "risk_assessment": {
            "overall_risk_level": overall,
            "risks": risks,
            "mitigation_strategies": [],
        },
    }


def _posture_paren(html: str) -> str:
    """The "(...)" clause right after the "N open migration risk(s)" sentence."""
    m = re.search(r"open migration risks? \(([^)]*)\)", html)
    assert m, html
    return m.group(1)


def test_a_single_critical_risk_is_counted_as_critical_not_folded_into_high() -> None:
    report = _report([_risk("RISK-001", "CRITICAL")], "CRITICAL")
    html = renderers.render_decision_report_html(report)
    assert "Overall risk <b>CRITICAL</b>" in html
    paren = _posture_paren(html)
    assert paren == "1 critical"
    assert "high" not in paren


def test_a_mix_of_medium_and_low_risks_counts_both_and_sums_to_the_total() -> None:
    risks = [_risk("RISK-001", "MEDIUM"), _risk("RISK-002", "MEDIUM"), _risk("RISK-003", "LOW")]
    report = _report(risks, "MEDIUM")
    html = renderers.render_decision_report_html(report)
    paren = _posture_paren(html)
    assert paren == "2 medium, 1 low"
    counted = sum(int(n) for n in re.findall(r"(\d+) (?:critical|high|medium|low)", paren))
    assert counted == len(risks)


def test_all_four_severities_are_shown_in_critical_high_medium_low_order() -> None:
    risks = [
        _risk("RISK-001", "LOW"),
        _risk("RISK-002", "CRITICAL"),
        _risk("RISK-003", "MEDIUM"),
        _risk("RISK-004", "HIGH"),
    ]
    report = _report(risks, "CRITICAL")
    html = renderers.render_decision_report_html(report)
    paren = _posture_paren(html)
    assert paren == "1 critical, 1 high, 1 medium, 1 low"


def test_a_severity_at_zero_count_is_not_mentioned() -> None:
    report = _report([_risk("RISK-001", "CRITICAL"), _risk("RISK-002", "CRITICAL")], "CRITICAL")
    html = renderers.render_decision_report_html(report)
    paren = _posture_paren(html)
    assert paren == "2 critical"
    for term in ("high", "medium", "low"):
        assert term not in paren


# -----------------------------------------------------------------------------
# _severity_counts_text directly (#270 round-1 review): lowercase severities
# must still count (the contract never requires upper case), and a severity
# outside CRITICAL/HIGH/MEDIUM/LOW must be named "unrated" rather than
# silently folded into LOW -- matching the deck's UNRATED handling
# (``pptx_report.py``).


def _counted(risks: list[dict]) -> dict[str, int]:
    """The facts layer's per-severity counts (#615), which the posture line formats."""
    counts: dict[str, int] = risk_facts({"risk_assessment": {"risks": risks}})[
        "open_risks_by_severity"
    ]
    return counts


def test_lowercase_severities_are_matched_case_insensitively() -> None:
    risks = [_risk("RISK-001", "critical"), _risk("RISK-002", "low")]
    assert _severity_counts_text(_counted(risks)) == "1 critical, 1 low"


def test_an_unknown_severity_is_named_unrated_not_folded_into_low() -> None:
    risks = [_risk("RISK-001", "MEDIUM"), _risk("RISK-002", "SOMETHING_ELSE")]
    text = _severity_counts_text(_counted(risks))
    assert text == "1 medium, 1 unrated"
    assert "low" not in text


def test_only_unknown_severities_still_sums_to_the_total() -> None:
    risks = [_risk("RISK-001", ""), _risk("RISK-002", "NOT_A_SEVERITY")]
    text = _severity_counts_text(_counted(risks))
    assert text == "2 unrated"


def test_an_unrated_risk_is_named_in_the_decision_report_posture_line() -> None:
    report = _report([_risk("RISK-001", "MEDIUM"), _risk("RISK-002", "SOMETHING_ELSE")], "MEDIUM")
    html = renderers.render_decision_report_html(report)
    paren = _posture_paren(html)
    assert paren == "1 medium, 1 unrated"
