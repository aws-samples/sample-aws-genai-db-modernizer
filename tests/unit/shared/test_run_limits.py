# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Opt-in per-run ceilings on tokens, model calls and wall-clock time (#145).

Off by default (no default cap, as decided on #384). Each ceiling is its own
environment variable; a bad value fails fast, and an exceeded ceiling names the
ceiling, its limit and the observed figure. No test calls a model: calls are
reported through ``record_usage``, the hook the model wrapper uses.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from src.shared import model_usage
from src.shared.model_usage import LEDGER, record_usage
from src.shared.run_limits import (
    ENV_MAX_RUN_MODEL_CALLS,
    ENV_MAX_RUN_SECONDS,
    ENV_MAX_RUN_TOKENS,
    RunLimitConfigError,
    RunLimitExceeded,
    RunLimits,
)

OPUS = "us.anthropic.claude-opus-5-5"
ALL = (ENV_MAX_RUN_TOKENS, ENV_MAX_RUN_MODEL_CALLS, ENV_MAX_RUN_SECONDS)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> Iterator[_Clock]:
    fake = _Clock()
    monkeypatch.setattr(model_usage, "_clock", fake)
    LEDGER.reset()
    yield fake
    LEDGER.reset()


@pytest.fixture(autouse=True)
def _no_ceiling_env(monkeypatch) -> None:
    for name in ALL:
        monkeypatch.delenv(name, raising=False)


def test_the_names_follow_the_ingestion_limits() -> None:
    assert ALL == (
        "MODERNIZER_MAX_RUN_TOKENS",
        "MODERNIZER_MAX_RUN_MODEL_CALLS",
        "MODERNIZER_MAX_RUN_SECONDS",
    )


def test_no_ceiling_is_set_by_default(clock) -> None:
    limits = RunLimits.from_env()
    assert limits == RunLimits(tokens=None, model_calls=None, seconds=None)
    assert not limits.any_set
    for _ in range(50):
        record_usage(OPUS, {"inputTokens": 10_000_000})
    clock.now += 10**6
    limits.check(LEDGER, "schema_design")  # never raises
    assert "ceiling_exceeded" not in LEDGER.snapshot()


def test_an_empty_value_is_no_ceiling(monkeypatch) -> None:
    monkeypatch.setenv(ENV_MAX_RUN_TOKENS, "")
    assert RunLimits.from_env().tokens is None


def test_each_variable_sets_its_own_ceiling(monkeypatch) -> None:
    monkeypatch.setenv(ENV_MAX_RUN_TOKENS, "1000000")
    monkeypatch.setenv(ENV_MAX_RUN_MODEL_CALLS, " 40 ")
    monkeypatch.setenv(ENV_MAX_RUN_SECONDS, "1800.5")
    assert RunLimits.from_env() == RunLimits(tokens=1_000_000, model_calls=40, seconds=1800.5)


@pytest.mark.parametrize("name", ALL)
@pytest.mark.parametrize("raw", ["abc", "0", "-5", "nan", "inf", "1e"])
def test_a_bad_value_fails_fast_naming_the_variable(monkeypatch, name: str, raw: str) -> None:
    monkeypatch.setenv(name, raw)
    with pytest.raises(RunLimitConfigError) as raised:
        RunLimits.from_env()
    message = str(raised.value)
    assert name in message and repr(raw) in message
    assert "positive" in message and "unset" in message
    assert isinstance(raised.value, ValueError)


@pytest.mark.parametrize("name", [ENV_MAX_RUN_TOKENS, ENV_MAX_RUN_MODEL_CALLS])
def test_a_count_ceiling_must_be_a_whole_number(monkeypatch, name: str) -> None:
    monkeypatch.setenv(name, "2.5")
    with pytest.raises(RunLimitConfigError, match="whole number"):
        RunLimits.from_env()


def test_tokens_count_every_counter_a_call_reports(clock) -> None:
    """Input, output, cache read and cache write: exactly at the limit passes."""
    record_usage(
        OPUS,
        {
            "inputTokens": 100,
            "outputTokens": 20,
            "cacheReadInputTokens": 300,
            "cacheWriteInputTokens": 80,
        },
    )
    RunLimits(tokens=500).check(LEDGER, "reality_check")
    with pytest.raises(RunLimitExceeded):
        RunLimits(tokens=499).check(LEDGER, "reality_check")


def test_just_over_the_token_ceiling_names_the_ceiling_the_limit_and_the_figure(clock) -> None:
    record_usage(OPUS, {"inputTokens": 1_000_001})
    with pytest.raises(RunLimitExceeded) as raised:
        RunLimits(tokens=1_000_000).check(LEDGER, "schema_design")
    exc = raised.value
    assert (exc.ceiling, exc.limit, exc.observed, exc.phase) == (
        "tokens",
        1_000_000,
        1_000_001,
        "schema_design",
    )
    message = str(exc)
    assert ENV_MAX_RUN_TOKENS in message
    assert "1,000,001 tokens" in message and "1,000,000" in message
    assert "in the schema_design phase" in message
    # The run's record says why it stopped.
    assert LEDGER.snapshot()["ceiling_exceeded"] == {
        "ceiling": "tokens",
        "env_var": ENV_MAX_RUN_TOKENS,
        "limit": 1_000_000,
        "observed": 1_000_001,
        "phase": "schema_design",
    }


def test_the_model_call_ceiling(clock) -> None:
    for _ in range(3):
        record_usage(OPUS, {"inputTokens": 1})
    RunLimits(model_calls=3).check(LEDGER, "synthesis")  # at the limit
    with pytest.raises(RunLimitExceeded) as raised:
        RunLimits(model_calls=2).check(LEDGER, "synthesis")
    assert (raised.value.ceiling, raised.value.limit, raised.value.observed) == (
        "model_calls",
        2,
        3,
    )
    assert ENV_MAX_RUN_MODEL_CALLS in str(raised.value)
    assert "3 model calls" in str(raised.value)


def test_the_wall_clock_ceiling_counts_with_no_model_call(clock) -> None:
    clock.now += 30.0
    RunLimits(seconds=30).check(LEDGER, "analysis")  # at the limit
    clock.now += 0.5
    with pytest.raises(RunLimitExceeded) as raised:
        RunLimits(seconds=30).check(LEDGER, "analysis")
    assert (raised.value.ceiling, raised.value.limit, raised.value.observed) == (
        "seconds",
        30,
        30.5,
    )
    assert ENV_MAX_RUN_SECONDS in str(raised.value)
    assert "30.5 seconds" in str(raised.value)


def test_a_ceiling_not_set_is_never_checked(clock) -> None:
    """Only the ceilings set apply: a token ceiling ignores calls and time."""
    for _ in range(100):
        record_usage(OPUS, {"inputTokens": 1})
    clock.now += 10**6
    RunLimits(tokens=100).check(LEDGER, "schema_design")
