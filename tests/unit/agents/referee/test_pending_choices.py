# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pending engine choices, one per table (#1003): the grouping, the picks and
what an ``--auto`` decision keeps."""

from __future__ import annotations

from typing import Any

import pytest

from src.agents.referee.pending_choices import (
    EngineNotTied,
    UnknownChoice,
    auto_decisions_still_holding,
    build_pending_choices,
    choice_overrides,
)
from src.agents.referee.tie_break import DECIDED_BY_SETTLED_WORKLOAD
from src.contracts.assignment_models import AutoDecidedChoice

PAIR = ["documentdb", "dynamodb"]


def _qa(qid: str, tables: list[str], tied: list[str] | None = PAIR, engine: str = "dynamodb"):
    return {
        "query_id": qid,
        "assigned_engine": engine,
        "source_tables": tables,
        "tied_engines": list(tied or []),
        "in_scope": True,
    }


def _collector(queries: list[dict[str, Any]], tables: list[str] | None = None) -> dict:
    return {
        "metadata": {"source_database": {"engine": "mysql"}},
        "queries": {"query_patterns": queries},
        "database_schema": {"tables": [{"table_id": t} for t in tables or []]},
    }


def _select(qid: str, tables: list[str], text: str = "") -> dict:
    return {
        "query_id": qid,
        "query_text": text or f"SELECT * FROM {tables[0]} WHERE id = 1",
        "query_type": "SELECT",
        "tables_accessed": tables,
    }


def test_one_choice_per_table_with_its_queries() -> None:
    qas = [
        _qa("q1", ["app.theme_fields"]),
        _qa("q2", ["app.theme_fields"]),
        _qa("q3", ["app.incoming_emails"]),
        _qa("q4", ["app.posts"], tied=None),
    ]
    choices = build_pending_choices(qas, _collector([]), None)
    assert [(c.choice_id, c.query_ids) for c in choices] == [
        ("app.incoming_emails", ["q3"]),
        ("app.theme_fields", ["q1", "q2"]),
    ]
    assert all(c.tied_engines == PAIR and c.current_engine == "dynamodb" for c in choices)


def test_a_multi_table_read_goes_under_the_table_most_tied_queries_touch() -> None:
    qas = [
        _qa("q1", ["app.users"]),
        _qa("q2", ["app.users"]),
        _qa("q3", ["app.allowed_pm_users", "app.users"]),
    ]
    choices = build_pending_choices(qas, _collector([]), None)
    assert [(c.table, c.query_ids, c.tables) for c in choices] == [
        ("app.users", ["q1", "q2", "q3"], ["app.allowed_pm_users", "app.users"])
    ]


def test_queries_tied_on_different_engines_are_separate_choices() -> None:
    qas = [
        _qa("q1", ["app.t"]),
        _qa("q2", ["app.t"], tied=["aurora_mysql", "documentdb"], engine="documentdb"),
    ]
    choices = build_pending_choices(qas, _collector([]), None)
    assert [(c.choice_id, c.query_ids, c.tied_engines) for c in choices] == [
        ("app.t", ["q1"], PAIR),
        ("app.t#2", ["q2"], ["aurora_mysql", "documentdb"]),
    ]


def test_a_write_goes_under_the_table_it_writes_and_marks_a_source_of_truth_choice() -> None:
    queries = [
        _select("q1", ["app.posts"]),
        {
            "query_id": "w1",
            "query_text": "INSERT INTO post_tags (post_id, tag) SELECT id, 'x' FROM posts",
            "query_type": "INSERT",
            "tables_accessed": ["app.post_tags", "app.posts"],
        },
    ]
    qas = [_qa("q1", ["app.posts"]), _qa("w1", ["app.post_tags", "app.posts"])]
    choices = build_pending_choices(qas, _collector(queries, ["app.posts", "app.post_tags"]), None)
    by_id = {c.choice_id: c for c in choices}
    assert by_id["app.post_tags"].query_ids == ["w1"]
    assert by_id["app.post_tags"].write_query_ids == ["w1"]
    assert by_id["app.posts"].write_query_ids == []
    assert "source of truth" in by_id["app.post_tags"].options[0].trade_off


def test_without_scores_decide_for_me_uses_the_plan_s_settled_workload() -> None:
    qas = [
        _qa("q1", ["app.t"]),
        _qa("s1", ["app.other"], tied=None, engine="documentdb"),
        _qa("s2", ["app.other"], tied=None, engine="documentdb"),
    ]
    (choice,) = build_pending_choices(qas, _collector([]), None)
    assert choice.decide_for_me_engine == "documentdb"
    assert choice.decide_for_me_rule == DECIDED_BY_SETTLED_WORKLOAD
    assert [e.settled_query_count for e in choice.options] == [2, 0]
    assert "adds no engine" in choice.options[0].trade_off


def test_no_ties_no_choices() -> None:
    assert build_pending_choices([_qa("q1", ["app.t"], tied=None)], _collector([]), None) == []
    assert build_pending_choices([], None, None) == []


def test_a_pick_applies_to_every_query_of_its_table_and_decide_fills_the_rest() -> None:
    qas = [_qa("q1", ["app.a"]), _qa("q2", ["app.a"]), _qa("q3", ["app.b"])]
    choices = build_pending_choices(qas, _collector([]), None)
    engine_of, auto, picked = choice_overrides(choices, {"a": "documentdb"}, decide=True)
    assert picked == {"app.a": "documentdb"}
    assert engine_of["q1"] == engine_of["q2"] == "documentdb"
    assert set(auto) == {"q3"}
    assert auto["q3"].engine == engine_of["q3"]
    assert auto["q3"].alternatives == [e for e in PAIR if e != auto["q3"].engine]
    # Without decide, nothing else moves.
    engine_of, auto, _ = choice_overrides(choices, {"app.b": "dynamodb"})
    assert engine_of == {"q3": "dynamodb"} and auto == {}


def test_a_pick_that_names_no_choice_or_an_untied_engine_is_refused() -> None:
    choices = build_pending_choices([_qa("q1", ["app.a"])], _collector([]), None)
    with pytest.raises(UnknownChoice, match="app.a"):
        choice_overrides(choices, {"app.zzz": "dynamodb"})
    with pytest.raises(EngineNotTied, match="DocumentDB or DynamoDB"):
        choice_overrides(choices, {"app.a": "aurora_mysql"})


def _decision(engine: str, qids: list[str]) -> AutoDecidedChoice:
    return AutoDecidedChoice(
        choice_id="app.a",
        table="app.a",
        query_ids=qids,
        engine=engine,
        alternatives=["dynamodb"],
        rule="unrounded_score",
        reason="x",
        decided_in_version=3,
    )


def test_a_decision_holds_only_while_every_query_stays_on_its_engine_untied() -> None:
    decision = _decision("documentdb", ["q1", "q2"])
    held = [_qa("q1", [], tied=None, engine="documentdb"), _qa("q2", [], None, "documentdb")]
    assert auto_decisions_still_holding([decision], held) == [decision]
    moved = [held[0], _qa("q2", [], tied=None, engine="dynamodb")]
    assert auto_decisions_still_holding([decision], moved) == []
    retied = [held[0], _qa("q2", [], engine="documentdb")]
    assert auto_decisions_still_holding([decision], retied) == []


def _join(qid: str, tables: list[str]) -> dict:
    """A query whose JOIN is significant on every table it reads (join_count 2)."""
    return {
        "query_id": qid,
        "query_text": f"SELECT * FROM {tables[0]} JOIN {tables[1]} ON 1 = 1",
        "query_type": "SELECT",
        "tables_accessed": tables,
        "has_joins": True,
        "join_count": 2,
    }


def _unit_fixture(q2_tie: list[str]) -> tuple[list[dict], dict]:
    """q1 joins orders+users and q2 users+posts: one co-dependency group whose
    members' own tables differ (q1's is orders, where q3 and q4 sit too; q2's is
    users)."""
    queries = [
        _join("q1", ["app.orders", "app.users"]),
        _join("q2", ["app.users", "app.posts"]),
        _select("q3", ["app.orders"]),
        _select("q4", ["app.orders"]),
    ]
    qas = [
        _qa("q1", ["app.orders", "app.users"]),
        _qa("q2", ["app.users", "app.posts"], tied=q2_tie),
        _qa("q3", ["app.orders"]),
        _qa("q4", ["app.orders"]),
    ]
    return qas, _collector(queries, ["app.orders", "app.users", "app.posts"])


def test_a_co_dependency_group_across_two_tables_is_one_choice() -> None:
    """A pick moves the whole unit, so the unit is never split over two choices."""
    from src.agents.referee.co_dependency import build_co_dependency_groups

    qas, collector = _unit_fixture(PAIR)
    groups = build_co_dependency_groups(
        collector["queries"]["query_patterns"], collector["database_schema"]["tables"]
    )
    assert any({"q1", "q2"} <= set(g) for g in groups)
    (choice,) = build_pending_choices(qas, collector, None)
    assert choice.table == "app.orders"
    assert choice.query_ids == ["q1", "q2", "q3", "q4"]
    engine_of, _, _ = choice_overrides([choice], {"orders": "documentdb"})
    assert set(engine_of) == {"q1", "q2", "q3", "q4"}


def test_a_unit_whose_members_tie_on_different_engines_is_one_choice_over_their_common_tie() -> (
    None
):
    qas, collector = _unit_fixture(["aurora_mysql", "documentdb", "dynamodb"])
    (choice,) = build_pending_choices(qas, collector, None)
    assert choice.query_ids == ["q1", "q2", "q3", "q4"]
    assert choice.tied_engines == PAIR


def test_a_unit_whose_members_share_fewer_than_two_tied_engines_offers_every_one() -> None:
    qas, collector = _unit_fixture(["aurora_mysql", "dynamodb"])
    (choice,) = build_pending_choices(qas, collector, None)
    assert choice.query_ids == ["q1", "q2", "q3", "q4"]
    assert choice.tied_engines == ["aurora_mysql", "documentdb", "dynamodb"]
    assert choice.decide_for_me_engine in choice.tied_engines


def test_a_choice_lists_every_engine_its_queries_sit_on() -> None:
    qas = [_qa("q1", ["app.t"]), _qa("q2", ["app.t"], engine="documentdb")]
    (choice,) = build_pending_choices(qas, _collector([]), None)
    assert choice.current_engines == PAIR
    assert choice.current_engine in PAIR
