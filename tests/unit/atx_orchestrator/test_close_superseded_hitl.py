# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Superseded BLOCKING HITL tasks are closed (#1742, ADR-028 Decision E).

The routing-review gate raises a BLOCKING ``TableComponent`` task. When the
customer approves in chat instead, or the gate is reopened and a new table is
raised, the earlier task is superseded and must be closed, or it stays open in
the job's Tasks tab. Closing is best-effort: an already-closed or missing task,
or a client error, is logged and never fails the job.

The Agentic API client is always a stub here; no test calls a real service.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from src.atx_orchestrator import tools
from src.atx_orchestrator.runtime import hitl
from src.atx_orchestrator.runtime.store import upgrade_store
from src.contracts.assignment_models import (
    Assignment,
    AssignmentSource,
    AssignmentStatus,
    QueryAssignment,
)
from src.storage.local_store import LocalArtifactStore

DB = "discourse"
JOB = "job-close-hitl"


class _ClientError(Exception):
    """Shaped like botocore's ClientError: the code is in ``response``."""

    def __init__(self, code: str) -> None:
        super().__init__(f"An error occurred ({code})")
        self.response = {"Error": {"Code": code, "Message": "stub"}}


def _sdk_hitl_statuses() -> set[str]:
    """``HitlTaskStatus`` from the installed SDK's botocore service model."""
    from pathlib import Path

    import agent_builder_sdk

    models = Path(agent_builder_sdk.__file__).parent / "botocore_models"
    (path,) = models.glob("transformagenticservice/*/service-2.json")
    return set(json.loads(path.read_text())["shapes"]["HitlTaskStatus"]["enum"])


def _client(status: str = "AWAITING_HUMAN_INPUT") -> MagicMock:
    client = MagicMock()
    client.get_hitl_task.return_value = {"hitlTask": {"hitlTaskStatus": status}}
    client.close_hitl_task.return_value = {"hitlTaskStatus": "CANCELLED"}
    return client


def _with_client(client: MagicMock):
    return patch.object(hitl, "_resolve_client_and_context", return_value=(client, {"ctx": 1}))


