"""Cache overlay downstream of the assignment (#296).

The cache layer owns no query, so it is never an owner share of the workload: its
schema-design scope is the queries it fronts, synthesis reports it as a cache
layer with its own counts, and after schema design the safety net drops the
overlay of cached queries its design does not serve (owner unchanged).
"""

from __future__ import annotations

import pytest

from src.agents.referee.post_schema_router import route_unsupported_queries
from src.agents.referee.synthesis_data import load_synthesis_data
from src.agents.referee.synthesis_grounding import build_fallback_summary
from src.agents.referee.synthesis_handler import run_synthesis_deterministic
from src.storage.assignment_versioning import (
    engine_scope,
    engines_with_in_scope_queries,
)
from src.storage.local_store import LocalArtifactStore

DB = "shop"
JOB = "job-cache-overlay"


def _query(qid: str, cps: float, table: str = "users") -> dict:
    return {
        "query_id": qid,
        "query_text": f"SELECT * FROM {table} WHERE id = ?",
        "query_type": "SELECT",
        "calls_per_second": cps,
        "rows_returned_avg": 1,
        "tables_accessed": [table],
    }


QUERIES = [_query("hot1", 6.0), _query("hot2", 2.0, "orders"), _query("cold", 2.0, "orders")]

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
            "query_id": "hot2",
            "assigned_engine": "aurora_mysql",
            "source_tables": ["orders"],
            "assignment_reason": "highest confidence for aurora_mysql",
            "cache_engine": "elasticache",
            "cache_pattern": "point_lookup",
        },
        {
            "query_id": "cold",
            "assigned_engine": "aurora_mysql",
            "source_tables": ["orders"],
            "assignment_reason": "highest confidence for aurora_mysql",
        },
    ],
}


def _analysis(conf: int) -> dict:
    return {
        "table_recommendations": [
            {"table_id": "users", "confidence_score": conf},
            {"table_id": "orders", "confidence_score": conf},
        ],
        "workload_analysis": {"patterns_detected": [], "anti_patterns_detected": []},
        "cost_estimate": {"monthly_cost_usd": 10},
    }


def _seed(store, assignment: dict, cache_schema: dict | None = None) -> None:
    engines = ["dynamodb", "aurora_mysql", "elasticache"]
    store.write_json(
        f"{DB}/{JOB}/referee-triage/triage.json",
        {"selected_agents": [{"agent_type": e} for e in engines], "signals": []},
    )
    store.write_json(
        f"{DB}/{JOB}/collector/output.json",
        {
            "database_schema": {"tables": [{"table_id": "users"}, {"table_id": "orders"}]},
            "queries": {"query_patterns": QUERIES},
        },
    )
    conf = {"dynamodb": 60, "aurora_mysql": 55, "elasticache": 90}
    for engine in engines:
        store.write_json(f"{DB}/{JOB}/analysis-{engine}/analysis.json", _analysis(conf[engine]))
    store.write_json(f"{DB}/{JOB}/assignment/v2/assignment.json", assignment)
    if cache_schema is not None:
        store.write_json(f"{DB}/{JOB}/schema-elasticache/v2/schema_output.json", cache_schema)


@pytest.fixture
def store(tmp_path):
    return LocalArtifactStore(base_dir=str(tmp_path))


class TestScope:
    def test_cache_scope_is_the_queries_it_fronts(self):
        scope = engine_scope(ASSIGNMENT, "elasticache")
        assert scope.query_ids == {"hot1", "hot2"}
        assert scope.source_tables == {"users", "orders"}
        assert engine_scope(ASSIGNMENT, "aurora_mysql").query_ids == {"hot2", "cold"}

    def test_cache_gets_a_schema_design(self, store):
        _seed(store, ASSIGNMENT)
        engines = engines_with_in_scope_queries(store, DB, JOB, 2)
        assert engines == {"dynamodb", "aurora_mysql", "elasticache"}

    def test_out_of_scope_overlay_does_not_count(self):
        assignment = {
            "query_assignments": [
                {
                    "query_id": "q",
                    "assigned_engine": "dynamodb",
                    "cache_engine": "elasticache",
                    "in_scope": False,
                }
            ]
        }
        assert engine_scope(assignment, "elasticache").query_ids == set()


