"""The post-correction re-settle uses the shared consolidation rule (#588).

When a validation correction brings back an engine the reality check had
removed, the committed set the reality check settled against has changed, so
the engines are settled again. ADR-031 Decision 7 says this uses the same
rule as the first reality check: remove an engine when the committed engines
can serve its queries, judged with the shared per-query scores and the shared
eligibility gate, and move each query to its highest-scoring remaining engine.

It replaces the old Aurora absorption pass, which had its own decision rule:
only engines under 10 queries (5 for a signal-pinned one) were considered,
Aurora had to score at least 50, a specialist 30 points ahead kept its query,
and every absorbed query went to Aurora. Each test below would have decided
differently under that rule.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import src.agents.referee.reality_check as reality_check
from src.agents.referee.reality_check import UNIQUE_DELTA_THRESHOLD, resettle_after_corrections

REALITY_CHECK_SOURCE = Path(reality_check.__file__)


def _assignments(engines: dict[str, list[str]]) -> list[dict]:
    return [
        {"query_id": qid, "assigned_engine": engine, "assignment_reason": "test"}
        for engine, qids in engines.items()
        for qid in qids
    ]


def _collector(tables: dict[str, str]) -> dict:
    return {
        "queries": {
            "query_patterns": [
                {"query_id": qid, "tables_accessed": [table], "query_type": "SELECT"}
                for qid, table in tables.items()
            ]
        }
    }


def _analysis(scores: dict[str, dict[str, int]]) -> dict:
    """``engine -> {table: confidence}`` as analysis outputs."""
    return {
        engine: {
            "table_recommendations": [
                {"table_id": table, "confidence_score": score} for table, score in tables.items()
            ]
        }
        for engine, tables in scores.items()
    }


def _engine_of(revised: list[dict]) -> dict[str, str]:
    return {qa["query_id"]: qa["assigned_engine"] for qa in revised}


class TestTheAuroraOnlyRuleIsGone:
    def test_no_aurora_absorption_thresholds_or_pass_remain(self):
        """ADR-031 Decision 7 supersedes the Aurora-only pass "with its own
        fitness gate and thresholds"; nothing in the module defines them."""
        tree = ast.parse(REALITY_CHECK_SOURCE.read_text())
        defined = {
            node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
        } | {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.ClassDef))
        }
        gone = {
            "AURORA_ABSORPTION_QUERY_THRESHOLD",
            "AURORA_ABSORPTION_MIN_FIT",
            "SPECIALIST_DELTA_THRESHOLD",
            "TINY_MANDATORY_QUERY_THRESHOLD",
            "AuroraAbsorptionResult",
            "_run_aurora_absorption_pass",
            "_absorb_tiny_mandatory_engine",
            "_apply_aurora_absorption",
            "rerun_aurora_absorption",
        }
        assert defined & gone == set()


