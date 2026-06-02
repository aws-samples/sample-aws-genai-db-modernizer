"""Unit tests for FastAPI Hello World endpoints.

Requirements: 7.1, 7.2, 7.3, 7.4
"""

from fastapi.testclient import TestClient

from src.api.main import app

client = TestClient(app)


class TestRootEndpoint:
    """Tests for GET / endpoint."""

    def test_root_returns_200(self):
        response = client.get("/")
        assert response.status_code == 200

    def test_root_has_message_field(self):
        response = client.get("/")
        data = response.json()
        assert "message" in data

    def test_root_content_type_is_json(self):
        response = client.get("/")
        assert response.headers["content-type"] == "application/json"


class TestHealthEndpoint:
    """Tests for GET /health endpoint."""

    def test_health_returns_200(self):
        response = client.get("/health")
        assert response.status_code == 200

    def test_health_has_status_field(self):
        response = client.get("/health")
        data = response.json()
        assert "status" in data

    def test_health_content_type_is_json(self):
        response = client.get("/health")
        assert response.headers["content-type"] == "application/json"
