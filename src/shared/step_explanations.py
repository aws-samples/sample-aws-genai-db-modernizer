# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Each step's "why" sentence and the plan metrics' plain names (#1699).

``facts.slice_plan`` orders its steps by rules a reader cannot see: the relational step
first, then the migration waves' order, and inside a wave each slice's share of calls
against what cutting it off needs (``dependency_graph.slice_order_key``). Every step
states, in one sentence filled from a fixed template, what it moves, why it sits where
it does, what it shares with other steps, what it needs copied across and what holds
it. ``METRIC_LABELS`` names each order metric in plain words, the one set of words the
sentences and every deliverable use.

Every number is read from the plan itself (the step's own counts, its stages' holding
risks, its slice metrics, its units' copies and writes, the plan's dependencies): no
figure is recomputed from the assignment, and nothing here ranks a step or an engine,
names a graph term, or says anything about the cutover. A model may smooth the wording;
it never adds a claim or a number (#1699). ``tests/unit/shared/test_step_explanations.py``
guards the templates.
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
BUILT_ON_ONE = "the later step builds on the tables it carries"
BUILT_ON_NONE = "no later step builds on the tables it carries"
NO_LATER_STEP = "no later step follows it"

CACHE_WHY = (
    "Step {step} puts {engine} in front of {reads}, {share} of calls, and moves no " "data{fronts}."
)
CACHE_FRONTS = "; it caches {tables} that step {source} carries on {fronted}"

SLICE_WHY = (
    "Step {step} moves {queries} and {tables} to {engine}, {share} of calls, placed in "
    "wave {wave} by those calls against its {copied} and {writes}{hub}; {shares}{held}."
)
HUB_LATE = "; as it is shared by many parts, it runs after every step in wave {wave} that is not"

ENGINE_WAVE_WHY = (
    "Step {step} moves {queries} and {tables} to {engine}, {share} of calls, as wave "
    "{wave} of the roadmap, with its {copied} and {writes}; {shares}{held}."
)

SHARES_SOME = "it shares {tables} with {steps}"
SHARES_NONE_BUT_FIRST = "no step but step 1 shares its tables"
SHARES_NONE = "no other step shares its tables"
HELD = ", and {risks} {verb} back {queries} until {pronoun} closed"

#: Every fixed text a sentence is filled from, for the customer-text guard.
TEMPLATES: tuple[str, ...] = (
    RELATIONAL_WHY,
    BUILT_ON_SOME,
    BUILT_ON_ALL,
    BUILT_ON_ONE,
    BUILT_ON_NONE,
    NO_LATER_STEP,
    CACHE_WHY,
    CACHE_FRONTS,
    SLICE_WHY,
    HUB_LATE,
    ENGINE_WAVE_WHY,
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


# --- The figures each sentence reads ---------------------------------------------------


def _step_of(plan: Mapping[str, Any]) -> dict[str, int]:
    """Each unit key's step number."""
    return {
        key: int(step["step_number"])
        for step in plan.get("plan_steps") or []
        for stage in step.get("step_stages") or []
        for key in stage.get("unit_keys") or []
    }


def _ties(plan: Mapping[str, Any], number: int) -> list[tuple[int, list[str]]]:
    """(the other step, the tables the tie runs through) for each dependency between one
    of step ``number``'s units and another step's unit."""
    step_of = _step_of(plan)
    out = []
    for dep in plan.get("dependencies") or []:
        a, b = step_of.get(dep.get("unit_key")), step_of.get(dep.get("depends_on"))
        if a is None or b is None or a == b or number not in (a, b):
            continue
        out.append((b if a == number else a, [str(t) for t in dep.get("tables") or []]))
    return out


def _relational_number(plan: Mapping[str, Any]) -> int | None:
    for step in plan.get("plan_steps") or []:
        if step.get("kind") == "relational_step":
            return int(step["step_number"])
    return None


def _shares(plan: Mapping[str, Any], number: int) -> str:
    relational = _relational_number(plan)
    tied = [(other, tables) for other, tables in _ties(plan, number) if other != relational]
    tables = sorted({t for _, ts in tied for t in ts})
    if tables:
        return SHARES_SOME.format(
            tables=_count(len(tables), "table", "tables"),
            steps=_steps(other for other, ts in tied if ts),
        )
    return SHARES_NONE_BUT_FIRST if relational is not None else SHARES_NONE


def _units(plan: Mapping[str, Any], step: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    by_key = {u.get("key"): u for u in plan.get("move_units") or []}
    keys = [k for stage in step.get("step_stages") or [] for k in stage.get("unit_keys") or []]
    return [by_key[k] for k in keys if k in by_key]


def _copied_and_writes(plan: Mapping[str, Any], step: Mapping[str, Any]) -> tuple[int, int]:
    """The step's tables copied across, either way, and its write statements that change
    engine: a slice step's cut cost as stored, else the same count over its units."""
    cut = (step.get("metrics") or {}).get("cut_cost")
    if isinstance(cut, Mapping):
        return int(cut.get("replica_table_count") or 0), int(cut.get("write_site_count") or 0)
    units = _units(plan, step)
    owned = {t for u in units for t in u.get("owned_tables") or []}
    copies_in = {r.get("table") for u in units for r in u.get("replicated_in") or []} - owned
    copies_out = {
        t for u in units for r in u.get("reverse_replicas") or [] for t in r.get("tables") or []
    }
    writes = {q for u in units for q in u.get("write_query_ids") or []}
    return len(copies_in | copies_out), len(writes)


def _held(plan: Mapping[str, Any], step: Mapping[str, Any]) -> str:
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


def _built_on(plan: Mapping[str, Any], number: int) -> str:
    later = [
        int(s["step_number"])
        for s in plan.get("plan_steps") or []
        if int(s["step_number"]) > number
    ]
    if not later:
        return NO_LATER_STEP
    dependents = {other for other, _ in _ties(plan, number) if other > number}
    if not dependents:
        return BUILT_ON_NONE
    if len(later) == 1:
        return BUILT_ON_ONE
    if len(dependents) == len(later):
        return BUILT_ON_ALL.format(later=len(later))
    return BUILT_ON_SOME.format(
        dependents=len(dependents),
        later=len(later),
        verb="builds" if len(dependents) == 1 else "build",
    )


def _cache_fronts(plan: Mapping[str, Any], step: Mapping[str, Any]) -> str:
    """The step the cache fronts: its ``cache_fronts`` ties, with their tables."""
    number = int(step["step_number"])
    keys = {u.get("key") for u in _units(plan, step)}
    step_of = _step_of(plan)
    engine_of = {u.get("key"): u.get("engine") for u in plan.get("move_units") or []}
    fronted = [
        d
        for d in plan.get("dependencies") or []
        if d.get("unit_key") in keys
        and "cache_fronts" in (d.get("kinds") or [])
        and step_of.get(d.get("depends_on"), number) != number
    ]
    sources = sorted({step_of[d["depends_on"]] for d in fronted})
    tables = sorted({str(t) for d in fronted for t in d.get("tables") or []})
    if len(sources) != 1 or not tables:
        return ""
    engines = sorted({str(engine_of.get(d["depends_on"])) for d in fronted})
    return CACHE_FRONTS.format(
        tables=_count(len(tables), "table", "tables"),
        source=sources[0],
        fronted=_and([display_engine(e) for e in engines]),
    )


def step_why(plan: Mapping[str, Any], step: Mapping[str, Any]) -> str:
    """``step``'s "why" sentence, filled from its kind's template with the plan's figures."""
    number = int(step["step_number"])
    kind = step.get("kind")
    engine = display_engine(str(step.get("engine")))
    queries = _count(int(step.get("query_count") or 0), "query", "queries")
    tables = _count(int(step.get("owned_table_count") or 0), "table", "tables")
    share = _percent(step.get("call_share_percent") or 0.0)
    if kind == "relational_step":
        return RELATIONAL_WHY.format(
            step=number,
            engine=engine,
            queries=queries,
            tables=tables,
            built_on=_built_on(plan, number),
        )
    if kind == "cache":
        return CACHE_WHY.format(
            step=number,
            engine=engine,
            reads=_count(int(step.get("query_count") or 0), "read", "reads"),
            share=share,
            fronts=_cache_fronts(plan, step),
        )
    copied, writes = _copied_and_writes(plan, step)
    fields = {
        "step": number,
        "queries": queries,
        "tables": tables,
        "engine": engine,
        "share": share,
        "wave": step.get("phase_wave"),
        "copied": _count(copied, "table copied across", METRIC_LABELS["replica_table_count"]),
        "writes": _count(
            writes, "write statement that changes engine", METRIC_LABELS["write_site_count"]
        ),
        "shares": _shares(plan, number),
        "held": _held(plan, step),
    }
    if kind == "slice":
        hub = float((step.get("metrics") or {}).get("hub_score") or 0.0)
        late = HUB_LATE.format(wave=step.get("phase_wave")) if hub >= HUB_HEAVY else ""
        return SLICE_WHY.format(hub=late, **fields)
    return ENGINE_WAVE_WHY.format(**fields)


def with_step_explanations(plan: Mapping[str, Any]) -> dict[str, Any]:
    """``plan`` with each step's ``why`` and the plan's ``metric_labels``: the one function
    synthesis and the reader of an older report call (ADR-032 Decision 4)."""
    out = dict(plan)
    out["plan_steps"] = [
        {**step, "why": step_why(plan, step)} for step in plan.get("plan_steps") or []
    ]
    out["metric_labels"] = dict(METRIC_LABELS)
    return out


def needs_step_explanations(plan: Any) -> bool:
    """A stored plan written before #1699: steps, and no metric labels or a step with no
    ``why`` (a plan re-dumped through the contract reads it back as None). A stored plan
    with no steps names no metric, so it is read as stored."""
    if not isinstance(plan, Mapping):
        return False
    steps = plan.get("plan_steps") or []
    if not steps:
        return False
    return not plan.get("metric_labels") or any(
        isinstance(s, Mapping) and not s.get("why") for s in steps
    )