class TestCloseHitlTask:
    def test_closes_an_open_task_as_cancelled(self) -> None:
        client = _client()
        with _with_client(client):
            assert hitl.close_hitl_task("task-1") == "closed"
        kwargs = client.close_hitl_task.call_args.kwargs
        assert kwargs["hitlTaskId"] == "task-1"
        assert kwargs["closureType"] == "CANCELLED"
        assert kwargs["requestContext"] == {"ctx": 1}

    def test_the_idempotency_token_is_stable_per_task(self) -> None:
        # A retried close of the same task sends the same token, so the service
        # can treat it as the same request.
        client = _client()
        with _with_client(client):
            hitl.close_hitl_task("task-1")
            hitl.close_hitl_task("task-1")
            hitl.close_hitl_task("task-2")
        tokens = [c.kwargs["idempotencyToken"] for c in client.close_hitl_task.call_args_list]
        assert tokens[0] == tokens[1]
        assert tokens[0] != tokens[2]

    def test_the_status_groups_cover_the_sdk_model_exactly(self) -> None:
        # Read HitlTaskStatus from the pinned SDK's service model, so a new SDK
        # status fails here instead of being closed (or waited on) by accident.
        groups = [hitl._OPEN_STATUSES, hitl._SUBMITTED_STATUSES, hitl._CLOSED_STATUSES]
        assert set().union(*groups) == _sdk_hitl_statuses()
        assert sum(len(g) for g in groups) == len(set().union(*groups))

    @pytest.mark.parametrize("status", ["CLOSED", "CANCELLED", "CLOSED_PENDING_NEXT_TASK"])
    def test_an_already_closed_task_is_not_closed_again(self, status) -> None:
        client = _client(status)
        with _with_client(client):
            assert hitl.close_hitl_task("task-1") == "already_closed"
        client.close_hitl_task.assert_not_called()

    @pytest.mark.parametrize(
        "status", ["SUBMITTED", "DELIVERED", "WAITING_APPROVAL", "AWAITING_APPROVAL"]
    )
    def test_a_submitted_task_is_never_closed(self, status) -> None:
        client = _client(status)
        with _with_client(client):
            assert hitl.close_hitl_task("task-1") == "submitted"
        client.close_hitl_task.assert_not_called()

    def test_a_missing_task_is_logged_and_skipped(self, caplog) -> None:
        client = _client()
        client.get_hitl_task.side_effect = _ClientError("ResourceNotFoundException")
        with _with_client(client), caplog.at_level(logging.INFO, logger=hitl.__name__):
            assert hitl.close_hitl_task("task-1") == "missing"
        client.close_hitl_task.assert_not_called()
        assert "task-1" in caplog.text

    @pytest.mark.parametrize("code", ["ConflictException", "TerminalResourceException"])
    def test_a_not_open_error_on_close_means_it_was_closed_meanwhile(self, code) -> None:
        client = _client()
        client.get_hitl_task.side_effect = [
            {"hitlTask": {"hitlTaskStatus": "AWAITING_HUMAN_INPUT"}},
            {"hitlTask": {"hitlTaskStatus": "CANCELLED"}},
        ]
        client.close_hitl_task.side_effect = _ClientError(code)
        with _with_client(client):
            assert hitl.close_hitl_task("task-1") == "already_closed"

    @pytest.mark.parametrize("code", ["ConflictException", "TerminalResourceException"])
    def test_a_submit_during_the_close_is_reported(self, code) -> None:
        # The customer submits between the status read and the close.
        client = _client()
        client.get_hitl_task.side_effect = [
            {"hitlTask": {"hitlTaskStatus": "AWAITING_HUMAN_INPUT"}},
            {"hitlTask": {"hitlTaskStatus": "SUBMITTED"}},
        ]
        client.close_hitl_task.side_effect = _ClientError(code)
        with _with_client(client):
            assert hitl.close_hitl_task("task-1") == "submitted"

    def test_a_failed_status_read_still_tries_the_close(self) -> None:
        client = _client()
        client.get_hitl_task.side_effect = _ClientError("ThrottlingException")
        with _with_client(client):
            assert hitl.close_hitl_task("task-1") == "closed"
        client.close_hitl_task.assert_called_once()

    def test_a_client_error_is_logged_not_raised(self, caplog) -> None:
        client = _client()
        client.close_hitl_task.side_effect = _ClientError("AccessDeniedException")
        with _with_client(client), caplog.at_level(logging.WARNING, logger=hitl.__name__):
            assert hitl.close_hitl_task("task-1") == "error"
        assert "task-1" in caplog.text

    def test_unavailable_outside_the_runtime(self) -> None:
        with patch.object(
            hitl, "_resolve_client_and_context", side_effect=RuntimeError("no runtime")
        ):
            assert hitl.close_hitl_task("task-1") == "unavailable"

    def test_an_empty_task_id_is_a_no_op(self) -> None:
        client = _client()
        with _with_client(client):
            assert hitl.close_hitl_task("") == "missing"
        client.get_hitl_task.assert_not_called()
        client.close_hitl_task.assert_not_called()


# ── The gate: which transitions close the superseded task ──────────────────


def _qa(query_id: str, engine: str, tables: list[str]) -> QueryAssignment:
    return QueryAssignment(
        query_id=query_id,
        assigned_engine=engine,
        confidence=80,
        source_tables=tables,
        assignment_reason=f"routed to {engine}",
    )


@pytest.fixture
def store(tmp_path):
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
    with patch("src.atx_orchestrator.tools._make_store", return_value=s):
        yield s


def _edited_markdown(store) -> str:
    from src.agents.referee.assignment_review import render_assignment_review

    assignment = Assignment.model_validate(
        store.read_json(f"{DB}/{JOB}/assignment/v1/assignment.json")
    )
    return render_assignment_review(assignment).replace(
        "| q2 | t.posts | dynamodb | dynamodb | yes |",
        "| q2 | t.posts | dynamodb | opensearch | yes |",
    )


def _open_table(task_id: str) -> dict:
    with patch.object(hitl, "raise_assignment_table", return_value=task_id):
        opened: dict = json.loads(tools.open_detailed_routing_review(JOB, DB))
    return opened


