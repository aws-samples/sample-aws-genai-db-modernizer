# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#1990: no synthetic peak factor; growth is a stated scenario, never in a design rate.

The peak is the source's own CloudWatch peak-to-average ratio when the metrics cover 7 to
14 days, nothing for a sampled rate, and "not measured" otherwise. Growth defaults to 5×,
the customer can set it, and it reaches only the scenario lines. Each assumption states
its origin.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.shared.assumptions import (
    DEFAULT_GROWTH,
    assumption_items,
    gate_assumption_lines,
    growth_assumption,
    growth_scenario_facts,
    peak_assumption,
    query_peak_factor,
    record_growth,
    scaled_collector,
    scenario_growths,
)


def _cw(days: float, read=(100.0, 300.0), write=(10.0, 25.0)) -> dict[str, Any]:
    points = int(days * 24)
    return {
        "metrics": {
            "rds_cloudwatch_metrics": {
                "period_seconds": 3600,
                # max: one minute's spike, never the peak; max_period_avg: the busiest hour.
                "read_iops": {
                    "avg": read[0],
                    "max": read[1] * 10,
                    "max_period_avg": read[1],
                    "datapoints": points,
                },
                "write_iops": {
                    "avg": write[0],
                    "max": write[1] * 10,
                    "max_period_avg": write[1],
                    "datapoints": points,
                },
            }
        },
        "queries": {
            "query_patterns": [
                {"query_id": "q1", "calls_per_second": 2.0, "rate_window_basis": "observed"},
                {"query_id": "q2", "calls_per_second": 1.0, "rate_window_basis": "sampled"},
            ]
        },
    }


@pytest.mark.parametrize("days", [7.0, 10.0, 14.0])
def test_seven_to_fourteen_days_of_metrics_give_their_observed_ratio(days: float) -> None:
    peak = peak_assumption(_cw(days))
    assert peak["basis"] == "cloudwatch"
    assert peak["ratio"] == 3.0  # read IOPS 300 / 100, above write's 2.5
    assert peak["coverage_days"] == days
    assert peak["statement"].startswith("3× peak-to-average ratio (the busiest hour's average")


@pytest.mark.parametrize("days", [1.0, 6.9, 15.0, 30.0])
def test_metrics_outside_seven_to_fourteen_days_leave_the_peak_not_measured(days) -> None:
    peak = peak_assumption(_cw(days))
    assert (peak["basis"], peak["ratio"]) == ("not_measured", 1.0)
    assert peak["statement"].startswith("not measured (the source's metrics cover")


def test_no_metrics_leave_the_peak_not_measured() -> None:
    peak = peak_assumption({"queries": {"query_patterns": [{"query_id": "q1"}]}})
    assert (peak["basis"], peak["ratio"]) == ("not_measured", 1.0)
    assert "no CloudWatch metrics" in peak["statement"]


def test_a_metrics_file_written_before_the_coverage_fields_is_not_measured() -> None:
    old = _cw(7.0)
    del old["metrics"]["rds_cloudwatch_metrics"]["period_seconds"]
    assert peak_assumption(old)["basis"] == "not_measured"


def test_a_sampled_collection_gets_no_extra_factor() -> None:
    collector = {"queries": {"query_patterns": [{"query_id": "q", "rate_window_basis": "sampled"}]}}
    peak = peak_assumption(collector)
    assert (peak["basis"], peak["ratio"]) == ("sampled", 1.0)


def test_a_sampled_rate_never_takes_the_measured_ratio() -> None:
    collector = _cw(7.0)
    peak = peak_assumption(collector)
    observed, sampled = collector["queries"]["query_patterns"]
    assert query_peak_factor(observed, peak) == 3.0
    assert query_peak_factor(sampled, peak) == 1.0


def test_a_customer_peak_is_stated_as_the_customers() -> None:
    peak = peak_assumption(None, {"peak_to_avg_ratio": 2.0})
    assert (peak["basis"], peak["ratio"]) == ("customer", 2.0)


