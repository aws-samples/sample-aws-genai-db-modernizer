# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The slice plan: steps, their ordered stages a to d, and their gates (#1129, #1698).

Built in our code from the effective assignment and the report's own facts, never by a
model (ADR-033 Decision 1, ``SlicePlanFacts``):

- **Step 1** is the relational step: the whole database moves to wave 1's Aurora engine,
  one stage holding one ``whole_database`` unit, with wave 1's table classes.
- **Later steps.** For a homogeneous source (``step_basis="slices"``) each later step
  moves one slice (``dependency_graph.slices_of``) to one engine, or the cache's fronted
  reads; the steps follow the migration waves' order (each names its ``phase_wave``),
  and inside a wave the slices run in ``dependency_graph.slice_order_key``'s order. For
  any other source, and when the plan cannot be numbered otherwise, each later step is
  one wave's engine (``step_basis="engine_waves"``), numbered as its wave.
- **Stages.** Each unit lands in the first stage that applies to it: d, held by an open
  critical or high risk; a, it owns no table (it reads copies); b, it owns tables and
  needs no copy either way; c, it owns tables and needs a copy either way. Stages run a,
  b, c, d; inside a stage units run by blast radius, then call share (highest first),
  then key. A step with more than one unit names its first unit the pilot.
- **Gates** are filled templates, with their table lists as ``stage_gate_parts``.

``build_slice_plan`` returns None when no plan can be stated (no waves, no queries, an
engine with no cost, or engine waves that cannot be one step each); the caller then
writes the empty plan, as before the computation existed.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from src.contracts.synthesis_output import (
    STAGE_LETTERS,
    STAGE_RANKS,
    SlicePlanFacts,
    SliceStep,
    join_gate_parts,
    phase_facts,
)
from src.shared.cache_engines import CACHE_ENGINES
from src.shared.cutover_message import CUTOVER_MESSAGE
from src.shared.dependency_graph import (
    Graph,
    Slice,
    build_graph,
    measure_slices,
    share,
    slice_order_key,
    slices_of,
    unit_gravity,
)
from src.shared.engine_names import SOURCE_ENGINE_DISPLAY_NAMES, display_engine
from src.shared.migration_wave_engines import RELATIONAL_ENGINES
from src.shared.move_units import (
    KeptWhole,
    SliceInputs,
    Unit,
    build_cache_units,
    build_units,
    kept_whole_groups,
)

__all__ = ["build_slice_plan", "stage_kind", "unit_order_key"]


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _names(engines: Iterable[str]) -> str:
    names = [display_engine(e) for e in sorted(set(engines))]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def stage_kind(unit: Unit) -> str:
    """The stage a moving unit runs in, by the first rule that applies (#1129)."""
    if unit.holding_risk_ids:
        return "risk_held"
    if not unit.owned_tables:
        return "replica_reads"
    if not unit.replicated_in and not unit.reverse_replicas:
        return "self_contained"
    return "with_reverse_replica"


def unit_order_key(unit: Unit) -> tuple[int, float, str]:
    """In-stage run order: blast radius, then call share (highest first), then key."""
    return (unit.blast_radius, -round(unit.calls_per_second, 6), unit.key)


# --- Gates -------------------------------------------------------------------------------


def _relational_gate(source: str, relational: str, tables: int) -> list[dict[str, Any]]:
    src = SOURCE_ENGINE_DISPLAY_NAMES.get(source, display_engine(source))
    return [
        {
            "text": (
                f"Full load complete and replication in progress from {src} to "
                f"{display_engine(relational)} for {_plural(tables, 'table', 'tables')}. "
                f"{CUTOVER_MESSAGE}"
            ),
            "items": [],
        }
    ]


def _cache_gate(fronted: str, reads: int) -> list[dict[str, Any]]:
    return [
        {
            "text": (
                f"Cache hit rate and invalidation verified against {display_engine(fronted)} "
                f"for the {_plural(reads, 'cached read', 'cached reads')}."
            ),
            "items": [],
        }
    ]


