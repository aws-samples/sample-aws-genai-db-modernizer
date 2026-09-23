"""Collection-upload gate: declare_plan_and_request_upload / finalize_collection_upload.

The gate replaces upload discovery-by-listing. The upload HITL is raised
DETERMINISTICALLY at job start (declare_plan_and_request_upload, called from the
orchestrator server's _finalize_agent_setup hook), not by the LLM. The submission
hands back the uploaded file's artifactId, which finalize_collection_upload records
as an ``artifact://<id>`` collection input_key for the collector.

These exercise the job-start raise, the finalize half, and the structural refusal
in run_assessment_core_via_a2a, against a real upgraded local store (so the
job-scoped pending / db-scoped resolved-key pointers round-trip) with the HITL
runtime stubbed.
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
# declare_plan_and_request_upload  (deterministic job-start raise)


class TestDeclarePlanAndRequestUpload:
    def test_raises_blocking_upload_and_records_job_scoped_pending(self, store) -> None:
        with (
            patch("src.atx_orchestrator.tools._make_store", return_value=store),
            patch("src.atx_orchestrator.tools._platform_job_id", side_effect=lambda x: x or JOB),
            patch("src.atx_orchestrator.tools.declare_pipeline_plan"),
            patch("src.atx_orchestrator.tools.get_step_id", return_value="step-upload"),
            patch(
                "src.atx_orchestrator.runtime.hitl.raise_file_upload", return_value="hitl-1"
            ) as raise_mock,
            patch("src.atx_orchestrator.tools.mark_step_pending_human_input") as mark_pending,
        ):
            task_id = tools.declare_plan_and_request_upload(JOB)

        assert task_id == "hitl-1"
        # The task is attached to the "upload" plan step.
        assert raise_mock.call_args.kwargs["step_id"] == "step-upload"
        mark_pending.assert_called_once()
        # The pending pointer is JOB-SCOPED (no db name known at job start).
        assert tools._read_pending_upload(store, JOB) == {"hitl_task_id": "hitl-1"}

    def test_none_outside_runtime_no_pending_recorded(self, store) -> None:
        with (
            patch("src.atx_orchestrator.tools._make_store", return_value=store),
            patch("src.atx_orchestrator.tools._platform_job_id", side_effect=lambda x: x or JOB),
            patch("src.atx_orchestrator.tools.declare_pipeline_plan"),
            patch("src.atx_orchestrator.tools.get_step_id", return_value=""),
            patch("src.atx_orchestrator.tools.register_steps_from_server", return_value={}),
            patch("src.atx_orchestrator.runtime.hitl.raise_file_upload", return_value=None),
        ):
            task_id = tools.declare_plan_and_request_upload(JOB)
        assert task_id is None
        assert tools._read_pending_upload(store, JOB) is None


# ─────────────────────────────────────────────────────────────────────────────
# finalize_collection_upload  (LLM resume turn, once db name is known)


class TestFinalizeCollectionUpload:
    def _seed_pending(self, store, task_id="hitl-1"):
        tools._record_pending_upload(store, JOB, task_id)

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
        # Job-scoped pending -> db-scoped resolved input_key (the bridge).
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
# run_assessment_core_via_a2a: structural gate + end-to-end feed


class TestAssessmentGate:
    def test_blocks_when_pending_upload_but_no_resolved_key(self, store) -> None:
        """A raised-but-not-finalized upload must BLOCK the assessment, not fall
        through to a missing seed key."""
        tools._record_pending_upload(store, JOB, "hitl-1")
        with (
            patch("src.atx_orchestrator.tools._make_store", return_value=store),
            patch("src.atx_orchestrator.tools._platform_job_id", side_effect=lambda x: x),
            patch("src.atx_orchestrator.tools.invoke_and_wait", return_value={"ok": 1}) as m,
        ):
            result = json.loads(tools.run_assessment_core_via_a2a(job_id=JOB, database_name=DB))
        assert result["status"] == "blocked"
        assert result["reason"] == "awaiting_collection_upload"
        # The subagent was never dispatched.
        m.assert_not_called()

    def test_seed_fallback_when_no_pending_upload(self, store) -> None:
        """With no pending upload at all (dev/reference), the assessment proceeds
        with an empty input_key (seed-key fallback)."""
        with (
            patch("src.atx_orchestrator.tools._make_store", return_value=store),
            patch("src.atx_orchestrator.tools._platform_job_id", side_effect=lambda x: x),
            patch("src.atx_orchestrator.tools.invoke_and_wait", return_value={"ok": 1}) as m,
        ):
            tools.run_assessment_core_via_a2a(job_id=JOB, database_name=DB)
        assert json.loads(m.call_args[0][1])["input_key"] == ""

    def test_resolved_key_flows_to_assessment_message(self, store) -> None:
        """finalize records artifact://<id>; run_assessment_core_via_a2a reads it."""
        tools._record_pending_upload(store, JOB, "hitl-1")
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


