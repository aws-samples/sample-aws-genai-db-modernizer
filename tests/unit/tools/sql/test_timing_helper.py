"""The scaling helper passes linear code and still fails quadratic code (#775)."""

from __future__ import annotations

import pytest

from tests.unit.tools.sql._timing import assert_linear


def _linear(text: str) -> int:
    return sum(sum(range(50)) for _ in range(len(text) // 10_000))


def _quadratic(text: str) -> int:
    return sum(sum(range(50)) for _ in range((len(text) // 10_000) ** 2))


def test_linear_code_passes() -> None:
    assert_linear(lambda n: "x" * n, _linear)


def test_quadratic_code_fails_even_with_many_samples() -> None:
    with pytest.raises(AssertionError, match="not linear"):
        assert_linear(lambda n: "x" * n, _quadratic)