class TestApproveInChatClosesTheOpenTable:
    def test_chat_approval_closes_the_open_task(self, store) -> None:
        assert _open_table("hitl-1")["transport"] == "hitl"
        with patch.object(hitl, "close_hitl_task", return_value="closed") as close:
            out = json.loads(tools.finalize_assignment_review(JOB, DB, _edited_markdown(store)))
        assert out["status"] == "approved"
        close.assert_called_once_with("hitl-1")
        pending = tools._read_pending_hitl(store, DB, JOB)
        assert pending["hitl_task_id"] == "hitl-1"
        assert pending["superseded"] is True

    def test_an_already_closed_task_still_approves(self, store) -> None:
        _open_table("hitl-1")
        client = _client("CLOSED")
        with _with_client(client):
            out = json.loads(tools.finalize_assignment_review(JOB, DB, _edited_markdown(store)))
        assert out["status"] == "approved"
        client.close_hitl_task.assert_not_called()
        assert tools._read_pending_hitl(store, DB, JOB)["superseded"] is True

    def test_a_client_error_is_logged_and_the_job_continues(self, store, caplog) -> None:
        _open_table("hitl-1")
        client = _client()
        client.close_hitl_task.side_effect = _ClientError("InternalServerException")
        with _with_client(client), caplog.at_level(logging.WARNING, logger=hitl.__name__):
            out = json.loads(tools.finalize_assignment_review(JOB, DB, _edited_markdown(store)))
        assert out["status"] == "approved"
        assert tools._assignment_review_approved(JOB) is True
        assert "hitl-1" in caplog.text

    def test_approval_from_the_table_itself_does_not_close_it(self, store) -> None:
        # The submitted task is the source of the approval, not superseded.
        _open_table("hitl-1")
        with (
            patch.object(hitl, "read_assignment_submission", return_value=("submitted_empty", [])),
            patch.object(hitl, "close_hitl_task") as close,
        ):
            out = json.loads(tools.finalize_assignment_review(JOB, DB))
        assert out["status"] == "approved"
        close.assert_not_called()

    def test_no_table_raised_means_nothing_to_close(self, store) -> None:
        with patch.object(hitl, "close_hitl_task") as close:
            out = json.loads(tools.finalize_assignment_review(JOB, DB, _edited_markdown(store)))
        assert out["status"] == "approved"
        close.assert_not_called()

    def test_an_infeasible_chat_edit_leaves_the_table_open(self, store) -> None:
        from src.contracts.feasibility_models import FindingSeverity

        _open_table("hitl-1")
        blocking = MagicMock(severity=FindingSeverity.BLOCKING)
        blocking.model_dump.return_value = {"severity": "blocking"}
        with (
            patch(
                "src.agents.referee.feasibility_review.review_assignment_feasibility",
                return_value=[blocking],
            ),
            patch.object(hitl, "close_hitl_task") as close,
        ):
            out = json.loads(tools.finalize_assignment_review(JOB, DB, _edited_markdown(store)))
        assert out["status"] == "infeasible"
        close.assert_not_called()
        assert "superseded" not in tools._read_pending_hitl(store, DB, JOB)

    def test_a_superseded_task_is_not_waited_on_after_reopen(self, store) -> None:
        # Round 1: table raised, approved in chat (the task is closed, never
        # submitted). Round 2: reopen and continue without a new table. The
        # gate must approve, not wait on the closed round-1 task.
        _open_table("hitl-1")
        with patch.object(hitl, "close_hitl_task", return_value="closed"):
            tools.finalize_assignment_review(JOB, DB, _edited_markdown(store))
        tools.reopen_assignment_review(JOB, DB)
        with (
            patch.object(hitl, "read_assignment_submission") as read,
            patch.object(hitl, "close_hitl_task") as close,
        ):
            out = json.loads(tools.finalize_assignment_review(JOB, DB))
        assert out["status"] == "approved"
        read.assert_not_called()
        close.assert_not_called()


