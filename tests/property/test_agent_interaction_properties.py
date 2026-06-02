"""
Property-based tests for the Agent Interaction Protocol.

Tests correctness properties from the design document:
- Property 12: Agent Questions Completeness — write_questions produces a valid
  artifact with ≥1 question.
- Property 13: Agent Answer Coverage — for any set of questions, answers must
  cover all question_ids.
- Property 18: Tiered Answer Validation — options present → deterministic
  validation (no Bedrock); options absent → Bedrock validation.

**Validates: Requirements 7.1, 7.3, 7.4, 7.5**
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from unittest.mock import patch

from hypothesis import given
from hypothesis import strategies as st

from src.agents.answer_validation import validate_answer
from src.agents.interaction import (
    AgentNeedsInputError,
    read_answers,
    read_partial_output,
    write_partial_output,
    write_questions,
)
from src.contracts.agent_interaction_models import (
    AgentAnswers,
    AgentQuestion,
    AgentQuestions,
    QuestionOption,
)
from src.storage.local_store import LocalArtifactStore

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_safe_id = st.from_regex(r"[a-z][a-z0-9_]{0,14}", fullmatch=True)
_safe_text = st.text(
    alphabet=st.characters(categories=("L", "N", "P", "Z"), exclude_characters="\x00"),
    min_size=1,
    max_size=60,
)

_question_option = st.builds(
    QuestionOption,
    id=_safe_id,
    description=_safe_text,
    recommended=st.booleans(),
)

_question_with_options = st.builds(
    AgentQuestion,
    question_id=_safe_id,
    category=_safe_id,
    table_id=st.none(),
    question=_safe_text,
    options=st.lists(_question_option, min_size=1, max_size=5).filter(
        lambda opts: len({o.id for o in opts}) == len(opts)  # unique IDs
    ),
    context=st.none(),
    default=st.none(),
)

_question_free_text = st.builds(
    AgentQuestion,
    question_id=_safe_id,
    category=_safe_id,
    table_id=st.none(),
    question=_safe_text,
    options=st.none(),
    context=st.none(),
    default=st.none(),
)

_agent_question = st.one_of(_question_with_options, _question_free_text)

_agent_questions_artifact = st.builds(
    AgentQuestions,
    agent_type=_safe_id,
    target_engine=st.one_of(st.none(), _safe_id),
    partial_result_key=st.none(),
    questions=st.lists(_agent_question, min_size=1, max_size=5).filter(
        lambda qs: len({q.question_id for q in qs}) == len(qs)  # unique question IDs
    ),
    timestamp=st.just(datetime.now(tz=UTC)),
)

_db_name = _safe_id
_job_id = _safe_id
_agent_name = _safe_id


# ---------------------------------------------------------------------------
# Property 12: Agent Questions Completeness
# ---------------------------------------------------------------------------


class TestAgentQuestionsCompleteness:
    """**Validates: Requirements 7.1, 7.5**

    Property 12: write_questions produces a valid artifact with ≥1 question.
    """

    @given(
        questions=_agent_questions_artifact,
        db=_db_name,
        job_id=_job_id,
        agent_name=_agent_name,
    )
    def test_write_questions_produces_artifact_with_at_least_one_question(
        self,
        questions: AgentQuestions,
        db: str,
        job_id: str,
        agent_name: str,
    ) -> None:
        """write_questions writes an artifact that can be read back and has ≥1 question."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LocalArtifactStore(base_dir=tmpdir)
            write_questions(store, db, job_id, agent_name, questions)

            # Artifact must exist
            path = f"{db}/{job_id}/{agent_name}/questions.json"
            assert store.exists(path)

            # Read back and validate
            raw = store.read_json(path)
            restored = AgentQuestions.model_validate(raw)
            assert len(restored.questions) >= 1

    @given(
        questions=_agent_questions_artifact,
        db=_db_name,
        job_id=_job_id,
        agent_name=_agent_name,
    )
    def test_write_questions_preserves_all_question_ids(
        self,
        questions: AgentQuestions,
        db: str,
        job_id: str,
        agent_name: str,
    ) -> None:
        """All question IDs from the input are preserved in the written artifact."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LocalArtifactStore(base_dir=tmpdir)
            write_questions(store, db, job_id, agent_name, questions)

            raw = store.read_json(f"{db}/{job_id}/{agent_name}/questions.json")
            restored = AgentQuestions.model_validate(raw)
            original_ids = {q.question_id for q in questions.questions}
            restored_ids = {q.question_id for q in restored.questions}
            assert original_ids == restored_ids

    def test_agent_needs_input_error_is_exception(self) -> None:
        """AgentNeedsInputError is a proper exception class."""
        err = AgentNeedsInputError("test")
        assert isinstance(err, Exception)
        assert str(err) == "test"


# ---------------------------------------------------------------------------
# Property 13: Agent Answer Coverage
# ---------------------------------------------------------------------------


class TestAgentAnswerCoverage:
    """**Validates: Requirements 7.1, 7.4**

    Property 13: For any set of questions, answers must cover all question_ids.
    """

    @given(
        questions=_agent_questions_artifact,
        db=_db_name,
        job_id=_job_id,
        agent_name=_agent_name,
    )
    def test_answers_covering_all_question_ids_round_trip(
        self,
        questions: AgentQuestions,
        db: str,
        job_id: str,
        agent_name: str,
    ) -> None:
        """Answers that cover all question_ids can be written and read back."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LocalArtifactStore(base_dir=tmpdir)

            # Build answers covering every question
            answers_dict = {}
            for q in questions.questions:
                if q.options:
                    answers_dict[q.question_id] = q.options[0].id
                else:
                    answers_dict[q.question_id] = "some free text answer"

            answers = AgentAnswers(
                agent_type=questions.agent_type,
                answers=answers_dict,
                answered_by="test-user",
                timestamp=datetime.now(tz=UTC),
            )

            # Write answers
            path = f"{db}/{job_id}/{agent_name}/answers.json"
            store.write_json(path, answers.model_dump(mode="json"))

            # Read back
            restored = read_answers(store, db, job_id, agent_name)
            assert restored is not None

            # All question IDs must be covered
            question_ids = {q.question_id for q in questions.questions}
            assert question_ids <= set(restored.answers.keys())

    @given(db=_db_name, job_id=_job_id, agent_name=_agent_name)
    def test_read_answers_returns_none_when_missing(
        self, db: str, job_id: str, agent_name: str
    ) -> None:
        """read_answers returns None when no answers artifact exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LocalArtifactStore(base_dir=tmpdir)
            assert read_answers(store, db, job_id, agent_name) is None


# ---------------------------------------------------------------------------
# Property 18: Tiered Answer Validation
# ---------------------------------------------------------------------------


class TestTieredAnswerValidation:
    """**Validates: Requirements 7.3, 7.5**

    Property 18: options present → deterministic validation (no Bedrock);
    options absent → Bedrock validation.
    """

    @given(question=_question_with_options)
    def test_option_select_valid_answer_accepted(self, question: AgentQuestion) -> None:
        """Selecting a valid option ID is accepted deterministically."""
        valid_id = question.options[0].id  # type: ignore[index]
        result = validate_answer(question, valid_id)
        assert result.valid is True
        assert result.errors == []

    @given(question=_question_with_options)
    def test_option_select_invalid_answer_rejected(self, question: AgentQuestion) -> None:
        """Selecting an invalid option ID is rejected deterministically."""
        invalid_id = "DEFINITELY_NOT_A_VALID_OPTION_ID_999"
        result = validate_answer(question, invalid_id)
        assert result.valid is False
        assert len(result.errors) > 0

    @given(question=_question_with_options)
    def test_option_select_does_not_call_bedrock(self, question: AgentQuestion) -> None:
        """Option-select validation never calls Bedrock."""
        valid_id = question.options[0].id  # type: ignore[index]
        with patch("src.agents.answer_validation._bedrock_validate") as mock_bedrock:
            validate_answer(question, valid_id)
            mock_bedrock.assert_not_called()

    @given(question=_question_free_text, answer=_safe_text)
    def test_free_text_calls_bedrock(self, question: AgentQuestion, answer: str) -> None:
        """Free-text validation calls Bedrock (or falls back gracefully)."""
        with patch("src.agents.answer_validation._bedrock_validate") as mock_bedrock:
            from src.contracts.assignment_models import ValidationResult

            mock_bedrock.return_value = ValidationResult(valid=True)
            result = validate_answer(question, answer)
            mock_bedrock.assert_called_once_with(question, answer)
            assert result.valid is True

    @given(question=_question_free_text, answer=_safe_text)
    def test_free_text_fallback_on_bedrock_failure(
        self, question: AgentQuestion, answer: str
    ) -> None:
        """When Bedrock is unavailable, free-text answers are accepted with a warning."""
        with patch(
            "src.agents.answer_validation._bedrock_validate",
            side_effect=Exception("Bedrock unavailable"),
        ):
            result = validate_answer(question, answer)
            assert result.valid is True
            assert len(result.warnings) > 0


# ---------------------------------------------------------------------------
# Partial output round-trip
# ---------------------------------------------------------------------------


class TestPartialOutputRoundTrip:
    """Validates partial output write/read helpers (Requirements 7.1, 7.4)."""

    @given(
        db=_db_name,
        job_id=_job_id,
        agent_name=_agent_name,
        data=st.dictionaries(
            st.text(min_size=1, max_size=10),
            st.one_of(st.integers(), st.text(max_size=20), st.booleans()),
            min_size=1,
            max_size=5,
        ),
    )
    def test_write_then_read_partial_output(
        self, db: str, job_id: str, agent_name: str, data: dict
    ) -> None:
        """write_partial_output followed by read_partial_output returns identical data."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LocalArtifactStore(base_dir=tmpdir)
            write_partial_output(store, db, job_id, agent_name, data)
            result = read_partial_output(store, db, job_id, agent_name)
            assert result == data

    @given(db=_db_name, job_id=_job_id, agent_name=_agent_name)
    def test_read_partial_output_returns_none_when_missing(
        self, db: str, job_id: str, agent_name: str
    ) -> None:
        """read_partial_output returns None when no partial output exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LocalArtifactStore(base_dir=tmpdir)
            assert read_partial_output(store, db, job_id, agent_name) is None
