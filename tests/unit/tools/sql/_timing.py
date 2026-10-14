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
    start = time.perf_counter()
    fn()
    return time.perf_counter() - start


def assert_linear(
    make_text: Callable[[int], str], fn: Callable[[str], object], repeats: int = 3
) -> None:
    """``fn`` on a 4 MB input takes under MAX_SCALING_RATIO times its 1 MB time
    (best of ``repeats`` each, after a warm-up of both), and the 1 MB run stays
    under HANG_CEILING_S.

    The 1 MB and 4 MB runs alternate, so a burst of load from other tests on the
    runner hits both sizes rather than skewing the ratio.
    """
    small, large = make_text(1_000_000), make_text(4_000_000)
    fn(small)
    fn(large)
    t_small = t_large = float("inf")
    for _ in range(repeats):
        t_small = min(t_small, _timed(lambda: fn(small)))
        assert t_small < HANG_CEILING_S, f"1 MB took {t_small:.2f}s"
        t_large = min(t_large, _timed(lambda: fn(large)))
    ratio = t_large / max(t_small, 1e-6)
    assert (
        ratio < MAX_SCALING_RATIO
    ), f"4 MB took {t_large:.3f}s vs {t_small:.3f}s for 1 MB (x{ratio:.1f}): not linear"
