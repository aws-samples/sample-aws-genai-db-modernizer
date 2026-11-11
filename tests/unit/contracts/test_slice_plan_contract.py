# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The slice plan and adoption path contract (#1128: #1129, #1130, #1698).

The fixtures under ``tests/fixtures/adoption_paths/`` are the shape the renderers
(#1131, #1132, #1133) and the e2e-llm judge (#1136) build against while the
computation is written. These tests keep them valid and internally consistent, and
pin each invariant the contract's validators enforce, the negative case included.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from src.contracts.synthesis_output import (
    MOVING_UNIT_KINDS,
    STAGE_RANKS,
    AdoptionFacts,
    MigrationWave,
    ReportFacts,
    SlicePlanFacts,
    check_adoption_against_plan,
)

REPO = Path(__file__).resolve().parents[3]
FIXTURES = REPO / "tests" / "fixtures" / "adoption_paths"
SAMPLE_REPORT = (
    REPO / "docs" / "examples" / "wordpress" / "sample-run" / "synthesis" / "v2" / "report.json"
)
CASES = ("wordpress", "discourse")


def _fragment(name: str) -> dict[str, Any]:
    fragment: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text())
    return fragment


def _plan(fragment: dict[str, Any]) -> dict[str, Any]:
    plan: dict[str, Any] = fragment["facts"]["slice_plan"]
    return plan


