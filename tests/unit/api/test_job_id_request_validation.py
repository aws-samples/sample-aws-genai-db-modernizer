"""AssessmentRequest.job_id must reject a malformed or path-traversing
value the same way database_name does (#420/#505), before it reaches
LocalExecutionService.start_execution's
``write_json(f"{database_name}/{job_id}/_meta.json", ...)`` (#508). job_id
is optional -- omitting it lets the server generate one, which is always
valid -- so this also checks that None is still accepted."""

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.routes import assessments

client = TestClient(app)

VALID_ASSESSMENT_REQUEST = {
    "source_database_type": "mysql",
    "database_name": "test_db",
    "connection": {
        "host": "localhost",
        "port": 3306,
        "database": "test",
        "secret_name": "app/db",  # pragma: allowlist secret
    },
}

_INVALID_JOB_IDS = ["../escape", "..", "a/b", "a b", "", "café", "a" * 129]


@pytest.fixture(autouse=True)
def mock_services():
    sfn = MagicMock()
    s3 = MagicMock()
    assessments.sfn_service = sfn
    assessments.s3_service = s3
    yield {"sfn": sfn, "s3": s3}
    assessments.sfn_service = None
    assessments.s3_service = None


@pytest.mark.parametrize("bad_job_id", _INVALID_JOB_IDS)
def test_create_assessment_rejects_invalid_job_id(bad_job_id: str, mock_services) -> None:
    request = {**VALID_ASSESSMENT_REQUEST, "job_id": bad_job_id}
    resp = client.post("/api/v1/assessments", json=request)
    assert resp.status_code == 422
    mock_services["sfn"].start_execution.assert_not_called()


def test_create_assessment_accepts_a_valid_job_id(mock_services) -> None:
    mock_services["sfn"].start_execution.return_value = {
        "execution_arn": "arn:test",
        "start_date": "2026-02-23T14:00:00Z",
    }
    request = {**VALID_ASSESSMENT_REQUEST, "job_id": "pre-created-id-123"}
    resp = client.post("/api/v1/assessments", json=request)
    assert resp.status_code == 202
    assert resp.json()["job_id"] == "pre-created-id-123"


def test_create_assessment_accepts_an_omitted_job_id(mock_services) -> None:
    """job_id is optional: the server generates one when it's absent, and
    that generated value is always valid, so this must not 422."""
    mock_services["sfn"].start_execution.return_value = {
        "execution_arn": "arn:test",
        "start_date": "2026-02-23T14:00:00Z",
    }
    resp = client.post("/api/v1/assessments", json=VALID_ASSESSMENT_REQUEST)
    assert resp.status_code == 202
