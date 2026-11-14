# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The slice plan (#1129, #1698): steps, stages a to d in order, gates, numbering."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from src.contracts.synthesis_output import SlicePlanFacts
from src.shared.adoption_facts import slice_plan_facts
from src.shared.move_units import Unit
from src.shared.slice_plan import build_slice_plan, stage_kind, unit_order_key
from tests.unit.agents.referee import slice_plan_jobs as job


def _plan(**kwargs: Any) -> dict[str, Any] | None:
    waves: Sequence[Mapping[str, Any]] = kwargs.pop("waves", None) or job.waves()
    risks = kwargs.pop("risks", [])
    costs = kwargs.pop("costs", job.COSTS)
    return build_slice_plan(job.REPORT, waves, job.inputs(**kwargs), costs, {}, risks)


def _unit(**fields: Any) -> Unit:
    base: dict[str, Any] = {
        "key": "e:t",
        "engine": "e",
        "kind": "owner",
        "query_ids": ["q1"],
        "write_query_ids": [],
        "owned_tables": ["t"],
        "read_tables": ["t"],
        "replicated_in": [],
        "reverse_replicas": [],
        "kept_whole_ids": [],
        "calls_per_second": 1.0,
    }
    base.update(fields)
    return Unit(**base)


def test_a_unit_lands_in_the_first_stage_that_applies() -> None:
    copy = [("s", "aurora_mysql")]
    reverse = [("aurora_mysql", ["t"], ["q9"])]
    assert stage_kind(_unit(holding_risk_ids=["RISK-1"], owned_tables=[])) == "risk_held"
    assert stage_kind(_unit(holding_risk_ids=["RISK-1"])) == "risk_held"
    assert stage_kind(_unit(owned_tables=[], kind="replica_reads", replicated_in=copy)) == (
        "replica_reads"
    )
    assert stage_kind(_unit()) == "self_contained"
    assert stage_kind(_unit(replicated_in=copy)) == "with_reverse_replica"
    assert stage_kind(_unit(reverse_replicas=reverse)) == "with_reverse_replica"


def test_inside_a_stage_units_run_by_blast_radius_then_call_share_then_key() -> None:
    small = _unit(key="e:z", query_ids=["q1"], calls_per_second=1.0)
    busy = _unit(key="e:y", query_ids=["q2", "q3"], calls_per_second=9.0)
    quiet = _unit(key="e:x", query_ids=["q4", "q5"], calls_per_second=2.0)
    twin = _unit(key="e:w", query_ids=["q6", "q7"], calls_per_second=2.0)
    ordered = sorted([quiet, busy, small, twin], key=unit_order_key)
    assert [u.key for u in ordered] == ["e:z", "e:y", "e:w", "e:x"]


def test_the_plan_s_steps_follow_the_waves_and_its_stages_run_a_b_c_d() -> None:
    plan = _plan()
    assert plan is not None
    SlicePlanFacts.model_validate(plan)
    assert plan["step_basis"] == "slices" and plan["slice_basis"] == "community"
    steps = plan["plan_steps"]
    assert [(s["step_number"], s["kind"], s["engine"], s["phase_wave"]) for s in steps] == [
        (1, "relational_step", "aurora_mysql", 1),
        (2, "cache", "elasticache", 2),
        # Wave 3: DynamoDB's slice has more value against its cut cost (67.3% of calls
        # over 1 + 3 copies + 5 writes, against DocumentDB's 4.1% over 1 + 1 + 1), but
        # DocumentDB reads its app.events, so it is a hub (betweenness 0.67) and runs last.
        (3, "slice", "documentdb", 3),
        (4, "slice", "dynamodb", 3),
    ]
    assert steps[3]["metrics"]["hub_score"] == 0.67
    assert steps[3]["metrics"]["value_call_share_percent"] == 67.3
    assert steps[2]["metrics"]["hub_score"] == 0.0
    ddb = steps[3]
    assert [(st["label"], st["unit_keys"]) for st in ddb["step_stages"]] == [
        ("4a", ["dynamodb:app.products"]),
        ("4b", ["dynamodb:app.sessions"]),
        # Blast radius 3 (two queries, one reverse read) before 4.
        ("4c", ["dynamodb:app.events", "dynamodb:app.cart_items"]),
    ]
    assert ddb["slice_key"] == "dynamodb:standalone" and ddb["new_engine"] is True
    units = {u["key"]: u for u in plan["move_units"]}
    assert [k for k, u in units.items() if u["pilot"]] == ["dynamodb:app.products"]
    assert ddb["metrics"]["cut_cost"] == {
        "replica_table_count": 3,
        "write_site_count": 5,
        "transactions_split": None,
    }
    assert [p["step_numbers"] for p in plan["wave_phases"]] == [[1], [2], [3, 4]]
    assert plan["wave_phases"][2]["engines"] == ["documentdb", "dynamodb"]
    assert plan["wave_phases"][0]["query_count"] == len(job.QUERIES)


