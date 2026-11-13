# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Each adoption path is priced for the queries it places on each engine (#2170).

A path is the slice plan stopped after a step (#1130): a query whose step the path does
not take stays on the relational engine. Synthesis passes the facts layer a pricer that
runs the TCO's own sizing (``build_tco_analysis`` with a placement, the same estimators
and prices as the full target); each path states its engines' sized estimates
(``engine_costs``), its cost is their sum, and ``cost_basis`` is ``per_path_sizing``.
The full target places nothing, so its figures are today's per-engine estimates.

The plan is ``slice_plan_jobs``' (see ``test_adoption_paths``): Aurora MySQL keeps q01,
q02 and q09, step 2 is the cache, step 3 moves DocumentDB's q10 and q11, step 4
DynamoDB's eight. The fake pricer below charges each engine its full-target cost plus $5
for each query a path places on it beyond the full target's (and $5 less for each it
takes away), so every expected figure is counted by hand.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from src.contracts.synthesis_output import (
    ADOPTION_COST_BASIS_NOTES,
    AdoptionFacts,
    SlicePlanFacts,
    SynthesisOutputContract,
    check_adoption_against_plan,
)
from src.shared.adoption_facts import adoption_facts
from src.shared.slice_plan import build_slice_plan
from tests.design_records import skip_undesigned
from tests.unit.agents.referee import slice_plan_jobs as job

MOVED_LATER = ["q03", "q04", "q05", "q06", "q07", "q08", "q10", "q11", "q12", "q13"]
DYNAMODB = ["q03", "q04", "q05", "q06", "q07", "q08", "q12", "q13"]


class FakePricer:
    """Each engine's cost for a placement, with the ``sizing`` the TCO row would state."""

    def __init__(self, full_target_off_by: float = 0.0) -> None:
        self.calls: list[dict[str, str]] = []
        self.off_by = full_target_off_by

    def __call__(self, placed: Mapping[str, str]) -> dict[str, dict[str, Any]]:
        self.calls.append(dict(placed))
        engine_of = {q: e for q, (e, *_) in job.QUERIES.items()}
        full = {e: sum(1 for x in engine_of.values() if x == e) for e in job.COSTS}
        engine_of.update(placed)
        out = {}
        for engine, cost in job.COSTS.items():
            n = sum(1 for x in engine_of.values() if x == engine)
            monthly = max(cost + 5.0 * (n - full[engine]), 0.0)
            if not placed:
                monthly += self.off_by
            out[engine] = {
                "engine": engine,
                "monthly_cost_usd": monthly,
                "sizing": {
                    "basis": "cache_overlay" if engine == "elasticache" else "assigned_queries",
                    "sized_query_count": 2 if engine == "elasticache" else n,
                    "calls_per_second": 0.0,
                    "read_calls_per_second": 0.0,
                    "write_calls_per_second": 0.0,
                    "sized_table_count": 0,
                    "storage_gb": 0.0,
                },
            }
        return out


def _plan(**overrides: Any) -> dict[str, Any]:
    plan = build_slice_plan(
        job.REPORT, job.waves(), job.inputs(**overrides), job.COSTS, {"dynamodb": 1}, []
    )
    assert plan is not None
    return plan


