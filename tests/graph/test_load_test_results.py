"""Tests for the load-test-results endpoint (grouped by access pattern)."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes import graph as graph_routes
from src.graph.populators import (
    populate_from_assignment,
    populate_from_collector,
    populate_from_load_test,
    populate_from_schema_design,
)
from src.graph.schema import initialize_schema
from src.graph.store import GraphStore


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


@pytest.fixture
def client(populated_store):
    """A TestClient over a dedicated app that mounts only the graph router.

    Building a fresh app (rather than importing src.api.main.app) keeps this
    test independent of the main app's import-time service wiring and of any
    other test mutating that shared app. The graph dependency is overridden to
    return the pre-populated store.
    """
    app = FastAPI()
    app.include_router(graph_routes.router)
    app.dependency_overrides[graph_routes.get_graph_for_job] = lambda: (
        populated_store,
        "test_db",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestLoadTestResults:
    """GET /api/v1/assessments/{job_id}/load-test-results."""

    def test_groups_by_pattern_with_nested_latency(self, client):
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

    def test_engine_filter_matches(self, client):
        """engine=dynamodb returns the pattern."""
        resp = client.get("/api/v1/assessments/job-1/load-test-results?engine=dynamodb")
        assert resp.status_code == 200
        assert len(resp.json()["results"]) == 1

    def test_engine_filter_excludes(self, client):
        """A non-matching engine returns no results."""
        resp = client.get("/api/v1/assessments/job-1/load-test-results?engine=documentdb")
        assert resp.status_code == 200
        assert resp.json()["results"] == []

    def test_prefix_filter_matches(self, client):
        """prefix=DDB- returns the DynamoDB pattern."""
        resp = client.get("/api/v1/assessments/job-1/load-test-results?prefix=DDB-")
        assert resp.status_code == 200
        assert len(resp.json()["results"]) == 1

    def test_prefix_filter_excludes(self, client):
        """A non-matching prefix returns no results."""
        resp = client.get("/api/v1/assessments/job-1/load-test-results?prefix=DOC-")
        assert resp.status_code == 200
        assert resp.json()["results"] == []

    def test_version_filter_matches(self, client):
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


def _run(query_id: str, p50: float, factor: float) -> dict:
    latency = {"p50": p50, "p90": p50, "p95": p50, "p99": p50, "p999": p50, "min": p50, "max": p50}
    return {
        "query_id": query_id,
        "source_latency_ms": latency,
        "target_latency_ms": latency,
        "improvement_factor": factor,
        "throughput_rps": 100.0,
        "error_rate_pct": 0.0,
    }


class TestMultiEngineRuns:
    """A query designed and load tested on two engines keeps each engine's runs apart."""

    @pytest.fixture
    def two_engine_store(self, tmp_path, sample_collector_output, sample_assignment):
        store = GraphStore(str(tmp_path / "two.lbug"))
        initialize_schema(store)
        populate_from_collector(sample_collector_output, store)
        populate_from_assignment(sample_assignment, store)
        for engine, pid in (("dynamodb", "DDB-AP-1"), ("opensearch", "OS-AP-1")):
            design = {
                "access_patterns": [
                    {"pattern_id": pid, "query_ids": ["q1", "q3"], "description": engine}
                ]
            }
            populate_from_schema_design(design, engine, 1, store)
        populate_from_load_test(
            {"pattern_results": [_run("q3", 3.0, 3.0), _run("q1", 2.0, 2.0)]}, "dynamodb", 1, store
        )
        populate_from_load_test(
            {"pattern_results": [_run("q1", 9.0, 9.0), _run("q3", 8.0, 8.0)]},
            "opensearch",
            1,
            store,
        )
        yield store
        store.close()

    def test_each_pattern_lists_only_its_own_engines_runs(self, two_engine_store):
        from src.graph.queries import load_test_results

        resp = load_test_results(two_engine_store, "job-1")
        by_id = {
            r.pattern_id: [(q.query_id, q.improvement_factor) for q in r.queries]
            for r in resp.results
        }
        assert by_id == {
            "DDB-AP-1": [("q1", 2.0), ("q3", 3.0)],
            "OS-AP-1": [("q1", 9.0), ("q3", 8.0)],
        }

    def test_ordering_is_stable_across_reads(self, two_engine_store):
        from src.graph.queries import load_test_results

        first = load_test_results(two_engine_store, "job-1").model_dump()
        for _ in range(3):
            assert load_test_results(two_engine_store, "job-1").model_dump() == first

    def test_run_from_other_schema_version_is_not_attached(self, two_engine_store):
        from src.graph.queries import load_test_results

        populate_from_load_test(
            {"pattern_results": [_run("q1", 50.0, 50.0)]}, "dynamodb", 2, two_engine_store
        )
        resp = load_test_results(two_engine_store, "job-1", engine="dynamodb", version=1)
        assert [(q.query_id, q.improvement_factor) for q in resp.results[0].queries] == [
            ("q1", 2.0),
            ("q3", 3.0),
        ]
