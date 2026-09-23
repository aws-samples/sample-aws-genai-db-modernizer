"""Bounded thread-pool fan-out for independent per-item ArtifactStore IO.

Under the ATX storage backend every ``read_json``/``write_json`` is a network
round trip (create presigned URL + S3 transfer, ~0.3-0.5s each). A loop that
does one such call per query/journey/artifact over hundreds or thousands of
items — the reference "discourse" workload has ~1,654 queries — becomes minutes
of pure serial latency. When the per-item work is independent (each item touches
a DISTINCT key; no shared accumulator, no read-modify-write of a common key),
fanning it across a thread pool collapses that to roughly ``ceil(n / workers)``.
Local/S3 backends are unaffected; the pool just runs cheap calls concurrently.

This module is the single home for that pattern. Two shapes cover every
per-artifact-IO call site in the codebase:

- :func:`run_parallel` — fire-and-forget (e.g. writing one artifact per query);
  ``work_one`` returns nothing.
- :func:`map_parallel` — map-and-collect that preserves input order and drops
  items whose work returned ``None`` or raised (e.g. reading a page of journeys
  and skipping the unreadable ones).

NOT for this module: the engine-/group-/AWS-resource fan-outs elsewhere
(``local_orchestrator`` analysis/schema phases, ``schema_design`` group runner,
the load-test provisioners). Those are bounded to a small constant (≤5-6), use
``submit``/``as_completed`` with per-item error *aggregation* rather than
skip-and-continue, and collect typed results — a different concern. Keep them
separate rather than forcing them through here.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

# Old-style TypeVars rather than PEP 695 (`def f[T](...)`): the project's mypy
# does not yet support PEP 695 generics. The noqa on each signature silences
# ruff's UP047 upgrade hint, which conflicts with that mypy limitation.
T = TypeVar("T")
R = TypeVar("R")

# Independent per-artifact IO is latency-bound (waiting on the network), not
# CPU-bound, so a wide pool helps and threads avoid pickling the store. Capped
# per call to len(items) so small workloads don't spin up idle threads.
_MAX_WORKERS = 32


def _worker_count(n: int) -> int:
    return min(_MAX_WORKERS, n)


def run_parallel(work_one: Callable[[T], None], items: Iterable[T]) -> None:  # noqa: UP047
    """Run ``work_one(item)`` for every item across a bounded thread pool.

    Fire-and-forget fan-out for independent per-item IO (typically writes).
    ``work_one`` performs the side effect and returns nothing. Callers must
    ensure each item's work touches a DISTINCT key — that independence is what
    makes the fan-out safe.

    ``list()`` forces evaluation of the lazy ``map`` so an exception in any
    worker surfaces here rather than being silently dropped.
    """
    work = list(items)
    if not work:
        return
    with ThreadPoolExecutor(max_workers=_worker_count(len(work))) as pool:
        list(pool.map(work_one, work))


def map_parallel(  # noqa: UP047
    fn: Callable[[T], R | None],
    items: Iterable[T],
    *,
    skip_errors: bool = True,
) -> list[R]:
    """Map ``fn`` over ``items`` concurrently, preserving input order.

    Map-and-collect fan-out for independent per-item reads. Returns the results
    in the SAME order as ``items``. An item is omitted from the result when its
    ``fn`` returns ``None`` or (with ``skip_errors=True``, the default) raises —
    the common "read a batch, skip the unreadable ones, keep the rest" shape.

    With ``skip_errors=False`` an exception in ``fn`` propagates instead of being
    swallowed; a ``None`` return is still dropped.
    """
    work = list(items)
    if not work:
        return []

    def _one(item: T) -> R | None:
        if not skip_errors:
            return fn(item)
        try:
            return fn(item)
        except Exception:  # noqa: BLE001 - skip an item that failed, keep the rest
            return None

    with ThreadPoolExecutor(max_workers=_worker_count(len(work))) as pool:
        results = list(pool.map(_one, work))
    return [r for r in results if r is not None]
