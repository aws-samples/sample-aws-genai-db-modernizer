"""
Agent Interaction Protocol — question/answer helpers and partial output management.

Enables any agent to pause execution, ask the customer questions, and resume
with answers. The agent writes partial state + questions to the artifact store,
exits with code 2, and gets re-launched with answers.

Artifact paths:
    {db}/{job}/{agent-name}/questions.json
    {db}/{job}/{agent-name}/answers.json
    {db}/{job}/{agent-name}/partial_output.json

Requirements: 7.1, 7.4
"""

from __future__ import annotations

from src.contracts.agent_interaction_models import AgentAnswers, AgentQuestions
from src.storage.artifact_store import ArtifactStore


class AgentNeedsInputError(Exception):
    """Raised by agents when they need customer input to continue.

    Caught by the entrypoint, which writes the questions artifact and
    exits with code 2.
    """


def _agent_artifact_path(db: str, job_id: str, agent_name: str, filename: str) -> str:
    """Build the artifact path for an agent-specific file."""
    return f"{db}/{job_id}/{agent_name}/{filename}"


def write_questions(
    store: ArtifactStore,
    db: str,
    job_id: str,
    agent_name: str,
    questions: AgentQuestions,
) -> None:
    """Write a questions artifact for an agent that needs customer input.

    The questions artifact is written to ``{db}/{job_id}/{agent_name}/questions.json``.
    """
    path = _agent_artifact_path(db, job_id, agent_name, "questions.json")
    store.write_json(path, questions.model_dump(mode="json"))


def read_answers(
    store: ArtifactStore,
    db: str,
    job_id: str,
    agent_name: str,
) -> AgentAnswers | None:
    """Read the answers artifact for an agent, if it exists.

    Returns ``None`` if no answers have been submitted yet.
    """
    path = _agent_artifact_path(db, job_id, agent_name, "answers.json")
    if not store.exists(path):
        return None
    data = store.read_json(path)
    return AgentAnswers.model_validate(data)


def write_partial_output(
    store: ArtifactStore,
    db: str,
    job_id: str,
    agent_name: str,
    data: dict,
) -> None:
    """Write a partial output artifact so the agent can resume from where it left off."""
    path = _agent_artifact_path(db, job_id, agent_name, "partial_output.json")
    store.write_json(path, data)


def read_partial_output(
    store: ArtifactStore,
    db: str,
    job_id: str,
    agent_name: str,
) -> dict | None:
    """Read the partial output artifact for an agent, if it exists.

    Returns ``None`` if no partial output has been written.
    """
    path = _agent_artifact_path(db, job_id, agent_name, "partial_output.json")
    if not store.exists(path):
        return None
    return store.read_json(path)
