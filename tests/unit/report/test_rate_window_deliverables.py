# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every deliverable states, once, what the call rates rest on (#1893).

Synthesis counts each collected statement's rate window basis once
(``facts.rate_windows``, ADR-032); the engineering report and the executive PDF print
its one sentence as it is, whichever basis drove the figures, and nothing for a report
rebuilt without the collection.
"""

from __future__ import annotations

from typing import Any

from src.contracts.synthesis_output import ReportFacts
from src.report.renderers import render_engineering_report_md
from src.shared.rate_window import rate_window_facts
from src.shared.report_facts import compute_report_facts
from tests.e2e.deliverable_figures import md_plain
from tests.executive_pdf import executive_text

STATEMENT = (
    "execution counts are divided by each statement's own observed window (1 statement), "
    "the server uptime (0), or an assumed 24 hours (2); costs assume that rate runs "
    "continuously"
)
OBSERVED_ONLY = (
    "execution counts are divided by each statement's own observed window (2 statements), "
    "the server uptime (1), or an assumed 24 hours (0); costs assume that rate runs "
    "continuously"
)


def _patterns(*bases: str | None) -> list[dict[str, Any]]:
    return [{"query_id": f"q{i}", "rate_window_basis": b} for i, b in enumerate(bases)]


def _report(patterns: list[dict[str, Any]] | None) -> dict[str, Any]:
    report: dict[str, Any] = {
        "database_name": "shop",
        "recommended_architecture": {"architecture_type": "MULTI_DATABASE", "databases": []},
        "ranking": [{"target": "aurora_postgresql", "workload_percent": 100.0}],
        "risk_assessment": {"overall_risk_level": "LOW", "risks": [], "resolved_risks": []},
        "tco_analysis": {
            "current_monthly_cost": 0.0,
            "current_cost_known": False,
            "projected_monthly_cost": 300.0,
            "savings_percent": 0,
            "cost_breakdown": [{"database": "aurora_postgresql", "monthly_cost_usd": 300.0}],
        },
        "trade_offs": [],
        "schema_designs": {},
        "migration_waves": None,
        "table_mappings": [],
        "query_groups": [],
        "cache_overlay": None,
    }
    report["facts"] = compute_report_facts(
        report, rate_windows=rate_window_facts(patterns) if patterns is not None else None
    )
    return report


def test_the_facts_carry_the_bases_once_and_validate() -> None:
    facts = _report(_patterns("assumed_24h", "observed", "assumed_24h"))["facts"]
    windows = facts["rate_windows"]
    assert windows["statement"] == STATEMENT
    assert (windows["observed_count"], windows["uptime_count"]) == (1, 0)
    assert windows["assumed_24h_count"] == 2
    assert ReportFacts.model_validate(facts).rate_windows.assumed_24h_count == 2


def test_the_engineering_report_states_it_once() -> None:
    md = md_plain(
        render_engineering_report_md(_report(_patterns("assumed_24h", "observed", "assumed_24h")))
    )
    assert md.count(f"Call rates: {STATEMENT}.") == 1


def test_the_executive_report_states_it_once() -> None:
    pdf = " ".join(
        executive_text(_report(_patterns("assumed_24h", "observed", "assumed_24h"))).split()
    )
    assert pdf.count(f"Call rates: {STATEMENT}.") == 1


def test_it_is_said_whichever_basis_drove_the_figures() -> None:
    """The review on #1901: a rate over a statement's observed window is priced as if it
    ran around the clock, and the deliverables say so even when no rate assumed 24 hours."""
    report = _report(_patterns("observed", "uptime", "observed"))
    assert report["facts"]["rate_windows"]["statement"] == OBSERVED_ONLY
    md = md_plain(render_engineering_report_md(report))
    assert md.count(f"Call rates: {OBSERVED_ONLY}.") == 1
    pdf = " ".join(executive_text(report).split())
    assert pdf.count(f"Call rates: {OBSERVED_ONLY}.") == 1


def test_a_report_without_the_collection_says_nothing() -> None:
    report = _report(None)
    assert report["facts"]["rate_windows"] is None
    assert "Call rates:" not in render_engineering_report_md(report)
    assert ReportFacts.model_validate(report["facts"]).rate_windows is None
