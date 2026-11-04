# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The hub plan: one capture per job (#1797)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.contracts.data_movement_plan import HubPlan
from src.tools.data_movement.plan import JobInputs, _hub_plan, _JobView, topic_prefix

from .test_plan import _aurora_tables, _job, _waves


def _hub(inputs: JobInputs) -> HubPlan | None:
    waves: list[Mapping[str, Any]] = sorted(
        inputs.report["migration_waves"], key=lambda w: int(w["wave"])
    )
    return _hub_plan(_JobView.build(inputs, None), waves)


def test_the_hub_prefix_is_per_job() -> None:
    assert topic_prefix("job-1") == "dm-job-1" and topic_prefix("job-1", 2) == "dm-job-1-w2"


def test_the_hub_captures_every_aurora_table_with_its_own_snapshot() -> None:
    inputs = _job()
    hub = _hub(inputs)
    assert hub is not None and hub.source.engine.startswith("aurora_")
    assert hub.cdc.snapshot_mode == "initial" and hub.initial_load.strategy == "connector_snapshot"
    assert {t.table for t in hub.topics} >= {"shop.orders", "shop.order_tags", "shop.settings"}
    assert {t.table for t in hub.topics} <= set(_aurora_tables(inputs))
    assert all(t.topic.startswith("dm-job-1.") for t in hub.topics)
    assert hub.cdc.topic_prefix == "dm-job-1"


def test_a_hub_on_the_source_has_no_signal_table() -> None:
    hub = _hub(_job(aurora=False))
    assert hub is not None and hub.source.engine == "mysql"
    assert hub.cdc.signal_table is None
    assert not [s for s in hub.cdc.required_source_settings if s.name == "signal table"]
    assert {t.table for t in hub.topics} >= {"shop.orders", "shop.order_tags"}


def test_a_hub_on_aurora_names_a_signal_table() -> None:
    hub = _hub(_job())
    assert hub is not None and hub.cdc.signal_table
    assert [s for s in hub.cdc.required_source_settings if s.name == "signal table"]


def test_a_job_with_only_the_aurora_wave_has_no_hub() -> None:
    assert _hub(_job(waves=_waves()[:1])) is None


def test_the_hub_prices_the_cluster_and_connector_once() -> None:
    hub = _hub(_job())
    assert hub is not None
    assert [s.name for s in hub.stacks] == ["msk-hub", "debezium-source"]


def test_a_keyed_table_a_step_reads_without_a_hub_topic_is_unsupported() -> None:
    from src.tools.data_movement.plan import build_step_plans

    waves = _waves()
    narrowed = [*waves]
    narrowed[0] = {**waves[0], "tables": ["shop.orders"]}
    steps, hub = build_step_plans(_job(waves=narrowed), prices=None)
    assert hub is not None and "shop.order_tags" not in {t.table for t in hub.topics}
    gaps = [
        u
        for u in steps[2].unsupported
        if u.kind == "unknown_table" and u.table == "shop.order_tags"
    ]
    assert [g.reason for g in gaps] == ["the hub plan carries no topic for it"]
    assert not [
        u for u in steps[2].unsupported if u.table == "shop.orders" and u.kind == "unknown_table"
    ]
