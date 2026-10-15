"""The reality check settles engines with one shared rule (ADR-031 Decision 7).

An engine is removed when the committed engines can serve its queries, judged
with the shared per-query scores and the shared eligibility gate, and each
moved query goes to its highest-scoring remaining engine. These tests drive
``run_reality_check`` itself.

The rule replaced the old Aurora absorption pass, which had its own decision
rule: only engines under 10 queries (5 for a signal-pinned one) were
considered, Aurora had to score at least 50, a specialist 30 points ahead kept
its query, and every absorbed query went to Aurora. Each test below would have
decided differently under that rule. The post-correction re-settle (#588,
``resettle_after_corrections`` and its held queries) ran this same rule a
second time after validation corrections; since model flags re-route only
among the engines the check kept and never re-settle (ADR-031 Amendment 2,
#647) nothing called it, so it was deleted (#616) and these tests moved here.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

import src.agents.referee.reality_check as reality_check
from src.agents.referee.reality_check import UNIQUE_DELTA_THRESHOLD, run_reality_check

# reality_check.py and the step modules it is split into (#617).
REALITY_CHECK_SOURCES = sorted(Path(reality_check.__file__).parent.glob("reality_check*.py"))


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


def _settle(
    assignments: list[dict],
    triage: dict,
    analysis: dict,
    collector: dict,
    source_engine: str | None = None,
) -> tuple[list[dict], list[dict]]:
    """``run_reality_check`` on ``assignments``: (revised assignments, records)."""
    result = run_reality_check(
        {"query_assignments": assignments},
        triage,
        analysis,
        collector,
        source_engine=source_engine,
    )
    return result["revised_assignments"], result["consolidations"]


def _engine_of(revised: list[dict]) -> dict[str, str]:
    return {qa["query_id"]: qa["assigned_engine"] for qa in revised}


class TestTheAuroraOnlyRuleIsGone:
    def test_no_aurora_absorption_thresholds_or_pass_remain(self):
        """ADR-031 Decision 7 supersedes the Aurora-only pass "with its own
        fitness gate and thresholds"; nothing in the module defines them."""
        trees = [ast.parse(path.read_text()) for path in REALITY_CHECK_SOURCES]
        defined = {
            node.id
            for tree in trees
            for node in ast.walk(tree)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
        } | {
            node.name
            for tree in trees
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


class TestNoPassThatDecidesNothingRemains:
    """#616: a pass that decides nothing for any input is deleted, not kept."""

    def test_the_post_correction_resettle_is_gone(self):
        """Nothing called ``resettle_after_corrections`` once model flags
        stopped re-settling (ADR-031 Amendment 2), and holding a query was
        only ever its input."""
        assert not hasattr(reality_check, "resettle_after_corrections")
        assert "held_query_ids" not in inspect.signature(run_reality_check).parameters

    def test_no_code_records_a_reserved_pass(self):
        """``candidates``, ``heterogeneous_choice`` and ``resettle`` are reserved
        names kept in the contract (``src/contracts/move_ledger.py``): no writer
        records a Move with them."""
        src = Path(reality_check.__file__).parents[2]
        reserved = ("MovePass.CANDIDATES", "MovePass.HETEROGENEOUS_CHOICE", "MovePass.RESETTLE")
        users = [
            f"{path.relative_to(src)}: {name}"
            for path in src.rglob("*.py")
            for name in reserved
            if name in path.read_text()
        ]
        assert users == []

    def test_no_aurora_absorption_summary_is_reported(self):
        """The ``aurora_absorption`` block summarised the old absorption pass;
        nothing read it, and the moves it counted are the consolidation records."""
        tables = {f"a{i}": "db.main" for i in range(10)} | {"d1": "db.docs"}
        analysis = _analysis(
            {"aurora_postgresql": {"db.main": 80, "db.docs": 70}, "documentdb": {"db.docs": 60}}
        )
        assignments = _assignments(
            {"aurora_postgresql": [f"a{i}" for i in range(10)], "documentdb": ["d1"]}
        )

        result = run_reality_check(
            {"query_assignments": assignments}, {}, analysis, _collector(tables)
        )

        assert "aurora_absorption" not in result
        assert [(c["from_engine"], c["to_engine"]) for c in result["consolidations"]] == [
            ("documentdb", "aurora_postgresql")
        ]


class TestTheSharedRule:
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

        revised, consolidations = _settle(
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

        revised, _ = _settle(
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

        revised, consolidations = _settle(
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

        revised, consolidations = _settle(
            assignments, {}, analysis, _collector(tables), source_engine="postgresql"
        )

        assert _engine_of(revised)["d1"] == "documentdb"
        assert consolidations == []

    def test_assignments_are_copied(self):
        tables = {f"a{i}": "db.main" for i in range(10)} | {"d1": "db.docs"}
        analysis = _analysis(
            {"aurora_postgresql": {"db.main": 80, "db.docs": 70}, "documentdb": {"db.docs": 60}}
        )
        assignments = _assignments(
            {"aurora_postgresql": [f"a{i}" for i in range(10)], "documentdb": ["d1"]}
        )

        _settle(assignments, {}, analysis, _collector(tables), source_engine="postgresql")

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

        revised, _ = _settle(assignments, {}, analysis, collector)

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

        revised, _ = _settle(assignments, {}, analysis, collector)

        assert _engine_of(revised) == dict.fromkeys(("m1", "m2", "p1", "d1"), expected)
