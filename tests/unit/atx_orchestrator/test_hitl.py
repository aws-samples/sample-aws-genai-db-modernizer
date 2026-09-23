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

    def test_no_human_artifact_is_awaiting(self) -> None:
        client = _StubClient(
            get_hitl_return={"hitlTask": {"hitlTaskStatus": "IN_PROGRESS", "humanArtifact": {}}}
        )
        with patch.object(hitl, "_resolve_client_and_context", return_value=(client, {})):
            assert hitl.read_assignment_submission("task-1") == ("awaiting_submission", None)

    def test_submitted_but_unparseable_is_unreadable_not_dropped(self) -> None:
        # A submission we cannot parse must surface as "unreadable" (never as an
        # empty "submitted") so the caller does not silently drop the edits.
        client = _StubClient(
            get_hitl_return={
                "hitlTask": {
                    "hitlTaskStatus": "SUBMITTED",
                    "humanArtifact": {"content": {"unexpected": "shape"}},
                }
            }
        )
        with patch.object(hitl, "_resolve_client_and_context", return_value=(client, {})):
            assert hitl.read_assignment_submission("task-1") == ("unreadable", None)


class TestExtractItems:
    ROW = {"query_id": "q1", "new_engine": "dynamodb", "in_scope": "yes"}

    def test_recognized_shapes(self) -> None:
        rows = [self.ROW]
        # bare list, common wrapper keys, nested properties, JSON string
        assert hitl._extract_items(rows) == rows
        assert hitl._extract_items({"items": rows}) == rows
        assert hitl._extract_items({"rows": rows}) == rows
        assert hitl._extract_items({"tableData": rows}) == rows
        assert hitl._extract_items({"properties": {"items": rows}}) == rows
        assert hitl._extract_items({"result": {"data": rows}}) == rows  # arbitrary nesting
        assert hitl._extract_items('{"items": [{"query_id": "q1", "new_engine": "dynamodb"}]}')

    def test_dict_keyed_by_row_id(self) -> None:
        # Some components hand back a map of row-id -> row rather than a list.
        payload = {"q1": self.ROW, "q2": {"id": "q2", "in_scope": "no"}}
        found = hitl._extract_items(payload)
        assert found is not None and len(found) == 2

    def test_id_plus_value_required(self) -> None:
        # A list of dicts that are not rows (no value key) is not matched.
        assert hitl._extract_items([{"query_id": "q1"}]) is None
        assert hitl._extract_items([{"header": "x", "field": "y"}]) is None

    def test_non_row_payloads(self) -> None:
        assert hitl._extract_items(None) is None
        assert hitl._extract_items("not json") is None
        assert hitl._extract_items({"no_items": 1}) is None


class TestRaiseFileUpload:
    def test_returns_none_outside_runtime(self) -> None:
        with patch.object(
            hitl, "_resolve_client_and_context", side_effect=RuntimeError("no runtime")
        ):
            assert hitl.raise_file_upload(title="t", description="d", label="l") is None

    def test_creates_and_starts_blocking_fileuploadv2_task(self) -> None:
        client = _StubClient()
        with (
            patch.object(hitl, "_resolve_client_and_context", return_value=(client, {"ctx": 1})),
            patch.object(hitl, "_upload_hitl_request", return_value="req-9"),
        ):
            task_id = hitl.raise_file_upload(
                title="Upload",
                description="upload your collection",
                label="Collection JSON",
                step_id="step-1",
                tag="collection-upload",
            )
        assert task_id == "task-1"
        kwargs = client.create_hitl_task.call_args.kwargs
        assert kwargs["uxComponentId"] == "FileUploadV2"
        assert kwargs["blockingType"] == "BLOCKING"
        assert kwargs["hitlRequestArtifact"] == {"artifactId": "req-9"}
        assert kwargs["stepId"] == "step-1"
        assert kwargs["tag"] == "collection-upload"
        client.start_hitl_task.assert_called_once()

    def test_returns_none_on_client_error(self) -> None:
        client = _StubClient()
        client.create_hitl_task.side_effect = RuntimeError("boom")
        with (
            patch.object(hitl, "_resolve_client_and_context", return_value=(client, {})),
            patch.object(hitl, "_upload_hitl_request", return_value="req-9"),
        ):
            assert hitl.raise_file_upload(title="t", description="d", label="l") is None