class TestReraiseClosesTheOldTable:
    def test_raising_a_new_table_closes_the_open_one_first(self, store) -> None:
        _open_table("hitl-1")
        client = _client()
        order = MagicMock()
        order.attach_mock(client.close_hitl_task, "close")
        order.raise_.return_value = "hitl-2"
        with _with_client(client), patch.object(hitl, "raise_assignment_table", order.raise_):
            out = json.loads(tools.open_detailed_routing_review(JOB, DB))
        assert out["hitl_task_id"] == "hitl-2"
        assert "superseded_submission_ignored" not in out
        assert [c[0] for c in order.mock_calls if c[0] in ("close", "raise_")] == [
            "close",
            "raise_",
        ]
        assert client.close_hitl_task.call_args.kwargs["hitlTaskId"] == "hitl-1"
        # The pointer now names the new, open task.
        assert tools._read_pending_hitl(store, DB, JOB) == {
            "hitl_task_id": "hitl-2",
            "assignment_version": 1,
            "lifecycle": 1,
        }

    def test_a_table_that_approved_by_its_own_submission_is_never_closed(self, store) -> None:
        # Round 1 approves through the table's own submission, read by the real
        # status and pointer logic. Reopening and raising a new table must not
        # select that task, whatever status the platform later moves it to.
        _open_table("hitl-1")
        client = _client()
        client.get_hitl_task.return_value = {
            "hitlTask": {"hitlTaskStatus": "SUBMITTED", "humanArtifact": {"content": "{}"}}
        }
        with _with_client(client):
            out = json.loads(tools.finalize_assignment_review(JOB, DB))
            assert out["status"] == "approved"
            assert tools._read_pending_hitl(store, DB, JOB)["answered"] == "approved"
            client.get_hitl_task.return_value = {
                "hitlTask": {"hitlTaskStatus": "AWAITING_HUMAN_INPUT"}
            }
            tools.reopen_assignment_review(JOB, DB)
            with patch.object(hitl, "raise_assignment_table", return_value="hitl-2"):
                tools.open_detailed_routing_review(JOB, DB)
        client.close_hitl_task.assert_not_called()

    def test_a_close_error_does_not_stop_the_new_table(self, store, caplog) -> None:
        _open_table("hitl-1")
        client = _client()
        client.close_hitl_task.side_effect = _ClientError("AccessDeniedException")
        with (
            _with_client(client),
            patch.object(hitl, "raise_assignment_table", return_value="hitl-2"),
            caplog.at_level(logging.WARNING, logger=hitl.__name__),
        ):
            out = json.loads(tools.open_detailed_routing_review(JOB, DB))
        assert out["transport"] == "hitl"
        assert out["hitl_task_id"] == "hitl-2"
        assert "hitl-1" in caplog.text

    def test_the_first_table_has_nothing_to_close(self, store) -> None:
        with patch.object(hitl, "close_hitl_task") as close:
            _open_table("hitl-1")
        close.assert_not_called()

    def test_a_chat_fallback_after_a_table_closes_the_table(self, store) -> None:
        # The new raise fails and falls back to chat: the old table is still
        # superseded, so a later chat approval does not wait on it.
        _open_table("hitl-1")
        with (
            patch.object(hitl, "close_hitl_task", return_value="closed") as close,
            patch.object(hitl, "raise_assignment_table", return_value=None),
        ):
            out = json.loads(tools.open_detailed_routing_review(JOB, DB))
        assert out["transport"] == "chat"
        close.assert_called_once_with("hitl-1")
        assert tools._read_pending_hitl(store, DB, JOB)["superseded"] is True


