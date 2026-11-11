# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A statement whose tables did not resolve makes no table's engine choice (#2147).

The collector writes ``tables_accessed: ["unknown"]`` for a statement it names no table
for (a constant ``SELECT`` over a derived one-row table, say). Routing used to copy the
placeholder into ``source_tables``, and when such a statement tied, the per-table
choices grouped it under a "table" named ``unknown``: ``--auto`` then recorded an
engine choice for it. A placeholder is no table: no query lists one, no choice is built
for queries that read no resolved table, and a job saved before the fix still loads
without showing one.
"""

from __future__ import annotations

from typing import Any

from src.agents.referee.assignment_overrides import pending_choices_from
from src.agents.referee.assignment_resolver import AssignmentResolver
from src.agents.referee.pending_choices import auto_decisions_still_holding, build_pending_choices
from src.contracts.assignment_models import Assignment, AutoDecidedChoice
from src.shared.placeholder_tables import is_placeholder_table

PAIR = ["aurora_postgresql", "dynamodb"]


def _qa(qid: str, tables: list[str], tied: list[str] | None = PAIR) -> dict[str, Any]:
    return {
        "query_id": qid,
        "assigned_engine": "aurora_postgresql",
        "confidence": 0,
        "source_tables": tables,
        "tied_engines": list(tied or []),
        "assignment_reason": "x",
        "in_scope": True,
    }


def _pattern(qid: str, text: str, tables: list[str], query_type: str = "SELECT") -> dict:
    return {
        "query_id": qid,
        "query_text": text,
        "query_type": query_type,
        "tables_accessed": tables,
        "join_count": 0,
        "has_joins": False,
        "has_aggregations": False,
        "calls_per_second": 1.0,
        "rows_returned_avg": 1,
    }


def _collector(patterns: list[dict], tables: list[str], engine: str = "sqlserver") -> dict:
    return {
        "job_id": "t",
        "metadata": {"source_database": {"engine": engine, "database_name": "app"}},
        "database_schema": {
            "tables": [{"table_id": t, "table_name": t.split(".")[-1]} for t in tables]
        },
        "queries": {"query_patterns": patterns},
    }


class TestNoQueryListsAPlaceholderTable:
    def test_a_statement_with_no_resolved_table_lists_none(self):
        collector = _collector(
            [
                _pattern("q1", "SELECT * FROM app.users WHERE id = ?", ["app.users"]),
                _pattern(
                    "q2", "SELECT ? AS c FROM (SELECT ? AS x) AS one WHERE ? = ?", ["unknown"]
                ),
                _pattern("q3", "SELECT ? FROM DUAL", ["DUAL"], query_type="SELECT"),
            ],
            ["app.users"],
        )
        analysis = {
            e: {
                "table_recommendations": [{"table_id": "app.users", "confidence_score": 70}],
                "workload_analysis": {"patterns_detected": [], "anti_patterns_detected": []},
            }
            for e in PAIR
        }
        result = AssignmentResolver().resolve(
            {"selected_agents": [{"agent_type": e} for e in PAIR], "signals": []},
            analysis,
            collector,
        )
        by_id = {qa.query_id: qa for qa in result.query_assignments}
        assert by_id["q1"].source_tables == ["app.users"]
        assert by_id["q2"].source_tables == []
        assert by_id["q3"].source_tables == []
        assert not [
            t
            for qa in result.query_assignments
            for t in qa.source_tables
            if is_placeholder_table(t)
        ]


class TestNoChoiceForAStatementWithNoTable:
    def test_a_tied_statement_on_the_unknown_placeholder_is_no_choice(self):
        qas = [_qa("q1", ["unknown"]), _qa("q2", ["unknown"])]
        assert build_pending_choices(qas, _collector([], []), None) == []

    def test_a_tied_statement_with_no_table_at_all_is_no_choice(self):
        # It used to become a choice named after the query (``query:q1``).
        assert build_pending_choices([_qa("q1", [])], _collector([], []), None) == []

    def test_the_placeholder_never_becomes_the_home_of_a_real_tables_query(self):
        # Two queries touch ``unknown`` and one touches ``app.t``: the most-touched
        # table used to be the placeholder, so the real table's query went under it.
        qas = [_qa("q1", ["unknown"]), _qa("q2", ["unknown"]), _qa("q3", ["app.t", "unknown"])]
        (choice,) = build_pending_choices(qas, _collector([], ["app.t"]), None)
        assert (choice.choice_id, choice.table, choice.tables) == ("app.t", "app.t", ["app.t"])
        assert choice.query_ids == ["q3"]

    def test_a_statement_with_no_table_in_a_tables_unit_stays_in_its_choice(self):
        # A unit (here a routine's statements) is one choice whatever its members read:
        # the unit's table names it, and the statement with no table goes with it.
        patterns = [
            _pattern("q1", "SELECT * FROM app.t WHERE id = @id", ["app.t"]),
            _pattern("q2", "SELECT ? AS c FROM (SELECT ? AS x) AS one", ["unknown"]),
        ]
        assignment = {
            "routine_units": [{"routine_id": "app.p", "query_ids": ["q1", "q2"], "fired_by": []}]
        }
        qas = [_qa("q1", ["app.t"]), _qa("q2", ["unknown"])]
        (choice,) = build_pending_choices(
            qas, _collector(patterns, ["app.t"]), None, assignment=assignment
        )
        assert (choice.table, choice.query_ids, choice.tables) == ("app.t", ["q1", "q2"], ["app.t"])

    def test_tied_queries_on_real_tables_still_make_their_choices(self):
        qas = [_qa("q1", ["app.a"]), _qa("q2", ["unknown"]), _qa("q3", ["app.b"])]
        choices = build_pending_choices(qas, _collector([], ["app.a", "app.b"]), None)
        assert [(c.choice_id, c.query_ids) for c in choices] == [
            ("app.a", ["q1"]),
            ("app.b", ["q3"]),
        ]


def _legacy_assignment() -> dict[str, Any]:
    """A version ``--auto`` wrote before #2147: an engine choice decided for the
    ``unknown`` placeholder, its queries listing it as their table."""
    placed = {
        "assigned_engine": "aurora_postgresql",
        "confidence": 0,
        "source_tables": ["unknown"],
        "assignment_reason": (
            "Placed on Aurora PostgreSQL: it tied with DynamoDB on table unknown. Level "
            "before rounding. Decided automatically, review if needed."
        ),
        "in_scope": True,
        "tied_engines": [],
    }
    return {
        "contract_version": "1.26",
        "job_id": "old",
        "version": 3,
        "status": "auto_decided",
        "source": "auto_decided",
        "timestamp": "2026-11-11T00:00:00Z",
        "query_assignments": [
            {"query_id": "q1", **placed},
            {"query_id": "q2", **placed},
            {**_qa("q3", ["app.t"], tied=None), "assigned_engine": "aurora_postgresql"},
        ],
        "table_assignments": [],
        "co_dependency_groups": [],
        "write_groups": {},
        "validation_warnings": [],
        "auto_decided_choices": [
            {
                "choice_id": "unknown",
                "table": "unknown",
                "query_ids": ["q1", "q2"],
                "engine": "aurora_postgresql",
                "alternatives": ["dynamodb"],
                "unrounded_scores": {"aurora_postgresql": 0.0, "dynamodb": 0.0},
                "scores": {"aurora_postgresql": 0.0, "dynamodb": 0.0},
                "rule": "settled_workload",
                "reason": "Level before rounding.",
                "decided_in_version": 3,
            }
        ],
    }


class TestAJobSavedBeforeTheFixStillLoads:
    def test_it_validates_and_its_placeholder_decision_no_longer_holds(self):
        saved = _legacy_assignment()
        model = Assignment.model_validate(saved)
        assert model.auto_decided_choices[0].table == "unknown"
        held = auto_decisions_still_holding(model.auto_decided_choices, model.query_assignments)
        assert held == []

    def test_a_real_tables_decision_beside_it_still_holds(self):
        saved = _legacy_assignment()
        saved["query_assignments"][2]["source_tables"] = ["app.t"]
        real = AutoDecidedChoice.model_validate(
            {**saved["auto_decided_choices"][0], "choice_id": "app.t", "table": "app.t"}
            | {"query_ids": ["q3"]}
        )
        model = Assignment.model_validate(saved)
        held = auto_decisions_still_holding(
            [*model.auto_decided_choices, real], model.query_assignments
        )
        assert held == [real]

    def test_its_open_placeholder_ties_build_no_choice(self):
        saved = _legacy_assignment()
        for qa in saved["query_assignments"][:2]:
            qa["tied_engines"] = list(PAIR)
        collector = _collector(
            [
                _pattern("q1", "SELECT ? AS c FROM (SELECT ? AS x) AS one", ["unknown"]),
                _pattern("q2", "SELECT ? AS c FROM (SELECT ? AS x) AS one", ["unknown"]),
                _pattern("q3", "SELECT * FROM app.t WHERE id = ?", ["app.t"]),
            ],
            ["app.t"],
        )
        assert pending_choices_from(saved, collector, {}, {}) == []


def test_the_access_pattern_record_lists_no_placeholder_table():
    from src.agents.referee.query_pattern import access_patterns_record

    collector = _collector(
        [
            _pattern("q1", "SELECT * FROM app.users WHERE id = ?", ["app.users"]),
            _pattern("q2", "SELECT ? AS c FROM (SELECT ? AS x) AS one WHERE ? = ?", ["unknown"]),
        ],
        ["app.users"],
    )
    rows = {r["query_id"]: r for r in access_patterns_record(collector)["queries"]}
    assert rows["q1"]["tables"] == ["app.users"]
    assert rows["q2"]["tables"] == []