class TestResettleUsesTheSharedRule:
    def test_an_engine_with_ten_or_more_queries_is_still_removed(self):
        """No size cut-off: 12 DocumentDB queries, each scored 5 points below
        Aurora's own score for it, have no unique value (the old pass never
        looked at an engine with 10 or more queries)."""
        doc_ids = [f"d{i}" for i in range(12)]
        tables = {f"a{i}": "db.main" for i in range(20)} | {qid: "db.docs" for qid in doc_ids}
        analysis = _analysis(
            {
                "aurora_postgresql": {"db.main": 80, "db.docs": 70},
                "documentdb": {"db.main": 40, "db.docs": 65},
            }
        )
        assignments = _assignments({"aurora_postgresql": list(tables)[:20], "documentdb": doc_ids})

        revised, consolidations = resettle_after_corrections(
            assignments, {}, analysis, _collector(tables), source_engine="postgresql"
        )

        assert {_engine_of(revised)[q] for q in doc_ids} == {"aurora_postgresql"}
        assert [(c["from_engine"], c["to_engine"], c["action"]) for c in consolidations] == [
            ("documentdb", "aurora_postgresql", "full")
        ]

    def test_no_minimum_fit_floor_for_the_destination(self):
        """Aurora scores 40 and DocumentDB 45: DocumentDB has no unique value
        (5 < UNIQUE_DELTA_THRESHOLD), so its queries move to the best
        remaining engine even though that score is below the old floor of 50."""
        tables = {f"a{i}": "db.main" for i in range(10)} | {"d1": "db.docs", "d2": "db.docs"}
        analysis = _analysis(
            {
                "aurora_postgresql": {"db.main": 80, "db.docs": 40},
                "documentdb": {"db.main": 30, "db.docs": 45},
            }
        )
        assignments = _assignments(
            {"aurora_postgresql": [f"a{i}" for i in range(10)], "documentdb": ["d1", "d2"]}
        )

        revised, _ = resettle_after_corrections(
            assignments, {}, analysis, _collector(tables), source_engine="postgresql"
        )

        assert _engine_of(revised)["d1"] == "aurora_postgresql"
        assert _engine_of(revised)["d2"] == "aurora_postgresql"

    def test_a_moved_query_goes_to_its_highest_scoring_remaining_engine(self):
        """DynamoDB scores the DocumentDB queries above Aurora, so that is
        where they go -- not Aurora by default."""
        tables = (
            {f"a{i}": "db.main" for i in range(10)}
            | {f"k{i}": "db.kv" for i in range(10)}
            | {"d1": "db.docs", "d2": "db.docs"}
        )
        analysis = _analysis(
            {
                "aurora_postgresql": {"db.main": 90, "db.kv": 50, "db.docs": 60},
                "dynamodb": {"db.main": 30, "db.kv": 90, "db.docs": 70},
                "documentdb": {"db.main": 30, "db.kv": 40, "db.docs": 65},
            }
        )
        assignments = _assignments(
            {
                "aurora_postgresql": [f"a{i}" for i in range(10)],
                "dynamodb": [f"k{i}" for i in range(10)],
                "documentdb": ["d1", "d2"],
            }
        )

        revised, consolidations = resettle_after_corrections(
            assignments, {}, analysis, _collector(tables), source_engine="postgresql"
        )

        assert _engine_of(revised)["d1"] == "dynamodb"
        assert _engine_of(revised)["d2"] == "dynamodb"
        assert {(c["from_engine"], c["to_engine"]) for c in consolidations} == {
            ("documentdb", "dynamodb")
        }

    def test_a_query_with_unique_value_keeps_its_engine(self):
        """DocumentDB scores its query UNIQUE_DELTA_THRESHOLD points above
        Aurora, so it earns its place -- the same threshold the first reality
        check uses, not the old 30-point specialist delta."""
        aurora_score = 60
        tables = {f"a{i}": "db.main" for i in range(10)} | {"d1": "db.docs"}
        analysis = _analysis(
            {
                "aurora_postgresql": {"db.main": 80, "db.docs": aurora_score},
                "documentdb": {"db.main": 30, "db.docs": aurora_score + UNIQUE_DELTA_THRESHOLD},
            }
        )
        assignments = _assignments(
            {"aurora_postgresql": [f"a{i}" for i in range(10)], "documentdb": ["d1"]}
        )

        revised, consolidations = resettle_after_corrections(
            assignments, {}, analysis, _collector(tables), source_engine="postgresql"
        )

        assert _engine_of(revised)["d1"] == "documentdb"
        assert consolidations == []

    def test_held_queries_stay_where_the_correction_put_them(self):
        """The corrected queries sit on the restored Aurora and score higher on
        DynamoDB; held, they stay, so Aurora is not removed a second time."""
        tables = {f"k{i}": "db.kv" for i in range(10)} | {"j1": "db.joins", "j2": "db.joins"}
        analysis = _analysis(
            {
                "aurora_mysql": {"db.kv": 50, "db.joins": 60},
                "dynamodb": {"db.kv": 90, "db.joins": 80},
            }
        )
        assignments = _assignments(
            {"dynamodb": [f"k{i}" for i in range(10)], "aurora_mysql": ["j1", "j2"]}
        )

        unheld, _ = resettle_after_corrections(
            assignments, {}, analysis, _collector(tables), source_engine="mysql"
        )
        held, consolidations = resettle_after_corrections(
            assignments,
            {},
            analysis,
            _collector(tables),
            source_engine="mysql",
            held_query_ids={"j1", "j2"},
        )

        assert _engine_of(unheld)["j1"] == "dynamodb"  # what the scores alone decide
        assert _engine_of(held)["j1"] == "aurora_mysql"
        assert _engine_of(held)["j2"] == "aurora_mysql"
        assert consolidations == []

    def test_assignments_are_copied(self):
        tables = {f"a{i}": "db.main" for i in range(10)} | {"d1": "db.docs"}
        analysis = _analysis(
            {"aurora_postgresql": {"db.main": 80, "db.docs": 70}, "documentdb": {"db.docs": 60}}
        )
        assignments = _assignments(
            {"aurora_postgresql": [f"a{i}" for i in range(10)], "documentdb": ["d1"]}
        )

        resettle_after_corrections(
            assignments, {}, analysis, _collector(tables), source_engine="postgresql"
        )

        assert _engine_of(assignments)["d1"] == "documentdb"

    @pytest.mark.parametrize(
        ("source", "expected"), [("mysql", "aurora_mysql"), ("postgresql", "aurora_postgresql")]
    )
    def test_the_source_defaults_to_the_collector(self, source, expected):
        """Each Aurora engine scores its own queries well above everything else,
        so both earn their place; DocumentDB's one query ties across all three
        engines, so DocumentDB goes and the tie between the two Aurora engines
        goes to the source-compatible one (ADR-031 Decision 2). No source is
        passed, so it comes from the collector. The three-way exact tie on
        every query is covered by the next test.
        """
        assignments = _assignments(
            {"aurora_mysql": ["m1", "m2"], "aurora_postgresql": ["p1", "p2"], "documentdb": ["d1"]}
        )
        analysis = _analysis(
            {
                "aurora_mysql": {"tm": 90, "tp": 50, "t": 80},
                "aurora_postgresql": {"tm": 50, "tp": 90, "t": 80},
                "documentdb": {"tm": 50, "tp": 50, "t": 80},
            }
        )
        collector = _collector({"m1": "tm", "m2": "tm", "p1": "tp", "p2": "tp", "d1": "t"})
        collector["metadata"] = {"source_database": {"engine": source}}

        revised, _ = resettle_after_corrections(assignments, {}, analysis, collector)

        assert _engine_of(revised)["d1"] == expected
        assert _engine_of(revised)["m1"] == "aurora_mysql"
        assert _engine_of(revised)["p1"] == "aurora_postgresql"

    @pytest.mark.parametrize(
        ("source", "expected"), [("mysql", "aurora_mysql"), ("postgresql", "aurora_postgresql")]
    )
    def test_a_three_way_exact_tie_keeps_the_collectors_source_engine(self, source, expected):
        """#600 regression, the fixture this test file first used: every
        engine scores every query identically, so the source-compatible
        Aurora engine survives Phase 1 and keeps all four queries (ADR-031
        Decisions 2 and 13: DocumentDB's $200 against Aurora's $250 is 20%
        and $50, short of both cost thresholds)."""
        assignments = _assignments(
            {"aurora_mysql": ["m1", "m2"], "aurora_postgresql": ["p1"], "documentdb": ["d1"]}
        )
        analysis = _analysis(
            {"aurora_mysql": {"t": 80}, "aurora_postgresql": {"t": 80}, "documentdb": {"t": 80}}
        )
        collector = _collector({"m1": "t", "m2": "t", "p1": "t", "d1": "t"})
        collector["metadata"] = {"source_database": {"engine": source}}

        revised, _ = resettle_after_corrections(assignments, {}, analysis, collector)

        assert _engine_of(revised) == dict.fromkeys(("m1", "m2", "p1", "d1"), expected)

    def test_a_partial_move_names_the_held_queries_as_corrected(self):
        """Aurora keeps its two held queries and loses the one DynamoDB scores
        higher; the record says why the two stayed, not "mandatory signal"."""
        tables = (
            {f"k{i}": "db.kv" for i in range(10)}
            | {"j1": "db.joins", "j2": "db.joins"}
            | {"x1": "db.kv"}
        )
        analysis = _analysis(
            {
                "aurora_mysql": {"db.kv": 50, "db.joins": 80},
                "dynamodb": {"db.kv": 90, "db.joins": 40},
            }
        )
        assignments = _assignments(
            {"dynamodb": [f"k{i}" for i in range(10)], "aurora_mysql": ["j1", "j2", "x1"]}
        )

        revised, consolidations = resettle_after_corrections(
            assignments,
            {},
            analysis,
            _collector(tables),
            source_engine="mysql",
            held_query_ids={"j1", "j2"},
        )

        assert _engine_of(revised)["x1"] == "dynamodb"
        [record] = consolidations
        assert (record["from_engine"], record["to_engine"], record["action"]) == (
            "aurora_mysql",
            "dynamodb",
            "partial",
        )
        assert record["queries_retained"] == ["j1", "j2"]
        assert record["retention_reason"] == (
            "Corrected queries stay where the correction placed them"
        )


