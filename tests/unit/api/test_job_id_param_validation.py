"""Every local API route that takes job_id as a path parameter must reject
an invalid one with 422 before any artifact is read, listed, written, or
deleted (#508) -- the same property #420/#505 established for
database_name. FastAPI resolves a route's Depends() dependencies while it
resolves the request, before the route body runs, so an invalid job_id
never reaches a service call.

``src.api.main`` wires every router's service globals to real,
filesystem-backed local services at import time (this is the production
app, not a test double), so the "accepts a valid job_id" cases below mock
them the same way the rest of the API test suite does (see
test_assessments.py, test_schema_revisions.py, ...): otherwise a *valid but
nonexistent* job_id would run real pipeline logic (e.g. resume_phase ->
the orchestrator -> the collector) instead of cleanly 404ing.

"a%2Fb" (an embedded separator) is excluded from the main table: Starlette's
router treats a decoded "/" as a segment boundary and 404s before any
dependency runs, a different but equally safe outcome covered by its own
test below. job_id is a *path* parameter, so a literal ".." doesn't reach
the server as typed either -- httpx (which TestClient wraps) resolves
dot-segments client-side -- hence the percent-encoded forms, which are what
actually reaches the router and gets decoded into the parameter.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.routes import (
    agent_interaction,
    assessments,
    assignments,
    graph,
    phases,
    query_journeys,
    results,
    schema_revisions,
)
from src.contracts.phase_models import Phase, PhaseProgression

client = TestClient(app)

_BAD_JOB_IDS = ["%2e%2e", "%2e", "a\\b", "a b", "café", "a" * 129]
_VALID_JOB_ID = "job1"
_VALID_DB = "wordpress"


@pytest.fixture(autouse=True)
def mock_services():
    """Replace every router module's real service globals with mocks, so a
    valid-but-nonexistent job_id gets a predictable, safe response instead
    of running real pipeline/graph logic."""
    store = MagicMock()
    store.exists.return_value = False
    store.read_json.return_value = {}
    store.list_prefix.return_value = []

    sfn = MagicMock()
    sfn.describe_execution.return_value = None
    sfn.get_execution_history.return_value = []
    sfn.get_full_execution_history.return_value = []

    s3 = MagicMock()
    s3.read_synthesis.return_value = None
    s3.read_collector.return_value = None
    s3.read_triage.return_value = None
    s3.read_reality_check.return_value = None
    s3.read_all_schema_designs.return_value = []
    s3.client.head_object.side_effect = Exception("not found")
    s3.client.list_objects_v2.return_value = {"Contents": []}

    orchestrator = MagicMock()
    orchestrator.get_progression.return_value = PhaseProgression(
        job_id=_VALID_JOB_ID, current_phase=Phase.COLLECT_TRIAGE, phases={}
    )

    graph_cache = MagicMock()
    graph_persistence = MagicMock()

    modules_and_values = [
        (assessments, "sfn_service", sfn),
        (assessments, "s3_service", s3),
        (agent_interaction, "artifact_store", store),
        (assignments, "artifact_store", store),
        (graph, "artifact_store", store),
        (graph, "graph_cache", graph_cache),
        (graph, "graph_persistence", graph_persistence),
        (graph, "sfn_service", sfn),
        (phases, "orchestrator", orchestrator),
        (query_journeys, "sfn_service", sfn),
        (query_journeys, "artifact_store", store),
        (results, "sfn_service", sfn),
        (results, "s3_service", s3),
        (schema_revisions, "artifact_store", store),
        (schema_revisions, "orchestrator", orchestrator),
    ]
    originals = [(mod, attr, getattr(mod, attr)) for mod, attr, _ in modules_and_values]
    for mod, attr, value in modules_and_values:
        setattr(mod, attr, value)
    yield {"store": store, "sfn": sfn, "s3": s3, "orchestrator": orchestrator}
    for mod, attr, original in originals:
        setattr(mod, attr, original)


# (method, path template with {job_id}, extra query params, json body)
_ROUTES: list[tuple[str, str, dict, dict | None]] = [
    # assessments.py
    ("POST", "/api/v1/assessments/{job_id}/uploads/confirm", {"database_name": _VALID_DB}, None),
    ("GET", "/api/v1/assessments/{job_id}/uploads", {"database_name": _VALID_DB}, None),
    (
        "DELETE",
        "/api/v1/assessments/{job_id}/uploads/collector-output.json",
        {"database_name": _VALID_DB},
        None,
    ),
    ("GET", "/api/v1/assessments/{job_id}", {}, None),
    ("DELETE", "/api/v1/assessments/{job_id}", {}, None),
    ("DELETE", "/api/v1/assessments/{job_id}/artifacts", {"database_name": _VALID_DB}, None),
    ("GET", "/api/v1/assessments/{job_id}/agents", {}, None),
    ("GET", "/api/v1/assessments/{job_id}/execution-history", {}, None),
    # agent_interaction.py -- "schema-dynamodb", not "triage": this PR adds
    # an allowlist for the agent path parameter, so an arbitrary placeholder
    # would 422 on *agent* and mask the job_id check this file is about.
    (
        "GET",
        "/api/v1/assessments/{job_id}/agents/schema-dynamodb/questions",
        {"database_name": _VALID_DB},
        None,
    ),
    (
        "PUT",
        "/api/v1/assessments/{job_id}/agents/schema-dynamodb/answers",
        {"database_name": _VALID_DB},
        {"answers": {}},
    ),
    (
        "POST",
        "/api/v1/assessments/{job_id}/agents/schema-dynamodb/validate-answer",
        {"database_name": _VALID_DB},
        {"question_id": "q1", "answer": "a"},
    ),
    # assignments.py
    ("GET", "/api/v1/assessments/{job_id}/assignments", {"database_name": _VALID_DB}, None),
    ("PUT", "/api/v1/assessments/{job_id}/assignments", {"database_name": _VALID_DB}, {}),
    # graph.py (curated GET endpoints -- always on, unlike raw query/rebuild)
    ("GET", "/api/v1/assessments/{job_id}/load-test-results", {}, None),
    ("GET", "/api/v1/assessments/{job_id}/graph/tables/t1/impact", {}, None),
    ("GET", "/api/v1/assessments/{job_id}/graph/queries/q1/provenance", {}, None),
    ("GET", "/api/v1/assessments/{job_id}/graph/engines/dynamodb", {}, None),
    ("GET", "/api/v1/assessments/{job_id}/graph/risks", {}, None),
    # phases.py
    ("GET", "/api/v1/assessments/{job_id}/phases", {}, None),
    ("POST", "/api/v1/assessments/{job_id}/resume", {}, {"phase": "collect_triage"}),
    # query_journeys.py
    ("GET", "/api/v1/assessments/{job_id}/query-journeys", {}, None),
    ("GET", "/api/v1/assessments/{job_id}/query-journeys/q1", {}, None),
    # results.py
    ("GET", "/api/v1/assessments/{job_id}/results", {}, None),
    ("GET", "/api/v1/assessments/{job_id}/collector", {}, None),
    ("GET", "/api/v1/assessments/{job_id}/triage", {}, None),
    ("GET", "/api/v1/assessments/{job_id}/analysis/dynamodb", {}, None),
    ("GET", "/api/v1/assessments/{job_id}/reality-check", {}, None),
    ("GET", "/api/v1/assessments/{job_id}/results/table-mappings", {}, None),
    ("GET", "/api/v1/assessments/{job_id}/schema-designs", {}, None),
    # schema_revisions.py
    (
        "GET",
        "/api/v1/assessments/{job_id}/schema/dynamodb",
        {"database_name": _VALID_DB},
        None,
    ),
    (
        "GET",
        "/api/v1/assessments/{job_id}/schema/dynamodb/versions",
        {"database_name": _VALID_DB},
        None,
    ),
    (
        "PUT",
        "/api/v1/assessments/{job_id}/schema/dynamodb/revisions",
        {"database_name": _VALID_DB},
        {
            "base_version": 1,
            "pattern_modifications": [],
            "table_modifications": [],
            "new_patterns": [],
        },
    ),
    (
        "POST",
        "/api/v1/assessments/{job_id}/schema/dynamodb/confirm",
        {"database_name": _VALID_DB},
        None,
    ),
    (
        "POST",
        "/api/v1/assessments/{job_id}/schema/confirm-all",
        {"database_name": _VALID_DB},
        None,
    ),
]


@pytest.mark.parametrize("method,path_template,params,body", _ROUTES)
@pytest.mark.parametrize("bad_job_id", _BAD_JOB_IDS)
def test_rejects_invalid_job_id(
    method: str, path_template: str, params: dict, body: dict | None, bad_job_id: str
) -> None:
    path = path_template.format(job_id=bad_job_id)
    resp = client.request(method, path, params=params, json=body)
    assert resp.status_code == 422, resp.text
    assert "job_id" in str(resp.json())


@pytest.mark.parametrize("method,path_template,params,body", _ROUTES)
def test_accepts_valid_job_id(
    method: str, path_template: str, params: dict, body: dict | None
) -> None:
    path = path_template.format(job_id=_VALID_JOB_ID)
    resp = client.request(method, path, params=params, json=body)
    assert resp.status_code != 422 or "job_id" not in str(resp.json())


def test_404s_rather_than_runs_on_an_embedded_separator() -> None:
    """ "a%2Fb" decodes to "a/b", which Starlette's router treats as two path
    segments, so it never matches a single {job_id} segment -- a 404, not a
    422, but still safe: no dependency or handler ever runs."""
    resp = client.get(
        "/api/v1/assessments/a%2Fb/uploads",
        params={"database_name": _VALID_DB},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# The two raw-graph-query endpoints: gated behind MODERNIZER_ENABLE_RAW_GRAPH_QUERY=1
# ---------------------------------------------------------------------------


@pytest.fixture
def raw_query_enabled(monkeypatch):
    monkeypatch.setenv("MODERNIZER_ENABLE_RAW_GRAPH_QUERY", "1")


@pytest.mark.parametrize("bad_job_id", _BAD_JOB_IDS)
def test_query_graph_rejects_invalid_job_id(bad_job_id: str, raw_query_enabled) -> None:
    resp = client.post(
        f"/api/v1/assessments/{bad_job_id}/graph/query",
        json={"cypher": "MATCH (n) RETURN n LIMIT 1"},
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422, resp.text
    assert "job_id" in str(resp.json())


def test_query_graph_accepts_valid_job_id(raw_query_enabled) -> None:
    resp = client.post(
        f"/api/v1/assessments/{_VALID_JOB_ID}/graph/query",
        json={"cypher": "MATCH (n) RETURN n LIMIT 1"},
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code != 422 or "job_id" not in str(resp.json())


@pytest.mark.parametrize("bad_job_id", _BAD_JOB_IDS)
def test_rebuild_graph_rejects_invalid_job_id(bad_job_id: str, raw_query_enabled) -> None:
    resp = client.post(
        f"/api/v1/assessments/{bad_job_id}/graph/rebuild",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422, resp.text
    assert "job_id" in str(resp.json())


def test_rebuild_graph_accepts_valid_job_id(raw_query_enabled) -> None:
    resp = client.post(
        f"/api/v1/assessments/{_VALID_JOB_ID}/graph/rebuild",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code != 422 or "job_id" not in str(resp.json())
