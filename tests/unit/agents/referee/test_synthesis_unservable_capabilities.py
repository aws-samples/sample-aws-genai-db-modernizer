"""Synthesis passes triage's per-query capabilities into the fallback list (#729).

``query_traces.unservable`` records the capabilities a fallback query needs
(``triage.query_capabilities``), and ``facts.unservable`` turns them into a
redesign hint for the engines that ruled it out. Without them every hint falls
back to the gate's generic sentence, so this runs the real synthesis writer
over a store and checks both.
"""

from __future__ import annotations

import pytest

from src.agents.referee.synthesis_handler import (
    _write_synthesis_report,
    run_synthesis_deterministic,
)
from src.contracts.move_ledger import CandidateScore, Move, MoveEvidence, MovePass, ReasonCode
from src.shared.capability_labels import CAPABILITY_REDESIGN_HINTS
from src.storage.local_store import LocalArtifactStore

DB = "shop"
JOB = "job-fallback"
COUNT_SQL = "SELECT COUNT(*) FROM orders"


def _move(seq: int, qid: str, to: str, code: ReasonCode, candidates: list) -> dict:
    move = Move(
        seq=seq,
        query_id=qid,
        pass_=MovePass.AURORA_FALLBACK if code.value.startswith("fallback") else MovePass.SCORE,
        to_engine=to,
        reason_code=code,
        score_after=None if code.value.startswith("fallback") else 90.0,
        evidence=MoveEvidence(candidates=candidates),
    )
    return move.model_dump(mode="json", by_alias=True)


@pytest.fixture
def store(tmp_path):
    s = LocalArtifactStore(base_dir=str(tmp_path))
    s.write_json(
        f"{DB}/{JOB}/referee-triage/triage.json",
        {
            "selected_agents": [{"agent_type": "dynamodb"}],
            "signals": [],
            "query_capabilities": {"count1": ["aggregation"]},
        },
    )
    queries = [
        {
            "query_id": "kv1",
            "query_text": "SELECT * FROM users WHERE id = ?",
            "query_type": "SELECT",
            "calls_per_second": 5.0,
            "rows_returned_avg": 1,
            "tables_accessed": ["users"],
        },
        {
            "query_id": "count1",
            "query_text": COUNT_SQL,
            "query_type": "SELECT",
            "calls_per_second": 1.0,
            "rows_returned_avg": 1,
            "tables_accessed": ["orders"],
        },
    ]
    s.write_json(
        f"{DB}/{JOB}/collector/output.json",
        {
            "metadata": {"source_database": {"engine": "mysql"}},
            "database_schema": {"tables": [{"table_id": "users"}, {"table_id": "orders"}]},
            "queries": {"query_patterns": queries},
        },
    )
    s.write_json(
        f"{DB}/{JOB}/analysis-dynamodb/analysis.json",
        {
            "table_recommendations": [
                {"table_id": "users", "confidence_score": 90},
                {"table_id": "orders", "confidence_score": 90},
            ],
            "workload_analysis": {"patterns_detected": [], "anti_patterns_detected": []},
            "cost_estimate": {"monthly_cost_usd": 10},
        },
    )
    s.write_json(
        f"{DB}/{JOB}/assignment/v1/assignment.json",
        {
            "job_id": JOB,
            "version": 1,
            "query_assignments": [
                {
                    "query_id": "kv1",
                    "assigned_engine": "dynamodb",
                    "source_tables": ["users"],
                    "assignment_reason": "highest confidence for dynamodb",
                },
                {
                    "query_id": "count1",
                    "assigned_engine": "aurora_mysql",
                    "source_tables": ["orders"],
                    "assignment_reason": "fallback",
                },
            ],
            "table_assignments": [],
            "moves": [
                _move(
                    1,
                    "kv1",
                    "dynamodb",
                    ReasonCode.HIGHEST_SCORE,
                    [CandidateScore(engine="dynamodb", score=90.0)],
                ),
                _move(
                    2,
                    "count1",
                    "aurora_mysql",
                    ReasonCode.FALLBACK_NO_SCORE,
                    [CandidateScore(engine="dynamodb", score=None, gate="capability")],
                ),
            ],
        },
    )
    return s


def test_synthesis_records_triages_capabilities_and_their_redesign(store) -> None:
    result = run_synthesis_deterministic(JOB, DB, store, assignment_version=1)
    _write_synthesis_report(store, result, assignment_version=1)
    report = store.read_json(f"{DB}/{JOB}/synthesis/v1/report.json")

    [row] = report["query_traces"]["unservable"]
    assert row["query_id"] == "count1"
    assert row["required_capabilities"] == ["aggregation"]
    [fact] = report["facts"]["unservable"]["unservable_queries"]
    assert fact["redesign_hints"] == [
        "For DynamoDB: " + CAPABILITY_REDESIGN_HINTS["aggregation"]["dynamodb"]
    ]
