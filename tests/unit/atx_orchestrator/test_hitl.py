"""Runtime HITL transport for the assignment-review gate (ADR-028 amendment).

These exercise the two halves of the transport against a stubbed Agentic API
client: raising a BLOCKING editable-table task, and reading the customer's
submission back. Everything degrades to a safe "unavailable" outside the ATX
runtime (no SDK / no client), which the gate uses to fall back to chat.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.atx_orchestrator.runtime import hitl


class _StubClient:
    def __init__(self, get_hitl_return=None):
        self.create_hitl_task = MagicMock(return_value={"hitlTaskId": "task-1"})
        self.start_hitl_task = MagicMock(return_value={"hitlTaskStatus": "IN_PROGRESS"})
        self.get_hitl_task = MagicMock(return_value=get_hitl_return or {})


class TestRaiseAssignmentTable:
    def test_returns_none_outside_runtime(self) -> None:
        # _resolve_client_and_context raises when the SDK/env is absent.
        with patch.object(
            hitl, "_resolve_client_and_context", side_effect=RuntimeError("no runtime")
        ):
            assert (
                hitl.raise_assignment_table(
                    column_definitions=[], items=[], header="h", title="t", description="d"
                )
                is None
            )

    def test_creates_and_starts_blocking_table_task(self) -> None:
        client = _StubClient()
        with (
            patch.object(hitl, "_resolve_client_and_context", return_value=(client, {"ctx": 1})),
            patch.object(hitl, "_upload_hitl_request", return_value="artifact-9"),
        ):
            task_id = hitl.raise_assignment_table(
                column_definitions=[{"field": "new_engine"}],
                items=[{"id": "q1"}],
                header="Routing",
                title="Review",
                description="edit",
                tag="assignment-review-v1",
            )
        assert task_id == "task-1"
        # BLOCKING TableComponent, request artifact wired, then started.
        kwargs = client.create_hitl_task.call_args.kwargs
        assert kwargs["uxComponentId"] == "TableComponent"
        assert kwargs["blockingType"] == "BLOCKING"
        assert kwargs["hitlRequestArtifact"] == {"artifactId": "artifact-9"}
        assert kwargs["tag"] == "assignment-review-v1"
        client.start_hitl_task.assert_called_once()

    def test_returns_none_on_client_error(self) -> None:
        client = _StubClient()
        client.create_hitl_task.side_effect = RuntimeError("boom")
        with (
            patch.object(hitl, "_resolve_client_and_context", return_value=(client, {})),
            patch.object(hitl, "_upload_hitl_request", return_value="artifact-9"),
        ):
            assert (
                hitl.raise_assignment_table(
                    column_definitions=[], items=[], header="h", title="t", description="d"
                )
                is None
            )


class TestReadAssignmentSubmission:
    def test_unavailable_outside_runtime(self) -> None:
        with patch.object(
            hitl, "_resolve_client_and_context", side_effect=RuntimeError("no runtime")
        ):
            assert hitl.read_assignment_submission("task-1") == ("unavailable", None)

    def test_awaiting_when_no_human_artifact(self) -> None:
        client = _StubClient(get_hitl_return={"hitlTask": {"hitlTaskStatus": "IN_PROGRESS"}})
        with patch.object(hitl, "_resolve_client_and_context", return_value=(client, {})):
            assert hitl.read_assignment_submission("task-1") == ("awaiting_submission", None)

    def test_inline_content_items_returned(self) -> None:
        rows = [{"query_id": "q1", "new_engine": "dynamodb", "in_scope": "yes"}]
        client = _StubClient(
            get_hitl_return={
                "hitlTask": {
                    "hitlTaskStatus": "SUBMITTED",
                    "humanArtifact": {"content": {"items": rows}},
                }
            }
        )
        with patch.object(hitl, "_resolve_client_and_context", return_value=(client, {})):
            status, items = hitl.read_assignment_submission("task-1")
        assert status == "submitted"
        assert items == rows

    def test_downloaded_artifact_items_returned(self) -> None:
        rows = [{"id": "q2", "new_engine": "opensearch", "in_scope": "no"}]
        client = _StubClient(
            get_hitl_return={
                "hitlTask": {
                    "hitlTaskStatus": "SUBMITTED",
                    "humanArtifact": {"artifactId": "art-77"},
                }
            }
        )
        with (
            patch.object(hitl, "_resolve_client_and_context", return_value=(client, {})),
            patch.object(hitl, "_download_artifact_json", return_value={"items": rows}) as dl,
        ):
            status, items = hitl.read_assignment_submission("task-1")
        assert status == "submitted"
        assert items == rows
        dl.assert_called_once()

    def test_flat_shape_tolerated(self) -> None:
        rows = [{"query_id": "q1", "new_engine": "dynamodb", "in_scope": "yes"}]
        client = _StubClient(
            get_hitl_return={"humanArtifact": {"content": rows}, "hitlTaskStatus": "SUBMITTED"}
        )
        with patch.object(hitl, "_resolve_client_and_context", return_value=(client, {})):
            status, items = hitl.read_assignment_submission("task-1")
        assert status == "submitted"
        assert items == rows

    def test_human_artifact_without_items_is_unavailable(self) -> None:
        client = _StubClient(
            get_hitl_return={"hitlTask": {"hitlTaskStatus": "SUBMITTED", "humanArtifact": {}}}
        )
        with patch.object(hitl, "_resolve_client_and_context", return_value=(client, {})):
            assert hitl.read_assignment_submission("task-1") == ("awaiting_submission", None)


class TestExtractItems:
    def test_various_shapes(self) -> None:
        rows = [{"query_id": "q1"}]
        assert hitl._extract_items(rows) == rows
        assert hitl._extract_items({"items": rows}) == rows
        assert hitl._extract_items({"properties": {"items": rows}}) == rows
        assert hitl._extract_items('{"items": [{"query_id": "q1"}]}') == rows
        assert hitl._extract_items(None) is None
        assert hitl._extract_items("not json") is None
        assert hitl._extract_items({"no_items": 1}) is None
