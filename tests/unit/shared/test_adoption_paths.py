# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The adoption paths (#1130): the slice plan stopped after each step, with every figure
its steps 1 to k give, the paths the relational engine's gates keep from being offered
(with their closure), and the one-step form of the full target.

The plan is the small hand-built job of ``slice_plan_jobs``: step 1 carries every query
to Aurora MySQL, step 2 fronts two reads with ElastiCache, step 3 moves DocumentDB's two
queries (stage 3c: it reads a copy of ``app.events``), and step 4 moves DynamoDB's eight
(stages 4a, 4b and 4c, two units with a reverse replica each). Every expected figure
below is counted by hand from that plan.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from src.agents.referee import capability_registry
from src.agents.referee.slice_inputs import relational_gate_failures, slice_inputs
from src.contracts.synthesis_output import (
    AdoptionFacts,
    SlicePlanFacts,
    check_adoption_against_plan,
    check_plan_against_facts,
)
from src.shared.adoption_facts import adoption_facts
from src.shared.slice_plan import build_slice_plan
from tests.unit.agents.referee import slice_plan_jobs as job

GATE = (
    "Full load complete and replication in progress from MySQL to Aurora MySQL for 8 "
    "tables. The cutover is your responsibility."
)


def _plan(risks: dict[str, int] | None = None, **overrides: Any) -> dict[str, Any]:
    waves = overrides.pop("waves", None) or job.waves()
    plan = build_slice_plan(
        job.REPORT, waves, job.inputs(**overrides), job.COSTS, risks or {"dynamodb": 1}, []
    )
    assert plan is not None
    return plan


