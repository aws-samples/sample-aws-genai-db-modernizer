# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A statement whose tables did not resolve makes no table's engine choice (#2147).

The collector writes ``tables_accessed: ["unknown"]`` for a statement it names no table
for (a constant ``SELECT`` over a derived one-row table, say). Routing used to copy the
placeholder into ``source_tables``, and when such a statement tied, the per-table
choices grouped it under a "table" named ``unknown``: ``--auto`` then recorded an
engine choice for it. A placeholder is no table: no query lists one, a tied statement that
reads no resolved table is a choice of its own (``query:<id>``, "No source table") that
``--auto`` settles like any other, and a job saved before the fix still loads, its
``unknown`` decision read as those per-statement choices.
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


class TestAStatementWithNoTableIsItsOwnChoice:
    def test_a_tied_statement_on_the_unknown_placeholder_is_its_own_choice(self):
        qas = [_qa("q1", ["unknown"]), _qa("q2", ["unknown"])]
        choices = build_pending_choices(qas, _collector([], []), None)
        assert [(c.choice_id, c.table, c.tables, c.query_ids) for c in choices] == [
            ("query:q1", "", [], ["q1"]),
            ("query:q2", "", [], ["q2"]),
        ]
        assert all("reads no table" in c.why_tied for c in choices)

    def test_a_tied_statement_with_no_table_at_all_is_its_own_choice(self):
        (choice,) = build_pending_choices([_qa("q1", [])], _collector([], []), None)
        assert (choice.choice_id, choice.table) == ("query:q1", "")

    def test_decide_for_me_settles_it_with_a_reason_that_names_no_table(self):
        from src.agents.referee.pending_choices import choice_overrides, decided_reason

        qas = [_qa("q1", ["unknown"]), _qa("s1", ["app.t"], tied=None)]
        choices = build_pending_choices(qas, _collector([], ["app.t"]), None)
        engine_of, auto, _ = choice_overrides(choices, decide=True)
        assert set(engine_of) == {"q1"} and auto["q1"].table == ""
        pick = auto["q1"]
        assert decided_reason(pick.engine, pick.alternatives, pick.table, pick.reason) == (
            "Placed on Aurora PostgreSQL: it tied with DynamoDB, and this statement reads no "
            "table. Decided automatically, review if needed."
        )
        assert pick.engine == "aurora_postgresql"  # the settled workload's engine

    def test_a_real_tables_decided_reason_still_names_it(self):
        from src.agents.referee.pending_choices import decided_reason

        assert decided_reason("dynamodb", ["documentdb"], "app.posts", "Higher score.") == (
            "Placed on DynamoDB: it tied with DocumentDB on table posts. Higher score. "
            "Decided automatically, review if needed."
        )

    def test_the_placeholder_never_becomes_the_home_of_a_real_tables_query(self):
        # Two queries touch ``unknown`` and one touches ``app.t``: the most-touched
        # table used to be the placeholder, so the real table's query went under it.
        qas = [_qa("q1", ["unknown"]), _qa("q2", ["unknown"]), _qa("q3", ["app.t", "unknown"])]
        choices = build_pending_choices(qas, _collector([], ["app.t"]), None)
        assert [(c.choice_id, c.table, c.tables, c.query_ids) for c in choices] == [
            ("app.t", "app.t", ["app.t"], ["q3"]),
            ("query:q1", "", [], ["q1"]),
            ("query:q2", "", [], ["q2"]),
        ]

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
            ("query:q2", ["q2"]),
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
    def test_its_placeholder_decision_holds_as_one_choice_per_statement(self):
        saved = _legacy_assignment()
        model = Assignment.model_validate(saved)
        assert model.auto_decided_choices[0].table == "unknown"
        held = auto_decisions_still_holding(model.auto_decided_choices, model.query_assignments)
        assert [(d.choice_id, d.table, d.query_ids, d.engine) for d in held] == [
            ("query:q1", "", ["q1"], "aurora_postgresql"),
            ("query:q2", "", ["q2"], "aurora_postgresql"),
        ]
        assert all(d.reason == "Level before rounding." for d in held)

    def test_reading_it_maps_the_decision_and_rewords_its_statements_reasons(self):
        from src.agents.referee.pending_choices import map_placeholder_choices

        saved = _legacy_assignment()
        mapped = map_placeholder_choices(saved)
        assert [d["choice_id"] for d in mapped["auto_decided_choices"]] == ["query:q1", "query:q2"]
        by_id = {qa["query_id"]: qa for qa in mapped["query_assignments"]}
        for qid in ("q1", "q2"):
            assert by_id[qid]["assignment_reason"] == (
                "Placed on Aurora PostgreSQL: it tied with DynamoDB, and this statement reads "
                "no table. Decided automatically, review if needed."
            )
            assert by_id[qid]["source_tables"] == []
        assert by_id["q3"] == saved["query_assignments"][2]
        Assignment.model_validate(mapped)
        # The saved file itself is left as it was.
        assert saved["auto_decided_choices"][0]["table"] == "unknown"

    def test_an_assignment_with_no_placeholder_decision_is_unchanged(self):
        from src.agents.referee.pending_choices import map_placeholder_choices

        saved = _legacy_assignment()
        saved["auto_decided_choices"] = []
        assert map_placeholder_choices(saved) == saved

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
        assert [d.choice_id for d in held] == ["query:q1", "query:q2", "app.t"]

    def test_its_open_placeholder_ties_are_choices_per_statement(self):
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
        choices = pending_choices_from(saved, collector, {}, {})
        assert [(c.choice_id, c.table) for c in choices] == [("query:q1", ""), ("query:q2", "")]


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