def test_growth_defaults_to_five_and_the_customer_can_set_it() -> None:
    assert growth_assumption(None) == (DEFAULT_GROWTH, "default") == (5.0, "default")
    assert growth_assumption({"growth_multiplier": 3}) == (3.0, "customer")
    assert growth_assumption({"growth_multiplier": 0}) == (5.0, "default")
    assert scenario_growths(5.0) == [1.0, 5.0, 10.0]
    assert scenario_growths(3.0) == [1.0, 3.0, 5.0, 10.0]


def test_every_assumption_states_its_origin() -> None:
    items = assumption_items(
        _cw(7.0),
        growth=5.0,
        growth_origin="default",
        price_basis={
            "region": "us-east-1",
            "retrieved_on": "2026-11-04",
            "source": "AWS Price List",
        },
        rate_window_statement="execution counts are divided by each statement's own window",
    )
    assert [(i["key"], i["origin"]) for i in items] == [
        ("growth", "default"),
        ("peak", "collected"),
        ("rate_window", "collected"),
        ("pricing", "default"),
    ]
    assert items[0]["line"].startswith(
        "Growth: 5× (default), shown as a separate what-if scenario."
    )
    assert items[1]["line"].startswith("Peak: 3× peak-to-average ratio")
    assert items[3]["line"] == (
        "Region and prices: us-east-1, on-demand prices from the AWS Price List retrieved "
        "2026-11-04 (default)"
    )


def test_a_scenario_reprices_the_scaled_rates_and_one_times_is_the_headline() -> None:
    collector = _cw(7.0)
    seen: list[float] = []

    def price(scaled: dict) -> float:
        rate = float(sum(q["calls_per_second"] for q in scaled["queries"]["query_patterns"]))
        seen.append(rate)
        return 100.0 + rate

    busiest = {
        "engine": "dynamodb",
        "table_name": "Options",
        "gsi_name": None,
        "operation": "read",
        "per_key_pct_of_limit": 2.0,
    }
    facts = growth_scenario_facts(
        collector,
        growth=5.0,
        origin="default",
        headline_cost=103.0,
        price=price,
        busiest_key=busiest,
    )
    assert seen == [15.0, 30.0]  # 3 calls/s at 5× and 10×; 1× is never re-priced
    assert [
        (r["growth"], r["monthly_cost_usd"], r["busiest_key_pct_of_limit"])
        for r in facts["scenarios"]
    ] == [
        (1.0, 103.0, 2.0),
        (5.0, 115.0, 10.0),
        (10.0, 130.0, 20.0),
    ]
    assert facts["lines"] == [
        "Growth scenario: 1× $103.00/mo (the collected traffic), 5× $115.00/mo (the default "
        "growth), 10× $130.00/mo; a what-if only, not part of the costs and risks in this report",
        "Busiest estimated partition key value (reads), share of its limit: 1× 2%, "
        "5× 10%, 10× 20%; that key value can take 50× its collected traffic before it reaches its "
        "limit",
    ]
    # The input is never changed.
    assert collector["queries"]["query_patterns"][0]["calls_per_second"] == 2.0


def test_scaling_multiplies_both_rate_fields() -> None:
    scaled = scaled_collector(
        {"queries": {"query_patterns": [{"calls_per_second": 2.0, "frequency_per_hour": 7200}]}},
        5.0,
    )
    assert scaled["queries"]["query_patterns"][0] == {
        "calls_per_second": 10.0,
        "frequency_per_hour": 36000.0,
    }


def test_the_gate_states_the_customers_growth(tmp_path) -> None:
    from src.storage.local_store import LocalArtifactStore

    store = LocalArtifactStore(base_dir=str(tmp_path))
    record = record_growth(store, "shop", "job-1", 3, via="cli")
    assert store.read_json("shop/job-1/decisions/assumptions.json") == record
    lines = gate_assumption_lines(_cw(3.0), record)
    assert lines[0].startswith("Growth: 3× (set in the run's options)")  # #1991: a flag
    assert lines[1].startswith("Peak: not measured (the source's metrics cover 3 days")
    with pytest.raises(ValueError):
        record_growth(store, "shop", "job-1", -1, via="cli")


