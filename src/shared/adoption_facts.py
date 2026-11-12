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
empty plan, as ``relational_gaps`` does without the collection. The adoption paths are
still the empty, validated structure (#1130 fills them behind the same entry point).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from src.contracts.synthesis_output import (
    AdoptionFacts,
    SlicePlanFacts,
    check_adoption_against_plan,
)
from src.shared.slice_plan import build_slice_plan

if TYPE_CHECKING:
    from src.shared.move_units import SliceInputs

__all__ = ["adoption_facts", "holding_risks", "slice_plan_facts"]


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
    empty = SlicePlanFacts(step_basis="engine_waves").model_dump(mode="json")
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
    report: Mapping[str, Any], slice_plan: Mapping[str, Any] | None
) -> dict[str, Any]:
    """``facts.adoption`` for ``report`` and its ``slice_plan``: no paths yet (#1130)."""
    del report  # read by the computation that follows (#1130)
    adoption = AdoptionFacts()
    if slice_plan is not None:
        check_adoption_against_plan(adoption, SlicePlanFacts.model_validate(slice_plan))
    return adoption.model_dump(mode="json")