def _reads_return(engine: str, reads: int) -> str:
    verb = "returns" if reads == 1 else "return"
    noun = "read" if reads == 1 else f"{reads} reads"
    return f"the {noun} {verb} the same rows on {display_engine(engine)}"


def _stage_gate(kind: str, engine: str, units: list[Unit]) -> list[dict[str, Any]]:
    """A moving stage's gate, as text runs and table lists."""
    owned = sorted({t for u in units for t in u.owned_tables})
    writers = {w for u in units for _, w in u.replicated_in}
    readers = {e for u in units for e, _, _ in u.reverse_replicas}
    reverse_reads = sum(u.reverse_read_count for u in units)
    reads = sum(u.query_count for u in units)
    parts: list[dict[str, Any]] = []
    lead = ""
    if kind == "risk_held":
        risks = sorted({r for u in units for r in u.holding_risk_ids})
        lead = f"{_names_plain(risks)} closed; then "
    if not owned:
        text = (
            f"CDC from {_names(writers)} to {display_engine(engine)} is within the reads' "
            f"staleness tolerance, and {_reads_return(engine, reads)}."
            if writers
            else f"{_reads_return(engine, reads)}."
        )
        parts.append({"text": _capital(lead + text), "items": []})
        return parts
    parts.append(
        {
            "text": _capital(
                f"{lead}backfill complete and query parity confirmed for "
                f"{_plural(len(owned), 'table', 'tables')}"
            ),
            "items": owned,
        }
    )
    tail = []
    if writers:
        tail.append(
            f"CDC from {_names(writers)} to {display_engine(engine)} is within the reads' "
            "staleness tolerance"
        )
    if readers:
        tail.append(
            f"CDC from {display_engine(engine)} to {_names(readers)} carries the "
            f"{_plural(reverse_reads, 'read', 'reads')} that stay there, within their tolerance"
        )
    if kind == "self_contained":
        tail.append("no other engine reads them" if len(owned) > 1 else "no other engine reads it")
    parts.append({"text": "; " + "; ".join(tail) + "." if tail else ".", "items": []})
    return parts


