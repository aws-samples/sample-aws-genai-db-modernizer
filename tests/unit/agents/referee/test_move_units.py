# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Move units (#1129): one writer per table, groups kept whole, deterministic."""

from __future__ import annotations

import random
from dataclasses import replace

from src.agents.referee.slice_inputs import slice_inputs
from src.shared.move_units import (
    SliceInputs,
    SliceQuery,
    Unit,
    build_cache_units,
    build_units,
    kept_whole_groups,
)
from tests.unit.agents.referee import slice_plan_jobs as job


def _units(inputs: SliceInputs, risks: list[dict] | None = None) -> dict[str, Unit]:
    groups = kept_whole_groups(inputs)
    return {u.key: u for u in build_units(inputs, "aurora_mysql", groups, risks or [])}


def test_an_engine_s_queries_join_over_the_tables_it_owns() -> None:
    units = _units(job.inputs())
    assert sorted(units) == [
        "documentdb:app.profiles",
        "dynamodb:app.cart_items",
        "dynamodb:app.events",
        "dynamodb:app.products",
        "dynamodb:app.sessions",
    ]
    carts = units["dynamodb:app.cart_items"]
    # q04 joins the two tables, so their writes and reads are one unit; Aurora's read of
    # app.carts is its reverse replica.
    assert carts.query_ids == ["q03", "q04", "q05"]
    assert carts.write_query_ids == ["q03", "q05"]
    assert carts.owned_tables == ["app.cart_items", "app.carts"]
    assert carts.reverse_replicas == [("aurora_mysql", ["app.carts"], ["q09"])]
    assert carts.kind == "owner" and carts.blast_radius == 4


def test_a_query_on_no_owned_table_reads_a_copy_from_its_writer() -> None:
    units = _units(job.inputs())
    products = units["dynamodb:app.products"]
    assert products.kind == "replica_reads" and products.owned_tables == []
    # Nobody writes app.products: it stays where the relational step put it.
    assert products.replicated_in == [("app.products", "aurora_mysql")]
    profiles = units["documentdb:app.profiles"]
    assert profiles.replicated_in == [("app.events", "dynamodb")]
    assert units["dynamodb:app.events"].reverse_replicas == [
        ("documentdb", ["app.events"], ["q11"])
    ]


def test_the_relational_engine_and_the_cache_have_no_moving_unit() -> None:
    units = _units(job.inputs())
    assert {u.engine for u in units.values()} == {"dynamodb", "documentdb"}
    (cache,) = build_cache_units(job.inputs())
    assert cache.key == "elasticache:app.cart_items" and cache.query_ids == ["q04", "q06"]


def test_a_unit_holds_every_kept_whole_group_it_touches_whole() -> None:
    inputs = job.inputs()
    groups = {g.group_id: g for g in kept_whole_groups(inputs)}
    # Single-statement write groups are no group; the co-dependency group and the
    # two-statement write group are.
    assert sorted(groups) == ["co_dependency:1", "write:app.sessions"]
    units = _units(inputs)
    for g in groups.values():
        holders = [u for u in units.values() if set(g.query_ids) & set(u.query_ids)]
        assert len(holders) == 1 and set(g.query_ids) <= set(holders[0].query_ids)
        assert g.group_id in holders[0].kept_whole_ids


def test_a_seed_group_joins_queries_no_table_ties() -> None:
    # A co-dependency group joining the sessions and products queries makes them one unit.
    inputs = job.inputs(co_dependency_groups=(("q04", "q05"), ("q06", "q12")))
    units = _units(inputs)
    assert "dynamodb:app.products" not in units
    (unit,) = [u for u in units.values() if "q06" in u.query_ids]
    assert unit.query_ids == ["q06", "q12", "q13"] and unit.owned_tables == ["app.sessions"]


