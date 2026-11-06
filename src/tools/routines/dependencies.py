# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Dependency checks and dependency order for one step's routines (#1937), no model.

The input is one :class:`RoutineNode` per routine: its id, kind, schema and name, and the
tables and routines it names, as the checkers record them:

- MySQL (:func:`mysql_node`): the verdict's ``dependencies`` and
  ``dependencies_complete`` (:func:`~src.tools.routines.mysql_checks.check_mysql_routine`).
- PostgreSQL (:func:`postgresql_node`): a trigger's table and function, from its
  ``CREATE TRIGGER``; a function's or procedure's are the ones the collection gives
  (``pg_depend``). PostgreSQL records none for a PL/pgSQL body, so a function's list is
  incomplete unless the caller says otherwise.

An unqualified name is the routine's own schema. Names are lower case. A routine is
keyed by its kind as well as its name, so a procedure and a function of one name, or a
trigger named like a routine, are never taken for each other: a dependency whose kind the
text tells (``RoutineDependency.routine_kind``) matches only that kind, and one whose kind
it does not tell matches a function or a procedure, never a trigger or an event.

Two outputs, each with no literal in it (names are identifiers):

- :func:`check_dependencies`: the dependency check rule of
  ``src/catalog/routine_checks/`` for the routine's engine, given the objects the step
  moves (:class:`MovedObjects`). It fails, naming each one, when the routine names a table
  or routine the step does not move. When the routine's list is incomplete and nothing it
  names is missing, the outcome is ``unknown``, never ``pass``: the dependencies are
  confirmed when the routine loads. An ``unknown`` does not block (every PL/pgSQL function
  would otherwise), and :func:`with_dependency_check` puts it in the routine's verdict,
  where ``unknown_rules`` lists it.
- :func:`dependency_order`: the ``dependency_order`` rewrite rule of
  ``src/catalog/routine_rewrites/``: every routine after the routines of the step it
  names (a trigger after its function, a MySQL trigger after the one it ``FOLLOWS``),
  ties in routine id order whatever order the input is in. A dependency cycle is
  reported in ``cycles`` and never followed round: its routines are placed together, in
  routine id order, where the cycle as a whole falls. A routine calling itself is no
  cycle. Tables are created before any routine in the step, so they add no order.
  :class:`DependencyOrder` records the rule, its reason and a diff from the input order.

