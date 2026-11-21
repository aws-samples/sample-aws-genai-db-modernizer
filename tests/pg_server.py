# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The one way tests start a PostgreSQL of their own (#2522).

``start_pg_server()`` starts pgserver's bundled PostgreSQL from its ``bin`` folder
through the disposable engine's own server class (``_PgBinaryServer``): bounded
``initdb``, start and shutdown times, a ``UTF8`` cluster, ``trust`` on the Unix
socket in a private ``/tmp`` folder and no TCP listener, an owner record and a
watcher that stop the server if the test process dies. ``pgserver.get_server`` is
never called, because it waits on a lock shared by every process of the user with
no time limit (#2518); ``tests/unit/test_no_pgserver_get_server.py`` keeps it out.

The returned server keeps the parts of pgserver's ``PostgresServer`` the tests use:
``psql(command)`` (the command on stdin, stdout returned, ``CalledProcessError`` on
a non-zero exit), ``get_uri(database)``, ``get_pid()`` and ``cleanup()``.

As root, which PostgreSQL refuses to run as, the server runs as an unprivileged
system user (``pgtest``, made with ``useradd`` when missing), as pgserver did.
"""

from __future__ import annotations

import atexit
import contextlib
import fcntl
import os
import pwd
import shutil
import stat
import subprocess
from pathlib import Path

from src.tools.database import disposable_engine as de

_ROOT_RUN_AS = "pgtest"
_USERADD_LOCK = Path("/tmp/modernizer-pgtest-useradd.lock")  # nosec B108 - root-only, a lock
_USERADD_EXISTS = 9  # useradd's exit status for "username already in use"
_PSQL_TIMEOUT_S = 240.0  # under ci/test.sh's 300 s per-test limit
_LIVE: list[PgTestServer] = []


class PgServerUnavailable(RuntimeError):
    """pgserver (and so its PostgreSQL) is not installed here: the one case a fixture
    may skip on. Any other start failure is an error the test run must show."""


class PgTestServer:
    """A running test PostgreSQL. ``cleanup()`` stops it and deletes its folder."""

    def __init__(self, workdir: Path, server: de._PgBinaryServer, bin_dir: Path) -> None:
        self.workdir = workdir
        self.bin_dir = bin_dir
        self._server = server
        self._stopped = False

    def get_uri(self, database: str | None = None) -> str:
        return f"postgresql://postgres:@/{database or 'postgres'}?host={self.workdir}"

    def get_pid(self) -> int | None:
        return self._server.pid

    def psql(self, command: str) -> str:
        done = subprocess.run(  # nosec B603 - the bundled psql, fixed arguments, no shell
            [str(self.bin_dir / "psql"), self.get_uri(), "-X"],
            input=command.encode(),
            stdout=subprocess.PIPE,
            check=True,
            timeout=_PSQL_TIMEOUT_S,
        )
        return done.stdout.decode("utf-8")

    def cleanup(self) -> None:
        """Stop the server PostgreSQL's way and remove its folder. Safe to call twice."""
        if self._stopped:
            return
        self._stopped = True
        try:
            self._server.stop()
        finally:
            _remove(self.workdir)
            if self in _LIVE:
                _LIVE.remove(self)


def _remove(workdir: Path) -> None:
    de._stop_workdir_servers(workdir)
    shutil.rmtree(workdir, ignore_errors=True)
    de._release_watcher(workdir)


_SEARCH = stat.S_IXGRP | stat.S_IXOTH
_READ = stat.S_IRGRP | stat.S_IROTH


def _permission_plan(bin_dir: Path) -> list[tuple[Path, int]]:
    """The modes to widen so an unprivileged user can run pgserver's install: each path
    with its new mode, only where a bit is missing.

    The install (``bin``, ``lib``, ``share``) gets read, and search or execute where it
    is a folder or a program. Each folder above it gets only the search bit, so the user
    can pass through without listing anything. The data and socket folder is handed to
    the user with ``chown`` instead, so nothing else is opened.
    """
    install = bin_dir.parent
    plan: list[tuple[Path, int]] = []

    def widen(path: Path, extra: int) -> None:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode | extra != mode:
            plan.append((path, mode | extra))

    for folder in reversed(install.parents):
        widen(folder, _SEARCH)
    widen(install, _READ | _SEARCH)
    for path in sorted(install.rglob("*")):
        if path.is_symlink():
            continue  # chmod would follow it; its target is in the tree or not ours
        executable = path.is_dir() or path.stat().st_mode & stat.S_IXUSR
        widen(path, _READ | (_SEARCH if executable else 0))
    return plan


def _readable_by_others(bin_dir: Path) -> None:
    """Apply :func:`_permission_plan`. Root only: a CI container running as root, where
    PostgreSQL runs as an unprivileged user (see :func:`_run_as_for_root`)."""
    for path, mode in _permission_plan(bin_dir):
        path.chmod(mode)


def _ensure_user(name: str) -> pwd.struct_passwd:
    """The system user ``name``, made with ``useradd`` when missing.

    Parallel workers start at once: a file lock lets one of them make the user, and a
    ``useradd`` that finds the name taken (exit 9, another process got there first) is
    success once the user can be read back.
    """
    try:
        return pwd.getpwnam(name)
    except KeyError:
        pass
    with _USERADD_LOCK.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            return pwd.getpwnam(name)
        except KeyError:
            pass
        made = subprocess.run(  # nosec B603 B607 - fixed arguments, root in a CI container
            ["useradd", "-s", "/bin/bash", name], capture_output=True, check=False
        )
        if made.returncode not in (0, _USERADD_EXISTS):
            raise subprocess.CalledProcessError(
                made.returncode, made.args, made.stdout, made.stderr
            )
        return pwd.getpwnam(name)


def _run_as_for_root(workdir: Path, bin_dir: Path) -> str | None:
    if not de._is_root():
        return None
    entry = _ensure_user(_ROOT_RUN_AS)
    _readable_by_others(bin_dir)
    os.chown(workdir, entry.pw_uid, entry.pw_gid)
    return _ROOT_RUN_AS


def start_pg_server() -> PgTestServer:
    """Start pgserver's PostgreSQL from its ``bin`` folder; raise when it cannot start.

    ``PgServerUnavailable`` when pgserver is not installed, the only error a fixture
    skips on; ``de.EngineStartError`` when the server does not start in time, and any
    other error, propagate. Nothing is left running when it fails.
    """
    bin_dir = de._pgserver_bin_dir()
    major = de._pg_bin_major(bin_dir) if bin_dir is not None else None
    if bin_dir is None or major is None:
        raise PgServerUnavailable("pgserver's PostgreSQL bin folder is not installed")
    de.sweep_stale()
    workdir = de._short_tempdir()
    de._start_watcher(workdir)
    try:
        run_as = _run_as_for_root(workdir, bin_dir)
        server = de._PgBinaryServer(workdir, bin_dir, major, run_as=run_as)
    except BaseException:  # includes KeyboardInterrupt: nothing is left running
        _remove(workdir)
        raise
    handle = PgTestServer(workdir, server, bin_dir)
    _LIVE.append(handle)
    return handle


@atexit.register
def _stop_all() -> None:
    for handle in list(_LIVE):
        with contextlib.suppress(Exception):
            handle.cleanup()
