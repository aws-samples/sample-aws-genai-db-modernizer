"""Collection-upload gate: request_collection_upload / finalize_collection_upload.

The gate replaces upload discovery-by-listing. It raises a BLOCKING FileUploadV2
HITL task at job start; the submission hands back the uploaded file's artifactId,
which is recorded as an ``artifact://<id>`` collection input_key for the collector.

These exercise both tools against a real upgraded local store (so the pending /
resolved-key pointers round-trip) with the HITL runtime stubbed.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from src.atx_orchestrator import tools
from src.atx_orchestrator.runtime.store import upgrade_store
from src.storage.local_store import LocalArtifactStore

DB = "discourse"
JOB = "job-upload"


@pytest.fixture
def store(tmp_path):
    return upgrade_store(LocalArtifactStore(base_dir=str(tmp_path)))


# ─────────────────────────────────────────────────────────────────────────────
# request_collection_upload


class TestRequestCollectionUpload:
    def test_raises_blocking_upload_and_records_pending(self, store) -> None:
        with (
            patch("src.atx_orchestrator.tools._make_store", return_value=store),
            patch("src.atx_orchestrator.tools._platform_job_id", side_effect=lambda x: x),
            patch("src.atx_orchestrator.tools.get_step_id", return_value="step-upload"),
            patch(
                "src.atx_orchestrator.runtime.hitl.raise_file_upload", return_value="hitl-1"
            ) as raise_mock,
            patch("src.atx_orchestrator.tools.mark_step_pending_human_input") as mark_pending,
        ):
            result = json.loads(tools.request_collection_upload(job_id=JOB, database_name=DB))

        assert result["status"] == "awaiting_upload"
        assert result["hitl_task_id"] == "hitl-1"
        # The task is attached to the "upload" plan step.
        assert raise_mock.call_args.kwargs["step_id"] == "step-upload"
        mark_pending.assert_called_once()
        # The pending pointer round-trips on the store.
        pending = tools._read_pending_upload(store, DB, JOB)
        assert pending == {"hitl_task_id": "hitl-1"}

    def test_unavailable_outside_runtime(self, store) -> None:
        with (
            patch("src.atx_orchestrator.tools._make_store", return_value=store),
            patch("src.atx_orchestrator.tools._platform_job_id", side_effect=lambda x: x),
            patch("src.atx_orchestrator.tools.get_step_id", return_value=""),
            patch("src.atx_orchestrator.tools.register_steps_from_server", return_value={}),
            patch("src.atx_orchestrator.runtime.hitl.raise_file_upload", return_value=None),
        ):
            result = json.loads(tools.request_collection_upload(job_id=JOB, database_name=DB))
        assert result["status"] == "unavailable"
        # No pending pointer recorded when the HITL could not be raised.
        assert tools._read_pending_upload(store, DB, JOB) is None


# ─────────────────────────────────────────────────────────────────────────────
# finalize_collection_upload


class TestFinalizeCollectionUpload:
    def _seed_pending(self, store, task_id="hitl-1"):
        tools._record_pending_upload(store, DB, JOB, task_id)

    def test_records_input_key_on_submission(self, store) -> None:
        self._seed_pending(store)
        with (
            patch("src.atx_orchestrator.tools._make_store", return_value=store),
            patch("src.atx_orchestrator.tools._platform_job_id", side_effect=lambda x: x),
            patch(
                "src.atx_orchestrator.runtime.hitl.read_file_upload_submission",
                return_value=("submitted", "file-42"),
            ),
            patch("src.atx_orchestrator.tools.mark_step_succeeded") as mark_ok,
        ):
            result = json.loads(tools.finalize_collection_upload(job_id=JOB, database_name=DB))

        assert result["status"] == "recorded"
        mark_ok.assert_called_once()
        # The resolved input_key is an artifact:// key for the uploaded file, and
        # is exactly what run_assessment_core_via_a2a will read.
        assert tools._read_resolved_input_key(store, DB, JOB) == "artifact://file-42"

    def test_awaiting_when_not_yet_submitted(self, store) -> None:
        self._seed_pending(store)
        with (
            patch("src.atx_orchestrator.tools._make_store", return_value=store),
            patch("src.atx_orchestrator.tools._platform_job_id", side_effect=lambda x: x),
            patch(
                "src.atx_orchestrator.runtime.hitl.read_file_upload_submission",
                return_value=("awaiting_submission", None),
            ),
        ):
            result = json.loads(tools.finalize_collection_upload(job_id=JOB, database_name=DB))
        assert result["status"] == "awaiting_upload"
        assert tools._read_resolved_input_key(store, DB, JOB) == ""

    def test_error_when_no_pending_task(self, store) -> None:
        with (
            patch("src.atx_orchestrator.tools._make_store", return_value=store),
            patch("src.atx_orchestrator.tools._platform_job_id", side_effect=lambda x: x),
        ):
            result = json.loads(tools.finalize_collection_upload(job_id=JOB, database_name=DB))
        assert result["status"] == "error"

    def test_error_when_submission_unreadable(self, store) -> None:
        self._seed_pending(store)
        with (
            patch("src.atx_orchestrator.tools._make_store", return_value=store),
            patch("src.atx_orchestrator.tools._platform_job_id", side_effect=lambda x: x),
            patch(
                "src.atx_orchestrator.runtime.hitl.read_file_upload_submission",
                return_value=("unreadable", None),
            ),
        ):
            result = json.loads(tools.finalize_collection_upload(job_id=JOB, database_name=DB))
        # Fail loud: nothing recorded, gate stays open.
        assert result["status"] == "error"
        assert tools._read_resolved_input_key(store, DB, JOB) == ""


# ─────────────────────────────────────────────────────────────────────────────
# end-to-end: gate output feeds the assessment tool


class TestGateFeedsAssessment:
    def test_resolved_key_flows_to_assessment_message(self, store) -> None:
        """finalize records artifact://<id>; run_assessment_core_via_a2a reads it."""
        tools._record_pending_upload(store, DB, JOB, "hitl-1")
        with (
            patch("src.atx_orchestrator.tools._make_store", return_value=store),
            patch("src.atx_orchestrator.tools._platform_job_id", side_effect=lambda x: x),
            patch(
                "src.atx_orchestrator.runtime.hitl.read_file_upload_submission",
                return_value=("submitted", "file-77"),
            ),
            patch("src.atx_orchestrator.tools.mark_step_succeeded"),
        ):
            tools.finalize_collection_upload(job_id=JOB, database_name=DB)

        with (
            patch("src.atx_orchestrator.tools._make_store", return_value=store),
            patch("src.atx_orchestrator.tools._platform_job_id", side_effect=lambda x: x),
            patch("src.atx_orchestrator.tools.invoke_and_wait", return_value={"ok": 1}) as m,
        ):
            tools.run_assessment_core_via_a2a(job_id=JOB, database_name=DB)

        message = json.loads(m.call_args[0][1])
        assert message["input_key"] == "artifact://file-77"
