"""Local API routes that take database_name (or filename, for delete_upload)
as a plain query or path parameter -- not through AssessmentRequest /
PrepareAssessmentRequest, which #420 already validates via a pydantic
field_validator -- must reject an invalid one with 422 before any artifact
is read, listed, or deleted (#505).

FastAPI resolves a route's Depends() dependencies while it resolves the
request, before the route body executes, so these tests don't need to
configure the backing services for the "rejects" cases: an invalid value
never reaches them. The "accepts" cases do configure the services, so a
valid value is proven not to be rejected by this check (it may still fail
later for an unrelated reason, e.g. a 404 or 503).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.routes import agent_interaction, assessments, assignments, schema_revisions

client = TestClient(app)

_BAD_DB_NAMES = ["../escape", "..", ".", "a/b", "a\\b", "a b", "", "café", "a" * 129]

# (method, path, json body or None)
_ROUTES: list[tuple[str, str, dict | None]] = [
    ("POST", "/api/v1/assessments/job1/uploads/confirm", None),
    ("GET", "/api/v1/assessments/job1/uploads", None),
    ("DELETE", "/api/v1/assessments/job1/uploads/collector-output.json", None),
    ("GET", "/api/v1/assessments/job1/agents/triage/questions", None),
    ("PUT", "/api/v1/assessments/job1/agents/triage/answers", {"answers": {}}),
    (
        "POST",
        "/api/v1/assessments/job1/agents/triage/validate-answer",
        {"question_id": "q1", "answer": "a"},
    ),
    ("GET", "/api/v1/assessments/job1/assignments", None),
    ("PUT", "/api/v1/assessments/job1/assignments", {}),
    ("GET", "/api/v1/assessments/job1/schema/dynamodb", None),
    ("GET", "/api/v1/assessments/job1/schema/dynamodb/versions", None),
    (
        "PUT",
        "/api/v1/assessments/job1/schema/dynamodb/revisions",
        {
            "base_version": 1,
            "pattern_modifications": [],
            "table_modifications": [],
            "new_patterns": [],
        },
    ),
    ("POST", "/api/v1/assessments/job1/schema/dynamodb/confirm", None),
    ("POST", "/api/v1/assessments/job1/schema/confirm-all", None),
]


@pytest.fixture(autouse=True)
def mock_services():
    """Point every router's module-level service globals at mocks so a
    *valid* request doesn't 503 before reaching the thing under test."""
    store = MagicMock()
    s3 = MagicMock()
    sfn = MagicMock()
    assessments.sfn_service = sfn
    assessments.s3_service = s3
    agent_interaction.artifact_store = store
    assignments.artifact_store = store
    schema_revisions.artifact_store = store
    yield {"store": store, "s3": s3, "sfn": sfn}
    assessments.sfn_service = None
    assessments.s3_service = None
    agent_interaction.artifact_store = None
    assignments.artifact_store = None
    schema_revisions.artifact_store = None


@pytest.mark.parametrize("method,path,body", _ROUTES)
@pytest.mark.parametrize("bad_name", _BAD_DB_NAMES)
def test_rejects_invalid_database_name(
    method: str, path: str, body: dict | None, bad_name: str, mock_services
) -> None:
    resp = client.request(method, path, params={"database_name": bad_name}, json=body)
    assert resp.status_code == 422, resp.text
    assert "database_name" in str(resp.json())
    # Nothing was read, listed, written, or deleted: an invalid database_name
    # never reaches the service layer at all.
    mock_services["store"].read_json.assert_not_called()
    mock_services["store"].write_json.assert_not_called()
    mock_services["store"].list_prefix.assert_not_called()
    mock_services["s3"].client.head_object.assert_not_called()
    mock_services["s3"].client.delete_object.assert_not_called()
    mock_services["s3"].client.list_objects_v2.assert_not_called()
    mock_services["sfn"].start_execution.assert_not_called()


@pytest.mark.parametrize("method,path,body", _ROUTES)
def test_accepts_valid_database_name(
    method: str, path: str, body: dict | None, mock_services
) -> None:
    mock_services["store"].list_prefix.return_value = []
    mock_services["store"].exists.return_value = False
    mock_services["s3"].client.head_object.side_effect = Exception("not found")
    mock_services["s3"].client.list_objects_v2.return_value = {"Contents": []}
    resp = client.request(method, path, params={"database_name": "wordpress"}, json=body)
    assert resp.status_code != 422 or "database_name" not in str(resp.json())


def test_rejects_missing_database_name() -> None:
    """A route that omits database_name entirely still 422s (ordinary
    FastAPI "field required" behavior, not this validator -- confirms the
    dependency doesn't make a required parameter optional)."""
    resp = client.get("/api/v1/assessments/job1/uploads")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# delete_upload's filename path parameter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename",
    [
        "%2e%2e",  # ".." -- a plain ".." in the URL is normalized away client-side
        # before it's even sent (see test below), so this is the form that
        # actually reaches the server and is decoded into the parameter.
        "a%5Cb",  # decodes to "a\\b"
    ],
)
def test_delete_upload_rejects_invalid_filename(filename: str, mock_services) -> None:
    resp = client.request(
        "DELETE",
        f"/api/v1/assessments/job1/uploads/{filename}",
        params={"database_name": "wordpress"},
    )
    assert resp.status_code == 422, resp.text
    assert "filename" in str(resp.json())
    mock_services["s3"].client.head_object.assert_not_called()
    mock_services["s3"].client.delete_object.assert_not_called()


def test_delete_upload_404s_rather_than_delete_on_an_embedded_separator(mock_services) -> None:
    """ "a%2Fb" decodes to "a/b", which Starlette's router treats as two path
    segments and so never matches the single {filename} segment -- a 404,
    not a 422, but still safe: the handler (and our dependency) never run,
    so nothing is deleted."""
    resp = client.request(
        "DELETE",
        "/api/v1/assessments/job1/uploads/a%2Fb",
        params={"database_name": "wordpress"},
    )
    assert resp.status_code == 404
    mock_services["s3"].client.head_object.assert_not_called()
    mock_services["s3"].client.delete_object.assert_not_called()


def test_delete_upload_rejects_percent_encoded_dot_dot(mock_services) -> None:
    """A literal ".." in the URL gets normalized away by well-behaved HTTP
    clients before the request is even sent, so this uses the
    percent-encoded form (%2e%2e) that reaches the server verbatim and is
    decoded into the filename parameter server-side -- the actual traversal
    vector a client-side normalizer doesn't protect against."""
    resp = client.request(
        "DELETE",
        "/api/v1/assessments/job1/uploads/%2e%2e",
        params={"database_name": "wordpress"},
    )
    assert resp.status_code == 422, resp.text
    assert "filename" in str(resp.json())
    mock_services["s3"].client.head_object.assert_not_called()
    mock_services["s3"].client.delete_object.assert_not_called()


def test_delete_upload_accepts_a_normal_filename(mock_services) -> None:
    mock_services["s3"].client.head_object.return_value = {"ContentLength": 10}
    resp = client.request(
        "DELETE",
        "/api/v1/assessments/job1/uploads/collector-output.json",
        params={"database_name": "wordpress"},
    )
    assert resp.status_code == 200
    mock_services["s3"].client.delete_object.assert_called_once()