def _adoption(
    price: FakePricer | None = None,
    report: dict[str, Any] | None = None,
    plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return adoption_facts(
        report or job.REPORT, plan or _plan(), job.inputs(), price=price or FakePricer()
    )


def _paths(adoption: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {p["path_id"]: p for p in adoption["paths"]}


def _costs(path: dict[str, Any]) -> dict[str, float]:
    return {c["engine"]: c["monthly_cost_usd"] for c in path["engine_costs"]}


def test_each_path_is_priced_for_the_queries_it_leaves_on_the_relational_engine() -> None:
    price = FakePricer()
    _adoption(price)
    placements = {tuple(sorted(c)): set(c.values()) for c in price.calls}
    # relational_only leaves every later step's query on Aurora MySQL, through_step_3
    # DynamoDB's eight, and the full target places nothing.
    assert set(placements) == {tuple(MOVED_LATER), tuple(DYNAMODB), ()}
    assert placements[tuple(MOVED_LATER)] == {"aurora_mysql"}
    assert placements[tuple(DYNAMODB)] == {"aurora_mysql"}


def test_a_path_s_cost_is_its_own_engines_sized_estimates() -> None:
    adoption = _adoption()
    assert adoption["cost_basis"] == "per_path_sizing"
    assert adoption["cost_basis_note"] == ADOPTION_COST_BASIS_NOTES["per_path_sizing"]
    paths = _paths(adoption)
    # Aurora MySQL takes back the ten queries the later steps move: $100 + 10 x $5.
    assert _costs(paths["relational_only"]) == {"aurora_mysql": 150.0, "elasticache": 50.0}
    assert paths["relational_only"]["monthly_cost_usd"] == 200.0
    # Through step 3: DocumentDB has its two, Aurora MySQL DynamoDB's eight.
    assert _costs(paths["through_step_3"]) == {
        "aurora_mysql": 140.0,
        "elasticache": 50.0,
        "documentdb": 30.0,
    }
    assert paths["through_step_3"]["monthly_cost_usd"] == 220.0
    # The sizing each engine was priced on is stated with its figure.
    sized = {c["engine"]: c["sizing"] for c in paths["relational_only"]["engine_costs"]}
    assert sized["aurora_mysql"]["sized_query_count"] == 13
    assert {p["cost_is_upper_bound"] for p in adoption["paths"]} == {False}
    AdoptionFacts.model_validate(adoption)
    check_adoption_against_plan(
        AdoptionFacts.model_validate(adoption), SlicePlanFacts.model_validate(_plan())
    )


def test_the_full_target_is_today_s_per_engine_estimates() -> None:
    full = _paths(_adoption())["full_target"]
    assert _costs(full) == job.COSTS
    assert full["monthly_cost_usd"] == round(sum(job.COSTS.values()), 2) == 200.0
    # A pricer whose full target is not the headline's estimates is refused.
    with pytest.raises(ValueError, match="full target"):
        _adoption(FakePricer(full_target_off_by=0.01))


def test_the_cost_differences_are_measured_from_the_relational_move_only() -> None:
    report = {
        **job.REPORT,
        "tco_analysis": {"current_monthly_cost": 120.0, "current_cost_known": True},
    }
    adoption = _adoption(report=report)
    assert adoption["relational_monthly_cost_usd"] == 200.0
    deltas = {
        k: (p["cost_delta_vs_relational_usd"], p["cost_delta_vs_current_usd"])
        for k, p in _paths(adoption).items()
    }
    assert deltas == {
        "relational_only": (0.0, 80.0),
        "through_step_3": (20.0, 100.0),
        "full_target": (0.0, 80.0),
    }


def test_a_tie_between_paths_ranks_nothing() -> None:
    # relational_only and the full target both cost $200: the full target stays the
    # default and the paths stay in step order.
    adoption = _adoption()
    paths = _paths(adoption)
    assert paths["relational_only"]["monthly_cost_usd"] == paths["full_target"]["monthly_cost_usd"]
    assert adoption["default_path_id"] == "full_target"
    assert [p["path_id"] for p in adoption["paths"]] == [
        "relational_only",
        "through_step_3",
        "full_target",
    ]


def test_a_relational_move_with_no_purpose_built_engine_is_priced_as_it_stands() -> None:
    queries = tuple(q for q in job.inputs().queries if q.engine == "aurora_mysql")
    plan = _plan(
        queries=queries,
        writes_by_table={"app.orders": ("q02",)},
        co_dependency_groups=(),
        foreign_keys=(),
    )
    price = FakePricer()
    adoption = adoption_facts(job.REPORT, plan, job.inputs(), price=price)
    (path,) = adoption["paths"]
    assert path["path_id"] == "relational_only" and price.calls == [{}]
    # No query is fronted here, so there is no cache step: Aurora MySQL alone.
    assert _costs(path) == {"aurora_mysql": 100.0}
    assert adoption["cost_basis"] == "per_path_sizing"
    assert adoption["relational_monthly_cost_usd"] == path["monthly_cost_usd"] == 100.0


def test_an_engine_a_path_empties_is_stated_at_its_sized_figure() -> None:
    # Every DynamoDB query stays on Aurora MySQL through step 3: DynamoDB is not one of
    # that path's engines, so its emptied figure is in no path's sum.
    paths = _paths(_adoption())
    assert "dynamodb" not in _costs(paths["through_step_3"])
    # An engine priced at $0 is a valid figure, not a gap.
    adoption = _adoption()
    adoption["paths"][0]["engine_costs"][1]["monthly_cost_usd"] = 0.0
    adoption["paths"][0]["monthly_cost_usd"] = 150.0
    adoption["relational_monthly_cost_usd"] = 150.0
    for p in adoption["paths"]:
        p["cost_delta_vs_relational_usd"] = round(p["monthly_cost_usd"] - 150.0, 2)
    AdoptionFacts.model_validate(adoption)


def _refused(adoption: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        check_adoption_against_plan(
            AdoptionFacts.model_validate(adoption), SlicePlanFacts.model_validate(_plan())
        )


def test_sized_figures_that_do_not_add_up_are_refused() -> None:
    adoption = _adoption()
    # A cost that is not its engines' sum.
    broken = json.loads(json.dumps(adoption))
    broken["paths"][1]["monthly_cost_usd"] += 1.0
    _refused(broken, "monthly_cost_usd")
    # An engine the path does not end on, even at $0 (the sum still adds up).
    broken = json.loads(json.dumps(adoption))
    extra = {**broken["paths"][0]["engine_costs"][0], "engine": "dynamodb", "monthly_cost_usd": 0}
    broken["paths"][0]["engine_costs"].append(extra)
    _refused(broken, "prices each of the path's engines once")
    # A per-path basis with a path priced on no sizing of its own.
    broken = json.loads(json.dumps(adoption))
    broken["paths"][1]["engine_costs"] = []
    _refused(broken, "engine_costs")
    # A difference not measured from the relational move only.
    broken = json.loads(json.dumps(adoption))
    broken["paths"][1]["cost_delta_vs_relational_usd"] = 30.0
    _refused(broken, "cost_delta_vs_relational_usd")
    # relational_only not at the relational move only's cost, every difference still
    # taken from that (wrong) figure.
    broken = json.loads(json.dumps(adoption))
    broken["relational_monthly_cost_usd"] += 1.0
    for p in broken["paths"]:
        p["cost_delta_vs_relational_usd"] = round(
            p["monthly_cost_usd"] - broken["relational_monthly_cost_usd"], 2
        )
    _refused(broken, "relational_only costs relational_monthly_cost_usd")
    # The full target not at today's estimates.
    broken = json.loads(json.dumps(adoption))
    full = broken["paths"][-1]
    full["engine_costs"][0]["monthly_cost_usd"] += 1.0
    full["monthly_cost_usd"] += 1.0
    full["cost_delta_vs_relational_usd"] += 1.0
    _refused(broken, "full target")


def test_without_a_pricer_no_path_claims_a_sizing_of_its_own() -> None:
    # A caller with no TCO to price from (a unit view, a report filled at read time)
    # keeps the full-target basis, and says so.
    adoption = adoption_facts(job.REPORT, _plan(), job.inputs())
    assert adoption["cost_basis"] == "full_target_estimates"
    assert adoption["relational_monthly_cost_usd"] is None
    assert {len(p["engine_costs"]) for p in adoption["paths"]} == {0}


OLD_REPORT = (
    Path(__file__).resolve().parents[1] / "report" / "fixtures" / "wordpress_pre_2170_report.json"
)


@pytest.mark.passes_on_main  # main wrote this file; the guard is that it still loads
def test_a_report_written_before_per_path_sizing_still_loads() -> None:
    report = json.loads(OLD_REPORT.read_text(encoding="utf-8"))
    adoption = report["facts"]["adoption"]
    assert adoption["cost_basis"] == "full_target_estimates"
    loaded = SynthesisOutputContract.model_validate(report)
    facts = loaded.model_dump(mode="json")["facts"]["adoption"]
    assert facts["cost_basis"] == "full_target_estimates"
    assert [p["path_id"] for p in facts["paths"]] == ["relational_only", "full_target"]
    assert {len(p.get("engine_costs") or []) for p in facts["paths"]} == {0}
    assert facts.get("relational_monthly_cost_usd") is None


def test_synthesis_prices_each_path_with_the_headline_s_own_sizing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    import src.agents.referee.synthesis_handler as handler
    import src.shared.report_facts as report_facts_module
    from tests.unit.agents.referee.test_synthesis_unservable_capabilities import (
        DB,
        JOB,
    )
    from tests.unit.agents.referee.test_synthesis_unservable_capabilities import (
        store as seeded_store,
    )

    handed: list[Any] = []
    compute = report_facts_module.compute_report_facts

    def spy(*args: Any, **kwargs: Any) -> dict[str, Any]:
        handed.append(kwargs.get("path_pricer"))
        return compute(*args, **kwargs)

    monkeypatch.setattr(report_facts_module, "compute_report_facts", spy)
    from src.tools.analysis.dynamodb_analysis_tools import estimate_dynamodb_costs

    store = seeded_store.__wrapped__(tmp_path)  # type: ignore[attr-defined]
    # DynamoDB's analysis priced the whole collection with its own estimator, so the TCO
    # re-sizes it (an estimate with no components keeps its figure).
    key = f"{DB}/{JOB}/analysis-dynamodb/analysis.json"
    collector = store.read_json(f"{DB}/{JOB}/collector/output.json")
    analysis = store.read_json(key)
    analysis["cost_estimate"] = estimate_dynamodb_costs(collector).model_dump(mode="json")
    store.write_json(key, analysis)
    skip_undesigned(store, DB, JOB, 1)  # the schema design phase without a model (#2154)
    result = handler.run_synthesis_deterministic(JOB, DB, store, assignment_version=1)
    price = result["path_pricer"]
    assert handed and all(p is price for p in handed)
    # No placement: every engine's figure and sizing is its headline TCO row.
    rows = {r["database"]: r for r in result["tco_analysis"]["cost_breakdown"]}
    assert {e: (c["monthly_cost_usd"], c["sizing"]) for e, c in price({}).items()} == {
        e: (r["monthly_cost_usd"], r["sizing"]) for e, r in rows.items()
    }
    # Placing every query on one engine sizes it for all of them, and no query moves.
    assignment = result["data"].assignment
    before = json.dumps(assignment, sort_keys=True)
    in_scope = [qa for qa in assignment["query_assignments"] if qa.get("in_scope", True)]
    assert rows["dynamodb"]["sizing"]["sized_query_count"] == 1
    placed = price({qa["query_id"]: "dynamodb" for qa in in_scope})
    assert placed["dynamodb"]["sizing"]["sized_query_count"] == len(in_scope) == 2
    assert json.dumps(assignment, sort_keys=True) == before


def test_with_no_relational_step_no_relational_figure_is_stated() -> None:
    """#2170 review: with no relational engine the relational move only is not offered
    (``no_relational_engine``), so no "relational move only" cost or difference from it
    is stated, never a $0.00 one."""
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
    assert plan is not None
    price = FakePricer()
    adoption = adoption_facts(job.REPORT, plan, inputs, price=price)
    (full,) = adoption["paths"]
    assert adoption["not_offered"][0]["reason"] == "no_relational_engine"
    assert adoption["cost_basis"] == "per_path_sizing"
    assert adoption["relational_monthly_cost_usd"] is None
    assert _costs(full) == {"dynamodb": 20.0, "documentdb": 30.0}
    check_adoption_against_plan(
        AdoptionFacts.model_validate(adoption), SlicePlanFacts.model_validate(plan)
    )
    # A $0.00 relational move only is refused.
    stated = {
        **adoption,
        "relational_monthly_cost_usd": 0.0,
        "paths": [{**full, "cost_delta_vs_relational_usd": full["monthly_cost_usd"]}],
    }
    with pytest.raises(ValueError, match="relational step"):
        check_adoption_against_plan(
            AdoptionFacts.model_validate(stated), SlicePlanFacts.model_validate(plan)
        )


def test_with_a_relational_step_the_relational_figures_are_stated() -> None:
    adoption = _adoption()
    plan = SlicePlanFacts.model_validate(_plan())
    unstated = {**adoption, "relational_monthly_cost_usd": None}
    with pytest.raises(ValueError, match="exactly when the plan has a relational step"):
        check_adoption_against_plan(AdoptionFacts.model_validate(unstated), plan)


def _synthesis_with_a_design(gsi: bool, overlap: bool) -> tuple[Any, dict[str, Any]]:
    """Synthesis on a DynamoDB job whose design has a derived copy kept by a stream sync
    (``overlap``) and a GSI on that copy (``gsi``); DDB-AP-3 is the write both replay."""
    import copy

    from src.agents.referee.synthesis_handler import run_synthesis_deterministic
    from tests.unit.agents.referee import test_synthesis_derived_copy_sync as sync_job
    from tests.unit.agents.referee.test_synthesis_llm_seam import _COLLECTOR, _mock_store

    schema = sync_job._overlap_schema() if overlap else sync_job._without_overlap()
    if gsi:
        schema["table_definitions"][-1].update(
            {
                "partition_key": {"attribute_name": "user_id", "attribute_type": "S"},
                "attributes": [{"name": "user_id", "type": "S"}, {"name": "email", "type": "S"}],
                "item_count": 50000,
                "item_size_bytes": 2000,
                "gsis": [
                    {
                        "gsi_name": "ByEmail",
                        "partition_key": [{"attribute_name": "email", "attribute_type": "S"}],
                        "projection": "ALL",
                        "item_count": 50000,
                        "item_size_bytes": 2000,
                    }
                ],
            }
        )
    collector: dict[str, Any] = copy.deepcopy(_COLLECTOR)
    collector["queries"]["query_patterns"].append(
        {
            "query_id": "DDB-AP-3",
            "query_text": "INSERT INTO users (user_id) VALUES (?)",
            "query_type": "INSERT",
            "tables_accessed": ["users"],
            "calls_per_second": 3.0,
        }
    )
    assignment = {
        "job_id": sync_job.JOB,
        "version": 1,
        "query_assignments": [
            {"query_id": q, "assigned_engine": "dynamodb", "source_tables": ["users"]}
            for q in ("DDB-AP-1", "DDB-AP-2", "DDB-AP-3")
        ],
        "table_assignments": [],
    }
    store = _mock_store()
    read, exists = store.read_json.side_effect, store.exists.side_effect

    def read_json(path: str) -> Any:
        if "schema-dynamodb/" in path and path.endswith("schema_output.json"):
            return schema
        if path.endswith("collector/output.json"):
            return collector
        if path.endswith("assignment/v1/assignment.json"):
            return assignment
        return read(path)

    ours = ("assignment/v1/assignment.json", "schema-dynamodb/v1/schema_output.json")
    store.read_json.side_effect = read_json
    store.exists.side_effect = lambda path: path.endswith(ours) or exists(path)
    result = run_synthesis_deterministic(sync_job.JOB, sync_job.DB, store, assignment_version=1)
    return result, {"schema": schema, "collector": collector}


def test_a_path_s_stream_sync_and_gsi_costs_follow_its_placement() -> None:
    """#2170 review: a path that leaves DDB-AP-3 off DynamoDB sizes DynamoDB's GSI writes
    and the derived copy's stream sync for the queries it keeps there, as it does its
    reads and writes; at no placement they are the headline's."""
    from src.agents.referee.derived_copy_sync import syncs_by_engine
    from src.agents.referee.gsi_cost import gsi_costs_by_engine

    designed, job_data = _synthesis_with_a_design(gsi=True, overlap=True)
    plain, _ = _synthesis_with_a_design(gsi=False, overlap=False)
    risks = designed["risk_assessment"]["risks"]
    away = {"DDB-AP-3": "aurora_mysql"}

    def extras(ids: list[str]) -> float:
        designs = {"dynamodb": job_data["schema"]}
        query_ids = {"dynamodb": ids}
        gsi = gsi_costs_by_engine(designs, job_data["collector"], query_ids)["dynamodb"]
        sync = syncs_by_engine(designs, risks, job_data["collector"], query_ids)["dynamodb"]
        plain_gsi = gsi_costs_by_engine(
            {"dynamodb": plain["data"].engines["dynamodb"].schema_design},
            job_data["collector"],
            query_ids,
        )
        return float(
            gsi["total_monthly_cost_usd"]
            + sync["total_monthly_cost_usd"]
            - sum(g["total_monthly_cost_usd"] for g in plain_gsi.values())
        )

    every, kept = ["DDB-AP-1", "DDB-AP-2", "DDB-AP-3"], ["DDB-AP-1", "DDB-AP-2"]
    # The write the GSI and the sync replay is what the placement takes away.
    assert extras(kept) < extras(every)
    for placed, ids in (({}, every), (away, kept)):
        with_design = designed["path_pricer"](placed)["dynamodb"]["monthly_cost_usd"]
        without = plain["path_pricer"](placed)["dynamodb"]["monthly_cost_usd"]
        assert with_design == pytest.approx(without + extras(ids), abs=0.011), placed
