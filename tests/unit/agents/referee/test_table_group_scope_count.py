# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A wave's table group counts in-scope queries only (#2372).

The engine's own figures (Target engines, cost, the wave's ``query_count``) count the
queries inside the customer's scope. A table group's ``query_count`` is stored on the wave
and read by every deliverable, so it counts the same queries.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.agents.referee.migration_wave_tables import (
    _dynamodb_table_groups,
    _table_group_coverage_note,
)
from src.agents.referee.migration_waves import build_migration_waves


def _qa(qid: str, tables: list[str], in_scope: bool = True) -> dict[str, Any]:
    return {
        "query_id": qid,
        "assigned_engine": "dynamodb",
        "source_tables": tables,
        "in_scope": in_scope,
    }


ROWS = [{"table_id": "app.a"}, {"table_id": "app.b"}]


def test_an_independent_group_counts_in_scope_queries_only() -> None:
    qas = [_qa("q1", ["app.a"]), _qa("q2", ["app.b"]), _qa("q3", ["app.a"], in_scope=False)]
    groups = _dynamodb_table_groups("dynamodb", ROWS, [], qas)
    assert [g["query_count"] for g in groups] == [2]


def test_a_co_dependency_group_counts_in_scope_queries_only() -> None:
    qas = [
        _qa("q1", ["app.a", "app.b"]),
        _qa("q2", ["app.a", "app.b"], in_scope=False),
        _qa("q3", ["app.a"], in_scope=False),
    ]
    groups = _dynamodb_table_groups("dynamodb", ROWS, [["q1", "q2"]], qas)
    assert [(g["kind"], g["query_count"]) for g in groups] == [("co_dependency", 1)]


def test_an_out_of_scope_member_does_not_shape_a_group() -> None:
    qas = [_qa("q1", ["app.a"]), _qa("q2", ["app.b"], in_scope=False)]
    groups = _dynamodb_table_groups("dynamodb", ROWS, [["q1", "q2"]], qas)
    # The co-dependency group is shaped by its in-scope member only, so app.b is left over.
    assert [(g["kind"], g["tables"], g["query_count"]) for g in groups] == [
        ("co_dependency", ["app.a"], 1),
        ("independent", ["app.b"], 0),
    ]


@pytest.mark.passes_on_main(reason="pins that a row with no scope flag still counts")
def test_a_query_with_no_scope_flag_still_counts() -> None:
    qa = {"query_id": "q1", "assigned_engine": "dynamodb", "source_tables": ["app.a"]}
    groups = _dynamodb_table_groups("dynamodb", ROWS, [], [qa])
    assert [g["query_count"] for g in groups] == [1]


def test_the_coverage_note_does_not_count_out_of_scope_queries() -> None:
    inside = [_qa("q1", ["app.a"])]
    outside = [*inside, _qa("q2", ["app.other"], in_scope=False)]

    def note(qas: list[dict[str, Any]]) -> str:
        return _table_group_coverage_note({"app.a"}, [], qas, "dynamodb", {"dynamodb": 2}, 2)

    assert note(outside) == note(inside)


def test_the_wave_groups_add_up_to_the_wave_query_count() -> None:
    qas = [_qa(f"q{i}", ["app.a"]) for i in range(3)] + [
        _qa(f"x{i}", ["app.a"], in_scope=False) for i in range(2)
    ]
    waves = build_migration_waves(
        ranking=[{"target": "dynamodb", "assigned_queries": 3, "workload_percent": 100.0}],
        table_assignments=[
            {"table_id": "app.a", "primary_engine": "dynamodb", "engines": ["dynamodb"]}
        ],
        query_assignments=qas,
        co_dependency_groups=[],
        cache_overlay=None,
        source_engine="mysql",
        known_tables={"app.a"},
    )
    wave = next(w for w in waves or [] if w.get("table_groups"))
    assert wave["query_count"] == 3
    assert sum(g["query_count"] for g in wave["table_groups"]) == 3
