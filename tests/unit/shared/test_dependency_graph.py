# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The dependency graph's edges, metrics and slices (#1698): the formulas, by hand."""

from __future__ import annotations

import random
from dataclasses import replace

import pytest

from src.shared.dependency_graph import (
    HUB_HEAVY,
    Slice,
    build_graph,
    edge_weight,
    gravity,
    measure_slices,
    share,
    slice_order_key,
    slices_of,
    unit_gravity,
)
from src.shared.move_units import (
    Unit,
    build_cache_units,
    build_units,
    kept_whole_groups,
)
from tests.unit.agents.referee import slice_plan_jobs as job


def _whole(inputs) -> Unit:  # type: ignore[no-untyped-def]
    return Unit(
        key="aurora_mysql:whole-database",
        engine="aurora_mysql",
        kind="whole_database",
        query_ids=sorted(q.query_id for q in inputs.queries),
        write_query_ids=[],
        owned_tables=list(job.TABLES),
        read_tables=[],
        replicated_in=[],
        reverse_replicas=[],
        kept_whole_ids=[],
        calls_per_second=sum(q.calls_per_second for q in inputs.queries),
    )


def _graph(inputs=None):  # type: ignore[no-untyped-def]
    inputs = inputs or job.inputs()
    units = build_units(inputs, "aurora_mysql", kept_whole_groups(inputs))
    return (
        build_graph(inputs, units, _whole(inputs), build_cache_units(inputs), "aurora_mysql"),
        units,
    )


@pytest.mark.parametrize(
    ("crossing", "statements", "keys", "expected"),
    [
        (0, 0, 0, 0.0),  # no statement and no key: no tie
        (2, 4, 0, 0.5),
        (1, 3, 0, 0.33),  # rounded to two places
        (0, 3, 1, 0.25),  # a foreign key counts as one statement
        (4, 4, 1, 1.0),
    ],
)
def test_the_edge_weight_is_the_share_of_statements_through_the_tie(
    crossing: int, statements: int, keys: int, expected: float
) -> None:
    assert edge_weight(crossing, statements, keys) == expected


@pytest.mark.parametrize(
    ("crossing", "touching", "expected"), [(0, 0, 0.0), (0, 5, 0.0), (1, 3, 0.33), (5, 5, 1.0)]
)
def test_gravity_is_the_share_of_touching_statements_that_cross(
    crossing: int, touching: int, expected: float
) -> None:
    assert gravity(crossing, touching) == expected


def test_share_is_a_rounded_percent_capped_at_100() -> None:
    assert share(1.0, 3.0) == 33.3
    assert share(5.0, 0.0) == 0.0
    assert share(4.0, 2.0) == 100.0


def test_a_unit_s_gravity_counts_its_copies_and_its_reverse_reads() -> None:
    graph, units = _graph()
    by_key = {u.key: u for u in units}
    tables = {q.query_id: frozenset(q.tables) for q in job.inputs().queries}
    # Three queries, none reading a copy; Aurora's q09 reads app.carts: 1 of 4.
    assert unit_gravity(by_key["dynamodb:app.cart_items"], tables) == 0.25
    # Self-contained.
    assert unit_gravity(by_key["dynamodb:app.sessions"], tables) == 0.0
    # Its one query reads only a copy.
    assert unit_gravity(by_key["dynamodb:app.products"], tables) == 1.0
    # One of its two queries reads DynamoDB's app.events.
    assert unit_gravity(by_key["documentdb:app.profiles"], tables) == 0.5


def test_the_edges_say_who_reads_whose_tables() -> None:
    graph, _ = _graph()
    ties = graph.ties
    carts = ties[("aurora_mysql:whole-database", "dynamodb:app.cart_items")]
    # Aurora reads DynamoDB's app.carts (q09); the foreign key from cart_items to carts
    # stays inside the unit, so it is no edge.
    assert carts.b_reads_a == frozenset() and carts.a_reads_b == frozenset({"app.carts"})
    assert carts.kinds(later="dynamodb:app.cart_items") == ["reverse_replica"]
    # q09 over the relational queries (q01, q02, q09) and the unit's (q03, q04, q05).
    assert carts.weight == edge_weight(1, 3 + 3, 0)
    events = ties[("documentdb:app.profiles", "dynamodb:app.events")]
    assert events.kinds(later="documentdb:app.profiles") == ["replica_read"]
    assert events.kinds(later="dynamodb:app.events") == ["reverse_replica"]
    assert events.weight == 0.25  # q11 of q07, q08, q10, q11
    cache = ties[("aurora_mysql:whole-database", "elasticache:app.cart_items")]
    assert cache.kinds(later="elasticache:app.cart_items") == ["cache_fronts"]
    assert cache.tables == ["app.cart_items", "app.carts", "app.products"]


