# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The units the design split keeps whole (#2141).

Routing places each placement unit as one (``write_groups.placement_groups``: a
co-dependency group, a table's write group and a routine unit, merged where they share
a statement, ADR-031 Amendment 9). The design split (``group_splitter``) keeps the
statements of each unit in one design part, so a group routing placed together is
designed together:

- :func:`placement_units` gives each merged unit an id (``unit:<its first statement>``)
  and the kinds of source it was merged from;
- :class:`SplitUnits` is one split's view of them: which splitter items (statements, or
  DynamoDB's query pattern units) must sit in one part, the cuts the split had to make,
  and their reasons;
- :func:`weakest_cut` picks where to cut a unit that is too large for one part by
  itself: between the two sides that share the fewest tables, then the fewest statements
  touching a shared table.

A unit is cut only when it alone is over a part's size (``over_part_size``) or input
page budget (``over_input_budget``), or when a post-schema reroute keeps the parts of
the version it came from (``kept_prior_parts``, #1650). Every cut is recorded in the
groups manifest's ``unit_cuts``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from src.agents.schema_design.pattern_input import members

#: The kinds of source a placement unit is merged from, in the order a ``kind`` lists them.
KIND_CO_DEPENDENCY = "co_dependency"
KIND_WRITE_GROUP = "write_group"
KIND_ROUTINE_UNIT = "routine_unit"
KINDS = (KIND_CO_DEPENDENCY, KIND_WRITE_GROUP, KIND_ROUTINE_UNIT)

#: Why a unit was cut (``UnitCut.reason``).
REASON_PART_SIZE = "over_part_size"
REASON_INPUT_BUDGET = "over_input_budget"
REASON_KEPT_PRIOR_PARTS = "kept_prior_parts"

# Block keys sit far above any query pattern index, so the splitter can key items by
# either in one map.
_BLOCK_BASE = 1 << 40


@dataclass(frozen=True)
class PlacementUnit:
    """One unit routing places as one: its id, the kinds it is merged from, its statements."""

    unit_id: str
    kind: str
    query_ids: tuple[str, ...]


def unit_id_of(query_ids: Sequence[str]) -> str:
    """A unit's id: its first statement's, as ``placement_groups`` orders them."""
    return f"unit:{query_ids[0]}"


def placement_units(
    co_dependency_groups: Iterable[Iterable[str]],
    write_groups: Mapping[str, Iterable[str]] | None,
    routine_units: Mapping[str, Iterable[str]] | None = None,
) -> list[PlacementUnit]:
    """``write_groups.placement_groups`` with each unit's id and kinds (``+``-joined, in
    :data:`KINDS` order)."""
    from src.agents.referee.write_groups import placement_groups

    co = [[str(q) for q in g] for g in co_dependency_groups]
    writes = {t: [str(q) for q in m] for t, m in (write_groups or {}).items()}
    routines = {r: [str(q) for q in m] for r, m in (routine_units or {}).items()}
    sources: list[tuple[str, set[str]]] = [(KIND_CO_DEPENDENCY, set(g)) for g in co]
    sources += [(KIND_WRITE_GROUP, set(m)) for m in writes.values() if len(m) >= 2]
    sources += [(KIND_ROUTINE_UNIT, set(m)) for m in routines.values() if len(m) >= 2]
    units: list[PlacementUnit] = []
    for unit in placement_groups(co, writes, routines):
        ids = set(unit)
        kinds = [k for k in KINDS if any(kind == k and s & ids for kind, s in sources)]
        units.append(PlacementUnit(unit_id_of(unit), "+".join(kinds), tuple(unit)))
    return units


def assignment_placement_units(
    assignment: Mapping | None, collector_output: Mapping | None = None
) -> list[PlacementUnit]:
    """:func:`placement_units` of an assignment artifact, read as
    ``write_groups.assignment_placement_groups`` reads it."""
    from src.agents.referee.routine_units import routine_units_of
    from src.shared.placeholder_tables import saved_write_groups

    assignment = assignment or {}
    return placement_units(
        assignment.get("co_dependency_groups") or [],
        saved_write_groups(assignment),
        routine_units_of(assignment, collector_output),
    )


def _qid(statement: Mapping[str, Any]) -> str:
    return str(statement.get("query_id") or "")


class SplitUnits:
    """One split's placement units: the items that must share a part, and the cuts made.

    Built from the engine's units and the statements the split designs; a unit with
    fewer than two of them cannot be cut and is left out. Items whose statements are in
    one unit form a block; a DynamoDB query pattern unit whose members are in two
    units joins them into one block (:meth:`absorb`). A block too large for one part is
    cut into pieces (:meth:`cut`), each piece then a block of its own, and the reason
    is recorded for every unit with statements on both sides.
    """

    def __init__(self, units: Iterable[PlacementUnit], statement_ids: Iterable[str]):
        present = {str(q) for q in statement_ids}
        self.units: list[PlacementUnit] = []
        for unit in units:
            ids = tuple(q for q in unit.query_ids if q in present)
            if len(ids) >= 2:
                self.units.append(PlacementUnit(unit.unit_id, unit.kind, ids))
        self._unit_of: dict[str, int] = {
            q: n for n, u in enumerate(self.units) for q in u.query_ids
        }
        self._parent: list[int] = list(range(len(self.units)))
        self._piece: dict[str, int] = {}
        self._next = len(self.units)
        self.reasons: dict[str, str] = {}

    def __bool__(self) -> bool:
        return bool(self.units)

    def _find(self, n: int) -> int:
        while self._parent[n] != n:
            self._parent[n] = self._parent[self._parent[n]]
            n = self._parent[n]
        return n

    def _key_of(self, qid: str) -> int | None:
        if qid in self._piece:
            return self._piece[qid]
        n = self._unit_of.get(qid)
        return None if n is None else _BLOCK_BASE + self._find(n)

    def absorb(self, items: Iterable[Mapping[str, Any]]) -> None:
        """Join into one block the units whose statements one item holds."""
        for item in items:
            found = [self._unit_of[q] for q in map(_qid, members(item)) if q in self._unit_of]
            for n in found[1:]:
                a, b = self._find(found[0]), self._find(n)
                if a != b:
                    self._parent[b] = a

    def unit_members(self, qid: str) -> tuple[str, ...]:
        """The statements of ``qid``'s unit (empty: it is in none)."""
        n = self._unit_of.get(str(qid))
        return () if n is None else self.units[n].query_ids

    def block_key(self, item: Mapping[str, Any]) -> int | None:
        """The block ``item`` belongs to (None: it is in no unit)."""
        for q in map(_qid, members(item)):
            key = self._key_of(q)
            if key is not None:
                return key
        return None

    def record(self, sides: Sequence[Sequence[Mapping[str, Any]]], reason: str) -> None:
        """Record ``reason`` for every unit whose statements ``sides`` (lists of items)
        put on two or more sides; a unit already recorded keeps its first reason."""
        side_of = {
            q: n for n, side in enumerate(sides) for i in side for q in map(_qid, members(i))
        }
        for unit in self.units:
            if len({side_of[q] for q in unit.query_ids if q in side_of}) > 1:
                self.reasons.setdefault(unit.unit_id, reason)

    def cut(
        self, left: Sequence[Mapping[str, Any]], right: Sequence[Mapping[str, Any]], reason: str
    ) -> None:
        """A block cut into ``left`` and ``right``: record it, and make ``right`` a block
        of its own, so a later cut between the two is no new cut."""
        self.record([left, right], reason)
        key = _BLOCK_BASE + self._next
        self._next += 1
        for item in right:
            for q in map(_qid, members(item)):
                if q in self._unit_of:
                    self._piece[q] = key

    def unit_cuts(self, parts: Sequence[Sequence[str]]) -> list[dict[str, Any]]:
        """One entry per unit whose statements the final ``parts`` (statement ids per
        part, in part order) hold in two or more parts and whose cut was recorded."""
        part_of: dict[str, int] = {}
        for n, ids in enumerate(parts):
            for q in ids:
                part_of.setdefault(q, n)
        cuts: list[dict[str, Any]] = []
        for unit in self.units:
            held = sorted({part_of[q] for q in unit.query_ids if q in part_of})
            if len(held) > 1 and unit.unit_id in self.reasons:
                cuts.append(
                    {
                        "unit_id": unit.unit_id,
                        "kind": unit.kind,
                        "query_ids": list(unit.query_ids),
                        "parts": held,
                        "reason": self.reasons[unit.unit_id],
                    }
                )
        return cuts


def weakest_cut[Item: Mapping[str, Any]](
    items: Sequence[Item],
    allowed: Iterable[int],
    tables_of: Callable[[Item], set[str]],
) -> int:
    """The position among ``allowed`` (1 to ``len(items) - 1``) to cut ``items`` at:
    the two sides share the fewest tables, then the fewest statements on either side
    touch a shared table, then the cut nearest the middle, then the first."""
    tables = [tables_of(i) for i in items]
    sizes = [len(members(i)) for i in items]
    mid = (len(items) + 1) // 2
    best: tuple[int, int, int, int] | None = None
    for c in sorted(set(allowed)):
        if not 0 < c < len(items):
            continue
        left: set[str] = set().union(*tables[:c])
        right: set[str] = set().union(*tables[c:])
        shared = left & right
        crossing = sum(s for t, s in zip(tables, sizes, strict=True) if t & shared)
        rank = (len(shared), crossing, abs(c - mid), c)
        if best is None or rank < best:
            best = rank
    if best is None:
        return mid
    return best[3]


def by_table[Item: Mapping[str, Any]](
    items: Sequence[Item], primary_of: Callable[[Item], str]
) -> list[Item]:
    """``items`` with each primary table's items next to each other, the tables in the
    order of their first item (stable), so a cut between neighbours separates tables."""
    order: dict[str, int] = {}
    for item in items:
        order.setdefault(primary_of(item), len(order))
    return sorted(items, key=lambda i: order[primary_of(i)])


def is_block_key(key: int) -> bool:
    """Whether a splitter item key (:meth:`SplitUnits.block_key`) names a block, not a
    query pattern."""
    return key >= _BLOCK_BASE
