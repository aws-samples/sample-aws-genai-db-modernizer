"""Regression test for issue #470: concurrent first use of GraphStore raced
ladybug's lazy, unsynchronized backend selection and could fail with
"Could not find lbug C API shared library" from a losing thread.

ladybug.Database picks its native backend (pybind, falling back to a ctypes
C API) the first time any Database is constructed. The choice is cached in
module-level globals inside ladybug._backend with a check-then-act pattern
and no lock: one thread sets an "already attempted" flag *before* its import
finishes, so a thread that checks the flag during that window sees "already
attempted" and returns the still-unset module, falls back to the C API
backend, and fails to find its shared library -- which this package never
ships.

The window between setting that flag and finishing the import is normally a
handful of microseconds, so the race is rare in practice. These tests widen
it deterministically (a short sleep patched into ladybug's import step) and
force several threads through a GraphStore() for the first time at once with
a barrier, so the race reproduces every time rather than occasionally.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

import src.graph.store as store_mod
from src.graph.store import GraphStore

lb_backend = pytest.importorskip("ladybug._backend")

_REQUIRED_BACKEND_ATTRS = ("_PYBIND_IMPORT_ATTEMPTED", "_PYBIND_MODULE", "_import_pybind_module")
if not all(hasattr(lb_backend, attr) for attr in _REQUIRED_BACKEND_ATTRS):
    pytest.skip(
        "ladybug backend internals changed; re-check #470 workaround", allow_module_level=True
    )

ITERATIONS = 10
THREAD_COUNT = 4
# Keep each GraphStore's buffer pool small -- the engine default reserves a
# large share of system memory per handle, which makes opening THREAD_COUNT
# of them per iteration needlessly slow for what this test checks.
_SMALL_BUFFER_POOL_BYTES = 16 * 1024 * 1024


@pytest.fixture(autouse=True)
def _reset_backend_state(monkeypatch):
    """Reset ladybug's lazy backend flags and our own warm-up flag.

    Simulates a fresh process where nobody has constructed a Database yet,
    so each test (and each loop iteration within a test) can force its own
    "first use" race window.
    """

    def reset() -> None:
        monkeypatch.setattr(lb_backend, "_PYBIND_IMPORT_ATTEMPTED", False)
        monkeypatch.setattr(lb_backend, "_PYBIND_MODULE", None)
        monkeypatch.setattr(store_mod, "_backend_loaded", False)

    reset()
    yield reset


def _widen_pybind_import_race(monkeypatch, delay: float = 0.01) -> None:
    """Patch ladybug's import step to pause after setting its "attempted" flag.

    ladybug._backend.get_pybind_module sets _PYBIND_IMPORT_ATTEMPTED = True,
    then calls _import_pybind_module() to actually do the import, then
    assigns the result to _PYBIND_MODULE. The sleep below lands inside that
    call, after the flag is set but before the module is assigned -- the
    exact window a racing thread can observe.
    """
    real_import = lb_backend._import_pybind_module

    def slow_import_pybind_module():
        time.sleep(delay)
        return real_import()

    monkeypatch.setattr(lb_backend, "_import_pybind_module", slow_import_pybind_module)


def _open_graph_stores_concurrently(
    base_dir: Path, label: str
) -> tuple[list[BaseException], list[bool]]:
    """Construct THREAD_COUNT GraphStores for the first time, all at once.

    Each thread opens a distinct path (mirroring GraphStoreCache opening
    several (db_name, job_id) keys in parallel) and waits on a barrier so
    they all reach GraphStore.__init__ together. Returns any exceptions
    raised by a thread (instead of letting pytest see them, so the caller
    can assert on them directly) and each successfully opened store's
    chosen backend, so the caller can confirm nobody fell back to the
    unshipped C API backend.
    """
    barrier = threading.Barrier(THREAD_COUNT)
    errors: list[BaseException] = []
    backends: list[bool] = []
    lock = threading.Lock()

    def open_one(i: int) -> None:
        try:
            path = base_dir / f"{label}-{i}" / "context.lbug"
            path.parent.mkdir(parents=True, exist_ok=True)
            barrier.wait()
            store = GraphStore(str(path), buffer_pool_size=_SMALL_BUFFER_POOL_BYTES)
            with lock:
                backends.append(store._db._use_pybind_backend)
            store.close()
        except BaseException as exc:  # noqa: BLE001 - surfaced by the caller's assert
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=open_one, args=(i,)) for i in range(THREAD_COUNT)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert not any(t.is_alive() for t in threads), "a thread never returned"
    return errors, backends


def test_concurrent_first_use_loads_the_backend_once(tmp_path, monkeypatch, _reset_backend_state):
    """Many threads racing to construct the first GraphStore never fail.

    Run many times (loop, not a single lucky pass) with the race window
    widened every iteration, to show the fix is stable and not just lucky.
    Every opened store must have picked the pybind backend -- the losing
    thread of this exact race falls back to the C API backend instead.
    """
    for i in range(ITERATIONS):
        _reset_backend_state()
        _widen_pybind_import_race(monkeypatch)
        errors, backends = _open_graph_stores_concurrently(tmp_path, label=f"iter{i}")
        assert errors == [], f"iteration {i}: {errors!r}"
        assert backends == [True] * THREAD_COUNT, f"iteration {i}: {backends!r}"
