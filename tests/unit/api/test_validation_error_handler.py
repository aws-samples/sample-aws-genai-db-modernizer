"""src.api.main's RequestValidationError handler must never echo the
rejected value back in a 422 response.

FastAPI's default handler puts the rejected value in each error's "input"
field (and sometimes mirrors it into "ctx"). A pydantic field_validator's
own ValueError -- e.g. AssessmentRequest.database_name/job_id (#420/#505/
#508) -- deliberately never echoes the rejected value in its *message* so
it can't carry attacker-controlled text into a response, only for FastAPI's
default serialization to put it right back via "input". This is the
general-purpose fix: it strips "input" (and any "ctx" value that mirrors
it) from every validation error, not just those two fields, so a future
field_validator gets the same protection for free.
"""

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.routes import assessments

client = TestClient(app)

_ATTACKER_VALUE = "../evil<script>payload"


@pytest.fixture(autouse=True)
def mock_services():
    sfn = MagicMock()
    s3 = MagicMock()
    assessments.sfn_service = sfn
    assessments.s3_service = s3
    yield {"sfn": sfn, "s3": s3}
    assessments.sfn_service = None
    assessments.s3_service = None


def _assert_no_echo(resp, rejected_value: str) -> None:
    assert resp.status_code == 422
    body = resp.json()
    assert "input" not in str(body), body
    assert rejected_value not in str(body), body
    for error in body["detail"]:
        assert "input" not in error, error


def test_prepare_assessment_422_does_not_echo_bad_database_name() -> None:
    resp = client.post("/api/v1/assessments/prepare", json={"database_name": _ATTACKER_VALUE})
    _assert_no_echo(resp, _ATTACKER_VALUE)


def test_create_assessment_422_does_not_echo_bad_database_name(mock_services) -> None:
    resp = client.post(
        "/api/v1/assessments",
        json={
            "source_database_type": "mysql",
            "database_name": _ATTACKER_VALUE,
            "connection": {"host": "localhost", "port": 3306, "database": "test"},
        },
    )
    _assert_no_echo(resp, _ATTACKER_VALUE)


def test_still_422s_with_a_usable_message_for_a_missing_field() -> None:
    """Sanity check: the handler still produces a normal, useful 422 for an
    ordinary validation failure (a required field simply isn't present, so
    there's no "input" to begin with)."""
    resp = client.post("/api/v1/assessments/prepare", json={})
    assert resp.status_code == 422
    body = resp.json()
    assert body["detail"]
    assert body["detail"][0]["loc"] == ["body", "database_name"]
