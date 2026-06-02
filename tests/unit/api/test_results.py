"""Unit tests for results routes."""

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.routes import results

client = TestClient(app)


@pytest.fixture(autouse=True)
def mock_services():
    """Inject mock services into the results router."""
    sfn = MagicMock()
    s3 = MagicMock()

    # Default: describe_execution returns valid input with database_name
    sfn.describe_execution.return_value = {
        "status": "SUCCEEDED",
        "started_at": "2026-02-23T14:00:00Z",
        "stopped_at": "2026-02-23T18:00:00Z",
        "input": {"database_name": "test_db", "source_database_type": "mysql"},
    }

    results.sfn_service = sfn
    results.s3_service = s3

    yield {"sfn": sfn, "s3": s3}

    results.sfn_service = None
    results.s3_service = None


class TestGetResults:
    """Tests for GET /api/v1/assessments/{job_id}/results."""

    def test_returns_synthesis(self, mock_services):
        mock_services["s3"].read_synthesis.return_value = {
            "ranking": [{"target": "dynamodb", "confidence": 0.92}],
            "needs_deeper_analysis": False,
        }
        mock_services["s3"].read_triage.return_value = {
            "selected_agents": [{"agent_type": "dynamodb"}],
            "confidence": 0.87,
        }
        response = client.get("/api/v1/assessments/job-1/results")
        assert response.status_code == 200
        data = response.json()
        assert "synthesis" in data
        assert "triage_summary" in data

    def test_404_when_no_synthesis(self, mock_services):
        mock_services["s3"].read_synthesis.return_value = None
        response = client.get("/api/v1/assessments/job-1/results")
        assert response.status_code == 404

    def test_404_when_job_not_found(self, mock_services):
        mock_services["sfn"].describe_execution.return_value = None
        response = client.get("/api/v1/assessments/nonexistent/results")
        assert response.status_code == 404


class TestGetCollectorOutput:
    """Tests for GET /api/v1/assessments/{job_id}/collector."""

    def test_returns_collector_data(self, mock_services):
        mock_services["s3"].read_collector.return_value = {
            "job_id": "job-1",
            "agent_type": "collector",
            "status": "completed",
        }
        response = client.get("/api/v1/assessments/job-1/collector")
        assert response.status_code == 200
        assert response.json()["agent_type"] == "collector"

    def test_404_when_not_available(self, mock_services):
        mock_services["s3"].read_collector.return_value = None
        response = client.get("/api/v1/assessments/job-1/collector")
        assert response.status_code == 404


class TestGetTriageOutput:
    """Tests for GET /api/v1/assessments/{job_id}/triage."""

    def test_returns_triage_data(self, mock_services):
        mock_services["s3"].read_triage.return_value = {
            "selected_agents": [{"agent_type": "dynamodb", "reason": "key-value patterns"}],
            "skipped_agents": [{"agent_type": "neptune", "reason": "no graph patterns"}],
            "confidence": 0.87,
        }
        response = client.get("/api/v1/assessments/job-1/triage")
        assert response.status_code == 200
        data = response.json()
        assert len(data["selected_agents"]) == 1
        assert data["confidence"] == 0.87

    def test_404_when_not_available(self, mock_services):
        mock_services["s3"].read_triage.return_value = None
        response = client.get("/api/v1/assessments/job-1/triage")
        assert response.status_code == 404


class TestGetAnalysisOutput:
    """Tests for GET /api/v1/assessments/{job_id}/analysis/{agent_type}."""

    def test_returns_analysis_data(self, mock_services):
        mock_services["s3"].read_analysis.return_value = {
            "agent_type": "dynamodb",
            "confidence": 0.92,
            "status": "completed",
        }
        response = client.get("/api/v1/assessments/job-1/analysis/dynamodb")
        assert response.status_code == 200
        assert response.json()["agent_type"] == "dynamodb"

    def test_404_when_not_available(self, mock_services):
        mock_services["s3"].read_analysis.return_value = None
        response = client.get("/api/v1/assessments/job-1/analysis/neptune")
        assert response.status_code == 404


class TestGetSchemaDesigns:
    """Tests for GET /api/v1/assessments/{job_id}/schema-designs."""

    def test_returns_schema_designs(self, mock_services):
        mock_services["s3"].read_all_schema_designs.return_value = [
            {"target_type": "dynamodb", "content": {"tables": []}},
        ]
        response = client.get("/api/v1/assessments/job-1/schema-designs")
        assert response.status_code == 200
        data = response.json()
        assert len(data["schema_designs"]) == 1
        assert data["schema_designs"][0]["target_type"] == "dynamodb"

    def test_returns_empty_when_none(self, mock_services):
        mock_services["s3"].read_all_schema_designs.return_value = []
        response = client.get("/api/v1/assessments/job-1/schema-designs")
        assert response.status_code == 200
        assert len(response.json()["schema_designs"]) == 0
