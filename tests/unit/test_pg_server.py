# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``tests/pg_server.py``, the one way tests start a PostgreSQL of their own (#2522).

It starts pgserver's binaries through the disposable engine's server class, so it
never waits on pgserver's user-wide lock (#2518), keeps the settings the fixtures had
from ``get_server`` (UTF8, ``trust`` for ``postgres`` on the socket, pgserver's
extensions), and leaves no server or folder behind.
"""

from __future__ import annotations

import os
import re
import select
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from src.tools.database import disposable_engine as de
from tests import pg_server

pgserver = pytest.importorskip("pgserver")

_HOLD_PGSERVER_LOCK = """
import sys, time
import fasteners
from pgserver.postgres_server import PostgresServer
with fasteners.InterProcessLock(PostgresServer.lock_path):
    print("held", flush=True)
    time.sleep(float(sys.argv[1]))
"""


def _alive(pid: int) -> bool:
    return de._running(pid)


def test_a_server_starts_while_another_process_holds_pgserver_s_lock(monkeypatch):
    def no_get_server(*args, **kwargs):  # a regression fails here at once, not after a wait
        raise AssertionError("pgserver.get_server was called; it waits on a user-wide lock")

    import pgserver.postgres_server as ps

    monkeypatch.setattr(pgserver, "get_server", no_get_server)
    monkeypatch.setattr(ps, "get_server", no_get_server)
    budget = de._INIT_TIMEOUT_S + de._START_TIMEOUT_S
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLD_PGSERVER_LOCK, str(budget)], stdout=subprocess.PIPE, text=True
    )
    try:
        ready, _, _ = select.select([holder.stdout], [], [], 60)
        assert ready and holder.stdout.readline().strip() == "held"
        srv = pg_server.start_pg_server()
        try:
            assert holder.poll() is None, "the start waited for pgserver's lock"
            out = srv.psql(
                "\\set ON_ERROR_STOP on\n\\t on\n\\a\n"
                "SELECT current_setting('server_encoding'), current_user, length(U&'\\00E9');\n"
                "CREATE EXTENSION vector;\n"
            )
            assert "UTF8|postgres|1" in out
            hba = (srv.workdir / "pgdata" / "pg_hba.conf").read_text()
            assert re.search(r"^local\s+all\s+all\s+trust", hba, re.M)
            assert not re.search(r"^host\s+\S+\s+\S+\s+\S+\s+trust", hba, re.M)
        finally:
            srv.cleanup()
    finally:
        holder.kill()
        holder.wait()


def test_cleanup_stops_the_server_and_removes_its_folder_and_can_run_twice():
    srv = pg_server.start_pg_server()
    pid, workdir = srv.get_pid(), srv.workdir
    assert pid is not None and _alive(pid)
    assert srv.get_uri("shop") == f"postgresql://postgres:@/shop?host={workdir}"
    srv.cleanup()
    srv.cleanup()
    assert not _alive(pid)
    assert not workdir.exists()
    assert srv not in pg_server._LIVE


def test_psql_raises_on_a_failed_command_as_pgserver_s_did():
    srv = pg_server.start_pg_server()
    try:
        with pytest.raises(subprocess.CalledProcessError):
            srv.psql("\\set ON_ERROR_STOP on\nSELECT no_such_column;\n")
        assert srv.psql("SELECT 41 + 1;\n").split()[2] == "42"
    finally:
        srv.cleanup()


@pytest.mark.passes_on_main
def test_a_start_that_fails_leaves_nothing_behind(monkeypatch):
    made: list[Path] = []
    real = de._short_tempdir

    def tracked() -> Path:
        made.append(real())
        return made[-1]

    def refuse(*args, **kwargs):
        raise de.EngineStartError("postgresql 16 is installed but did not start: test")

    monkeypatch.setattr(de, "_short_tempdir", tracked)
    monkeypatch.setattr(de, "_PgBinaryServer", refuse)
    with pytest.raises(de.EngineStartError) as raised:
        pg_server.start_pg_server()
    # Not the error the fixtures skip on: a server that does not start fails the run.
    assert not isinstance(raised.value, pg_server.PgServerUnavailable)
    assert made and not made[0].exists()
    assert made[0] not in de._WATCHERS


@pytest.mark.passes_on_main
def test_no_pgserver_install_is_an_error_the_fixtures_can_skip_on(monkeypatch):
    monkeypatch.setattr(de, "_pgserver_bin_dir", lambda: None)
    with pytest.raises(pg_server.PgServerUnavailable, match="pgserver"):
        pg_server.start_pg_server()


@pytest.mark.passes_on_main
def test_as_root_the_server_runs_as_an_unprivileged_user(monkeypatch, tmp_path):
    """PostgreSQL refuses root and one CI image runs as root: as pgserver did, the
    helper hands the folder to a plain system user and runs every binary as it."""
    seen: dict[str, object] = {}
    monkeypatch.setattr(de, "_is_root", lambda: True)
    monkeypatch.setattr(pg_server, "_readable_by_others", lambda bin_dir: None)
    monkeypatch.setattr(os, "chown", lambda path, uid, gid: seen.update(chown=(path, uid, gid)))

    class Entry:
        pw_uid, pw_gid = 4321, 4322

    import pwd

    monkeypatch.setattr(pwd, "getpwnam", lambda name: seen.update(user=name) or Entry())

    def server(workdir, bin_dir, major, *, run_as=None):
        seen["run_as"] = run_as
        raise de.EngineStartError("stop here")

    monkeypatch.setattr(de, "_PgBinaryServer", server)
    with pytest.raises(de.EngineStartError, match="stop here"):
        pg_server.start_pg_server()
    assert seen["user"] == seen["run_as"] == "pgtest"
    assert seen["chown"][1:] == (4321, 4322)


def test_the_server_class_runs_every_binary_as_run_as(monkeypatch, tmp_path):
    """initdb, postgres and pg_ctl all get ``user=run_as``; without it, ``user=None``."""
    calls: list[tuple[str, object]] = []

    class Proc:
        pid = 2**30  # never a live process
        done = False

        def poll(self):
            return 0 if self.done else None

    proc = Proc()

    def run(args, **kwargs):
        calls.append((Path(args[0]).name, kwargs.get("user")))
        if args[-1] == "stop":
            proc.done = True
        return subprocess.CompletedProcess(args, 0, b"", b"")

    def popen(args, **kwargs):
        calls.append((Path(args[0]).name, kwargs.get("user")))
        return proc

    monkeypatch.setattr(de.subprocess, "run", run)
    monkeypatch.setattr(de.subprocess, "Popen", popen)
    monkeypatch.setattr(de._PgBinaryServer, "_wait", lambda self, major: None)
    for run_as in ("pgtest", None):
        calls.clear()
        proc.done = False
        workdir = tmp_path / str(run_as)
        workdir.mkdir()
        de._PgBinaryServer(workdir, tmp_path / "bin", "16", run_as=run_as).stop()
        assert calls == [("initdb", run_as), ("postgres", run_as), ("pg_ctl", run_as)]


class _Entry:
    pw_uid, pw_gid = 4321, 4322


def _fake_users(monkeypatch, tmp_path, *, exists_after: int, useradd_code: int):
    """``getpwnam`` fails ``exists_after`` times, then finds the user; ``useradd``
    exits with ``useradd_code``. Returns the list of ``useradd`` calls."""
    import pwd

    lookups = {"n": 0}
    useradds: list[list[str]] = []

    def getpwnam(name):
        lookups["n"] += 1
        if lookups["n"] <= exists_after:
            raise KeyError(name)
        return _Entry()

    def run(args, **kwargs):
        useradds.append(list(args))
        return subprocess.CompletedProcess(args, useradd_code, b"", b"useradd: taken")

    monkeypatch.setattr(pwd, "getpwnam", getpwnam)
    monkeypatch.setattr(pg_server.subprocess, "run", run)
    monkeypatch.setattr(pg_server, "_USERADD_LOCK", tmp_path / "useradd.lock")
    return useradds


@pytest.mark.passes_on_main
def test_a_missing_user_is_made_with_useradd_under_the_lock(monkeypatch, tmp_path):
    useradds = _fake_users(monkeypatch, tmp_path, exists_after=2, useradd_code=0)
    assert pg_server._ensure_user("pgtest").pw_uid == 4321
    assert useradds == [["useradd", "-s", "/bin/bash", "pgtest"]]
    assert (tmp_path / "useradd.lock").exists()


@pytest.mark.passes_on_main
def test_a_user_made_by_another_worker_first_is_found_without_useradd(monkeypatch, tmp_path):
    """The second worker waits on the lock, then reads the user the first one made."""
    useradds = _fake_users(monkeypatch, tmp_path, exists_after=1, useradd_code=0)
    assert pg_server._ensure_user("pgtest").pw_uid == 4321
    assert useradds == []


@pytest.mark.passes_on_main
def test_useradd_finding_the_name_taken_is_success(monkeypatch, tmp_path):
    """Exit 9: a process outside the lock made it between the check and useradd."""
    useradds = _fake_users(monkeypatch, tmp_path, exists_after=2, useradd_code=9)
    assert pg_server._ensure_user("pgtest").pw_gid == 4322
    assert len(useradds) == 1


@pytest.mark.passes_on_main
def test_any_other_useradd_failure_is_raised(monkeypatch, tmp_path):
    _fake_users(monkeypatch, tmp_path, exists_after=99, useradd_code=1)
    with pytest.raises(subprocess.CalledProcessError):
        pg_server._ensure_user("pgtest")


@pytest.mark.passes_on_main
def test_workers_starting_together_make_the_user_once(monkeypatch, tmp_path):
    """Two threads, each with its own lock file handle, as two workers would have."""
    import pwd
    import threading
    import time

    made = threading.Event()
    useradds: list[str] = []

    def getpwnam(name):
        if not made.is_set():
            raise KeyError(name)
        return _Entry()

    def run(args, **kwargs):
        useradds.append(args[-1])
        time.sleep(0.2)  # the other thread arrives meanwhile and waits on the lock
        made.set()
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(pwd, "getpwnam", getpwnam)
    monkeypatch.setattr(pg_server.subprocess, "run", run)
    monkeypatch.setattr(pg_server, "_USERADD_LOCK", tmp_path / "useradd.lock")
    errors: list[BaseException] = []

    def worker():
        try:
            pg_server._ensure_user("pgtest")
        except BaseException as exc:  # noqa: BLE001 -- reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert not errors and useradds == ["pgtest"]


@pytest.mark.passes_on_main
def test_the_install_is_opened_to_others_and_its_parents_only_for_search(tmp_path):
    parent = tmp_path / "venv" / "site-packages"
    install = parent / "pgserver" / "pginstall"
    for sub in ("bin", "lib", "share/extension"):
        (install / sub).mkdir(parents=True)
    (install / "bin" / "postgres").write_text("")
    (install / "bin" / "postgres").chmod(0o700)
    (install / "lib" / "libpq.so").write_text("")
    (install / "lib" / "libpq.so").chmod(0o600)
    (install / "share" / "extension" / "vector.control").write_text("")
    (install / "share" / "extension" / "vector.control").chmod(0o600)
    (install / "lib" / "link.so").symlink_to(install / "lib" / "libpq.so")
    for folder in [tmp_path / "venv", parent, parent / "pgserver", install,
                   *(p for p in install.rglob("*") if p.is_dir())]:  # fmt: skip
        folder.chmod(0o700)
    plan = dict(pg_server._permission_plan(install / "bin"))
    above = {p: m for p, m in plan.items() if not p.is_relative_to(install)}
    # Folders above the install: the search bit only, never read.
    assert above[parent] == 0o711 and above[tmp_path / "venv"] == 0o711
    assert all(not m & (stat.S_IRGRP | stat.S_IROTH) for p, m in above.items() if p != install)
    assert plan[install] == plan[install / "bin"] == plan[install / "share"] == 0o755
    assert plan[install / "bin" / "postgres"] == 0o755
    assert plan[install / "lib" / "libpq.so"] == 0o644  # read, not execute
    assert plan[install / "share" / "extension" / "vector.control"] == 0o644
    assert install / "lib" / "link.so" not in plan
    # Applied inside the test folder only (the folders above it are the user's own).
    for path, mode in plan.items():
        if path.is_relative_to(tmp_path):
            path.chmod(mode)
    assert stat.S_IMODE((install / "lib" / "libpq.so").stat().st_mode) == 0o644
    assert pg_server._permission_plan(install / "bin") == [
        (p, m) for p, m in pg_server._permission_plan(install / "bin")
        if not p.is_relative_to(tmp_path)
    ]  # fmt: skip
