# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Plans per step (#1797)."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from src.contracts.data_movement_plan import ContractRef
from src.storage.local_store import LocalArtifactStore
from src.tools.data_movement.plan import build_step_plans, write_plans

from .test_plan import PRICES, _job, _sized, _waves, _write_job


def _steps_of(count: int, **kwargs):  # type: ignore[no-untyped-def]
    return build_step_plans(_job(waves=_waves()[:count]), **kwargs)


def test_steps_follow_the_waves_until_the_slice_plan() -> None:
    steps, _hub = _steps_of(3, prices=None)
    assert [(p.step, p.wave) for p in steps] == [(1, 1), (2, 2), (3, 3)]
    assert steps[0].cdc.tool == "dms" and steps[1].hub == [] and steps[1].hub_topics


def test_two_steps_reading_a_table_share_its_topic() -> None:
    steps, hub = _steps_of(3, prices=None)
    assert hub is not None
    shared = set(steps[1].hub_topics) & set(steps[2].hub_topics)
    assert shared and all(sum(t.topic == s for t in hub.topics) == 1 for s in shared)


def test_a_later_step_takes_the_hubs_capture() -> None:
    steps, hub = _steps_of(3, prices=None)
    assert hub is not None
    for step in steps[1:]:
        assert step.cdc == hub.cdc and step.initial_load == hub.initial_load
        assert step.hub_topics == sorted(step.hub_topics)
        assert set(step.hub_topics) <= {t.topic for t in hub.topics}


def test_the_cluster_is_priced_once() -> None:
    steps, hub = build_step_plans(_sized(100.0), prices=PRICES)
    assert hub is not None
    assert any("MSK cluster" in a for a in hub.cost_assumptions)
    assert not any("MSK cluster" in a for p in steps for a in p.cost_assumptions)
    # The brokers sit in the hub's cost only: a step's cost is its writers.
    assert hub.monthly_cost_usd is not None and hub.monthly_cost_usd >= 0.2 * 3 * 730
    for step in steps[1:]:
        connectors = len(step.targets)
        assert step.monthly_cost_usd == round(0.1 * connectors * 730, 2)
        assert not [u for u in step.unsupported if u.kind == "cost"]


def test_no_later_step_means_no_hub() -> None:
    steps, hub = _steps_of(1, prices=None)
    assert len(steps) == 1 and hub is None


def test_step1_without_a_contract_has_no_reference() -> None:
    inputs = _job(waves=_waves()[:2])
    steps, _ = build_step_plans(inputs, prices=None)
    assert steps[0].contract is None
    ref = ContractRef(artifact="transformation-contract/v2/step-1.json", sha256="a" * 64)
    steps, _ = build_step_plans(inputs, prices=None, contracts={1: ref})
    assert steps[0].contract == ref and steps[1].contract is None


def test_the_writer_writes_steps_then_the_hub(tmp_path: Path) -> None:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    _write_job(store)
    written = write_plans(store, "shop", "job-1")
    assert written == [f"shop/job-1/data-movement/step-{n}/plan.json" for n in (1, 2, 3)] + [
        "shop/job-1/data-movement/hub/plan.json"
    ]
    assert store.read_json(written[0])["contract"] is None


def test_the_writer_pins_step_ones_contract_hash(tmp_path: Path) -> None:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    _write_job(store)
    base = "shop/job-1/transformation-contract/v2/step-1.json"
    store.write_json(base, {})
    store.write_bytes(
        "shop/job-1/transformation-contract/v2/step-1.sha256", ("b" * 64 + "\n").encode()
    )
    written = write_plans(store, "shop", "job-1")
    assert store.read_json(written[0])["contract"] == {
        "artifact": "transformation-contract/v2/step-1.json",
        "sha256": "b" * 64,
    }


def test_a_job_with_one_wave_writes_no_hub_file(tmp_path: Path) -> None:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    _write_job(store, _job(waves=_waves()[:1]))
    assert write_plans(store, "shop", "job-1") == ["shop/job-1/data-movement/step-1/plan.json"]


def test_a_malformed_contract_hash_names_its_file(tmp_path: Path) -> None:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    _write_job(store)
    store.write_json("shop/job-1/transformation-contract/v2/step-1.json", {})
    store.write_bytes("shop/job-1/transformation-contract/v2/step-1.sha256", b"ABC\n")
    with pytest.raises(ValueError, match="step-1.sha256"):
        write_plans(store, "shop", "job-1")


def test_the_writer_removes_a_stale_hub_plan_when_no_hub_is_built(tmp_path: Path) -> None:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    _write_job(store)
    write_plans(store, "shop", "job-1")
    assert store.exists("shop/job-1/data-movement/hub/plan.json")
    store.write_json("shop/job-1/data-movement/wave-2/plan.json", {"old": True})
    _write_job(store, _job(waves=_waves()[:1]))
    write_plans(store, "shop", "job-1")
    assert not store.exists("shop/job-1/data-movement/hub/plan.json")
    # A per-wave file from before #1797 is left alone.
    assert store.exists("shop/job-1/data-movement/wave-2/plan.json")


def test_contract_refs_use_the_resolved_assignment_version(tmp_path: Path) -> None:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    inputs = _job(waves=_waves()[:2])
    report = {k: v for k, v in inputs.report.items() if k != "assignment_summary"}
    inputs = dataclasses.replace(inputs, report=report)
    _write_job(store, inputs)
    store.write_json("shop/job-1/transformation-contract/v2/step-1.json", {})
    store.write_bytes("shop/job-1/transformation-contract/v2/step-1.sha256", ("c" * 64).encode())
    written = write_plans(store, "shop", "job-1")
    assert store.read_json(written[0])["contract"] == {
        "artifact": "transformation-contract/v2/step-1.json",
        "sha256": "c" * 64,
    }
