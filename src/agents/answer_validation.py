"""
Tiered answer validation for agent interaction protocol.

Option-select answers are validated deterministically (no Bedrock call).
Free-text answers are validated via Bedrock to check the answer addresses
the question. If Bedrock is unavailable, the answer is accepted with a warning.

Requirements: 7.3
"""

from __future__ import annotations

import logging

from src.contracts.agent_interaction_models import AgentQuestion
from src.contracts.assignment_models import ValidationResult

logger = logging.getLogger(__name__)


def _validate_option_select(question: AgentQuestion, answer: str) -> ValidationResult:
    """Deterministic validation: answer must be one of the valid option IDs."""
    valid_ids = {opt.id for opt in question.options}  # type: ignore[union-attr]
    if answer in valid_ids:
        return ValidationResult(valid=True)
    return ValidationResult(
        valid=False,
        errors=[f"Answer must be one of: {sorted(valid_ids)}"],
    )


def _validate_free_text(question: AgentQuestion, answer: str) -> ValidationResult:
    """Validate free-text answer via Bedrock.

    Falls back to accepting the answer with a warning if Bedrock is
    unavailable or raises an exception.
    """
    try:
        return _bedrock_validate(question, answer)
    except Exception:
        logger.warning(
            "Bedrock unavailable for free-text validation of question %s; "
            "accepting answer with warning.",
            question.question_id,
            exc_info=True,
        )
        return ValidationResult(
            valid=True,
            warnings=["Bedrock validation unavailable — answer accepted without semantic check"],
        )


def _bedrock_validate(question: AgentQuestion, answer: str) -> ValidationResult:
    """Call Bedrock to check whether the answer addresses the question."""
    import boto3  # deferred import — only needed for free-text validation

    client = boto3.client("bedrock-runtime")
    prompt = (
        f"Does the following answer adequately address the question?\n\n"
        f"Question: {question.question}\n"
        f"Answer: {answer}\n\n"
        f"Respond with only 'yes' or 'no'."
    )
    response = client.invoke_model(
        modelId="anthropic.claude-3-haiku-20240307-v1:0",
        contentType="application/json",
        accept="application/json",
        body=__import__("json").dumps(
            {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": prompt}],
            }
        ),
    )
    import json

    result = json.loads(response["body"].read())
    answer_text = result["content"][0]["text"].strip().lower()
    if answer_text.startswith("yes"):
        return ValidationResult(valid=True)
    return ValidationResult(
        valid=False,
        errors=["Answer does not adequately address the question"],
    )


def validate_answer(question: AgentQuestion, answer: str) -> ValidationResult:
    """Validate an answer using the tiered approach.

    - Option-select (question.options is not None): deterministic check
    - Free-text (question.options is None): Bedrock validation with fallback
    """
    if question.options:
        return _validate_option_select(question, answer)
    return _validate_free_text(question, answer)
