# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The experimental graph explorer's view overlays (#308): waves, placement units,
who writes each table, and engine cost, read from the job's artifacts.

The context graph does not hold these yet (#306), so they come from the
effective assignment and the synthesis report. Each one is taken as the
pipeline wrote it; nothing here works out a new figure:

- waves: ``report.json`` ``facts.waves`` (each wave's engines). A query moves
  in exactly one wave, its assigned engine's; the cache wave moves nothing,
  it puts a cache in front of reads, so a cached read is "cached in" that wave,
  never "moves in" it. The path to the final engine comes from the wave plan
  (``migration_waves``): a homogeneous (or cross-engine) first wave carries
  ``cutover_query_count``, every query with a move wave, all of which run on
  the first wave's engine once it finishes; a query that moves on later is on
  that engine from wave 1, then moves in its own wave;
- placement units ("moves together"): ``assignment_placement_groups`` of the
  assignment, the units every routing pass places as one (ADR-031 Amendment 9);
- who writes: the graph's WRITES edges say which query writes or locks which
  table (#306); ``report.json`` ``table_sources`` gives each written table's
  source of truth and the read queries served from a replica;
- cost: ``facts.engines[].monthly_cost_usd`` (per engine; there is no
  per-query cost);
- access-pattern class: ``assignment/v<N>/access_patterns.json`` (each
  query's class and the reason routing gave for it).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from src.agents.referee.write_groups import assignment_placement_groups
from src.storage.assignment_versioning import (
    assignment_artifact_path,
    synthesis_report_candidates,
)

logger = logging.getLogger(__name__)

# "read_or_replica": a read the job's report cannot yet say is served from a
# replica, because it was built from another assignment version than the graph's.
Access = Literal["write", "replica_read", "read", "read_or_replica"]

# The view menu. Waves is a filter (and a start point), not a view: it applies
# on top of whichever view is shown.
VIEWS = ("traffic", "moves-together", "who-writes", "cost")


@dataclass(frozen=True)
class Overlays:
    waves: list[dict] = field(default_factory=list)
    # engine -> its one wave. An engine the plan lists in two waves has no
    # single wave: it is left out (and named in ``ambiguous_engines``), so no
    # query of it claims a wave rather than two.
    engine_wave: dict[str, int] = field(default_factory=dict)
    ambiguous_engines: frozenset[str] = frozenset()
    # query -> the one wave it moves in; query -> the cache wave in front of it.
    query_move_wave: dict[str, int] = field(default_factory=dict)
    query_cached_in: dict[str, int] = field(default_factory=dict)
    # query -> its assigned (final) engine, the engine of its move step.
    query_engine: dict[str, str] = field(default_factory=dict)
    # The homogeneous first wave every moving query cuts over in:
    # {"wave": n, "engines": [...]}, or None.
    cutover: dict | None = None
    # The wave plan as the deliverables show it (``migration_waves``): each
    # wave's title, rationale, gate, engines and table groups.
    plan: list[dict] = field(default_factory=list)
    # facts.engines: engine -> {"role", "rationale", "workload_percent", "monthly_cost_usd"}.
    engine_facts: dict[str, dict] = field(default_factory=dict)
    query_units: dict[str, tuple[str, int]] = field(default_factory=dict)
    # table -> {"source_of_truth", "writers", "replica_reads", "replica_engines"}
    table_sources: dict[str, dict] = field(default_factory=dict)
    engine_costs: dict[str, float] = field(default_factory=dict)
    # query -> (access-pattern class, its reason)
    query_classes: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    # The assignment version the report was built from (``assignment_summary.version``).
    report_version: int | None = None

    def views(self) -> list[str]:
        """The views whose data this job has, in menu order (traffic always)."""
        have = {
            "traffic": True,
            "moves-together": bool(self.query_units),
            "who-writes": bool(self.table_sources),
            "cost": bool(self.engine_costs),
        }
        return [v for v in VIEWS if have[v]]


EMPTY = Overlays()


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def build_overlays(
    assignment: Mapping | None, report: Mapping | None, access_patterns: Mapping | None = None
) -> Overlays:
    """The overlays of one job; a missing artifact leaves its views out."""
    access_patterns = access_patterns if isinstance(access_patterns, Mapping) else {}
    query_classes = {
        str(r["query_id"]): (str(r["class"]), r.get("reason"))
        for r in _list(access_patterns.get("queries"))
        if isinstance(r, Mapping) and r.get("query_id") and r.get("class")
    }
    assignment = assignment if isinstance(assignment, Mapping) else {}
    report = report if isinstance(report, Mapping) else {}
    raw_facts = report.get("facts")
    facts: Mapping = raw_facts if isinstance(raw_facts, Mapping) else {}

    waves: list[dict] = []
    seen: dict[str, list[int]] = {}
    for w in _list(facts.get("waves")):
        if not isinstance(w, Mapping) or not isinstance(w.get("wave"), int):
            continue
        engines = [e for e in _list(w.get("engines")) if isinstance(e, str)]
        waves.append({"wave": w["wave"], "engines": engines, "migrating": bool(w.get("migrating"))})
        for engine in engines:
            seen.setdefault(engine, []).append(w["wave"])
    engine_wave = {e: ws[0] for e, ws in seen.items() if len(ws) == 1}
    ambiguous = frozenset(e for e, ws in seen.items() if len(ws) > 1)

    query_move_wave: dict[str, int] = {}
    query_cached_in: dict[str, int] = {}
    query_engine: dict[str, str] = {}
    for qa in _list(assignment.get("query_assignments")):
        if not isinstance(qa, Mapping) or not qa.get("query_id"):
            continue
        qid = str(qa["query_id"])
        move = engine_wave.get(str(qa.get("assigned_engine")))
        if move is not None:
            query_move_wave[qid] = move
            query_engine[qid] = str(qa.get("assigned_engine"))
        cache = qa.get("cache_engine")
        cached = engine_wave.get(str(cache)) if cache and not qa.get("cache_dropped") else None
        if cached is not None and cached != move:
            query_cached_in[qid] = cached
    cutover = _cutover(report, query_move_wave)
    plan = [
        {
            "wave": w["wave"],
            "title": w.get("title"),
            "rationale": w.get("rationale"),
            "gate": w.get("gate"),
            "engines": [e for e in _list(w.get("engines")) if isinstance(e, str)],
            "table_groups": [
                {
                    "tables": [t for t in _list(g.get("tables")) if isinstance(t, str)],
                    "query_count": g.get("query_count"),
                    "kind": g.get("kind"),
                }
                for g in _list(w.get("table_groups"))
                if isinstance(g, Mapping)
            ],
        }
        for w in _list(report.get("migration_waves"))
        if isinstance(w, Mapping) and isinstance(w.get("wave"), int)
    ]
    engine_facts = {
        str(e["engine"]): {
            "role": e.get("role"),
            "rationale": e.get("rationale"),
            "workload_percent": e.get("workload_percent"),
            "monthly_cost_usd": e.get("monthly_cost_usd"),
        }
        for e in _list(facts.get("engines"))
        if isinstance(e, Mapping) and e.get("engine")
    }

    query_units: dict[str, tuple[str, int]] = {}
    for index, unit in enumerate(assignment_placement_groups(assignment), start=1):
        members = [str(q) for q in unit]
        if len(members) < 2:
            continue
        for qid in members:
            query_units.setdefault(qid, (f"unit-{index}", len(members)))

    table_sources: dict[str, dict] = {}
    for ts in _list(report.get("table_sources")):
        if not isinstance(ts, Mapping) or not ts.get("table"):
            continue
        replicas = [r for r in _list(ts.get("replicas")) if isinstance(r, Mapping)]
        table_sources[str(ts["table"])] = {
            "source_of_truth": ts.get("source_of_truth"),
            "writers": {str(q) for q in _list(ts.get("write_query_ids"))},
            "replica_reads": {str(q) for r in replicas for q in _list(r.get("read_query_ids"))},
            "replica_engines": sorted({str(r["engine"]) for r in replicas if r.get("engine")}),
        }

    engine_costs = {
        str(e["engine"]): float(e["monthly_cost_usd"])
        for e in _list(facts.get("engines"))
        if isinstance(e, Mapping)
        and e.get("engine")
        and isinstance(e.get("monthly_cost_usd"), (int, float))
    }
    return Overlays(
        waves=waves,
        engine_wave=engine_wave,
        ambiguous_engines=ambiguous,
        query_move_wave=query_move_wave,
        query_cached_in=query_cached_in,
        query_engine=query_engine,
        cutover=cutover,
        plan=plan,
        engine_facts=engine_facts,
        query_units=query_units,
        table_sources=table_sources,
        engine_costs=engine_costs,
        query_classes=query_classes,
        report_version=(
            summary.get("version")
            if isinstance(summary := report.get("assignment_summary"), Mapping)
            and isinstance(summary.get("version"), int)
            else None
        ),
    )


def _read_first(store: Any, keys: list[str]) -> dict | None:
    for key in keys:
        try:
            if store.exists(key):
                data = store.read_json(key)
                return data if isinstance(data, dict) else None
        except Exception as exc:  # noqa: BLE001 - an unreadable artifact leaves its views out
            logger.warning("graph explorer could not read %s: %s", key, exc)
            return None
    return None


def read_overlays(store: Any, db_name: str, job_id: str, version: int) -> Overlays:
    """The overlays of one job at its effective assignment ``version`` (0: none),
    read from its artifact store; a missing or unreadable artifact leaves its
    views out."""
    assignment = (
        _read_first(store, [assignment_artifact_path(db_name, job_id, version)])
        if version
        else None
    )
    try:
        report_keys = synthesis_report_candidates(store, db_name, job_id)
    except Exception:  # noqa: BLE001
        report_keys = []
    access_patterns = (
        _read_first(store, [f"{db_name}/{job_id}/assignment/v{version}/access_patterns.json"])
        if version
        else None
    )
    return build_overlays(assignment, _read_first(store, report_keys), access_patterns)


def replicas_pending(overlays: Overlays, graph_version: int | None) -> bool:
    """True when the report's replica reads may be stale: it was built from another
    assignment version than the graph's effective one (a reroute or a customer edit
    since, with synthesis not run again yet)."""
    return (
        bool(overlays.table_sources)
        and isinstance(overlays.report_version, int)
        and bool(graph_version)
        and overlays.report_version != graph_version
    )


def access_of(
    overlays: Overlays, query_id: str, table: str, writes: bool, pending: bool = False
) -> Access:
    """How ``query_id`` uses ``table``: "write" when it writes or locks it (the
    graph's WRITES edge, ``writes``, #306); for a read, "read_or_replica" when the
    report's replica reads may be stale (``pending``), "replica_read" when the job's
    report serves it from a replica kept in sync, else "read"."""
    if writes:
        return "write"
    if pending:
        return "read_or_replica"
    ts = overlays.table_sources.get(table)
    if ts and query_id in ts["replica_reads"]:
        return "replica_read"
    return "read"


def bundle_access(
    overlays: Overlays, table: str, query_ids: list[str], writers: set[str], pending: bool = False
) -> Access:
    """A bundle's access: "write" when one of its queries writes or locks the table
    (``writers``, from the graph), "read_or_replica" when the report's replica reads
    may be stale (``pending``), "replica_read" when every one reads a replica copy,
    else "read"."""
    kinds = {access_of(overlays, q, table, q in writers, pending) for q in query_ids}
    if "write" in kinds:
        return "write"
    if pending:
        return "read_or_replica"
    if kinds == {"replica_read"}:
        return "replica_read"
    return "read"


def _cutover(report: Mapping, query_move_wave: dict[str, int]) -> dict | None:
    """The wave plan's homogeneous first wave, when its ``cutover_query_count``
    is exactly the queries that have a move wave; otherwise None (no path is
    shown rather than one the plan does not back)."""
    plan = [w for w in _list(report.get("migration_waves")) if isinstance(w, Mapping)]
    first = next((w for w in plan if isinstance(w.get("cutover_query_count"), int)), None)
    if first is None or not isinstance(first.get("wave"), int):
        return None
    engines = [e for e in _list(first.get("engines")) if isinstance(e, str)]
    if len(engines) != 1 or first["cutover_query_count"] != len(query_move_wave):
        return None
    return {"wave": first["wave"], "engines": engines}


def query_path(overlays: Overlays, query_id: str) -> list[dict]:
    """The query's wave path: ``[{"wave", "engine", "kind"}]`` with kind
    "cutover" (it runs on the first wave's engine from that wave) and "move"
    (the wave it reaches its final engine in). Empty without a move wave."""
    move = overlays.query_move_wave.get(query_id)
    if move is None:
        return []
    engine = overlays.query_engine.get(query_id)
    steps = []
    cut = overlays.cutover
    if cut and move > cut["wave"]:
        steps.append({"wave": cut["wave"], "engine": cut["engines"][0], "kind": "cutover"})
    return steps + [{"wave": move, "engine": engine, "kind": "move"}]


def bundle_wave(overlays: Overlays, query_ids: list[str]) -> int | None:
    """A bundle's move wave (one table to one engine: one wave), or None."""
    found = {overlays.query_move_wave[q] for q in query_ids if q in overlays.query_move_wave}
    return found.pop() if len(found) == 1 else None


def bundle_cached_in(overlays: Overlays, query_ids: list[str]) -> int | None:
    """The cache wave in front of some of a bundle's reads, or None."""
    found = {overlays.query_cached_in[q] for q in query_ids if q in overlays.query_cached_in}
    return min(found) if found else None


def wave_query_ids(overlays: Overlays, wave: int) -> list[str]:
    """The queries that move in ``wave``, or that a cache in ``wave`` fronts
    (in id order). The cutover wave's other passengers are not listed: that
    would be the whole workload."""
    return sorted(
        q
        for q in {*overlays.query_move_wave, *overlays.query_cached_in}
        if overlays.query_move_wave.get(q) == wave or overlays.query_cached_in.get(q) == wave
    )
