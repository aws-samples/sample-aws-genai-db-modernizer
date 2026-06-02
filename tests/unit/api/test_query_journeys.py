"""Unit tests for query journeys route."""

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.routes import query_journeys

client = TestClient(app)


@pytest.fixture(autouse=True)
def mock_services():
    """Inject mock services into the query_journeys router."""
    sfn = MagicMock()
    store = MagicMock()

    sfn.describe_execution.return_value = {
        "status": "SUCCEEDED",
        "input": {"database_name": "test_db"},
    }

    query_journeys.sfn_service = sfn
    query_journeys.artifact_store = store

    yield {"sfn": sfn, "store": store}

    query_journeys.sfn_service = None
    query_journeys.artifact_store = None


class TestListQueryJourneys:
    """Tests for GET /api/v1/assessments/{job_id}/query-journeys (list)."""

    def test_returns_paginated_journeys(self, mock_services):
        """Returns first page of journeys with pagination metadata."""
        mock_services["store"].list_prefix.return_value = [
            "test_db/job-1/query-journeys/q_001.json",
            "test_db/job-1/query-journeys/q_002.json",
            "test_db/job-1/query-journeys/q_003.json",
        ]
        mock_services["store"].read_json.side_effect = [
            {
                "query_id": "q_001",
                "source": {},
                "assignment": None,
                "design": None,
                "load_test": None,
                "sdk_code": None,
            },
            {
                "query_id": "q_002",
                "source": {},
                "assignment": None,
                "design": None,
                "load_test": None,
                "sdk_code": None,
            },
            {
                "query_id": "q_003",
                "source": {},
                "assignment": None,
                "design": None,
                "load_test": None,
                "sdk_code": None,
            },
        ]

        response = client.get("/api/v1/assessments/job-1/query-journeys")

        assert response.status_code == 200
        data = response.json()
        assert data["job_id"] == "job-1"
        assert data["total"] == 3
        assert data["page"] == 1
        assert data["page_size"] == 50
        assert data["total_pages"] == 1
        assert len(data["items"]) == 3
        assert data["items"][0]["query_id"] == "q_001"

    def test_respects_page_and_page_size(self, mock_services):
        """Returns the requested page with correct slicing."""
        mock_services["store"].list_prefix.return_value = [
            f"test_db/job-1/query-journeys/q_00{i}.json" for i in range(1, 6)
        ]
        mock_services["store"].read_json.side_effect = [
            {
                "query_id": "q_003",
                "source": {},
                "assignment": None,
                "design": None,
                "load_test": None,
                "sdk_code": None,
            },
            {
                "query_id": "q_004",
                "source": {},
                "assignment": None,
                "design": None,
                "load_test": None,
                "sdk_code": None,
            },
        ]

        response = client.get("/api/v1/assessments/job-1/query-journeys?page=2&page_size=2")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 5
        assert data["page"] == 2
        assert data["page_size"] == 2
        assert data["total_pages"] == 3
        assert len(data["items"]) == 2

    def test_returns_404_when_no_journeys(self, mock_services):
        """Returns 404 when query-journeys folder is empty or missing."""
        mock_services["store"].list_prefix.return_value = []

        response = client.get("/api/v1/assessments/job-1/query-journeys")

        assert response.status_code == 404

    def test_returns_empty_items_for_page_beyond_total(self, mock_services):
        """Returns 200 with empty items when page exceeds total_pages."""
        mock_services["store"].list_prefix.return_value = [
            "test_db/job-1/query-journeys/q_001.json",
        ]

        response = client.get("/api/v1/assessments/job-1/query-journeys?page=99")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert data["page"] == 99
        assert data["items"] == []

    def test_clamps_page_size_to_max(self, mock_services):
        """page_size > 200 is clamped to 200."""
        mock_services["store"].list_prefix.return_value = [
            "test_db/job-1/query-journeys/q_001.json",
        ]
        mock_services["store"].read_json.return_value = {
            "query_id": "q_001",
            "source": {},
            "assignment": None,
            "design": None,
            "load_test": None,
            "sdk_code": None,
        }

        response = client.get("/api/v1/assessments/job-1/query-journeys?page_size=500")

        assert response.status_code == 200
        data = response.json()
        assert data["page_size"] == 200

    def test_returns_503_when_store_not_configured(self, mock_services):
        """Returns 503 when artifact_store is None."""
        query_journeys.artifact_store = None

        response = client.get("/api/v1/assessments/job-1/query-journeys")

        assert response.status_code == 503


class TestGetQueryJourney:
    """Tests for GET /api/v1/assessments/{job_id}/query-journeys/{query_id}."""

    def test_returns_200_with_journey_data(self, mock_services):
        """Returns 200 with journey data when found."""
        mock_services["store"].read_json.return_value = {
            "query_id": "q-001",
            "original_sql": "SELECT * FROM orders",
            "modernized_sql": "SELECT * FROM orders",
            "steps": [],
        }

        response = client.get("/api/v1/assessments/job-1/query-journeys/q-001")

        assert response.status_code == 200
        data = response.json()
        assert data["query_id"] == "q-001"
        assert "original_sql" in data
        mock_services["store"].read_json.assert_called_once_with(
            "test_db/job-1/query-journeys/q-001.json"
        )

    def test_returns_404_when_journey_file_not_found(self, mock_services):
        """Returns 404 when the journey file does not exist in the artifact store."""
        mock_services["store"].read_json.side_effect = FileNotFoundError("not found")

        response = client.get("/api/v1/assessments/job-1/query-journeys/q-999")

        assert response.status_code == 404
        assert "q-999" in response.json()["detail"]

    def test_returns_404_when_job_not_found(self, mock_services):
        """Returns 404 when the Step Functions execution does not exist."""
        mock_services["sfn"].describe_execution.return_value = None

        response = client.get("/api/v1/assessments/nonexistent-job/query-journeys/q-001")

        assert response.status_code == 404
        assert response.json()["detail"] == "Assessment not found"

    def test_returns_503_when_sfn_service_not_configured(self, mock_services):
        """Returns 503 when sfn_service is None (services not configured)."""
        query_journeys.sfn_service = None

        response = client.get("/api/v1/assessments/job-1/query-journeys/q-001")

        assert response.status_code == 503
        assert response.json()["detail"] == "Services not configured"

    def test_returns_503_when_artifact_store_not_configured(self, mock_services):
        """Returns 503 when artifact_store is None (services not configured)."""
        query_journeys.artifact_store = None

        response = client.get("/api/v1/assessments/job-1/query-journeys/q-001")

        assert response.status_code == 503
        assert response.json()["detail"] == "Services not configured"
