"""Timing helpers for the scaling tests (#557, #1230).

Absolute time budgets flake on slow or loaded CI runners, where the same
linear code can run 20-30x slower than on a laptop, and slower again under
coverage. These helpers assert the property that matters instead: cost grows
linearly with input size, or stays within a ratio of a reference workload run
in the same process. Nothing here compares a time with a fixed number of
seconds, except a generous ceiling that only a real hang reaches.
"""

from __future__ import annotations

import gc
import time
from collections.abc import Callable

# The default input sizes: 0.5 MB and 4 MB of text. An 8x size step puts linear
# code near x8 and quadratic code near x64, so the limit between them (their
# geometric mean, about x22.6) leaves each a margin of nearly 3x (#1230).
SIZES = (500_000, 4_000_000)
# Only a hang (the quadratic regex took about an hour on 1 MB) gets near this.
HANG_CEILING_S = 10.0
# assert_linear passes only on the best of at least this many samples of each
# size: a single sample slowed by load could pass quadratic code (#1304).
MIN_ROUNDS = 2


def _timed(fn: Callable[[], object]) -> float:
    """CPU time of ``fn`` in this thread, with the cyclic garbage collector off.

    Wall-clock time also counts the time the process spends waiting for a
    core, which on a busy runner (a full xdist run, other suites in parallel)
    is the dominant noise for runs this short. Process CPU time leaves that
    out but still counts every other thread of the process, such as a server
    or a native query thread an earlier test left running: that distorts the
    ratio of two short runs (#1230). The thread's own CPU time counts only
    ``fn``, which must therefore do its work on the calling thread.

    Work ``fn`` hands to a thread pool or a subprocess is not counted, so such
    work reads as near zero and a check over it passes vacuously: time only
    code that runs on the calling thread.

    A collection costs in proportion to every object the process holds, not to
    ``fn``'s input, so one landing in a short run inflates it the most and
    shrinks a ratio. ``timeit`` turns the collector off for the same reason.
    The helpers therefore check algorithmic growth, not garbage-collection or
    memory pressure, and a large run keeps its garbage until it returns.
    """
    enabled = gc.isenabled()
    gc.disable()
    try:
        start = time.thread_time()
        fn()
        return time.thread_time() - start
    finally:
        if enabled:
            gc.enable()


def max_scaling_ratio(sizes: tuple[int, int]) -> float:
    """The limit for ``time(large) / time(small)``: halfway, on a log scale,
    between linear growth (``large / small``) and quadratic growth (its square)."""
    small, large = sizes
    return float((large / small) ** 1.5)


def assert_linear(
    make_text: Callable[[int], str],
    fn: Callable[[str], object],
    max_rounds: int = 12,
    *,
    sizes: tuple[int, int] = SIZES,
    backstop_s: float | None = None,
) -> None:
    """``fn`` on ``make_text(large)`` takes under ``max_scaling_ratio(sizes)``
    times its time on ``make_text(small)`` (best of up to ``max_rounds`` each,
    after a warm-up of both), and the small run stays under HANG_CEILING_S.

    A growth check alone passes linear code of any constant factor. Where a
    much slower linear implementation matters (a filter in front of an
    endpoint), ``backstop_s`` also bounds the best large-input CPU time. Set it
    orders of magnitude above the measured cost, so only a regression of that
    size, never a slow or loaded runner, reaches it. Neither bound can stop a
    hang: both are checked once ``fn`` returns.

    The small and large runs alternate, so a burst of load from other tests on
    the runner hits both sizes rather than skewing the ratio. Sampling stops as
    soon as the ratio of the two best times is under the limit, from round
    MIN_ROUNDS on: more samples can only lower a best time, so linear code
    passes in the first rounds on a quiet machine and keeps getting chances on a
    noisy one, while quadratic code (a ratio near the square of the size step in
    every sample) never gets under it.

    Load only ever inflates a sample, so a pass is the one verdict noise can
    fake: one small run slowed past the margin (about 2.8x for an 8x step) makes
    quadratic code read as linear. A pass therefore needs the best of at least
    MIN_ROUNDS samples of each size, never a single one (#1304).
    """
    if max_rounds < MIN_ROUNDS:
        raise ValueError(f"max_rounds must be at least {MIN_ROUNDS}, got {max_rounds}")
    small_n, large_n = sizes
    limit = max_scaling_ratio(sizes)
    small, large = make_text(small_n), make_text(large_n)
    fn(small)
    fn(large)
    t_small = t_large = float("inf")
    ratio = float("inf")
    for rounds in range(1, max_rounds + 1):
        t_small = min(t_small, _timed(lambda: fn(small)))
        assert t_small < HANG_CEILING_S, f"size {small_n:,} took {t_small:.2f}s"
        t_large = min(t_large, _timed(lambda: fn(large)))
        ratio = t_large / max(t_small, 1e-6)
        if rounds < MIN_ROUNDS:
            continue
        if ratio < limit and (backstop_s is None or t_large < backstop_s):
            return
    if ratio < limit:
        raise AssertionError(
            f"size {large_n:,} took {t_large:.3f}s CPU at best, "
            f"over the {backstop_s:g}s backstop: linear but far too slow"
        )
    raise AssertionError(
        f"size {large_n:,} took {t_large:.3f}s vs {t_small:.3f}s for size {small_n:,} "
        f"(x{ratio:.1f}, limit x{limit:.1f}): not linear"
    )


# A single round this many times over the limit is slow code, not load: fail at once
# instead of re-running it for every round.
HOPELESS_FACTOR = 10.0


def assert_cost_ratio(
    fn: Callable[[], object],
    reference: Callable[[], object],
    max_ratio: float,
    max_rounds: int = 12,
) -> None:
    """``fn`` costs at most ``max_ratio`` times ``reference`` (CPU time, best of
    up to ``max_rounds`` runs each, taken in the same process; see ``_timed``
    for what that time leaves out).

    An absolute budget measures the machine as much as the code: the same run
    takes 43 ms on a laptop and 255 ms on a CI runner. Both workloads slow down
    together on a slow machine, so their ratio does not. The reference is the
    same code on input where the property under test cannot show (for example
    short queries), so the ratio isolates the cost that grows with the input.

    The reference and ``fn`` alternate, so a burst of load hits both. Sampling
    stops as soon as the ratio of the two best times fits: more samples can only
    lower a best time, so fast code passes in the first rounds and keeps getting
    chances on a noisy machine, while slow code is over the limit in every
    round. A round over HOPELESS_FACTOR times the limit fails at once.
    """
    reference()
    fn()
    t_ref = t_fn = float("inf")
    ratio = float("inf")
    for _ in range(max_rounds):
        t_ref = min(t_ref, _timed(reference))
        elapsed = _timed(fn)
        t_fn = min(t_fn, elapsed)
        ratio = t_fn / max(t_ref, 1e-6)
        if ratio < max_ratio:
            return
        if elapsed / max(t_ref, 1e-6) > max_ratio * HOPELESS_FACTOR:
            break
    raise AssertionError(
        f"took {t_fn:.3f}s CPU vs {t_ref:.3f}s for the reference "
        f"(x{ratio:.1f}), limit x{max_ratio:g}: over budget relative to the reference"
    )
