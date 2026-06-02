"""Property-based tests for FastAPI endpoints.

Feature: basic-infrastructure-cicd
"""

from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis.strategies import integers

from src.api.main import app

client = TestClient(app)


# Feature: basic-infrastructure-cicd, Property 5: API Endpoint Response Format
class TestApiResponseFormatProperty:
    """**Validates: Requirements 7.2, 10.3**"""

    @settings(max_examples=100)
    @given(iteration=integers(min_value=0, max_value=10000))
    def test_root_endpoint_always_returns_200_with_message(self, iteration):
        """Property: All GET / responses have 200 status and a 'message' string field."""
        response = client.get("/")
        assert response.status_code == 200
        data = response.json()
        assert "message" in data
        assert isinstance(data["message"], str)


# Feature: basic-infrastructure-cicd, Property 6: Health Endpoint Availability
class TestHealthEndpointAvailabilityProperty:
    """**Validates: Requirements 7.4**"""

    @settings(max_examples=100)
    @given(iteration=integers(min_value=0, max_value=10000))
    def test_health_endpoint_always_returns_200(self, iteration):
        """Property: All GET /health responses return 200 status."""
        response = client.get("/health")
        assert response.status_code == 200
