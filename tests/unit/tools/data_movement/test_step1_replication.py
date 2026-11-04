# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The step 1 plan's replication section (#1798)."""

from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import json
from pathlib import Path

from src.shared.catalog.dms_sizing import dms_sizing
from src.shared.catalog.prices import HOURS_PER_MONTH, PriceTable
from src.tools.data_movement.dms_sizing import size_from_collector
from src.tools.data_movement.plan import JobInputs, build_step_plans

from .test_plan import _job, _waves


def _prices(**dms: object) -> PriceTable:
    return PriceTable(
        region="us-east-1",
        retrieved_on=dt.date(2026, 11, 1),
        published={"AWSDatabaseMigrationSvc": dt.date(2026, 11, 1)},
        sections={"dms": {"replication": dms}},
    )  # fmt: skip


def _versioned(inputs: JobInputs, version: str) -> JobInputs:
    collector = copy.deepcopy(dict(inputs.collector))
    collector["metadata"]["source_database"]["version"] = version
    return dataclasses.replace(inputs, collector=collector)


def test_the_step1_plan_carries_its_sizing_lob_and_line() -> None:
    inputs = _versioned(_job(), "8.0.36")
    steps, _hub = build_step_plans(inputs, prices=None)
    rep = steps[0].replication
    assert rep is not None and rep.multi_az_default is True
    assert rep.sizing == size_from_collector(inputs.collector)
    assert rep.target_engine == "aurora_mysql" and rep.lob_max_size_kb >= 1
    # aurora_versions.toml: MySQL 8.0 maps to the 8.0 line.
    assert rep.target_engine_line == "8.0"
    assert rep.unbounded_varchar.max_bytes is None  # a MySQL source needs no such setting
    assert all(p.replication is None for p in steps[1:])


def test_a_wave_1_with_no_aurora_engine_has_no_target() -> None:
    inputs = _job(waves=_waves(["dynamodb"]))
    rep = build_step_plans(inputs, prices=None)[0][0].replication
    assert rep is not None and rep.target_engine is None and rep.target_engine_line is None


def test_step1_cost_prices_the_sized_class_multi_az() -> None:
    inputs = _job(waves=[_waves()[0]])
    s = size_from_collector(inputs.collector)
    billed = max(0, s.allocated_storage_gb - s.included_storage_gb)
    prices = _prices(
        instance_hourly={s.instance_class: 0.1},
        multi_az_instance_hourly={s.instance_class: 0.2},
        storage_price_per_gb_month=0.115,
        multi_az_storage_price_per_gb_month=0.23,
    )  # fmt: skip
    plan = build_step_plans(inputs, prices=prices)[0][0]
    assert plan.monthly_cost_usd == round(0.2 * HOURS_PER_MONTH + 0.23 * billed, 2)
    single = 0.1 * HOURS_PER_MONTH + 0.115 * billed
    assert any("MultiAZ=false" in a and f"{single:.2f}" in a for a in plan.cost_assumptions)
    assert any(s.instance_class in a and "Multi-AZ" in a for a in plan.cost_assumptions)
    assert any(s.reason in a for a in plan.cost_assumptions)


def test_an_unpriced_class_leaves_the_cost_null_and_names_it() -> None:
    inputs = _job(waves=[_waves()[0]])
    s = size_from_collector(inputs.collector)
    prices = _prices(
        multi_az_instance_hourly={"dms.other.class": 1.0}, multi_az_storage_price_per_gb_month=0.1
    )  # fmt: skip
    plan = build_step_plans(inputs, prices=prices)[0][0]
    assert plan.monthly_cost_usd is None
    assert any(u.kind == "cost" and s.instance_class in u.reason for u in plan.unsupported)


def test_an_unpriced_multi_az_class_still_states_the_single_az_figure() -> None:
    inputs = _job(waves=[_waves()[0]])
    s = size_from_collector(inputs.collector)
    billed = max(0, s.allocated_storage_gb - s.included_storage_gb)
    prices = _prices(
        instance_hourly={s.instance_class: 0.1}, storage_price_per_gb_month=0.115
    )  # fmt: skip
    plan = build_step_plans(inputs, prices=prices)[0][0]
    assert plan.monthly_cost_usd is None
    single = 0.1 * HOURS_PER_MONTH + 0.115 * billed
    assert any("MultiAZ=false" in a and f"{single:.2f}" in a for a in plan.cost_assumptions)
    assert {u.item for u in plan.unsupported if u.kind == "cost"} == {"dms", "dms_storage"}