def _units(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {u["key"]: u for u in plan["move_units"]}


def _positions(plan: dict[str, Any]) -> dict[str, tuple[int, int]]:
    return {
        key: (step["step_number"], STAGE_RANKS[stage["kind"]])
        for step in plan["plan_steps"]
        for stage in step["step_stages"]
        for key in stage["unit_keys"]
    }


# --- The fixtures validate and their figures agree --------------------------------------


@pytest.mark.parametrize("name", CASES)
def test_the_fixture_validates(name: str) -> None:
    fragment = _fragment(name)
    plan = SlicePlanFacts.model_validate(_plan(fragment))
    adoption = AdoptionFacts.model_validate(fragment["facts"]["adoption"])
    check_adoption_against_plan(adoption, plan)
    for wave in fragment["migration_waves"]:
        MigrationWave.model_validate(wave)
    assert plan.step_basis == "slices" and plan.step_count >= 3
    assert any(len(s.step_stages) >= 3 for s in plan.plan_steps), "several stages in a step"
    assert plan.dependencies and adoption.one_step is not None
    assert 2 <= adoption.path_count <= 3


@pytest.mark.parametrize("name", CASES)
def test_stage_query_counts_sum_to_the_step_and_the_wave(name: str) -> None:
    fragment = _fragment(name)
    plan, waves = _plan(fragment), {w["wave"]: w for w in fragment["migration_waves"]}
    units = _units(plan)
    by_wave: dict[int, int] = {}
    for step in plan["plan_steps"]:
        assert step["query_count"] == sum(s["query_count"] for s in step["step_stages"])
        for stage in step["step_stages"]:
            assert stage["query_count"] == sum(units[k]["query_count"] for k in stage["unit_keys"])
        if step["kind"] != "relational_cutover":
            by_wave[step["phase_wave"]] = by_wave.get(step["phase_wave"], 0) + step["query_count"]
    cutover = plan["plan_steps"][0]
    assert cutover["query_count"] == waves[1]["cutover_query_count"]
    assert cutover["query_count"] == fragment["facts"]["in_scope_query_count"]
    # Every later step rolls up into its engine phase: the wave's queries, exactly.
    assert by_wave == {n: w["query_count"] for n, w in waves.items() if n != 1}


@pytest.mark.parametrize("name", CASES)
def test_every_move_unit_is_in_exactly_one_stage(name: str) -> None:
    plan = _plan(_fragment(name))
    listed = [k for step in plan["plan_steps"] for s in step["step_stages"] for k in s["unit_keys"]]
    assert sorted(listed) == sorted(_units(plan))


@pytest.mark.parametrize("name", CASES)
def test_a_dependency_never_points_to_a_later_stage(name: str) -> None:
    plan = _plan(_fragment(name))
    position = _positions(plan)
    for dep in plan["dependencies"]:
        assert position[dep["depends_on"]] <= position[dep["unit_key"]], dep


@pytest.mark.parametrize("name", CASES)
def test_no_move_unit_splits_a_kept_whole_group(name: str) -> None:
    plan = _plan(_fragment(name))
    moving = [u for u in plan["move_units"] if u["kind"] in MOVING_UNIT_KINDS]
    for group in plan["kept_whole"]:
        holders = [u for u in moving if set(group["query_ids"]) & set(u["query_ids"])]
        assert len(holders) <= 1, group["group_id"]
        if holders:
            assert set(group["query_ids"]) <= set(holders[0]["query_ids"])
    # The fixture exercises both: groups a unit moves, and groups that stay on Aurora.
    held = {g for u in moving for g in u["kept_whole_ids"]}
    assert held and {g["group_id"] for g in plan["kept_whole"]} - held


@pytest.mark.parametrize("name", CASES)
def test_the_table_classes_sort_every_collected_table_once(name: str) -> None:
    fragment = _fragment(name)
    classes = _plan(fragment)["plan_steps"][0]["table_classes"]
    total = sum(classes[k] for k in ("stays_count", "leaves_count", "replicated_out_count"))
    assert total + classes["unread_count"] == fragment["facts"]["source_table_count"]
    assert classes["leaves_count"] == fragment["facts"]["tables_migrate"]


@pytest.mark.parametrize("name", CASES)
def test_the_full_target_path_agrees_with_the_headline_facts(name: str) -> None:
    facts = _fragment(name)["facts"]
    paths = {p["path_id"]: p for p in facts["adoption"]["paths"]}
    full = paths["full_target"]
    assert full["monthly_cost_usd"] == facts["projected_monthly_cost_usd"]
    assert full["monthly_cost_usd"] == round(
        sum(e["monthly_cost_usd"] for e in facts["engines"]), 2
    )
    assert full["queries_moved"] == facts["moving_query_count"]
    assert full["tables_migrate"] == facts["tables_migrate"]
    assert sum(full["queries_by_engine"].values()) == facts["in_scope_query_count"]
    assert full["critical_high_risks"] == sum(facts["critical_high_risks_by_engine"].values())
    if "relational_only" in paths:
        assert paths["relational_only"]["engines"] == facts["no_migration"]["engines"]
        assert paths["relational_only"]["queries_moved"] == 0
    # Each path's cost is its engines' cost; the delta is against Aurora's (+ cache's) alone.
    cost = {e["engine"]: e["monthly_cost_usd"] for e in facts["engines"]}
    relational = round(sum(cost[e] for e in facts["no_migration"]["engines"]), 2)
    for path in paths.values():
        assert path["monthly_cost_usd"] == round(sum(cost[e] for e in path["engines"]), 2)
        assert path["cost_delta_vs_relational_usd"] == round(
            path["monthly_cost_usd"] - relational, 2
        )


@pytest.mark.parametrize("name", CASES)
def test_the_one_step_checks_state_the_plan_s_own_figures(name: str) -> None:
    facts = _fragment(name)["facts"]
    plan, adoption = facts["slice_plan"], facts["adoption"]
    units = _units(plan)
    stage_c = [
        s
        for step in plan["plan_steps"]
        for s in step["step_stages"]
        if s["kind"] == "with_reverse_replica"
    ]
    targets = {s["engine"] for s in plan["plan_steps"] if s["kind"] in ("slice", "engine_wave")}
    observed = {c["rule"]: c["observed"] for c in adoption["one_step"]["one_step_checks"]}
    assert observed == {
        "no_open_critical_high_risk": sum(facts["critical_high_risks_by_engine"].values()),
        "at_most_one_purpose_built_target": len(targets),
        "no_reverse_replica_stage": len(stage_c),
    }
    streams = sum(len(units[k]["reverse_replicas"]) for s in stage_c for k in s["unit_keys"])
    assert adoption["one_step"]["staged_temporary_stream_count"] == streams


@pytest.mark.parametrize("name", CASES)
def test_a_held_stage_lists_exactly_its_units_risks(name: str) -> None:
    plan = _plan(_fragment(name))
    units = _units(plan)
    for step in plan["plan_steps"]:
        for stage in step["step_stages"]:
            risks = sorted({r for k in stage["unit_keys"] for r in units[k]["holding_risk_ids"]})
            assert sorted(stage["holding_risk_ids"]) == risks


def test_the_report_facts_validator_checks_the_paths_against_the_plan() -> None:
    sample = json.loads(SAMPLE_REPORT.read_text())["facts"]
    fixture = _fragment("discourse")["facts"]
    facts = {**sample, "slice_plan": fixture["slice_plan"], "adoption": fixture["adoption"]}
    ReportFacts.model_validate(facts)
    adoption = copy.deepcopy(fixture["adoption"])
    adoption["paths"][0]["stage_count"] = adoption["paths"][0]["gate_count"] = 5
    with pytest.raises(ValidationError, match="stage_count must count its steps' stages"):
        ReportFacts.model_validate({**facts, "adoption": adoption})
    # Either block alone is not cross-checked: a report can carry one before the other.
    ReportFacts.model_validate({**sample, "adoption": adoption})


# --- Every invariant fails when broken ---------------------------------------------------


def _unit(plan: dict[str, Any], key_part: str) -> dict[str, Any]:
    return next(u for u in plan["move_units"] if key_part in u["key"])


def _recount(plan: dict[str, Any]) -> None:
    """Every stage and step count summed again from the units, so a break reaches the
    invariant it targets rather than a count check first."""
    units = _units(plan)
    for step in plan["plan_steps"]:
        for stage in step["step_stages"]:
            members = [units[k] for k in stage["unit_keys"] if k in units]
            stage["unit_count"] = len(stage["unit_keys"])
            stage["query_count"] = sum(u["query_count"] for u in members)
            stage["owned_table_count"] = sum(u["owned_table_count"] for u in members)
        for key in ("unit_count", "query_count", "owned_table_count"):
            step[key] = sum(s[key] for s in step["step_stages"])


def _split_a_write_group(plan: dict[str, Any]) -> None:
    # Move one of wp_posts' writes into the comments unit: the group is split.
    posts, comments = _unit(plan, "wp_postmeta"), _unit(plan, "wp_commentmeta")
    posts["query_ids"].remove("q094")
    posts["write_query_ids"].remove("q094")
    posts["query_count"] -= 1
    comments["query_ids"].append("q094")
    comments["query_count"] += 1


def _drop_a_kept_whole_claim(plan: dict[str, Any]) -> None:
    _unit(plan, "wp_postmeta")["kept_whole_ids"].remove("write:wordpress.wp_posts")


def _query_in_two_units(plan: dict[str, Any]) -> None:
    sessions = _unit(plan, "wp_woocommerce_sessions")
    sessions["query_ids"].append("q077")
    sessions["query_count"] += 1


def _unit_in_two_stages(plan: dict[str, Any]) -> None:
    stage = plan["plan_steps"][2]["step_stages"][1]
    stage["unit_keys"].append(plan["plan_steps"][2]["step_stages"][0]["unit_keys"][0])


def _unit_in_no_stage(plan: dict[str, Any]) -> None:
    stage = plan["plan_steps"][2]["step_stages"][0]
    stage["unit_keys"].pop()


def _dependency_on_a_later_stage(plan: dict[str, Any]) -> None:
    plan["dependencies"].append(
        {
            "unit_key": "dynamodb:wordpress.wp_users",
            "depends_on": "dynamodb:wordpress.wp_postmeta",
            "kinds": ["shared_table"],
            "edge_weight": 0.1,
            "tables": ["wordpress.wp_users"],
        }
    )


def _stage_count_off(plan: dict[str, Any]) -> None:
    plan["plan_steps"][2]["step_stages"][2]["query_count"] += 1
    plan["plan_steps"][2]["query_count"] += 1


def _step_count_off(plan: dict[str, Any]) -> None:
    plan["plan_steps"][2]["query_count"] += 1


def _stages_out_of_order(plan: dict[str, Any]) -> None:
    stages = plan["plan_steps"][2]["step_stages"]
    stages[0], stages[1] = stages[1], stages[0]


def _wrong_label(plan: dict[str, Any]) -> None:
    plan["plan_steps"][2]["step_stages"][1]["label"] = "3a"


def _risk_outside_stage_d(plan: dict[str, Any]) -> None:
    _unit(plan, "wp_commentmeta")["holding_risk_ids"] = ["RISK-001"]


def _pilot_not_first(plan: dict[str, Any]) -> None:
    _unit(plan, "wordpress.wp_term_taxonomy")["pilot"] = False
    _unit(plan, "dynamodb:wordpress.wp_users")["pilot"] = True


def _self_contained_with_a_replica(plan: dict[str, Any]) -> None:
    _unit(plan, "wp_woocommerce_sessions")["replicated_in"] = [
        {"table": "wordpress.wp_users", "source_of_truth": "aurora_mysql"}
    ]


def _unit_on_another_engine(plan: dict[str, Any]) -> None:
    _unit(plan, "wp_woocommerce_sessions")["engine"] = "documentdb"


def _leaving_table_in_the_wrong_stage(plan: dict[str, Any]) -> None:
    classes = plan["plan_steps"][0]["table_classes"]
    next(t for t in classes["leaves"] if t["table"] == "wordpress.wp_posts")["stage_label"] = "3b"


def _table_in_two_classes(plan: dict[str, Any]) -> None:
    classes = plan["plan_steps"][0]["table_classes"]
    classes["stays"].append("wordpress.wp_users")
    classes["stays_count"] += 1


def _steps_not_consecutive(plan: dict[str, Any]) -> None:
    plan["plan_steps"][2]["step_number"] = 4
    for stage in plan["plan_steps"][2]["step_stages"]:
        stage["label"] = "4" + stage["label"][1:]


BREAKS: dict[str, tuple[Callable[[dict[str, Any]], None], str]] = {
    "split write group": (_split_a_write_group, "split across units"),
    "unclaimed group": (_drop_a_kept_whole_claim, "is held by"),
    "query in two units": (_query_in_two_units, "is in units"),
    "unit in two stages": (_unit_in_two_stages, "more than one stage"),
    "unit in no stage": (_unit_in_no_stage, "units in no stage"),
    "dependency on a later stage": (_dependency_on_a_later_stage, "moves in a later stage"),
    "stage count": (_stage_count_off, "query_count must sum its units"),
    "step count": (_step_count_off, "query_count must sum the stages"),
    "stage order": (_stages_out_of_order, "stages run a, b, c, d"),
    "stage label": (_wrong_label, "labelled by its step and kind"),
    "risk outside stage d": (_risk_outside_stage_d, "a risk holds its unit in stage d"),
    "pilot not first": (_pilot_not_first, "the pilot is the first unit"),
    "self-contained with a replica": (_self_contained_with_a_replica, "not self-contained"),
    "unit on another engine": (_unit_on_another_engine, "not dynamodb"),
    "leaving stage": (_leaving_table_in_the_wrong_stage, "leaves must be every table"),
    "table in two classes": (_table_in_two_classes, "exactly one class"),
    "step numbers": (_steps_not_consecutive, "numbered 1, 2"),
}


@pytest.mark.parametrize("case", sorted(BREAKS))
def test_a_broken_plan_is_refused(case: str) -> None:
    breaker, message = BREAKS[case]
    plan = copy.deepcopy(_plan(_fragment("wordpress")))
    breaker(plan)
    if case not in ("stage count", "step count"):
        _recount(plan)
    with pytest.raises(ValidationError, match=message):
        SlicePlanFacts.model_validate(plan)


def test_a_moving_unit_holds_a_whole_group_even_when_it_alone_names_it() -> None:
    # A group of two queries where one unit moves one of them and no other unit moves the
    # other: the unit moves part of the group, which is a split too (the rest stays).
    plan = copy.deepcopy(_plan(_fragment("wordpress")))
    plan["kept_whole"].append(
        {
            "group_id": "co_dependency:3",
            "kind": "co_dependency",
            "query_ids": ["q075", "q050"],
            "table": None,
        }
    )
    _unit(plan, "wp_woocommerce_sessions")["kept_whole_ids"].append("co_dependency:3")
    with pytest.raises(ValidationError, match="moves part of group co_dependency:3"):
        SlicePlanFacts.model_validate(plan)


def test_the_cache_and_the_cutover_do_not_split_a_group() -> None:
    # The cache fronts reads that still run on their owner, and the cutover carries
    # everything: a group with a cached member and members that stay is whole.
    plan = copy.deepcopy(_plan(_fragment("wordpress")))
    plan["kept_whole"].append(
        {"group_id": "co_dependency:3", "kind": "co_dependency", "query_ids": ["q001", "q050"]}
    )
    SlicePlanFacts.model_validate(plan)


ADOPTION_BREAKS: dict[str, tuple[Callable[[dict[str, Any]], None], str]] = {
    "path count": (lambda a: a.update(path_count=3), "path_count"),
    "default not offered": (
        lambda a: a.update(default_path_id="through_step_9"),
        "default path is an offered",
    ),
    "full target not last": (lambda a: a["paths"].reverse(), "listed by the step"),
    "one-step suits with a failed check": (
        lambda a: a["one_step"].update(suits_one_step=True),
        "every check passed",
    ),
    "one-step check passed wrongly": (
        lambda a: a["one_step"]["one_step_checks"][2].update(passed=True),
        "observed is at most limit",
    ),
    "path steps": (lambda a: a["paths"][0].update(step_count=1), "takes steps 1 to"),
}


@pytest.mark.parametrize("case", sorted(ADOPTION_BREAKS))
def test_broken_adoption_facts_are_refused(case: str) -> None:
    breaker, message = ADOPTION_BREAKS[case]
    adoption = copy.deepcopy(_fragment("wordpress")["facts"]["adoption"])
    breaker(adoption)
    with pytest.raises(ValidationError, match=message):
        AdoptionFacts.model_validate(adoption)


def test_paths_must_follow_the_plan_s_steps() -> None:
    fragment = _fragment("wordpress")
    plan = SlicePlanFacts.model_validate(_plan(fragment))
    adoption = copy.deepcopy(fragment["facts"]["adoption"])
    adoption["paths"][1]["engines"] = ["aurora_mysql", "dynamodb"]
    adoption["paths"][1]["queries_by_engine"] = {"aurora_mysql": 67, "dynamodb": 40}
    with pytest.raises(ValueError, match="engines must be its steps' engines"):
        check_adoption_against_plan(AdoptionFacts.model_validate(adoption), plan)
    adoption = copy.deepcopy(fragment["facts"]["adoption"])
    adoption["one_step"]["staged_cutovers"] = 4
    with pytest.raises(ValueError, match="every stage of the plan"):
        check_adoption_against_plan(AdoptionFacts.model_validate(adoption), plan)


def test_the_empty_structures_validate() -> None:
    plan = SlicePlanFacts(step_basis="engine_waves")
    adoption = AdoptionFacts()
    check_adoption_against_plan(adoption, plan)
    with pytest.raises(ValidationError, match="slice_basis is set exactly"):
        SlicePlanFacts(step_basis="slices")
