"""Synthesis writes ``migration_waves`` to report.json deterministically (#225).

End to end through ``run_synthesis_deterministic``: the roadmap is built from
the same assignment artifact every other synthesis builder reads (ranking,
cache overlay, table assignments, co-dependency groups), with no model call.
"""

from __future__ import annotations

import pytest

from src.agents.referee.synthesis_handler import run_synthesis_deterministic
from src.storage.local_store import LocalArtifactStore

DB = "shop"
JOB = "job-waves"


def _query(qid: str, cps: float, table: str) -> dict:
    return {
        "query_id": qid,
        "query_text": f"SELECT * FROM {table} WHERE id = ?",
        "query_type": "SELECT",
        "calls_per_second": cps,
        "rows_returned_avg": 1,
        "tables_accessed": [table],
    }


QUERIES = [
    _query("hot1", 6.0, "users"),
    _query("kv1", 3.0, "users"),
    _query("kv2", 3.0, "sessions"),
    _query("rel1", 2.0, "orders"),
]

ASSIGNMENT = {
    "job_id": JOB,
    "version": 2,
    "query_assignments": [
        {
            "query_id": "hot1",
            "assigned_engine": "dynamodb",
            "source_tables": ["users"],
            "assignment_reason": "highest confidence for dynamodb",
            "cache_engine": "elasticache",
            "cache_pattern": "point_lookup",
        },
        {
            "query_id": "kv1",
            "assigned_engine": "dynamodb",
            "source_tables": ["users"],
            "assignment_reason": "highest confidence for dynamodb",
        },
        {
            "query_id": "kv2",
            "assigned_engine": "dynamodb",
            "source_tables": ["sessions"],
            "assignment_reason": "highest confidence for dynamodb",
        },
        {
            "query_id": "rel1",
            "assigned_engine": "aurora_mysql",
            "source_tables": ["orders"],
            "assignment_reason": "highest confidence for aurora_mysql",
        },
    ],
    "table_assignments": [
        {
            "table_id": "users",
            "primary_engine": "dynamodb",
            "engines": ["dynamodb"],
            "query_count": 2,
        },
        {
            "table_id": "sessions",
            "primary_engine": "dynamodb",
            "engines": ["dynamodb"],
            "query_count": 1,
        },
        {
            "table_id": "orders",
            "primary_engine": "aurora_mysql",
            "engines": ["aurora_mysql"],
            "query_count": 1,
        },
    ],
    "co_dependency_groups": [["kv1", "kv2"]],
}


def _analysis(conf: int) -> dict:
    return {
        "table_recommendations": [
            {"table_id": "users", "confidence_score": conf},
            {"table_id": "sessions", "confidence_score": conf},
            {"table_id": "orders", "confidence_score": conf},
        ],
        "workload_analysis": {"patterns_detected": [], "anti_patterns_detected": []},
        "cost_estimate": {"monthly_cost_usd": 10},
    }


@pytest.fixture
def store(tmp_path):
    s = LocalArtifactStore(base_dir=str(tmp_path))
    engines = ["dynamodb", "aurora_mysql", "elasticache"]
    s.write_json(
        f"{DB}/{JOB}/referee-triage/triage.json",
        {"selected_agents": [{"agent_type": e} for e in engines], "signals": []},
    )
    s.write_json(
        f"{DB}/{JOB}/collector/output.json",
        {
            "metadata": {"source_database": {"engine": "mysql"}},
            "database_schema": {
                "tables": [{"table_id": "users"}, {"table_id": "sessions"}, {"table_id": "orders"}]
            },
            "queries": {"query_patterns": QUERIES},
        },
    )
    conf = {"dynamodb": 90, "aurora_mysql": 60, "elasticache": 80}
    for engine in engines:
        s.write_json(f"{DB}/{JOB}/analysis-{engine}/analysis.json", _analysis(conf[engine]))
    s.write_json(f"{DB}/{JOB}/assignment/v2/assignment.json", ASSIGNMENT)
    return s


class TestMigrationWaves:
    def test_waves_present_in_the_deterministic_result(self, store):
        result = run_synthesis_deterministic(JOB, DB, store, assignment_version=2)
        waves = result["migration_waves"]
        assert waves is not None
        engine_lists = [w["engines"] for w in waves]
        assert engine_lists == [["elasticache"], ["dynamodb"], ["aurora_mysql"]]
        assert [w["wave"] for w in waves] == [1, 2, 3]

    def test_cache_wave_is_first_and_reversible(self, store):
        result = run_synthesis_deterministic(JOB, DB, store, assignment_version=2)
        cache = result["migration_waves"][0]
        assert cache["query_count"] == 1
        assert cache["share_basis"] == "calls"
        assert "reversible" in cache["rationale"]

    def test_dynamodb_wave_respects_co_dependency_groups(self, store):
        result = run_synthesis_deterministic(JOB, DB, store, assignment_version=2)
        dynamo = result["migration_waves"][1]
        assert sorted(dynamo["tables"]) == ["sessions", "users"]
        assert dynamo["table_groups"] == [{"tables": ["sessions", "users"], "query_count": 3}]

    def test_retained_wave_is_last_and_carried_over_1_to_1(self, store):
        result = run_synthesis_deterministic(JOB, DB, store, assignment_version=2)
        retained = result["migration_waves"][-1]
        assert retained["engines"] == ["aurora_mysql"]
        assert retained["tables"] == ["orders"]
        assert "carried over 1:1" in retained["rationale"]

    def test_written_report_round_trips_through_the_contract(self, store):
        from src.agents.referee.synthesis_handler import _write_synthesis_report

        result = run_synthesis_deterministic(JOB, DB, store, assignment_version=2)
        _write_synthesis_report(store, result, assignment_version=2)
        report = store.read_json(f"{DB}/{JOB}/synthesis/v2/report.json")
        assert report["contract_version"] == "1.4"
        assert len(report["migration_waves"]) == 3
        assert report["migration_waves"][1]["engines"] == ["dynamodb"]
