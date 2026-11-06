# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

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

# Each sample's projected monthly cost in whole dollars ($512.58, $535.27, $433.15). #1476
# re-priced DynamoDB's request units by item size and read kind (was $505.02, $535.32,
# $435.08): its row moves +$7.56 on wordpress, +$1.74 on discourse, -$1.93 on trailgear.
# #1197 then bills DocumentDB reads from the collection-wide buffer-cache hit ratio the
# collector now keeps (scaled by target over source memory, at least 1% billed as misses):
# only discourse moves, by -$1.79 ($537.06 to $535.27); wordpress and trailgear are
# unchanged. #1893 divides each statement's execution count by the window it covers
# instead of 24 hours: wordpress $512.58 to $749.21 (Aurora MySQL's sized load, 96.5 to
# 118.3 calls/s, takes the next instance size) and trailgear $433.15 to $432.19.
# discourse is unchanged: a PostgreSQL collection with no stats reset time assumes 24 hours.
# #1994: wordpress $749.21 to $757.62. wp_postmeta's meta_id read (23.8 calls/s) moves from
# DynamoDB to Aurora MySQL once simple-cache-read fires only above Aurora's documented read
# capacity: Aurora MySQL $484.94 to $497.25, DynamoDB $79.68 to $75.78.
# #1955 refreshes the wordpress sample (163 statements under a seeded workload, rates over
# its ~27-minute window): $757.62 to $1,178.33. DynamoDB $75.78 to $496.49 (its sized load
# rises to 923.7 calls/s with 191.0 writes/s, on-demand), Aurora MySQL unchanged at $497.25
# (108 sized queries, 647.7 calls/s), ElastiCache unchanged at $184.59.
WHOLE_DOLLARS = {"wordpress": 1178, "discourse": 535, "trailgear": 432}


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
    # #1992: the first suggested wave; with none (no wave designed on a run without a
    # model), the Decision says so and approves none.
    if brief["decision_wave"] is None:
        assert decision.startswith("Decision: no wave is suggested yet.")
    else:
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
