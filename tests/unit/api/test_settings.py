"""Unit tests for settings routes."""

from fastapi.testclient import TestClient

from src.api.main import app

client = TestClient(app)


class TestGetSettings:
    """Tests for GET /api/v1/settings."""

    def test_returns_200(self):
        response = client.get("/api/v1/settings")
        assert response.status_code == 200

    def test_returns_all_sections(self):
        response = client.get("/api/v1/settings")
        data = response.json()
        assert "aws_configuration" in data
        assert "default_analysis_options" in data
        assert "ui_preferences" in data

    def test_default_values(self):
        response = client.get("/api/v1/settings")
        data = response.json()
        assert data["default_analysis_options"]["query_log_period_days"] == 7
        assert data["default_analysis_options"]["sample_size"] == 1000
        assert data["default_analysis_options"]["anonymize_pii"] is True
        assert data["ui_preferences"]["color_theme"] == "system"


class TestUpdateSettings:
    """Tests for PUT /api/v1/settings."""

    def test_returns_200(self):
        response = client.put(
            "/api/v1/settings",
            json={
                "ui_preferences": {"color_theme": "dark"},
            },
        )
        assert response.status_code == 200

    def test_updates_values(self):
        client.put(
            "/api/v1/settings",
            json={
                "ui_preferences": {"color_theme": "dark", "compact_mode": True},
            },
        )
        response = client.get("/api/v1/settings")
        data = response.json()
        assert data["ui_preferences"]["color_theme"] == "dark"


class TestTestConnection:
    """Tests for POST /api/v1/settings/test-connection."""

    def test_returns_200(self):
        response = client.post("/api/v1/settings/test-connection")
        assert response.status_code == 200

    def test_returns_status_per_service(self):
        response = client.post("/api/v1/settings/test-connection")
        data = response.json()
        assert "s3_bucket" in data
        assert "dynamodb_table" in data
        assert data["s3_bucket"]["status"] in ("ok", "error", "skipped")
