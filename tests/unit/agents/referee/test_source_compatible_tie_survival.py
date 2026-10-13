"""Phase 1 keeps the source-compatible engine when it scores at least as well (#600).

ADR-031 Decision 2 gives an exact tie to the source-compatible Aurora engine.
Phase 1 of the reality check used to pick which of two mutually redundant
engines to remove by burden per unique query alone, so Aurora's higher
standing cost ($250 against DocumentDB's $200) removed it even though every
query tied. The maintainer decision of 2026-10-13 (ADR-031 Decision 13): on an
exact tie the source-compatible engine survives, unless another tied engine is
at least 40% cheaper in standing cost AND saves at least $100 a month. The same
holds when the source-compatible engine scores every query equal to or higher
than its best other committed engine without enough margin to count as unique:
nobody picks a NoSQL engine over the source-compatible SQL engine for a small
saving.
"""

from __future__ import annotations

import pytest

import src.agents.referee.reality_check as reality_check
from src.agents.referee.reality_check import UNIQUE_DELTA_THRESHOLD, run_reality_check


def _run(
    engines: dict[str, list[str]],
    scores: dict[str, int] | dict[str, dict[str, int]],
    source: str,
    tables: dict[str, str] | None = None,
) -> dict[str, str]:
    """``scores`` is each engine's confidence for table ``t`` (an int), or per
    table (a dict); ``tables`` maps a query to its table (default ``t``)."""
    tables = tables or {}
    assignments = [
        {"query_id": qid, "assigned_engine": engine, "assignment_reason": "test"}
        for engine, qids in engines.items()
        for qid in qids
    ]
    collector = {
        "queries": {
            "query_patterns": [
                {"query_id": qid, "tables_accessed": [tables.get(qid, "t")], "query_type": "SELECT"}
                for qids in engines.values()
                for qid in qids
            ]
        },
        "metadata": {"source_database": {"engine": source}},
    }
    analysis = {
        engine: {
            "table_recommendations": [
                {"table_id": table, "confidence_score": score}
                for table, score in (
                    by_table if isinstance(by_table, dict) else {"t": by_table}
                ).items()
            ]
        }
        for engine, by_table in scores.items()
    }
    result = run_reality_check({"query_assignments": assignments}, {}, analysis, collector)
    return {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]}


THREE_WAY = {"aurora_mysql": ["m1", "m2"], "aurora_postgresql": ["p1"], "documentdb": ["d1"]}
THREE_WAY_SCORES = {"aurora_mysql": 80, "aurora_postgresql": 80, "documentdb": 80}

TWO_WAY = {"aurora_mysql": ["m1", "m2"], "documentdb": ["d1"]}
TWO_WAY_SCORES = {"aurora_mysql": 80, "documentdb": 80}


def _costs(monkeypatch: pytest.MonkeyPatch, aurora: float, documentdb: float) -> None:
    monkeypatch.setitem(reality_check.ENGINE_BASE_COST, "aurora_mysql", aurora)
    monkeypatch.setitem(reality_check.ENGINE_BASE_COST, "documentdb", documentdb)


class TestTheIssueRepro:
    """$250 against $200 is 20% and $50: neither threshold is met."""

    def test_a_mysql_source_keeps_aurora_mysql(self):
        placed = _run(THREE_WAY, THREE_WAY_SCORES, "mysql")
        assert placed == dict.fromkeys(("m1", "m2", "p1", "d1"), "aurora_mysql")

    def test_a_postgresql_source_keeps_aurora_postgresql(self):
        placed = _run(THREE_WAY, THREE_WAY_SCORES, "postgresql")
        assert placed == dict.fromkeys(("m1", "m2", "p1", "d1"), "aurora_postgresql")


class TestTheCostException:
    def test_40_percent_and_100_dollars_both_met_the_cheaper_engine_wins(self, monkeypatch):
        _costs(monkeypatch, aurora=500, documentdb=250)  # 50%, $250
        placed = _run(TWO_WAY, TWO_WAY_SCORES, "mysql")
        assert placed == dict.fromkeys(("m1", "m2", "d1"), "documentdb")

    def test_45_percent_cheaper_but_only_60_dollars_saved_aurora_stays(self, monkeypatch):
        aurora = 400 / 3  # 45% of $133.33 is $60
        _costs(monkeypatch, aurora=aurora, documentdb=aurora * 0.55)
        placed = _run(TWO_WAY, TWO_WAY_SCORES, "mysql")
        assert placed == dict.fromkeys(("m1", "m2", "d1"), "aurora_mysql")

    def test_150_dollars_saved_but_only_30_percent_cheaper_aurora_stays(self, monkeypatch):
        _costs(monkeypatch, aurora=500, documentdb=350)
        placed = _run(TWO_WAY, TWO_WAY_SCORES, "mysql")
        assert placed == dict.fromkeys(("m1", "m2", "d1"), "aurora_mysql")

    @pytest.mark.parametrize(
        ("aurora", "documentdb", "expected"),
        [
            (250, 150, "documentdb"),  # exactly 40% and exactly $100
            (250, 150.01, "aurora_mysql"),  # just under both
            (500, 300, "documentdb"),  # exactly 40%, $200
            (500, 300.01, "aurora_mysql"),  # just under 40%
            (200, 100, "documentdb"),  # 50%, exactly $100
            (200, 100.01, "aurora_mysql"),  # just under $100
        ],
    )
    def test_the_thresholds_are_inclusive_at_the_boundary(
        self, monkeypatch, aurora, documentdb, expected
    ):
        _costs(monkeypatch, aurora=aurora, documentdb=documentdb)
        placed = _run(TWO_WAY, TWO_WAY_SCORES, "mysql")
        assert placed == dict.fromkeys(("m1", "m2", "d1"), expected)


