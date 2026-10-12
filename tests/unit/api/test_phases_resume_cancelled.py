"""POST /{job_id}/resume refuses a cancelled job (#383, #518 round 1).

Cancelling a job blocks any further API-driven phase: the API only ever runs
a phase synchronously inside this one request, so by the time a resume call
is made there is nothing left running to interrupt from an earlier call --
what matters is refusing to start a *new* one.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from src.api.main import app
    from src.api.routes import phases

    mock_orch = MagicMock()
    phases.orchestrator = mock_orch
    phases.sfn_service = None
    yield TestClient(app), mock_orch
    phases.orchestrator = None
    phases.sfn_service = None


def test_resume_returns_409_when_job_was_cancelled(client):
    tc, mock_orch = client
    from src.api.routes import phases

    mock_sfn = MagicMock()
    mock_sfn.is_cancelled.return_value = True
    phases.sfn_service = mock_sfn

    resp = tc.post(
        "/api/v1/assessments/job-1/resume",
        json={"phase": "analysis"},
    )

    assert resp.status_code == 409
    mock_sfn.is_cancelled.assert_called_once_with("job-1")
    mock_orch.resume_lenient.assert_not_called()
    mock_orch.resume.assert_not_called()


def test_resume_proceeds_when_job_is_not_cancelled(client):
    tc, mock_orch = client
    from src.api.routes import phases

    mock_sfn = MagicMock()
    mock_sfn.is_cancelled.return_value = False
    phases.sfn_service = mock_sfn

    resp = tc.post(
        "/api/v1/assessments/job-1/resume",
        json={"phase": "analysis"},
    )

    assert resp.status_code == 200
    mock_sfn.is_cancelled.assert_called_once_with("job-1")
    mock_orch.resume_lenient.assert_called_once()


def test_resume_proceeds_when_sfn_service_not_configured(client):
    """No sfn_service wired (e.g. a test that never set it up) must not break
    the existing resume behavior -- the cancel check is best-effort."""
    tc, mock_orch = client

    resp = tc.post(
        "/api/v1/assessments/job-1/resume",
        json={"phase": "analysis"},
    )

    assert resp.status_code == 200
    mock_orch.resume_lenient.assert_called_once()