class TestSubmissionRaces:
    """A submission the gate no longer waits on is reported, never dropped silently."""

    def _race_client(self, code: str = "TerminalResourceException") -> MagicMock:
        # Open at the status read, submitted by the time the close lands.
        client = _client()
        client.get_hitl_task.side_effect = [
            {"hitlTask": {"hitlTaskStatus": "AWAITING_HUMAN_INPUT"}},
            {"hitlTask": {"hitlTaskStatus": "SUBMITTED"}},
        ]
        client.close_hitl_task.side_effect = _ClientError(code)
        return client

    def test_a_submit_during_the_chat_close_is_reported(self, store) -> None:
        _open_table("hitl-1")
        with _with_client(self._race_client()):
            out = json.loads(tools.finalize_assignment_review(JOB, DB, _edited_markdown(store)))
        assert out["status"] == "approved"
        assert out["superseded_submission_ignored"] == "hitl-1"
        assert "already decided in chat, so its edits were not applied" in out["message"]
        assert tools._read_pending_hitl(store, DB, JOB)["submission_ignored"] is True

    def test_the_later_finalize_for_that_submission_reports_it(self, store) -> None:
        # The platform re-invokes the agent for the submission; finalize says the
        # edits were not applied instead of silently approving as-is again.
        _open_table("hitl-1")
        with _with_client(self._race_client()):
            tools.finalize_assignment_review(JOB, DB, _edited_markdown(store))
        with patch.object(hitl, "read_assignment_submission") as read:
            out = json.loads(tools.finalize_assignment_review(JOB, DB))
        read.assert_not_called()
        assert out["status"] == "approved"
        assert out["changed"] is False
        assert out["superseded_submission_ignored"] == "hitl-1"
        assert "were not applied" in out["message"]
        assert not store.exists(f"{DB}/{JOB}/assignment/v3/assignment.json")

    def test_a_submit_after_a_failed_close_is_reported(self, store) -> None:
        _open_table("hitl-1")
        client = _client()
        client.close_hitl_task.side_effect = _ClientError("ThrottlingException")
        with _with_client(client):
            out = json.loads(tools.finalize_assignment_review(JOB, DB, _edited_markdown(store)))
        assert "superseded_submission_ignored" not in out
        # The customer submits the table after the failed close; the next
        # finalize retries the close, finds the submission and reports it.
        client = _client("SUBMITTED")
        with _with_client(client):
            out = json.loads(tools.finalize_assignment_review(JOB, DB))
        assert out["superseded_submission_ignored"] == "hitl-1"
        assert out["changed"] is False
        client.close_hitl_task.assert_not_called()  # a submitted task is never closed

    def test_a_submit_during_the_reraise_close_is_reported(self, store) -> None:
        _open_table("hitl-1")
        with (
            _with_client(self._race_client("ConflictException")),
            patch.object(hitl, "raise_assignment_table", return_value="hitl-2"),
        ):
            out = json.loads(tools.open_detailed_routing_review(JOB, DB))
        assert out["hitl_task_id"] == "hitl-2"
        assert out["superseded_submission_ignored"] == "hitl-1"
        assert "were not applied" in out["message"]

    def test_a_consumed_submission_is_not_reported_on_reraise(self, store) -> None:
        # An infeasible table edit was read and applied; the customer then opens
        # a new table. That submission was not dropped, so nothing is reported.
        from src.contracts.feasibility_models import FindingSeverity

        _open_table("hitl-1")
        blocking = MagicMock(severity=FindingSeverity.BLOCKING)
        blocking.model_dump.return_value = {"severity": "blocking"}
        rows = [{"query_id": "q2", "new_engine": "opensearch", "in_scope": "yes"}]
        with (
            patch(
                "src.agents.referee.feasibility_review.review_assignment_feasibility",
                return_value=[blocking],
            ),
            patch.object(hitl, "read_assignment_submission", return_value=("submitted", rows)),
        ):
            assert json.loads(tools.finalize_assignment_review(JOB, DB))["status"] == "infeasible"
        with (
            patch.object(hitl, "close_hitl_task") as close,
            patch.object(hitl, "raise_assignment_table", return_value="hitl-2"),
        ):
            out = json.loads(tools.open_detailed_routing_review(JOB, DB))
        close.assert_not_called()
        assert "superseded_submission_ignored" not in out


class TestStalePointers:
    def test_a_closed_task_behind_an_open_pointer_is_not_waited_on(self, store) -> None:
        # The close succeeded but marking the pointer superseded was lost: the
        # status read shows the task closed, so finalize does not wait forever.
        _open_table("hitl-1")
        client = _client("CANCELLED")
        with _with_client(client):
            out = json.loads(tools.finalize_assignment_review(JOB, DB))
        assert out["status"] == "approved"
        assert tools._read_pending_hitl(store, DB, JOB)["superseded"] is True

    def test_a_lost_superseded_write_is_logged(self, store, caplog) -> None:
        _open_table("hitl-1")
        real_write = store.write_json

        def failing_write(key, data, *args, **kwargs):
            if key.endswith("pending_hitl.json"):
                raise OSError("disk full")
            return real_write(key, data, *args, **kwargs)

        with (
            patch.object(hitl, "close_hitl_task", return_value="closed"),
            patch.object(store, "write_json", side_effect=failing_write),
            caplog.at_level(logging.WARNING, logger=tools.__name__),
        ):
            out = json.loads(tools.finalize_assignment_review(JOB, DB, _edited_markdown(store)))
        assert out["status"] == "approved"
        assert "re-reads the task's status" in caplog.text

    def test_reopen_closes_the_previous_rounds_open_table(self, store) -> None:
        # A pointer left open by a job saved before pointers were marked answered:
        # reopen marks it superseded, so "continue" does not re-read the old
        # submission, and nothing is reported as dropped.
        store.write_json(
            f"{DB}/{JOB}/assignment/review/pending_hitl.json",
            {"hitl_task_id": "hitl-old", "assignment_version": 1},
        )
        tools._mark_assignment_review(JOB, tools.PhaseStatus.COMPLETED)
        with _with_client(_client("SUBMITTED")):
            out = json.loads(tools.reopen_assignment_review(JOB, DB))
        assert "superseded_submission_ignored" not in out
        pending = tools._read_pending_hitl(store, DB, JOB)
        assert pending["superseded"] is True
        assert "submission_ignored" not in pending
        with patch.object(hitl, "read_assignment_submission") as read:
            out = json.loads(tools.finalize_assignment_review(JOB, DB))
        read.assert_not_called()
        assert out["status"] == "approved"
        assert "superseded_submission_ignored" not in out


