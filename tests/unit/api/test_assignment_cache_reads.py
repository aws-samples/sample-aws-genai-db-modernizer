"""The assignment gate response names the hottest cache candidates (#462), computed once by
``src.shared.gate_facts.cache_candidate_reads`` for the chat gate and the UI gate alike."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from src.api.main import app
from src.api.routes import assignments
from src.contracts.assignment_models import (
    Assignment,
    AssignmentSource,
    AssignmentStatus,
    QueryAssignment,
)
from src.storage.local_store import LocalArtifactStore

client = TestClient(app)
DB, JOB = "shop", "job-1"


def _qa(qid: str, cached: bool) -> QueryAssignment:
    return QueryAssignment(
        query_id=qid,
        assigned_engine="dynamodb",
        confidence=78,
        source_tables=[f"shop.{qid}"],
        assignment_reason="test",
        cache_engine="elasticache" if cached else None,
        cache_pattern="point_lookup" if cached else None,
    )


def _store(tmp_path, monkeypatch, cached: bool) -> None:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    assignment = Assignment(
        job_id=JOB,
        version=1,
        status=AssignmentStatus.AUTO_GENERATED,
        source=AssignmentSource.ASSIGNMENT_RESOLUTION,
        timestamp=datetime.now(UTC),
        query_assignments=[_qa("hot", cached), _qa("warm", cached), _qa("cold", False)],
        table_assignments=[],
        co_dependency_groups=[],
        validation_warnings=[],
    )
    store.write_json(
        f"{DB}/{JOB}/assignment/v1/assignment.json", assignment.model_dump(mode="json")
    )
    rates = {"hot": 30, "warm": 10, "cold": 60}
    store.write_json(
        f"{DB}/{JOB}/collector/output.json",
        {
            "queries": {
                "query_patterns": [
                    {"query_id": q, "query_text": "SELECT 1", "calls_per_second": r}
                    for q, r in rates.items()
                ]
            },
            "database_schema": {"tables": []},
        },
    )
    monkeypatch.setattr(assignments, "artifact_store", store)


def _url() -> str:
    return f"/api/v1/assessments/{JOB}/assignments?database_name={DB}"


def test_get_lists_the_cache_candidates_hottest_first(tmp_path, monkeypatch) -> None:
    _store(tmp_path, monkeypatch, cached=True)
    reads = client.get(_url()).json()["cache_reads"]
    assert [r["query_id"] for r in reads["reads"]] == ["hot", "warm"]
    assert reads["reads"][0]["call_share_percent"] == 30.0  # 30 of 100 calls/s
    assert reads["more"] == 0


def test_get_has_no_cache_reads_when_nothing_is_cached(tmp_path, monkeypatch) -> None:
    _store(tmp_path, monkeypatch, cached=False)
    assert client.get(_url()).json()["cache_reads"] is None
