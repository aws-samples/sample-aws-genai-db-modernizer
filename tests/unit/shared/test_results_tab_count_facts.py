# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The counts on the results page's tabs are report facts (#2363, ADR-032).

The tabs say "Queries (107)" and "Risks and decisions (9)". The page only formats the
figure: synthesis counts each once into ``facts.results_tab_counts``, and a report
written before it has none, so its tabs show the plain label.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.contracts.synthesis_output import ReportFacts
from src.report.facts import report_facts
from src.shared.report_facts import compute_report_facts

FIXTURES = Path(__file__).resolve().parents[1] / "report" / "fixtures"


def _report(name: str = "wordpress_report") -> dict[str, Any]:
    report: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text())
    report.pop("facts", None)
    return report


def test_the_queries_tab_counts_every_query_the_headline_does() -> None:
    facts = compute_report_facts(_report())
    assert facts["results_tab_counts"]["queries"] == facts["query_total_count"] == 73


def test_the_risks_tab_counts_the_open_risks_and_the_trade_offs() -> None:
    facts = compute_report_facts(_report())
    assert (
        facts["results_tab_counts"]["risks"] == facts["open_risk_count"] + facts["trade_off_count"]
    )
    assert facts["results_tab_counts"]["risks"] == 55


def test_the_plan_tab_counts_the_waves() -> None:
    facts = compute_report_facts(_report("wordpress_no_schema_design_report"))
    assert facts["results_tab_counts"]["plan"] == facts["migration_wave_count"] == 3


def test_the_data_model_tab_has_no_count() -> None:
    # It opens on the source tables, so a designed-table count would read as a mismatch.
    assert "data_model" not in compute_report_facts(_report())["results_tab_counts"]


def test_the_contract_accepts_the_counts_and_an_old_block_has_none() -> None:
    facts = compute_report_facts(_report())
    kept = ReportFacts.model_validate(facts).model_dump()
    assert kept["results_tab_counts"] == facts["results_tab_counts"]
    old = _report()
    stored = compute_report_facts(old)
    stored.pop("results_tab_counts")
    old["facts"] = stored
    assert report_facts(old).get("results_tab_counts") is None


def test_a_tab_with_nothing_to_count_has_no_count() -> None:
    report = _report("wordpress_no_schema_design_report")
    report["migration_waves"] = []
    counts = compute_report_facts(report)["results_tab_counts"]
    assert "plan" not in counts


def test_risks_added_after_the_facts_were_written_move_the_risks_count() -> None:
    from src.shared.report_facts import refresh_risk_facts

    report = _report()
    stored = compute_report_facts(report)
    before = stored["results_tab_counts"]["risks"]
    report["risk_assessment"]["risks"] = [
        *report["risk_assessment"].get("risks", []),
        {
            "risk_id": "RISK-X",
            "risk_type": "other",
            "severity": "LOW",
            "description": "A new open risk.",
        },
    ]
    refreshed = refresh_risk_facts(stored, report)
    assert refreshed["results_tab_counts"] == compute_report_facts(report)["results_tab_counts"]
    assert refreshed["results_tab_counts"]["risks"] == before + 1


def test_refreshing_to_no_open_risks_and_no_trade_offs_drops_the_risks_count() -> None:
    from src.shared.report_facts import refresh_risk_facts

    report = _report()
    stored = compute_report_facts(report)
    assert "risks" in stored["results_tab_counts"]
    stored["trade_off_count"] = 0
    report["risk_assessment"]["risks"] = []
    refreshed = refresh_risk_facts(stored, report)
    assert "risks" not in refreshed["results_tab_counts"]
    assert refreshed["results_tab_counts"]["queries"] == stored["results_tab_counts"]["queries"]
