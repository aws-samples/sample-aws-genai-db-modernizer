"""ATX assignment-review gate (ADR-028).

The gate is a hard interrupt: schema design must refuse until the customer has
approved the routing (the ASSIGNMENT_REVIEW phase in .meta is COMPLETED).
present_assignment_review only marks it awaiting; apply_assignment_edits records
approval (applying any edits as a new customer_gate version first).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

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
JOB = "job-gate"


def _qa(query_id, engine, tables):
    return QueryAssignment(
        query_id=query_id,
        assigned_engine=engine,
        confidence=80,
        source_tables=tables,
        assignment_reason=f"routed to {engine}",
    )


@pytest.fixture
def store(tmp_path):
    """Upgraded local store seeded with a v1 assignment + collector + analysis."""
    s = upgrade_store(LocalArtifactStore(base_dir=str(tmp_path)))
    assignment = Assignment(
        job_id=JOB,
        version=1,
        status=AssignmentStatus.AUTO_GENERATED,
        source=AssignmentSource.ASSIGNMENT_RESOLUTION,
        timestamp=datetime.now(UTC),
        query_assignments=[
            _qa("q1", "dynamodb", ["t.users"]),
            _qa("q2", "dynamodb", ["t.posts"]),
            _qa("q3", "opensearch", ["t.docs"]),
        ],
        table_assignments=[],
        co_dependency_groups=[],
        validation_warnings=[],
    )
    s.write_json(f"{DB}/{JOB}/assignment/v1/assignment.json", assignment.model_dump(mode="json"))
    s.write_json(
        f"{DB}/{JOB}/collector/output.json",
        {
            "queries": {
                "query_patterns": [
                    {"query_id": "q1", "tables_accessed": ["t.users"]},
                    {"query_id": "q2", "tables_accessed": ["t.posts"]},
                    {"query_id": "q3", "tables_accessed": ["t.docs"]},
                ]
            },
            "database_schema": {"tables": []},
        },
    )
    for engine in ("dynamodb", "opensearch"):
        s.write_json(f"{DB}/{JOB}/analysis-{engine}/analysis.json", {"workload_analysis": {}})
    return s


class TestGateBlocksSchemaDesign:
    def test_schema_design_blocked_before_approval(self, store) -> None:
        with (
            patch("src.atx_orchestrator.tools._make_store", return_value=store),
            patch("src.atx_orchestrator.tools.invoke_and_wait") as mock_invoke,
        ):
            out = json.loads(tools._run_schema_design_via_a2a("dynamodb", JOB, DB))
        assert out["status"] == "blocked"
        assert out["reason"] == "awaiting_assignment_review_approval"
        mock_invoke.assert_not_called()

    def test_present_does_not_approve(self, store) -> None:
        with patch("src.atx_orchestrator.tools._make_store", return_value=store):
            out = json.loads(tools.present_assignment_review(JOB, DB))
            assert out["status"] == "awaiting_review"
            assert "ATX-ASSIGNMENT-REVIEW:BEGIN" in out["review_markdown"]
            # Still blocked: present only marks awaiting, it does not approve.
            assert tools._assignment_review_approved(JOB) is False


class TestApproveAsIs:
    def test_empty_reply_approves_without_new_version(self, store) -> None:
        with patch("src.atx_orchestrator.tools._make_store", return_value=store):
            out = json.loads(tools.apply_assignment_edits(JOB, DB, ""))
            assert out["status"] == "approved"
            assert out["changed"] is False
            assert out["assignment_version"] == 1
            assert tools._assignment_review_approved(JOB) is True
        # No v2 written on approve-as-is.
        assert not store.exists(f"{DB}/{JOB}/assignment/v2/assignment.json")

    def test_gate_opens_after_approval(self, store) -> None:
        with (
            patch("src.atx_orchestrator.tools._make_store", return_value=store),
            patch(
                "src.atx_orchestrator.tools.invoke_and_wait", return_value={"status": "complete"}
            ) as mock_invoke,
            patch("src.atx_orchestrator.tools.mark_step_running"),
            patch("src.atx_orchestrator.tools.mark_step_succeeded"),
        ):
            tools.apply_assignment_edits(JOB, DB, "")
            out = json.loads(tools._run_schema_design_via_a2a("dynamodb", JOB, DB))
        assert out.get("status") != "blocked"
        mock_invoke.assert_called_once()  # dynamodb is routed + implemented -> dispatched


class TestApplyEdits:
    def _edited(self, store) -> str:
        from src.agents.referee.assignment_review import render_assignment_review

        assignment = Assignment.model_validate(
            store.read_json(f"{DB}/{JOB}/assignment/v1/assignment.json")
        )
        md = render_assignment_review(assignment)
        # Move q2 from dynamodb to opensearch (both analyzed -> valid).
        return md.replace(
            "| q2 | t.posts | dynamodb | dynamodb | yes |",
            "| q2 | t.posts | dynamodb | opensearch | yes |",
        )

    def test_edit_creates_new_version_and_approves(self, store) -> None:
        edited = self._edited(store)
        assert "| q2 | t.posts | dynamodb | opensearch | yes |" in edited
        with patch("src.atx_orchestrator.tools._make_store", return_value=store):
            out = json.loads(tools.apply_assignment_edits(JOB, DB, edited))
            assert out["status"] == "approved"
            assert out["changed"] is True
            assert out["applied_overrides"] == 1
            assert out["assignment_version"] == 2
            assert tools._assignment_review_approved(JOB) is True
        v2 = store.read_json(f"{DB}/{JOB}/assignment/v2/assignment.json")
        assert v2["source"] == "customer_gate"
        assert v2["previous_version"] == 1
        by_id = {qa["query_id"]: qa for qa in v2["query_assignments"]}
        assert by_id["q2"]["assigned_engine"] == "opensearch"

    def test_invalid_edit_does_not_approve(self, store) -> None:
        from src.agents.referee.assignment_review import render_assignment_review

        assignment = Assignment.model_validate(
            store.read_json(f"{DB}/{JOB}/assignment/v1/assignment.json")
        )
        bad = render_assignment_review(assignment).replace(
            "| q1 | t.users | dynamodb | dynamodb | yes |",
            "| q1 | t.users | dynamodb | dynamdb | yes |",  # typo'd engine
        )
        with patch("src.atx_orchestrator.tools._make_store", return_value=store):
            out = json.loads(tools.apply_assignment_edits(JOB, DB, bad))
            assert out["status"] == "invalid_edit"
            assert tools._assignment_review_approved(JOB) is False
        assert not store.exists(f"{DB}/{JOB}/assignment/v2/assignment.json")