def _names_plain(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _capital(text: str) -> str:
    return text[:1].upper() + text[1:]


# --- Units and stages as contract dicts ------------------------------------------------------


def _unit_dict(
    unit: Unit, calls_total: float, graph: Graph, tables_of: Mapping[str, Any]
) -> dict[str, Any]:
    moving = unit.kind in ("owner", "replica_reads", "read_model")
    return {
        "key": unit.key,
        "engine": unit.engine,
        "kind": unit.kind,
        "query_ids": [] if unit.kind == "whole_database" else list(unit.query_ids),
        "query_count": unit.query_count,
        "write_query_ids": [] if unit.kind == "whole_database" else list(unit.write_query_ids),
        "owned_tables": list(unit.owned_tables),
        "owned_table_count": len(unit.owned_tables),
        "replicated_in": [{"table": t, "source_of_truth": w} for t, w in unit.replicated_in],
        "reverse_replicas": [
            {"engine": e, "tables": tables, "query_ids": reads, "read_count": len(reads)}
            for e, tables, reads in unit.reverse_replicas
        ],
        "reverse_read_count": unit.reverse_read_count,
        "kept_whole_ids": list(unit.kept_whole_ids),
        "call_share_percent": share(unit.calls_per_second, calls_total),
        "holding_risk_ids": list(unit.holding_risk_ids),
        "pilot": False,
        "metrics": (
            {
                "gravity": unit_gravity(unit, tables_of),
                "blast_radius": unit.blast_radius,
                "hub_score": graph.hub_score.get(unit.key, 0.0),
            }
            if moving
            else None
        ),
    }


def _stage_dict(
    label: str, kind: str, units: list[Unit], gate: list[dict[str, Any]], calls_total: float
) -> dict[str, Any]:
    return {
        "label": label,
        "kind": kind,
        "unit_keys": [u.key for u in units],
        "unit_count": len(units),
        "query_count": sum(u.query_count for u in units),
        "owned_table_count": sum(len(u.owned_tables) for u in units),
        "call_share_percent": share(sum(u.calls_per_second for u in units), calls_total),
        "holding_risk_ids": sorted({r for u in units for r in u.holding_risk_ids}),
        "gate": join_gate_parts(gate),
        "stage_gate_parts": gate,
    }


def _moving_stages(
    step: int, engine: str, units: list[Unit], calls_total: float
) -> list[dict[str, Any]]:
    by_kind: dict[str, list[Unit]] = {}
    for u in units:
        by_kind.setdefault(stage_kind(u), []).append(u)
    out = []
    for kind in sorted(by_kind, key=lambda k: STAGE_RANKS[k]):
        members = sorted(by_kind[kind], key=unit_order_key)
        out.append(
            _stage_dict(
                f"{step}{STAGE_LETTERS[kind]}",
                kind,
                members,
                _stage_gate(kind, engine, members),
                calls_total,
            )
        )
    return out


def _step_dict(
    number: int,
    kind: str,
    engine: str,
    stages: list[dict[str, Any]],
    *,
    new_engine: bool,
    movement: str,
    phase_wave: int | None,
    slice_key: str | None = None,
    metrics: dict[str, Any] | None = None,
    table_classes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "step_number": number,
        "kind": kind,
        "engine": engine,
        "new_engine": new_engine,
        "movement": movement,
        "phase_wave": phase_wave,
        "slice_key": slice_key,
        "step_stages": stages,
        "stage_count": len(stages),
        "unit_count": sum(s["unit_count"] for s in stages),
        "query_count": sum(s["query_count"] for s in stages),
        "owned_table_count": sum(s["owned_table_count"] for s in stages),
        "call_share_percent": round(min(100.0, sum(s["call_share_percent"] for s in stages)), 1),
        "metrics": metrics,
        "table_classes": table_classes,
    }


def _slice_metrics(s: Slice) -> dict[str, Any]:
    return {
        "gravity": s.gravity,
        "hub_score": s.hub_score,
        "cut_cost": {
            "replica_table_count": s.replica_table_count,
            "write_site_count": s.write_site_count,
            "transactions_split": None,
        },
        "value_call_share_percent": s.value_call_share_percent,
        "value_monthly_cost_usd": None,
    }


def _table_classes(
    whole: Unit, units: list[Unit], stage_label: Mapping[str, str], relational: str, read: set[str]
) -> dict[str, Any]:
    owned = {t: stage_label[u.key] for u in units for t in u.owned_tables}
    replicated = {
        t for u in units for t, w in u.replicated_in if w == relational and t not in owned
    }
    leaves = [{"table": t, "stage_label": owned[t]} for t in sorted(owned)]
    rest = [t for t in whole.owned_tables if t not in owned and t not in replicated]
    stays = [t for t in rest if t in read]
    unread = [t for t in rest if t not in read]
    out_tables = sorted(replicated)
    return {
        "stays": stays,
        "stays_count": len(stays),
        "leaves": leaves,
        "leaves_count": len(leaves),
        "replicated_out": out_tables,
        "replicated_out_count": len(out_tables),
        "unread": unread,
        "unread_count": len(unread),
    }


# --- The plan ----------------------------------------------------------------------------


def _relational_engine(waves: list[Mapping[str, Any]]) -> str | None:
    if not waves:
        return None
    engines = [str(e) for e in waves[0].get("engines") or []]
    return engines[0] if len(engines) == 1 and engines[0] in RELATIONAL_ENGINES else None


def build_slice_plan(
    report: Mapping[str, Any],
    waves: Sequence[Mapping[str, Any]],
    inputs: SliceInputs,
    engine_costs: Mapping[str, float | None],
    risks_by_engine: Mapping[str, int],
    holding_risks: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any] | None:
    """``facts.slice_plan`` for ``report`` and its ``waves``, validated, or None when no
    plan can be stated. ``engine_costs`` and ``risks_by_engine`` are the facts layer's
    (``facts.engines``' costs, ``critical_high_risks_by_engine``), ``holding_risks`` the
    open critical and high risks, each with its ``engine``."""
    waves = [w for w in waves if isinstance(w, Mapping) and w.get("wave")]
    if not waves or not inputs.queries:
        return None
    relational = _relational_engine(waves)
    homogeneous = relational is not None and waves[0].get("homogeneity") == "homogeneous"
    basis = "slices" if homogeneous else "engine_waves"
    groups = kept_whole_groups(inputs)
    units = build_units(inputs, relational, groups, holding_risks)
    caches = build_cache_units(inputs)
    tables_of = {q.query_id: frozenset(q.tables) for q in inputs.queries}
    calls_total = sum(q.calls_per_second for q in inputs.queries)
    in_use = {str(e) for e in report.get("engines_in_use") or []}
    whole = None
    if relational is not None:
        wave_tables = {str(t) for t in waves[0].get("tables") or []}
        carried = wave_tables | {t for q in inputs.queries for t in q.tables}
        whole = Unit(
            key=f"{relational}:whole-database",
            engine=relational,
            kind="whole_database",
            query_ids=sorted(q.query_id for q in inputs.queries),
            write_query_ids=[],
            owned_tables=sorted(carried),
            read_tables=[],
            replicated_in=[],
            reverse_replicas=[],
            kept_whole_ids=[],
            calls_per_second=calls_total,
        )
    graph = build_graph(inputs, units, whole, caches, relational)
    by_key = {u.key: u for u in units}

    # Each later step: (kind, engine, phase wave, units, slice) in run order.
    planned: list[tuple[str, str, int | None, list[Unit], Slice | None]] = []
    cache_of = {c.engine: c for c in caches}
    placed: set[str] = set()
    for wave in waves:
        number = int(wave["wave"])
        engines = [str(e) for e in wave.get("engines") or []]
        if relational is not None and wave is waves[0]:
            continue
        wave_steps: list[tuple[str, str, int | None, list[Unit], Slice | None]] = []
        for engine in engines:
            if engine in placed:
                continue
            if engine in CACHE_ENGINES:
                if engine in cache_of:
                    wave_steps.append(("cache", engine, number, [cache_of[engine]], None))
                    placed.add(engine)
                continue
            mine = [u for u in units if u.engine == engine]
            if not mine:
                continue
            placed.add(engine)
            if basis == "engine_waves":
                wave_steps.append(("engine_wave", engine, number, mine, None))
        if basis == "slices":
            wave_engines = [e for e in engines if e not in CACHE_ENGINES]
            wave_units = [u for u in units if u.engine in wave_engines]
            found = slices_of(graph, wave_units)
            measure_slices(graph, found, by_key, calls_total, tables_of)
            for s in sorted(found, key=slice_order_key):
                wave_steps.append(("slice", s.engine, number, [by_key[k] for k in s.unit_keys], s))
        if basis == "engine_waves" and len(wave_steps) > 1:
            return None  # a wave of two engines cannot be numbered as one step
        planned.extend(wave_steps)
    if any(u.engine not in placed for u in units):
        # An engine no migration wave names: no step could name its phase wave.
        return None

    steps: list[dict[str, Any]] = []
    stage_label: dict[str, str] = {}
    position: dict[str, tuple[int, int, int]] = {}
    pilots: set[str] = set()
    seen_engines: set[str] = set()
    if whole is not None:
        stage = _stage_dict(
            "1",
            "whole_database",
            [whole],
            _relational_gate(
                str((waves[0].get("moves_from") or ["source"])[0]),
                whole.engine,
                len(whole.owned_tables),
            ),
            calls_total,
        )
        steps.append(
            _step_dict(
                1,
                "relational_step",
                whole.engine,
                [stage],
                new_engine=whole.engine not in in_use,
                movement=(
                    "homogeneous"
                    if waves[0].get("homogeneity") in ("homogeneous", "no_move")
                    else "heterogeneous"
                ),
                phase_wave=int(waves[0]["wave"]),
            )
        )
        stage_label[whole.key], position[whole.key] = "1", (1, 0, 0)
        seen_engines.add(whole.engine)
    for kind, engine, phase, members, found_slice in planned:
        number = len(steps) + 1
        if basis == "engine_waves" and phase != number:
            return None
        if kind == "cache":
            (cache,) = members
            fronts = next((w.get("fronts") for w in waves if int(w["wave"]) == phase), None)
            fronted = str(fronts or relational or engine)
            stages = [
                _stage_dict(
                    f"{number}a",
                    "cache_reads",
                    [cache],
                    _cache_gate(fronted, cache.query_count),
                    calls_total,
                )
            ]
        else:
            stages = _moving_stages(number, engine, members, calls_total)
            if sum(s["unit_count"] for s in stages) > 1:
                pilots.add(stages[0]["unit_keys"][0])
        for stage in stages:
            for i, key in enumerate(stage["unit_keys"]):
                stage_label[key] = stage["label"]
                position[key] = (number, STAGE_RANKS[stage["kind"]], i)
        steps.append(
            _step_dict(
                number,
                kind,
                engine,
                stages,
                new_engine=engine not in in_use and engine not in seen_engines,
                movement="heterogeneous",
                phase_wave=phase,
                slice_key=found_slice.key if found_slice is not None else None,
                metrics=_slice_metrics(found_slice) if found_slice is not None else None,
            )
        )
        seen_engines.add(engine)
    if not steps:
        return None
    step_engines = {s["engine"] for s in steps}
    costs = {e: engine_costs.get(e) for e in sorted(step_engines)}
    if any(c is None for c in costs.values()):
        return None
    stepped = [c for c in caches if c.key in position]
    all_units = ([whole] if whole is not None else []) + units + stepped
    unit_dicts = []
    for u in all_units:
        d = _unit_dict(u, calls_total, graph, tables_of)
        d["pilot"] = u.key in pilots
        unit_dicts.append(d)
    if whole is not None:
        read = {t for q in inputs.queries for t in q.tables}
        steps[0]["table_classes"] = _table_classes(whole, units, stage_label, whole.engine, read)
    dependencies = []
    for (a, b), tie in sorted(graph.ties.items()):
        if a not in position or b not in position:
            continue
        later, earlier = (a, b) if position[a] > position[b] else (b, a)
        dependencies.append(
            {
                "unit_key": later,
                "depends_on": earlier,
                "kinds": tie.kinds(later),
                "edge_weight": tie.weight,
                "tables": tie.tables,
            }
        )
    dependencies.sort(key=lambda d: (d["unit_key"], d["depends_on"]))
    plan = {
        "step_basis": basis,
        "slice_basis": "community" if basis == "slices" else None,
        "plan_steps": steps,
        "step_count": len(steps),
        "wave_phases": [],
        "move_units": unit_dicts,
        "dependencies": dependencies,
        "kept_whole": [_group_dict(g) for g in groups],
        "engine_monthly_cost_usd": costs,
        "engine_critical_high_risks": {
            e: int(risks_by_engine.get(e, 0)) for e in sorted(step_engines)
        },
    }
    plan["wave_phases"] = [
        p.model_dump(mode="json") for p in phase_facts([SliceStep.model_validate(s) for s in steps])
    ]
    return SlicePlanFacts.model_validate(plan).model_dump(mode="json")


def _group_dict(g: KeptWhole) -> dict[str, Any]:
    return {
        "group_id": g.group_id,
        "kind": g.kind,
        "query_ids": list(g.query_ids),
        "table": g.table,
    }
