"""
Tiered answer validation for agent interaction protocol.

Option-select answers are validated deterministically (no Bedrock call).
Free-text answers are validated via Bedrock to check the answer addresses
the question. If Bedrock is unavailable, or the model gives no verdict (a
safety refusal or an empty answer), the answer is accepted with a warning.

Requirements: 7.3
"""

from __future__ import annotations

import json
import logging

from src.agents.prompt_framing import SYSTEM_PROMPT_DATA_DIRECTIVE, frame_untrusted
from src.contracts.agent_interaction_models import AgentQuestion
from src.contracts.assignment_models import ValidationResult
from src.shared.bedrock_guardrail import (
    GuardrailConfigError,
    GuardrailIntervened,
    check_invoke_model_body,
    invoke_model_guardrail_kwargs,
    tag_guarded_input,
)
from src.shared.models import Role, invoke_model_body, resolve_model_id

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
    except GuardrailConfigError:
        raise  # a partial guardrail setting is never "Bedrock unavailable"
    except GuardrailIntervened as exc:
        # Not retried. The check only gates the answer, so it stays accepted,
        # with a warning that says why no verdict exists.
        logger.error("%s", exc)
        return ValidationResult(
            valid=True,
            warnings=[
                "A Bedrock guardrail blocked the free-text answer check "
                "(not retried) — answer accepted without semantic check"
            ],
        )
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
    """Call Bedrock (role LIGHT_CHECK) to check whether the answer addresses the question."""
    import boto3  # deferred import — only needed for free-text validation

    client = boto3.client("bedrock-runtime")
    # With a guardrail on, the customer's answer is tagged so the prompt-attack
    # filter evaluates it (InvokeModel only evaluates tagged content).
    guarded_answer, guardrail_body = tag_guarded_input(answer)
    # The question names customer tables and the answer is the customer's own
    # text: both are framed as data, not instructions (#1317).
    prompt = (
        "Does the answer in the block below adequately address the question in it?\n\n"
        + frame_untrusted(
            f"Question: {question.question}\nAnswer: {guarded_answer}",
            label="a question about the customer's database and the customer's answer",
        )
        + "\n\nRespond with only 'yes' or 'no'."
    )
    model_id = resolve_model_id(Role.LIGHT_CHECK)
    response = client.invoke_model(
        modelId=model_id,
        contentType="application/json",
        accept="application/json",
        **invoke_model_guardrail_kwargs(),
        body=json.dumps(
            {
                **invoke_model_body(
                    Role.LIGHT_CHECK, [{"role": "user", "content": prompt}], model_id=model_id
                ),
                "system": SYSTEM_PROMPT_DATA_DIRECTIVE,
                **guardrail_body,
            }
        ),
    )

    result = json.loads(response["body"].read())
    check_invoke_model_body(result, "free-text answer check")
    # Newer models may lead with a (possibly empty) thinking block.
    answer_text = next(
        (
            block.get("text", "")
            for block in result.get("content") or []
            if block.get("type") == "text"
        ),
        "",
    )
    if result.get("stop_reason") == "refusal" or not answer_text.strip():
        # A safety decline (HTTP 200, stop_reason "refusal", no text) says
        # nothing about the answer: treat it as "could not validate", like an
        # unavailable model, never as an inadequate answer.
        logger.warning(
            "Free-text validation of question %s got no verdict (stop_reason=%s); "
            "accepting answer with warning.",
            question.question_id,
            result.get("stop_reason"),
        )
        return ValidationResult(
            valid=True,
            warnings=[
                "Answer could not be validated (no model verdict) — accepted without semantic check"
            ],
        )
    if answer_text.strip().lower().startswith("yes"):
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
