# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The slice plan and the adoption paths, computed once for ``facts`` (#1128, ADR-032).

``facts.slice_plan`` holds the steps, their ordered stages and move units, and the
dependency graph behind their order (#1129, #1698: ``src.shared.slice_plan``, from the inputs ``src.agents.referee.slice_inputs`` reads);
``facts.adoption`` holds the adoption paths and the one-step form of the full target
(#1130, #473). Both are computed deterministically from the effective assignment and the
report synthesis writes; a model may explain them, never decide them. Neither records a
Move or re-scores a query: a path is a projection of the effective assignment.

The slice plan needs the assignment and the collection (``SliceInputs``), which synthesis
passes. A report rebuilt without them (an older report filled at read time) states the
empty plan, as ``relational_gaps`` does without the collection, and so no adoption path.

An adoption path is the plan stopped after step k (#1130): ``relational_only`` stops after
the steps that move no query (the relational step and a cache step before any slice),
``through_step_<k>`` after each later step but the last, and ``full_target``, the default,
takes every step. Every figure a path states is its steps 1 to k's: engines, steps,
stages and gates, the queries and tables it moves off the relational engine, its engines'
``cost_breakdown`` estimates summed, their open critical and high risks, the tables it
reads through a copy and the reverse streams its stage c units need. A path that leaves
on the relational engine a query the customer gate refuses there
(``SliceInputs.relational_gate_failures``) is not offered; its closure, the first path that
moves every such query, is offered instead. The one-step form of the full target states
the plan's counts and the three "suits one step" checks, which describe the workload's
shape only: no figure here says how or when to cut over. ``summary_path_ids`` names the
paths a view capped at five rows shows.

Each path is priced for itself (#2170): synthesis passes a ``PathPricer``, the TCO's own
sizing (the same estimators and prices as the full target) run with the path's placement,
each query whose step the path does not take placed on the relational engine. A path's
``engine_costs`` are its engines' sized estimates and its cost their sum, and its
difference is measured from the relational move only priced the same way
(``relational_monthly_cost_usd``); ``cost_basis`` is ``per_path_sizing``. The full target
places nothing, so it is the headline's per-engine estimates. Without a pricer (a caller
with no TCO to price from) every cost is each engine's full-target estimate, stated as
``full_target_estimates``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING, Any

from src.contracts.synthesis_output import (
    ADOPTION_COST_BASIS_NOTES,
    MOVING_STEP_KINDS,
    MOVING_UNIT_KINDS,
    NO_MOVE_STEP_KINDS,
    AdoptionFacts,
    SlicePlanFacts,
    SliceStep,
    check_adoption_against_plan,
    summary_path_ids,
)
from src.shared.slice_plan import build_slice_plan
from src.shared.step_explanations import with_step_explanations

if TYPE_CHECKING:
    from src.shared.move_units import SliceInputs

__all__ = ["PathPricer", "adoption_facts", "holding_risks", "slice_plan_facts"]

# #2170: by engine, its estimate for a placement (query id -> the engine a path places it
# on instead of its assigned one): ``{"engine", "monthly_cost_usd", ...}``, an
# ``AdoptionEngineCost``. Synthesis builds it from the TCO's own sizing; an empty
# placement is the full target.
PathPricer = Callable[[Mapping[str, str]], Mapping[str, Mapping[str, Any]]]


def holding_risks(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The open critical and high risks that can hold a move unit (#1129), each with the
    ``engine`` its description names: the ones ``critical_high_risks_by_engine`` counts."""
    from src.shared.report_facts import open_risks, risk_engine_and_body, severity_of

    out = []
    for risk in open_risks(dict(report)):
        if severity_of(risk) in ("CRITICAL", "HIGH"):
            engine, _ = risk_engine_and_body(risk.get("description"))
            out.append({**risk, "engine": engine})
    return out


def slice_plan_facts(
    report: Mapping[str, Any],
    inputs: SliceInputs | None = None,
    *,
    waves: Iterable[Mapping[str, Any]] | None = None,
    engine_costs: Mapping[str, float | None] | None = None,
    risks_by_engine: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """``facts.slice_plan`` for ``report``: the plan ``build_slice_plan`` computes from
    ``inputs``, or, with no inputs or when no plan can be stated, the empty plan that
    follows the migration waves (``step_basis="engine_waves"``, no steps).

    ``waves`` defaults to ``report["migration_waves"]``; ``engine_costs`` and
    ``risks_by_engine`` default to the stored facts' (``facts.engines``' costs and
    ``critical_high_risks_by_engine``)."""
    empty = with_step_explanations(
        SlicePlanFacts(step_basis="engine_waves").model_dump(mode="json")
    )
    if inputs is None:
        return empty
    facts = report.get("facts") or {}
    if waves is None:
        waves = report.get("migration_waves") or []
    if engine_costs is None:
        engine_costs = {
            str(e.get("engine")): e.get("monthly_cost_usd")
            for e in facts.get("engines") or []
            if isinstance(e, Mapping)
        }
    if risks_by_engine is None:
        risks_by_engine = facts.get("critical_high_risks_by_engine") or {}
    plan = build_slice_plan(
        report,
        [w for w in waves if isinstance(w, Mapping)],
        inputs,
        engine_costs,
        risks_by_engine,
        holding_risks(report),
    )
    return plan if plan is not None else empty


def adoption_facts(
    report: Mapping[str, Any],
    slice_plan: Mapping[str, Any] | None,
    inputs: SliceInputs | None = None,
    price: PathPricer | None = None,
) -> dict[str, Any]:
    """``facts.adoption`` for ``report`` and its ``slice_plan``: the paths, those not
    offered and the one-step form (#1130, #473). ``inputs`` carries the customer gate's
    answer for the relational engine (``relational_gate_failures``); without it no query
    is gated. ``price`` prices each path for the queries it places on each engine
    (#2170); without it every path states the full target's estimates. A plan with no
    steps has no path."""
    plan = SlicePlanFacts.model_validate(slice_plan) if slice_plan is not None else None
    adoption = _adoption(report, plan, inputs, price) if plan is not None else AdoptionFacts()
    if plan is not None:
        check_adoption_against_plan(adoption, plan)
    return adoption.model_dump(mode="json")


def _adoption(
    report: Mapping[str, Any],
    plan: SlicePlanFacts,
    inputs: SliceInputs | None,
    price: PathPricer | None = None,
) -> AdoptionFacts:
    steps = plan.plan_steps
    if not steps:
        return AdoptionFacts()
    last = len(steps)
    relational = steps[0] if steps[0].kind == "relational_step" else None
    lead = 0
    while lead < last and steps[lead].kind in NO_MOVE_STEP_KINDS:
        lead += 1
    step_of, engine_of = _moving_queries(plan)
    gated = set(
        (inputs.relational_gate_failures.get(relational.engine, ()) if inputs else ())
        if relational is not None
        else ()
    )
    # Where each path could stop: after the steps that move no query, after each later
    # step, and after the last. With no relational engine nothing can stay behind, so
    # only the full target is a path.
    stops = [lead, *range(lead + 1, last)] if relational is not None else []
    if last > lead or relational is None:
        stops.append(last)
    priced = _Priced(plan, step_of, relational.engine if relational else None, price)
    paths, not_offered = [], []
    for stop in dict.fromkeys(stops):
        path_id = _path_id(stop, lead, last, relational is not None)
        failing = sorted(q for q, k in step_of.items() if k > stop and q in gated)
        if failing:
            closure = max(step_of[q] for q in failing)
            not_offered.append(
                {
                    "path_id": path_id,
                    "reason": "fails_relational_gates",
                    "failing_query_ids": failing,
                    "failing_query_count": len(failing),
                    "serving_engines": sorted({engine_of[q] for q in failing}),
                    "offered_instead": _path_id(closure, lead, last, True),
                }
            )
            continue
        paths.append(_path(report, plan, stop, path_id, priced))
    if relational is None:
        not_offered.insert(
            0,
            {
                "path_id": "relational_only",
                "reason": "no_relational_engine",
                "offered_instead": paths[-1]["path_id"] if paths else None,
            },
        )
    ids = [p["path_id"] for p in paths]
    default = "full_target" if "full_target" in ids else (ids[0] if ids else None)
    return AdoptionFacts.model_validate(
        {
            "default_path_id": default,
            "paths": paths,
            "path_count": len(paths),
            # #1133's capped table: the first and last paths and evenly spaced steps.
            "summary_path_ids": summary_path_ids(ids),
            # #2170: what every path's cost rests on, stated once.
            "cost_basis": priced.basis if paths else None,
            "cost_basis_note": ADOPTION_COST_BASIS_NOTES[priced.basis] if paths else None,
            "relational_monthly_cost_usd": (
                # #2170 review: no relational step, no relational move only to price.
                priced.relational_cost()
                if paths and price is not None and relational is not None
                else None
            ),
            "not_offered": not_offered,
            "one_step": _one_step(plan) if "full_target" in ids else None,
        }
    )


class _Priced:
    """Each engine's cost on a path (#2170): the pricer's, for the path's placement, or,
    with no pricer, the plan's full-target estimates."""

    def __init__(
        self,
        plan: SlicePlanFacts,
        step_of: Mapping[str, int],
        relational: str | None,
        price: PathPricer | None,
    ) -> None:
        self.plan, self.step_of, self.relational, self.price = plan, step_of, relational, price
        self.basis = "per_path_sizing" if price is not None else "full_target_estimates"
        self._cache: dict[frozenset[tuple[str, str]], Mapping[str, Mapping[str, Any]]] = {}

    def engines(self, stop: int) -> Mapping[str, Mapping[str, Any]]:
        """By engine, its estimate when the plan stops after step ``stop``: every query a
        later step moves stays on the relational engine. Empty with no pricer."""
        if self.price is None:
            return {}
        relational = self.relational
        placed = (
            {q: relational for q, k in self.step_of.items() if k > stop}
            if relational is not None
            else {}
        )
        key = frozenset(placed.items())
        if key not in self._cache:
            self._cache[key] = self.price(placed)
        return self._cache[key]

    def cost(self, engine: str, stop: int) -> float:
        if self.price is None:
            return self.plan.engine_monthly_cost_usd[engine]
        return float(self.engines(stop)[engine]["monthly_cost_usd"])

    def engine_costs(self, engines: list[str], stop: int) -> list[dict[str, Any]]:
        priced = self.engines(stop)
        return [dict(priced[e]) for e in engines] if priced else []

    def relational_cost(self) -> float:
        """The relational move only's cost: the engines of the steps that move no query,
        with every moving query on the relational engine."""
        steps = self.plan.plan_steps
        baseline = dict.fromkeys(s.engine for s in steps if s.kind in NO_MOVE_STEP_KINDS)
        return round(sum(self.cost(e, 0) for e in baseline), 2)


def _path_id(stop: int, lead: int, last: int, relational: bool) -> str:
    if relational and stop == lead:
        return "relational_only"
    if stop == last:
        return "full_target"
    return f"through_step_{stop}"


def _moving_queries(plan: SlicePlanFacts) -> tuple[dict[str, int], dict[str, str]]:
    """Each query a step moves off the relational engine: the step that moves it, and
    its engine there."""
    units = {u.key: u for u in plan.move_units}
    step_of: dict[str, int] = {}
    engine_of: dict[str, str] = {}
    for step in plan.plan_steps:
        for stage in step.step_stages:
            for key in stage.unit_keys:
                unit = units[key]
                if unit.kind in MOVING_UNIT_KINDS:
                    for qid in unit.query_ids:
                        step_of[qid], engine_of[qid] = step.step_number, unit.engine
    return step_of, engine_of


def _reverse_streams(plan: SlicePlanFacts, steps: list[SliceStep]) -> int:
    """The reverse streams the stage c units of ``steps`` need until the later stages
    finish: one per engine that reads a copy of a table such a unit owns."""
    units = {u.key: u for u in plan.move_units}
    return sum(
        len(units[key].reverse_replicas)
        for step in steps
        for stage in step.step_stages
        if stage.kind == "with_reverse_replica"
        for key in stage.unit_keys
    )


def _path(
    report: Mapping[str, Any],
    plan: SlicePlanFacts,
    stop: int,
    path_id: str,
    priced: _Priced,
) -> dict[str, Any]:
    """The path that stops after step ``stop``: every figure from steps 1 to ``stop``."""
    from src.shared.report_facts import cost_baseline_known

    steps = plan.plan_steps
    taken = steps[:stop]
    moving = [s for s in taken if s.kind in MOVING_STEP_KINDS]
    moved = sum(s.query_count for s in moving)
    by_engine: dict[str, int] = {}
    if steps[0].kind == "relational_step":
        by_engine[steps[0].engine] = steps[0].query_count - moved
    for s in moving:
        by_engine[s.engine] = by_engine.get(s.engine, 0) + s.query_count
    engines = list(dict.fromkeys(s.engine for s in taken))
    cost = round(sum(priced.cost(e, stop) for e in engines), 2)
    # The relational-only end state: the engines of the steps that move no query, priced
    # with every moving query on the relational engine (#2170).
    relational_cost = priced.relational_cost()
    tco = report.get("tco_analysis") or {}
    current = tco.get("current_monthly_cost") if cost_baseline_known(tco) else None
    units = {u.key: u for u in plan.move_units}
    replicated = sorted(
        {
            table
            for step in taken
            for stage in step.step_stages
            for key in stage.unit_keys
            if units[key].kind in MOVING_UNIT_KINDS
            for table in [
                *(r.table for r in units[key].replicated_in),
                *(t for r in units[key].reverse_replicas for t in r.tables),
            ]
        }
    )
    stages = sum(s.stage_count for s in taken)
    return {
        "path_id": path_id,
        "engines": engines,
        "stops_after_step": stop,
        "step_count": stop,
        "stage_count": stages,
        "gate_count": stages,
        "queries_moved": moved,
        "queries_by_engine": by_engine,
        "tables_migrate": sum(s.owned_table_count for s in moving),
        "monthly_cost_usd": cost,
        # #2170: each engine's estimate for the queries this path places on it.
        "engine_costs": priced.engine_costs(engines, stop),
        "cost_delta_vs_relational_usd": round(cost - relational_cost, 2),
        "cost_delta_vs_current_usd": (
            round(cost - float(current), 2)
            if isinstance(current, (int, float)) and not isinstance(current, bool)
            else None
        ),
        # An estimate, not a bound: with a pricer each engine is sized for the queries
        # the path places on it; without one, for the full target's (#2170).
        "cost_is_upper_bound": False,
        "critical_high_risks": sum(plan.engine_critical_high_risks[e] for e in engines),
        "replicated_tables": replicated,
        "replicated_table_count": len(replicated),
        "temporary_stream_count": _reverse_streams(plan, taken),
        "first_gate": steps[0].step_stages[0].gate,
    }


def _one_step(plan: SlicePlanFacts) -> dict[str, Any]:
    """The full target in one step (#473): the staged form's counts, and the three
    "suits one step" checks, each with what the plan has. They describe the workload's
    shape, not a cutover."""
    steps = plan.plan_steps
    stages = [st for s in steps for st in s.step_stages]
    observed = {
        "no_open_critical_high_risk": (sum(plan.engine_critical_high_risks.values()), 0),
        "at_most_one_purpose_built_target": (
            len({s.engine for s in steps if s.kind in MOVING_STEP_KINDS}),
            1,
        ),
        "no_reverse_replica_stage": (
            sum(1 for st in stages if st.kind == "with_reverse_replica"),
            0,
        ),
    }
    checks = [
        {"rule": rule, "observed": seen, "limit": limit, "passed": seen <= limit}
        for rule, (seen, limit) in observed.items()
    ]
    return {
        "step_count": 1,
        "staged_step_count": len(steps),
        "staged_stage_count": len(stages),
        "gate_count": len(stages),
        "temporary_stream_count": 0,
        "staged_temporary_stream_count": _reverse_streams(plan, steps),
        "suits_one_step": all(c["passed"] for c in checks),
        "one_step_checks": checks,
    }