class TestHeldQueries:
    """What holding a query does, and what it must not do (#588 review)."""

    def test_a_held_query_on_a_floor_failing_engine_is_not_moved_by_the_floor(self):
        """OpenSearch's three signal-pinned queries fail its justification
        floor (no search depth, no traffic) and move to Aurora; the held query
        on OpenSearch fails the same floor but stays where it was put."""
        os_ids = ["os1", "os2", "os3"]
        tables = {f"a{i}": "db.main" for i in range(10)} | {q: "db.posts" for q in [*os_ids, "h1"]}
        analysis = _analysis(
            {
                "aurora_mysql": {"db.main": 90, "db.posts": 70},
                "opensearch": {"db.main": 10, "db.posts": 75},
            }
        )
        assignments = _assignments({"aurora_mysql": [f"a{i}" for i in range(10)]}) + [
            {
                "query_id": qid,
                "assigned_engine": "opensearch",
                "assignment_reason": "signal override: text_search",
                "signal_override": "text_search",
            }
            for qid in os_ids
        ]
        assignments.append(
            {"query_id": "h1", "assigned_engine": "opensearch", "assignment_reason": "corrected"}
        )
        triage = {
            "signals": [{"signal": "text_search", "query_ids": [*os_ids, "h1"]}],
            "query_capabilities": {q: ["inverted_index"] for q in [*os_ids, "h1"]},
        }

        revised, _ = resettle_after_corrections(
            assignments,
            triage,
            analysis,
            _collector(tables),
            source_engine="mysql",
            held_query_ids={"h1"},
        )

        engine_of = _engine_of(revised)
        assert {engine_of[q] for q in os_ids} == {"aurora_mysql"}
        assert engine_of["h1"] == "opensearch"

    def test_a_held_query_does_not_make_its_engine_mandatory(self):
        """A held query on DocumentDB keeps DocumentDB committed, but does not
        put it under the cost-share floor (which applies to signal-pinned
        engines only): DocumentDB serves 2 of 42 queries at $200/mo, which
        would fail that floor and move ``u1``, the query it scores best."""
        tables = {f"a{i}": "db.main" for i in range(40)} | {"h1": "db.docs"} | {"u1": "db.docs"}
        analysis = _analysis(
            {
                "aurora_mysql": {"db.main": 90, "db.docs": 50},
                "documentdb": {"db.main": 30, "db.docs": 90},
            }
        )
        assignments = _assignments(
            {"aurora_mysql": [f"a{i}" for i in range(40)], "documentdb": ["h1", "u1"]}
        )

        revised, consolidations = resettle_after_corrections(
            assignments,
            {},
            analysis,
            _collector(tables),
            source_engine="mysql",
            held_query_ids={"h1"},
        )

        assert _engine_of(revised)["h1"] == "documentdb"
        assert _engine_of(revised)["u1"] == "documentdb"
        assert consolidations == []

    def test_a_customer_pick_is_left_out_not_held(self):
        """The reviewer's repro, in small: the first run removed DocumentDB
        (its 8 queries score 5 points better there than on Aurora, not
        enough to earn its place). The customer then pins one other query to
        DocumentDB. Re-settling must leave the pick out, as the first run
        does, so it never commits DocumentDB and pulls the 8 back."""
        doc_ids = [f"d{i}" for i in range(8)]
        tables = (
            {f"a{i}": "db.main" for i in range(20)}
            | {qid: "db.docs" for qid in doc_ids}
            | {"c1": "db.main"}
        )
        analysis = _analysis(
            {
                "aurora_mysql": {"db.main": 90, "db.docs": 70},
                "documentdb": {"db.main": 30, "db.docs": 75},
            }
        )
        assignments = _assignments({"aurora_mysql": [f"a{i}" for i in range(20)] + doc_ids})
        assignments.append(
            {
                "query_id": "c1",
                "assigned_engine": "documentdb",
                "assignment_reason": "customer override",
                "customer_override": True,
            }
        )

        revised, consolidations = resettle_after_corrections(
            assignments, {}, analysis, _collector(tables), source_engine="mysql"
        )

        engine_of = _engine_of(revised)
        assert {engine_of[q] for q in doc_ids} == {"aurora_mysql"}
        assert engine_of["c1"] == "documentdb"
        assert consolidations == []
        assert [qa["query_id"] for qa in revised] == [qa["query_id"] for qa in assignments]
