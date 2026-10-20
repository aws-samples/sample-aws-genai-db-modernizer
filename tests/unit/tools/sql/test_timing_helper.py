"""The scaling helper passes linear code and still fails quadratic code (#775, #1230)."""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.unit.tools.sql import _timing
from tests.unit.tools.sql._timing import SIZES, assert_cost_ratio, assert_linear, max_scaling_ratio

SMALL, LARGE = SIZES


def _linear(text: str) -> int:
    return sum(sum(range(50)) for _ in range(len(text) // 5_000))


def _quadratic(text: str) -> int:
    # About 1.5 ms on the small input on a laptop: long enough that the fixed
    # cost of a sample is noise, short enough for twelve rounds.
    return sum(sum(range(50)) for _ in range((len(text) // 5_000) ** 2))


def test_linear_code_passes() -> None:
    assert_linear(lambda n: "x" * n, _linear)


def test_quadratic_code_fails_even_with_many_samples() -> None:
    with pytest.raises(AssertionError, match="not linear"):
        assert_linear(lambda n: "x" * n, _quadratic)


def test_the_limit_sits_between_linear_and_quadratic_growth() -> None:
    assert max_scaling_ratio((1, 4)) == 8.0
    linear, quadratic = LARGE / SMALL, (LARGE / SMALL) ** 2
    limit = max_scaling_ratio(SIZES)
    # Each side has the same margin, and it is well over the 2x of a 4x size step.
    assert limit / linear == pytest.approx(quadratic / limit)
    assert limit / linear > 2.5


def test_another_threads_cpu_time_is_not_counted() -> None:
    """A thread an earlier test left running must not count as the measured
    work (#1230): process CPU time would count it, so a short sample could
    read as long as a large one."""
    burned: list[float] = []

    def burn() -> None:
        start = _timing.time.thread_time()
        buf = b"x" * 20_000_000
        for _ in range(10):
            hashlib.sha256(buf).digest()  # releases the GIL, like a native thread
        burned.append(_timing.time.thread_time() - start)

    worker = threading.Thread(target=burn)
    worker.start()
    measured = _timing._timed(worker.join)
    assert burned and burned[0] > 0
    assert measured < burned[0] / 4, (measured, burned[0])


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
    _scripted(monkeypatch, [0.1, 0.8])
    assert_linear(lambda n: "x" * n, _recording(calls))
    assert calls == [SMALL, LARGE] * 2


def test_a_noisy_round_gets_more_samples_and_keeps_the_best_times(monkeypatch) -> None:
    """Round 1 is hit by load (ratio 40); round 2's best times pass (0.5 vs 4.0)."""
    calls: list[int] = []
    _scripted(monkeypatch, [1.0, 40.0, 0.5, 4.0])
    assert_linear(lambda n: "x" * n, _recording(calls))
    assert calls == [SMALL, LARGE] * 3


def test_quadratic_code_uses_every_round_then_fails(monkeypatch) -> None:
    calls: list[int] = []
    _scripted(monkeypatch, [0.1, 6.4] * 5)
    with pytest.raises(AssertionError, match="not linear"):
        assert_linear(lambda n: "x" * n, _recording(calls), max_rounds=5)
    assert calls == [SMALL, LARGE] * 6  # 2 warm-up calls + 5 rounds of 2


@pytest.mark.parametrize(("large_time", "passes"), [(2.25, True), (2.27, False)])
def test_the_limit_is_exclusive_at_the_edge(monkeypatch, large_time, passes) -> None:
    """x22.5 is under the x22.6 limit; x22.7 is over it in every round."""
    _scripted(monkeypatch, [0.1, large_time] * 3)
    if passes:
        assert_linear(lambda n: "x" * n, lambda text: None, max_rounds=3)
    else:
        with pytest.raises(AssertionError, match="limit x22.6"):
            assert_linear(lambda n: "x" * n, lambda text: None, max_rounds=3)


def test_custom_sizes_set_the_inputs_and_the_limit(monkeypatch) -> None:
    """A 4x step: x7.9 passes the x8 limit, x8.1 fails it."""
    calls: list[int] = []
    _scripted(monkeypatch, [0.1, 0.79])
    assert_linear(lambda n: "x" * n, _recording(calls), sizes=(10, 40))
    assert calls == [10, 40] * 2

    _scripted(monkeypatch, [0.1, 0.81])
    with pytest.raises(AssertionError, match="limit x8.0"):
        assert_linear(lambda n: "x" * n, lambda text: None, max_rounds=1, sizes=(10, 40))


def test_the_backstop_takes_a_later_round_under_it(monkeypatch) -> None:
    """Round 1 grows linearly (x8) but is over the 0.5 s backstop; round 2 fits."""
    calls: list[int] = []
    _scripted(monkeypatch, [0.1, 0.8, 0.1, 0.3])
    assert_linear(lambda n: "x" * n, _recording(calls), backstop_s=0.5)
    assert calls == [SMALL, LARGE] * 3


def test_linear_code_over_the_backstop_in_every_round_fails(monkeypatch) -> None:
    _scripted(monkeypatch, [0.1, 0.8] * 3)
    with pytest.raises(AssertionError, match="over the 0.5s backstop: linear but far too slow"):
        assert_linear(lambda n: "x" * n, lambda text: None, max_rounds=3, backstop_s=0.5)


def test_time_is_this_threads_cpu_time(monkeypatch) -> None:
    """Only ``thread_time`` exists on this clock: a wall-clock or process-wide
    helper would raise."""
    ticks = iter([0.0, 0.1, 0.1, 0.8])
    clock = SimpleNamespace(thread_time=lambda: next(ticks))
    monkeypatch.setattr(_timing, "time", clock)
    assert_linear(lambda n: "x" * n, lambda text: None)


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


@pytest.mark.parametrize("enabled_before", [True, False])
def test_the_garbage_collector_is_off_while_timing(enabled_before) -> None:
    """A collection costs in proportion to the whole heap, so one landing in a
    short run would shrink a ratio. Timing turns the collector off and then
    restores whatever state it found."""
    import gc

    seen: list[bool] = []
    (gc.enable if enabled_before else gc.disable)()
    try:
        _timing._timed(lambda: seen.append(gc.isenabled()))
        assert seen == [False]
        assert gc.isenabled() is enabled_before
    finally:
        gc.enable()


# Every clock in ``time`` that can time a run, and the module that times runs.
CLOCKS = {
    name + suffix
    for name in ("perf_counter", "process_time", "thread_time", "monotonic", "time")
    for suffix in ("", "_ns")
}

# Clock reads that time nothing in the test itself, with the reason. Anything
# else in a test file is how an absolute budget comes back (#1230).
ALLOWED_CLOCKS = {
    # file modification times, not a timer
    "tests/unit/shared/test_sample_collection.py": {"time.time"},
    "tests/unit/api/test_local_execution_artifact_deletion.py": {"time.time"},
    # waiting for a live server, and the bounds of the wall-clock query
    # timeout the test is about
    "tests/graph/test_read_bounds.py": {"time.monotonic"},
    # waiting for the e2e servers to start
    "tests/e2e/conftest.py": {"time.time"},
    # this file's self-tests of the helper's own clock
    "tests/unit/tools/sql/test_timing_helper.py": {"time.thread_time"},
}


def _clock_uses(source: str) -> set[str]:
    """Every use of a ``time`` clock or of ``timeit`` in ``source``, as
    ``time.<clock>`` or ``timeit``: called or not, through ``import time as t``,
    ``from time import perf_counter as pc`` or another module's ``time``."""
    import ast

    tree = ast.parse(source)
    time_names: set[str] = set()
    uses: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "time":
                    time_names.add(alias.asname or "time")
                elif alias.name.split(".")[0] == "timeit":
                    uses.add("timeit")
        elif isinstance(node, ast.ImportFrom) and node.module in ("time", "timeit"):
            for alias in node.names:
                if node.module == "timeit":
                    uses.add("timeit")
                elif alias.name in CLOCKS or alias.name == "*":
                    uses.add(f"time.{alias.name}")
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in CLOCKS:
            base = node.value
            if (isinstance(base, ast.Name) and base.id in time_names) or (
                isinstance(base, ast.Attribute) and base.attr == "time"
            ):
                uses.add(f"time.{node.attr}")
    return uses


@pytest.mark.parametrize(
    "source",
    [
        "import time\nt = time.perf_counter()",
        "import time\nt = time.perf_counter_ns()",
        "import time\ntime.process_time_ns()",
        "import time\ntime.monotonic_ns()",
        "import time\nstart = time.time()",
        "import time as clock\nclock.thread_time()",
        "from time import perf_counter as pc\npc()",
        "from time import monotonic",
        "import time\nt = time.perf_counter\nt()",
        "import timeit\ntimeit.timeit('1')",
        "from timeit import default_timer",
        "from tests.unit.tools.sql import _timing\n_timing.time.thread_time()",
    ],
)
def test_the_clock_scan_sees_every_way_to_read_a_clock(source) -> None:
    assert _clock_uses(source)


@pytest.mark.parametrize(
    "source",
    [
        "import time\ntime.sleep(0.1)",
        "from unittest.mock import patch\npatch('time.time')",
        "import datetime\ndatetime.datetime.now()",
        "x = {'duration_seconds': 1.0}",
    ],
)
def test_the_clock_scan_ignores_what_is_not_a_clock(source) -> None:
    assert not _clock_uses(source)


def _scanned_files():
    """Every Python file under tests/, and the test files under ci/ and benchmarks/."""
    repo = Path(__file__).parents[4]
    files = set((repo / "tests").rglob("*.py"))
    for root in ("ci", "benchmarks"):
        files |= {
            f
            for f in (repo / root).rglob("*.py")
            if f.name.startswith("test_") or f.name == "conftest.py"
        }
    helper = Path(_timing.__file__).resolve()
    return repo, sorted(f for f in files if f.resolve() != helper)


def test_no_test_times_itself_against_a_fixed_number_of_seconds() -> None:
    """Every timing check goes through this helper's relative assertions
    (AGENTS.md checklist item 8, #1230). The scan reads each file's syntax tree,
    so an ``_ns`` clock, an alias or ``timeit`` is seen too; the few clock
    reads that time nothing are listed in ALLOWED_CLOCKS with their reason."""
    repo, files = _scanned_files()
    offenders = {}
    for path in files:
        rel = path.relative_to(repo).as_posix()
        source = path.read_text(encoding="utf-8")
        extra = _clock_uses(source) - ALLOWED_CLOCKS.get(rel, set())
        if "assert_fast" in source and path.resolve() != Path(__file__).resolve():
            extra.add("assert_fast (removed: it compared CPU time with fixed seconds)")
        if extra:
            offenders[rel] = sorted(extra)
    assert not offenders, offenders


def test_every_allowed_clock_is_still_used() -> None:
    """An entry nobody needs any more is a hole in the scan: remove it."""
    repo, _ = _scanned_files()
    for rel, allowed in ALLOWED_CLOCKS.items():
        assert allowed <= _clock_uses((repo / rel).read_text(encoding="utf-8")), rel
