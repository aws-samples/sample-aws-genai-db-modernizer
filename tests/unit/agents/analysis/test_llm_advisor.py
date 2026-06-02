"""Unit tests for LLM advisor retry, fallback, and disabled behavior.

Tests cover:
- 3 consecutive failures → deterministic-only output (None), exactly 3 attempts, backoff applied
- Success on 2nd attempt → only 2 attempts, 1s backoff, LLM output present
- LLM disabled → no LLM call, advisor.enabled is False

Requirements: 6.5, 7.3
"""

from __future__ import annotations

from unittest.mock import patch

from src.tools.analysis.dynamodb_analysis_tools import (
    AggregateKeyDesign,
    DenormStrategy,
    LlmAdvisor,
    LlmAdvisorOutput,
)


def _make_valid_llm_output() -> LlmAdvisorOutput:
    """Create a valid LlmAdvisorOutput for mocking successful LLM calls."""
    return LlmAdvisorOutput(
        aggregate_recommendations=[
            AggregateKeyDesign(
                aggregate_id="agg-orders",
                partition_key="customer_id",
                sort_key="order_date",
                rationale="Customer-scoped orders with date ordering",
            )
        ],
        denormalization_strategies=[
            DenormStrategy(
                opportunity_id="denorm-1",
                strategy="embed child items",
                rationale="Bounded parent-child with low cardinality",
            )
        ],
    )


class TestLlmAdvisorRetryExhausted:
    """Test 3 consecutive failures → deterministic-only output."""

    def test_returns_none_after_three_failures(self):
        """After 3 failed attempts, advise() returns None."""
        advisor = LlmAdvisor(enabled=True)

        with patch.object(advisor, "_call_llm", side_effect=RuntimeError("LLM call failed")), patch(
            "time.sleep"
        ):
            result = advisor.advise(
                deterministic_results={},
                schema={},
                queries=[],
                aggregates=[],
                denorm_opportunities=[],
            )

        assert result is None

    def test_exactly_three_attempts_made(self):
        """Exactly 3 attempts are made before giving up."""
        advisor = LlmAdvisor(enabled=True)

        with patch.object(advisor, "_call_llm", side_effect=RuntimeError("LLM call failed")), patch(
            "time.sleep"
        ):
            advisor.advise(
                deterministic_results={},
                schema={},
                queries=[],
                aggregates=[],
                denorm_opportunities=[],
            )

        assert advisor.attempts_made == 3

    def test_exponential_backoff_applied(self):
        """Backoff delays of 1s and 2s are applied between retries."""
        advisor = LlmAdvisor(enabled=True)

        with patch.object(advisor, "_call_llm", side_effect=RuntimeError("LLM call failed")), patch(
            "time.sleep"
        ) as mock_sleep:
            advisor.advise(
                deterministic_results={},
                schema={},
                queries=[],
                aggregates=[],
                denorm_opportunities=[],
            )

        # Attempt 1: no sleep, Attempt 2: sleep(1.0), Attempt 3: sleep(2.0)
        assert mock_sleep.call_count == 2
        mock_sleep.assert_any_call(1.0)
        mock_sleep.assert_any_call(2.0)


class TestLlmAdvisorRetrySuccess:
    """Test success on 2nd attempt → only 2 attempts, 1s backoff."""

    def test_returns_output_on_second_attempt(self):
        """When first call fails and second succeeds, LLM output is returned."""
        advisor = LlmAdvisor(enabled=True)
        valid_output = _make_valid_llm_output()

        with patch.object(
            advisor,
            "_call_llm",
            side_effect=[RuntimeError("first call failed"), valid_output],
        ), patch("time.sleep"):
            result = advisor.advise(
                deterministic_results={},
                schema={},
                queries=[],
                aggregates=[],
                denorm_opportunities=[],
            )

        assert result is not None
        assert isinstance(result, LlmAdvisorOutput)
        assert len(result.aggregate_recommendations) == 1
        assert result.aggregate_recommendations[0].aggregate_id == "agg-orders"

    def test_only_two_attempts_on_second_success(self):
        """Only 2 attempts are made when the second succeeds."""
        advisor = LlmAdvisor(enabled=True)
        valid_output = _make_valid_llm_output()

        with patch.object(
            advisor,
            "_call_llm",
            side_effect=[RuntimeError("first call failed"), valid_output],
        ), patch("time.sleep"):
            advisor.advise(
                deterministic_results={},
                schema={},
                queries=[],
                aggregates=[],
                denorm_opportunities=[],
            )

        assert advisor.attempts_made == 2

    def test_one_second_backoff_before_second_attempt(self):
        """1s backoff is applied before the second attempt."""
        advisor = LlmAdvisor(enabled=True)
        valid_output = _make_valid_llm_output()

        with patch.object(
            advisor,
            "_call_llm",
            side_effect=[RuntimeError("first call failed"), valid_output],
        ), patch("time.sleep") as mock_sleep:
            advisor.advise(
                deterministic_results={},
                schema={},
                queries=[],
                aggregates=[],
                denorm_opportunities=[],
            )

        mock_sleep.assert_called_once_with(1.0)


class TestLlmAdvisorDisabled:
    """Test LLM disabled → no LLM call."""

    def test_returns_none_when_disabled(self):
        """advise() returns None immediately when disabled."""
        advisor = LlmAdvisor(enabled=False)
        result = advisor.advise(
            deterministic_results={},
            schema={},
            queries=[],
            aggregates=[],
            denorm_opportunities=[],
        )
        assert result is None

    def test_no_attempts_when_disabled(self):
        """No attempts are made when the advisor is disabled."""
        advisor = LlmAdvisor(enabled=False)
        advisor.advise(
            deterministic_results={},
            schema={},
            queries=[],
            aggregates=[],
            denorm_opportunities=[],
        )
        assert advisor.attempts_made == 0

    def test_enabled_true_by_default(self):
        """LlmAdvisor defaults to enabled (ENABLE_LLM_ADVISOR not set)."""
        import os

        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("ENABLE_LLM_ADVISOR", None)
            advisor = LlmAdvisor()
            assert advisor.enabled is True

    def test_enabled_from_env_var(self):
        """LlmAdvisor reads ENABLE_LLM_ADVISOR env var."""
        with patch.dict("os.environ", {"ENABLE_LLM_ADVISOR": "false"}):
            advisor = LlmAdvisor()
            assert advisor.enabled is False

    def test_enabled_from_constructor_overrides_env(self):
        """Constructor parameter overrides env var."""
        with patch.dict("os.environ", {"ENABLE_LLM_ADVISOR": "true"}):
            advisor = LlmAdvisor(enabled=False)
            assert advisor.enabled is False


class TestLlmAdvisorFirstAttemptSuccess:
    """Test immediate success on first attempt."""

    def test_returns_output_on_first_attempt(self):
        """When first call succeeds, output is returned with 1 attempt."""
        advisor = LlmAdvisor(enabled=True)
        valid_output = _make_valid_llm_output()

        with patch.object(advisor, "_call_llm", return_value=valid_output), patch(
            "time.sleep"
        ) as mock_sleep:
            result = advisor.advise(
                deterministic_results={},
                schema={},
                queries=[],
                aggregates=[],
                denorm_opportunities=[],
            )

        assert result is not None
        assert advisor.attempts_made == 1
        mock_sleep.assert_not_called()