The order runs in time linear in the routines and dependencies, apart from the
id-ordered queue (a heap) that breaks ties. The same input always gives the same output.
"""

from __future__ import annotations

import heapq
from collections.abc import Iterable, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.shared.catalog.routine_checks import RoutineCheckRule, RoutineKind, routine_check_rules
from src.shared.catalog.routine_rewrites import RoutineRewriteRule, routine_rewrite_rules
from src.tools.routines import postgresql_checks
from src.tools.routines.mysql_checks import MySqlRoutine
from src.tools.routines.postgresql_checks import PostgresRoutine
from src.tools.routines.verdict import RoutineCheckResult, RoutineDependency, RoutineVerdict

__all__ = [
    "DependencyOrder",
    "MovedObjects",
    "RoutineNode",
    "check_dependencies",
    "dependency_order",
    "mysql_node",
    "postgresql_node",
    "with_dependency_check",
]

_RECORD = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
SourceEngine = Literal["postgresql", "mysql"]
ObjectName = tuple[str, str]
# A routine's key: its kind, schema and name.
RoutineKey = tuple[str, str, str]
# The kinds a dependency whose kind the text does not tell may be.
_UNTOLD_KINDS: tuple[RoutineKind, ...] = ("function", "procedure")
_UNKNOWN_FINDING = (
    "dependencies not fully known: the source does not record every object the routine "
    "names, so they are confirmed when the routine loads"
)


def _lower(value: str | None) -> str | None:
    text = (value or "").strip().lower()
    return text or None


class RoutineNode(BaseModel):
    """One routine of a step, for the dependency check and the order."""

    model_config = _RECORD

    routine_id: str = Field(min_length=1)
    source_engine: SourceEngine
    kind: RoutineKind
    schema_name: str | None
    name: str = Field(min_length=1)
    dependencies: tuple[RoutineDependency, ...] = ()
    dependencies_complete: bool = True

    @field_validator("schema_name", "name")
    @classmethod
    def _lower_names(cls, value: str | None) -> str | None:
        return _lower(value)

    def resolve(self, dependency: RoutineDependency) -> ObjectName:
        """A dependency's ``(schema, name)``; an unqualified one is in this routine's schema."""
        return (_lower(dependency.schema_name) or self.schema_name or "", dependency.name.lower())

    def resolve_routine(self, dependency: RoutineDependency) -> tuple[RoutineKey, ...]:
        """The keys a routine dependency may name: its own kind when the text tells it,
        else a function or a procedure."""
        schema, name = self.resolve(dependency)
        kinds = (dependency.routine_kind,) if dependency.routine_kind else _UNTOLD_KINDS
        return tuple((kind, schema, name) for kind in kinds)

    @property
    def key(self) -> RoutineKey:
        return (self.kind, self.schema_name or "", self.name)


class MovedObjects(BaseModel):
    """The tables one step moves, each ``(schema, name)``, and its routines, each
    ``(kind, schema, name)``, lower case."""

    model_config = _RECORD

    tables: frozenset[ObjectName] = frozenset()
    routines: frozenset[RoutineKey] = frozenset()

    @field_validator("tables", mode="before")
    @classmethod
    def _lower_pairs(cls, value: Iterable[Sequence[str]]) -> frozenset[ObjectName]:
        out: set[ObjectName] = set()
        for pair in value:
            schema, name = pair
            out.add(((schema or "").strip().lower(), name.strip().lower()))
        return frozenset(out)

    @field_validator("routines", mode="before")
    @classmethod
    def _lower_triples(cls, value: Iterable[Sequence[str]]) -> frozenset[RoutineKey]:
        out: set[RoutineKey] = set()
        for triple in value:
            kind, schema, name = triple
            out.add((kind, (schema or "").strip().lower(), name.strip().lower()))
        return frozenset(out)

    @classmethod
    def named(
        cls,
        tables: Iterable[Sequence[str]] = (),
        routines: Iterable[Sequence[str]] = (),
    ) -> MovedObjects:
        """The objects a step moves: tables as ``(schema, name)`` and routines as
        ``(kind, schema, name)``, in any case."""
        return cls.model_validate({"tables": list(tables), "routines": list(routines)})

    @classmethod
    def of(cls, nodes: Iterable[RoutineNode], tables: Iterable[Sequence[str]]) -> MovedObjects:
        """The objects a step moves: ``tables`` and the routines of ``nodes``."""
        return cls.named(tables=tables, routines=[n.key for n in nodes])


class DependencyOrder(BaseModel):
    """The order a step creates its routines in, and the cycles found on the way.

    - ``rule_id``, ``reason``: the ``dependency_order`` rewrite rule's.
    - ``order``: every routine id, each after the routines of the step it names.
    - ``cycles``: each dependency cycle's routine ids, sorted; the cycles sorted.
    - ``diff``: the unified diff from the input order to ``order`` (zero context lines;
      a single line ``unchanged`` when they are the same), one routine id per line.
    """

    model_config = _RECORD

    rule_id: str
    reason: str
    order: tuple[str, ...]
    cycles: tuple[tuple[str, ...], ...] = ()
    diff: str


# --------------------------------------------------------------------------- nodes


def mysql_node(routine: MySqlRoutine, verdict: RoutineVerdict) -> RoutineNode:
    """A MySQL routine's node, its dependencies from its checker verdict."""
    if verdict.routine_id != routine.routine_id or verdict.kind != routine.kind:
        raise ValueError("the verdict is not this routine's")
    schema, name = routine.schema_and_name
    return RoutineNode(
        routine_id=routine.routine_id,
        source_engine="mysql",
        kind=routine.kind,
        schema_name=schema,
        name=name,
        dependencies=verdict.dependencies,
        dependencies_complete=verdict.dependencies_complete,
    )


def postgresql_node(
    routine: PostgresRoutine,
    *,
    schema_name: str | None,
    name: str,
    dependencies: Iterable[RoutineDependency] = (),
    dependencies_complete: bool | None = None,
) -> RoutineNode:
    """A PostgreSQL routine's node. ``schema_name`` and ``name`` are the routine's own (a
    trigger's: its table's schema and its own name, as the collection gives them);
    ``dependencies`` are the collection's (``pg_depend``). A trigger adds the table it is
    on and the function it runs, read from its statement; its list is complete when the
    statement parses. A function's or procedure's list is complete only when
    ``dependencies_complete`` says so."""
    deps = {
        (d.kind, _lower(d.schema_name), d.name.lower(), d.routine_kind): d for d in dependencies
    }
    complete = bool(dependencies_complete)
    if routine.kind == "trigger":
        refs = postgresql_checks.trigger_references(routine.create_sql)
        for ref in refs or ():
            deps.setdefault((ref.kind, ref.schema_name, ref.name, ref.routine_kind), ref)
        complete = (
            refs is not None if dependencies_complete is None else complete and refs is not None
        )
    return RoutineNode(
        routine_id=routine.routine_id,
        source_engine="postgresql",
        kind=routine.kind,
        schema_name=schema_name,
        name=name,
        dependencies=tuple(
            deps[k] for k in sorted(deps, key=lambda k: (k[0], k[1] or "", k[2], k[3] or ""))
        ),
        dependencies_complete=complete,
    )


