# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The assignment review's PUT runs the feasibility review on the edit (ADR-029
Decision C, #2519): a blocking finding the edit introduces is a 422 with the
finding's message among the errors, like every other hard gate error, and nothing is
written; an advisory one comes back in ``feasibility_findings``."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
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
ENGINES = ["aurora_postgresql", "dynamodb"]


def _qa(qid: str, tables: list[str]) -> QueryAssignment:
    return QueryAssignment(
        query_id=qid,
        assigned_engine="aurora_postgresql",
        confidence=80,
        source_tables=tables,
        assignment_reason="test",
    )


@pytest.fixture
def store(tmp_path, monkeypatch):
    """q1-q3 share a JOIN on shop.orders (one co-dependency group); q4 writes
    shop.users and q5 reads it. Everything starts on Aurora PostgreSQL."""
    store = LocalArtifactStore(base_dir=str(tmp_path))
    joined = ["shop.orders", "shop.items"]
    assignment = Assignment(
        job_id=JOB,
        version=1,
        status=AssignmentStatus.AUTO_GENERATED,
        source=AssignmentSource.ASSIGNMENT_RESOLUTION,
        timestamp=datetime.now(UTC),
        query_assignments=[
            *(_qa(q, joined) for q in ("q1", "q2", "q3")),
            _qa("q4", ["shop.users"]),
            _qa("q5", ["shop.users"]),
        ],
        table_assignments=[],
        co_dependency_groups=[],
        validation_warnings=[],
    )
    store.write_json(
        f"{DB}/{JOB}/assignment/v1/assignment.json", assignment.model_dump(mode="json")
    )
    queries = [
        {
            "query_id": q,
            "query_type": "SELECT",
            "tables_accessed": joined,
            "has_joins": True,
            "join_count": 2,
        }
        for q in ("q1", "q2", "q3")
    ] + [
        {
            "query_id": "q4",
            "query_type": "UPDATE",
            "query_text": "UPDATE shop.users SET name = ? WHERE id = ?",
            "tables_accessed": ["shop.users"],
        },
        {
            "query_id": "q5",
            "query_type": "SELECT",
            "query_text": "SELECT * FROM shop.users WHERE id = ?",
            "tables_accessed": ["shop.users"],
        },
    ]
    store.write_json(
        f"{DB}/{JOB}/collector/output.json",
        {"queries": {"query_patterns": queries}, "database_schema": {"tables": []}},
    )
    for engine in ENGINES:
        store.write_json(f"{DB}/{JOB}/analysis-{engine}/analysis.json", {"workload_analysis": {}})
    monkeypatch.setattr(assignments, "artifact_store", store)
    return store


def _put(overrides: list[dict]):
    return client.put(
        f"/api/v1/assessments/{JOB}/assignments?database_name={DB}",
        json={"overrides": overrides},
    )


def test_an_edit_that_splits_a_join_onto_dynamodb_is_a_422_and_writes_nothing(store) -> None:
    resp = _put(
        [
            {"query_id": "q1", "assigned_engine": "dynamodb"},
            {"query_id": "q2", "assigned_engine": "aurora_postgresql"},
        ]
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["message"] == "Assignment validation failed with hard errors"
    (error,) = detail["errors"]
    assert error.startswith("ERROR: Co-dependent queries (q1, q2, q3) share a significant JOIN")
    assert "DynamoDB cannot serve the JOIN" in error
    assert not store.exists(f"{DB}/{JOB}/assignment/v2/assignment.json")


def test_a_feasible_edit_is_saved_with_no_finding(store) -> None:
    # shop.users moves whole: its writes and its reads land on DynamoDB together.
    resp = _put(
        [
            {"query_id": "q4", "assigned_engine": "dynamodb"},
            {"query_id": "q5", "assigned_engine": "dynamodb"},
        ]
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["feasibility_findings"] == []
    engines = {
        qa["query_id"]: qa["assigned_engine"] for qa in body["assignment"]["query_assignments"]
    }
    assert (engines["q4"], engines["q5"]) == ("dynamodb", "dynamodb")


def test_an_advisory_finding_comes_back_with_the_saved_edit(store) -> None:
    resp = _put([{"query_id": "q5", "assigned_engine": "dynamodb"}])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    (finding,) = body["feasibility_findings"]
    assert (finding["kind"], finding["severity"], finding["table"]) == (
        "read_write_split",
        "advisory",
        "shop.users",
    )
    assert finding["query_ids"] == ["q4", "q5"]
    assert finding["recommended_pattern"]
    assert f"WARNING [MEDIUM]: {finding['message']}" in body["assignment"]["validation_warnings"]
    assert store.exists(f"{DB}/{JOB}/assignment/v2/assignment.json")


def test_a_refused_edit_after_a_reroute_names_the_version_the_reroute_wrote(store) -> None:
    # The saved routing writes shop.users on two engines: the PUT first re-routes it
    # to one source of truth (a new version that stands), then refuses the split.
    path = f"{DB}/{JOB}/assignment/v1/assignment.json"
    saved = store.read_json(path)
    saved["query_assignments"].append(
        {**saved["query_assignments"][3], "query_id": "q6", "assigned_engine": "dynamodb"}
    )
    store.write_json(path, saved)
    collector = store.read_json(f"{DB}/{JOB}/collector/output.json")
    collector["queries"]["query_patterns"].append(
        {**collector["queries"]["query_patterns"][3], "query_id": "q6"}
    )
    store.write_json(f"{DB}/{JOB}/collector/output.json", collector)
    resp = _put(
        [
            {"query_id": "q1", "assigned_engine": "dynamodb"},
            {"query_id": "q2", "assigned_engine": "aurora_postgresql"},
        ]
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["message"].endswith("one engine: assignment version 2.)")
    assert "DynamoDB cannot serve the JOIN" in detail["errors"][0]
    assert store.exists(f"{DB}/{JOB}/assignment/v2/assignment.json")
    assert not store.exists(f"{DB}/{JOB}/assignment/v3/assignment.json")
