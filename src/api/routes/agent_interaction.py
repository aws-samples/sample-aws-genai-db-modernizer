"""Agent interaction routes — questions, answers, and answer validation.

Requirements: 14.5, 14.6, 14.7
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.agents.answer_validation import validate_answer
from src.api.dependencies import database_name_param, job_id_param
from src.contracts.agent_interaction_models import AgentAnswers, AgentQuestions
from src.contracts.assignment_models import ValidationResult
from src.storage.artifact_store import ArtifactStore

router = APIRouter(prefix="/api/v1/assessments", tags=["agent-interaction"])

# Service injected by main.py at startup
artifact_store: ArtifactStore | None = None

#: The only agents wired for the pause-for-customer-input protocol
#: (AgentNeedsInputError / write_questions / read_answers in
#: src/agents/interaction.py) are the per-engine schema design agents --
#: see src/agents/schema_design/handler.py's ``agent_name = f"schema-{target_type}"``
#: and AgentQuestions.agent_type's own docstring example ("schema-dynamodb").
#: database_name and job_id are validated against a character class
#: (#420/#505/#508), but "agent" has no such shape of its own: unconstrained,
#: "agent=.." resolves _questions_path/_answers_path one level up (into the
#: job's own database_name folder) -- still inside the artifact root, so not
#: a traversal out of it, but still a real file at a path this protocol
#: never writes to. An explicit allowlist closes that off rather than
#: relying on path-shape validation alone.
_KNOWN_AGENT_FOLDERS = frozenset(
    f"schema-{engine}"
    for engine in (
        "dynamodb",
        "documentdb",
        "opensearch",
        "elasticache",
        "aurora_postgresql",
        "aurora_mysql",
    )
)


def validated_agent(agent: str) -> str:
    """FastAPI dependency: the ``agent`` path parameter, constrained to a
    known agent folder name."""
    if agent not in _KNOWN_AGENT_FOLDERS:
        raise HTTPException(
            status_code=422,
            detail=("agent must be one of: " + ", ".join(sorted(_KNOWN_AGENT_FOLDERS))),
        )
    return agent


def _require_store() -> ArtifactStore:
    if not artifact_store:
        raise HTTPException(status_code=503, detail="ArtifactStore not configured")
    return artifact_store


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class SubmitAnswersRequest(BaseModel):
    """PUT body for submitting answers to agent questions."""

    answers: dict[str, str] = Field(
        ..., description="Mapping of question_id to answer (option ID or free text)"
    )
    answered_by: str | None = Field(None, description="Identifier of who provided the answers")


class ValidateAnswerRequest(BaseModel):
    """POST body for validating a single answer."""

    question_id: str = Field(..., description="ID of the question being answered")
    answer: str = Field(..., description="The answer to validate")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _questions_path(db: str, job_id: str, agent: str) -> str:
    return f"{db}/{job_id}/{agent}/questions.json"


def _answers_path(db: str, job_id: str, agent: str) -> str:
    return f"{db}/{job_id}/{agent}/answers.json"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/{job_id}/agents/{agent}/questions", response_model=AgentQuestions)
async def get_questions(
    job_id: str = Depends(job_id_param),
    agent: str = Depends(validated_agent),
    database_name: str = Depends(database_name_param),
):
    """Read the questions artifact for an agent."""
    store = _require_store()
    path = _questions_path(database_name, job_id, agent)
    if not store.exists(path):
        raise HTTPException(status_code=404, detail="No questions artifact found for this agent")
    data = store.read_json(path)
    return AgentQuestions.model_validate(data)


@router.put("/{job_id}/agents/{agent}/answers")
async def put_answers(
    body: SubmitAnswersRequest,
    job_id: str = Depends(job_id_param),
    agent: str = Depends(validated_agent),
    database_name: str = Depends(database_name_param),
):
    """Write the answers artifact for an agent."""
    store = _require_store()

    # Verify questions exist so we know the agent type
    q_path = _questions_path(database_name, job_id, agent)
    if not store.exists(q_path):
        raise HTTPException(status_code=404, detail="No questions artifact found for this agent")

    questions_data = store.read_json(q_path)
    questions = AgentQuestions.model_validate(questions_data)

    answers = AgentAnswers(
        agent_type=questions.agent_type,
        answers=body.answers,
        answered_by=body.answered_by,
        timestamp=datetime.now(UTC),
    )

    path = _answers_path(database_name, job_id, agent)
    store.write_json(path, answers.model_dump(mode="json"))

    return {"job_id": job_id, "agent": agent, "status": "answers_saved"}


@router.post(
    "/{job_id}/agents/{agent}/validate-answer",
    response_model=ValidationResult,
)
async def validate_single_answer(
    body: ValidateAnswerRequest,
    job_id: str = Depends(job_id_param),
    agent: str = Depends(validated_agent),
    database_name: str = Depends(database_name_param),
):
    """Validate a single answer using tiered validation.

    Option-select questions are validated deterministically.
    Free-text questions are validated via Bedrock.
    """
    store = _require_store()

    # Read questions to find the specific question
    q_path = _questions_path(database_name, job_id, agent)
    if not store.exists(q_path):
        raise HTTPException(status_code=404, detail="No questions artifact found for this agent")

    questions_data = store.read_json(q_path)
    questions = AgentQuestions.model_validate(questions_data)

    # Find the question by ID
    question = next(
        (q for q in questions.questions if q.question_id == body.question_id),
        None,
    )
    if question is None:
        raise HTTPException(
            status_code=404,
            detail=f"Question {body.question_id} not found in agent questions",
        )

    return validate_answer(question, body.answer)
