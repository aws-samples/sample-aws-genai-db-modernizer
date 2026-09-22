"""Tests for the shared store-IO fan-out helpers (src.storage.parallel).

These pin the contract every per-artifact-IO call site relies on:
- run_parallel: fire-and-forget, runs work for every item, empty-safe, and
  surfaces worker exceptions rather than swallowing them.
- map_parallel: order-preserving map that drops None/errored items, empty-safe,
  and can propagate errors when asked.
"""

from __future__ import annotations

import threading

import pytest

from src.storage.parallel import map_parallel, run_parallel


class TestRunParallel:
    def test_runs_work_for_every_item(self):
        seen: set[int] = set()
        lock = threading.Lock()

        def _work(n: int) -> None:
            with lock:
                seen.add(n)

        run_parallel(_work, range(250))

        assert seen == set(range(250))

    def test_empty_items_is_noop(self):
        calls: list[object] = []
        run_parallel(calls.append, [])
        assert calls == []

    def test_worker_exception_surfaces(self):
        # A lazy pool.map would swallow this; the helper forces evaluation so it
        # propagates to the caller.
        def _boom(n: int) -> None:
            if n == 7:
                raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            run_parallel(_boom, range(50))


class TestMapParallel:
    def test_preserves_input_order(self):
        # Even though work completes out of order across threads, results come
        # back aligned to the input sequence.
        got = map_parallel(lambda n: n * 2, range(100))
        assert got == [n * 2 for n in range(100)]

    def test_drops_none_results(self):
        # Odd numbers map to None and are omitted; evens are kept, in order.
        got = map_parallel(lambda n: n if n % 2 == 0 else None, range(10))
        assert got == [0, 2, 4, 6, 8]

    def test_skips_errored_items_by_default(self):
        def _fn(n: int) -> int:
            if n % 3 == 0:
                raise RuntimeError("skip me")
            return n

        got = map_parallel(_fn, range(10))
        # 0,3,6,9 raise and are dropped; the rest survive in order.
        assert got == [1, 2, 4, 5, 7, 8]

    def test_skip_errors_false_propagates(self):
        def _fn(n: int) -> int:
            if n == 4:
                raise RuntimeError("stop")
            return n

        with pytest.raises(RuntimeError, match="stop"):
            map_parallel(_fn, range(10), skip_errors=False)

    def test_skip_errors_false_still_drops_none(self):
        # A None return is a "skip this item" signal, distinct from an error;
        # it is dropped even when errors are set to propagate.
        got = map_parallel(lambda n: None if n == 2 else n, range(5), skip_errors=False)
        assert got == [0, 1, 3, 4]

    def test_empty_items_returns_empty_list(self):
        assert map_parallel(lambda x: x, []) == []
