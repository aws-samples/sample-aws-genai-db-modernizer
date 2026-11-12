# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The dependency graph of move units, its metrics and its slices (#1698, epic #1128).

Computed in our code from the move units (``move_units``), never by a model and never by
querying the context graph. Every figure is rounded and every tie broken by a key, so the
same assignment gives the same graph whatever order its queries come in.

**Nodes.** Every move unit, plus the relational step's whole-database unit (it owns every
table no moving unit owns, and runs the relational engine's queries) and the cache's unit.

**Edges.** Two nodes are tied when one reads a copy of a table the other owns
(``replica_read`` from the reader's side, ``reverse_replica`` from the owner's), when both
read a copy of the same table neither owns (``shared_table``), when a foreign key joins
tables they own (``foreign_key``), or when the cache fronts reads the other runs
(``cache_fronts``). An edge's weight is the share of the two nodes' statements that run
through the tie, a foreign key counting as one statement:
``(crossing statements + foreign keys) / (statements of either + foreign keys)``, 0 to 1.

**Metrics.**

- gravity: of the statements a unit runs or whose tables it owns, the share that cross
  its boundary (its reads of copies and the other engines' reads of its tables), 0 to 1;
- blast radius: its queries plus its reverse-replica reads;
- hub score: its betweenness centrality in the graph (normalized, 0 to 1); a slice's is
  its betweenness in the graph with each slice contracted to one node;
- cut cost of a slice: the tables that need a CDC copy across its boundary, either way,
  and its write statements (each switches engine); transactions split stay None until
  captured transactions exist (#1682);
- value of a slice: its share of the in-scope calls per second.

**Slices.** Per engine, Louvain community detection (``networkx``, a fixed seed, nodes and
edges added in key order) over the edges between that engine's units. Each community of
two or more units is a slice; the units left on their own form the engine's one
``standalone`` slice, since nothing ties them to each other. Module seeds wait for
application evidence (#1692).

**Order.** Slices run by value against cut cost (``value / (1 + replica tables + write
sites)``, highest first), hub-heavy slices (hub score at least ``HUB_HEAVY``) last, then by
slice key.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

import networkx as nx

from src.shared.move_units import SliceInputs, Unit

__all__ = [
    "HUB_HEAVY",
    "LOUVAIN_SEED",
    "Graph",
    "Slice",
    "Tie",
    "build_graph",
    "edge_weight",
    "gravity",
    "measure_slices",
    "share",
    "slice_order_key",
    "slices_of",
    "unit_gravity",
]

# A slice whose hub score reaches this is hub-heavy and runs after the others (#1698).
HUB_HEAVY = 0.5
# Louvain's seed: fixed, so community detection is deterministic.
LOUVAIN_SEED = 0
ROUND = 2


@dataclass
class Node:
    """A graph node: a move unit, the whole-database unit or the cache's unit."""

    key: str
    engine: str
    kind: str
    query_ids: frozenset[str]
    owned: frozenset[str]
    reads: frozenset[str]  # tables its queries read as copies (not owned)


@dataclass
class Tie:
    """The tie between two nodes, ``a < b`` by key; directed later at plan time."""

    a: str
    b: str
    a_reads_b: frozenset[str] = frozenset()  # tables a reads as a copy of b's
    b_reads_a: frozenset[str] = frozenset()
    shared: frozenset[str] = frozenset()
    foreign_keys: frozenset[tuple[str, str]] = frozenset()
    cached: frozenset[str] = frozenset()  # cached reads the other node runs
    cache_tables: frozenset[str] = frozenset()
    weight: float = 0.0

    def kinds(self, later: str) -> list[str]:
        """The edge's kinds with ``later`` the node that runs after the other."""
        later_reads, earlier_reads = (
            (self.a_reads_b, self.b_reads_a)
            if later == self.a
            else (self.b_reads_a, self.a_reads_b)
        )
        out = []
        if later_reads:
            out.append("replica_read")
        if earlier_reads:
            out.append("reverse_replica")
        if self.shared:
            out.append("shared_table")
        if self.foreign_keys:
            out.append("foreign_key")
        if self.cached:
            out.append("cache_fronts")
        return out

    @property
    def tables(self) -> list[str]:
        fk = {t for pair in self.foreign_keys for t in pair}
        return sorted(self.a_reads_b | self.b_reads_a | self.shared | fk | self.cache_tables)


@dataclass
class Graph:
    nodes: dict[str, Node]
    ties: dict[tuple[str, str], Tie]
    hub_score: dict[str, float] = field(default_factory=dict)

    def nx_graph(self, keys: Iterable[str] | None = None) -> nx.Graph:
        """The graph (or the subgraph on ``keys``), nodes and edges added in key order."""
        wanted = set(self.nodes) if keys is None else set(keys)
        g = nx.Graph()
        g.add_nodes_from(sorted(wanted))
        for (a, b), tie in sorted(self.ties.items()):
            if a in wanted and b in wanted:
                g.add_edge(a, b, weight=tie.weight)
        return g


def edge_weight(crossing: int, statements: int, foreign_keys: int) -> float:
    """The share of the two nodes' statements that run through their tie, a foreign key
    counting as one statement, rounded: 0 when neither has a statement or key."""
    total = statements + foreign_keys
    return round((crossing + foreign_keys) / total, ROUND) if total else 0.0


def gravity(crossing: int, touching: int) -> float:
    """Of the statements a unit (or slice) touches, the share that cross its boundary."""
    return round(crossing / touching, ROUND) if touching else 0.0


def unit_gravity(unit: Unit, tables_of: Mapping[str, frozenset[str]]) -> float:
    """A unit's gravity: its reads of copies and the other engines' reads of its tables,
    over every statement it runs or whose tables it owns."""
    copies = {t for t, _ in unit.replicated_in}
    crossing = sum(1 for q in unit.query_ids if tables_of.get(q, frozenset()) & copies)
    return gravity(crossing + unit.reverse_read_count, unit.query_count + unit.reverse_read_count)


def _node(unit: Unit) -> Node:
    return Node(
        key=unit.key,
        engine=unit.engine,
        kind=unit.kind,
        query_ids=frozenset(unit.query_ids),
        owned=frozenset(unit.owned_tables),
        reads=frozenset(t for t, _ in unit.replicated_in),
    )


def build_graph(
    inputs: SliceInputs,
    units: list[Unit],
    whole: Unit | None,
    caches: list[Unit],
    relational: str | None,
) -> Graph:
    """The dependency graph over ``units``, the whole-database unit and the cache units."""
    tables_of = {q.query_id: frozenset(q.tables) for q in inputs.queries}
    nodes = {u.key: _node(u) for u in units}
    moving_owned = {t for u in units for t in u.owned_tables}
    if whole is not None:
        rel_queries = frozenset(q.query_id for q in inputs.queries if q.engine == relational)
        nodes[whole.key] = Node(
            key=whole.key,
            engine=whole.engine,
            kind=whole.kind,
            query_ids=rel_queries,
            owned=frozenset(set(whole.owned_tables) - moving_owned),
            reads=frozenset(
                t for q in rel_queries for t in tables_of.get(q, ()) if t in moving_owned
            ),
        )
    for cache in caches:
        nodes[cache.key] = _node(cache)
    ties: dict[tuple[str, str], Tie] = {}

    def tie(a: str, b: str) -> Tie:
        key = (a, b) if a < b else (b, a)
        if key not in ties:
            ties[key] = Tie(*key)
        return ties[key]

    owner_node = {t: n.key for n in nodes.values() if n.kind != "cache_reads" for t in n.owned}
    for n in sorted(nodes.values(), key=lambda n: n.key):
        if n.kind == "cache_reads":
            continue
        for t in sorted(n.reads):
            other = owner_node.get(t)
            if other is None or other == n.key:
                continue
            x = tie(n.key, other)
            if x.a == n.key:
                x.a_reads_b = x.a_reads_b | {t}
            else:
                x.b_reads_a = x.b_reads_a | {t}
    # Copies two moving units both read, owned by neither.
    readers: dict[str, list[str]] = {}
    for n in nodes.values():
        if n.kind in ("owner", "replica_reads", "read_model"):
            for t in n.reads:
                readers.setdefault(t, []).append(n.key)
    for t, keys in sorted(readers.items()):
        keys = sorted(keys)
        for i, first in enumerate(keys):
            for second in keys[i + 1 :]:
                x = tie(first, second)
                x.shared = x.shared | {t}
    for t1, t2 in inputs.foreign_keys:
        k1, k2 = owner_node.get(t1), owner_node.get(t2)
        if k1 and k2 and k1 != k2:
            x = tie(k1, k2)
            x.foreign_keys = x.foreign_keys | {(t1, t2)}
    for cache in caches:
        cached = set(cache.query_ids)
        for n in sorted(nodes.values(), key=lambda n: n.key):
            if n.kind == "cache_reads" or (whole is not None and n.key != whole.key):
                continue
            # The cache comes after the relational step and fronts reads where they run
            # then: every cached read runs on the relational engine, so it fronts that
            # engine's whole-database unit. With no relational step it fronts the units
            # that hold its reads.
            held = cached if n.kind == "whole_database" else cached & n.query_ids
            if held:
                x = tie(cache.key, n.key)
                x.cached = frozenset(held)
                x.cache_tables = frozenset(t for q in held for t in tables_of.get(q, ()))
    for x in ties.values():
        na, nb = nodes[x.a], nodes[x.b]
        if x.cached:
            crossing = len(x.cached)
            statements = len(na.query_ids | nb.query_ids | x.cached)
        else:
            crossing = len(_crossing(na, x.a_reads_b | x.shared, tables_of)) + len(
                _crossing(nb, x.b_reads_a | x.shared, tables_of)
            )
            statements = len(na.query_ids | nb.query_ids)
        x.weight = edge_weight(crossing, statements, len(x.foreign_keys))
    graph = Graph(nodes=nodes, ties=ties)
    graph.hub_score = _betweenness(graph.nx_graph())
    return graph


def _crossing(
    node: Node, tables: frozenset[str] | set[str], tables_of: Mapping[str, frozenset[str]]
) -> set[str]:
    return {q for q in node.query_ids if tables_of.get(q, frozenset()) & tables}


def _betweenness(g: nx.Graph) -> dict[str, float]:
    if g.number_of_nodes() < 3:
        return {n: 0.0 for n in g}
    scores = nx.betweenness_centrality(g, normalized=True)
    return {n: round(min(1.0, max(0.0, s)), ROUND) for n, s in scores.items()}


@dataclass
class Slice:
    """A set of one engine's move units that belong together (#1698)."""

    key: str
    engine: str
    unit_keys: list[str]
    gravity: float = 0.0
    hub_score: float = 0.0
    replica_table_count: int = 0
    write_site_count: int = 0
    value_call_share_percent: float = 0.0


def slices_of(graph: Graph, units: list[Unit]) -> list[Slice]:
    """Each engine's slices: Louvain communities of two or more units, and the units left
    on their own as one ``standalone`` slice. Slices and their units sorted by key."""
    out: list[Slice] = []
    for engine in sorted({u.engine for u in units}):
        keys = sorted(u.key for u in units if u.engine == engine)
        g = graph.nx_graph(keys)
        g.remove_edges_from([(a, b) for a, b, w in g.edges(data="weight") if not w])
        communities = (
            nx.community.louvain_communities(g, weight="weight", seed=LOUVAIN_SEED)
            if g.number_of_edges()
            else [{k} for k in keys]
        )
        groups = sorted((sorted(c) for c in communities if len(c) >= 2), key=lambda c: c[0])
        alone = sorted(k for c in communities if len(c) < 2 for k in c)
        for members in groups:
            out.append(
                Slice(
                    key=f"{engine}:community:{_suffix(members[0])}",
                    engine=engine,
                    unit_keys=members,
                )
            )
        if alone:
            out.append(Slice(key=f"{engine}:standalone", engine=engine, unit_keys=alone))
    return out


def _suffix(unit_key: str) -> str:
    return unit_key.split(":", 1)[1] if ":" in unit_key else unit_key


def measure_slices(
    graph: Graph,
    slices: list[Slice],
    units: Mapping[str, Unit],
    calls_total: float,
    tables_of: Mapping[str, frozenset[str]],
) -> None:
    """Fill each slice's gravity, hub score, cut cost and value."""
    member_of = {k: s.key for s in slices for k in s.unit_keys}
    quotient = nx.Graph()
    quotient.add_nodes_from(sorted({member_of.get(k, k) for k in graph.nodes}))
    for (a, b), _tie in sorted(graph.ties.items()):
        qa, qb = member_of.get(a, a), member_of.get(b, b)
        if qa != qb:
            quotient.add_edge(qa, qb)
    hub = _betweenness(quotient)
    for s in slices:
        members = [units[k] for k in s.unit_keys]
        owned = {t for u in members for t in u.owned_tables}
        copies_in = {t for u in members for t, _ in u.replicated_in} - owned
        copies_out = {t for u in members for _, tables, _ in u.reverse_replicas for t in tables}
        queries = {q for u in members for q in u.query_ids}
        crossing_in = {q for q in queries if tables_of.get(q, frozenset()) & copies_in}
        reverse = {q for u in members for _, _, reads in u.reverse_replicas for q in reads}
        s.gravity = gravity(len(crossing_in) + len(reverse), len(queries) + len(reverse))
        s.hub_score = hub.get(s.key, 0.0)
        s.replica_table_count = len(copies_in | copies_out)
        s.write_site_count = len({q for u in members for q in u.write_query_ids})
        calls = sum(u.calls_per_second for u in members)
        s.value_call_share_percent = share(calls, calls_total)


def share(calls: float, total: float) -> float:
    """``calls`` as a percent of ``total``, rounded to one place, 0 to 100."""
    return round(min(100.0, 100.0 * calls / total), 1) if total > 0 else 0.0


def slice_order_key(s: Slice) -> tuple[bool, float, str]:
    """Value against cut cost, highest first; hub-heavy slices last; then the slice key."""
    ratio = s.value_call_share_percent / (1 + s.replica_table_count + s.write_site_count)
    return (s.hub_score >= HUB_HEAVY, -round(ratio, 6), s.key)
