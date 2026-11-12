# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The slice plan on every sample's real run (#1129, #1698, epic #1128).

Synthesis computes ``facts.slice_plan`` from the effective assignment: move units that
keep every group routing placed as one whole and one writer per table, the dependency
graph's edges and metrics, the steps (the relational step, then one slice or the cache's
reads per step for a homogeneous source, one wave's engine per step otherwise) and their
stages a to d. Here every contract validator passes on each sample's plan, the plan
covers every moving query exactly once and rolls up into the migration waves, it is the
same under any hash seed, and a plan broken by hand is refused.
"""

from __future__ import annotations

import copy
import json
import subprocess  # nosec B404 -- runs this repo's own interpreter on a fixed script
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from src.agents.referee.write_groups import assignment_placement_groups
from src.contracts.synthesis_output import ReportFacts, SlicePlanFacts
from src.shared.cache_engines import CACHE_ENGINES
from src.tools.validation.customer_text_guard import scan_json
from tests.e2e.pipeline import REPO, PipelineResult, _env

MOVING = {"owner", "replica_reads", "read_model"}


def _report(run: PipelineResult) -> dict[str, Any]:
    out: dict[str, Any] = json.loads(Path(run.report_json()).read_text(encoding="utf-8"))
    return out


def _assignment(run: PipelineResult, report: dict[str, Any]) -> dict[str, Any]:
    version = report["assignment_summary"]["version"]
    path = run.job_dir() / "assignment" / f"v{version}" / "assignment.json"
    out: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return out


def _plan(report: dict[str, Any]) -> dict[str, Any]:
    plan: dict[str, Any] = report["facts"]["slice_plan"]
    return plan


def test_the_slice_plan_passes_every_contract_validator(run: PipelineResult) -> None:
    report = _report(run)
    plan = _plan(report)
    assert plan["plan_steps"], run.db
    assert SlicePlanFacts.model_validate(plan).model_dump(mode="json") == plan
    # The cross-checks against the report's own engines, costs and risks.
    ReportFacts.model_validate(report["facts"])
    assert scan_json(plan, "$.facts.slice_plan") == []
    homogeneous = report["migration_waves"][0].get("homogeneity") == "homogeneous"
    assert plan["step_basis"] == ("slices" if homogeneous else "engine_waves"), run.db
    first = plan["plan_steps"][0]
    assert first["kind"] == "relational_step" and first["table_classes"] is not None


def test_every_moving_query_is_in_one_unit_and_no_group_is_split(run: PipelineResult) -> None:
    report = _report(run)
    plan = _plan(report)
    assignment = _assignment(run, report)
    relational = plan["plan_steps"][0]["engine"]
    holder: dict[str, str] = {}
    for unit in plan["move_units"]:
        if unit["kind"] in MOVING:
            for qid in unit["query_ids"]:
                assert qid not in holder, (run.db, qid)
                holder[qid] = unit["key"]
    engine_of = {u["key"]: u["engine"] for u in plan["move_units"]}
    moving = {
        qa["query_id"]: qa["assigned_engine"]
        for qa in assignment["query_assignments"]
        if qa.get("in_scope", True)
        and qa.get("assigned_engine") not in (None, "unservable", relational)
        and qa["assigned_engine"] not in CACHE_ENGINES
    }
    assert moving and set(holder) == set(moving), run.db
    assert all(engine_of[holder[q]] == e for q, e in moving.items())
    # Every group routing places as one sits in one unit, or on the relational engine.
    in_scope = {
        qa["query_id"] for qa in assignment["query_assignments"] if qa.get("in_scope", True)
    }
    for group in assignment_placement_groups(assignment):
        placed = [q for q in group if q in moving]
        assert len({holder[q] for q in placed}) <= 1, (run.db, group[:3])
        if placed:
            assert set(placed) == {q for q in group if q in in_scope}, (run.db, group[:3])
    # One writer per table: a table a unit owns is written by that unit only.
    owner = {
        t: u["key"] for u in plan["move_units"] if u["kind"] in MOVING for t in u["owned_tables"]
    }
    for table, writes in (assignment.get("write_groups") or {}).items():
        if table in owner:
            assert {holder.get(q) for q in writes} == {owner[table]}, (run.db, table)


def test_the_steps_roll_up_into_the_migration_waves(run: PipelineResult) -> None:
    report = _report(run)
    plan = _plan(report)
    waves = {w["wave"]: w for w in report["migration_waves"]}
    for phase in plan["wave_phases"]:
        wave = waves[phase["phase_wave"]]
        assert set(phase["engines"]) <= set(wave["engines"]), (run.db, phase["phase_wave"])
        expected = wave.get("cutover_query_count") or wave["query_count"]
        assert phase["query_count"] == expected, (run.db, phase["phase_wave"])
    # Step 1 carries every collected table, each in one class.
    classes = plan["plan_steps"][0]["table_classes"]
    sorted_out = [
        *classes["stays"],
        *(t["table"] for t in classes["leaves"]),
        *classes["replicated_out"],
        *classes["unread"],
    ]
    assert sorted(sorted_out) == sorted(waves[1]["tables"]), run.db


def test_the_slice_plan_ignores_the_hash_seed(run: PipelineResult) -> None:
    """The plan recomputed from the job's artifacts in fresh processes, under several hash
    seeds, is the stored plan."""
    report = _report(run)
    script = (
        "import json, sys\n"
        "from src.agents.referee.slice_inputs import slice_inputs\n"
        "from src.shared.adoption_facts import slice_plan_facts\n"
        "report, assignment, collector = (json.load(open(p)) for p in sys.argv[1:4])\n"
        "inputs = slice_inputs(assignment, collector,"
        " report.get('routine_followers') or [])\n"
        "print(json.dumps(slice_plan_facts(report, inputs), sort_keys=True))\n"
    )
    version = report["assignment_summary"]["version"]
    paths = [
        str(run.report_json()),
        str(run.job_dir() / "assignment" / f"v{version}" / "assignment.json"),
        str(run.job_dir() / "collector" / "output.json"),
    ]
    stored = json.dumps(_plan(report), sort_keys=True)
    for seed in ("0", "1", "2"):
        proc = subprocess.run(  # nosec B603 -- fixed interpreter and script
            [sys.executable, "-c", script, *paths],
            cwd=REPO,
            capture_output=True,
            text=True,
            env={**_env(), "PYTHONHASHSEED": seed},
            timeout=300,
        )
        assert proc.returncode == 0, proc.stderr[-2000:]
        assert proc.stdout.strip() == stored, (run.db, seed)


def _swap_split(plan: dict[str, Any]) -> dict[str, Any]:
    """The plan with one query of a kept-whole group swapped into another moving unit
    for one of that unit's ungrouped reads: every count stays the same, so only the group
    rule can refuse it."""
    broken = copy.deepcopy(plan)
    units = [u for u in broken["move_units"] if u["kind"] in MOVING]
    grouped = {q for g in broken["kept_whole"] for q in g["query_ids"]}
    holder = next(u for u in units if u["kept_whole_ids"])
    group = next(g for g in broken["kept_whole"] if g["group_id"] in holder["kept_whole_ids"])
    x = group["query_ids"][0]
    other = next(
        u
        for u in units
        if u is not holder
        and any(q not in grouped and q not in u["write_query_ids"] for q in u["query_ids"])
    )
    y = next(
        q for q in other["query_ids"] if q not in grouped and q not in other["write_query_ids"]
    )
    holder["query_ids"] = sorted([q for q in holder["query_ids"] if q != x] + [y])
    other["query_ids"] = sorted([q for q in other["query_ids"] if q != y] + [x])
    if x in holder["write_query_ids"]:
        holder["write_query_ids"].remove(x)
        other["write_query_ids"] = sorted([*other["write_query_ids"], x])
    return broken


def test_a_plan_that_splits_a_kept_whole_group_is_refused(run: PipelineResult) -> None:
    plan = _plan(_report(run))
    broken = _swap_split(plan)
    assert broken != plan
    with pytest.raises(ValidationError, match="group"):
        SlicePlanFacts.model_validate(broken)


def test_a_dependency_on_a_later_unit_is_refused(run: PipelineResult) -> None:
    plan = _plan(_report(run))
    assert plan["dependencies"], run.db
    broken = copy.deepcopy(plan)
    edge = broken["dependencies"][0]
    edge["unit_key"], edge["depends_on"] = edge["depends_on"], edge["unit_key"]
    with pytest.raises(ValidationError, match="does not run before it"):
        SlicePlanFacts.model_validate(broken)