# --------------------------------------------------------------------------- the check


def _dependency_rule(
    node: RoutineNode, target_engine: str, rules: Iterable[RoutineCheckRule] | None
) -> RoutineCheckRule | None:
    for rule in routine_check_rules() if rules is None else rules:
        if (
            rule.match.check == "dependencies"
            and node.source_engine in rule.source_engines
            and target_engine in rule.targets
            and node.kind in rule.kinds
        ):
            return rule
    return None


def check_dependencies(
    node: RoutineNode,
    moved: MovedObjects,
    *,
    target_engine: str,
    rules: Iterable[RoutineCheckRule] | None = None,
) -> RoutineCheckResult | None:
    """The dependency check's result for ``node`` in a step that moves ``moved``: a fail
    naming each table (``table schema.name``) and routine (``routine schema.name``) it
    names that the step does not move; ``unknown`` (with a finding saying why) when its
    list is incomplete and nothing it names is missing; else a pass. None when no
    dependency rule applies to its engine, ``target_engine`` and kind. ``rules`` defaults
    to the catalog's."""
    rule = _dependency_rule(node, target_engine, rules)
    if rule is None:
        return None
    missing: list[str] = []
    for dependency in node.dependencies:
        schema, name = node.resolve(dependency)
        if dependency.kind == "table":
            found_it = (schema, name) in moved.tables
        else:
            found_it = any(key in moved.routines for key in node.resolve_routine(dependency))
        if not found_it:
            missing.append(
                f"{dependency.kind} {schema}.{name}" if schema else f"{dependency.kind} {name}"
            )
    found = tuple(sorted(set(missing)))
    outcome: Literal["pass", "fail", "unknown"] = (
        "fail" if found else ("pass" if node.dependencies_complete else "unknown")
    )
    return RoutineCheckResult(
        rule_id=rule.id,
        check="dependencies",
        outcome=outcome,
        impact=rule.impact,
        reason=rule.reason,
        fix_hint=rule.fix_hint,
        findings=found if found else ((_UNKNOWN_FINDING,) if outcome == "unknown" else ()),
    )


def with_dependency_check(
    verdict: RoutineVerdict,
    node: RoutineNode,
    moved: MovedObjects,
    *,
    rules: Iterable[RoutineCheckRule] | None = None,
) -> RoutineVerdict:
    """``verdict`` with the dependency check's result for ``node`` added to its checks
    (in place of any earlier one for the same rule), so a fail counts toward
    ``needs_attention`` and an ``unknown`` shows in ``unknown_rules``. Unchanged when no
    dependency rule applies; a verdict for another routine is a ValueError."""
    if verdict.routine_id != node.routine_id or verdict.kind != node.kind:
        raise ValueError("the verdict is not this routine's")
    result = check_dependencies(node, moved, target_engine=verdict.target_engine, rules=rules)
    if result is None:
        return verdict
    checks = tuple(c for c in verdict.checks if c.rule_id != result.rule_id) + (result,)
    return verdict.model_copy(update={"checks": checks})


# --------------------------------------------------------------------------- the order


