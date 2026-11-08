# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A rate window that is not representative labels the costs and the risks (#1991).

Synthesis checks the window once (``facts.rate_windows``, ADR-032). When a check fails,
the engineering report, the executive PDF and the final chat summary print the facts'
own scenario label where the costs are stated and where the risks are, and the
assumptions block states the window's length, dates and check; a representative window
or a report rebuilt without the collection prints no label.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from src.contracts.synthesis_output import ReportFacts
from src.report.printed_summary import render_final_summary
from src.report.renderers import render_engineering_report_md
from src.shared.assumptions import assumption_items
from src.shared.rate_window import OBSERVED, rate_window_facts
from src.shared.report_facts import compute_report_facts
from tests.e2e.deliverable_figures import md_plain
from tests.executive_pdf import executive_text

DAY = 86_400.0
RESET = datetime(2026, 11, 5, 14, 2, 0)


def _observed(i: int, start: datetime, seconds: float) -> dict[str, Any]:
    return {
        "query_id": f"q{i}",
        "rate_window_basis": OBSERVED,
        "rate_window_seconds": seconds,
        "calls_per_second": 40.0,
        "first_seen": start.isoformat(sep=" "),
        "last_seen": (start + timedelta(seconds=seconds)).isoformat(sep=" "),
    }


STRESS = [_observed(i, RESET + timedelta(seconds=i), 28 * 60) for i in range(3)]
STEADY = [_observed(i, datetime(2026, 10, 1) + timedelta(hours=i), 20 * DAY) for i in range(3)]
UP_198_DAYS = {"uptime_seconds": 198 * DAY}


def _report(patterns: list[dict[str, Any]] | None, counters: dict | None = UP_198_DAYS) -> dict:
    report: dict[str, Any] = {
        "database_name": "shop",
        "recommended_architecture": {"architecture_type": "MULTI_DATABASE", "databases": []},
        "ranking": [{"target": "dynamodb", "workload_percent": 100.0}],
        "risk_assessment": {
            "overall_risk_level": "MEDIUM",
            "risks": [
                {
                    "risk_id": "R1",
                    "risk_type": "PERFORMANCE_DEGRADATION",
                    "severity": "MEDIUM",
                    "description": "[dynamodb] The busiest key value takes 40% of its limit.",
                    "mitigation": "Watch it.",
                }
            ],
            "resolved_risks": [],
        },
        "tco_analysis": {
            "current_monthly_cost": 0.0,
            "current_cost_known": False,
            "projected_monthly_cost": 300.0,
            "savings_percent": 0,
            "cost_breakdown": [{"database": "dynamodb", "monthly_cost_usd": 300.0}],
        },
        "trade_offs": [],
        "schema_designs": {},
        "migration_waves": None,
        "table_mappings": [],
        "query_groups": [],
        "cache_overlay": None,
    }
    windows = rate_window_facts(patterns, counters) if patterns is not None else None
    report["facts"] = compute_report_facts(
        report,
        rate_windows=windows,
        assumptions=(
            assumption_items(
                None,
                growth=5.0,
                growth_origin="default",
                price_basis=None,
                rate_window_statement=None,
                rate_windows=windows,
            )
            if windows
            else None
        ),
    )
    return report


def _label(report: dict) -> str:
    label = str(report["facts"]["rate_windows"]["scenario_label"] or "")
    assert label and label.startswith("Scenario at the measured rate:")
    return label


def _note(report: dict) -> str:
    note = str(report["facts"]["rate_windows"]["scenario_risk_note"] or "")
    assert note.startswith("The rate-driven risks are a scenario at the measured rate too")
    return note


def test_the_facts_carry_the_check_and_validate() -> None:
    facts = _report(STRESS)["facts"]
    windows = ReportFacts.model_validate(facts).rate_windows
    assert windows is not None
    assert windows.representative is False
    assert windows.flags == ["short_window", "short_against_uptime", "after_reset"]
    assert windows.window_start == "2026-11-05 14:02" and windows.window_clock == "server"


def test_the_engineering_report_labels_its_costs_and_its_risks() -> None:
    report = _report(STRESS)
    md = md_plain(render_engineering_report_md(report))
    label = _label(report)
    # The full label once, with the costs; the risks carry the short note (the concision
    # guard: no long paragraph twice).
    assert md.count(f"{label}.") == 1 and _section(md, md.index(label)) == "Cost"
    note = _note(report)
    assert md.count(f"{note}.") == 1 and _section(md, md.index(note)) == "Risk posture"
    assert "Rate window check: not representative: " in md
    assert "between 2026-11-05 14:02 and 2026-11-05 14:30 (the server's clock)" in md


def _section(md: str, at: int) -> str:
    start = md.rfind("\n## ", 0, at)
    return md[start + 4 : md.index("\n", start + 1)].strip()


def test_the_executive_report_labels_its_costs_and_its_risks() -> None:
    report = _report(STRESS)
    pdf = " ".join(executive_text(report).split())
    assert pdf.count(f"{_label(report)}.") == 1
    assert pdf.count(f"{_note(report)}.") == 1
    assert pdf.index(_label(report)) < pdf.index("Risks") < pdf.index(_note(report))
    # The window's detail stays in the engineering report and at the gate (#2045 review):
    # the label carries the short reason.
    assert "Rate window check:" not in pdf and "Rate window:" not in pdf
    assert "not representative (under a day; short against the server's uptime; counted " in pdf


def test_the_final_summary_labels_its_cost_and_its_risks() -> None:
    report = _report(STRESS)
    text = md_plain(
        render_final_summary(
            report, database_name="shop", job_id="j", deliverables_dir="d/", deliverables=[]
        )
    )
    label = _label(report)
    note = _note(report)
    assert text.count(label) == 1 and text.count(note) == 1
    glance, risks = text.index("### At a glance"), text.index("### Risks")
    assert glance < text.index(label) < risks < text.index(note)
    assert "- Rate window: " in text


def test_a_representative_window_carries_no_label() -> None:
    report = _report(STEADY, {"uptime_seconds": 40 * DAY})
    assert report["facts"]["rate_windows"]["scenario_label"] is None
    md = render_engineering_report_md(report)
    assert "Scenario at the measured rate" not in md
    assert "Scenario at the measured rate" not in executive_text(report)
    assert "Rate window check: no sign of an unrepresentative window" in md_plain(md)


# A guard of what main already does: an older report carries no label.
@pytest.mark.passes_on_main
def test_a_report_without_the_collection_carries_no_label() -> None:
    report = _report(None)
    assert "Scenario at the measured rate" not in render_engineering_report_md(report)
    text = render_final_summary(
        report, database_name="shop", job_id="j", deliverables_dir="d/", deliverables=[]
    )
    assert "Scenario at the measured rate" not in text and "Rate window:" not in text
