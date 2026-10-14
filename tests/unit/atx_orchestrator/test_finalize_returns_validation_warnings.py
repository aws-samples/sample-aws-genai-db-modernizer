"""``finalize_assignment_review`` surfaces the new version's warnings (ADR-029 Layer B, #704).

The co-dependency-split ``WARNING [HIGH]`` strings the validator computes for an edited
version must reach the orchestrator in the tool result, not stay buried on the artifact.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from src.agents.referee.assignment_review import render_assignment_review
from src.atx_orchestrator import tools
from src.atx_orchestrator.runtime.store import upgrade_store
from src.contracts.assignment_models import (
    Assignment,
    AssignmentSource,
    AssignmentStatus,
    QueryAssignment,
)
from src.storage.local_store import LocalArtifactStore

DB = "discourse"
JOB = "job-warnings"
STALE = "WARNING [HIGH]: a warning that belonged to version 1 only"


def _qa(query_id: str, engine: str, table: str) -> QueryAssignment:
    return QueryAssignment(
        query_id=query_id,
        assigned_engine=engine,
        confidence=80,
        source_tables=[table],
        assignment_reason=f"routed to {engine}",
    )


def _join(query_id: str) -> dict:
    return {
        "query_id": query_id,
        "tables_accessed": ["t.users", "t.docs"],
        "has_joins": True,
        "join_count": 2,
    }


@pytest.fixture
def store(tmp_path):
    """v1 routes two JOIN-sharing queries to different engines and carries a stale warning."""
    s = upgrade_store(LocalArtifactStore(base_dir=str(tmp_path)))
    assignment = Assignment(
        job_id=JOB,
        version=1,
        status=AssignmentStatus.AUTO_GENERATED,
        source=AssignmentSource.ASSIGNMENT_RESOLUTION,
        timestamp=datetime.now(UTC),
        query_assignments=[_qa("q1", "dynamodb", "t.users"), _qa("q3", "opensearch", "t.docs")],
        table_assignments=[],
        co_dependency_groups=[],
        validation_warnings=[STALE],
    )
    s.write_json(f"{DB}/{JOB}/assignment/v1/assignment.json", assignment.model_dump(mode="json"))
    s.write_json(
        f"{DB}/{JOB}/collector/output.json",
        {
            "queries": {"query_patterns": [_join("q1"), _join("q3")]},
            "database_schema": {"tables": []},
        },
    )
    for engine in ("dynamodb", "opensearch"):
        s.write_json(f"{DB}/{JOB}/analysis-{engine}/analysis.json", {"workload_analysis": {}})
    return s


def _swap_both_engines(store: LocalArtifactStore) -> str:
    """An edit that explicitly picks a different engine for each member of the JOIN group."""
    v1 = Assignment.model_validate(store.read_json(f"{DB}/{JOB}/assignment/v1/assignment.json"))
    return (
        render_assignment_review(v1)
        .replace(
            "| q1 | t.users | dynamodb | dynamodb | yes |",
            "| q1 | t.users | dynamodb | opensearch | yes |",
        )
        .replace(
            "| q3 | t.docs | opensearch | opensearch | yes |",
            "| q3 | t.docs | opensearch | dynamodb | yes |",
        )
    )


class TestFinalizeReturnsTheNewVersionsWarnings:
    def test_the_split_warning_the_validator_wrote_is_in_the_tool_result(self, store) -> None:
        edited = _swap_both_engines(store)
        with patch("src.atx_orchestrator.tools._make_store", return_value=store):
            out = json.loads(
                tools.finalize_assignment_review(JOB, DB, edited, accept_feasibility_risks=True)
            )

        assert out["status"] == "approved"
        assert out["assignment_version"] == 2
        v2 = store.read_json(f"{DB}/{JOB}/assignment/v2/assignment.json")
        assert v2["validation_warnings"], "the fixture must make the validator warn"
        assert out["validation_warnings"] == v2["validation_warnings"]
        assert any(
            w.startswith("WARNING [HIGH]: Co-dependent queries ['q1', 'q3']")
            for w in out["validation_warnings"]
        ), out["validation_warnings"]

    def test_the_previous_versions_warnings_are_not_carried_into_the_result(self, store) -> None:
        edited = _swap_both_engines(store)
        with patch("src.atx_orchestrator.tools._make_store", return_value=store):
            out = json.loads(
                tools.finalize_assignment_review(JOB, DB, edited, accept_feasibility_risks=True)
            )

        assert STALE not in out["validation_warnings"]