def _adoption(
    plan: dict[str, Any] | None = None,
    gated: Mapping[str, tuple[str, ...]] | None = None,
    report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    inputs = job.inputs(relational_gate_failures=gated or {})
    return adoption_facts(report or job.REPORT, plan or _plan(), inputs)


def _paths(adoption: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {p["path_id"]: p for p in adoption["paths"]}


def test_the_paths_stop_after_each_step_with_the_full_target_the_default() -> None:
    adoption = _adoption()
    assert [(p["path_id"], p["stops_after_step"]) for p in adoption["paths"]] == [
        ("relational_only", 2),
        ("through_step_3", 3),
        ("full_target", 4),
    ]
    assert adoption["default_path_id"] == "full_target" and adoption["path_count"] == 3
    assert adoption["not_offered"] == []
    assert AdoptionFacts.model_validate(adoption).model_dump(mode="json") == adoption


def test_the_relational_only_path_moves_nothing() -> None:
    path = _paths(_adoption())["relational_only"]
    assert path["engines"] == ["aurora_mysql", "elasticache"]
    assert (path["step_count"], path["stage_count"], path["gate_count"]) == (2, 2, 2)
    assert path["queries_moved"] == 0 and path["queries_by_engine"] == {"aurora_mysql": 13}
    assert path["tables_migrate"] == 0
    assert path["monthly_cost_usd"] == 150.0 and path["cost_delta_vs_relational_usd"] == 0.0
    assert path["critical_high_risks"] == 0
    assert path["replicated_tables"] == [] and path["replicated_table_count"] == 0
    assert path["temporary_stream_count"] == 0
    assert path["first_gate"] == GATE


def test_a_path_through_a_step_states_steps_one_to_k() -> None:
    path = _paths(_adoption())["through_step_3"]
    assert path["engines"] == ["aurora_mysql", "elasticache", "documentdb"]
    assert (path["step_count"], path["stage_count"], path["gate_count"]) == (3, 3, 3)
    assert path["queries_moved"] == 2
    assert path["queries_by_engine"] == {"aurora_mysql": 11, "documentdb": 2}
    assert path["tables_migrate"] == 1
    assert path["monthly_cost_usd"] == 180.0 and path["cost_delta_vs_relational_usd"] == 30.0
    # DynamoDB's open high risk is on a step this path does not take.
    assert path["critical_high_risks"] == 0
    # DocumentDB reads a copy of app.events, which nothing writes off Aurora yet: a copy,
    # but no reverse stream (stage 3c's unit owns no table another engine reads).
    assert path["replicated_tables"] == ["app.events"] and path["temporary_stream_count"] == 0
    assert path["first_gate"] == GATE


def test_the_full_target_takes_every_step() -> None:
    path = _paths(_adoption())["full_target"]
    assert path["engines"] == ["aurora_mysql", "elasticache", "documentdb", "dynamodb"]
    assert (path["step_count"], path["stage_count"], path["gate_count"]) == (4, 6, 6)
    assert path["queries_moved"] == 10
    assert path["queries_by_engine"] == {"aurora_mysql": 3, "documentdb": 2, "dynamodb": 8}
    assert path["tables_migrate"] == 5
    assert path["monthly_cost_usd"] == 200.0 and path["cost_delta_vs_relational_usd"] == 50.0
    assert path["critical_high_risks"] == 1
    assert path["replicated_tables"] == ["app.carts", "app.events", "app.products"]
    assert path["replicated_table_count"] == 3
    # Stage 4c: app.carts copied back to Aurora, app.events copied to DocumentDB.
    assert path["temporary_stream_count"] == 2


def test_the_cost_difference_from_today_needs_a_known_current_cost() -> None:
    unknown = _paths(_adoption())
    assert {p["cost_delta_vs_current_usd"] for p in unknown.values()} == {None}
    report = {
        **job.REPORT,
        "tco_analysis": {"current_monthly_cost": 120.0, "current_cost_known": True},
    }
    known = _paths(_adoption(report=report))
    assert {k: p["cost_delta_vs_current_usd"] for k, p in known.items()} == {
        "relational_only": 30.0,
        "through_step_3": 60.0,
        "full_target": 80.0,
    }
    placeholder = {**job.REPORT, "tco_analysis": {"current_monthly_cost": 120.0}}
    placeholder["tco_analysis"]["current_cost_known"] = False
    assert {p["cost_delta_vs_current_usd"] for p in _adoption(report=placeholder)["paths"]} == {
        None
    }


def test_no_path_claims_its_cost_is_an_upper_bound() -> None:
    # Each engine's estimate is sized for the queries it serves in the full target, so a
    # path that leaves more queries on Aurora is not bounded by it.
    assert {p["cost_is_upper_bound"] for p in _adoption()["paths"]} == {False}


def test_a_path_that_leaves_a_gated_query_on_aurora_is_not_offered() -> None:
    # q10 moves to DocumentDB in step 3, and Aurora MySQL could not take it back.
    adoption = _adoption(gated={"aurora_mysql": ("q10",)})
    assert [p["path_id"] for p in adoption["paths"]] == ["through_step_3", "full_target"]
    assert adoption["not_offered"] == [
        {
            "path_id": "relational_only",
            "reason": "fails_relational_gates",
            "failing_query_ids": ["q10"],
            "failing_query_count": 1,
            "serving_engines": ["documentdb"],
            "offered_instead": "through_step_3",
        }
    ]
    assert adoption["default_path_id"] == "full_target"


def test_the_closure_is_the_first_path_that_moves_every_gated_query() -> None:
    # q12 moves in step 4 and q10 in step 3: only the full target moves both.
    adoption = _adoption(gated={"aurora_mysql": ("q12", "q10")})
    assert [p["path_id"] for p in adoption["paths"]] == ["full_target"]
    by_id = {n["path_id"]: n for n in adoption["not_offered"]}
    assert by_id["relational_only"]["failing_query_ids"] == ["q10", "q12"]
    assert by_id["relational_only"]["serving_engines"] == ["documentdb", "dynamodb"]
    assert by_id["through_step_3"]["failing_query_ids"] == ["q12"]
    assert by_id["through_step_3"]["serving_engines"] == ["dynamodb"]
    assert {n["offered_instead"] for n in adoption["not_offered"]} == {"full_target"}


def test_only_the_plan_s_relational_engine_gates_a_path() -> None:
    # A gate on another Aurora engine, or on a query that stays on Aurora, holds nothing.
    for gated in ({"aurora_postgresql": ("q10", "q12")}, {"aurora_mysql": ("q01", "q99")}):
        adoption = _adoption(gated=gated)
        assert adoption["not_offered"] == [] and adoption["path_count"] == 3


def test_the_one_step_form_states_the_plan_s_counts_and_checks() -> None:
    one = _adoption()["one_step"]
    assert (one["step_count"], one["staged_step_count"], one["staged_stage_count"]) == (1, 4, 6)
    assert one["gate_count"] == 6
    assert one["temporary_stream_count"] == 0 and one["staged_temporary_stream_count"] == 2
    assert {
        c["rule"]: (c["observed"], c["limit"], c["passed"]) for c in one["one_step_checks"]
    } == {
        "no_open_critical_high_risk": (1, 0, False),
        "at_most_one_purpose_built_target": (2, 1, False),
        "no_reverse_replica_stage": (2, 0, False),
    }
    assert one["suits_one_step"] is False


def _sessions_only() -> dict[str, Any]:
    """Aurora keeps app.orders; DynamoDB takes the self-contained app.sessions only."""
    queries = tuple(q for q in job.inputs().queries if q.query_id in {"q01", "q02", "q12", "q13"})
    writes = {t: w for t, w in job.WRITE_GROUPS.items() if t in ("app.orders", "app.sessions")}
    return _plan(
        risks={"dynamodb": 0},
        queries=queries,
        writes_by_table=writes,
        co_dependency_groups=(),
        foreign_keys=(),
    )


def test_one_target_with_no_risk_and_no_stage_c_suits_one_step() -> None:
    plan = _sessions_only()
    adoption = adoption_facts(job.REPORT, plan, job.inputs())
    one = adoption["one_step"]
    assert [c["passed"] for c in one["one_step_checks"]] == [True, True, True]
    assert [c["observed"] for c in one["one_step_checks"]] == [0, 1, 0]
    assert one["suits_one_step"] is True
    assert [p["path_id"] for p in adoption["paths"]] == ["relational_only", "full_target"]


def test_a_plan_that_moves_no_query_offers_the_relational_move_only() -> None:
    queries = tuple(q for q in job.inputs().queries if q.engine == "aurora_mysql")
    plan = _plan(
        queries=queries,
        writes_by_table={"app.orders": ("q02",)},
        co_dependency_groups=(),
        foreign_keys=(),
    )
    adoption = adoption_facts(job.REPORT, plan, job.inputs())
    assert [p["path_id"] for p in adoption["paths"]] == ["relational_only"]
    assert adoption["default_path_id"] == "relational_only" and adoption["one_step"] is None


def test_a_plan_with_no_relational_step_offers_only_the_full_target() -> None:
    # A source with no Aurora engine chosen: one engine wave per step, nothing to stay on.
    queries = tuple(q for q in job.inputs().queries if q.query_id in {"q10", "q12", "q13"})
    inputs = job.inputs(
        queries=queries,
        writes_by_table={"app.sessions": ("q13", "q12"), "app.profiles": ("q10",)},
        co_dependency_groups=(),
        foreign_keys=(),
    )
    waves = [
        {"wave": 1, "engines": ["dynamodb"], "tables": ["app.sessions"]},
        {"wave": 2, "engines": ["documentdb"], "tables": ["app.profiles"]},
    ]
    plan = build_slice_plan(job.REPORT, waves, inputs, job.COSTS, {}, [])
    assert plan is not None and plan["plan_steps"][0]["kind"] == "engine_wave"
    adoption = adoption_facts(job.REPORT, plan, inputs)
    assert [p["path_id"] for p in adoption["paths"]] == ["full_target"]
    full = adoption["paths"][0]
    assert full["queries_by_engine"] == {"dynamodb": 2, "documentdb": 1}
    assert adoption["not_offered"] == [
        {
            "path_id": "relational_only",
            "reason": "no_relational_engine",
            "failing_query_ids": [],
            "failing_query_count": 0,
            "serving_engines": [],
            "offered_instead": "full_target",
        }
    ]


def test_every_figure_is_the_plan_s_own_so_a_wrong_one_is_refused() -> None:
    plan = SlicePlanFacts.model_validate(_plan())
    adoption = AdoptionFacts.model_validate(_adoption())
    check_adoption_against_plan(adoption, plan)
    for field, value in (
        ("queries_moved", 9),
        ("tables_migrate", 4),
        ("monthly_cost_usd", 199.0),
        ("critical_high_risks", 0),
        ("temporary_stream_count", 1),
    ):
        broken = adoption.model_copy(deep=True)
        setattr(broken.paths[-1], field, value)
        with pytest.raises(ValueError, match=field):
            check_adoption_against_plan(broken, plan)


def test_relational_only_on_a_cross_engine_source_ends_on_aurora_and_the_cache() -> None:
    plan = SlicePlanFacts.model_validate(_plan())
    adoption = AdoptionFacts.model_validate(_adoption())
    costs: dict[str, float | None] = dict(job.COSTS)
    risks = {"dynamodb": 1}
    # Homogeneous: Aurora is retained, so it is one of facts.no_migration's engines.
    check_plan_against_facts(plan, adoption, costs, risks, ["aurora_mysql", "elasticache"])
    # Cross-engine: the move to Aurora is a migration, so no_migration has the cache only,
    # and relational_only ends on Aurora too.
    check_plan_against_facts(
        plan, adoption, costs, risks, ["elasticache"], {"aurora_mysql", "dynamodb"}
    )
    # Aurora named nowhere: the path's engines are not the report's.
    with pytest.raises(ValueError, match="no_migration"):
        check_plan_against_facts(plan, adoption, costs, risks, ["elasticache"], {"dynamodb"})


def test_the_facts_layer_passes_the_gate_answer_through_the_slice_inputs() -> None:
    inputs = slice_inputs(
        {
            "query_assignments": [
                {"query_id": "q1", "assigned_engine": "aurora_mysql", "source_tables": ["t"]},
            ]
        },
        {},
        gate_failures={"aurora_mysql": ["q9", "q3", "q9"]},
    )
    assert inputs.relational_gate_failures == {"aurora_mysql": ("q3", "q9")}
    assert slice_inputs({}, {}).relational_gate_failures == {}


def _gate_job() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    assignment = {
        "query_assignments": [
            {"query_id": "q1", "assigned_engine": "aurora_mysql", "source_tables": ["app.t"]},
            {"query_id": "q2", "assigned_engine": "documentdb", "source_tables": ["app.t"]},
            {"query_id": "q3", "assigned_engine": "dynamodb", "source_tables": ["app.u"]},
            {
                "query_id": "q4",
                "assigned_engine": "dynamodb",
                "source_tables": ["app.u"],
                "in_scope": False,
            },
        ]
    }
    collector = {
        "source_database": {"engine": "mysql"},
        "queries": {
            "query_patterns": [
                {"query_id": "q1", "query_text": "SELECT * FROM app.t WHERE id = ?"},
                {"query_id": "q2", "query_text": "SELECT COUNT(*) FROM app.t GROUP BY kind"},
                {"query_id": "q3", "query_text": "SELECT * FROM app.u WHERE id = ?"},
                {"query_id": "q4", "query_text": "SELECT COUNT(*) FROM app.u GROUP BY kind"},
            ]
        },
    }
    triage = {"query_capabilities": {"q2": ["aggregation"], "q4": ["aggregation"]}}
    return assignment, collector, triage


def test_the_customer_gate_names_the_queries_aurora_could_not_take_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assignment, collector, triage = _gate_job()
    # Every Aurora engine serves every capability today, so no query is gated.
    assert relational_gate_failures(assignment, collector, {}, triage) == {}
    # An Aurora MySQL without aggregation could not take q2 back; the out-of-scope q4 and
    # the queries that need nothing it lacks are not named.
    caps = capability_registry.ENGINE_CAPABILITIES
    monkeypatch.setitem(caps, "aurora_mysql", set(caps["aurora_mysql"]) - {"aggregation"})
    assert relational_gate_failures(assignment, collector, {}, triage) == {"aurora_mysql": ("q2",)}
    assert relational_gate_failures({}, collector, {}, triage) == {}


def test_the_facts_layer_hands_the_slice_inputs_to_the_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.shared.report_facts as report_facts_module
    from src.shared.report_facts import compute_report_facts

    seen: list[Any] = []

    def spy(report: Any, plan: Any, inputs: Any = None) -> dict[str, Any]:
        seen.append(inputs)
        return adoption_facts(report, plan, inputs)

    monkeypatch.setattr(report_facts_module, "adoption_facts", spy)
    inputs = job.inputs(relational_gate_failures={"aurora_mysql": ("q10",)})
    report = {**job.REPORT, "migration_waves": job.waves()}
    compute_report_facts(report, slice_inputs=inputs)
    assert seen == [inputs]


def test_synthesis_asks_the_customer_gate_and_passes_its_answer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    import src.agents.referee.synthesis_handler as handler
    from tests.unit.agents.referee.test_synthesis_unservable_capabilities import (
        DB,
        JOB,
    )
    from tests.unit.agents.referee.test_synthesis_unservable_capabilities import (
        store as seeded_store,
    )

    asked: list[tuple[Any, ...]] = []
    answer: dict[str, tuple[str, ...]] = {"aurora_mysql": ("q1",)}

    def gate(*args: Any) -> dict[str, tuple[str, ...]]:
        asked.append(args)
        return answer

    built: list[Any] = []

    def inputs(*args: Any, **kwargs: Any) -> Any:
        built.append(kwargs.get("gate_failures"))
        return slice_inputs(*args, **kwargs)

    monkeypatch.setattr(handler, "relational_gate_failures", gate)
    monkeypatch.setattr(handler, "slice_inputs", inputs)
    store = seeded_store.__wrapped__(tmp_path)  # type: ignore[attr-defined]
    handler.run_synthesis_deterministic(JOB, DB, store, assignment_version=1)
    assert built == [answer]
    ((assignment, collector, analyses, triage),) = asked
    assert assignment["query_assignments"] and collector["queries"]
    assert set(analyses) == {"dynamodb"} and "selected_agents" in triage