class TestANonTieIsUnaffected:
    def test_a_strictly_higher_redundant_engine_still_takes_the_queries(self):
        """DocumentDB scores every query a little higher (not a tie, and not
        unique value either), so fixed cost decides as before: Aurora goes."""
        scores = {"aurora_mysql": 80, "documentdb": 80 + UNIQUE_DELTA_THRESHOLD // 2}
        placed = _run(TWO_WAY, scores, "mysql")
        assert placed == dict.fromkeys(("m1", "m2", "d1"), "documentdb")


class TestAHigherScoringSourceCompatibleEngineIsNotRemovedOnCost:
    """Aurora scores every query a little higher than DocumentDB, below
    ``UNIQUE_DELTA_THRESHOLD``: the burden rank used to remove it on cost."""

    @pytest.mark.parametrize("margin", [1, 3, 5])
    def test_aurora_stays_when_the_alternative_is_20_percent_and_50_dollars_cheaper(self, margin):
        scores = {"aurora_mysql": 80 + margin, "documentdb": 80}  # $250 vs $200
        placed = _run(TWO_WAY, scores, "mysql")
        assert placed == dict.fromkeys(("m1", "m2", "d1"), "aurora_mysql")

    def test_the_alternative_wins_when_45_percent_and_150_dollars_cheaper(self, monkeypatch):
        aurora = 1000 / 3  # 45% of $333.33 is $150
        _costs(monkeypatch, aurora=aurora, documentdb=aurora * 0.55)
        scores = {"aurora_mysql": 83, "documentdb": 80}
        placed = _run(TWO_WAY, scores, "mysql")
        assert placed == dict.fromkeys(("m1", "m2", "d1"), "documentdb")

    def test_aurora_lower_on_some_queries_keeps_the_existing_rules(self):
        """Aurora is higher on ``m1``'s table and lower on ``m2``'s: not every
        query scores at least as well, so the burden rank decides as before
        and Aurora's higher standing cost removes it."""
        scores = {
            "aurora_mysql": {"t_hi": 85, "t_lo": 75, "t": 80},
            "documentdb": {"t_hi": 80, "t_lo": 80, "t": 80},
        }
        tables = {"m1": "t_hi", "m2": "t_lo", "d1": "t"}
        placed = _run(TWO_WAY, scores, "mysql", tables)
        assert placed == dict.fromkeys(("m1", "m2", "d1"), "documentdb")


class TestTheBurdenPathWithUniqueQueries:
    """#603 review: Aurora MySQL wins ``m1`` by at least
    ``UNIQUE_DELTA_THRESHOLD`` points and three engines are committed, so
    the burden-per-unique-query path (not the no-unique-value path) decides
    whether it is removed."""

    ENGINES = {
        "aurora_mysql": ["m1"],
        "aurora_postgresql": [f"p{i}" for i in range(6)],
        "documentdb": [f"d{i}" for i in range(6)],
    }
    SCORES = {
        "aurora_mysql": {"t_m": 95, "t_p": 70, "t_d": 70},
        "aurora_postgresql": {"t_m": 80, "t_p": 95, "t_d": 70},
        "documentdb": {"t_m": 80, "t_p": 70, "t_d": 95},
    }
    TABLES = {"m1": "t_m"} | {f"p{i}": "t_p" for i in range(6)} | {f"d{i}": "t_d" for i in range(6)}

    def test_aurora_mysql_is_not_removed_on_cost(self):
        assert self.SCORES["aurora_mysql"]["t_m"] - 80 >= UNIQUE_DELTA_THRESHOLD
        placed = _run(self.ENGINES, self.SCORES, "mysql", self.TABLES)
        assert placed["m1"] == "aurora_mysql"

    def test_a_cheap_enough_alternative_still_wins_on_this_path(self, monkeypatch):
        # Both of m1's best alternatives are at least 40% and $100 cheaper.
        monkeypatch.setitem(reality_check.ENGINE_BASE_COST, "aurora_mysql", 500)
        monkeypatch.setitem(reality_check.ENGINE_BASE_COST, "aurora_postgresql", 250)
        monkeypatch.setitem(reality_check.ENGINE_BASE_COST, "documentdb", 200)
        placed = _run(self.ENGINES, self.SCORES, "mysql", self.TABLES)
        assert placed["m1"] in {"aurora_postgresql", "documentdb"}
