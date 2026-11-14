# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The slice plan and adoption path contract (#1128: #1129, #1130, #1698).

The fixtures under ``tests/fixtures/adoption_paths/`` are the shape the renderers
(#1131, #1132, #1133) and the e2e-llm judge (#1136) build against while the
computation is written. These tests keep them valid and internally consistent, and
break each invariant the contract's validators enforce: every ``raise`` in them is
reached by one case below.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, get_args

import pytest
from pydantic import ValidationError

from src.contracts.synthesis_output import (
    MOVING_UNIT_KINDS,
    AdoptionFacts,
    MigrationWave,
    RelationalTableClasses,
    ReportFacts,
    SlicePlanFacts,
    SliceStep,
    check_adoption_against_plan,
    phase_facts,
    summary_path_ids,
)
from src.contracts.transformation_contract import TableClass
from src.tools.validation.customer_text_guard import scan_json

REPO = Path(__file__).resolve().parents[3]
FIXTURES = REPO / "tests" / "fixtures" / "adoption_paths"
SAMPLE_REPORT = (
    REPO / "docs" / "examples" / "wordpress" / "sample-run" / "synthesis" / "v2" / "report.json"
)
CASES = ("wordpress", "discourse")

Mutate = Callable[[dict[str, Any]], Any]


def _fragment(name: str) -> dict[str, Any]:
    fragment: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text())
    return fragment


def _plan(fragment: dict[str, Any]) -> dict[str, Any]:
    plan: dict[str, Any] = fragment["facts"]["slice_plan"]
    return plan


