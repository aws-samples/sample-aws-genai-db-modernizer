"""Unit tests for assessment routes."""

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.routes import assessments

client = TestClient(app)

VALID_REQUEST = {
    "source_database_type": "mysql",
    "database_name": "test_db",
    "connection": {"host": "localhost", "port": 3306, "database": "test"},
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
    cw = MagicMock()
    assessments.sfn_service = sfn
    assessments.s3_service = s3
    assessments.cw_service = cw
    yield {"sfn": sfn, "s3": s3, "cw": cw}
    assessments.sfn_service = None
    assessments.s3_service = None
    assessments.cw_service = None


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

    def test_calls_step_functions(self, mock_services):
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
        mock_services["sfn"].describe_execution.return_value = {
            **MOCK_EXECUTION,
            "status": "SUCCEEDED",
        }
        client.delete("/api/v1/assessments/job-1")
        mock_services["sfn"].stop_execution.assert_not_called()

    def test_404_when_not_found(self, mock_services):
        mock_services["sfn"].describe_execution.return_value = None
        assert client.delete("/api/v1/assessments/nope").status_code == 404


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


# === GET /api/v1/assessments/{job_id}/logs ===


class TestGetLogs:
    def test_returns_logs(self, mock_services):
        mock_services["cw"].get_logs.return_value = {
            "logs": [
                {"timestamp": 1708700595000, "message": "Starting", "log_stream": "collector/abc"}
            ],
            "next_token": None,
        }
        data = client.get("/api/v1/assessments/job-1/logs").json()
        assert len(data["logs"]) == 1
        assert data["logs"][0]["agent"] == "collector"

    def test_filters_by_agent(self, mock_services):
        mock_services["cw"].get_logs.return_value = {"logs": [], "next_token": None}
        client.get("/api/v1/assessments/job-1/logs?agent=collector")
        assert mock_services["cw"].get_logs.call_args[1]["stream_prefix"] == "collector"

    def test_returns_next_token(self, mock_services):
        mock_services["cw"].get_logs.return_value = {
            "logs": [{"timestamp": 1, "message": "x", "log_stream": "a/b"}],
            "next_token": "tok123",
        }
        assert client.get("/api/v1/assessments/job-1/logs").json()["next_token"] == "tok123"
