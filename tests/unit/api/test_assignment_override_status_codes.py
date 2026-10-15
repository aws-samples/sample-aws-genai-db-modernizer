"""The PUT /assignments route must map every ``AssignmentOverrideError``
subclass to HTTP 400, not just the ones caught by name (#548 round 2 review
item 3). Before this, ``LosingAuroraEngineOverride`` (#381) fell through to
an unhandled 500 because the route only caught ``UnknownQuery`` and
``ExcludedEngineOverride`` by name.
"""

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
    AuroraEngineChoice,
    QueryAssignment,
)
from src.storage.local_store import LocalArtifactStore

client = TestClient(app)
DB, JOB = "sqlserver-db", "job-1"


@pytest.fixture
def heterogeneous_store(tmp_path):
    """#381: a SQL Server source where the resolver picked aurora_postgresql
    over aurora_mysql -- overriding onto the losing engine is rejected."""
    store = LocalArtifactStore(base_dir=str(tmp_path))
    qa = QueryAssignment(
        query_id="q1",
        assigned_engine="aurora_postgresql",
        confidence=80,
        source_tables=["Sales.Orders"],
        assignment_reason="test",
    )
    assignment = Assignment(
        job_id=JOB,
        version=1,
        status=AssignmentStatus.AUTO_GENERATED,
        source=AssignmentSource.ASSIGNMENT_RESOLUTION,
        timestamp=datetime.now(UTC),
        query_assignments=[qa],
        table_assignments=[],
        co_dependency_groups=[],
        validation_warnings=[],
        aurora_engine_choice=AuroraEngineChoice(
            source_engine="sqlserver",
            engine="aurora_postgresql",
            totals={"aurora_mysql": 60.0, "aurora_postgresql": 65.0},
            margin=5.0,
            deciding_features=[],
            reason="higher total adjusted score wins (60.0 vs 65.0)",
        ),
    )
    store.write_json(
        f"{DB}/{JOB}/assignment/v1/assignment.json", assignment.model_dump(mode="json")
    )
    store.write_json(
        f"{DB}/{JOB}/collector/output.json",
        {
            "queries": {
                "query_patterns": [
                    {
                        "query_id": "q1",
                        "query_text": "SELECT * FROM Sales.Orders",
                        "tables_accessed": ["Sales.Orders"],
                    }
                ]
            },
            "database_schema": {"tables": []},
        },
    )
    for engine in ("aurora_mysql", "aurora_postgresql"):
        store.write_json(f"{DB}/{JOB}/analysis-{engine}/analysis.json", {"workload_analysis": {}})
    assignments.artifact_store = store
    yield store
    assignments.artifact_store = None


class TestLosingAuroraEngineOverrideStatusCode:
    def test_returns_400_not_500(self, heterogeneous_store):
        response = client.put(
            f"/api/v1/assessments/{JOB}/assignments?database_name={DB}",
            json={"overrides": [{"query_id": "q1", "assigned_engine": "aurora_mysql"}]},
        )
        assert response.status_code == 400
        assert response.json()["detail"] == (
            "This assessment chose Aurora PostgreSQL for this SQL Server source; "
            "Aurora MySQL is not a target."
        )

    def test_writes_nothing(self, heterogeneous_store):
        client.put(
            f"/api/v1/assessments/{JOB}/assignments?database_name={DB}",
            json={"overrides": [{"query_id": "q1", "assigned_engine": "aurora_mysql"}]},
        )
        assert not heterogeneous_store.exists(f"{DB}/{JOB}/assignment/v2/assignment.json")


class TestExcludedEngineOverrideStatusCode:
    def test_returns_400_with_the_customer_facing_reason(self, tmp_path):
        store = LocalArtifactStore(base_dir=str(tmp_path))
        qa = QueryAssignment(
            query_id="q1",
            assigned_engine="aurora_postgresql",
            confidence=80,
            source_tables=["t.posts"],
            assignment_reason="test",
        )
        assignment = Assignment(
            job_id=JOB,
            version=1,
            status=AssignmentStatus.AUTO_GENERATED,
            source=AssignmentSource.ASSIGNMENT_RESOLUTION,
            timestamp=datetime.now(UTC),
            query_assignments=[qa],
            table_assignments=[],
            co_dependency_groups=[],
            validation_warnings=[],
        )
        store.write_json(
            f"{DB}/{JOB}/assignment/v1/assignment.json", assignment.model_dump(mode="json")
        )
        store.write_json(
            f"{DB}/{JOB}/collector/output.json",
            {
                "queries": {
                    "query_patterns": [
                        {
                            "query_id": "q1",
                            "query_text": "SELECT id FROM t.posts WHERE title LIKE '%term%'",
                            "tables_accessed": ["t.posts"],
                        }
                    ]
                },
                "database_schema": {"tables": []},
            },
        )
        for engine in ("aurora_postgresql", "dynamodb"):
            store.write_json(
                f"{DB}/{JOB}/analysis-{engine}/analysis.json", {"workload_analysis": {}}
            )
        assignments.artifact_store = store
        try:
            response = client.put(
                f"/api/v1/assessments/{JOB}/assignments?database_name={DB}",
                json={"overrides": [{"query_id": "q1", "assigned_engine": "dynamodb"}]},
            )
        finally:
            assignments.artifact_store = None
        assert response.status_code == 400
        assert response.json()["detail"] == (
            "DynamoDB can't run query q1: it uses a leading-wildcard search (LIKE '%...')."
        )


class TestCapabilityGateOverrideStatusCode:
    """#801: a pick onto an engine that fails the capability gate is rejected
    the same way as a hard exclusion (#548): HTTP 400 with the plain reason in
    ``detail``, which the UI's ``errorFromResponse`` shows as-is."""

    def test_returns_400_with_the_customer_facing_reason(self, tmp_path):
        store = LocalArtifactStore(base_dir=str(tmp_path))
        qa = QueryAssignment(
            query_id="q1",
            assigned_engine="aurora_postgresql",
            confidence=80,
            source_tables=["t.comments"],
            assignment_reason="test",
        )
        assignment = Assignment(
            job_id=JOB,
            version=1,
            status=AssignmentStatus.AUTO_GENERATED,
            source=AssignmentSource.ASSIGNMENT_RESOLUTION,
            timestamp=datetime.now(UTC),
            query_assignments=[qa],
            table_assignments=[],
            co_dependency_groups=[],
            validation_warnings=[],
        )
        store.write_json(
            f"{DB}/{JOB}/assignment/v1/assignment.json", assignment.model_dump(mode="json")
        )
        store.write_json(
            f"{DB}/{JOB}/collector/output.json",
            {
                "queries": {
                    "query_patterns": [
                        {
                            "query_id": "q1",
                            "query_text": (
                                "SELECT post_id, COUNT(*) FROM t.comments GROUP BY post_id"
                            ),
                            "tables_accessed": ["t.comments"],
                        }
                    ]
                },
                "database_schema": {"tables": []},
            },
        )
        for engine in ("aurora_postgresql", "dynamodb"):
            store.write_json(
                f"{DB}/{JOB}/analysis-{engine}/analysis.json", {"workload_analysis": {}}
            )
        assignments.artifact_store = store
        try:
            response = client.put(
                f"/api/v1/assessments/{JOB}/assignments?database_name={DB}",
                json={"overrides": [{"query_id": "q1", "assigned_engine": "dynamodb"}]},
            )
        finally:
            assignments.artifact_store = None
        assert response.status_code == 400
        assert response.json()["detail"] == "DynamoDB can't run query q1: it needs aggregation."
        assert not store.exists(f"{DB}/{JOB}/assignment/v2/assignment.json")