class TestEveryDeliverableShowsNoSourceTable:
    """A choice for a statement that reads no table is "No source table" wherever a
    choice's table is shown, never ``unknown`` and never a bare ``query:`` id."""

    def _choices(self):
        qas = [_qa("q1", ["unknown"]), _qa("s1", ["app.t"], tied=None)]
        return build_pending_choices(qas, _collector([], ["app.t"]), None)

    def test_the_engineering_report(self):
        from src.report.engine_choices import auto_decided_md, pending_choices_md

        (choice,) = self._choices()
        report = {
            "pending_engine_choices": [choice.model_dump(mode="json")],
            "auto_decided_choices": [
                {
                    "choice_id": "query:q1",
                    "table": "",
                    "query_ids": ["q1"],
                    "engine": "aurora_postgresql",
                    "alternatives": ["dynamodb"],
                    "rule": "settled_workload",
                    "reason": "Level before rounding.",
                    "decided_in_version": 3,
                }
            ],
        }
        md = "\n".join(pending_choices_md(report) + auto_decided_md(report))
        assert "| No source table (`q1`) |" in md
        assert "### No source table (`q1`) (1 query)" in md
        assert "`unknown`" not in md and "`query:q1`" not in md

    def test_the_review_chat_and_the_gate_summary(self):
        from src.agents.referee.assignment_review import render_pending_choices
        from src.report.printed_summary import render_gate_block

        (choice,) = self._choices()
        assert "### query:q1 (No source table) (1 queries" in render_pending_choices([choice])
        gate = {
            "pending_choices": [
                {"choice": "query:q1", "table": "No source table", "queries": 1, "why_tied": "x"}
            ]
        }
        assert "- No source table (`query:q1`) (1 query): x" in render_gate_block(
            gate, database_name="app", job_id="j1"
        )

    def test_the_facts_layer(self):
        from src.shared.report_facts import engine_choice_facts

        (choice,) = self._choices()
        facts = engine_choice_facts(
            {
                "pending_engine_choices": [choice.model_dump(mode="json")],
                "auto_decided_choices": [],
            }
        )
        assert facts["pending_choice_pairs"][0]["tables"] == ["No source table"]


def _dnn_shape_decided() -> list[dict[str, Any]]:
    """--auto's decisions on the DNN sample: one table's choice, and two statements that
    read no table, each its own choice (#2147)."""

    def decision(choice_id: str, table: str, qid: str) -> dict[str, Any]:
        return {
            "choice_id": choice_id,
            "table": table,
            "query_ids": [qid],
            "engine": "aurora_postgresql",
            "alternatives": ["dynamodb"],
            "rule": "settled_workload",
            "reason": "Level before rounding.",
            "decided_in_version": 3,
        }

    return [
        decision("app.order_note", "app.order_note", "q1"),
        decision("query:q2", "", "q2"),
        decision("query:q3", "", "q3"),
    ]


class TestChoiceHeadingsCountTablesAndStatementsApart:
    """The facts split a choice count into tables and statements that read no table,
    once (ADR-032); every heading reads that split, and reads as before without one."""

    def test_the_facts_split_the_dnn_shape(self):
        from src.shared.report_facts import engine_choice_facts

        facts = engine_choice_facts({"auto_decided_choices": _dnn_shape_decided()})
        assert (
            facts["auto_decided_choice_count"],
            facts["auto_decided_table_count"],
            facts["auto_decided_no_table_count"],
        ) == (3, 1, 2)
        (group,) = facts["auto_decided_by_engine"]
        assert (group["choice_count"], group["table_count"], group["no_table_count"]) == (3, 1, 2)

    def test_the_engineering_report_heading(self):
        from src.report.engine_choices import auto_decided_md

        md = "\n".join(auto_decided_md({"auto_decided_choices": _dnn_shape_decided()}))
        assert (
            "## Decided automatically, review if needed (1 table and 2 statements with no "
            "source table, 3 queries)"
        ) in md

    def test_a_heading_with_no_such_statement_reads_as_before(self):
        from src.report.engine_choices import auto_decided_md
        from src.report.formatting import choice_scope_text

        tables_only = [d for d in _dnn_shape_decided() if d["table"]]
        md = "\n".join(auto_decided_md({"auto_decided_choices": tables_only}))
        assert "## Decided automatically, review if needed (1 table, 1 query)" in md
        assert choice_scope_text(3, 0, 5) == "3 tables, 5 queries"
        assert choice_scope_text(0, 1, 1) == "1 statement with no source table, 1 query"
        assert choice_scope_text(2, 1, 3) == (
            "2 tables and 1 statement with no source table, 3 queries"
        )

    def test_the_executive_report_card_and_its_groups(self):
        from src.report.executive_report import _choice_count_text
        from src.shared.report_facts import engine_choice_facts

        facts = engine_choice_facts({"auto_decided_choices": _dnn_shape_decided()})
        (group,) = facts["auto_decided_by_engine"]
        assert _choice_count_text(group) == (
            "1 table and 2 statements with no source table, 3 queries"
        )
        # A report saved before the split: every choice was a table's.
        assert _choice_count_text({"choice_count": 3, "query_count": 3}) == "3 tables, 3 queries"

    def test_the_review_chat_heading(self):
        from src.agents.referee.assignment_review import render_pending_choices

        qas = [_qa("q1", ["app.t"]), _qa("q2", ["unknown"]), _qa("s1", ["app.t"], tied=None)]
        choices = build_pending_choices(qas, _collector([], ["app.t"]), None)
        text = render_pending_choices(choices)
        assert (
            "## Pending engine choices (1 table and 1 statement with no source table, 2 queries)"
            in text
        )
        tables_only = [c for c in choices if c.table]
        assert "## Pending engine choices (1 table(s), 1 queries)" in render_pending_choices(
            tables_only
        )