class TestRouter:
    def test_cache_unsupported_queries_are_not_rerouted(self):
        out = route_unsupported_queries(
            {
                "elasticache": {
                    "unsupported_patterns": [{"source_query_ids": ["hot1"], "reason": "x"}]
                }
            },
            ["dynamodb", "documentdb", "elasticache"],
        )
        assert out.routings == [] and out.terminal_queries == []


class TestSynthesisData:
    def test_cache_engine_survives_without_owning_a_query(self, store):
        _seed(store, ASSIGNMENT)
        data = load_synthesis_data(store, JOB, DB, assignment_version=2)
        assert set(data.engines) == {"dynamodb", "aurora_mysql", "elasticache"}
        assert data.cache_overlay_dropped == []

    def test_cache_without_overlay_is_dropped(self, store):
        assignment = {
            **ASSIGNMENT,
            "query_assignments": [
                {k: v for k, v in qa.items() if not k.startswith("cache_")}
                for qa in ASSIGNMENT["query_assignments"]
            ],
        }
        _seed(store, assignment)
        data = load_synthesis_data(store, JOB, DB, assignment_version=2)
        assert "elasticache" not in data.engines

    def test_safety_net_drops_uncovered_overlay_and_keeps_the_owner(self, store):
        schema = {"access_patterns": [{"pattern_id": "p1", "source_query_ids": ["hot1"]}]}
        _seed(store, ASSIGNMENT, schema)
        data = load_synthesis_data(store, JOB, DB, assignment_version=2)
        hot2 = next(qa for qa in data.assignment["query_assignments"] if qa["query_id"] == "hot2")
        assert hot2["cache_engine"] is None
        assert hot2["assigned_engine"] == "aurora_mysql"
        assert data.cache_overlay_dropped == ["hot2"]
        assert "owner unchanged" in data.cache_overlay_notes[0]
        assert "elasticache" in data.engines

    def test_safety_net_removes_a_cache_that_serves_nothing(self, store):
        _seed(store, ASSIGNMENT, {"access_patterns": []})
        data = load_synthesis_data(store, JOB, DB, assignment_version=2)
        assert "elasticache" not in data.engines
        assert data.cache_overlay_dropped == ["hot1", "hot2"]


class TestSynthesisReport:
    def test_ranking_summary_and_overlay(self, store):
        _seed(store, ASSIGNMENT)
        result = run_synthesis_deterministic(JOB, DB, store, assignment_version=2)
        ranking = result["ranking"]
        cache = ranking[-1]
        assert cache["target"] == "elasticache"  # last, despite the highest weight
        assert cache["role"] == "cache_layer"
        assert cache["workload_percent"] == 0.0
        assert cache["assigned_queries"] == 0
        assert cache["cache_overlay_queries"] == 2
        assert cache["cache_call_share_percent"] == 80.0  # 8 of 10 calls/s
        owners = {r["target"]: r["workload_percent"] for r in ranking[:-1]}
        assert owners == {"aurora_mysql": 66.7, "dynamodb": 33.3}

        overlay = result["cache_overlay"]
        assert overlay["query_count"] == 2 and overlay["owners"] == {
            "aurora_mysql": 1,
            "dynamodb": 1,
        }

        summary = result["summary"]
        assert "Cache layer: elasticache fronts 2 hot reads (80.0% of calls)" in summary
        assert "elasticache handles" not in summary
        assert "elasticache (no queries assigned)" not in summary

        arch = result["architecture"]
        assert arch["architecture_type"] == "HYBRID_WITH_CACHE"
        cache_db = next(d for d in arch["databases"] if d["service"] == "elasticache")
        assert cache_db["rationale"].startswith("Cache layer for 2 hot reads (80.0% of calls)")

        assert result["engine_tables"]["elasticache"] == ["orders", "users"]
        eff = next(
            e for e in result["effective_architecture"]["engines"] if e["engine"] == "elasticache"
        )
        assert eff["role"] == "cache_layer" and "workload_percent" not in eff
        assert "elasticache" not in result["eliminated_engines"]

    def test_fallback_summary_describes_the_cache_layer(self):
        text = build_fallback_summary(
            {
                "engines": [
                    {"engine": "dynamodb", "assigned_queries": 3, "workload_percent": 100.0},
                    {
                        "engine": "elasticache",
                        "role": "cache_layer",
                        "cached_queries": 2,
                        "cached_call_share_percent": 80.0,
                    },
                ]
            }
        )
        assert "caches 2 hot reads (80.0% of calls)" in text
        assert "% of the workload)" in text and "ElastiCache serves" not in text
