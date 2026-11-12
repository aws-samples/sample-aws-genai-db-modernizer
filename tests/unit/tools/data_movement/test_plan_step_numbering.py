# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step k names one step everywhere (#1128, #1698, ADR-033 Decision 1).

The data movement plans number their steps by ``facts.slice_plan.plan_steps[].step_number``
(``step-<k>/plan.json``). Without a slice plan they follow the migration waves; with one,
each step is planned from its phase wave: as the wave when it is that wave's one step on
its one engine, else as the wave narrowed to the step's engine, tables and queries.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.contracts.synthesis_output import SlicePlanFacts
from src.tools.data_movement.plan import JobInputs, _steps, build_step_plans

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "adoption_paths"


def _fragment(name: str) -> dict[str, Any]:
    fragment: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text())
    return fragment


def test_without_a_slice_plan_the_steps_are_the_waves() -> None:
    waves = _fragment("discourse")["migration_waves"]
    assert [k for k, _, _ in _steps(waves)] == [1, 2]
    assert [k for k, _, _ in _steps(waves, {"plan_steps": []})] == [1, 2]
    assert all(
        view is wave and queries is None
        for (_, view, queries), wave in zip(_steps(waves), waves, strict=True)
    )


def test_a_slice_plan_numbered_by_wave_gives_the_same_step_numbers() -> None:
    fragment = _fragment("wordpress")
    plan = fragment["facts"]["slice_plan"]
    steps = _steps(fragment["migration_waves"], plan)
    assert [k for k, _, _ in steps] == [s.step_number for s in SlicePlanFacts(**plan).plan_steps]
    # Each step is its wave's one step on the wave's one engine: planned as the wave.
    assert all(queries is None for _, _, queries in steps)


def test_the_steps_are_the_slice_plan_s_when_a_wave_holds_several() -> None:
    # Discourse: DynamoDB (step 2) and DocumentDB (step 3) both sit in wave 2, so each is
    # planned from wave 2 narrowed to its own engine, tables and queries.
    fragment = _fragment("discourse")
    plan = SlicePlanFacts(**fragment["facts"]["slice_plan"])
    steps = _steps(fragment["migration_waves"], fragment["facts"]["slice_plan"])
    assert [k for k, _, _ in steps] == [s.step_number for s in plan.plan_steps] == [1, 2, 3]
    assert [int(view["wave"]) for _, view, _ in steps] == [s.phase_wave for s in plan.plan_steps]
    units = {u.key: u for u in plan.move_units}
    for (_, view, queries), step in zip(steps[1:], plan.plan_steps[1:], strict=True):
        assert view["engines"] == [step.engine]
        keys = [k for stage in step.step_stages for k in stage.unit_keys]
        assert queries == {q for k in keys for q in units[k].query_ids}
        read = {
            t
            for k in keys
            for t in [*units[k].owned_tables, *(r.table for r in units[k].replicated_in)]
        }
        assert set(view["tables"]) <= read


def test_build_step_plans_numbers_its_plans_by_the_slice_plan() -> None:
    fragment = _fragment("discourse")
    inputs = JobInputs(
        job_id="job",
        database_name="discourse",
        report={"migration_waves": fragment["migration_waves"], "facts": fragment["facts"]},
        assignment={},
        collector={},
        assignment_version=1,
    )
    plans, _ = build_step_plans(inputs, prices=None)
    steps = fragment["facts"]["slice_plan"]["plan_steps"]
    assert [(p.step, p.wave) for p in plans] == [(s["step_number"], s["phase_wave"]) for s in steps]
    assert [[t.engine for t in p.targets] for p in plans[1:]] == [
        [s["engine"]] * len(p.targets) for p, s in zip(plans[1:], steps[1:], strict=True)
    ]
