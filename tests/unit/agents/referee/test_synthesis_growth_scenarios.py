# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#1990: synthesis states the assumptions block and the growth scenario once; growth
never moves the headline cost or a risk."""

from __future__ import annotations

from tests.unit.agents.referee.test_synthesis_hot_partitions import DB, JOB, _report, store

__all__ = ["store"]  # the fixture, shared with the hot-partition tests


def test_the_facts_state_every_assumption_with_its_origin(store) -> None:
    facts = _report(store)["facts"]
    origins = {a["key"]: a["origin"] for a in facts["assumptions"]["assumptions"]}
    assert origins == {
        "growth": "default",
        "peak": "default",
        "rate_window": "collected",
        # #1991: whether the rate window is representative.
        "rate_window_check": "collected",
    }  # no TCO row here is priced from the price table, so no region or price date
    assert facts["assumptions"]["lines"][0].startswith("Growth: 5× (default)")
    assert facts["assumptions"]["lines"][1].startswith("Peak: not measured")


def test_one_times_growth_is_the_headline_and_the_rows_are_1_5_10(store) -> None:
    report = _report(store)
    scenario = report["facts"]["growth_scenarios"]
    assert [r["growth"] for r in scenario["scenarios"]] == [1.0, 5.0, 10.0]
    assert (
        scenario["scenarios"][0]["monthly_cost_usd"]
        == report["facts"]["projected_monthly_cost_usd"]
    )
    # The busiest key value of the design is measured (55,527 option names), scaled by growth.
    busiest = [r["busiest_key_pct_of_limit"] for r in scenario["scenarios"]]
    assert busiest[1] == round(busiest[0] * 5, 4) and busiest[2] == round(busiest[0] * 10, 4)
    assert scenario["lines"][0].startswith("Growth scenario: 1× ")


def test_a_customer_growth_reaches_only_the_scenario(store) -> None:
    from src.shared.assumptions import record_growth

    before = _report(store)
    record_growth(store, DB, JOB, 3, via="cli")
    after = _report(store)
    scenario = after["facts"]["growth_scenarios"]
    assert (scenario["growth"], scenario["origin"]) == (3.0, "customer")
    assert [r["growth"] for r in scenario["scenarios"]] == [1.0, 3.0, 5.0, 10.0]
    assert after["facts"]["assumptions"]["lines"][0].startswith("Growth: 3× (customer)")
    for figure in ("projected_monthly_cost_usd", "open_risks_by_severity"):
        assert after["facts"][figure] == before["facts"][figure], figure
    assert after["risk_assessment"]["risks"] == before["risk_assessment"]["risks"]


def test_the_executive_report_names_no_design_table_in_the_growth_scenario(store) -> None:
    """#2025 e2e-llm: the growth scenario's busiest-key line reached the executive PDF
    with its table's name; the line names none, and the PDF prints it without one."""
    from src.report import executive_report
    from tests.executive_pdf import body_text

    report = _report(store)
    lines = report["facts"]["growth_scenarios"]["lines"]
    assert lines[1].startswith("Busiest estimated partition key value (reads), share")
    pdf = " ".join(body_text(executive_report.render_executive_report_pdf(report), DB).split())
    assert "Busiest estimated partition key value (reads)" in pdf
    assert "Busiest estimated partition key value (Options" not in pdf