def _strongly_connected(edges: Sequence[Sequence[int]]) -> list[list[int]]:
    """Tarjan's strongly connected components of a graph given as adjacency lists, without
    recursion (a long chain of routines cannot exhaust the stack). Linear in its size."""
    count = len(edges)
    index = [-1] * count
    low = [0] * count
    on_stack = [False] * count
    stack: list[int] = []
    components: list[list[int]] = []
    counter = 0
    for root in range(count):
        if index[root] != -1:
            continue
        work: list[tuple[int, int]] = [(root, 0)]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack[root] = True
        while work:
            node, i = work[-1]
            if i < len(edges[node]):
                work[-1] = (node, i + 1)
                child = edges[node][i]
                if index[child] == -1:
                    index[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack[child] = True
                    work.append((child, 0))
                elif on_stack[child]:
                    low[node] = min(low[node], index[child])
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                component: list[int] = []
                while True:
                    member = stack.pop()
                    on_stack[member] = False
                    component.append(member)
                    if member == node:
                        break
                components.append(component)
    return components


def _order_rule(
    nodes: Sequence[RoutineNode], rules: Iterable[RoutineRewriteRule] | None
) -> RoutineRewriteRule:
    engines = {n.source_engine for n in nodes}
    for rule in routine_rewrite_rules() if rules is None else rules:
        if rule.match.rewrite == "dependency_order" and engines <= set(rule.source_engines):
            return rule
    raise ValueError("no dependency_order rule applies to these routines")


def dependency_order(
    nodes: Iterable[RoutineNode], *, rules: Iterable[RoutineRewriteRule] | None = None
) -> DependencyOrder:
    """The order to create ``nodes`` in (module docstring). Two nodes with the same id, or
    no ``dependency_order`` rule for their engines (``rules`` defaults to the catalog's),
    is a ValueError."""
    given = list(nodes)
    rule = _order_rule(given, rules)
    ids = [n.routine_id for n in given]
    if len(set(ids)) != len(ids):
        raise ValueError("two routines have the same routine_id")
    # Index the nodes in id order, so everything below is independent of the input order.
    ranked = sorted(range(len(given)), key=lambda i: ids[i])
    by_key: dict[RoutineKey, list[int]] = {}
    for rank, position in enumerate(ranked):
        by_key.setdefault(given[position].key, []).append(rank)
    # edges[a] holds b when a names b: b is created first.
    edges: list[list[int]] = [[] for _ in ranked]
    for rank, position in enumerate(ranked):
        node = given[position]
        targets: set[int] = set()
        for dependency in node.dependencies:
            if dependency.kind == "routine":
                for key in node.resolve_routine(dependency):
                    targets.update(by_key.get(key, ()))
        # A routine calling itself stays one component of one: no cycle, no order.
        edges[rank] = sorted(targets)
    components = _strongly_connected(edges)
    component_of = [0] * len(ranked)
    for c, members in enumerate(components):
        members.sort()
        for member in members:
            component_of[member] = c
    # Kahn's algorithm on the components: a component goes once every one it names has.
    waiting = [0] * len(components)
    named_by: list[list[int]] = [[] for _ in components]
    for rank, named in enumerate(edges):
        source = component_of[rank]
        for target in {component_of[t] for t in named} - {source}:
            waiting[source] += 1
            named_by[target].append(source)
    ready = [(components[c][0], c) for c in range(len(components)) if waiting[c] == 0]
    heapq.heapify(ready)
    order: list[str] = []
    while ready:
        _, c = heapq.heappop(ready)
        order += [ids[ranked[member]] for member in components[c]]
        for later in named_by[c]:
            waiting[later] -= 1
            if waiting[later] == 0:
                heapq.heappush(ready, (components[later][0], later))
    cycles = sorted(
        tuple(ids[ranked[member]] for member in members)
        for members in components
        if len(members) > 1
    )
    return DependencyOrder(
        rule_id=rule.id,
        reason=rule.reason,
        order=tuple(order),
        cycles=tuple(cycles),
        diff=_order_diff(ids, order),
    )


def _kept(before: Sequence[str], after: Sequence[str]) -> list[int]:
    """The positions in ``before`` of a longest run of ids that keep their relative order
    in ``after`` (a longest increasing subsequence, patience-sorted: n log n; a general
    line diff can take quadratic time on 100K lines)."""
    where = {routine_id: i for i, routine_id in enumerate(after)}
    seq = [where[routine_id] for routine_id in before]
    tails: list[int] = []  # tails[k]: the position in before ending the best run of k+1
    previous = [-1] * len(seq)
    for i, value in enumerate(seq):
        k = _lower_bound(seq, tails, value)
        if k > 0:
            previous[i] = tails[k - 1]
        if k == len(tails):
            tails.append(i)
        else:
            tails[k] = i
    kept: list[int] = []
    i = tails[-1] if tails else -1
    while i != -1:
        kept.append(i)
        i = previous[i]
    return kept[::-1]


def _lower_bound(seq: Sequence[int], tails: Sequence[int], value: int) -> int:
    lo, hi = 0, len(tails)
    while lo < hi:
        mid = (lo + hi) // 2
        if seq[tails[mid]] < value:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _range(start: int, stop: int) -> str:
    """A unified diff hunk range, as :mod:`difflib` writes one."""
    length = stop - start
    if length == 1:
        return str(start + 1)
    return f"{start if not length else start + 1},{length}"


def _order_diff(before: Sequence[str], after: Sequence[str]) -> str:
    """The unified diff (zero context lines) from ``before`` to ``after``, two orders of
    the same ids; ``unchanged`` when they are equal. n log n in the number of ids."""
    if list(before) == list(after):
        return "unchanged\n"
    where = {routine_id: i for i, routine_id in enumerate(after)}
    out = ["--- source/order", "+++ target/order"]
    i = j = 0
    for anchor in [*_kept(before, after), len(before)]:
        stop = where[before[anchor]] if anchor < len(before) else len(after)
        if anchor > i or stop > j:
            out.append(f"@@ -{_range(i, anchor)} +{_range(j, stop)} @@")
            out += [f"-{routine_id}" for routine_id in before[i:anchor]]
            out += [f"+{routine_id}" for routine_id in after[j:stop]]
        i, j = anchor + 1, stop + 1
    return "\n".join(out) + "\n"
