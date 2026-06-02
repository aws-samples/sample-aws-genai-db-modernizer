"""Unit tests for dashboard routes."""

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.routes import dashboard

client = TestClient(app)


@pytest.fixture(autouse=True)
def mock_services():
    sfn = MagicMock()
    dashboard.sfn_service = sfn
    yield {"sfn": sfn}
    dashboard.sfn_service = None


class TestDashboardStats:
    """Tests for GET /api/v1/dashboard/stats."""

    def test_returns_200(self, mock_services):
        mock_services["sfn"].list_executions.return_value = []
        response = client.get("/api/v1/dashboard/stats")
        assert response.status_code == 200

    def test_returns_stats_fields(self, mock_services):
        mock_services["sfn"].list_executions.return_value = []
        response = client.get("/api/v1/dashboard/stats")
        data = response.json()
        assert "total_assessments" in data
        assert "active_jobs" in data
        assert "success_rate_percent" in data
        assert "average_duration_hours" in data

    def test_computes_stats_from_executions(self, mock_services):
        all_execs = [
            {
                "job_id": "j1",
                "status": "SUCCEEDED",
                "started_at": "2026-02-23T10:00:00+00:00",
                "stopped_at": "2026-02-23T14:00:00+00:00",
            },
            {
                "job_id": "j2",
                "status": "SUCCEEDED",
                "started_at": "2026-02-23T11:00:00+00:00",
                "stopped_at": "2026-02-23T13:00:00+00:00",
            },
            {
                "job_id": "j3",
                "status": "RUNNING",
                "started_at": "2026-02-23T13:00:00+00:00",
                "stopped_at": None,
            },
        ]
        succeeded = [all_execs[0], all_execs[1]]
        running = [all_execs[2]]

        def side_effect(status_filter=None, max_results=100):
            if status_filter == "RUNNING":
                return running
            if status_filter == "SUCCEEDED":
                return succeeded
            return all_execs

        mock_services["sfn"].list_executions.side_effect = side_effect
        response = client.get("/api/v1/dashboard/stats")
        data = response.json()
        assert data["total_assessments"] == 3
        assert data["active_jobs"] == 1
        assert data["success_rate_percent"] == 66.7
        assert data["average_duration_hours"] == 3.0

    def test_handles_no_executions(self, mock_services):
        mock_services["sfn"].list_executions.return_value = []
        response = client.get("/api/v1/dashboard/stats")
        data = response.json()
        assert data["total_assessments"] == 0
        assert data["active_jobs"] == 0
        assert data["success_rate_percent"] == 0
        assert data["average_duration_hours"] == 0
