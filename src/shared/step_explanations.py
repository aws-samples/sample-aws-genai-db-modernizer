# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Each step's "why" sentence and the plan metrics' plain names (#1699).

``facts.slice_plan`` orders its steps by rules a reader cannot see: the relational step
first, then each step in its engine's wave of the roadmap (the migration waves), and
inside a wave the slices by each one's share of calls against what cutting it off needs,
the slices shared by many parts last (``dependency_graph.slice_order_key``). Every step
states, in one sentence filled from a fixed template, what it moves, why it sits where
it does, what it shares with other steps, what it needs copied across and what holds
it. ``METRIC_LABELS`` names each order metric in plain words, the one set of words the
sentences and every deliverable use.

Every number in a sentence is a figure the plan states: the step's own counts, its
stages' holding risks and its ``ties`` (``StepTies``), which ``step_ties`` counts once
from the plan's dependencies and steps; a moving step's tables copied across and write
statements that change engine are its slice metrics' cut cost, or for an engine-wave
step the same ``dependency_graph.cut_cost`` the slice plan passes in. Nothing here ranks
a step or an engine, names a graph term, or says anything about the cutover. A model may
smooth the wording; it never adds a claim or a number (#1699).
``tests/unit/shared/test_step_explanations.py`` guards the templates.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from src.shared.dependency_graph import HUB_HEAVY
from src.shared.engine_names import display_engine

__all__ = [
    "METRIC_LABELS",
    "TEMPLATES",
    "needs_step_explanations",
    "step_ties",
    "step_why",
    "with_step_explanations",
]

#: The plain name of each order metric, by its field in ``UnitMetrics``, ``SliceMetrics``
#: and ``CutCost``: the one set of words for them everywhere (#1699).
METRIC_LABELS: dict[str, str] = {
    "gravity": "how tightly tied",
    "hub_score": "shared by many parts",
    "blast_radius": "what the move touches",
    "cut_cost": "what it breaks",
    "replica_table_count": "tables copied across",
    "write_site_count": "write statements that change engine",
    "transactions_split": "transactions it splits",
    "value_call_share_percent": "share of calls it moves",
    "value_monthly_cost_usd": "monthly cost it moves",
}

# --- The templates ---------------------------------------------------------------------
# Each placeholder is a figure of the plan, formatted, or one of the fragments below.

RELATIONAL_WHY = (
    "Step {step} comes first: it moves the whole database to {engine}, {queries} and "
    "{tables}, and {built_on}."
)
BUILT_ON_SOME = "{dependents} of the {later} later steps {verb} on the tables it carries"
BUILT_ON_ALL = "all {later} later steps build on the tables it carries"
BUILT_ON_BOTH = "both later steps build on the tables it carries"
BUILT_ON_ONE = "the later step builds on the tables it carries"
BUILT_ON_NONE = "no later step builds on the tables it carries"
NO_LATER_STEP = "no later step follows it"

CACHE_WHY = (
    "Step {step} puts {engine} in front of {reads}, {share} of calls, and moves no data{fronts}."
)
CACHE_FRONTS = "; it caches {tables} that step {source} carries on {fronted}"

MOVING_WHY = (
    "Step {step} moves {queries} and {tables} to {engine}, {share} of calls, in wave "
    "{wave} of the roadmap{cut}{hub}; {shares}{held}."
)
CUT_ORDERED = ", ordered in that wave by those calls against its {copied} and {writes}"
CUT_STATED = ", with its {copied} and {writes}"
HUB_AFTER = "; it runs after {steps} of its wave, which {verb} not shared by many parts"

SHARES_SOME = "it shares {tables} with {steps}"
SHARES_NONE_BUT_FIRST = "no step but step {first} shares its tables"
SHARES_NONE = "no other step shares its tables"
HELD = ", and {risks} {verb} back {queries} until {pronoun} closed"

#: Every fixed text a sentence is filled from, for the customer-text guard.
TEMPLATES: tuple[str, ...] = (
    RELATIONAL_WHY,
    BUILT_ON_SOME,
    BUILT_ON_ALL,
    BUILT_ON_BOTH,
    BUILT_ON_ONE,
    BUILT_ON_NONE,
    NO_LATER_STEP,
    CACHE_WHY,
    CACHE_FRONTS,
    MOVING_WHY,
    CUT_ORDERED,
    CUT_STATED,
    HUB_AFTER,
    SHARES_SOME,
    SHARES_NONE_BUT_FIRST,
    SHARES_NONE,
    HELD,
)


# --- Formatting ------------------------------------------------------------------------


def _count(n: int, one: str, many: str) -> str:
    return f"{n:,} {one if n == 1 else many}"


def _percent(value: float) -> str:
    return f"{float(value):g}%"


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _steps(numbers: Iterable[int]) -> str:
    ordered = sorted(set(numbers))
    word = "step" if len(ordered) == 1 else "steps"
    return f"{word} {_and([str(n) for n in ordered])}"


# --- The figures, counted once -----------------------------------------------------------


def _step_of(plan: Mapping[str, Any]) -> dict[str, int]:
    """Each unit key's step number."""
    return {
        key: int(step["step_number"])
        for step in plan.get("plan_steps") or []
        for stage in step.get("step_stages") or []
        for key in stage.get("unit_keys") or []
    }


def _hub_heavy(step: Mapping[str, Any]) -> bool:
    return float((step.get("metrics") or {}).get("hub_score") or 0.0) >= HUB_HEAVY


def _cut(
    step: Mapping[str, Any], cut_costs: Mapping[int, tuple[int, int]]
) -> tuple[int | None, int | None]:
    """A moving step's tables copied across and write statements that change engine: its
    slice cut cost, else the slice plan's ``cut_cost`` for it, else as stored."""
    if step.get("kind") not in ("slice", "engine_wave"):
        return None, None
    cut = (step.get("metrics") or {}).get("cut_cost")
    if isinstance(cut, Mapping):
        return int(cut["replica_table_count"]), int(cut["write_site_count"])
    number = int(step["step_number"])
    if number in cut_costs:
        return cut_costs[number]
    stored = step.get("ties") or {}
    return stored.get("replica_table_count"), stored.get("write_site_count")


def step_ties(
    plan: Mapping[str, Any], cut_costs: Mapping[int, tuple[int, int]] | None = None
) -> dict[int, dict[str, Any]]:
    """Each step's ``ties`` (``StepTies``), by step number: the one place the figures its
    sentence states are counted, from the plan's dependencies and steps. ``cut_costs`` is
    an engine-wave step's ``dependency_graph.cut_cost``, which the slice plan passes in."""
    steps = [s for s in plan.get("plan_steps") or [] if isinstance(s, Mapping)]
    step_of = _step_of(plan)
    relational = {int(s["step_number"]) for s in steps if s.get("kind") == "relational_step"}
    edges: list[tuple[int, int, list[str], list[str]]] = []
    for dep in plan.get("dependencies") or []:
        later, earlier = step_of.get(dep.get("unit_key")), step_of.get(dep.get("depends_on"))
        if later is None or earlier is None or later == earlier:
            continue
        tables = [str(t) for t in dep.get("tables") or []]
        edges.append((later, earlier, tables, [str(k) for k in dep.get("kinds") or []]))
    out: dict[int, dict[str, Any]] = {}
    for step in steps:
        n = int(step["step_number"])
        dependents = sorted({a for a, b, _, _ in edges if b == n})
        shared = [
            (b if a == n else a, tables)
            for a, b, tables, _ in edges
            if n in (a, b) and (b if a == n else a) not in relational and tables
        ]
        fronts = [
            (b, tables)
            for a, b, tables, kinds in edges
            if a == n and step.get("kind") == "cache" and "cache_fronts" in kinds and tables
        ]
        fronted_steps = sorted({b for b, _ in fronts})
        fronted = sorted({t for _, ts in fronts for t in ts}) if len(fronted_steps) == 1 else []
        wave = [
            s
            for s in steps
            if step.get("kind") == "slice"
            and s.get("kind") == "slice"
            and s.get("phase_wave") == step.get("phase_wave")
            and int(s["step_number"]) != n
        ]
        copied, writes = _cut(step, cut_costs or {})
        tables = sorted({t for _, ts in shared for t in ts})
        out[n] = {
            "later_step_count": sum(1 for s in steps if int(s["step_number"]) > n),
            "dependent_steps": dependents,
            "dependent_step_count": len(dependents),
            "shared_with_steps": sorted({other for other, _ in shared}),
            "shared_tables": tables,
            "shared_table_count": len(tables),
            "fronted_step": fronted_steps[0] if fronted else None,
            "fronted_tables": fronted,
            "fronted_table_count": len(fronted),
            "replica_table_count": copied,
            "write_site_count": writes,
            "ordered_with_steps": sorted(int(s["step_number"]) for s in wave),
            "runs_after_steps": (
                sorted(
                    int(s["step_number"])
                    for s in wave
                    if int(s["step_number"]) < n and not _hub_heavy(s)
                )
                if _hub_heavy(step)
                else []
            ),
        }
    return out


# --- The sentence ------------------------------------------------------------------------


def _held(step: Mapping[str, Any]) -> str:
    held = [s for s in step.get("step_stages") or [] if s.get("kind") == "risk_held"]
    risks = sorted({str(r) for s in held for r in s.get("holding_risk_ids") or []})
    if not risks:
        return ""
    queries = sum(int(s.get("query_count") or 0) for s in held)
    return HELD.format(
        risks=_and(risks),
        verb="holds" if len(risks) == 1 else "hold",
        queries=_count(queries, "query", "queries"),
        pronoun="it is" if len(risks) == 1 else "they are",
    )


def _built_on(ties: Mapping[str, Any]) -> str:
    later = int(ties["later_step_count"])
    dependents = int(ties["dependent_step_count"])
    if not later:
        return NO_LATER_STEP
    if not dependents:
        return BUILT_ON_NONE
    if later == 1:
        return BUILT_ON_ONE
    if dependents == later:
        return BUILT_ON_BOTH if later == 2 else BUILT_ON_ALL.format(later=later)
    return BUILT_ON_SOME.format(
        dependents=dependents, later=later, verb="builds" if dependents == 1 else "build"
    )


def _shares(plan: Mapping[str, Any], ties: Mapping[str, Any]) -> str:
    if ties["shared_table_count"]:
        return SHARES_SOME.format(
            tables=_count(int(ties["shared_table_count"]), "table", "tables"),
            steps=_steps(ties["shared_with_steps"]),
        )
    first = [
        s.get("step_number")
        for s in plan.get("plan_steps") or []
        if s.get("kind") == "relational_step"
    ]
    return SHARES_NONE_BUT_FIRST.format(first=first[0]) if first else SHARES_NONE


def _fronts(plan: Mapping[str, Any], ties: Mapping[str, Any]) -> str:
    source = ties.get("fronted_step")
    if source is None:
        return ""
    engine = next(
        s.get("engine") for s in plan.get("plan_steps") or [] if s.get("step_number") == source
    )
    return CACHE_FRONTS.format(
        tables=_count(int(ties["fronted_table_count"]), "table", "tables"),
        source=source,
        fronted=display_engine(str(engine)),
    )


def _cut_clause(ties: Mapping[str, Any]) -> str:
    copied, writes = ties.get("replica_table_count"), ties.get("write_site_count")
    if copied is None or writes is None:
        return ""
    template = CUT_ORDERED if ties.get("ordered_with_steps") else CUT_STATED
    return template.format(
        copied=_count(int(copied), "table copied across", METRIC_LABELS["replica_table_count"]),
        writes=_count(
            int(writes), "write statement that changes engine", METRIC_LABELS["write_site_count"]
        ),
    )


def step_why(plan: Mapping[str, Any], step: Mapping[str, Any]) -> str:
    """``step``'s "why" sentence, filled from its kind's template with its ``ties`` (or
    ``step_ties``' when it has none yet) and its own counts."""
    number = int(step["step_number"])
    ties = step.get("ties") or step_ties(plan)[number]
    engine = display_engine(str(step.get("engine")))
    queries = _count(int(step.get("query_count") or 0), "query", "queries")
    tables = _count(int(step.get("owned_table_count") or 0), "table", "tables")
    share = _percent(step.get("call_share_percent") or 0.0)
    kind = step.get("kind")
    if kind == "relational_step":
        return RELATIONAL_WHY.format(
            step=number, engine=engine, queries=queries, tables=tables, built_on=_built_on(ties)
        )
    if kind == "cache":
        return CACHE_WHY.format(
            step=number,
            engine=engine,
            reads=_count(int(step.get("query_count") or 0), "read", "reads"),
            share=share,
            fronts=_fronts(plan, ties),
        )
    after = ties.get("runs_after_steps") or []
    return MOVING_WHY.format(
        step=number,
        queries=queries,
        tables=tables,
        engine=engine,
        share=share,
        wave=step.get("phase_wave"),
        cut=_cut_clause(ties),
        hub=(
            HUB_AFTER.format(steps=_steps(after), verb="is" if len(after) == 1 else "are")
            if after
            else ""
        ),
        shares=_shares(plan, ties),
        held=_held(step),
    )


def with_step_explanations(
    plan: Mapping[str, Any], cut_costs: Mapping[int, tuple[int, int]] | None = None
) -> dict[str, Any]:
    """``plan`` with what it lacks of each step's ``ties`` and ``why`` and of the plan's
    ``metric_labels``; what it states is kept. The one function synthesis and the reader of
    an older report call (ADR-032 Decision 4)."""
    ties = step_ties(plan, cut_costs)
    steps = []
    for step in plan.get("plan_steps") or []:
        filled = {**step, "ties": step.get("ties") or ties[int(step["step_number"])]}
        filled["why"] = step.get("why") or step_why(plan, filled)
        steps.append(filled)
    stored = plan.get("metric_labels") or {}
    return {**plan, "plan_steps": steps, "metric_labels": {**METRIC_LABELS, **stored}}


def needs_step_explanations(plan: Any) -> bool:
    """A stored plan written before #1699 or before a metric was added: a metric with no
    label, or a step with no ``ties`` or ``why`` (a plan re-dumped through the contract
    reads them back as None)."""
    if not isinstance(plan, Mapping):
        return False
    if not set(METRIC_LABELS) <= set(plan.get("metric_labels") or {}):
        return True
    return any(
        isinstance(s, Mapping) and (not s.get("why") or not s.get("ties"))
        for s in plan.get("plan_steps") or []
    )
