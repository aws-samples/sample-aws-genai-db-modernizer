"""The scaling helper passes linear code and still fails quadratic code (#775)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.unit.tools.sql import _timing
from tests.unit.tools.sql._timing import assert_cost_ratio, assert_fast, assert_linear


def _linear(text: str) -> int:
    return sum(sum(range(50)) for _ in range(len(text) // 10_000))


def _quadratic(text: str) -> int:
    return sum(sum(range(50)) for _ in range((len(text) // 10_000) ** 2))


def test_linear_code_passes() -> None:
    assert_linear(lambda n: "x" * n, _linear)


def test_quadratic_code_fails_even_with_many_samples() -> None:
    with pytest.raises(AssertionError, match="not linear"):
        assert_linear(lambda n: "x" * n, _quadratic)


def _scripted(monkeypatch, values: list[float]) -> None:
    """Replace the clock with scripted times; ``fn`` still runs and is recorded."""
    it = iter(values)

    def fake_timed(fn):
        fn()
        return next(it)

    monkeypatch.setattr(_timing, "_timed", fake_timed)


def _recording(calls: list[int]):
    def fn(text: str) -> None:
        calls.append(len(text))

    return fn


def test_linear_code_is_sampled_once_after_the_warm_up(monkeypatch) -> None:
    """A quiet machine: warm up both sizes, one alternating round, stop."""
    calls: list[int] = []
    _scripted(monkeypatch, [0.1, 0.4])
    assert_linear(lambda n: "x" * n, _recording(calls))
    assert calls == [1_000_000, 4_000_000] * 2


def test_a_noisy_round_gets_more_samples_and_keeps_the_best_times(monkeypatch) -> None:
    """Round 1 is hit by load (ratio 20); round 2's best times pass (0.5 vs 2.0)."""
    calls: list[int] = []
    _scripted(monkeypatch, [1.0, 20.0, 0.5, 2.0])
    assert_linear(lambda n: "x" * n, _recording(calls))
    assert calls == [1_000_000, 4_000_000] * 3


def test_quadratic_code_uses_every_round_then_fails(monkeypatch) -> None:
    calls: list[int] = []
    _scripted(monkeypatch, [0.1, 1.6] * 5)
    with pytest.raises(AssertionError, match="not linear"):
        assert_linear(lambda n: "x" * n, _recording(calls), max_rounds=5)
    assert calls == [1_000_000, 4_000_000] * 6  # 2 warm-up calls + 5 rounds of 2


def test_time_is_cpu_time_not_wall_clock(monkeypatch) -> None:
    """Only ``process_time`` exists on this clock: a wall-clock helper would raise."""
    ticks = iter([0.0, 0.1, 0.1, 0.5])
    clock = SimpleNamespace(process_time=lambda: next(ticks))
    monkeypatch.setattr(_timing, "time", clock)
    assert_linear(lambda n: "x" * n, lambda text: None)


def test_assert_fast_passes_on_a_later_round_and_fails_when_every_round_is_slow(
    monkeypatch,
) -> None:
    runs: list[int] = []
    _scripted(monkeypatch, [0.3, 0.3, 0.02])
    assert_fast(lambda: runs.append(1), 0.05)
    assert len(runs) == 3

    runs.clear()
    _scripted(monkeypatch, [0.3] * 4)
    with pytest.raises(AssertionError, match="CPU"):
        assert_fast(lambda: runs.append(1), 0.05, max_rounds=4)
    assert len(runs) == 4


def test_assert_fast_fails_on_work_far_over_the_budget() -> None:
    with pytest.raises(AssertionError, match="CPU"):
        assert_fast(lambda: sum(range(3_000_000)), 0.0001, max_rounds=3)


def test_assert_fast_fails_at_once_when_one_run_is_far_over_the_budget(monkeypatch) -> None:
    runs: list[int] = []
    _scripted(monkeypatch, [0.6, 0.01])  # 12x the 0.05s budget on the first run
    with pytest.raises(AssertionError, match="over 10x"):
        assert_fast(lambda: runs.append(1), 0.05)
    assert len(runs) == 1


def test_cost_ratio_passes_work_that_scales_with_the_reference() -> None:
    assert_cost_ratio(lambda: sum(range(300_000)), lambda: sum(range(200_000)), 3.0)


def test_cost_ratio_fails_work_far_over_the_reference() -> None:
    with pytest.raises(AssertionError, match="relative to the reference"):
        assert_cost_ratio(lambda: sum(range(3_000_000)), lambda: sum(range(100_000)), 3.0, 3)


def test_cost_ratio_passes_on_a_later_round(monkeypatch) -> None:
    runs: list[str] = []
    times = iter([0.1, 0.4, 0.1, 0.25])  # per round: reference, then fn (x4, then x2.5)
    monkeypatch.setattr(_timing, "_timed", lambda fn: (fn(), next(times))[1])
    assert_cost_ratio(lambda: runs.append("fn"), lambda: runs.append("ref"), 3.0)
    assert runs == ["ref", "fn"] * 3  # a warm-up, then two timed rounds


def test_cost_ratio_fails_at_once_when_one_run_is_hopeless(monkeypatch) -> None:
    runs: list[int] = []
    times = iter([0.01, 1.0, 0.01, 0.01])  # x100 on the first round
    monkeypatch.setattr(_timing, "_timed", lambda fn: (fn(), next(times))[1])
    with pytest.raises(AssertionError, match="relative to the reference"):
        assert_cost_ratio(lambda: runs.append(1), lambda: None, 3.0)
    assert len(runs) == 2  # the warm-up and the one timed round
