"""Retry/throttle resilience for the shared schema designer loop.

Under a small shared Bedrock quota the group workers get throttled; a throttle
storm must be waited out (large budget, backoff + jitter) while a genuine error
(e.g. an unparseable design) still fails fast on the small regular budget.
"""

from __future__ import annotations

import types
from collections.abc import Callable
from unittest.mock import patch

import pytest
from pydantic import BaseModel

from src.tools.schema import base_schema_agent as bsa


class _Out(BaseModel):
    ok: bool = True


def _designer_result(out: _Out) -> types.SimpleNamespace:
    return types.SimpleNamespace(structured_output=out)


def _stub_runner(designer: Callable[[str], object]) -> types.SimpleNamespace:
    # A stand-in `self`: _invoke_designer only touches these three attributes.
    # It is passed to the unbound method (the type-ignore covers the stub-as-self).
    return types.SimpleNamespace(target_type="dynamodb", output_model=_Out, designer=designer)


class TestIsThrottleError:
    @pytest.mark.parametrize(
        "exc",
        [
            Exception("ThrottlingException: Rate exceeded"),
            Exception("An error occurred (ServiceUnavailableException)"),
            Exception("Read timed out."),
            Exception("Too Many Requests"),
            Exception("HTTP 429"),
            Exception("503 Service Unavailable"),
        ],
    )
    def test_transient_markers_are_throttle(self, exc: Exception) -> None:
        assert bsa._is_throttle_error(exc) is True

    @pytest.mark.parametrize(
        "exc",
        [ValueError("invalid design"), KeyError("missing field"), TypeError("bad type")],
    )
    def test_genuine_errors_are_not_throttle(self, exc: Exception) -> None:
        assert bsa._is_throttle_error(exc) is False

    def test_botocore_error_code_is_detected(self) -> None:
        exc = Exception("opaque")
        exc.response = {"Error": {"Code": "ThrottlingException"}}  # type: ignore[attr-defined]
        assert bsa._is_throttle_error(exc) is True


class TestThrottleTuning:
    def test_default_throttle_retries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SCHEMA_THROTTLE_MAX_RETRIES", raising=False)
        assert bsa._throttle_max_retries() == 8

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SCHEMA_THROTTLE_MAX_RETRIES", "20")
        assert bsa._throttle_max_retries() == 20

    def test_invalid_env_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SCHEMA_THROTTLE_MAX_RETRIES", "nope")
        assert bsa._throttle_max_retries() == 8

    def test_backoff_grows_and_is_capped(self) -> None:
        b1 = bsa._throttle_backoff(1)
        b3 = bsa._throttle_backoff(3)
        assert b1 >= bsa._THROTTLE_BASE_BACKOFF_SECONDS
        assert b3 > b1
        # Cap (+ up to 25% jitter) bounds even a very high failure count.
        huge = bsa._throttle_backoff(50)
        assert huge <= bsa._THROTTLE_MAX_BACKOFF_SECONDS * 1.25


class TestInvokeDesignerRetryBudgets:
    def test_throttling_is_retried_beyond_regular_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Five throttles (more than MAX_DESIGNER_RETRIES=3) then success: the old
        # 3-strike loop would have failed; the throttle budget must carry it.
        monkeypatch.delenv("SCHEMA_THROTTLE_MAX_RETRIES", raising=False)
        calls = {"n": 0}

        def designer(_prompt):
            calls["n"] += 1
            if calls["n"] <= 5:
                raise Exception("ThrottlingException: Rate exceeded")
            return _designer_result(_Out())

        with patch.object(bsa.time, "sleep"):
            out: object = bsa.SchemaDesignRunner._invoke_designer(_stub_runner(designer), "p")  # type: ignore[arg-type]

        assert isinstance(out, _Out)
        assert calls["n"] == 6

    def test_genuine_error_fails_fast_on_regular_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A non-throttle error must not consume the large throttle budget: exactly
        # MAX_DESIGNER_RETRIES attempts, then raise (no previous_output to salvage).
        calls = {"n": 0}

        def designer(_prompt):
            calls["n"] += 1
            raise ValueError("unparseable design")

        with patch.object(bsa.time, "sleep"), pytest.raises(RuntimeError):
            bsa.SchemaDesignRunner._invoke_designer(_stub_runner(designer), "p")  # type: ignore[arg-type]

        assert calls["n"] == bsa.MAX_DESIGNER_RETRIES
