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
    # #1991: --growth is a flag, and the gate and the deliverables say so.
    assert (scenario["growth"], scenario["origin"]) == (3.0, "flag")
    assert [r["growth"] for r in scenario["scenarios"]] == [1.0, 3.0, 5.0, 10.0]
    assert after["facts"]["assumptions"]["lines"][0].startswith(
        "Growth: 3× (set in the run's options)"
    )
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


def test_the_assumptions_files_values_are_stated_with_origin_file(store) -> None:
    """#1991: the file's growth and peak reach the facts once, each stated as the file's."""
    from src.contracts.synthesis_output import SynthesisOutputContract
    from src.shared.assumptions import record_assumptions

    before = _report(store)
    record_assumptions(
        store, DB, JOB, {"growth_multiplier": 3.0, "peak_to_avg_ratio": 2.0}, via="file"
    )
    after = _report(store)
    facts = after["facts"]
    items = {a["key"]: a for a in facts["assumptions"]["assumptions"]}
    assert (items["growth"]["origin"], items["growth"]["setting"]) == ("file", 3.0)
    assert (items["peak"]["origin"], items["peak"]["basis"]) == ("file", "customer")
    assert items["peak"]["setting"] == 2.0
    assert facts["assumptions"]["lines"][0].startswith("Growth: 3× (set in the assumptions file)")
    assert facts["assumptions"]["lines"][1] == (
        "Peak: 2× peak-to-average ratio (set in the assumptions file)"
    )
    assert "(the growth set in the assumptions file)" in facts["growth_scenarios"]["lines"][0]
    assert facts["growth_scenarios"]["origin"] == "file"
    assert facts["projected_monthly_cost_usd"] == before["facts"]["projected_monthly_cost_usd"]
    SynthesisOutputContract.model_validate(after)


def test_a_report_written_before_1991_still_loads() -> None:
    """A report main wrote at 1.85 with --growth 3 (origin customer) still loads, saved
    as main wrote it (fixtures/report_1_85_customer_growth.json)."""
    import json
    from pathlib import Path

    from src.contracts.synthesis_output import (
        AssumptionItem,
        GrowthScenarioFacts,
        SynthesisOutputContract,
    )
    from src.shared.contract_versioning import load_contract

    path = Path(__file__).parent / "fixtures" / "report_1_85_customer_growth.json"
    saved = json.loads(path.read_text())
    assert saved["contract_version"] == "1.85"
    report = load_contract(SynthesisOutputContract, saved, artifact=str(path))
    facts = report.facts
    assert facts is not None and facts.assumptions is not None
    assert facts.assumptions.assumptions[0].origin == "customer"
    assert facts.growth_scenarios is not None and facts.growth_scenarios.origin == "customer"
    for origin in ("flag", "file"):
        assert AssumptionItem(key="growth", origin=origin, line="x").origin == origin
        assert GrowthScenarioFacts(growth=3.0, origin=origin).origin == origin


def test_a_decision_written_before_1991_re_synthesizes_as_a_flag(store) -> None:
    """main's decisions/assumptions.json from --growth (origin customer, via cli, saved in
    fixtures/) re-synthesizes as the run's options' growth."""
    import json
    from pathlib import Path

    saved = Path(__file__).parent / "fixtures" / "assumptions_decision_1_85_growth_flag.json"
    store.write_json(f"{DB}/{JOB}/decisions/assumptions.json", json.loads(saved.read_text()))
    facts = _report(store)["facts"]
    assert facts["assumptions"]["assumptions"][0]["origin"] == "flag"
    assert facts["assumptions"]["lines"][0].startswith("Growth: 3× (set in the run's options)")


def test_a_saved_growth_out_of_range_is_stated_in_the_deliverables(store) -> None:
    """#1991 review: a job main recorded with --growth 150 re-synthesizes at the default,
    and the deliverables' line says the saved value is not used."""
    store.write_json(
        f"{DB}/{JOB}/decisions/assumptions.json",
        {"growth_multiplier": 150.0, "origin": "customer", "via": "cli"},
    )
    facts = _report(store)["facts"]
    assert facts["growth_scenarios"]["growth"] == 5.0
    line = facts["assumptions"]["lines"][0]
    assert line.startswith("Growth: 5× (default; the saved growth 150× is out of range (")
