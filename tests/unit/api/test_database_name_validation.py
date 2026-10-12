"""The local API's assessment request models must reject a malformed or
path-traversing database_name before anything is written, the same as the
AWS Transform invocation path (#418) and the scripts/run_*.py CLI entry
points (#420). This exercises src.api.models.requests.AssessmentRequest and
PrepareAssessmentRequest, which both delegate to
src.shared.database_name.validate_database_name."""

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.routes import assessments

client = TestClient(app)

VALID_ASSESSMENT_REQUEST = {
    "source_database_type": "mysql",
    "database_name": "test_db",
    "connection": {"host": "localhost", "port": 3306, "database": "test"},
}

_INVALID_NAMES = ["../escape", "..", "a/b", "a b", "", "café", "a" * 129]


@pytest.fixture(autouse=True)
def mock_services():
    sfn = MagicMock()
    s3 = MagicMock()
    assessments.sfn_service = sfn
    assessments.s3_service = s3
    yield {"sfn": sfn, "s3": s3}
    assessments.sfn_service = None
    assessments.s3_service = None


@pytest.mark.parametrize("bad_name", _INVALID_NAMES)
def test_create_assessment_rejects_invalid_database_name(bad_name: str, mock_services) -> None:
    request = {**VALID_ASSESSMENT_REQUEST, "database_name": bad_name}
    resp = client.post("/api/v1/assessments", json=request)
    assert resp.status_code == 422
    mock_services["sfn"].start_execution.assert_not_called()


def test_create_assessment_accepts_valid_database_name(mock_services) -> None:
    mock_services["sfn"].start_execution.return_value = {
        "execution_arn": "arn:test",
        "start_date": "2026-02-23T14:00:00Z",
    }
    resp = client.post("/api/v1/assessments", json=VALID_ASSESSMENT_REQUEST)
    assert resp.status_code == 202


@pytest.mark.parametrize("bad_name", _INVALID_NAMES)
def test_prepare_assessment_rejects_invalid_database_name(bad_name: str, mock_services) -> None:
    resp = client.post("/api/v1/assessments/prepare", json={"database_name": bad_name})
    assert resp.status_code == 422
    mock_services["s3"].client.generate_presigned_url.assert_not_called()


def test_prepare_assessment_accepts_valid_database_name(mock_services) -> None:
    mock_services["s3"].client.generate_presigned_url.return_value = (
        "https://s3.amazonaws.com/presigned"
    )
    mock_services["s3"].bucket = "test-bucket"
    resp = client.post("/api/v1/assessments/prepare", json={"database_name": "test_db"})
    assert resp.status_code == 201
