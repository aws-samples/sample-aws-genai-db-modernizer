# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Move units: the smallest sets of queries and tables that move to one engine on their
own (#1129, epic #1128).

A move unit belongs to one engine the relational step does not keep (a purpose-built
target). It is built from the effective assignment alone, never by a model:

1. Its seeds are the groups routing placed as one (``KeptWhole``): co-dependency groups,
   write groups of two or more statements and routine units
   (``write_groups.placement_groups``' sources, ADR-031 Amendments 9 and 21), and each
   routine's temporary-object statements with the statements they follow (Amendment 20,
   ``report.json``'s ``routine_followers``). A unit holds such a group whole or not at all.
2. A table's one writer owns it (Amendment 9): the engine all of its writes sit on
   (``write_groups.sources_of_truth``). The engine's queries are joined over the tables
   it owns, so every query on an owned table, its writes included, is in the unit that
   owns the table. A query that touches no owned table reads only copies (replica reads)
   and is joined with the other such queries over the tables they read.
3. Each unit lists the tables it owns, the tables it reads as a copy (and their writer),
   the reads elsewhere of the tables it owns (its reverse replicas), its call share, and
   the open critical or high risks that hold it.

The cache's fronted reads form one unit per cache engine. Nothing here records a Move or
re-scores a query: units are a projection of the assignment, so routing never reads this
module.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from src.shared.cache_engines import CACHE_ENGINES
from src.shared.migration_wave_engines import SEARCH_ENGINES
from src.shared.table_writer import table_writer

__all__ = [
    "KeptWhole",
    "SliceInputs",
    "SliceQuery",
    "Unit",
    "build_cache_units",
    "build_units",
    "kept_whole_groups",
]


@dataclass(frozen=True)
class SliceQuery:
    """One in-scope query with an engine: what the slice plan reads of it."""

    query_id: str
    engine: str
    tables: tuple[str, ...] = ()
    cache_engine: str | None = None
    calls_per_second: float = 0.0


@dataclass(frozen=True)
class SliceInputs:
    """What the slice plan reads of the assignment and the collection (#1129, #1698).

    Built once in synthesis (``src.agents.referee.slice_inputs``) and passed to the facts
    layer, so the plan is computed from the same effective assignment every other figure
    is. ``writes_by_table`` is each table's writes and locking reads (the assignment's
    write groups); ``owners`` each written table's one writer (``write_groups.
    sources_of_truth``, ADR-031 Amendment 9)."""

    queries: tuple[SliceQuery, ...] = ()
    writes_by_table: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    owners: Mapping[str, str] = field(default_factory=dict)
    co_dependency_groups: tuple[tuple[str, ...], ...] = ()
    routine_units: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    routine_followers: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    foreign_keys: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class KeptWhole:
    """A group routing placed as one, so a move unit holds all of it or none of it."""

    group_id: str
    kind: str  # write_group, co_dependency, routine_unit
    query_ids: tuple[str, ...]
    table: str | None = None


def kept_whole_groups(inputs: SliceInputs) -> list[KeptWhole]:
    """The groups no move unit splits, restricted to the in-scope queries with an engine:
    ``write_groups.placement_groups``' sources (co-dependency groups, write groups of two or
    more, routine units), and each routine's statements with the temporary-object
    statements that follow it (ADR-031 Amendment 20), merged into its routine unit's group
    when it has one. A group left with no query is dropped."""
    placed = {q.query_id for q in inputs.queries}

    def members(ids: Iterable[str]) -> tuple[str, ...]:
        return tuple(q for q in dict.fromkeys(str(i) for i in ids) if q in placed)

    out: list[KeptWhole] = []
    for n, group in enumerate(inputs.co_dependency_groups, start=1):
        if len(group) >= 2 and (ids := members(group)):
            out.append(KeptWhole(f"co_dependency:{n}", "co_dependency", ids))
    for table, group in sorted(inputs.writes_by_table.items()):
        if len(group) >= 2 and (ids := members(group)):
            out.append(KeptWhole(f"write:{table}", "write_group", ids, table))
    routines = sorted(set(inputs.routine_units) | set(inputs.routine_followers))
    for routine in routines:
        unit = inputs.routine_units.get(routine, ())
        follow = inputs.routine_followers.get(routine, ())
        if len(unit) < 2 and not follow:
            continue
        ids = members([*unit, *follow])
        if len(ids) >= 1:
            out.append(KeptWhole(f"routine:{routine}", "routine_unit", ids))
    return out


@dataclass
class Unit:
    """A move unit while the plan is built; ``SlicePlanFacts`` validates the result."""

    key: str
    engine: str
    kind: str
    query_ids: list[str]
    write_query_ids: list[str]
    owned_tables: list[str]
    read_tables: list[str]
    replicated_in: list[tuple[str, str]]  # (table, its writer)
    reverse_replicas: list[tuple[str, list[str], list[str]]]  # (engine, tables, reads)
    kept_whole_ids: list[str]
    calls_per_second: float
    holding_risk_ids: list[str] = field(default_factory=list)

    @property
    def query_count(self) -> int:
        return len(self.query_ids)

    @property
    def reverse_read_count(self) -> int:
        return sum(len(reads) for _, _, reads in self.reverse_replicas)

    @property
    def blast_radius(self) -> int:
        """What the move touches: its queries plus its reverse-replica reads."""
        return self.query_count + self.reverse_read_count


class _Union:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        root = x
        while self.parent.setdefault(root, root) != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def join(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # The smaller id is the root, so the result does not depend on input order.
            lo, hi = sorted((ra, rb))
            self.parent[hi] = lo


def build_units(
    inputs: SliceInputs,
    relational: str | None,
    groups: list[KeptWhole],
    risks: Iterable[Mapping[str, Any]] = (),
) -> list[Unit]:
    """The move units of every engine but ``relational`` and the cache engines, sorted
    by key. Deterministic, and the same for the queries in any order."""
    owner = inputs.owners
    writes = {q for members in inputs.writes_by_table.values() for q in members}
    by_engine: dict[str, list[SliceQuery]] = {}
    for q in sorted(inputs.queries, key=lambda q: q.query_id):
        by_engine.setdefault(q.engine, []).append(q)
    seeds = [g.query_ids for g in groups]
    units: list[Unit] = []
    for engine, queries in sorted(by_engine.items()):
        if engine == relational or engine in CACHE_ENGINES:
            continue
        units.extend(_engine_units(engine, queries, owner, seeds, writes, relational))
    _attach_reverse_replicas(units, inputs, owner, writes)
    held = {g.group_id: set(g.query_ids) for g in groups}
    for unit in units:
        ids = set(unit.query_ids)
        unit.kept_whole_ids = sorted(g for g, members in held.items() if members & ids)
    _attach_risks(units, risks)
    return sorted(units, key=lambda u: u.key)


def _engine_units(
    engine: str,
    queries: list[SliceQuery],
    owner: Mapping[str, str],
    seeds: list[tuple[str, ...]],
    writes: set[str],
    relational: str | None,
) -> list[Unit]:
    ids = {q.query_id for q in queries}
    union = _Union()
    for q in queries:
        union.find(q.query_id)
    for seed in seeds:
        mine = sorted(qid for qid in seed if qid in ids)
        for left, right in zip(mine, mine[1:], strict=False):
            union.join(left, right)
    # Join over the tables each query anchors on: the owned tables it touches, or, for a
    # query that touches none, the copies it reads.
    anchor: dict[str, str] = {}
    for q in queries:
        owned = [t for t in q.tables if owner.get(t) == engine]
        for t in owned or [f"read:{t}" for t in q.tables]:
            if t in anchor:
                union.join(anchor[t], q.query_id)
            else:
                anchor[t] = q.query_id
    members: dict[str, list[SliceQuery]] = {}
    for q in queries:
        members.setdefault(union.find(q.query_id), []).append(q)
    out = []
    for group in members.values():
        tables = sorted({t for q in group for t in q.tables})
        owned = [t for t in tables if owner.get(t) == engine]
        read = [t for t in tables if t not in owned]
        if owned:
            kind = "owner"
        elif engine in SEARCH_ENGINES:
            kind = "read_model"
        else:
            kind = "replica_reads"
        first = (owned or read or [f"statement-{min(q.query_id for q in group)}"])[0]
        out.append(
            Unit(
                key=f"{engine}:{first}",
                engine=engine,
                kind=kind,
                query_ids=sorted(q.query_id for q in group),
                write_query_ids=sorted(q.query_id for q in group if q.query_id in writes),
                owned_tables=owned,
                read_tables=tables,
                replicated_in=[(t, table_writer(t, owner, relational)) for t in read],
                reverse_replicas=[],
                kept_whole_ids=[],
                calls_per_second=sum(q.calls_per_second for q in group),
            )
        )
    return out


def _attach_reverse_replicas(
    units: list[Unit], inputs: SliceInputs, owner: Mapping[str, str], writes: set[str]
) -> None:
    """Each unit's reverse replicas: the reads on other engines of the tables it owns."""
    owned_by = {t: u for u in units for t in u.owned_tables}
    reads: dict[str, dict[str, dict[str, set[str]]]] = {}
    for q in inputs.queries:
        if q.query_id in writes:
            continue
        for t in q.tables:
            unit = owned_by.get(t)
            if unit is None or q.engine == unit.engine:
                continue
            reads.setdefault(unit.key, {}).setdefault(q.engine, {}).setdefault(t, set()).add(
                q.query_id
            )
    for unit in units:
        unit.reverse_replicas = [
            (engine, sorted(by_table), sorted({q for qs in by_table.values() for q in qs}))
            for engine, by_table in sorted(reads.get(unit.key, {}).items())
        ]


def _attach_risks(units: list[Unit], risks: Iterable[Mapping[str, Any]]) -> None:
    """The open critical and high risks that hold each unit: a risk on the unit's engine
    names one of its queries, or, naming none, one of the tables its queries read."""
    for risk in risks:
        engine = str(risk.get("engine") or "")
        rid = str(risk.get("risk_id") or "")
        if not rid:
            continue
        query_ids = {str(q) for q in risk.get("query_ids") or []}
        tables = {str(t) for t in risk.get("affected_tables") or []}
        for unit in units:
            if unit.engine != engine:
                continue
            hit = (
                bool(query_ids & set(unit.query_ids))
                if query_ids
                else bool(tables & set(unit.read_tables))
            )
            if hit and rid not in unit.holding_risk_ids:
                unit.holding_risk_ids.append(rid)
    for unit in units:
        unit.holding_risk_ids.sort()


def build_cache_units(inputs: SliceInputs) -> list[Unit]:
    """One unit per cache engine: the reads it fronts, which keep running on their owner."""
    by_cache: dict[str, list[SliceQuery]] = {}
    for q in sorted(inputs.queries, key=lambda q: q.query_id):
        if q.cache_engine:
            by_cache.setdefault(q.cache_engine, []).append(q)
    out = []
    for cache, queries in sorted(by_cache.items()):
        tables = sorted({t for q in queries for t in q.tables})
        first = (tables or [f"statement-{queries[0].query_id}"])[0]
        out.append(
            Unit(
                key=f"{cache}:{first}",
                engine=cache,
                kind="cache_reads",
                query_ids=[q.query_id for q in queries],
                write_query_ids=[],
                owned_tables=[],
                read_tables=tables,
                replicated_in=[],
                reverse_replicas=[],
                kept_whole_ids=[],
                calls_per_second=sum(q.calls_per_second for q in queries),
            )
        )
    return out
