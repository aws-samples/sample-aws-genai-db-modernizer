# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""An "Affects" list names collected source tables first, then other names (#1914).

PostgreSQL ids are ``public.<table>``, so a catalog relation (``pg_class``) or a
view sorted among them by plain alphabetical order and led the line. The order is
decided once in synthesis, so report.json, the engineering report and the UI agree.
"""

from __future__ import annotations

from src.agents.referee.synthesis_data import EngineArtifacts, SynthesisData
from src.agents.referee.synthesis_report import build_risk_assessment
from src.report.engineering_report import _affects_line, _trade_off_lines
from src.shared.table_order import collected_tables_first

COLLECTED = {"public.users", "public.topics", "public.posts"}


def test_catalog_names_follow_the_tables_each_group_sorted() -> None:
    names = ["pg_class", "public.users", "pg_attribute", "public.posts", "public.topics"]
    assert collected_tables_first(names, COLLECTED) == [
        "public.posts",
        "public.topics",
        "public.users",
        "pg_attribute",
        "pg_class",
    ]


def test_views_follow_the_tables() -> None:
    names = ["badges_view", "public.users", "a_matview", "public.posts"]
    assert collected_tables_first(names, COLLECTED) == [
        "public.posts",
        "public.users",
        "a_matview",
        "badges_view",
    ]


def test_empty_list() -> None:
    assert collected_tables_first([], COLLECTED) == []


def test_only_non_table_names_are_sorted() -> None:
    assert collected_tables_first(["pg_class", "pg_attribute"], COLLECTED) == [
        "pg_attribute",
        "pg_class",
    ]


def test_mysql_ids_without_a_schema_prefix_stay_sorted_as_tables() -> None:
    collected = {"shop.orders", "shop.items"}
    assert collected_tables_first(
        ["shop.orders", "information_schema.x", "shop.items"], collected
    ) == [
        "shop.items",
        "shop.orders",
        "information_schema.x",
    ]
    bare = {"orders", "items"}
    assert collected_tables_first(["orders", "items"], bare) == ["items", "orders"]


def test_a_bare_name_that_resolves_to_a_collected_table_counts_as_a_table() -> None:
    def resolve(name: str) -> str:
        return "public.users" if name == "users" else name

    assert collected_tables_first(["pg_class", "users"], COLLECTED, resolve) == [
        "users",
        "pg_class",
    ]


def _data() -> SynthesisData:
    tables = ["public.users", "public.posts", "public.topics"]
    analysis = {
        "workload_analysis": {
            "anti_patterns_detected": [
                {
                    "anti_pattern_type": "complex-aggregation",
                    "description": "aggregation",
                    "query_ids": ["q1"],
                    "table_ids": ["pg_class", "public.users", "pg_attribute", "public.posts"],
                    "severity_weight": 0.7,
                    "recommendation": "fix",
                }
            ]
        }
    }
    return SynthesisData(
        job_id="j",
        database_name="d",
        collector={
            "database_schema": {"tables": [{"table_id": t} for t in tables]},
            "queries": {
                "query_patterns": [
                    {"query_id": "q1", "tables_accessed": ["pg_class", "public.users"]}
                ]
            },
        },
        engines={
            "aurora_postgresql": EngineArtifacts(
                "aurora_postgresql", analysis=analysis, schema_design={}
            )
        },
        assignment={
            "query_assignments": [
                {"query_id": "q1", "assigned_engine": "aurora_postgresql", "in_scope": True}
            ]
        },
    )


def test_risk_register_lists_tables_before_catalog_names() -> None:
    risks = build_risk_assessment(_data())["risks"]
    affected = [r["affected_tables"] for r in risks if r.get("affected_tables")]
    assert affected, "fixture must produce a risk with affected tables"
    for names in affected:
        assert names == collected_tables_first(names, {"public.users", "public.posts"})
        assert names[0].startswith("public."), names


def test_trade_offs_list_tables_before_catalog_names() -> None:
    from src.agents.referee.synthesis_handler import order_trade_off_tables

    out = order_trade_off_tables(
        [{"source_tables": ["pg_class", "public.users", "public.posts"]}, {"source_tables": []}],
        _data(),
    )
    assert out[0]["source_tables"] == ["public.posts", "public.users", "pg_class"]
    assert out[1]["source_tables"] == []


def test_engineering_report_prints_the_order_it_is_given() -> None:
    line = _affects_line({"affected_tables": ["public.a", "pg_class"]})
    assert line == "  - Affects: `public.a`, `pg_class`"
    assert (
        _trade_off_lines({"description": "d", "source_tables": ["public.a", "pg_x"]}, None)[-1]
        == "  - Affects: `public.a`, `pg_x`"
    )
