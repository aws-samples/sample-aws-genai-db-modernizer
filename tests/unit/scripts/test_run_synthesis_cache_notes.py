"""``run_synthesis.py``'s compact status line carries the cache overlay's
safety-net notes (#424, #459): the chat/orchestrator reads only script
stdout, never artifact contents (AGENTS.md's headless rules), so a cache
layer that shrank after schema design has to be explained from the status
line alone.
"""

from __future__ import annotations

import json

from scripts import run_synthesis
from src.storage.local_store import LocalArtifactStore

DB = "shop"
JOB = "job-run-synthesis-cache-notes"


def _query(qid: str, cps: float, table: str) -> dict:
    return {
        "query_id": qid,
        "query_text": f"SELECT * FROM {table} WHERE id = ?",  # nosec B608 -- test fixture, never executed
        "query_type": "SELECT",
        "calls_per_second": cps,
        "rows_returned_avg": 1,
        "tables_accessed": [table],
    }


QUERIES = [_query("hot1", 6.0, "users"), _query("hot2", 2.0, "orders")]

ASSIGNMENT = {
    "job_id": JOB,
    "version": 2,
    "query_assignments": [
        {
            "query_id": "hot1",
            "assigned_engine": "dynamodb",
            "source_tables": ["users"],
            "assignment_reason": "x",
            "cache_engine": "elasticache",
            "cache_pattern": "point_lookup",
        },
        {
            "query_id": "hot2",
            "assigned_engine": "aurora_mysql",
            "source_tables": ["orders"],
            "assignment_reason": "x",
            "cache_engine": "elasticache",
            "cache_pattern": "point_lookup",
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


def _seed(store) -> None:
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
    store.write_json(f"{DB}/{JOB}/assignment/v2/assignment.json", ASSIGNMENT)
    # Only hot1 is covered: the safety net drops hot2 (#296).
    store.write_json(
        f"{DB}/{JOB}/schema-elasticache/v2/schema_output.json",
        {"access_patterns": [{"pattern_id": "p1", "source_query_ids": ["hot1"]}]},
    )


def test_run_standard_prints_the_safety_net_note_in_the_status_line(tmp_path, capsys):
    store = LocalArtifactStore(base_dir=str(tmp_path))
    _seed(store)

    run_synthesis.run_standard(store, JOB, DB, assignment_version=2, llm_mode="none")

    status = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert status["status"] == "complete"
    (note,) = status["cache_safety_net_notes"]
    assert note.startswith("2 hot reads")
    assert "assigned at the assignment gate" in note
    assert "ElastiCache schema design covers 1 of them" in note


def test_run_finalize_prints_the_safety_net_note_in_the_status_line(tmp_path, capsys):
    store = LocalArtifactStore(base_dir=str(tmp_path))
    _seed(store)
    # --finalize needs an external llm_response already written.
    store.write_json(
        f"{DB}/{JOB}/synthesis/v2/llm_response.json",
        {"executive_summary": "deterministic enough for this test"},
    )

    run_synthesis.run_finalize(store, JOB, DB, assignment_version=2)

    status = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert status["status"] == "complete"
    (note,) = status["cache_safety_net_notes"]
    assert "assigned at the assignment gate" in note


def test_no_notes_when_nothing_was_dropped(tmp_path, capsys):
    store = LocalArtifactStore(base_dir=str(tmp_path))
    _seed(store)
    # Both queries covered: nothing for the safety net to drop.
    store.write_json(
        f"{DB}/{JOB}/schema-elasticache/v2/schema_output.json",
        {
            "access_patterns": [
                {"pattern_id": "p1", "source_query_ids": ["hot1"]},
                {"pattern_id": "p2", "source_query_ids": ["hot2"]},
            ]
        },
    )

    run_synthesis.run_standard(store, JOB, DB, assignment_version=2, llm_mode="none")

    status = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert status["cache_safety_net_notes"] == []
