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
engine, mode, bin_dir = sys.argv[1], sys.argv[2], sys.argv[3]
kw = {"pg_bin_dir": bin_dir} if bin_dir else {}
spied = {}
real_start_watcher = getattr(de, "_start_watcher", None)  # none on a build without one
def spy(workdir):
    real_start_watcher(workdir)
    spied["pid"] = de._WATCHERS[workdir].pid if workdir in de._WATCHERS else 0
spied["pid"] = 0
if real_start_watcher is not None:
    de._start_watcher = spy
if mode == "start_fails":
    def boom(*a, **k):
        raise RuntimeError("the backend did not come up")
    de._MySQLBackend = boom
    de._PostgresBackend = boom
    try:
        de.start(engine, "", newest=True, **kw)
    except RuntimeError:
        pass
    print("-", 0, spied["pid"], flush=True)
    print("failed", flush=True)
    time.sleep(600)
h = de.start(engine, "", newest=True, **kw)
print(h.workdir, h.pid, spied["pid"], flush=True)
if mode == "hold":
    time.sleep(600)
de.stop(h)
if mode == "stop_then_hold":
    print("stopped", flush=True)
    time.sleep(600)
"""
_PGSERVER_BIN = de._pgserver_bin_dir()
_ENGINES = [
    pytest.param(
        ("mysql", ""),
        id="mysql",
        marks=[
            pytest.mark.integration,
            pytest.mark.skipif(not de.available_majors("mysql"), reason="no mysqld"),
        ],
    ),
    # Which PostgreSQL is found is a choice of the test, not of whatever is installed:
    # the newest major discovery finds (pgserver's, where nothing else is installed) and
    # an explicit bin folder. Both run initdb and postgres directly (#2518).
    pytest.param(
        ("postgresql", ""),
        id="postgresql-pgserver",
        marks=pytest.mark.skipif(not de.available_majors("postgresql"), reason="no PostgreSQL"),
    ),
    pytest.param(
        ("postgresql", str(_PGSERVER_BIN) if _PGSERVER_BIN else ""),
        id="postgresql-bin-folder",
        marks=pytest.mark.skipif(_PGSERVER_BIN is None, reason="no PostgreSQL bin folder"),
    ),
]


@pytest.fixture(autouse=True)
def _not_as_root():
    if os.geteuid() == 0:
        pytest.skip("the engine refuses to run as root")


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
    # The watcher's own grace before SIGKILL plus a margin, and clearly under the sweep's
    # minimum age: ``sweep_stale`` in another run must never be what stops the engine.
    return 60.0


def _spawn(kind: tuple[str, str], mode: str):
    engine, bin_dir = kind
    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD, engine, mode, bin_dir],
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


def _is_watcher(pid: int, folder: Path) -> bool:
    """A live watcher process for this folder, not just any non-zero pid."""
    command = de._proc_cmdline(pid) or ""
    return "_engine_watcher" in command and str(folder) in command


def _read_line(proc) -> str:
    # A plain readline: select would not see a line the text reader already buffered. The
    # child prints it right after the first one, or exits (an empty line).
    return str(proc.stdout.readline().strip())


@pytest.mark.parametrize("kind", _ENGINES)
def test_a_sigkilled_owner_takes_its_engine_with_it(kind):
    proc, folder, server, watcher = _spawn(kind, "hold")
    try:
        assert de._running(server)
        assert watcher and _is_watcher(watcher, folder), "no watcher runs for this engine"
        proc.send_signal(signal.SIGKILL)
        proc.wait()
        left = _wait_gone([server, watcher], _budget())
        assert left == [], f"still running after the owner was killed: {left}"
        assert _own_leftovers(folder) == []
    finally:
        _cleanup(proc, folder, [server, watcher])
    assert _own_leftovers(folder) == []


@pytest.mark.parametrize("kind", _ENGINES)
def test_a_terminated_owner_takes_its_engine_with_it(kind):
    """SIGTERM with no handler, like a timeout's kill: no atexit hook, no ``finally``."""
    proc, folder, server, watcher = _spawn(kind, "hold")
    try:
        assert watcher and _is_watcher(watcher, folder), "no watcher runs for this engine"
        os.kill(proc.pid, signal.SIGTERM)  # the default action: no handler, no cleanup
        proc.wait()
        left = _wait_gone([server, watcher], _budget())
        assert left == [], f"still running after the owner was terminated: {left}"
    finally:
        _cleanup(proc, folder, [server, watcher])
    assert _own_leftovers(folder) == []


@pytest.mark.passes_on_main  # guards the explicit cleanup, which main has; the watcher must not break it
@pytest.mark.parametrize("kind", _ENGINES)
def test_a_normal_stop_leaves_neither_the_engine_nor_its_watcher(kind):
    proc, folder, server, watcher = _spawn(kind, "stop")
    try:
        proc.wait(timeout=_budget())
        assert proc.returncode == 0
        left = _wait_gone([p for p in (server, watcher) if p], _budget())
        assert left == [], f"still running after a normal stop: {left}"
        assert not folder.exists()
    finally:
        _cleanup(proc, folder, [server, watcher])
    assert _own_leftovers(folder) == []