def test_a_seven_day_window_missing_an_hour_still_covers_seven_days() -> None:
    """Review S4: the 7 to 14 day bounds are inclusive, and a window whose periods are
    nearly all there covers the days it asked for."""
    collector = _cw(7.0)
    cw = collector["metrics"]["rds_cloudwatch_metrics"]
    cw["window_days"] = 7
    for key in ("read_iops", "write_iops"):
        cw[key]["datapoints"] = 167  # one hour of 168 missing
    peak = peak_assumption(collector)
    assert (peak["basis"], peak["coverage_days"]) == ("cloudwatch", 7.0)
    del cw["window_days"]
    assert peak_assumption(collector)["basis"] == "not_measured"  # 6.96 days, no window


def test_one_minutes_maximum_is_never_the_peak() -> None:
    """Review S4: a backup's one-minute Maximum is no workload peak; with only it, the
    peak is not measured."""
    collector = _cw(7.0)
    for key in ("read_iops", "write_iops"):
        del collector["metrics"]["rds_cloudwatch_metrics"][key]["max_period_avg"]
    assert peak_assumption(collector)["basis"] == "not_measured"


def test_the_growth_lines_name_no_table() -> None:
    """The executive report prints the growth scenario lines and must name no collected
    table (#2025 e2e-llm: "wp_wc_tax_rate_classes" reached the PDF through this line)."""
    busiest = {
        "engine": "dynamodb",
        "table_name": "wp_wc_tax_rate_classes",
        "gsi_name": "ByScope",
        "operation": "read",
        "per_key_pct_of_limit": 4.6,
    }
    facts = growth_scenario_facts(
        {"queries": {"query_patterns": []}},
        growth=5.0,
        origin="default",
        headline_cost=10.0,
        price=lambda _scaled: 10.0,
        busiest_key=busiest,
    )
    text = " ".join(facts["lines"])
    assert "wp_wc_tax_rate_classes" not in text and "ByScope" not in text
    assert facts["busiest_key"]["table_name"] == "wp_wc_tax_rate_classes"  # kept as data


#: Internal terms a customer does not know (#2103): a guard phrased as a negative, and the
#: model's own names for the rates and the cost the reader sees.
INTERNAL_PHRASES = ("design rate", "design-rate", "headline cost", "never applied")


@pytest.mark.parametrize("origin", ["default", "flag", "file", "ui", "customer"])
def test_the_customer_facing_assumption_lines_use_no_internal_wording(origin: Any) -> None:
    """The assumptions block and the growth scenario read in the customer's words: growth
    is a what-if scenario and the costs and risks use the collected traffic."""
    windows: dict[str, Any] = {
        "statement": "execution counts are divided by each statement's own window",
        "window_statement": "27.5 minutes; a short window overstates traffic",
        "check_statement": "not representative: under a day",
        "representative": False,
        "flags": ["short_window"],
        "typical_window_seconds": 1650.0,
    }
    items = assumption_items(
        _cw(7.0),
        growth=3.0,
        growth_origin=origin,
        price_basis={
            "region": "us-east-1",
            "retrieved_on": "2026-11-04",
            "source": "AWS Price List",
        },
        rate_window_statement=windows["statement"],
        rate_windows=windows,
    )
    scenario = growth_scenario_facts(
        _cw(7.0),
        growth=3.0,
        origin=origin,
        headline_cost=10.0,
        price=lambda _scaled: 12.0,
        busiest_key={
            "engine": "dynamodb",
            "table_name": "t",
            "gsi_name": None,
            "operation": "read",
            "per_key_pct_of_limit": 2.0,
        },
    )
    lines = [i["line"] for i in items] + scenario["lines"] + gate_assumption_lines(_cw(7.0), None)
    assert len(lines) >= 7
    for line in lines:
        for phrase in INTERNAL_PHRASES:
            assert phrase not in line.lower(), (phrase, line)
    assert items[0]["line"].endswith(
        "shown as a separate what-if scenario. The costs and risks in this report use the "
        "collected traffic"
    )