def _submitted(rows: list[dict] | None = None) -> dict:
    """A get_hitl_task response for a submitted table (real read path)."""
    content = json.dumps({"items": rows}) if rows else "{}"
    return {"hitlTask": {"hitlTaskStatus": "SUBMITTED", "humanArtifact": {"content": content}}}


_Q2_TO_OPENSEARCH = [{"query_id": "q2", "new_engine": "opensearch", "in_scope": "yes"}]


def _infeasible():
    from src.contracts.feasibility_models import (
        FeasibilityFinding,
        FindingKind,
        FindingSeverity,
    )

    blocking = FeasibilityFinding(
        kind=FindingKind.READ_WRITE_SPLIT,
        severity=FindingSeverity.BLOCKING,
        table="t.posts",
        message="stub blocking finding",
    )
    return patch(
        "src.agents.referee.feasibility_review.review_assignment_feasibility",
        return_value=[blocking],
    )


def _versions(store) -> list[int]:
    return [v for v in range(1, 10) if store.exists(f"{DB}/{JOB}/assignment/v{v}/assignment.json")]


class TestEveryPointerBelongsToOneRound:
    def test_a_consumed_pointer_does_not_outlive_its_round(self, store) -> None:
        # The re-review's sequence: an infeasible table submission, fixed by a
        # chat edit, then reopen and continue. Round 2 must not apply round 1's
        # table edit again.
        _open_table("hitl-1")
        client = _client()
        client.get_hitl_task.return_value = _submitted(_Q2_TO_OPENSEARCH)
        with _with_client(client):
            with _infeasible():
                out = json.loads(tools.finalize_assignment_review(JOB, DB))
            assert out["status"] == "infeasible"
            assert tools._read_pending_hitl(store, DB, JOB)["answered"] == "consumed"
            out = json.loads(tools.finalize_assignment_review(JOB, DB, _edited_markdown(store)))
            assert out["status"] == "approved"
            before = _versions(store)
            tools.reopen_assignment_review(JOB, DB)
            reads = client.get_hitl_task.call_count
            out = json.loads(tools.finalize_assignment_review(JOB, DB))
        assert out["status"] == "approved"
        assert out["changed"] is False
        assert _versions(store) == before
        assert client.get_hitl_task.call_count == reads  # round 1's task is not read
        client.close_hitl_task.assert_not_called()  # its submission was read: never closed

    def test_a_table_approval_is_not_read_again_after_reopen(self, store) -> None:
        _open_table("hitl-1")
        client = _client()
        client.get_hitl_task.return_value = _submitted(_Q2_TO_OPENSEARCH)
        with _with_client(client):
            assert json.loads(tools.finalize_assignment_review(JOB, DB))["changed"] is True
            before = _versions(store)
            tools.reopen_assignment_review(JOB, DB)
            reads = client.get_hitl_task.call_count
            out = json.loads(tools.finalize_assignment_review(JOB, DB))
        assert out["status"] == "approved"
        assert out["changed"] is False
        assert _versions(store) == before
        assert client.get_hitl_task.call_count == reads
        client.close_hitl_task.assert_not_called()

    def test_a_consumed_pointer_is_retired_by_reopen(self, store) -> None:
        # Infeasible, never fixed in round 1; the agent reopens. Round 2 does not
        # re-read round 1's submission.
        _open_table("hitl-1")
        client = _client()
        client.get_hitl_task.return_value = _submitted(_Q2_TO_OPENSEARCH)
        with _with_client(client):
            with _infeasible():
                tools.finalize_assignment_review(JOB, DB)
            before = _versions(store)
            out = json.loads(tools.reopen_assignment_review(JOB, DB))
            assert "superseded_submission_ignored" not in out
            reads = client.get_hitl_task.call_count
            out = json.loads(tools.finalize_assignment_review(JOB, DB))
        assert out["status"] == "approved"
        assert _versions(store) == before
        assert client.get_hitl_task.call_count == reads
        client.close_hitl_task.assert_not_called()

    def test_reopen_reports_a_submitted_table_nobody_finalized(self, store) -> None:
        # A new job: the table was submitted, and the agent reopens before it
        # finalizes. The edits are reported as not applied, never dropped silently.
        _open_table("hitl-1")
        client = _client()
        client.get_hitl_task.return_value = _submitted(_Q2_TO_OPENSEARCH)
        with _with_client(client):
            out = json.loads(tools.reopen_assignment_review(JOB, DB))
            assert out["superseded_submission_ignored"] == "hitl-1"
            assert "had been submitted but not finalized" in out["message"]
            client.close_hitl_task.assert_not_called()  # a submitted task is never closed
            before = _versions(store)
            out = json.loads(tools.finalize_assignment_review(JOB, DB))
        assert out["status"] == "approved"
        assert _versions(store) == before  # the dropped edits are not applied later either

    @pytest.mark.passes_on_main  # main never reports a dropped submission at all
    def test_a_legacy_pointer_is_never_reported_on_reraise(self, store) -> None:
        # A pointer saved before answered states were recorded: whether its
        # submission was read is unknown, so it is retired without a report.
        store.write_json(
            f"{DB}/{JOB}/assignment/review/pending_hitl.json",
            {"hitl_task_id": "hitl-old", "assignment_version": 1},
        )
        with (
            _with_client(_client("SUBMITTED")),
            patch.object(hitl, "raise_assignment_table", return_value="hitl-2"),
        ):
            out = json.loads(tools.open_detailed_routing_review(JOB, DB))
        assert out["hitl_task_id"] == "hitl-2"
        assert "superseded_submission_ignored" not in out


