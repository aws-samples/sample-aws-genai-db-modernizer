"""The executive summary leads with the recommendation, the risk and the decision (#1403).

On every deterministic sample (``--llm-mode none``) the summary is the facts' three parts:
the projected cost as the facts state it, the overall risk level, "approve wave 1", no
saving with no source cost, within the word budget, and the engineering report and the
results page show the same text.
"""

from __future__ import annotations

import json

import pytest

from src.report import escaping
from src.report.engineering_report import summary_paragraphs
from src.shared.executive_brief import (
    SUMMARY_WORD_BUDGET,
    check_summary_brief,
    usd,
    whole_usd,
    word_count,
)
from tests.e2e.pipeline import PipelineResult

pytestmark = pytest.mark.deterministic

# Each sample's projected monthly cost in whole dollars ($505.02, $535.32, $435.08).
WHOLE_DOLLARS = {"wordpress": 505, "discourse": 535, "trailgear": 435}


def _report(run: PipelineResult) -> dict:
    report: dict = json.loads(run.report_json().read_text(encoding="utf-8"))
    return report


def test_the_summary_has_the_three_parts_with_the_facts_figures(run: PipelineResult) -> None:
    report = _report(run)
    facts = report["facts"]
    brief = facts["executive_brief"]
    parts = report["summary"].split("\n\n")
    assert [p.split(":", 1)[0] for p in parts] == ["Recommendation", "Risk", "Decision"]
    recommendation, risk, decision = parts
    # No cents in the summary (#1403): the whole-dollar figure the facts computed.
    whole = brief["projected_monthly_cost_whole_usd"]
    assert whole == WHOLE_DOLLARS[run.db]
    assert (
        f"The projected cost of the full modernization is {whole_usd(whole)} per month"
        in recommendation
    )
    assert usd(facts["projected_monthly_cost_usd"]) not in report["summary"]
    assert facts["price_list"]["region"] in recommendation
    assert f"overall risk is {brief['overall_risk_level'].lower()}" in risk
    assert decision.startswith(f"Decision: approve wave {brief['decision_wave']}.")
    assert word_count(report["summary"]) <= SUMMARY_WORD_BUDGET
    # Engineering detail stays out of the summary.
    for detail in ("target objects", "access pattern", "TTL", "strongly consistent"):
        assert detail not in report["summary"]


def test_no_source_cost_means_no_saving_is_claimed(run: PipelineResult) -> None:
    report = _report(run)
    brief = report["facts"]["executive_brief"]
    assert brief["source_cost_known"] is False and brief["saving_counted"] is False
    assert "No current cost was collected for the source database" in report["summary"]
    assert check_summary_brief(report["summary"], report["facts"]) == []


def test_the_engineering_report_shows_the_same_summary(run: PipelineResult) -> None:
    report = _report(run)
    paragraphs, withheld = summary_paragraphs(report)
    assert paragraphs == report["summary"].split("\n\n")
    assert withheld is None
    md = run.deliverable("engineering-report.md").read_text(encoding="utf-8")
    section = md.split("## Executive summary", 1)[1].split("\n## ", 1)[0]
    for p in paragraphs:
        assert escaping.md_text(p) in section