def _with_lob_view(inputs: JobInputs) -> JobInputs:
    """A job whose wave 1 lists a view next to base tables, one with a LOB column."""
    collector = copy.deepcopy(dict(inputs.collector))
    for t in collector["database_schema"]["tables"]:
        if t["table_id"] == "shop.settings":
            t["columns"].append(
                {
                    "column_name": "body",
                    "nullable": True,
                    "data_type": "longtext",
                    "max_length": 100,
                }
            )
    return dataclasses.replace(inputs, collector=collector)


def test_a_view_in_wave_1_never_reaches_the_lob_plan() -> None:
    # Discourse-shaped: wave 1 lists a view (shop.order_view) that the collector has no table for.
    inputs = _with_lob_view(_job())
    assert "shop.order_view" in inputs.report["migration_waves"][0]["tables"]
    rep = build_step_plans(inputs, prices=None)[0][0].replication
    assert rep is not None
    assert [t.table for t in rep.lob_tables] == ["shop.settings"]
    assert rep.lob_tables[0].mode == "limited"


def test_a_postgres_source_carries_its_unbounded_varchar_setting() -> None:
    inputs = _versioned(
        _job(waves=_waves(["aurora_postgresql"]), source_engine="postgresql"), "15.6"
    )
    collector = copy.deepcopy(dict(inputs.collector))
    for t in collector["database_schema"]["tables"]:
        if t["table_id"] == "shop.orders":
            t["columns"].append(
                {
                    "column_name": "note",
                    "nullable": True,
                    "data_type": "character varying",
                    "max_length": None,
                }
            )
    inputs = dataclasses.replace(inputs, collector=collector)
    rep = build_step_plans(inputs, prices=None)[0][0].replication
    assert rep is not None
    assert rep.target_engine == "aurora_postgresql" and rep.target_engine_line == "15"
    assert rep.unbounded_varchar.columns == 1
    assert rep.unbounded_varchar.column_names == ["shop.orders.note"]
    assert rep.unbounded_varchar.max_bytes is not None


def test_wordpress_step1_lists_every_wave_1_table_with_a_lob_column() -> None:
    root = Path(__file__).resolve().parents[4] / "tests/fixtures/real_runs/wordpress-both"
    report = json.loads((root / "report.json").read_text())
    collector = json.loads((root / "job/collector/output.json").read_text())
    wave1 = report["migration_waves"][0]
    inputs = JobInputs(
        job_id="wp",
        database_name="wordpress",
        report=report,
        assignment={"version": report["assignment_summary"]["version"], "query_assignments": []},
        collector=collector,
        schema_designs={},
    )
    rep = build_step_plans(inputs, prices=None)[0][0].replication
    assert rep is not None
    listed = {t.table for t in rep.lob_tables}
    assert "wordpress.wp_posts" in listed
    views = {v["view_id"] for v in collector["database_schema"].get("views") or []}
    base = [t for t in wave1["tables"] if t not in views]
    lob_types = set(dms_sizing().lob.types["mysql"])
    with_lob = {
        t["table_id"]
        for t in collector["database_schema"]["tables"]
        if t["table_id"] in base
        and any(str(c.get("data_type") or "").lower() in lob_types for c in t["columns"])
    }
    assert len(with_lob) == 16
    assert with_lob <= listed


def test_only_the_storage_above_the_included_amount_is_billed() -> None:
    inputs = _job(waves=[_waves()[0]])
    s = size_from_collector(inputs.collector)
    # Included storage is a cost-model input: a sizing with 20 GB above the included amount.
    big = s.model_copy(update={"allocated_storage_gb": s.included_storage_gb + 20})
    from src.tools.data_movement import plan as plan_module

    sections = _prices(
        instance_hourly={s.instance_class: 0.1},
        multi_az_instance_hourly={s.instance_class: 0.2},
        storage_price_per_gb_month=0.1,
        multi_az_storage_price_per_gb_month=0.25,
    ).sections  # fmt: skip
    multi, single, gaps = plan_module._dms_cost(sections, big)
    assert not gaps
    assert multi == 0.2 * HOURS_PER_MONTH + 0.25 * 20
    assert single == 0.1 * HOURS_PER_MONTH + 0.1 * 20
    # Allocated at or below the included amount bills no storage at all.
    small = s.model_copy(update={"allocated_storage_gb": s.included_storage_gb})
    assert plan_module._dms_cost(sections, small)[0] == 0.2 * HOURS_PER_MONTH


def test_the_cost_assumption_states_the_included_storage() -> None:
    inputs = _job(waves=[_waves()[0]])
    s = size_from_collector(inputs.collector)
    plan = build_step_plans(inputs, prices=_prices())[0][0]
    assert any(f"{s.included_storage_gb} GB included" in a for a in plan.cost_assumptions)
