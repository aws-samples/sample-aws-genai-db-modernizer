"""src.api.routes.agent_interaction's agent path parameter had no shape of
its own: database_name and job_id are both validated against a character
class (#420/#505/#508), but "agent" wasn't, so "agent=.." resolved
_questions_path/_answers_path one level up -- out of the job's folder and
into its own database_name folder, still inside the artifact root but not a
path this protocol ever writes to. ``validated_agent`` closes that with an
explicit allowlist of the only agent folders the pause-for-customer-input
protocol is wired for: the per-engine schema design agents.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.routes.agent_interaction import _KNOWN_AGENT_FOLDERS, validated_agent

client = TestClient(app)


class TestValidatedAgentDirectly:
    @pytest.mark.parametrize("agent", sorted(_KNOWN_AGENT_FOLDERS))
    def test_accepts_every_known_agent_folder(self, agent: str) -> None:
        assert validated_agent(agent) == agent

    @pytest.mark.parametrize(
        "bad_agent",
        ["triage", "collector", "..", ".", "schema-neptune", "schema-dynamodb/../x", ""],
    )
    def test_rejects_anything_outside_the_allowlist(self, bad_agent: str) -> None:
        with pytest.raises(HTTPException) as exc_info:
            validated_agent(bad_agent)
        assert exc_info.value.status_code == 422
        assert "agent" in exc_info.value.detail


_VALID_JOB_ID = "job1"
_VALID_DB = "wordpress"


@pytest.fixture
def mock_store():
    from src.api.routes import agent_interaction

    store = MagicMock()
    store.exists.return_value = False
    agent_interaction.artifact_store = store
    yield store
    agent_interaction.artifact_store = None


def test_get_questions_rejects_an_agent_outside_the_allowlist(mock_store) -> None:
    resp = client.get(
        f"/api/v1/assessments/{_VALID_JOB_ID}/agents/triage/questions",
        params={"database_name": _VALID_DB},
    )
    assert resp.status_code == 422, resp.text
    assert "agent" in resp.json()["detail"]
    mock_store.exists.assert_not_called()


def test_get_questions_rejects_the_one_level_up_traversal(mock_store) -> None:
    """ "agent=.." resolves _questions_path to one level up from the job
    folder (still inside the artifact root, so not caught by a path-shape
    check alone) -- the allowlist is what actually closes this off."""
    resp = client.get(
        f"/api/v1/assessments/{_VALID_JOB_ID}/agents/%2e%2e/questions",
        params={"database_name": _VALID_DB},
    )
    assert resp.status_code == 422, resp.text
    assert "agent" in resp.json()["detail"]
    mock_store.exists.assert_not_called()


def test_get_questions_accepts_a_known_agent_folder(mock_store) -> None:
    resp = client.get(
        f"/api/v1/assessments/{_VALID_JOB_ID}/agents/schema-dynamodb/questions",
        params={"database_name": _VALID_DB},
    )
    # Not rejected for the agent: it 404s because no questions artifact
    # exists for this (mocked, empty) store.
    assert resp.status_code == 404
    assert "must be one of" not in str(resp.json())


def test_put_answers_rejects_an_agent_outside_the_allowlist(mock_store) -> None:
    resp = client.put(
        f"/api/v1/assessments/{_VALID_JOB_ID}/agents/triage/answers",
        params={"database_name": _VALID_DB},
        json={"answers": {}},
    )
    assert resp.status_code == 422, resp.text
    assert "agent" in resp.json()["detail"]
    mock_store.exists.assert_not_called()


def test_validate_single_answer_rejects_an_agent_outside_the_allowlist(mock_store) -> None:
    resp = client.post(
        f"/api/v1/assessments/{_VALID_JOB_ID}/agents/triage/validate-answer",
        params={"database_name": _VALID_DB},
        json={"question_id": "q1", "answer": "a"},
    )
    assert resp.status_code == 422, resp.text
    assert "agent" in resp.json()["detail"]
    mock_store.exists.assert_not_called()
