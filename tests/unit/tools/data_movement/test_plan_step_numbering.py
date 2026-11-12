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

import pytest

import src.tools.data_movement.plan as plan_module
from src.contracts.data_movement_plan import DataMovementPlan
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
        assert set(view["tables"]) == read


def _inputs(name: str) -> JobInputs:
    """The fragment as a job: a collection that holds every table the plan names, and the
    sources of truth its move units own, so every step plans real targets."""
    fragment = _fragment(name)
    plan = fragment["facts"]["slice_plan"]
    tables = sorted(
        {str(t) for w in fragment["migration_waves"] for t in w.get("tables") or []}
        | {t for u in plan["move_units"] for t in u["owned_tables"]}
        | {r["table"] for u in plan["move_units"] for r in u["replicated_in"]}
    )
    owned = {
        t: u["engine"]
        for u in plan["move_units"]
        if u["kind"] in ("owner", "replica_reads", "read_model")
        for t in u["owned_tables"]
    }
    return JobInputs(
        job_id="job",
        database_name=name,
        report={
            "migration_waves": fragment["migration_waves"],
            "facts": fragment["facts"],
            "table_sources": [
                {"table": t, "source_of_truth": e, "write_engines": [e], "replicas": []}
                for t, e in sorted(owned.items())
            ],
        },
        assignment={},
        collector={
            "database_schema": {
                "tables": [
                    {
                        "table_id": t,
                        "schema_name": t.split(".")[0],
                        "table_name": t.split(".")[-1],
                        "primary_key": ["id"],
                        "columns": [],
                    }
                    for t in tables
                ]
            }
        },
        assignment_version=1,
    )


def _numbering_problems(plans: list[DataMovementPlan], plan: SlicePlanFacts) -> list[str]:
    """How ``plans`` differ from the slice plan: step numbers, phase waves, and each later
    step's engine and owner tables (its move units' ``owned_tables``)."""
    problems = []
    if [(p.step, p.wave) for p in plans] != [
        (s.step_number, s.phase_wave) for s in plan.plan_steps
    ]:
        problems.append("numbering")
    units = {u.key: u for u in plan.move_units}
    for p, s in zip(plans[1:], plan.plan_steps[1:], strict=False):
        if not p.targets:
            problems.append(f"step {s.step_number} plans no target")
        if {t.engine for t in p.targets} != {s.engine}:
            problems.append(f"step {s.step_number} engines")
        if s.kind == "cache":
            continue
        owned = {t for st in s.step_stages for k in st.unit_keys for t in units[k].owned_tables}
        planned = {t for tg in p.targets if tg.role == "owner" for t in tg.tables}
        if planned != owned:
            problems.append(f"step {s.step_number} owners")
    return problems


@pytest.mark.parametrize("name", ["wordpress", "discourse"])
def test_build_step_plans_numbers_its_plans_by_the_slice_plan(name: str) -> None:
    inputs = _inputs(name)
    plans, _ = build_step_plans(inputs, prices=None)
    plan = SlicePlanFacts(**_fragment(name)["facts"]["slice_plan"])
    assert _numbering_problems(plans, plan) == []


def test_plans_numbered_by_wave_fail_the_check(monkeypatch: pytest.MonkeyPatch) -> None:
    # The per-wave numbering this replaces: discourse's two wave-2 steps become one plan.
    inputs = _inputs("discourse")
    monkeypatch.setattr(
        plan_module,
        "_steps",
        lambda waves, slice_plan=None: [(int(w["wave"]), w, None) for w in waves],
    )
    plans, _ = build_step_plans(inputs, prices=None)
    plan = SlicePlanFacts(**_fragment("discourse")["facts"]["slice_plan"])
    assert "numbering" in _numbering_problems(plans, plan)
