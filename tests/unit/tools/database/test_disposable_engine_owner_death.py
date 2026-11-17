# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A disposable engine never outlives the process that started it (#2350).

Each test starts a real throwaway server from a child Python process, then ends the
child and checks that the server (and the watcher that stops it) is gone. The timeouts
are generous and relative to the engine's own shutdown budget: the tests wait on the
event (the pid disappearing), so a slow runner only takes longer.

MySQL runs where a ``mysqld`` is present and is marked ``integration``; PostgreSQL runs
wherever ``pgserver`` or a ``MODERNIZER_PG_BIN_<major>`` is installed.
"""

from __future__ import annotations

import contextlib
import os
import select
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from src.tools.database import disposable_engine as de

_REPO = Path(__file__).resolve().parents[4]
_CHILD = """
import sys, time
from src.tools.database import disposable_engine as de
h = de.start(sys.argv[1], "", newest=True)
watcher = getattr(de, "_WATCHERS", {}).get(h.workdir)  # none on a build without one
print(h.workdir, h.pid, watcher.pid if watcher else 0, flush=True)
if sys.argv[2] == "hold":
    time.sleep(600)
else:
    de.stop(h)
"""
_ENGINES = [
    pytest.param(
        "mysql",
        marks=[
            pytest.mark.integration,
            pytest.mark.skipif(not de.available_majors("mysql"), reason="no mysqld"),
        ],
    ),
    pytest.param(
        "postgresql",
        marks=pytest.mark.skipif(not de.available_majors("postgresql"), reason="no PostgreSQL"),
    ),
]
pytestmark = pytest.mark.skipif(os.geteuid() == 0, reason="the engine refuses to run as root")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_gone(pids: list[int], budget_s: float) -> list[int]:
    """The pids still alive after ``budget_s``; returns as soon as all are gone."""
    deadline = time.monotonic() + budget_s
    left = [p for p in pids if _alive(p)]
    while left and time.monotonic() < deadline:
        time.sleep(0.2)
        left = [p for p in left if _alive(p)]
    return left


def _budget() -> float:
    # Shutdown escalates to SIGKILL after the engine's own budget; allow a few of those.
    return de._SHUTDOWN_TIMEOUT_S * 4 + 30


def _spawn(engine: str, mode: str):
    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD, engine, mode],
        cwd=_REPO,
        env={**os.environ, "PYTHONPATH": str(_REPO)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    ready, _, _ = select.select([proc.stdout], [], [], de._INIT_TIMEOUT_S + de._START_TIMEOUT_S)
    line = proc.stdout.readline().split() if ready else []
    if len(line) != 3:
        proc.kill()
        _, err = proc.communicate()
        raise AssertionError(f"the engine did not start in the child process: {err[-1500:]}")
    return proc, Path(line[0]), int(line[1]), int(line[2])


def _own_leftovers(folder: Path) -> list[str]:
    """Processes with this test's own folder in their command line (``pgrep -fl``)."""
    out = subprocess.run(
        ["pgrep", "-fl", "mysqld|postgres|_engine_watcher"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return [line for line in out.splitlines() if str(folder) in line]


def _cleanup(proc, folder: Path, pids: list[int]) -> None:
    proc.kill()
    proc.wait()
    for pid in filter(
        None, pids
    ):  # by pid, and only a process that still has our folder in its command line
        if de._ours(pid, folder):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
    _wait_gone(pids, 10)
    shutil.rmtree(folder, ignore_errors=True)


@pytest.mark.parametrize("engine", _ENGINES)
def test_a_sigkilled_owner_takes_its_engine_with_it(engine):
    proc, folder, server, watcher = _spawn(engine, "hold")
    try:
        assert de._running(server)
        proc.send_signal(signal.SIGKILL)
        proc.wait()
        left = _wait_gone([p for p in (server, watcher) if p], _budget())
        assert left == [], f"still running after the owner was killed: {left}"
        assert _own_leftovers(folder) == []
    finally:
        _cleanup(proc, folder, [server, watcher])
    assert _own_leftovers(folder) == []


@pytest.mark.parametrize("engine", _ENGINES)
def test_a_terminated_owner_takes_its_engine_with_it(engine):
    """SIGTERM with no handler, like a timeout's kill: no atexit hook, no ``finally``."""
    proc, folder, server, watcher = _spawn(engine, "hold")
    try:
        os.kill(proc.pid, signal.SIGTERM)  # the default action: no handler, no cleanup
        proc.wait()
        left = _wait_gone([p for p in (server, watcher) if p], _budget())
        assert left == [], f"still running after the owner was terminated: {left}"
    finally:
        _cleanup(proc, folder, [server, watcher])
    assert _own_leftovers(folder) == []


@pytest.mark.passes_on_main  # guards the explicit cleanup, which main has; the watcher must not break it
@pytest.mark.parametrize("engine", _ENGINES)
def test_a_normal_stop_leaves_neither_the_engine_nor_its_watcher(engine):
    proc, folder, server, watcher = _spawn(engine, "stop")
    try:
        proc.wait(timeout=_budget())
        assert proc.returncode == 0
        left = _wait_gone([p for p in (server, watcher) if p], _budget())
        assert left == [], f"still running after a normal stop: {left}"
        assert not folder.exists()
    finally:
        _cleanup(proc, folder, [server, watcher])
    assert _own_leftovers(folder) == []