def test_the_relational_step_sorts_every_carried_table_once() -> None:
    plan = _plan()
    assert plan is not None
    classes = plan["plan_steps"][0]["table_classes"]
    assert classes["stays"] == ["app.orders"]
    assert classes["leaves"] == [
        {"table": "app.cart_items", "stage_label": "4c"},
        {"table": "app.carts", "stage_label": "4c"},
        {"table": "app.events", "stage_label": "4c"},
        {"table": "app.profiles", "stage_label": "3c"},
        {"table": "app.sessions", "stage_label": "4b"},
    ]
    assert classes["replicated_out"] == ["app.products"]
    assert classes["unread"] == ["app.unused"]


def test_each_dependency_points_at_a_unit_that_runs_earlier() -> None:
    plan = _plan()
    assert plan is not None
    edges = {(d["unit_key"], d["depends_on"]): d["kinds"] for d in plan["dependencies"]}
    # DocumentDB runs first and reads a copy of app.events, whose writes DynamoDB takes
    # later: from DynamoDB's side, a reverse replica.
    assert edges[("dynamodb:app.events", "documentdb:app.profiles")] == ["reverse_replica"]
    assert edges[("dynamodb:app.cart_items", "aurora_mysql:whole-database")] == ["reverse_replica"]
    assert edges[("elasticache:app.cart_items", "aurora_mysql:whole-database")] == ["cache_fronts"]
    assert edges[("dynamodb:app.products", "aurora_mysql:whole-database")] == ["replica_read"]


def test_the_gates_are_filled_templates() -> None:
    plan = _plan()
    assert plan is not None
    gates = {st["label"]: st["gate"] for s in plan["plan_steps"] for st in s["step_stages"]}
    assert gates == {
        "1": (
            "Full load complete and replication in progress from MySQL to Aurora MySQL for "
            "8 tables. The cutover is your responsibility."
        ),
        "2a": "Cache hit rate and invalidation verified against Aurora MySQL for the 2 cached reads.",
        "4a": (
            "CDC from Aurora MySQL to DynamoDB is within the reads' staleness tolerance, and "
            "the read returns the same rows on DynamoDB."
        ),
        "4b": (
            "Backfill complete and query parity confirmed for 1 table (app.sessions); no "
            "other engine reads it."
        ),
        "4c": (
            "Backfill complete and query parity confirmed for 3 tables (app.cart_items, "
            "app.carts, app.events); CDC from DynamoDB to Aurora MySQL and DocumentDB carries "
            "the 2 reads that stay there, within their tolerance."
        ),
        "3c": (
            "Backfill complete and query parity confirmed for 1 table (app.profiles); CDC "
            "from DynamoDB to DocumentDB is within the reads' staleness tolerance."
        ),
    }


def test_a_risk_holds_its_unit_in_stage_d() -> None:
    risks = [{"risk_id": "RISK-007", "engine": "dynamodb", "query_ids": ["q12"]}]
    plan = _plan(risks=risks)
    assert plan is not None
    stages = {st["label"]: st for st in plan["plan_steps"][3]["step_stages"]}
    assert "4b" not in stages
    assert stages["4d"]["unit_keys"] == ["dynamodb:app.sessions"]
    assert stages["4d"]["holding_risk_ids"] == ["RISK-007"]
    assert stages["4d"]["gate"].startswith("RISK-007 closed; then backfill complete")


