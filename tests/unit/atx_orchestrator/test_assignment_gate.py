"""ATX assignment-review gate (ADR-028 + HITL editable-table amendment).

The gate is a hard interrupt: schema design must refuse until the customer has
approved the routing (the ASSIGNMENT_REVIEW phase in .meta is COMPLETED).

Two-step gate:
- present_assignment_review shows the engine-level recommendation and marks the
  phase awaiting (it does not approve).
- open_detailed_routing_review raises the editable per-query table (HITL), or
  falls back to publishing the markdown table for chat.
- finalize_assignment_review records approval, applying any edits first (read
  back from the HITL submission, or parsed from the chat-fallback markdown, or
  approve-as-is when there is nothing to apply).
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

    def test_present_offers_choice_without_approving(self, store) -> None:
        with patch("src.atx_orchestrator.tools._make_store", return_value=store):
            out = json.loads(tools.present_assignment_review(JOB, DB))
            assert out["status"] == "awaiting_choice"
            # A recommendation, not the full per-query table (no markers).
            assert "recommendation" in out["summary_markdown"].lower()
            assert "ATX-ASSIGNMENT-REVIEW:BEGIN" not in out["summary_markdown"]
            # Still blocked: present only marks awaiting, it does not approve.
            assert tools._assignment_review_approved(JOB) is False


class TestApproveAsIs:
    def test_continue_approves_without_new_version(self, store) -> None:
        with patch("src.atx_orchestrator.tools._make_store", return_value=store):
            out = json.loads(tools.finalize_assignment_review(JOB, DB, ""))
            assert out["status"] == "approved"
            assert out["changed"] is False
            assert out["assignment_version"] == 1
            assert tools._assignment_review_approved(JOB) is True
        # No v2 written on approve-as-is.
        assert not store.exists(f"{DB}/{JOB}/assignment/v2/assignment.json")
        # Approval is also stamped on the effective artifact (in place, no new version).
        assert (
            store.read_json(f"{DB}/{JOB}/assignment/v1/assignment.json")["status"]
            == "customer_approved"
        )

    def test_gate_opens_after_approval(self, store) -> None:
        with (
            patch("src.atx_orchestrator.tools._make_store", return_value=store),
            patch(
                "src.atx_orchestrator.tools.invoke_and_wait", return_value={"status": "complete"}
            ) as mock_invoke,
            patch("src.atx_orchestrator.tools.mark_step_running"),
            patch("src.atx_orchestrator.tools.mark_step_succeeded"),
        ):
            tools.finalize_assignment_review(JOB, DB, "")
            out = json.loads(tools._run_schema_design_via_a2a("dynamodb", JOB, DB))
        assert out.get("status") != "blocked"
        mock_invoke.assert_called_once()  # dynamodb is routed + implemented -> dispatched


class TestDetailedReviewChatFallback:
    """When HITL is unavailable, open_detailed_routing_review publishes the full
    editable markdown table and finalize accepts the edited markdown back."""

    def test_open_detailed_falls_back_to_chat(self, store) -> None:
        with (
            patch("src.atx_orchestrator.tools._make_store", return_value=store),
            # HITL unavailable -> None -> chat fallback.
            patch("src.atx_orchestrator.runtime.hitl.raise_assignment_table", return_value=None),
        ):
            out = json.loads(tools.open_detailed_routing_review(JOB, DB))
        assert out["status"] == "awaiting_review"
        assert out["transport"] == "chat"
        assert "ATX-ASSIGNMENT-REVIEW:BEGIN" in out["review_markdown"]
        assert tools._assignment_review_approved(JOB) is False

    def test_finalize_applies_edited_markdown(self, store) -> None:
        from src.agents.referee.assignment_review import render_assignment_review

        assignment = Assignment.model_validate(
            store.read_json(f"{DB}/{JOB}/assignment/v1/assignment.json")
        )
        edited = render_assignment_review(assignment).replace(
            "| q2 | t.posts | dynamodb | dynamodb | yes |",
            "| q2 | t.posts | dynamodb | opensearch | yes |",
        )
        with patch("src.atx_orchestrator.tools._make_store", return_value=store):
            out = json.loads(tools.finalize_assignment_review(JOB, DB, edited))
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

    def test_invalid_edited_markdown_does_not_approve(self, store) -> None:
        from src.agents.referee.assignment_review import render_assignment_review

        assignment = Assignment.model_validate(
            store.read_json(f"{DB}/{JOB}/assignment/v1/assignment.json")
        )
        bad = render_assignment_review(assignment).replace(
            "| q1 | t.users | dynamodb | dynamodb | yes |",
            "| q1 | t.users | dynamodb | dynamdb | yes |",  # typo'd engine
        )
        with patch("src.atx_orchestrator.tools._make_store", return_value=store):
            out = json.loads(tools.finalize_assignment_review(JOB, DB, bad))
            assert out["status"] == "invalid_edit"
            assert tools._assignment_review_approved(JOB) is False
        assert not store.exists(f"{DB}/{JOB}/assignment/v2/assignment.json")


class TestDetailedReviewHitl:
    """The HITL transport: open records a pending task; finalize reads the
    customer's submitted edits back and applies them."""

    def test_open_records_pending_and_finalize_reads_back(self, store) -> None:
        with (
            patch("src.atx_orchestrator.tools._make_store", return_value=store),
            patch(
                "src.atx_orchestrator.runtime.hitl.raise_assignment_table",
                return_value="hitl-123",
            ),
        ):
            opened = json.loads(tools.open_detailed_routing_review(JOB, DB))
            assert opened["transport"] == "hitl"
            assert opened["hitl_task_id"] == "hitl-123"
            # Pending pointer persisted; still not approved.
            pending = tools._read_pending_hitl(store, DB, JOB)
            assert pending is not None and pending["hitl_task_id"] == "hitl-123"
            assert tools._assignment_review_approved(JOB) is False

            # Customer submits: q2 -> opensearch. finalize reads it back.
            submitted = [
                {"query_id": "q1", "new_engine": "dynamodb", "in_scope": "yes"},
                {"query_id": "q2", "new_engine": "opensearch", "in_scope": "yes"},
                {"query_id": "q3", "new_engine": "opensearch", "in_scope": "yes"},
            ]
            with patch(
                "src.atx_orchestrator.runtime.hitl.read_assignment_submission",
                return_value=("submitted", submitted),
            ):
                out = json.loads(tools.finalize_assignment_review(JOB, DB))
        assert out["status"] == "approved"
        assert out["changed"] is True
        assert out["applied_overrides"] == 1
        assert out["assignment_version"] == 2
        v2 = store.read_json(f"{DB}/{JOB}/assignment/v2/assignment.json")
        assert {qa["query_id"]: qa for qa in v2["query_assignments"]}["q2"][
            "assigned_engine"
        ] == "opensearch"

    def test_open_passes_step_id_so_task_renders_under_the_plan_step(self, store) -> None:
        """The HITL task must be attached to the assignment_review plan step, or the
        WebApp has no step to render it under and the customer sees nothing to open."""
        from src.atx_orchestrator.runtime import job_plan

        job_plan.register_steps({"assignment_review": "step-xyz"})
        try:
            with (
                patch("src.atx_orchestrator.tools._make_store", return_value=store),
                patch(
                    "src.atx_orchestrator.runtime.hitl.raise_assignment_table",
                    return_value="hitl-123",
                ) as mock_raise,
            ):
                tools.open_detailed_routing_review(JOB, DB)
            assert mock_raise.call_args.kwargs["step_id"] == "step-xyz"
        finally:
            job_plan.clear_step_registry()

    def test_finalize_refuses_when_submission_unreadable(self, store) -> None:
        """A submitted-but-unreadable table must NOT be approved as-is (that would
        silently drop the customer's edits). The gate stays closed."""
        with (
            patch("src.atx_orchestrator.tools._make_store", return_value=store),
            patch(
                "src.atx_orchestrator.runtime.hitl.raise_assignment_table",
                return_value="hitl-123",
            ),
        ):
            tools.open_detailed_routing_review(JOB, DB)
            with patch(
                "src.atx_orchestrator.runtime.hitl.read_assignment_submission",
                return_value=("unreadable", None),
            ):
                out = json.loads(tools.finalize_assignment_review(JOB, DB))
        assert out["status"] == "error"
        assert tools._assignment_review_approved(JOB) is False
        assert not store.exists(f"{DB}/{JOB}/assignment/v2/assignment.json")

    def test_finalize_waits_when_not_yet_submitted(self, store) -> None:
        with (
            patch("src.atx_orchestrator.tools._make_store", return_value=store),
            patch(
                "src.atx_orchestrator.runtime.hitl.raise_assignment_table",
                return_value="hitl-123",
            ),
        ):
            tools.open_detailed_routing_review(JOB, DB)
            with patch(
                "src.atx_orchestrator.runtime.hitl.read_assignment_submission",
                return_value=("awaiting_submission", None),
            ):
                out = json.loads(tools.finalize_assignment_review(JOB, DB))
        assert out["status"] == "awaiting_review"
        assert tools._assignment_review_approved(JOB) is False
