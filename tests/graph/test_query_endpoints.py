"""Endpoint tests for curated graph views (dedicated-app pattern)."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes import graph as graph_routes
from src.graph.populators import (
    populate_from_assignment,
    populate_from_collector,
    populate_from_schema_design,
)
from src.graph.schema import initialize_schema
from src.graph.store import GraphStore


@pytest.fixture
def populated_store(
    tmp_path, sample_collector_output, sample_assignment, sample_schema_design_output
):
    store = GraphStore(str(tmp_path / "q.lbug"))
    initialize_schema(store)
    populate_from_collector(sample_collector_output, store)
    populate_from_assignment(sample_assignment, store)
    populate_from_schema_design(sample_schema_design_output, "dynamodb", 1, store)
    yield store
    store.close()


@pytest.fixture
def client(populated_store):
    app = FastAPI()
    app.include_router(graph_routes.router)
    app.dependency_overrides[graph_routes.get_graph_for_job] = lambda: (populated_store, "db")
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_engine_detail_endpoint(client):
    resp = client.get("/api/v1/assessments/job-1/graph/engines/dynamodb")
    assert resp.status_code == 200
    assert resp.json()["engine"] == "dynamodb"


def test_risks_endpoint_empty_ok(client):
    resp = client.get("/api/v1/assessments/job-1/graph/risks")
    assert resp.status_code == 200
    assert resp.json()["hotspots"] == []


def test_table_impact_endpoint(client):
    resp = client.get("/api/v1/assessments/job-1/graph/tables/orders/impact")
    assert resp.status_code == 200
    assert resp.json()["table_id"] == "orders"


def test_query_provenance_endpoint(client, sample_collector_output):
    qid = sample_collector_output["queries"]["query_patterns"][0]["query_id"]
    resp = client.get(f"/api/v1/assessments/job-1/graph/queries/{qid}/provenance")
    assert resp.status_code == 200
    assert resp.json()["query_id"] == qid