def test_a_temporary_object_statement_stays_with_its_routine() -> None:
    # ADR-031 Amendment 20: q21 touches only a temporary table and follows routine p1's
    # statements (q20) to DynamoDB. With no table of its own it would be a unit alone;
    # kept whole, it moves with q20.
    extra = (
        SliceQuery(query_id="q20", engine="dynamodb", tables=("app.audit",)),
        SliceQuery(query_id="q21", engine="dynamodb", tables=()),
    )
    base = job.inputs()
    write_groups = {**base.writes_by_table, "app.audit": ("q20",)}
    owners = {**base.owners, "app.audit": "dynamodb"}
    alone = _units(
        replace(base, queries=base.queries + extra, writes_by_table=write_groups, owners=owners)
    )
    assert "dynamodb:statement-q21" in alone
    inputs = replace(
        base,
        queries=base.queries + extra,
        writes_by_table=write_groups,
        owners=owners,
        routine_followers={"p1": ("q20", "q21")},
    )
    groups = {g.group_id: g for g in kept_whole_groups(inputs)}
    assert groups["routine:p1"].kind == "routine_unit"
    assert groups["routine:p1"].query_ids == ("q20", "q21")
    units = _units(inputs)
    (unit,) = [u for u in units.values() if "q21" in u.query_ids]
    assert unit.query_ids == ["q20", "q21"] and "routine:p1" in unit.kept_whole_ids


def test_followers_from_the_report_join_their_routine_s_leaders() -> None:
    collector = {
        "metadata": {"source_database": {"engine": "sqlserver"}},
        "queries": {
            "query_patterns": [
                {
                    "query_id": "q20",
                    "query_text": "INSERT INTO dbo.audit (id) VALUES (1)",
                    "query_type": "INSERT",
                    "tables_accessed": ["dbo.audit"],
                    "parent_routine_ids": ["dbo.p1"],
                },
                {
                    "query_id": "q21",
                    "query_text": "INSERT INTO #work (id) VALUES (1)",
                    "query_type": "INSERT",
                    "tables_accessed": ["#work"],
                    "parent_routine_ids": ["dbo.p1"],
                },
            ]
        },
        "database_schema": {"tables": [{"table_id": "dbo.audit", "table_name": "audit"}]},
    }
    assignment = {
        "query_assignments": [
            {"query_id": "q20", "assigned_engine": "dynamodb", "source_tables": ["dbo.audit"]},
            {"query_id": "q21", "assigned_engine": "dynamodb", "source_tables": ["#work"]},
        ],
        "write_groups": {"dbo.audit": ["q20"]},
    }
    followers = [{"query_id": "q21", "routine_id": "dbo.p1", "engine": "dynamodb"}]
    inputs = slice_inputs(assignment, collector, followers)
    assert inputs.routine_followers == {"dbo.p1": ("q20", "q21")}
    # The temporary table is not a collected table: the follower reads none.
    assert {q.query_id: q.tables for q in inputs.queries} == {"q20": ("dbo.audit",), "q21": ()}
    (unit,) = build_units(inputs, None, kept_whole_groups(inputs))
    assert unit.query_ids == ["q20", "q21"] and unit.kept_whole_ids == ["routine:dbo.p1"]


def test_an_open_critical_or_high_risk_holds_the_unit_it_names() -> None:
    risks = [
        {"risk_id": "RISK-002", "engine": "dynamodb", "query_ids": ["q07"]},
        # Named by table only.
        {"risk_id": "RISK-004", "engine": "documentdb", "affected_tables": ["app.events"]},
        # Another engine's risk holds nothing here.
        {"risk_id": "RISK-005", "engine": "aurora_mysql", "query_ids": ["q04"]},
    ]
    units = _units(job.inputs(), risks)
    assert units["dynamodb:app.events"].holding_risk_ids == ["RISK-002"]
    assert units["documentdb:app.profiles"].holding_risk_ids == ["RISK-004"]
    assert units["dynamodb:app.cart_items"].holding_risk_ids == []


def test_the_units_do_not_depend_on_the_order_of_the_queries() -> None:
    inputs = job.inputs()
    expected = build_units(inputs, "aurora_mysql", kept_whole_groups(inputs))
    rng = random.Random(7)
    for _ in range(5):
        shuffled = list(inputs.queries)
        rng.shuffle(shuffled)
        again = replace(inputs, queries=tuple(shuffled))
        assert build_units(again, "aurora_mysql", kept_whole_groups(again)) == expected


def test_placeholder_and_uncollected_names_are_no_tables() -> None:
    assignment = {
        "query_assignments": [
            {
                "query_id": "q1",
                "assigned_engine": "dynamodb",
                "source_tables": ["app.a", "pg_class", "DUAL"],
            },
            {"query_id": "q2", "assigned_engine": "dynamodb", "in_scope": False},
            {"query_id": "q3", "assigned_engine": None},
        ]
    }
    collector = {"database_schema": {"tables": [{"table_id": "app.a", "table_name": "a"}]}}
    inputs = slice_inputs(assignment, collector)
    assert [(q.query_id, q.tables) for q in inputs.queries] == [("q1", ("app.a",))]