@pytest.mark.parametrize("kind", _ENGINES)
def test_stop_releases_the_watcher_while_the_owner_lives_on(kind):
    """The pipe closing at the owner's exit would hide a ``stop`` that forgot to release it."""
    proc, folder, server, watcher = _spawn(kind, "stop_then_hold")
    try:
        assert watcher and _is_watcher(watcher, folder), "no watcher runs for this engine"
        assert _read_line(proc) == "stopped"
        assert proc.poll() is None, "the owner must still be alive"
        left = _wait_gone([server, watcher], _budget())
        assert left == [], f"still running after stop, with the owner alive: {left}"
    finally:
        _cleanup(proc, folder, [server, watcher])
    assert _own_leftovers(folder) == []


@pytest.mark.parametrize("kind", _ENGINES)
def test_a_failed_start_releases_the_watcher_while_the_owner_lives_on(kind):
    proc, folder_unused, _, watcher = _spawn(kind, "start_fails")
    try:
        assert watcher, "no watcher was started"
        assert _read_line(proc) == "failed"
        assert proc.poll() is None, "the owner must still be alive"
        left = _wait_gone([watcher], _budget())
        assert left == [], f"the watcher outlived the failed start: {left}"
    finally:
        proc.kill()
        proc.wait()
        if watcher and _alive(watcher):
            os.kill(watcher, signal.SIGKILL)


def test_a_watcher_that_cannot_start_is_a_warning_and_the_sweep_stays_the_backstop(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setattr(de.sys, "executable", str(tmp_path / "no-such-python"))
    with caplog.at_level("WARNING", logger=de.logger.name):
        de._start_watcher(tmp_path)
    assert tmp_path not in de._WATCHERS
    assert "no watcher" in caplog.text and "next sweep" in caplog.text
    de._release_watcher(tmp_path)  # nothing to release: no error


@pytest.mark.parametrize("kind", _ENGINES)
def test_an_engine_still_starts_and_stops_without_a_watcher(kind, monkeypatch, tmp_path):
    engine, bin_dir = kind
    monkeypatch.setattr(de.sys, "executable", str(tmp_path / "no-such-python"))
    kw = {"pg_bin_dir": bin_dir} if bin_dir else {}
    handle = de.start(engine, "", newest=True, **kw)
    try:
        assert handle.workdir not in de._WATCHERS
        assert de.execute(handle, "SELECT 1").error is None
    finally:
        de.stop(handle)
    assert not handle.workdir.exists()


def _watcher_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("_engine_watcher_under_test", de._WATCHER_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_watcher_signals_server_programs_only_and_picks_the_engines_own_signal():
    import re

    w = _watcher_module()
    folder = "/tmp/modernizer-engine-abc"
    pattern = re.compile(re.escape(folder) + r"(?![\w.-])")
    mysqld = f"/opt/homebrew/bin/mysqld --no-defaults --datadir={folder}/data"
    postgres = f"/opt/pg 16/bin/postgres -D {folder}/pgdata -c fsync=off"  # a path with a space
    assert w._victim(pattern, mysqld) and w._victim(pattern, postgres)
    assert w._signal_for(mysqld) == signal.SIGTERM
    assert w._signal_for(postgres) == signal.SIGINT  # fast shutdown, not the smart one
    # these name the folder but are nobody's server
    assert not w._victim(pattern, f"tail -f {folder}/mysqld.log")
    assert not w._victim(pattern, f"psql -h {folder} postgres")
    assert not w._victim(pattern, f"vim {folder}/postgres.log")
    assert not w._victim(
        pattern, "/opt/homebrew/bin/mysqld --datadir=/tmp/modernizer-engine-other/data"
    )


def test_the_watcher_logs_a_failure_and_retries(monkeypatch, capsys):
    w = _watcher_module()
    calls = []

    def flaky(folder):
        calls.append(folder)
        if len(calls) < 3:
            raise FileNotFoundError("ps")

    monkeypatch.setattr(w, "stop_folder_servers", flaky)
    monkeypatch.setattr(w.time, "sleep", lambda s: None)
    monkeypatch.setattr(w.os, "read", lambda fd, n: b"")  # the pipe is already closed
    assert w.main(["watcher", "/tmp/x"]) == 0 and len(calls) == 3
    assert "attempt 1 failed" in capsys.readouterr().err
    calls.clear()
    monkeypatch.setattr(w, "stop_folder_servers", lambda f: (_ for _ in ()).throw(OSError("ps")))
    assert w.main(["watcher", "/tmp/x"]) == 1  # gives up, loudly


def test_the_watchers_errors_go_to_a_log_in_the_folder(tmp_path, monkeypatch):
    seen = {}
    real = subprocess.Popen

    def spy(args, **kwargs):
        seen["stderr"] = kwargs.get("stderr")
        return real(args, **kwargs)

    monkeypatch.setattr(de.subprocess, "Popen", spy)
    de._start_watcher(tmp_path)
    try:
        assert getattr(seen["stderr"], "name", None) == str(tmp_path / "watcher.log")
    finally:
        de._release_watcher(tmp_path)
