"""Timing helpers for the SQL tools' scaling tests (#557).

Absolute time budgets flake on slow or loaded CI runners, where the same
linear code can run 20-30x slower than on a laptop. These helpers assert
the property that matters instead: time grows linearly with input size.
A generous absolute ceiling still catches a real hang.
"""

from __future__ import annotations

import time
from collections.abc import Callable

# time(4 MB) / time(1 MB) is about 4 for linear code and about 16 for
# quadratic code; 8 leaves room for a loaded CI runner without letting
# quadratic pass.
MAX_SCALING_RATIO = 8.0
# Only a hang (the quadratic regex took about an hour on 1 MB) gets near this.
HANG_CEILING_S = 10.0


def _timed(fn: Callable[[], object]) -> float:
    """CPU time of ``fn`` in this process.

    Wall-clock time also counts the time the process spends waiting for a
    core, which on a busy runner (a full xdist run, other suites in parallel)
    is the dominant noise for runs this short. CPU time leaves that out.
    """
    start = time.process_time()
    fn()
    return time.process_time() - start


def assert_linear(
    make_text: Callable[[int], str], fn: Callable[[str], object], max_rounds: int = 12
) -> None:
    """``fn`` on a 4 MB input takes under MAX_SCALING_RATIO times its 1 MB time
    (best of up to ``max_rounds`` each, after a warm-up of both), and the 1 MB
    run stays under HANG_CEILING_S.

    The 1 MB and 4 MB runs alternate, so a burst of load from other tests on the
    runner hits both sizes rather than skewing the ratio. Sampling stops as soon
    as the ratio of the two best times is under the limit: more samples can only
    lower a best time, so linear code passes in the first rounds on a quiet
    machine and keeps getting chances on a noisy one, while quadratic code
    (a ratio near 16 in every sample) never gets under the limit.
    """
    small, large = make_text(1_000_000), make_text(4_000_000)
    fn(small)
    fn(large)
    t_small = t_large = float("inf")
    ratio = float("inf")
    for _ in range(max_rounds):
        t_small = min(t_small, _timed(lambda: fn(small)))
        assert t_small < HANG_CEILING_S, f"1 MB took {t_small:.2f}s"
        t_large = min(t_large, _timed(lambda: fn(large)))
        ratio = t_large / max(t_small, 1e-6)
        if ratio < MAX_SCALING_RATIO:
            return
    raise AssertionError(
        f"4 MB took {t_large:.3f}s vs {t_small:.3f}s for 1 MB (x{ratio:.1f}): not linear"
    )