# ─────────────────────────────────────────────────────────────────────────────
# Regression: the gate pointers must round-trip on the JSON-only ATX store.
#
# The ATX artifact store (production backend) is JSON-only — write_text RAISES.
# An earlier version persisted the pending / resolved pointers via write_text,
# which the store rejected, so the pointer was silently lost and the assessment
# fell through to an empty input_key (collector FileNotFoundError). This test
# drives the gate against a real TransformAtxStore (JSON-only) so that class of
# bug fails here instead of only in the deployed runtime.


class TestGateOnAtxStore:
    def _atx_store(self):
        from src.atx_orchestrator.runtime.atx_store import TransformAtxStore
        from tests.unit.atx_orchestrator.test_atx_store import _FakeSdkStore

        return TransformAtxStore(sdk_store=_FakeSdkStore(), agent_instance_id="inst-1")

    def test_pending_and_resolved_pointers_roundtrip(self) -> None:
        store = self._atx_store()
        # record_pending must not raise on the JSON-only store, and must be readable.
        tools._record_pending_upload(store, JOB, "hitl-1")
        assert tools._read_pending_upload(store, JOB) == {"hitl_task_id": "hitl-1"}
        # resolved input_key likewise.
        tools._record_resolved_input_key(store, DB, JOB, "artifact://file-9")
        assert tools._read_resolved_input_key(store, DB, JOB) == "artifact://file-9"

    def test_full_gate_flow_on_atx_store(self) -> None:
        """Job-start raise -> finalize -> assessment reads the key, all on the ATX store."""
        store = self._atx_store()
        with (
            patch("src.atx_orchestrator.tools._make_store", return_value=store),
            patch("src.atx_orchestrator.tools._platform_job_id", side_effect=lambda x: x or JOB),
            patch("src.atx_orchestrator.tools.declare_pipeline_plan"),
            patch("src.atx_orchestrator.tools.get_step_id", return_value="step-upload"),
            patch("src.atx_orchestrator.runtime.hitl.raise_file_upload", return_value="hitl-1"),
            patch("src.atx_orchestrator.tools.mark_step_pending_human_input"),
        ):
            tools.declare_plan_and_request_upload(JOB)
        # The pending pointer survived the JSON-only write.
        assert tools._read_pending_upload(store, JOB) == {"hitl_task_id": "hitl-1"}

        with (
            patch("src.atx_orchestrator.tools._make_store", return_value=store),
            patch("src.atx_orchestrator.tools._platform_job_id", side_effect=lambda x: x),
            patch(
                "src.atx_orchestrator.runtime.hitl.read_file_upload_submission",
                return_value=("submitted", "file-9"),
            ),
            patch("src.atx_orchestrator.tools.mark_step_succeeded"),
        ):
            result = json.loads(tools.finalize_collection_upload(job_id=JOB, database_name=DB))
        assert result["status"] == "recorded"

        with (
            patch("src.atx_orchestrator.tools._make_store", return_value=store),
            patch("src.atx_orchestrator.tools._platform_job_id", side_effect=lambda x: x),
            patch("src.atx_orchestrator.tools.invoke_and_wait", return_value={"ok": 1}) as m,
        ):
            tools.run_assessment_core_via_a2a(job_id=JOB, database_name=DB)
        assert json.loads(m.call_args[0][1])["input_key"] == "artifact://file-9"
