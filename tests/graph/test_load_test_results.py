"""Tests for the load-test-results endpoint (grouped by access pattern)."""

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.routes import graph as graph_routes
from src.graph.populators import (
    populate_from_assignment,
    populate_from_collector,
    populate_from_load_test,
    populate_from_schema_design,
)
from src.graph.schema import initialize_schema
from src.graph.store import GraphStore

client = TestClient(app)


@pytest.fixture
def populated_store(
    tmp_path,
    sample_collector_output,
    sample_assignment,
    sample_schema_design_output,
    sample_load_test_output,
):
    """A graph store with AccessPattern, Query, and LoadTestRun nodes wired up."""
    store = GraphStore(str(tmp_path / "ltr.lbug"))
    initialize_schema(store)
    populate_from_collector(sample_collector_output, store)
    populate_from_assignment(sample_assignment, store)
    populate_from_schema_design(sample_schema_design_output, "dynamodb", 1, store)
    populate_from_load_test(sample_load_test_output, "dynamodb", 1, store)
    yield store
    store.close()


@pytest.fixture(autouse=True)
def override_graph(populated_store):
    """Override the graph dependency so the route sees the populated store.

    Uses app.dependency_overrides rather than patching module globals, so the
    test is immune to import order and to other tests mutating shared state.
    """
    app.dependency_overrides[graph_routes.get_graph_for_job] = lambda: (
        populated_store,
        "test_db",
    )
    yield
    app.dependency_overrides.pop(graph_routes.get_graph_for_job, None)


class TestLoadTestResults:
    """GET /api/v1/assessments/{job_id}/load-test-results."""

    def test_groups_by_pattern_with_nested_latency(self):
        """Results group under pattern_id with nested source/target latency objects."""
        resp = client.get("/api/v1/assessments/job-1/load-test-results")
        assert resp.status_code == 200
        body = resp.json()
        assert body["job_id"] == "job-1"
        assert len(body["results"]) == 1

        pattern = body["results"][0]
        assert pattern["pattern_id"] == "DDB-AP-1"
        assert pattern["engine"] == "dynamodb"
        assert pattern["schema_version"] == 1
        assert len(pattern["queries"]) == 1

        query = pattern["queries"][0]
        assert query["query_id"] == "q1"
        assert query["source_latency"]["p50"] == 45.0
        assert query["source_latency"]["p99"] == 132.0
        assert query["target_latency"]["p50"] == 3.0
        assert query["improvement_factor"] == 15.0

    def test_engine_filter_matches(self):
        """engine=dynamodb returns the pattern."""
        resp = client.get("/api/v1/assessments/job-1/load-test-results?engine=dynamodb")
        assert resp.status_code == 200
        assert len(resp.json()["results"]) == 1

    def test_engine_filter_excludes(self):
        """A non-matching engine returns no results."""
        resp = client.get("/api/v1/assessments/job-1/load-test-results?engine=documentdb")
        assert resp.status_code == 200
        assert resp.json()["results"] == []

    def test_prefix_filter_matches(self):
        """prefix=DDB- returns the DynamoDB pattern."""
        resp = client.get("/api/v1/assessments/job-1/load-test-results?prefix=DDB-")
        assert resp.status_code == 200
        assert len(resp.json()["results"]) == 1

    def test_prefix_filter_excludes(self):
        """A non-matching prefix returns no results."""
        resp = client.get("/api/v1/assessments/job-1/load-test-results?prefix=DOC-")
        assert resp.status_code == 200
        assert resp.json()["results"] == []

    def test_version_filter_matches(self):
        """version=1 returns the pattern; a missing version returns nothing."""
        assert (
            len(
                client.get("/api/v1/assessments/job-1/load-test-results?version=1").json()[
                    "results"
                ]
            )
            == 1
        )
        assert (
            client.get("/api/v1/assessments/job-1/load-test-results?version=2").json()["results"]
            == []
        )
