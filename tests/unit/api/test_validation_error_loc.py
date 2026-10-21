"""#1303: each mode check of AssessmentRequest names the field to fix in its 422 `loc`.

A model_validator's ValueError has an empty field path, so FastAPI reported ``["body"]`` and a
client mapping errors to form fields could not highlight anything. The message text is unchanged.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.api.main import app
from src.api.models.requests import AssessmentRequest

client = TestClient(app)

_CONNECTION = {"host": "db.example.internal", "port": 3306, "database": "app"}


def _body(**overrides: object) -> dict:
    return {
        "source_database_type": "mysql",
        "database_name": "test_db",
        "collection_mode": "live",
        "connection": dict(_CONNECTION),
        **overrides,
    }


CASES = [
    pytest.param(
        _body(),
        ["body", "connection"],
        "live mode needs a secret reference: set connection.secret_arn or connection.secret_name",
        id="live-without-secret",
    ),
    pytest.param(
        _body(connection=None),
        ["body", "connection"],
        "connection is required for live and ddl modes",
        id="live-without-connection",
    ),
    pytest.param(
        _body(collection_mode="ddl", connection=None),
        ["body", "connection"],
        "connection is required for live and ddl modes",
        id="ddl-without-connection",
    ),
    pytest.param(
        _body(collection_mode="offline", connection=None),
        ["body", "offline_s3_key"],
        "offline_s3_key is required for offline mode",
        id="offline-without-key",
    ),
    pytest.param(
        _body(source_database_type="auto", collection_mode="ddl"),
        ["body", "collection_mode"],
        "needs collection_mode offline",
        id="detect-from-file-not-offline",
    ),
]


@pytest.mark.parametrize(("body", "loc", "message"), CASES)
def test_the_422_names_the_field_to_fix(body, loc, message) -> None:
    resp = client.post("/api/v1/assessments", json=body)

    assert resp.status_code == 422, resp.text
    details = resp.json()["detail"]
    assert [d["loc"] for d in details] == [loc]
    assert message in details[0]["msg"]
    assert details[0]["msg"].startswith("Value error, ")
    assert "input" not in details[0]


@pytest.mark.parametrize(("body", "loc", "message"), CASES)
def test_the_model_error_carries_the_same_loc(body, loc, message) -> None:
    with pytest.raises(ValidationError) as caught:
        AssessmentRequest(**body)

    assert [list(e["loc"]) for e in caught.value.errors()] == [loc[1:]]


@pytest.mark.passes_on_main  # what the change must keep accepting
def test_a_valid_offline_request_is_still_accepted() -> None:
    request = AssessmentRequest(
        **_body(collection_mode="offline", connection=None, offline_s3_key="db/job/up.json")
    )
    assert request.offline_s3_key == "db/job/up.json"


@pytest.mark.passes_on_main  # what the change must not move
def test_other_field_errors_keep_their_own_loc() -> None:
    resp = client.post("/api/v1/assessments", json=_body(database_name="../evil"))
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", "database_name"]
