# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Plans per step and the hub plan (#1797)."""

from __future__ import annotations

from src.contracts.data_movement_plan import ContractRef, DataMovementPlan, HubPlan
from src.shared.contract_versioning import contract_for_artifact, current_version, load_contract

_MIN = {"job_id": "j", "database_name": "d", "assignment_version": 2, "wave": 1, "wave_count": 2, "title": "t",
        "source": {"engine": "mysql", "database": "d"}, "cdc": {"tool": "dms"},
        "initial_load": {"strategy": "dms_full_load"},
        "gates": {"wave_gate": "g", "max_lag_seconds": 5}}  # fmt: skip


def test_a_plan_written_before_steps_still_loads():
    path = "d/j/data-movement/wave-2/plan.json"
    model = contract_for_artifact(path)
    assert model is DataMovementPlan
    old = {**_MIN, "contract_version": "1.0", "wave": 2,
        "hub": [{"table": "d.t", "topic": "dm-j-w2.d.t", "key_columns": ["id"],
                 "key_source": "primary_key", "partitions": 3}]}  # fmt: skip
    p = load_contract(DataMovementPlan, old, artifact=path)
    assert p.wave == 2 and p.step is None and p.contract is None and p.hub_topics == []
    assert [t.topic for t in p.hub] == ["dm-j-w2.d.t"]
    assert p.contract_version == current_version(DataMovementPlan)


def test_a_step_plan_carries_its_step_contract_and_topics():
    p = DataMovementPlan.model_validate({**_MIN, "step": 2, "hub_topics": ["dm-j.d.t"],
        "contract": {"artifact": "transformation-contract/v2/step-2.json", "sha256": None}})  # fmt: skip
    assert p.step == 2 and p.hub_topics == ["dm-j.d.t"] and isinstance(p.contract, ContractRef)
    assert DataMovementPlan.model_validate(_MIN).step is None


def test_the_hub_plan_is_its_own_contract():
    h = HubPlan.model_validate({"job_id": "j", "database_name": "d", "assignment_version": 2,
        "source": {"engine": "aurora_mysql", "database": "d"}, "cdc": {"tool": "debezium"},
        "initial_load": {"strategy": "connector_snapshot"}})  # fmt: skip
    assert h.contract_version == current_version(HubPlan) and h.topics == []


def test_the_new_paths_are_registered():
    assert contract_for_artifact("d/j/data-movement/step-3/plan.json") is DataMovementPlan
    assert contract_for_artifact("d/j/data-movement/hub/plan.json") is HubPlan
    assert contract_for_artifact("d/j/data-movement/wave-2/plan.json") is DataMovementPlan
