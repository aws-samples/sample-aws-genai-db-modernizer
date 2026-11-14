# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The catalog routes (#307) sit behind the same gate as the other graph routes.

The API's gate is app-wide: it listens on loopback only, the Host allowlist
refuses any other host name before a route runs, CORS admits only the local UI
origin, and request bodies are capped. The catalog routes are curated and
read-only, so like the curated ``GET /graph/*`` views they need no flag; unlike
the raw query route they never take Cypher.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from src.api.dependencies import job_id_param
from src.api.main import app
from src.api.routes import graph

CATALOG_ROUTES = [
    ("GET", "/api/v1/assessments/job1/graph/catalog", None),
    ("POST", "/api/v1/assessments/job1/graph/catalog/node_census", b"{}"),
]
CURATED_ROUTES = [
    ("GET", "/api/v1/assessments/job1/graph/risks", None),
    ("GET", "/api/v1/assessments/job1/graph/engines/dynamodb", None),
]


def _routes() -> dict[tuple[str, str], APIRoute]:
    found: dict[tuple[str, str], APIRoute] = {}
    for route in app.routes:
        candidates = [route]
        original = getattr(route, "original_router", None)
        if original is not None:
            candidates += list(getattr(original, "routes", []))
        for sub in candidates:
            if isinstance(sub, APIRoute):
                for method in sub.methods:
                    found[(method, sub.path)] = sub
    return found


@pytest.fixture
def no_job(monkeypatch):
    """Services wired so a valid job id 404s cleanly instead of reading artifacts."""
    sfn = MagicMock()
    sfn.describe_execution.return_value = None
    for attr, value in (
        ("sfn_service", sfn),
        ("graph_cache", MagicMock()),
        ("artifact_store", MagicMock()),
        ("graph_persistence", MagicMock()),
    ):
        monkeypatch.setattr(graph, attr, value)


def test_the_catalog_routes_are_registered_on_the_app():
    routes = _routes()
    assert ("GET", "/api/v1/assessments/{job_id}/graph/catalog") in routes
    assert ("POST", "/api/v1/assessments/{job_id}/graph/catalog/{entry_id}") in routes


@pytest.mark.parametrize(
    "key",
    [
        ("GET", "/api/v1/assessments/{job_id}/graph/catalog"),
        ("POST", "/api/v1/assessments/{job_id}/graph/catalog/{entry_id}"),
    ],
)
def test_the_catalog_routes_validate_the_job_id_and_lease_the_graph(key):
    route = _routes()[key]
    calls: set = set()

    def walk(dependant):
        for sub in dependant.dependencies:
            calls.add(sub.call)
            walk(sub)

    walk(route.dependant)
    assert job_id_param in calls
    assert graph.get_graph_for_job in calls
    assert graph.require_raw_query_enabled not in calls


@pytest.mark.passes_on_main  # the guard is app-wide: it already covers every path
@pytest.mark.parametrize(("method", "path", "body"), CATALOG_ROUTES + CURATED_ROUTES)  # type: ignore[operator]
def test_a_foreign_host_header_is_refused_before_the_route(no_job, method, path, body):
    client = TestClient(app, base_url="http://evil.example")
    resp = client.request(method, path, content=body, headers={"Content-Type": "application/json"})
    assert resp.status_code == 400
    assert resp.text == "Invalid host header."


@pytest.mark.parametrize(("method", "path", "body"), CATALOG_ROUTES)
def test_a_loopback_host_reaches_the_route(no_job, method, path, body, monkeypatch):
    monkeypatch.delenv(graph.RAW_QUERY_ENV_FLAG, raising=False)
    client = TestClient(app, base_url="http://127.0.0.1:8000")
    resp = client.request(method, path, content=body, headers={"Content-Type": "application/json"})
    # The route ran and found no such job: not the host guard, not the raw-query flag.
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Assessment not found"


@pytest.mark.passes_on_main  # CORS is app-wide: it already covers every path
@pytest.mark.parametrize(("method", "path", "body"), CATALOG_ROUTES + CURATED_ROUTES)  # type: ignore[operator]
def test_another_origin_gets_no_cors_grant(no_job, method, path, body):
    client = TestClient(app, base_url="http://localhost:8000")
    resp = client.request(
        method,
        path,
        content=body,
        headers={"Content-Type": "application/json", "Origin": "http://evil.example"},
    )
    assert "access-control-allow-origin" not in resp.headers


@pytest.mark.passes_on_main  # CORS is app-wide: it already covers every path
@pytest.mark.parametrize("path", [path for method, path, _ in CATALOG_ROUTES if method == "POST"])
def test_a_foreign_preflight_is_refused_and_the_ui_origin_granted(no_job, path):
    """A cross-origin JSON POST needs this preflight; only the local UI origin passes it."""
    client = TestClient(app, base_url="http://localhost:8000")
    preflight = {
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type",
    }
    refused = client.options(path, headers={**preflight, "Origin": "http://evil.example"})
    assert refused.status_code == 400
    assert "access-control-allow-origin" not in refused.headers
    granted = client.options(path, headers={**preflight, "Origin": "http://localhost:3000"})
    assert granted.status_code == 200
    assert granted.headers["access-control-allow-origin"] == "http://localhost:3000"


@pytest.mark.passes_on_main  # the body cap is app-wide: it already covers every path
def test_the_app_wide_body_cap_applies_to_the_catalog_run(no_job):
    client = TestClient(app, base_url="http://localhost:8000")
    resp = client.post(
        "/api/v1/assessments/job1/graph/catalog/node_census",
        content=b" " * (2 * 1024 * 1024),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 413