class TestReportScope:
    def test_an_earlier_rounds_dropped_submission_is_not_reported_again(self, store) -> None:
        # Round 1: the table is submitted during the chat approval's close, so its
        # edits are dropped and reported. After a reopen, round 2's finalize calls
        # must not keep reporting round 1's submission.
        _open_table("hitl-1")
        client = _client()
        client.get_hitl_task.side_effect = [
            {"hitlTask": {"hitlTaskStatus": "AWAITING_HUMAN_INPUT"}},
            {"hitlTask": {"hitlTaskStatus": "SUBMITTED"}},
        ]
        client.close_hitl_task.side_effect = _ClientError("TerminalResourceException")
        with _with_client(client):
            out = json.loads(tools.finalize_assignment_review(JOB, DB, _edited_markdown(store)))
        assert out["superseded_submission_ignored"] == "hitl-1"
        tools.reopen_assignment_review(JOB, DB)
        first = json.loads(tools.finalize_assignment_review(JOB, DB))
        again = json.loads(tools.finalize_assignment_review(JOB, DB))
        assert first["status"] == again["status"] == "approved"
        assert "superseded_submission_ignored" not in first
        assert "superseded_submission_ignored" not in again


class TestASubmissionIsAppliedOnce:
    @pytest.mark.parametrize("accept", [False, True])
    def test_an_infeasible_submission_is_not_applied_again(self, store, accept) -> None:
        # The table edit is applied (v2) but infeasible; the next finalize re-reads
        # the same submission and must diff it against v2, not v1.
        _open_table("hitl-1")
        client = _client()
        client.get_hitl_task.return_value = _submitted(_Q2_TO_OPENSEARCH)
        with _with_client(client):
            with _infeasible():
                out = json.loads(tools.finalize_assignment_review(JOB, DB))
            assert out["status"] == "infeasible"
            assert out["assignment_version"] == 2
            assert tools._read_pending_hitl(store, DB, JOB)["applied_version"] == 2
            if accept:
                with _infeasible():
                    out = json.loads(
                        tools.finalize_assignment_review(JOB, DB, accept_feasibility_risks=True)
                    )
            else:
                out = json.loads(tools.finalize_assignment_review(JOB, DB))
        assert out["status"] == "approved"
        assert out["changed"] is False
        assert out["assignment_version"] == 2
        assert _versions(store) == [1, 2]