class TestReadFileUploadSubmission:
    def test_unavailable_outside_runtime(self) -> None:
        with patch.object(
            hitl, "_resolve_client_and_context", side_effect=RuntimeError("no runtime")
        ):
            assert hitl.read_file_upload_submission("task-1") == ("unavailable", None)

    def test_awaiting_when_no_human_artifact(self) -> None:
        client = _StubClient(get_hitl_return={"hitlTask": {"hitlTaskStatus": "IN_PROGRESS"}})
        with patch.object(hitl, "_resolve_client_and_context", return_value=(client, {})):
            assert hitl.read_file_upload_submission("task-1") == ("awaiting_submission", None)

    def test_inline_manifest_uploadedartifacts(self) -> None:
        manifest = {"uploadedArtifacts": [{"name": "c.json", "artifactId": "file-1"}]}
        client = _StubClient(
            get_hitl_return={
                "hitlTask": {
                    "hitlTaskStatus": "SUBMITTED",
                    "humanArtifact": {"content": manifest},
                }
            }
        )
        with patch.object(hitl, "_resolve_client_and_context", return_value=(client, {})):
            status, artifact_id = hitl.read_file_upload_submission("task-1")
        assert status == "submitted"
        assert artifact_id == "file-1"

    def test_downloaded_manifest_legacy_uploadedfiles_wrapper(self) -> None:
        manifest = {"uploadedFiles": [{"name": "c.json", "artifactId": "file-2"}]}
        client = _StubClient(
            get_hitl_return={
                "hitlTask": {
                    "hitlTaskStatus": "SUBMITTED",
                    "humanArtifact": {"artifactId": "resp-1"},
                }
            }
        )
        with (
            patch.object(hitl, "_resolve_client_and_context", return_value=(client, {})),
            patch.object(hitl, "_download_artifact_json", return_value=manifest) as dl,
        ):
            status, artifact_id = hitl.read_file_upload_submission("task-1")
        assert status == "submitted"
        assert artifact_id == "file-2"
        dl.assert_called_once()

    def test_bare_list_manifest_tolerated(self) -> None:
        client = _StubClient(
            get_hitl_return={
                "hitlTask": {
                    "hitlTaskStatus": "SUBMITTED",
                    "humanArtifact": {"content": [{"name": "c.json", "artifactId": "file-3"}]},
                }
            }
        )
        with patch.object(hitl, "_resolve_client_and_context", return_value=(client, {})):
            status, artifact_id = hitl.read_file_upload_submission("task-1")
        assert status == "submitted"
        assert artifact_id == "file-3"

    def test_submitted_but_no_artifact_id_is_unreadable(self) -> None:
        # A submission whose manifest has no resolvable artifactId must surface as
        # "unreadable" (never a false "submitted") so the caller fails loudly.
        client = _StubClient(
            get_hitl_return={
                "hitlTask": {
                    "hitlTaskStatus": "SUBMITTED",
                    "humanArtifact": {"content": {"unexpected": "shape"}},
                }
            }
        )
        with patch.object(hitl, "_resolve_client_and_context", return_value=(client, {})):
            assert hitl.read_file_upload_submission("task-1") == ("unreadable", None)


class TestFirstUploadedArtifactId:
    def test_shapes(self) -> None:
        item = {"name": "c.json", "artifactId": "a1", "mimeType": "application/json"}
        assert hitl._first_uploaded_artifact_id([item]) == "a1"
        assert hitl._first_uploaded_artifact_id({"uploadedArtifacts": [item]}) == "a1"
        assert hitl._first_uploaded_artifact_id({"uploadedFiles": [item]}) == "a1"
        assert (
            hitl._first_uploaded_artifact_id({"properties": {"uploadedArtifacts": [item]}}) == "a1"
        )

    def test_picks_first_non_empty(self) -> None:
        items = [{"name": "x"}, {"artifactId": ""}, {"artifactId": "good"}]
        assert hitl._first_uploaded_artifact_id(items) == "good"

    def test_none_shapes(self) -> None:
        assert hitl._first_uploaded_artifact_id(None) is None
        assert hitl._first_uploaded_artifact_id({}) is None
        assert hitl._first_uploaded_artifact_id({"uploadedArtifacts": []}) is None
        assert hitl._first_uploaded_artifact_id([{"name": "no-id"}]) is None