def _units(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {u["key"]: u for u in plan["move_units"]}


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
def test_the_fixture_passes_the_customer_text_guard(name: str) -> None:
    # Unit and slice keys lead with an engine id; the guard reads them as ids, not prose.
    assert scan_json(_fragment(name)) == []


@pytest.mark.parametrize("name", CASES)
def test_the_phases_roll_the_steps_up_into_the_waves(name: str) -> None:
    fragment = _fragment(name)
    plan, waves = _plan(fragment), {w["wave"]: w for w in fragment["migration_waves"]}
    phases = {p["phase_wave"]: p for p in plan["wave_phases"]}
    assert set(phases) == set(waves)
    for n, wave in waves.items():
        expected = wave["cutover_query_count"] if n == 1 else wave["query_count"]
        assert phases[n]["query_count"] == expected
    assert plan["plan_steps"][0]["query_count"] == fragment["facts"]["in_scope_query_count"]


def test_two_steps_in_one_wave_roll_up_into_one_phase() -> None:
    # Discourse: DynamoDB (step 2) and DocumentDB (step 3) share wave 2's engine phase,
    # and its stages are both steps' (#1131's per-wave stages, #1133's stage count).
    phase = _plan(_fragment("discourse"))["wave_phases"][1]
    assert phase["phase_wave"] == 2 and phase["step_numbers"] == [2, 3]
    assert phase["engines"] == ["dynamodb", "documentdb"]
    assert phase["stage_labels"] == ["2a", "2c", "2d", "3a", "3b"]
    assert phase["stage_count"] == 5
    assert phase["stage_kinds"] == [
        "replica_reads",
        "self_contained",
        "with_reverse_replica",
        "risk_held",
    ]


@pytest.mark.parametrize("name", CASES)
def test_no_move_unit_splits_a_kept_whole_group(name: str) -> None:
    plan = _plan(_fragment(name))
    moving = [u for u in plan["move_units"] if u["kind"] in MOVING_UNIT_KINDS]
    for group in plan["kept_whole"]:
        holders = [u for u in moving if set(group["query_ids"]) & set(u["query_ids"])]
        assert len(holders) <= 1, group["group_id"]
        if holders:
            assert set(group["query_ids"]) <= set(holders[0]["query_ids"])
            if group["table"]:
                assert group["table"] in holders[0]["owned_tables"]
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


def test_the_table_classes_are_the_transformation_contract_s() -> None:
    lists = {
        name
        for name, field in RelationalTableClasses.model_fields.items()
        if not name.endswith("_count")
    }
    assert lists == set(get_args(TableClass))


@pytest.mark.parametrize("name", CASES)
def test_the_full_target_path_agrees_with_the_headline_facts(name: str) -> None:
    facts = _fragment(name)["facts"]
    paths = {p["path_id"]: p for p in facts["adoption"]["paths"]}
    full = paths["full_target"]
    assert full["monthly_cost_usd"] == facts["projected_monthly_cost_usd"]
    assert full["queries_moved"] == facts["moving_query_count"]
    assert full["tables_migrate"] == facts["tables_migrate"]
    assert sum(full["queries_by_engine"].values()) == facts["in_scope_query_count"]
    if "relational_only" in paths:
        assert paths["relational_only"]["engines"] == facts["no_migration"]["engines"]


def _report_facts(name: str) -> dict[str, Any]:
    """The sample report's stored facts with the fixture's blocks and the per-engine
    figures they rest on: a whole ``ReportFacts`` block to validate."""
    sample = json.loads(SAMPLE_REPORT.read_text())["facts"]
    fixture = copy.deepcopy(_fragment(name)["facts"])
    template = sample["engines"][0]
    engines = [{**template, **e} for e in fixture["engines"]]
    return {
        **sample,
        "engines": engines,
        "critical_high_risks_by_engine": fixture["critical_high_risks_by_engine"],
        "no_migration": fixture["no_migration"],
        "slice_plan": fixture["slice_plan"],
        "adoption": fixture["adoption"],
    }


@pytest.mark.parametrize("name", CASES)
def test_the_report_facts_validate_with_the_fixture_s_blocks(name: str) -> None:
    ReportFacts.model_validate(_report_facts(name))


FACTS_BREAKS: dict[str, tuple[str, Mutate, str]] = {
    "engine cost": (
        "wordpress",
        lambda f: f["engines"][1].update(monthly_cost_usd=1.0),
        "dynamodb's cost is 1.0",
    ),
    "engine risks": (
        "discourse",
        lambda f: f.update(critical_high_risks_by_engine={}),
        "risks differ from the facts'",
    ),
    "relational only engines": (
        "wordpress",
        lambda f: f["no_migration"].update(engines=["aurora_mysql"]),
        "relational_only ends on facts.no_migration's engines",
    ),
    "path against plan": (
        "discourse",
        lambda f: f["adoption"]["paths"][0].update(stage_count=5, gate_count=5),
        "stage_count is 5, the steps give 4",
    ),
}


@pytest.mark.parametrize("case", sorted(FACTS_BREAKS))
def test_report_facts_that_disagree_with_the_plan_are_refused(case: str) -> None:
    name, mutate, message = FACTS_BREAKS[case]
    facts = _report_facts(name)
    mutate(facts)
    with pytest.raises(ValidationError, match=message):
        ReportFacts.model_validate(facts)


def test_either_block_alone_is_not_cross_checked() -> None:
    # A report can carry one block before the other is computed.
    facts = _report_facts("discourse")
    facts["adoption"]["paths"][0]["stage_count"] = facts["adoption"]["paths"][0]["gate_count"] = 5
    ReportFacts.model_validate({**facts, "slice_plan": None})


# --- Every invariant fails when broken ---------------------------------------------------


def _unit(plan: dict[str, Any], key_part: str) -> dict[str, Any]:
    return next(u for u in plan["move_units"] if key_part in u["key"])


def _step(plan: dict[str, Any], n: int) -> dict[str, Any]:
    step: dict[str, Any] = plan["plan_steps"][n - 1]
    return step


def _stage(plan: dict[str, Any], label: str) -> dict[str, Any]:
    return next(
        s for step in plan["plan_steps"] for s in step["step_stages"] if s["label"] == label
    )


def _recount(plan: dict[str, Any]) -> None:
    """Every stage, step and phase figure summed again from the units, so a break reaches
    the invariant it targets rather than a count check first."""
    units = _units(plan)
    for step in plan["plan_steps"]:
        for stage in step["step_stages"]:
            members = [units[k] for k in stage["unit_keys"] if k in units]
            stage["unit_count"] = len(stage["unit_keys"])
            stage["query_count"] = sum(u["query_count"] for u in members)
            stage["owned_table_count"] = sum(u["owned_table_count"] for u in members)
            if stage["kind"] == "risk_held":
                stage["holding_risk_ids"] = sorted(
                    {r for u in members for r in u["holding_risk_ids"]}
                )
        for key in ("unit_count", "query_count", "owned_table_count"):
            step[key] = sum(s[key] for s in step["step_stages"])
        step["stage_count"] = len(step["step_stages"])
    plan["step_count"] = len(plan["plan_steps"])
    try:
        steps = [SliceStep.model_validate(s) for s in plan["plan_steps"]]
    except ValidationError:
        return
    plan["wave_phases"] = [p.model_dump(mode="json") for p in phase_facts(steps)]


def _move_query(plan: dict[str, Any], qid: str, src: str, dst: str) -> None:
    a, b = _unit(plan, src), _unit(plan, dst)
    a["query_ids"].remove(qid)
    if qid in a["write_query_ids"]:
        a["write_query_ids"].remove(qid)
    a["query_count"] -= 1
    b["query_ids"].append(qid)
    b["query_count"] += 1


def _add_dependency(plan: dict[str, Any], unit: str, on: str) -> None:
    plan["dependencies"].append(
        {"unit_key": unit, "depends_on": on, "kinds": ["shared_table"], "edge_weight": 0.1}
    )


def _transfer_table(plan: dict[str, Any], table: str, src: str, dst: str) -> None:
    a, b = _unit(plan, src), _unit(plan, dst)
    a["owned_tables"].remove(table)
    a["owned_table_count"] -= 1
    b["owned_tables"].append(table)
    b["owned_table_count"] += 1


POSTS, COMMENTS = "wp_postmeta", "wp_commentmeta"
SESSIONS, ACTIONS = "wp_woocommerce_sessions", "wp_actionscheduler_actions"
TAXONOMY, USERS = "wordpress.wp_term_taxonomy", "dynamodb:wordpress.wp_users"
CACHE, WHOLE = "elasticache:", "whole-database"
POSTS_KEY = "dynamodb:wordpress.wp_postmeta"


def _set(path: str, value: Any) -> Mutate:
    """Set a dotted path: steps by number (``s3``), stages by label (``@3b``), units by key
    part (``u:wp_postmeta``), list indexes as digits, anything else a dict key."""

    def mutate(plan: dict[str, Any]) -> None:
        node: Any = plan
        parts = path.split(".")
        for part in parts[:-1]:
            node = _resolve(plan, node, part)
        last = parts[-1]
        if isinstance(node, list):
            node[int(last)] = value
        else:
            node[last] = value

    return mutate


def _resolve(plan: dict[str, Any], node: Any, part: str) -> Any:
    if part.startswith("s") and part[1:].isdigit():
        return _step(plan, int(part[1:]))
    if part.startswith("@"):
        return _stage(plan, part[1:])
    if part.startswith("u:"):
        return _unit(plan, part[2:])
    if isinstance(node, list):
        return node[int(part)]
    return node[part]


def _do(*mutations: Mutate) -> Mutate:
    def mutate(plan: dict[str, Any]) -> None:
        for m in mutations:
            m(plan)

    return mutate


def _owned_and_replicated(plan: dict[str, Any]) -> None:
    _unit(plan, POSTS)["replicated_in"].append(
        {"table": "wordpress.wp_posts", "source_of_truth": "aurora_mysql"}
    )


def _engine_waves_basis(plan: dict[str, Any]) -> None:
    plan["step_basis"], plan["slice_basis"] = "engine_waves", None
    for n in (2, 3):
        step = _step(plan, n)
        step["kind"], step["slice_key"], step["metrics"] = "engine_wave", None, None


# (fixture, mutation, recount, message). Every raise in the slice plan models.
PLAN_BREAKS: dict[str, tuple[str, Mutate, bool, str]] = {
    # ReverseReplica
    "reverse read count": (
        "wordpress",
        _set("u:wp_postmeta.reverse_replicas.0.read_count", 9),
        True,
        "read_count is 9",
    ),
    "reverse tables twice": (
        "wordpress",
        _set("u:wp_postmeta.reverse_replicas.0.tables", ["wordpress.wp_posts"] * 2),
        True,
        "reverse replica tables lists a value twice",
    ),
    # MoveUnit
    "query twice in a unit": (
        "wordpress",
        lambda p: _unit(p, POSTS)["query_ids"].append("q085"),
        False,
        "query_ids lists a value twice",
    ),
    "owned twice in a unit": (
        "wordpress",
        lambda p: _unit(p, POSTS)["owned_tables"].append("wordpress.wp_posts"),
        False,
        "owned_tables lists a value twice",
    ),
    "whole-database unit with ids": (
        "wordpress",
        _set("u:whole-database.query_ids", ["q001"]),
        False,
        "the whole-database unit lists no query ids",
    ),
    "unit query count": (
        "wordpress",
        _set("u:wp_postmeta.query_count", 2),
        False,
        "query_count is 2",
    ),
    "writes outside the unit": (
        "wordpress",
        lambda p: _unit(p, POSTS)["write_query_ids"].append("q001"),
        False,
        "write_query_ids must be a subset",
    ),
    "unit owned count": (
        "wordpress",
        _set("u:wp_postmeta.owned_table_count", 9),
        False,
        "owned_table_count is 9",
    ),
    "unit reverse reads": (
        "wordpress",
        _set("u:wp_postmeta.reverse_read_count", 9),
        False,
        "reverse_read_count must sum",
    ),
    "reverse copy of a table it does not own": (
        "wordpress",
        _set("u:wp_postmeta.reverse_replicas.0.tables", ["wordpress.wp_users"]),
        False,
        "copies only tables the unit owns",
    ),
    "replicated in twice": (
        "wordpress",
        lambda p: _unit(p, POSTS)["replicated_in"].append(_unit(p, POSTS)["replicated_in"][0]),
        False,
        "replicated_in lists a value twice",
    ),
    "owned and replicated in": (
        "wordpress",
        _owned_and_replicated,
        False,
        "owned or replicated in, never both",
    ),
    "replica reads unit owns": (
        "wordpress",
        _do(
            _set("u:wp_term_taxonomy.owned_tables", ["wordpress.wp_x"]),
            _set("u:wp_term_taxonomy.owned_table_count", 1),
        ),
        False,
        "a replica_reads unit owns no table",
    ),
    "cache held by a risk": (
        "wordpress",
        _set("u:elasticache:.holding_risk_ids", ["RISK-1"]),
        False,
        "only a moving unit is held by a risk",
    ),
    # KeptWholeGroup
    "write group with no table": (
        "wordpress",
        _set("kept_whole.2.table", None),
        False,
        "a write group, and only it, names its table",
    ),
    # UnitDependency
    "self dependency": (
        "wordpress",
        lambda p: _add_dependency(p, USERS, USERS),
        False,
        "does not depend on itself",
    ),
    "dependency kinds twice": (
        "wordpress",
        _set("dependencies.0.kinds", ["cache_fronts", "cache_fronts"]),
        False,
        "kinds lists a value twice",
    ),
    # SliceStage
    "stage units twice": (
        "wordpress",
        lambda p: _stage(p, "3a")["unit_keys"].append(_stage(p, "3a")["unit_keys"][0]),
        False,
        "unit_keys lists a value twice",
    ),
    "stage unit count": ("wordpress", _set("@3a.unit_count", 5), False, "unit_count is 5"),
    "gate parts": (
        "wordpress",
        _set("@3b.stage_gate_parts", [{"text": "other", "items": []}]),
        False,
        "stage_gate_parts must join back",
    ),
    "risks outside stage d": (
        "wordpress",
        _set("@3b.holding_risk_ids", ["RISK-1"]),
        False,
        "only a risk_held stage lists holding risks",
    ),
    # RelationalTableClasses
    "stays count": ("wordpress", _set("s1.table_classes.stays_count", 0), False, "stays_count"),
    "leaves count": ("wordpress", _set("s1.table_classes.leaves_count", 0), False, "leaves_count"),
    "replicated out count": (
        "wordpress",
        _set("s1.table_classes.replicated_out_count", 0),
        False,
        "replicated_out_count",
    ),
    "unread count": ("wordpress", _set("s1.table_classes.unread_count", 0), False, "unread_count"),
    "table in two classes": (
        "wordpress",
        lambda p: (
            _step(p, 1)["table_classes"]["stays"].append("wordpress.wp_users"),
            _step(p, 1)["table_classes"].update(stays_count=5),
        ),
        False,
        "exactly one class",
    ),
    # SliceStep
    "step stage count": ("wordpress", _set("s3.stage_count", 2), False, "stage_count is 2"),
    "step unit count": ("wordpress", _set("s3.unit_count", 2), False, "unit_count must sum"),
    "step query count": ("wordpress", _set("s3.query_count", 2), False, "query_count must sum the"),
    "step owned count": (
        "wordpress",
        _set("s3.owned_table_count", 2),
        False,
        "owned_table_count must sum the stages",
    ),
    "stage order": (
        "wordpress",
        lambda p: _step(p, 3)["step_stages"].reverse(),
        False,
        "stages run a, b, c, d",
    ),
    "stage label": ("wordpress", _set("@3b.label", "3a"), False, "labelled by its step and kind"),
    "relational step not first": (
        "wordpress",
        _set("s2.kind", "relational_step"),
        False,
        "the relational step is step 1",
    ),
    "whole database stage elsewhere": (
        "wordpress",
        _do(_set("@2a.kind", "whole_database"), _set("@2a.label", "2")),
        False,
        "only the relational step has a whole_database stage",
    ),
    "cache step shape": ("wordpress", _set("s2.kind", "slice"), False, "the cache step, and only"),
    "slice without key": ("wordpress", _set("s3.slice_key", None), False, "has a slice_key"),
    "metrics off a slice": (
        "wordpress",
        _set(
            "s2.metrics", _fragment("wordpress")["facts"]["slice_plan"]["plan_steps"][2]["metrics"]
        ),
        False,
        "only a slice step carries slice metrics",
    ),
    "table classes off the relational step": (
        "wordpress",
        _set("s2.table_classes", {}),
        False,
        "only the relational step carries table classes",
    ),
    # SlicePlanFacts
    "plan step count": ("wordpress", _set("step_count", 9), False, "step_count is 9"),
    "step numbers": (
        "wordpress",
        lambda p: (
            _step(p, 3).update(step_number=4),
            [s.update(label="4" + s["label"][1:]) for s in _step(p, 3)["step_stages"]],
        ),
        False,
        "numbered 1, 2",
    ),
    "slice basis": ("wordpress", _set("slice_basis", None), False, "slice_basis is set exactly"),
    "engine wave in a slices plan": (
        "wordpress",
        _do(_set("s3.kind", "engine_wave"), _set("s3.slice_key", None), _set("s3.metrics", None)),
        False,
        "a slices plan has no engine_wave step",
    ),
    "engine wave numbered apart from its wave": (
        "discourse",
        _engine_waves_basis,
        False,
        "an engine_waves step is numbered as its wave",
    ),
    "slice keys twice": (
        "discourse",
        _set("s3.slice_key", "dynamodb:activity"),
        False,
        "slice keys lists a value twice",
    ),
    "engine reintroduced": (
        "discourse",
        _do(
            _set("s3.engine", "dynamodb"),
            lambda p: [
                u.update(engine="dynamodb") for u in p["move_units"] if u["engine"] == "documentdb"
            ],
        ),
        True,
        "step 3 reintroduces dynamodb",
    ),
    "phases": (
        "wordpress",
        _set("wave_phases.2.query_count", 1),
        False,
        "wave_phases must roll the steps",
    ),
    "unit keys twice": (
        "wordpress",
        lambda p: p["move_units"].append(copy.deepcopy(_unit(p, POSTS))),
        False,
        "move unit keys are unique",
    ),
    "unit in no stage": (
        "wordpress",
        lambda p: _stage(p, "3a")["unit_keys"].pop(),
        True,
        "units in no stage",
    ),
    "dependency on an unknown unit": (
        "wordpress",
        lambda p: _add_dependency(p, USERS, "dynamodb:nothing"),
        False,
        "dependency names unknown unit",
    ),
    "dependency on a later stage": (
        "wordpress",
        lambda p: _add_dependency(p, USERS, "dynamodb:wordpress.wp_postmeta"),
        False,
        "does not run before it",
    ),
    "dependency on one run after it in the same stage": (
        "wordpress",
        lambda p: _add_dependency(
            p, "dynamodb:wordpress.wp_commentmeta", "dynamodb:wordpress.wp_postmeta"
        ),
        False,
        "does not run before it",
    ),
    "dependency cycle in a stage": (
        "wordpress",
        _do(
            lambda p: _add_dependency(p, "dynamodb:wordpress.wp_wc_customer_lookup", POSTS_KEY),
            lambda p: _add_dependency(p, POSTS_KEY, "dynamodb:wordpress.wp_wc_customer_lookup"),
        ),
        False,
        "does not run before it",
    ),
    "engine costs": (
        "wordpress",
        lambda p: p["engine_monthly_cost_usd"].pop("dynamodb"),
        False,
        "engine_monthly_cost_usd names exactly",
    ),
    "engine risks": (
        "wordpress",
        lambda p: p["engine_critical_high_risks"].update(opensearch=0),
        False,
        "engine_critical_high_risks names exactly",
    ),
    # _place_units
    "stage names an unknown unit": (
        "wordpress",
        lambda p: _stage(p, "3a")["unit_keys"].append("dynamodb:nothing"),
        True,
        "names unknown unit",
    ),
    "unit in two stages": (
        "wordpress",
        lambda p: _stage(p, "3b")["unit_keys"].append(_stage(p, "3a")["unit_keys"][0]),
        True,
        "more than one stage",
    ),
    "unit on another engine": (
        "wordpress",
        _set("u:wp_woocommerce_sessions.engine", "documentdb"),
        False,
        "not dynamodb",
    ),
    "unit kind in its stage": (
        "wordpress",
        _do(
            _set("u:wp_woocommerce_sessions.kind", "read_model"),
            _set("u:wp_woocommerce_sessions.owned_tables", []),
            _set("u:wp_woocommerce_sessions.owned_table_count", 0),
            lambda p: _unit(p, ACTIONS)["owned_tables"].append("wordpress.wp_woocommerce_sessions"),
            _set("u:wp_actionscheduler_actions.owned_table_count", 3),
        ),
        True,
        "a self_contained stage holds no read_model unit",
    ),
    "pilot not first": (
        "wordpress",
        _do(_set("u:wp_term_taxonomy.pilot", False), _set("u:wp_users.pilot", True)),
        False,
        "the pilot is the first unit",
    ),
    "stage query count": (
        "wordpress",
        _do(
            _set("@3c.query_count", 32),
            _set("s3.query_count", 41),
            _set("wave_phases.2.query_count", 41),
        ),
        False,
        "stage 3c: query_count must sum",
    ),
    "stage owned count": (
        "wordpress",
        _do(_set("@3c.owned_table_count", 13), _set("s3.owned_table_count", 16)),
        False,
        "stage 3c: owned_table_count must sum",
    ),
    "stage risks are not its units'": (
        "discourse",
        _set("@2d.holding_risk_ids", ["RISK-008"]),
        False,
        "holding_risk_ids are its units' risks",
    ),
    "two pilots": (
        "wordpress",
        _set(f"u:{SESSIONS}.pilot", True),
        False,
        "the pilot is the first unit",
    ),
    "two pilots in one stage": (
        "wordpress",
        lambda p: _stage(p, "3a")["unit_keys"].reverse() or _set("u:wp_users.pilot", True)(p),
        False,
        "the pilot is the first unit",
    ),
    "slice step with no pilot": (
        "wordpress",
        _set("u:wp_term_taxonomy.pilot", False),
        False,
        "slice step 3 names no pilot",
    ),
    # _check_stage_shape
    "risk outside stage d": (
        "wordpress",
        _set(f"u:{COMMENTS}.holding_risk_ids", ["RISK-001"]),
        False,
        "a risk holds its unit in stage d",
    ),
    "replica stage unit owns": (
        "wordpress",
        _do(
            _set("u:wp_term_taxonomy.kind", "owner"),
            _set("u:wp_term_taxonomy.owned_tables", ["wordpress.wp_x"]),
            _set("u:wp_term_taxonomy.owned_table_count", 1),
            _set("s1.table_classes", None),
        ),
        True,
        "a replica_reads stage holds no owner unit",
    ),
    "self-contained with a replica": (
        "wordpress",
        _set(
            f"u:{SESSIONS}.replicated_in",
            [{"table": "wordpress.wp_users", "source_of_truth": "aurora_mysql"}],
        ),
        False,
        "not self-contained",
    ),
    "owner stage unit owns nothing": (
        "wordpress",
        _do(
            _set(f"u:{SESSIONS}.owned_tables", []),
            _set(f"u:{SESSIONS}.owned_table_count", 0),
            _set("s1.table_classes", None),
            lambda p: p["kept_whole"].clear()
            or [u.update(kept_whole_ids=[]) for u in p["move_units"]],
        ),
        True,
        "owns no table",
    ),
    "reverse stage without a replica": (
        "wordpress",
        _do(
            _set(f"u:{COMMENTS}.replicated_in", []),
            _set(f"u:{COMMENTS}.reverse_replicas", []),
            _set(f"u:{COMMENTS}.reverse_read_count", 0),
        ),
        False,
        "needs no replica, so it is self-contained",
    ),
    # _check_queries_and_groups, _query_holders
    "cached read twice": (
        "wordpress",
        lambda p: p["move_units"].append(
            {**copy.deepcopy(_unit(p, CACHE)), "key": "elasticache:wordpress.wp_users"}
        )
        or _stage(p, "2a")["unit_keys"].append("elasticache:wordpress.wp_users"),
        True,
        "a cached read is in two cache units",
    ),
    "group ids twice": (
        "wordpress",
        lambda p: p["kept_whole"].append(copy.deepcopy(p["kept_whole"][0])),
        False,
        "kept_whole group ids lists a value twice",
    ),
    "unknown group": (
        "wordpress",
        lambda p: _unit(p, POSTS)["kept_whole_ids"].append("co_dependency:99"),
        False,
        "names unknown groups",
    ),
    "split write group": (
        "wordpress",
        lambda p: _move_query(p, "q094", POSTS, COMMENTS),
        True,
        "split across units",
    ),
    "unclaimed group": (
        "wordpress",
        lambda p: _unit(p, POSTS)["kept_whole_ids"].remove("co_dependency:2"),
        False,
        "is held by",
    ),
    "query in two units": (
        "wordpress",
        lambda p: (
            _unit(p, SESSIONS)["query_ids"].append("q077"),
            _unit(p, SESSIONS).update(query_count=3),
        ),
        True,
        "is in units",
    ),
    # _check_one_writer (ADR-031 Amendment 9)
    "two units own one table": (
        "wordpress",
        lambda p: (
            _unit(p, SESSIONS)["owned_tables"].append("wordpress.wp_comments"),
            _unit(p, SESSIONS).update(owned_table_count=2),
        ),
        True,
        "table wordpress.wp_comments is owned by units",
    ),
    "owns a table without its write group": (
        "wordpress",
        _do(
            lambda p: _transfer_table(p, "wordpress.wp_posts", POSTS, COMMENTS),
            lambda p: [
                t.update(stage_label="3c")
                for t in _step(p, 1)["table_classes"]["leaves"]
                if t["table"] == "wordpress.wp_posts"
            ],
            lambda p: _unit(p, POSTS)["reverse_replicas"][0].update(
                tables=["wordpress.wp_postmeta"]
            ),
        ),
        True,
        "moves wordpress.wp_posts's writes but does not own it",
    ),
    "writes move while the table stays": (
        "wordpress",
        _do(
            lambda p: _unit(p, POSTS)["owned_tables"].remove("wordpress.wp_posts"),
            _set(f"u:{POSTS}.owned_table_count", 2),
            _set(f"u:{POSTS}.reverse_replicas.0.tables", ["wordpress.wp_postmeta"]),
            _set("s1.table_classes", None),
        ),
        True,
        "moves wordpress.wp_posts's writes but does not own it",
    ),
    "owns a table whose write group stays": (
        "wordpress",
        _do(
            lambda p: _unit(p, POSTS)["owned_tables"].append("wordpress.wp_options"),
            _set(f"u:{POSTS}.owned_table_count", 4),
            _set("s1.table_classes", None),
        ),
        True,
        "owns wordpress.wp_options without its write group",
    ),
    # _check_table_classes
    "classes miss a table": (
        "wordpress",
        lambda p: (
            _step(p, 1)["table_classes"]["unread"].pop(),
            _step(p, 1)["table_classes"].update(unread_count=27),
        ),
        False,
        "sort exactly the tables the relational step carries",
    ),
    "leaving stage": (
        "wordpress",
        lambda p: next(
            t for t in _step(p, 1)["table_classes"]["leaves"] if t["table"] == "wordpress.wp_posts"
        ).update(stage_label="3b"),
        False,
        "leaves must be every table",
    ),
    "replicated out": (
        "wordpress",
        lambda p: (
            _step(p, 1)["table_classes"]["replicated_out"].remove("wordpress.wp_users"),
            _step(p, 1)["table_classes"]["stays"].append("wordpress.wp_users"),
            _step(p, 1)["table_classes"].update(replicated_out_count=2, stays_count=5),
        ),
        False,
        "replicated_out must be the relational tables",
    ),
}


@pytest.mark.parametrize("case", sorted(PLAN_BREAKS))
def test_a_broken_plan_is_refused(case: str) -> None:
    name, mutate, recount, message = PLAN_BREAKS[case]
    plan = copy.deepcopy(_plan(_fragment(name)))
    mutate(plan)
    if recount:
        _recount(plan)
    with pytest.raises(ValidationError, match=message):
        SlicePlanFacts.model_validate(plan)


def test_the_cache_and_the_relational_step_do_not_split_a_group() -> None:
    # The cache fronts reads that still run on their owner, and the relational step
    # carries everything: a group with a cached member and members that stay is whole.
    plan = copy.deepcopy(_plan(_fragment("wordpress")))
    plan["kept_whole"].append(
        {"group_id": "co_dependency:3", "kind": "co_dependency", "query_ids": ["q001", "q050"]}
    )
    SlicePlanFacts.model_validate(plan)


def test_a_moving_unit_holds_a_whole_group_even_when_it_alone_names_it() -> None:
    plan = copy.deepcopy(_plan(_fragment("wordpress")))
    plan["kept_whole"].append(
        {"group_id": "co_dependency:3", "kind": "co_dependency", "query_ids": ["q075", "q050"]}
    )
    _unit(plan, SESSIONS)["kept_whole_ids"].append("co_dependency:3")
    with pytest.raises(ValidationError, match="moves part of group co_dependency:3"):
        SlicePlanFacts.model_validate(plan)


def test_a_single_unit_slice_names_no_pilot() -> None:
    plan = copy.deepcopy(_plan(_fragment("discourse")))
    step = _step(plan, 3)
    step["step_stages"] = [_stage(plan, "3b")]
    themes = _stage(plan, "3b")
    dropped = themes["unit_keys"].pop()
    plan["move_units"] = [
        u for u in plan["move_units"] if u["key"] not in (dropped, "documentdb:public.categories")
    ]
    plan["dependencies"] = [
        d
        for d in plan["dependencies"]
        if d["unit_key"] not in (dropped, "documentdb:public.categories")
    ]
    plan["kept_whole"] = [
        g for g in plan["kept_whole"] if g["group_id"] != "write:public.user_options"
    ]
    classes = _step(plan, 1)["table_classes"]
    classes["leaves"] = [t for t in classes["leaves"] if t["table"] != "public.user_options"]
    classes["leaves_count"] = len(classes["leaves"])
    classes["replicated_out"].remove("public.categories")
    classes["stays"] += ["public.categories", "public.user_options"]
    classes["replicated_out_count"], classes["stays_count"] = 5, len(classes["stays"])
    _recount(plan)
    assert _step(plan, 3)["unit_count"] == 1
    SlicePlanFacts.model_validate(plan)


# --- Adoption paths ---------------------------------------------------------------------


ADOPTION_BREAKS: dict[str, tuple[str, Mutate, str]] = {
    "path count": ("wordpress", lambda a: a.update(path_count=3), "path_count is 3"),
    "path ids twice": (
        "wordpress",
        lambda a: a["paths"].append(copy.deepcopy(a["paths"][1])) or a.update(path_count=3),
        "path ids lists a value twice",
    ),
    "full target not last": (
        "wordpress",
        lambda a: a["paths"].reverse(),
        "full_target goes last",
    ),
    "relational only not first": (
        "wordpress",
        lambda a: a["paths"][1].update(path_id="through_step_3")
        or a.update(default_path_id="through_step_3")
        or a["paths"].reverse(),
        "relational_only goes first",
    ),
    "stops out of order": (
        "discourse",
        lambda a: a["paths"][0].update(stops_after_step=3, step_count=3, path_id="through_step_3"),
        "listed by the step they stop after",
    ),
    "default with no paths": (
        "wordpress",
        lambda a: a.update(paths=[], path_count=0),
        "a default path is named exactly when paths exist",
    ),
    "default not offered": (
        "discourse",
        lambda a: a.update(default_path_id="relational_only"),
        "default path is an offered",
    ),
    "offered and not offered": (
        "discourse",
        lambda a: a["not_offered"][0].update(path_id="full_target"),
        "offered or not offered, not both",
    ),
    "closure not offered": (
        "discourse",
        lambda a: a["not_offered"][0].update(offered_instead="through_step_9"),
        "closure through_step_9 is not offered",
    ),
    "unknown path id": (
        "wordpress",
        lambda a: a["paths"][0].update(path_id="cache_first"),
        "path id 'cache_first' is not",
    ),
    "unknown not offered id": (
        "discourse",
        lambda a: a["not_offered"][0].update(path_id="quickest_win"),
        "path id 'quickest_win' is not",
    ),
    "through step names another step": (
        "discourse",
        lambda a: a["paths"][0].update(path_id="through_step_9"),
        "through_step_9 stops after step 9",
    ),
    "path steps": ("wordpress", lambda a: a["paths"][0].update(step_count=1), "takes steps 1 to"),
    "queries by an engine off the path": (
        "wordpress",
        lambda a: a["paths"][0]["queries_by_engine"].update(dynamodb=1),
        "names only the path's engines",
    ),
    "replicated count": (
        "wordpress",
        lambda a: a["paths"][1].update(replicated_table_count=1),
        "replicated_table_count is 1",
    ),
    "replicated unsorted": (
        "wordpress",
        lambda a: a["paths"][1]["replicated_tables"].reverse(),
        "sorted, each once",
    ),
    "failing count": (
        "discourse",
        lambda a: a["not_offered"][0].update(failing_query_count=3),
        "failing_query_count is 3",
    ),
    "one-step check passed wrongly": (
        "wordpress",
        lambda a: a["one_step"]["one_step_checks"][2].update(passed=True),
        "observed is at most limit",
    ),
    "one-step rule twice": (
        "wordpress",
        lambda a: a["one_step"]["one_step_checks"].append(a["one_step"]["one_step_checks"][0]),
        "rules lists a value twice",
    ),
    "one-step rule missing": (
        "wordpress",
        lambda a: a["one_step"]["one_step_checks"].pop(),
        "states every rule once",
    ),
    "one-step suits with a failed check": (
        "wordpress",
        lambda a: a["one_step"].update(suits_one_step=True),
        "every check passed",
    ),
}


@pytest.mark.parametrize("case", sorted(ADOPTION_BREAKS))
def test_broken_adoption_facts_are_refused(case: str) -> None:
    name, mutate, message = ADOPTION_BREAKS[case]
    adoption = copy.deepcopy(_fragment(name)["facts"]["adoption"])
    mutate(adoption)
    with pytest.raises(ValidationError, match=message):
        AdoptionFacts.model_validate(adoption)


def _path(a: dict[str, Any], pid: str) -> dict[str, Any]:
    path: dict[str, Any] = next(p for p in a["paths"] if p["path_id"] == pid)
    return path


# (fixture, mutate the adoption facts, mutate the plan, message): checked against the plan.
PlanMutate = Callable[[SlicePlanFacts], SlicePlanFacts]
CROSS_BREAKS: dict[str, tuple[str, Mutate | None, PlanMutate | None, str]] = {
    "stops beyond the plan": (
        "wordpress",
        lambda a: _path(a, "full_target").update(stops_after_step=4, step_count=4),
        None,
        "stops after a step the plan does not have",
    ),
    "relational only stops late": (
        "wordpress",
        lambda a: a["paths"].pop()
        and a.update(path_count=1, default_path_id="relational_only")
        and None
        or _path(a, "relational_only").update(stops_after_step=3, step_count=3),
        None,
        "relational_only stops after the steps that move no query",
    ),
    "full target stops early": (
        "discourse",
        lambda a: a["paths"].pop(0)
        and None
        or a.update(path_count=1, not_offered=[])
        or _path(a, "full_target").update(stops_after_step=2, step_count=2),
        None,
        "full_target takes every step",
    ),
    "through step outside the menu": (
        "wordpress",
        lambda a: _path(a, "relational_only").update(path_id="through_step_2")
        or a.update(default_path_id="through_step_2"),
        None,
        "through_step_2 must stop between relational_only and full_target",
    ),
    "queries moved": (
        "wordpress",
        lambda a: _path(a, "full_target").update(queries_moved=39),
        None,
        "queries_moved is 39, the steps give 40",
    ),
    "queries by engine": (
        "wordpress",
        lambda a: _path(a, "full_target")["queries_by_engine"].update(aurora_mysql=66),
        None,
        "queries_by_engine is",
    ),
    "tables migrate": (
        "discourse",
        lambda a: _path(a, "through_step_2").update(tables_migrate=8),
        None,
        "tables_migrate is 8, the steps give 5",
    ),
    "cost": (
        "wordpress",
        lambda a: _path(a, "full_target").update(monthly_cost_usd=500.0),
        None,
        "monthly_cost_usd is 500.0",
    ),
    "cost delta": (
        "discourse",
        lambda a: _path(a, "full_target").update(cost_delta_vs_relational_usd=1.0),
        None,
        "cost_delta_vs_relational_usd is 1.0",
    ),
    "risks": (
        "discourse",
        lambda a: _path(a, "through_step_2").update(critical_high_risks=0),
        None,
        "critical_high_risks is 0, the steps give 1",
    ),
    "replicated tables": (
        "discourse",
        lambda a: _path(a, "through_step_2").update(
            replicated_tables=["public.users"], replicated_table_count=1
        ),
        None,
        "replicated_tables is",
    ),
    "temporary streams": (
        "wordpress",
        lambda a: _path(a, "full_target").update(temporary_stream_count=1),
        None,
        "temporary_stream_count is 1, the steps give 3",
    ),
    "engines": (
        "wordpress",
        lambda a: _path(a, "full_target").update(
            engines=["aurora_mysql", "dynamodb"],
            monthly_cost_usd=327.99,
        ),
        None,
        "engines is",
    ),
    "gate count": (
        "wordpress",
        lambda a: _path(a, "full_target").update(gate_count=4),
        None,
        "every stage has one gate",
    ),
    "first gate": (
        "wordpress",
        lambda a: _path(a, "full_target").update(first_gate="another gate"),
        None,
        "first_gate is its first stage's gate",
    ),
    "a path engine with no cost": (
        "wordpress",
        None,
        # The plan's own validator refuses the gap; a model built past it still fails here.
        lambda p: p.model_copy(update={"engine_monthly_cost_usd": {}}),
        "the plan states no cost or risks for",
    ),
    "one-step counts": (
        "wordpress",
        lambda a: a["one_step"].update(staged_stage_count=4),
        None,
        "states the plan's step, stage and gate counts",
    ),
    "one-step steps": (
        "wordpress",
        lambda a: a["one_step"].update(staged_step_count=2),
        None,
        "states the plan's step, stage and gate counts",
    ),
    "one-step streams": (
        "wordpress",
        lambda a: a["one_step"].update(staged_temporary_stream_count=1),
        None,
        "the plan's stage c streams",
    ),
    "one-step observed": (
        "discourse",
        lambda a: a["one_step"]["one_step_checks"][1].update(observed=3),
        None,
        "the one-step checks state the plan's own figures",
    ),
}


@pytest.mark.parametrize("case", sorted(CROSS_BREAKS))
def test_paths_that_disagree_with_the_plan_are_refused(case: str) -> None:
    name, on_adoption, on_plan, message = CROSS_BREAKS[case]
    facts = copy.deepcopy(_fragment(name)["facts"])
    if on_adoption:
        on_adoption(facts["adoption"])
        # The capped view follows the edited menu, so each break reaches its own check.
        ids = [p["path_id"] for p in facts["adoption"]["paths"]]
        facts["adoption"]["summary_path_ids"] = summary_path_ids(ids)
    plan = SlicePlanFacts.model_validate(facts["slice_plan"])
    if on_plan:
        plan = on_plan(plan)
    with pytest.raises(ValueError, match=message):
        check_adoption_against_plan(AdoptionFacts.model_validate(facts["adoption"]), plan)


def test_the_empty_structures_validate() -> None:
    plan = SlicePlanFacts(step_basis="engine_waves")
    adoption = AdoptionFacts()
    check_adoption_against_plan(adoption, plan)
    with pytest.raises(ValidationError, match="slice_basis is set exactly"):
        SlicePlanFacts(step_basis="slices")


@pytest.mark.passes_on_main  # guards history #1130 already wrote
def test_the_slice_plan_minor_has_its_version_history_entry() -> None:
    """The one MINOR the slice plan added (#1129) is in the Version History, and the
    history runs on to the version the model writes, with no gap."""
    import re

    from src.contracts import synthesis_output
    from src.contracts.synthesis_output import SynthesisOutputContract
    from src.shared.contract_versioning import current_version

    doc = synthesis_output.__doc__ or ""
    entries = re.findall(r"^- (\d+)\.(\d+) \(([^)]*)\)", doc, re.MULTILINE)
    versions = [(int(a), int(b)) for a, b, _ in entries]
    assert versions == sorted(versions)
    assert f"{versions[-1][0]}.{versions[-1][1]}" == current_version(SynthesisOutputContract)
    chunks = re.split(r"^(?=- \d+\.\d+ \()", doc, flags=re.MULTILINE)
    (slice_entry,) = [c for c in chunks if "#1129" in c]
    assert "facts.slice_plan" in slice_entry and "Backward" in slice_entry
