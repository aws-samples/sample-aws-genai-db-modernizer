# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step k names one step everywhere (#1128, ADR-033 Decision 1).

The data movement plans number their steps by migration wave (``step-<k>/plan.json``)
until they plan per slice step (#1698). ``facts.slice_plan`` numbers its steps too, so
the two are tied: a slice plan numbered one step per wave plans as before, and one
numbered otherwise (two slice steps in one wave) is refused, never planned by wave.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.contracts.synthesis_output import SlicePlanFacts
from src.tools.data_movement.plan import JobInputs, _steps, build_step_plans

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "adoption_paths"


def _fragment(name: str) -> dict[str, Any]:
    fragment: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text())
    return fragment


def test_without_a_slice_plan_the_steps_are_the_waves() -> None:
    waves = _fragment("discourse")["migration_waves"]
    assert [k for k, _ in _steps(waves)] == [1, 2]
    assert [k for k, _ in _steps(waves, {"plan_steps": []})] == [1, 2]


def test_a_slice_plan_numbered_by_wave_gives_the_same_step_numbers() -> None:
    fragment = _fragment("wordpress")
    plan = fragment["facts"]["slice_plan"]
    steps = _steps(fragment["migration_waves"], plan)
    assert [k for k, _ in steps] == [s.step_number for s in SlicePlanFacts(**plan).plan_steps]


def test_a_slice_plan_numbered_apart_from_the_waves_is_refused() -> None:
    # Discourse: DynamoDB (step 2) and DocumentDB (step 3) both sit in wave 2, so step 3
    # by the slice plan would be step 2's second engine by wave.
    fragment = _fragment("discourse")
    with pytest.raises(ValueError, match="numbers its steps apart from the migration waves"):
        _steps(fragment["migration_waves"], fragment["facts"]["slice_plan"])


def test_build_step_plans_reads_the_slice_plan_before_planning() -> None:
    fragment = _fragment("discourse")
    inputs = JobInputs(
        job_id="job",
        database_name="discourse",
        report={"migration_waves": fragment["migration_waves"], "facts": fragment["facts"]},
        assignment={},
        collector={},
    )
    with pytest.raises(ValueError, match="numbers its steps apart from the migration waves"):
        build_step_plans(inputs, prices=None)