def test_another_source_follows_the_migration_waves_one_step_each() -> None:
    # A cross-engine source is not sliced (#1702): each wave is one step, numbered as it.
    one_engine = job.waves(homogeneity="cross_engine")
    one_engine[2]["engines"] = ["dynamodb"]
    plan = _plan(
        waves=one_engine,
        queries=tuple(q for q in job.inputs().queries if q.engine != "documentdb"),
    )
    assert plan is not None
    assert plan["step_basis"] == "engine_waves" and plan["slice_basis"] is None
    assert [(s["step_number"], s["kind"], s["phase_wave"]) for s in plan["plan_steps"]] == [
        (1, "relational_step", 1),
        (2, "cache", 2),
        (3, "engine_wave", 3),
    ]
    assert plan["plan_steps"][0]["movement"] == "heterogeneous"
    assert plan["plan_steps"][2]["metrics"] is None


def test_engine_waves_that_cannot_be_one_step_each_state_no_plan() -> None:
    # Wave 3 holds two engines, so it cannot be one step numbered as the wave.
    assert _plan(waves=job.waves(homogeneity="cross_engine")) is None


@pytest.mark.parametrize("missing", ["dynamodb", "elasticache"])
def test_an_engine_with_no_cost_states_no_plan(missing: str) -> None:
    assert _plan(costs={**job.COSTS, missing: None}) is None


def test_an_engine_no_wave_names_states_no_plan() -> None:
    waves = job.waves()
    waves[2]["engines"] = ["dynamodb"]
    assert _plan(waves=waves) is None


def test_the_facts_layer_states_the_plan_only_with_its_inputs() -> None:
    report = {**job.REPORT, "migration_waves": job.waves()}
    empty = slice_plan_facts(report)
    assert empty["plan_steps"] == [] and empty["step_basis"] == "engine_waves"
    plan = slice_plan_facts(
        report, job.inputs(), engine_costs=job.COSTS, risks_by_engine={"dynamodb": 0}
    )
    assert plan == _plan()


def _gap_risk(design_ran: bool, severity: str = "HIGH") -> dict[str, Any]:
    """The coverage-gap risk synthesis writes for DynamoDB's q12, as ``report`` stores it."""
    from src.agents.referee.synthesis_report import _coverage_gap_risk

    risk = _coverage_gap_risk(
        "RISK-009",
        "dynamodb",
        {"q12": severity},
        {"q12": "SELECT * FROM app.sessions"},
        {"q12": {"app.sessions"}},
        design_ran,
    )
    return {**risk, "status": "open"}


def _plan_from_report_risks(risks: list[dict[str, Any]]) -> dict[str, Any]:
    from src.shared.adoption_facts import holding_risks

    report = {**job.REPORT, "risk_assessment": {"risks": risks}}
    plan = build_slice_plan(report, job.waves(), job.inputs(), job.COSTS, {}, holding_risks(report))
    assert plan is not None
    return plan


def _stage_labels(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        st["label"]: st
        for step in plan["plan_steps"]
        for st in step["step_stages"]
        if step.get("engine") == "dynamodb" or "dynamodb" in step.get("engines", [])
    }


@pytest.mark.passes_on_main  # guards behaviour #2163 already has
def test_a_schema_design_gap_holds_its_unit_when_schema_design_ran() -> None:
    # Open question 5 (#1129): the gap is a real risk only against a design that ran.
    plan = _plan_from_report_risks([_gap_risk(design_ran=True)])
    held = [st for st in _stage_labels(plan).values() if st["kind"] == "risk_held"]
    assert [st["unit_keys"] for st in held] == [["dynamodb:app.sessions"]]
    assert held[0]["label"].endswith("d")
    assert held[0]["holding_risk_ids"] == ["RISK-009"]


@pytest.mark.passes_on_main  # guards behaviour #2163 already has
def test_a_run_with_no_schema_design_leaves_stage_d_empty() -> None:
    # No design ran: the gap risk is LOW ("Not checked"), so it holds no unit.
    risk = _gap_risk(design_ran=False)
    assert risk["severity"] == "LOW" and "Not checked" in risk["description"]
    plan = _plan_from_report_risks([risk])
    assert not [st for st in _stage_labels(plan).values() if st["kind"] == "risk_held"]
    assert not [st for st in _stage_labels(plan).values() if st["label"].endswith("d")]
    assert all(
        "RISK-009" not in st["holding_risk_ids"]
        for step in plan["plan_steps"]
        for st in step["step_stages"]
    )
    # The same unit lands in its self-contained stage, as with no risk at all.
    assert plan == _plan_from_report_risks([])
