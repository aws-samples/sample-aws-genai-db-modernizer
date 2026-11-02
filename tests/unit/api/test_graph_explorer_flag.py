# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The experimental graph explorer flag (#308) on the real app.

Off (the default), the probe answers 200 ``{"enabled": false}`` (the results
page asks on every load, so a 404 there would be a failed request), and every
explorer data path is, for every method, a plain 404 like an unknown path. The
OpenAPI document never lists them. On, the probe says so with its bounds.
"""

from fastapi.testclient import TestClient

from src.api.main import app
from src.api.routes.graph_explorer import EXPLORER_ENV_FLAG

PROBE = "/api/v1/experimental/graph-explorer"
EXPLORER_PATHS = (
    "/api/v1/assessments/job-1/graph/explore/options",
    "/api/v1/assessments/job-1/graph/explore/links?kind=table&id=t",
    "/api/v1/assessments/job-1/graph/explore/search?text=select",
    "/api/v1/assessments/job-1/graph/explore/query?id=q",
    "/api/v1/assessments/job-1/graph/explore/overlays",
    "/api/v1/assessments/job-1/graph/explore/walkthroughs",
    "/api/v1/assessments/job-1/graph/explore/below-traffic?min_calls_per_second=1&query_id=q",
)


def test_flag_off_the_probe_answers_disabled(monkeypatch):
    for value in (None, "0", "true", "yes", ""):
        if value is None:
            monkeypatch.delenv(EXPLORER_ENV_FLAG, raising=False)
        else:
            monkeypatch.setenv(EXPLORER_ENV_FLAG, value)
        resp = TestClient(app).get(PROBE)
        assert resp.status_code == 200, value
        assert resp.json() == {"enabled": False}, value


def test_flag_off_by_default_every_explorer_path_is_not_found(monkeypatch):
    monkeypatch.delenv(EXPLORER_ENV_FLAG, raising=False)
    client = TestClient(app)
    unknown = client.get("/api/v1/no-such-route")
    for path in EXPLORER_PATHS:
        resp = client.get(path)
        assert resp.status_code == 404, path
        # The same answer as a route that does not exist.
        assert resp.content == unknown.content


def test_flag_on_probe_reports_enabled(monkeypatch):
    monkeypatch.setenv(EXPLORER_ENV_FLAG, "1")
    resp = TestClient(app).get(PROBE)
    assert resp.status_code == 200
    assert resp.json() == {"enabled": True, "max_links": 60, "max_options": 1000}


OTHER_METHODS = ("POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
ROUTE_PATHS = tuple(p.split("?")[0] for p in EXPLORER_PATHS) + (
    "/api/v1/assessments/job-1/graph/explore",
    "/api/v1/assessments/job-1/graph/explore/",
    "/api/v1/assessments/job-1/graph/explore/no-such-view",
)


def test_flag_off_every_method_on_an_explorer_path_is_not_found(monkeypatch):
    monkeypatch.delenv(EXPLORER_ENV_FLAG, raising=False)
    client = TestClient(app)
    for method in OTHER_METHODS:
        unknown = client.request(method, "/api/v1/no-such-route")
        assert unknown.status_code == 404, method
        for path in ROUTE_PATHS:
            resp = client.request(method, path)
            assert resp.status_code == 404, (method, path)
            assert resp.content == unknown.content, (method, path)
            assert "allow" not in resp.headers, (method, path)


def test_flag_on_other_methods_are_not_allowed(monkeypatch):
    monkeypatch.setenv(EXPLORER_ENV_FLAG, "1")
    client = TestClient(app)
    for method in OTHER_METHODS:
        for path in ROUTE_PATHS:
            resp = client.request(method, path)
            assert resp.status_code == 405, (method, path)
            assert resp.headers["allow"] == "GET", (method, path)


NO_VIEW_PATHS = (
    "/api/v1/assessments/job-1/graph/explore",
    "/api/v1/assessments/job-1/graph/explore/",
    "/api/v1/assessments/job-1/graph/explore/no-such-view",
)


def test_a_get_to_a_path_that_is_no_view_is_not_found_on_or_off(monkeypatch):
    # The catch-all must not turn main's 404 for these paths into a 405.
    for value in (None, "1"):
        if value is None:
            monkeypatch.delenv(EXPLORER_ENV_FLAG, raising=False)
        else:
            monkeypatch.setenv(EXPLORER_ENV_FLAG, value)
        client = TestClient(app)
        unknown = client.get("/api/v1/no-such-route")
        for path in NO_VIEW_PATHS:
            resp = client.get(path)
            assert resp.status_code == 404, (value, path)
            assert resp.content == unknown.content, (value, path)
            assert "allow" not in resp.headers, (value, path)


def test_explorer_paths_never_join_the_openapi_document(monkeypatch):
    for value in ("1", None):
        if value is None:
            monkeypatch.delenv(EXPLORER_ENV_FLAG, raising=False)
        else:
            monkeypatch.setenv(EXPLORER_ENV_FLAG, value)
        paths = TestClient(app).get("/openapi.json").json()["paths"]
        assert not [p for p in paths if "explore" in p], value
