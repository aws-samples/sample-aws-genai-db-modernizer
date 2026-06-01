"""
Agent Interaction Contract Models

Data models for the agent interaction protocol (human-in-the-loop):
structured questions from agents, answer options, and customer answers.

When an agent encounters an ambiguity it cannot resolve deterministically,
it writes a questions artifact (AgentQuestions), exits with code 2, and
waits for the customer to answer. The customer's responses are captured
in an AgentAnswers artifact.

Version History:
- 1.0 (2026-04-01): Initial version — Phase 1A agent interaction models
"""

from datetime import datetime

from pydantic import BaseModel, Field


class QuestionOption(BaseModel):
    """A selectable option for an agent question."""

    id: str = Field(..., description="Unique option identifier (e.g., 'A', 'B', 'option_1')")
    description: str = Field(..., description="Human-readable description of this option")
    recommended: bool = Field(
        default=False,
        description="True if this option is the agent's recommended choice",
    )


class AgentQuestion(BaseModel):
    """A question an agent needs answered to continue execution."""

    question_id: str = Field(..., description="Unique question identifier within the agent run")
    category: str = Field(
        ...,
        description="Question category (e.g., 'partition_key_strategy', 'index_selection')",
    )
    table_id: str | None = Field(None, description="Table this question relates to, if applicable")
    question: str = Field(..., description="Human-readable question text")
    options: list[QuestionOption] | None = Field(
        None, description="Selectable options; None for free-text questions"
    )
    context: str | None = Field(None, description="Supporting data or rationale for the customer")
    default: str | None = Field(
        None, description="Recommended default answer (option ID or suggested text)"
    )


class AgentQuestions(BaseModel):
    """Questions artifact written by an agent that needs customer input.

    Written to ``{db}/{job}/{agent-name}/questions.json`` when the agent
    exits with code 2.
    """

    agent_type: str = Field(
        ..., description="Agent type that produced the questions (e.g., 'schema-dynamodb')"
    )
    target_engine: str | None = Field(
        None, description="Target engine the agent is designing for, if applicable"
    )
    partial_result_key: str | None = Field(
        None,
        description="Artifact key for the agent's partial output (e.g., 'partial_output.json')",
    )
    questions: list[AgentQuestion] = Field(
        ..., description="List of questions the agent needs answered"
    )
    timestamp: datetime = Field(..., description="ISO 8601 timestamp when questions were generated")


class AgentAnswers(BaseModel):
    """Answers artifact written by the customer or API.

    Written to ``{db}/{job}/{agent-name}/answers.json`` after the customer
    responds to agent questions.
    """

    agent_type: str = Field(..., description="Agent type the answers are for")
    answers: dict[str, str] = Field(
        ..., description="Mapping of question_id to answer (option ID or free text)"
    )
    answered_by: str | None = Field(None, description="Identifier of who provided the answers")
    timestamp: datetime = Field(..., description="ISO 8601 timestamp when answers were submitted")
