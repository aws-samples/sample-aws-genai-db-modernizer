"""A table's catalog pattern score never drops when more evidence is found (#784).

Each engine's catalog gives every pattern a base score: how strongly that
pattern calls for the engine. A table's ``pattern_match_score`` used to be the
calls-weighted mean of its patterns' base scores, with each pattern weighted
by its calls across the whole workload. Two problems followed:

- finding one more pattern with a lower base score pulled the mean down, so
  more evidence that a table needs an engine lowered that engine's fit
  (discourse's ``web_crawler_requests``, Aurora PostgreSQL 79 -> 78, once #781
  read its OTHER-typed queries);
- a pattern's weight counted its queries on every table, so a table's score
  moved when another table's traffic changed.

The rule now, the same for every engine with a pattern catalog (Aurora,
DynamoDB, DocumentDB, OpenSearch): the score is the highest base score among
the catalog patterns found on the table. Another pattern or another matching
query can only keep it or raise it, and nothing outside the table changes it.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from src.contracts.analysis_output import Confidence, Pattern, WorkloadAnalysis
from src.tools.analysis import (
    aurora_common_analysis_tools,
    documentdb_analysis_tools,
    dynamodb_analysis_tools,
    opensearch_analysis_tools,
)
from src.tools.analysis.aurora_common_pattern_catalog import PATTERN_BY_ID as COMMON_PATTERN_BY_ID
from src.tools.analysis.aurora_mysql_analysis_tools import analyze_aurora_mysql_patterns
from src.tools.analysis.aurora_pg_analysis_tools import analyze_aurora_pg_patterns
from src.tools.analysis.aurora_pg_pattern_catalog import PG_PATTERN_BY_ID
from src.tools.analysis.documentdb_pattern_catalog import PATTERN_BY_ID as DOCDB_BY_ID
from src.tools.analysis.dynamodb_pattern_catalog import PATTERN_BY_ID as DDB_BY_ID
from src.tools.analysis.opensearch_pattern_catalog import PATTERN_BY_ID as OS_BY_ID
from src.tools.analysis.scoring import catalog_pattern_score

AURORA_PG_BY_ID = {**COMMON_PATTERN_BY_ID, **PG_PATTERN_BY_ID}
CATALOGS = {
    "aurora_postgresql": AURORA_PG_BY_ID,
    "dynamodb": DDB_BY_ID,
    "documentdb": DOCDB_BY_ID,
    "opensearch": OS_BY_ID,
}


def _pattern(pattern_id: str, query_ids: list[str], table_ids: list[str] | None = None) -> Pattern:
    return Pattern(
        pattern_id=pattern_id,
        pattern_type=pattern_id,
        confidence=Confidence.MEDIUM,
        query_ids=query_ids,
        table_ids=table_ids or ["db.t"],
    )


def _collector(cps: dict[str, float], tables: dict[str, list[str]] | None = None) -> dict:
    return {
        "queries": {
            "query_patterns": [
                {
                    "query_id": qid,
                    "calls_per_second": value,
                    "tables_accessed": (tables or {}).get(qid, ["db.t"]),
                }
                for qid, value in cps.items()
            ]
        }
    }


# Each engine's own entry point for the table score, called the way its
# analysis module calls it.
def _aurora(patterns, collector):
    return aurora_common_analysis_tools.compute_catalog_pattern_score(
        patterns, collector, AURORA_PG_BY_ID
    )


ENGINE_SCORERS = {
    "aurora_postgresql": _aurora,
    "dynamodb": dynamodb_analysis_tools._compute_catalog_pattern_score,
    "documentdb": documentdb_analysis_tools._compute_catalog_pattern_score,
    "opensearch": opensearch_analysis_tools._compute_catalog_pattern_score,
}


def _strongest_and_weakest(engine: str) -> tuple[str, str]:
    by_score = sorted(CATALOGS[engine].items(), key=lambda kv: kv[1].base_score)
    return by_score[-1][0], by_score[0][0]


class TestTheRule:
    def test_no_pattern_scores_zero(self):
        assert catalog_pattern_score([], AURORA_PG_BY_ID) == 0

    def test_unknown_patterns_score_zero(self):
        assert catalog_pattern_score([_pattern("not-in-catalog", ["q1"])], AURORA_PG_BY_ID) == 0

    def test_the_score_is_the_strongest_pattern(self):
        patterns = [_pattern("aurora-common-10", ["q1"]), _pattern("aurora-common-01", ["q2"])]
        assert catalog_pattern_score(patterns, AURORA_PG_BY_ID) == 90

    def test_order_does_not_matter(self):
        a, b = _pattern("aurora-common-10", ["q1"]), _pattern("aurora-common-01", ["q2"])
        assert catalog_pattern_score([a, b], AURORA_PG_BY_ID) == catalog_pattern_score(
            [b, a], AURORA_PG_BY_ID
        )


@pytest.mark.parametrize("engine", sorted(ENGINE_SCORERS))
class TestEveryEngine:
    def test_a_weaker_pattern_on_hot_traffic_does_not_lower_the_score(self, engine):
        # The #784 case: the new pattern has a lower base score and more calls.
        strong, weak = _strongest_and_weakest(engine)
        score = ENGINE_SCORERS[engine]
        collector = _collector({"q1": 0.1, "q2": 50.0})
        before = score([_pattern(strong, ["q1"])], collector)
        after = score([_pattern(strong, ["q1"]), _pattern(weak, ["q2"])], collector)
        assert before == CATALOGS[engine][strong].base_score
        assert after >= before

    def test_another_tables_traffic_does_not_move_the_score(self, engine):
        # A pattern's queries on other tables used to weigh in.
        strong, weak = _strongest_and_weakest(engine)
        score = ENGINE_SCORERS[engine]
        tables = {"q1": ["db.t"], "q2": ["db.t"], "q3": ["db.other"]}
        quiet = _collector({"q1": 1.0, "q2": 1.0, "q3": 0.0}, tables)
        busy = _collector({"q1": 1.0, "q2": 1.0, "q3": 500.0}, tables)
        patterns = [_pattern(strong, ["q1"]), _pattern(weak, ["q2", "q3"])]
        assert score(patterns, quiet) == score(patterns, busy)

    def test_the_engine_uses_the_shared_rule(self, engine):
        strong, weak = _strongest_and_weakest(engine)
        patterns = [_pattern(weak, ["q1"]), _pattern(strong, ["q2"])]
        collector = _collector({"q1": 9.0, "q2": 0.0})
        assert ENGINE_SCORERS[engine](patterns, collector) == catalog_pattern_score(
            patterns, CATALOGS[engine]
        )


@pytest.mark.parametrize("engine", sorted(CATALOGS))
@given(data=st.data())
def test_adding_evidence_never_lowers_the_score(engine, data):
    ids = sorted(CATALOGS[engine])
    found = data.draw(st.lists(st.sampled_from(ids), max_size=6))
    extra = data.draw(st.lists(st.sampled_from(ids), min_size=1, max_size=4))
    before = catalog_pattern_score([_pattern(p, ["q"]) for p in found], CATALOGS[engine])
    after = catalog_pattern_score([_pattern(p, ["q"]) for p in found + extra], CATALOGS[engine])
    assert after >= before


# --- Aurora's table recommendation, end to end ------------------------------

AURORA_ANALYSES = {
    "aurora_postgresql": analyze_aurora_pg_patterns,
    "aurora_mysql": analyze_aurora_mysql_patterns,
}


def _schema_collector(cps: dict[str, float], fk_heavy: bool = False) -> dict:
    """``db.t`` and its queries; with ``fk_heavy``, three foreign keys out of
    ``db.t`` and three into it, so its relational baseline is the 65 cap."""
    out_fks = [{"referenced_table": f"db.p{i}", "columns": [f"p{i}_id"]} for i in range(3)]
    tables = [
        {
            "table_id": "db.t",
            "columns": [],
            "primary_key": {"columns": ["id"]},
            "foreign_keys": out_fks if fk_heavy else [],
            "row_count": 1000,
            "size_mb": 1.0,
        }
    ]
    if fk_heavy:
        tables += [
            {
                "table_id": f"db.c{i}",
                "columns": [],
                "primary_key": {"columns": ["id"]},
                "foreign_keys": [{"referenced_table": "db.t", "columns": ["t_id"]}],
                "row_count": 10,
                "size_mb": 0.1,
            }
            for i in range(3)
        ]
    queries = [
        {
            "query_id": qid,
            "query_type": "SELECT",
            "query_text": "SELECT * FROM t WHERE id = ?",
            "tables_accessed": ["db.t"],
            "calls_per_second": value,
            "has_joins": False,
            "join_count": 0,
            "rows_returned_avg": 1.0,
        }
        for qid, value in cps.items()
    ]
    return {"database_schema": {"tables": tables}, "queries": {"query_patterns": queries}}


def _table_rec(engine: str, collector: dict, patterns: list[Pattern]):
    recs = AURORA_ANALYSES[engine](collector, WorkloadAnalysis(patterns_detected=patterns))
    return next(r for r in recs if r.table_id == "db.t")


@pytest.mark.parametrize("engine", sorted(AURORA_ANALYSES))
def test_aurora_a_weaker_relational_pattern_never_lowers_the_table(engine):
    # discourse's web_crawler_requests: one more, weaker, busier pattern (#784).
    collector = _schema_collector({"q1": 0.1, "q2": 50.0})
    join = _pattern("aurora-common-01", ["q1"])  # complex-join, 90
    crud = _pattern("aurora-common-10", ["q2"])  # standard-crud, 60
    before = _table_rec(engine, collector, [join])
    after = _table_rec(engine, collector, [join, crud])
    assert after.score_breakdown.pattern_match_score >= before.score_breakdown.pattern_match_score
    assert after.confidence_score >= before.confidence_score


@pytest.mark.parametrize("engine", sorted(AURORA_ANALYSES))
def test_aurora_a_first_pattern_under_the_relational_floor_never_lowers_the_table(engine):
    # With no catalog pattern the table scores its relational baseline (the 65
    # cap here, before the engine's own adjustments); finding standard-crud
    # (base score 60) used to replace that with 60.
    collector = _schema_collector({"q1": 1.0}, fk_heavy=True)
    before = _table_rec(engine, collector, [])
    after = _table_rec(engine, collector, [_pattern("aurora-common-10", ["q1"])])
    assert after.score_breakdown.pattern_match_score >= before.score_breakdown.pattern_match_score
    assert after.confidence_score >= before.confidence_score
