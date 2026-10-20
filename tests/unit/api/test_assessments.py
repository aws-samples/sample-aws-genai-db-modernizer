"""Unit tests for assessment routes."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.routes import assessments

client = TestClient(app)

VALID_REQUEST = {
    "source_database_type": "mysql",
    "database_name": "test_db",
    "connection": {
        "host": "localhost",
        "port": 3306,
        "database": "test",
        "secret_name": "app/db",  # pragma: allowlist secret
    },
}

MOCK_EXECUTION = {
    "status": "RUNNING",
    "started_at": "2026-02-23T14:00:00Z",
    "stopped_at": None,
    "input": {"database_name": "test_db", "source_database_type": "mysql"},
}

MOCK_HISTORY = [
    {
        "name": "RunCollector",
        "status": "completed",
        "started_at": "2026-02-23T14:00:00+00:00",
        "completed_at": "2026-02-23T14:15:00+00:00",
    },
    {
        "name": "RunRefereeTriage",
        "status": "in-progress",
        "started_at": "2026-02-23T14:15:00+00:00",
        "completed_at": None,
    },
]


@pytest.fixture(autouse=True)
def mock_services():
    sfn = MagicMock()
    s3 = MagicMock()
    assessments.sfn_service = sfn
    assessments.s3_service = s3
    yield {"sfn": sfn, "s3": s3}
    assessments.sfn_service = None
    assessments.s3_service = None


# === POST /api/v1/assessments ===


class TestCreateAssessment:
    def test_returns_202(self, mock_services):
        mock_services["sfn"].start_execution.return_value = {
            "execution_arn": "arn:test",
            "start_date": "2026-02-23T14:00:00Z",
        }
        assert client.post("/api/v1/assessments", json=VALID_REQUEST).status_code == 202

    def test_returns_job_id_and_status(self, mock_services):
        mock_services["sfn"].start_execution.return_value = {
            "execution_arn": "arn:test",
            "start_date": "2026-02-23T14:00:00Z",
        }
        data = client.post("/api/v1/assessments", json=VALID_REQUEST).json()
        assert "job_id" in data
        assert data["status"] == "PENDING"
        assert "execution_arn" in data

    def test_calls_local_execution_service(self, mock_services):
        mock_services["sfn"].start_execution.return_value = {
            "execution_arn": "arn:test",
            "start_date": "2026-02-23T14:00:00Z",
        }
        client.post("/api/v1/assessments", json=VALID_REQUEST)
        mock_services["sfn"].start_execution.assert_called_once()

    def test_validates_port_range(self, mock_services):
        bad = {**VALID_REQUEST, "connection": {"host": "x", "port": 99999, "database": "t"}}
        assert client.post("/api/v1/assessments", json=bad).status_code == 422

    def test_requires_host(self, mock_services):
        bad = {**VALID_REQUEST, "connection": {"port": 3306, "database": "t"}}
        assert client.post("/api/v1/assessments", json=bad).status_code == 422

    def test_requires_database_name(self, mock_services):
        bad = {"source_database_type": "mysql", "connection": VALID_REQUEST["connection"]}
        assert client.post("/api/v1/assessments", json=bad).status_code == 422

    def test_offline_mode_without_connection(self, mock_services):
        mock_services["sfn"].start_execution.return_value = {
            "execution_arn": "arn:test",
            "start_date": "2026-02-23T14:00:00Z",
        }
        offline_request = {
            "source_database_type": "mysql",
            "database_name": "test_db",
            "collection_mode": "offline",
            "offline_s3_key": "uploads/test.json",
        }
        assert client.post("/api/v1/assessments", json=offline_request).status_code == 202

    def test_offline_mode_requires_s3_key(self, mock_services):
        bad = {
            "source_database_type": "mysql",
            "database_name": "test_db",
            "collection_mode": "offline",
        }
        assert client.post("/api/v1/assessments", json=bad).status_code == 422

    def test_live_mode_requires_connection(self, mock_services):
        bad = {
            "source_database_type": "mysql",
            "database_name": "test_db",
            "collection_mode": "live",
        }
        assert client.post("/api/v1/assessments", json=bad).status_code == 422

    def test_uses_precreated_job_id(self, mock_services):
        mock_services["sfn"].start_execution.return_value = {
            "execution_arn": "arn:test",
            "start_date": "2026-02-23T14:00:00Z",
        }
        request = {
            **VALID_REQUEST,
            "job_id": "pre-created-id-123",
        }
        data = client.post("/api/v1/assessments", json=request).json()
        assert data["job_id"] == "pre-created-id-123"

    def test_cluster_id_automation_path_is_gated_off_by_default(self, mock_services, monkeypatch):
        """The SSM automation-instance path provisions billable AWS resources
        (#175, #342) -- it must never run without an explicit opt-in."""
        monkeypatch.delenv("MODERNIZER_ENABLE_AUTOMATION", raising=False)
        request = {**VALID_REQUEST, "cluster_id": "my-cluster-id"}
        resp = client.post("/api/v1/assessments", json=request)
        assert resp.status_code == 501
        assert "MODERNIZER_ENABLE_AUTOMATION" in resp.json()["detail"]
        mock_services["sfn"].start_execution.assert_not_called()

    def test_cluster_id_automation_path_runs_when_opted_in(self, mock_services, monkeypatch):
        monkeypatch.setenv("MODERNIZER_ENABLE_AUTOMATION", "1")
        mock_services["sfn"].start_execution.return_value = {
            "execution_arn": "arn:test",
            "start_date": "2026-02-23T14:00:00Z",
        }
        request = {**VALID_REQUEST, "cluster_id": "my-cluster-id"}
        with (
            patch("src.tools.aws.automation.discover_cluster") as mock_discover,
            patch("src.tools.aws.automation.ensure_automation_machine") as mock_ensure,
            patch("src.tools.aws.automation.add_ingress_rule") as mock_ingress,
        ):
            mock_discover.return_value = {
                "vpc_id": "vpc-1",
                "subnet_id": "subnet-1",
                "subnet_id_2": "",
                "vpc_cidr": "10.0.0.0/16",
                "route_table_id": "rtb-1",
                "rds_security_group_id": "sg-rds",
                "port": 5432,
                "engine": "postgres",
                "endpoint": "db.example.com",
                "db_instance_identifier": "my-cluster-id",
            }
            mock_ensure.return_value = {"instance_id": "i-abc", "security_group_id": "sg-auto"}
            resp = client.post("/api/v1/assessments", json=request)
        assert resp.status_code == 202
        mock_discover.assert_called_once_with("my-cluster-id")
        mock_ensure.assert_called_once()
        mock_ingress.assert_called_once()
        mock_services["sfn"].start_execution.assert_called_once()


# === POST /api/v1/assessments/prepare ===


class TestPrepareAssessment:
    def test_returns_201(self, mock_services):
        mock_services["s3"].client.generate_presigned_url.return_value = (
            "https://s3.amazonaws.com/presigned"
        )
        mock_services["s3"].bucket = "test-bucket"
        resp = client.post(
            "/api/v1/assessments/prepare",
            json={"database_name": "test_db"},
        )
        assert resp.status_code == 201

    def test_returns_job_id_and_upload_info(self, mock_services):
        mock_services["s3"].client.generate_presigned_url.return_value = (
            "https://s3.amazonaws.com/presigned"
        )
        mock_services["s3"].bucket = "test-bucket"
        data = client.post(
            "/api/v1/assessments/prepare",
            json={"database_name": "test_db"},
        ).json()
        assert "job_id" in data
        assert data["upload_bucket"] == "test-bucket"
        assert "test_db/" in data["upload_prefix"]
        assert data["upload_url"] == "https://s3.amazonaws.com/presigned"
        assert data["upload_key"].endswith("collector-output.json")
        assert data["status"] == "PREPARED"
        assert data["expires_in_seconds"] == 3600

    def test_generates_presigned_url(self, mock_services):
        mock_services["s3"].client.generate_presigned_url.return_value = (
            "https://s3.amazonaws.com/presigned"
        )
        mock_services["s3"].bucket = "test-bucket"
        client.post(
            "/api/v1/assessments/prepare",
            json={"database_name": "test_db"},
        )
        mock_services["s3"].client.generate_presigned_url.assert_called_once()

    def test_custom_filename(self, mock_services):
        mock_services["s3"].client.generate_presigned_url.return_value = (
            "https://s3.amazonaws.com/presigned"
        )
        mock_services["s3"].bucket = "test-bucket"
        data = client.post(
            "/api/v1/assessments/prepare",
            json={"database_name": "test_db"},
        ).json()
        assert data["upload_key"].endswith("collector-output.json")


# === POST /api/v1/assessments/{job_id}/uploads/confirm ===


class TestConfirmUpload:
    def test_confirms_existing_upload(self, mock_services):
        mock_services["s3"].client.head_object.return_value = {"ContentLength": 12345}
        mock_services["s3"].bucket = "test-bucket"
        data = client.post("/api/v1/assessments/job-1/uploads/confirm?database_name=test_db").json()
        assert data["status"] == "confirmed"
        assert data["size_bytes"] == 12345

    def test_404_when_upload_missing(self, mock_services):
        mock_services["s3"].client.head_object.side_effect = Exception("Not found")
        mock_services["s3"].bucket = "test-bucket"
        resp = client.post("/api/v1/assessments/job-1/uploads/confirm?database_name=test_db")
        assert resp.status_code == 404

    def test_413_when_upload_exceeds_size_limit(self, mock_services, monkeypatch):
        """#142: an oversized upload is rejected before the pipeline reads it,
        and the oversized object is deleted rather than left for a retry."""
        monkeypatch.setenv("MODERNIZER_MAX_UPLOAD_BYTES", "100")
        mock_services["s3"].client.head_object.return_value = {"ContentLength": 101}
        mock_services["s3"].bucket = "test-bucket"

        resp = client.post("/api/v1/assessments/job-1/uploads/confirm?database_name=test_db")

        assert resp.status_code == 413
        assert "MODERNIZER_MAX_UPLOAD_BYTES" in resp.json()["detail"]
        mock_services["s3"].client.delete_object.assert_called_once_with(
            Bucket="test-bucket",
            Key="test_db/job-1/uploads/collector-output.json",
        )

    def test_413_even_when_delete_of_oversized_upload_fails(self, mock_services, monkeypatch):
        """#142 round 1 (S1): a delete failure (e.g. permissions, already gone)
        must not hide the 413 -- the caller still needs to know the upload was
        rejected, even if cleanup didn't happen."""
        monkeypatch.setenv("MODERNIZER_MAX_UPLOAD_BYTES", "100")
        mock_services["s3"].client.head_object.return_value = {"ContentLength": 101}
        mock_services["s3"].client.delete_object.side_effect = Exception("boom")
        mock_services["s3"].bucket = "test-bucket"

        resp = client.post("/api/v1/assessments/job-1/uploads/confirm?database_name=test_db")

        assert resp.status_code == 413
        assert "MODERNIZER_MAX_UPLOAD_BYTES" in resp.json()["detail"]


# === GET /api/v1/assessments/{job_id}/uploads ===


class TestListUploads:
    def test_returns_files(self, mock_services):
        from datetime import datetime

        mock_services["s3"].client.list_objects_v2.return_value = {
            "Contents": [
                {
                    "Key": "test_db/job-1/uploads/collector-output.json",
                    "Size": 12345,
                    "LastModified": datetime(2026, 3, 23, 12, 0, 0),
                }
            ]
        }
        mock_services["s3"].bucket = "test-bucket"
        data = client.get("/api/v1/assessments/job-1/uploads?database_name=test_db").json()
        assert len(data["uploads"]) == 1
        assert data["uploads"][0]["filename"] == "collector-output.json"
        assert data["uploads"][0]["size_bytes"] == 12345

    def test_returns_empty_list(self, mock_services):
        mock_services["s3"].client.list_objects_v2.return_value = {}
        mock_services["s3"].bucket = "test-bucket"
        data = client.get("/api/v1/assessments/job-1/uploads?database_name=test_db").json()
        assert data["uploads"] == []


# === DELETE /api/v1/assessments/{job_id}/uploads/{filename} ===


class TestDeleteUpload:
    def test_deletes_file(self, mock_services):
        mock_services["s3"].client.head_object.return_value = {}
        mock_services["s3"].client.delete_object.return_value = {}
        mock_services["s3"].bucket = "test-bucket"
        data = client.delete(
            "/api/v1/assessments/job-1/uploads/collector-output.json?database_name=test_db"
        ).json()
        assert data["status"] == "deleted"
        assert data["filename"] == "collector-output.json"


# === GET /api/v1/assessments ===


class TestListAssessments:
    def test_returns_200_empty(self, mock_services):
        mock_services["sfn"].list_executions.return_value = []
        data = client.get("/api/v1/assessments").json()
        assert data["total_count"] == 0

    def test_returns_list(self, mock_services):
        mock_services["sfn"].list_executions.return_value = [
            {
                "job_id": "j1",
                "status": "SUCCEEDED",
                "started_at": "2026-02-23T10:00:00Z",
                "stopped_at": "2026-02-23T14:00:00Z",
            },
            {
                "job_id": "j2",
                "status": "RUNNING",
                "started_at": "2026-02-23T13:00:00Z",
                "stopped_at": None,
            },
        ]
        data = client.get("/api/v1/assessments").json()
        assert len(data["assessments"]) == 2

    def test_pagination(self, mock_services):
        mock_services["sfn"].list_executions.return_value = [
            {
                "job_id": f"j{i}",
                "status": "SUCCEEDED",
                "started_at": "2026-02-23T10:00:00Z",
                "stopped_at": "2026-02-23T14:00:00Z",
            }
            for i in range(10)
        ]
        data = client.get("/api/v1/assessments?limit=3&offset=2").json()
        assert len(data["assessments"]) == 3
        assert data["offset"] == 2


# === GET /api/v1/assessments/{job_id} ===


class TestGetAssessment:
    def test_returns_200(self, mock_services):
        mock_services["sfn"].describe_execution.return_value = MOCK_EXECUTION
        mock_services["sfn"].get_execution_history.return_value = MOCK_HISTORY[:1]
        mock_services["sfn"]._execution_arn.return_value = "arn:test"
        assert client.get("/api/v1/assessments/job-1").status_code == 200

    def test_returns_progress(self, mock_services):
        mock_services["sfn"].describe_execution.return_value = MOCK_EXECUTION
        mock_services["sfn"].get_execution_history.return_value = MOCK_HISTORY
        mock_services["sfn"]._execution_arn.return_value = "arn:test"
        data = client.get("/api/v1/assessments/job-1").json()
        assert data["progress"]["percent_complete"] == 50
        assert data["progress"]["current_stage"] == "RunRefereeTriage"
        assert data["progress"]["stages"][0]["duration_seconds"] == 900

    def test_404_when_not_found(self, mock_services):
        mock_services["sfn"].describe_execution.return_value = None
        assert client.get("/api/v1/assessments/nope").status_code == 404


# === DELETE /api/v1/assessments/{job_id} ===


class TestCancelAssessment:
    def test_cancels_running(self, mock_services):
        mock_services["sfn"].describe_execution.return_value = {
            **MOCK_EXECUTION,
            "status": "RUNNING",
        }
        mock_services["sfn"].stop_execution.return_value = True
        data = client.delete("/api/v1/assessments/job-1").json()
        assert data["status"] == "CANCELLED"
        mock_services["sfn"].stop_execution.assert_called_once_with("job-1")

    def test_skips_stop_for_completed(self, mock_services):
        """A terminal job can't be cancelled -- DELETE must report what the job
        actually is, not fabricate CANCELLED (#518 round 1 suggestion)."""
        mock_services["sfn"].describe_execution.return_value = {
            **MOCK_EXECUTION,
            "status": "SUCCEEDED",
        }
        data = client.delete("/api/v1/assessments/job-1").json()
        assert data["status"] == "SUCCEEDED"
        mock_services["sfn"].stop_execution.assert_not_called()

    def test_skips_stop_for_already_cancelled(self, mock_services):
        mock_services["sfn"].describe_execution.return_value = {
            **MOCK_EXECUTION,
            "status": "CANCELLED",
        }
        data = client.delete("/api/v1/assessments/job-1").json()
        assert data["status"] == "CANCELLED"
        mock_services["sfn"].stop_execution.assert_not_called()

    def test_skips_stop_for_failed(self, mock_services):
        """A FAILED job is also terminal -- DELETE reports FAILED, not a
        fabricated CANCELLED, and never calls stop_execution (#518 round 2)."""
        mock_services["sfn"].describe_execution.return_value = {
            **MOCK_EXECUTION,
            "status": "FAILED",
        }
        data = client.delete("/api/v1/assessments/job-1").json()
        assert data["status"] == "FAILED"
        mock_services["sfn"].stop_execution.assert_not_called()

    def test_cancels_waiting(self, mock_services):
        """A job waiting on an external LLM response can be cancelled too (#383)
        -- it is blocked, not finished, so it isn't exempt like SUCCEEDED is."""
        mock_services["sfn"].describe_execution.return_value = {
            **MOCK_EXECUTION,
            "status": "WAITING",
        }
        mock_services["sfn"].stop_execution.return_value = True
        data = client.delete("/api/v1/assessments/job-1").json()
        assert data["status"] == "CANCELLED"
        mock_services["sfn"].stop_execution.assert_called_once_with("job-1")

    def test_404_when_not_found(self, mock_services):
        mock_services["sfn"].describe_execution.return_value = None
        assert client.delete("/api/v1/assessments/nope").status_code == 404


# === DELETE /api/v1/assessments/{job_id}/artifacts ===


class TestDeleteAssessmentArtifacts:
    """Local-mode-only cleanup of a finished job's artifacts (#529). A
    separate endpoint from DELETE /{job_id} above, which keeps meaning
    "cancel" -- these tests never touch that route."""

    def _delete(self, job_id="job-1", database_name="test_db"):
        return client.delete(
            f"/api/v1/assessments/{job_id}/artifacts",
            params={"database_name": database_name},
        )

    def test_404_when_job_not_found(self, mock_services):
        mock_services["sfn"].job_status.return_value = None
        resp = self._delete()
        assert resp.status_code == 404
        mock_services["sfn"].delete_job_artifacts.assert_not_called()

    @pytest.mark.parametrize("status", ["RUNNING", "WAITING", "PENDING"])
    def test_409_when_job_is_not_terminal(self, mock_services, status):
        mock_services["sfn"].job_status.return_value = status
        resp = self._delete()
        assert resp.status_code == 409
        assert status in resp.text
        mock_services["sfn"].delete_job_artifacts.assert_not_called()

    @pytest.mark.parametrize("status", ["SUCCEEDED", "CANCELLED", "FAILED"])
    def test_deletes_and_returns_removed_paths_for_a_terminal_job(self, mock_services, status):
        mock_services["sfn"].job_status.return_value = status
        mock_services["sfn"].delete_job_artifacts.return_value = [
            "test_db/job-1/collector/output.json",
            ".meta/job-1.json",
        ]
        resp = self._delete()
        assert resp.status_code == 200
        data = resp.json()
        assert data["job_id"] == "job-1"
        assert data["database_name"] == "test_db"
        assert sorted(data["removed"]) == sorted(
            ["test_db/job-1/collector/output.json", ".meta/job-1.json"]
        )
        mock_services["sfn"].delete_job_artifacts.assert_called_once_with("test_db", "job-1")

    def test_404_when_delete_finds_nothing_despite_a_terminal_status(self, mock_services):
        """A race between the status check and the delete call (or a status
        marker surviving an already-cleaned-up folder) -- treated the same
        as "never existed", not a 500."""
        mock_services["sfn"].job_status.return_value = "SUCCEEDED"
        mock_services["sfn"].delete_job_artifacts.return_value = None
        resp = self._delete()
        assert resp.status_code == 404

    def test_400_when_s3_is_configured(self, mock_services, monkeypatch):
        monkeypatch.setenv("S3_BUCKET", "some-bucket")
        resp = self._delete()
        assert resp.status_code == 400
        mock_services["sfn"].job_status.assert_not_called()
        mock_services["sfn"].delete_job_artifacts.assert_not_called()

    def test_rejects_missing_database_name(self, mock_services):
        resp = client.delete("/api/v1/assessments/job-1/artifacts")
        assert resp.status_code == 422

    def test_422_when_job_id_starts_with_underscore(self, mock_services):
        """job_id_param allows a leading "_" (it's a valid artifact-path
        character), but "_status.json"/".meta"-style names are internal
        markers, never a real job -- this endpoint rejects it on its own
        (#529 round-1 review, S2)."""
        resp = self._delete(job_id="_status")
        assert resp.status_code == 422
        mock_services["sfn"].job_status.assert_not_called()
        mock_services["sfn"].delete_job_artifacts.assert_not_called()

    def test_400_when_job_status_raises_value_error(self, mock_services):
        """A path-containment failure (a symlink planted directly on disk)
        is a fixed 400 (#529 round-1 review, I1) -- the detail never
        echoes the underlying ValueError's own message."""
        mock_services["sfn"].job_status.side_effect = ValueError("path escapes the root: 'x'")
        resp = self._delete()
        assert resp.status_code == 400
        assert "escapes the root" not in resp.text
        mock_services["sfn"].delete_job_artifacts.assert_not_called()

    def test_400_when_delete_job_artifacts_raises_value_error(self, mock_services):
        mock_services["sfn"].job_status.return_value = "SUCCEEDED"
        mock_services["sfn"].delete_job_artifacts.side_effect = ValueError(
            "path escapes the root: 'x'"
        )
        resp = self._delete()
        assert resp.status_code == 400
        assert "escapes the root" not in resp.text

    def test_409_when_delete_job_artifacts_raises_job_recently_modified(self, mock_services):
        from src.api.services.local_execution import JobRecentlyModifiedError

        mock_services["sfn"].job_status.return_value = "SUCCEEDED"
        mock_services["sfn"].delete_job_artifacts.side_effect = JobRecentlyModifiedError(
            "job 'job-1' has files modified in the last 60s; try again shortly"
        )
        resp = self._delete()
        assert resp.status_code == 409
        assert "try again shortly" in resp.text

    def test_500_with_partial_removed_list_on_artifact_deletion_error(self, mock_services, caplog):
        from src.api.services.local_execution import ArtifactDeletionError

        mock_services["sfn"].job_status.return_value = "SUCCEEDED"
        mock_services["sfn"].delete_job_artifacts.side_effect = ArtifactDeletionError(
            ["test_db/job-1/collector/output.json"], OSError("boom")
        )
        import logging

        with caplog.at_level(logging.ERROR):
            resp = self._delete()
        assert resp.status_code == 500
        data = resp.json()
        assert data["detail"]["message"] == "Could not remove all artifacts"
        assert data["detail"]["removed"] == ["test_db/job-1/collector/output.json"]
        assert any("partial failure" in record.message for record in caplog.records)


class TestDeleteAssessmentArtifactsRealStore:
    """At least one test against a real, filesystem-backed store (#529
    round-1 review, I4) -- every other test in this file mocks
    LocalExecutionService entirely, which would never catch a mismatch
    between the mock's behavior and the real service's."""

    def test_deletes_a_real_job_folder_end_to_end(self, tmp_path, monkeypatch):
        import src.api.services.local_execution as local_execution_module
        from src.api.services.local_execution import LocalExecutionService
        from src.storage.local_store import LocalArtifactStore

        # The staleness guard (I2) would otherwise refuse a file written
        # moments ago by this very test.
        monkeypatch.setattr(local_execution_module, "_STALE_WINDOW_SECONDS", -1)

        store = LocalArtifactStore(base_dir=str(tmp_path))
        store.write_json(
            "test_db/job-1/collector/output.json",
            {"metadata": {"source_database": {"engine": "mysql", "database_name": "test_db"}}},
        )
        store.write_json("test_db/job-1/referee-synthesis/report.json", {"done": True})

        real_sfn = LocalExecutionService(store)
        original_sfn = assessments.sfn_service
        assessments.sfn_service = real_sfn
        try:
            resp = client.delete(
                "/api/v1/assessments/job-1/artifacts",
                params={"database_name": "test_db"},
            )
        finally:
            assessments.sfn_service = original_sfn

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert sorted(data["removed"]) == sorted(
            [
                "test_db/job-1/collector/output.json",
                "test_db/job-1/referee-synthesis/report.json",
            ]
        )
        assert not (tmp_path / "test_db" / "job-1").exists()


# === GET /api/v1/assessments/{job_id}/agents ===


class TestGetAgentStatuses:
    def test_returns_agents(self, mock_services):
        mock_services["sfn"].describe_execution.return_value = MOCK_EXECUTION
        mock_services["sfn"].get_execution_history.return_value = MOCK_HISTORY
        mock_services["s3"].artifact_size.return_value = 25000000
        data = client.get("/api/v1/assessments/job-1/agents").json()
        assert len(data["agents"]) == 2
        assert data["agents"][0]["duration_seconds"] == 900

    def test_404_when_not_found(self, mock_services):
        mock_services["sfn"].describe_execution.return_value = None
        assert client.get("/api/v1/assessments/nope/agents").status_code == 404


class TestSynthesisStatusSummary:
    """The status line names the engine owning most of the workload (#152)."""

    def test_names_the_main_engine_with_its_routed_confidence(self):
        summary = {
            "ranking": [
                {"target": "opensearch", "confidence_score": 13, "workload_percent": 3.7},
                {
                    "target": "dynamodb",
                    "confidence_score": 50,
                    "routed_confidence": 90,
                    "workload_percent": 91.6,
                },
                {
                    "target": "elasticache",
                    "role": "cache_layer",
                    "confidence_score": 48,
                    "workload_percent": 0.0,
                },
            ]
        }
        assert (
            assessments._summarize_artifact(summary)
            == "Main engine: dynamodb (91.6% of workload, 90% fit)"
        )

    def test_without_workload_shares_keeps_the_top_entry(self):
        summary = {"ranking": [{"target": "dynamodb", "confidence_score": 70}]}
        assert assessments._summarize_artifact(summary) == (
            "Top recommendation: dynamodb (70% fit)"
        )

    def test_artifact_summary_carries_the_routed_fields(self):
        store = MagicMock()
        store.read_artifact.return_value = {
            "ranking": [
                {
                    "target": "dynamodb",
                    "confidence_score": 50,
                    "routed_confidence": 90,
                    "workload_percent": 91.6,
                    "weight": 0.5,
                },
                {"target": "elasticache", "role": "cache_layer", "confidence_score": 48},
            ]
        }
        original = assessments.s3_service
        assessments.s3_service = store
        try:
            out = assessments._extract_artifact_summary("db", "job", "referee-synthesis")
        finally:
            assessments.s3_service = original
        assert out is not None
        assert out["ranking"][0] == {
            "target": "dynamodb",
            "confidence_score": 50,
            "routed_confidence": 90,
            "routed_confidence_evidence": None,
            "workload_percent": 91.6,
            "role": None,
        }
        assert out["ranking"][1]["role"] == "cache_layer"

    def test_artifact_summary_carries_the_routed_confidence_evidence(self):
        """#312 review: a signal-only routed fit must reach the API summary, not
        just the deliverables, so a caller never shows a bare number as solid."""
        store = MagicMock()
        store.read_artifact.return_value = {
            "ranking": [
                {
                    "target": "opensearch",
                    "confidence_score": 2,
                    "routed_confidence": 60,
                    "routed_confidence_evidence": "signal_only",
                    "workload_percent": 0.2,
                },
            ]
        }
        original = assessments.s3_service
        assessments.s3_service = store
        try:
            out = assessments._extract_artifact_summary("db", "job", "referee-synthesis")
        finally:
            assessments.s3_service = original
        assert out is not None
        assert out["ranking"][0]["routed_confidence_evidence"] == "signal_only"