def test_the_hub_score_is_betweenness() -> None:
    graph, _ = _graph()
    # Every unit is tied to the whole-database unit, so it lies on the most paths.
    top = max(graph.hub_score, key=lambda k: (graph.hub_score[k], k))
    assert top == "aurora_mysql:whole-database"
    assert all(0.0 <= s <= 1.0 for s in graph.hub_score.values())


def test_units_tied_to_no_other_unit_of_their_engine_form_one_standalone_slice() -> None:
    graph, units = _graph()
    found = slices_of(graph, units)
    assert [(s.key, s.unit_keys) for s in found] == [
        ("documentdb:standalone", ["documentdb:app.profiles"]),
        (
            "dynamodb:standalone",
            [
                "dynamodb:app.cart_items",
                "dynamodb:app.events",
                "dynamodb:app.products",
                "dynamodb:app.sessions",
            ],
        ),
    ]


def test_units_tied_to_each_other_form_a_community_slice() -> None:
    # A foreign key from app.sessions to app.events ties those two DynamoDB units.
    inputs = job.inputs(
        foreign_keys=(("app.cart_items", "app.carts"), ("app.sessions", "app.events"))
    )
    graph, units = _graph(inputs)
    found = {s.key: s.unit_keys for s in slices_of(graph, units)}
    assert found["dynamodb:community:app.events"] == [
        "dynamodb:app.events",
        "dynamodb:app.sessions",
    ]
    assert found["dynamodb:standalone"] == ["dynamodb:app.cart_items", "dynamodb:app.products"]


def test_a_slice_s_cut_cost_and_value() -> None:
    graph, units = _graph()
    found = slices_of(graph, units)
    tables = {q.query_id: frozenset(q.tables) for q in job.inputs().queries}
    calls = sum(q.calls_per_second for q in job.inputs().queries)
    measure_slices(graph, found, {u.key: u for u in units}, calls, tables)
    ddb = next(s for s in found if s.engine == "dynamodb")
    # Copies across the cut: app.products in, app.carts and app.events out.
    assert ddb.replica_table_count == 3
    # Writes that switch engine: q03, q05, q08, q12, q13.
    assert ddb.write_site_count == 5
    assert ddb.value_call_share_percent == share(4 + 8 + 1 + 6 + 3 + 1 + 5 + 5, calls)
    # Its statements: 8; crossing: q06 (a copy in) plus the reverse reads q09 and q11.
    assert ddb.gravity == gravity(1 + 2, 8 + 2)


def test_slices_run_by_value_against_cut_cost_with_hub_heavy_ones_last() -> None:
    cheap = Slice(key="e:b", engine="e", unit_keys=[], value_call_share_percent=10.0)
    costly = Slice(
        key="e:a", engine="e", unit_keys=[], value_call_share_percent=30.0, replica_table_count=4
    )
    hub = Slice(
        key="e:c", engine="e", unit_keys=[], value_call_share_percent=90.0, hub_score=HUB_HEAVY
    )
    tie = Slice(key="e:0", engine="e", unit_keys=[], value_call_share_percent=10.0)
    ordered = sorted([hub, costly, cheap, tie], key=slice_order_key)
    # 10/1 = 10 beats 30/5 = 6; a tie goes by key; the hub runs last whatever its value.
    assert [s.key for s in ordered] == ["e:0", "e:b", "e:a", "e:c"]


def test_the_graph_does_not_depend_on_the_order_of_the_queries() -> None:
    inputs = job.inputs()
    graph, units = _graph(inputs)
    expected = (graph.ties, graph.hub_score, slices_of(graph, units))
    rng = random.Random(11)
    for _ in range(5):
        shuffled = list(inputs.queries)
        rng.shuffle(shuffled)
        again, again_units = _graph(replace(inputs, queries=tuple(shuffled)))
        assert (again.ties, again.hub_score, slices_of(again, again_units)) == expected
