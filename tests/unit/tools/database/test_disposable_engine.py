# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Disposable source engines (#1943).

The round-trip tests start a real throwaway server on a Unix socket, load a synthetic
table and a routine, call it, read the table back and stop it, once for every major
present: ``pgserver`` (PostgreSQL 16) and any ``MODERNIZER_PG_BIN_<major>`` for
PostgreSQL, any ``MODERNIZER_MYSQLD_<major>`` or a ``mysqld`` on ``PATH`` for MySQL.
The binary-path PostgreSQL tests use ``pgserver``'s own ``bin`` folder, so they run
wherever the dev extra is installed, CI included. The MySQL ones are marked
``integration``, because the gated CI run has no ``mysqld`` and turns a skip into a
failure.
"""

from __future__ import annotations

import contextlib
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from src.tools.database import disposable_engine as de

_PG_BIN = de._pgserver_bin_dir()
_PG = bool(de.available_majors("postgresql"))
_MY = bool(de.available_majors("mysql"))
needs_pg = pytest.mark.skipif(
    not _PG, reason="no PostgreSQL binary (pgserver or MODERNIZER_PG_BIN)"
)
_CREATED: list[Path] = []  # the folders this test's starts created (xdist-safe: per test)


@pytest.fixture(autouse=True)
def _track_workdirs(monkeypatch):
    """Remember each folder a start creates, so a test checks its own and never ``/tmp``.

    Under ``-n auto`` other workers create and remove folders at the same time, so a
    global listing of ``/tmp`` or of the shared-memory table says nothing about one test.
    """
    _CREATED.clear()
    real = de._short_tempdir

    def tracked() -> Path:
        path: Path = real()
        _CREATED.append(path)
        return path

    monkeypatch.setattr(de, "_short_tempdir", tracked)
    yield
    for path in _CREATED:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def not_root(monkeypatch):
    """For a test of a different error: take the root refusal out of the way.

    The mirror's gated ``test`` job runs as root, where the refusal would come first.
    """
    monkeypatch.setattr(de, "_is_root", lambda: False)


def _leftovers() -> list[Path]:
    return [p for p in _CREATED if p.exists()]


def _as_root() -> bool:
    return os.geteuid() == 0


def _refused_as_root(engine: str = "postgresql", major: str = "16") -> bool:
    """As root every start is refused; assert that rather than skip, because a gated
    run turns a skip into a failure and one CI image runs as root."""
    if not _as_root():
        return False
    with pytest.raises(de.EngineStartError, match="running as root"):
        de.start(engine, major)
    return True


def _alive(pid: int) -> bool:
    return de._running(pid)


def _gone(pid: int) -> bool:
    for _ in range(100):
        if not _alive(pid):
            return True
        time.sleep(0.1)
    return False


def _shm_ids() -> set[int] | None:
    """The System V shared-memory segment ids on this machine, or None where unreadable."""
    try:  # Linux
        rows = Path("/proc/sysvipc/shm").read_text().splitlines()[1:]
        return {int(r.split()[1]) for r in rows if len(r.split()) > 1}
    except OSError:
        pass
    if shutil.which("ipcs"):
        out = subprocess.run(["ipcs", "-m"], capture_output=True, text=True, check=False).stdout
        return {int(line.split()[1]) for line in out.splitlines() if line.startswith("m ")}
    return None


def _segment_id(workdir: Path) -> int | None:
    """This server's own segment, from line 7 of ``postmaster.pid`` ("key id")."""
    try:
        return int((workdir / "pgdata" / "postmaster.pid").read_text().splitlines()[6].split()[1])
    except (OSError, IndexError, ValueError):
        return None


PG_DDL = [
    "CREATE TABLE items (id int PRIMARY KEY, qty int NOT NULL)",
    "CREATE TABLE audit (item int, note text)",
]
PG_ROUTINES = [
    de.SourceRoutine(
        "add_one",
        "function",
        "CREATE FUNCTION add_one(x int) RETURNS int LANGUAGE sql AS $$ SELECT x + 1 $$",
    ),
    de.SourceRoutine(
        "restock",
        "procedure",
        "CREATE PROCEDURE restock(i int, n int) LANGUAGE plpgsql AS $$ BEGIN "
        "UPDATE items SET qty = qty + n WHERE id = i; INSERT INTO audit VALUES (i, 'restock'); END $$",
    ),
]
MY_ROUTINES = [
    de.SourceRoutine(
        "add_one",
        "function",
        "CREATE FUNCTION add_one(x INT) RETURNS INT DETERMINISTIC RETURN x + 1",
    ),
    de.SourceRoutine(
        "restock",
        "procedure",
        "CREATE PROCEDURE restock(i INT, n INT) BEGIN "
        "UPDATE items SET qty = qty + n WHERE id = i; INSERT INTO audit VALUES (i, 'restock'); END",
    ),
]
ROWS = {"items": [{"id": 1, "qty": 5}, {"id": 2, "qty": 7}]}


def _round_trip(engine: str, routines: list[de.SourceRoutine], major: str, **kwargs) -> None:
    with de.running(engine, major, **kwargs) as h:
        assert h.version == major
        de.load(h, PG_DDL, ROWS, routines)
        assert de.call(h, "add_one", [41]).rows == [[42]]
        assert de.call(h, "restock", [1, 10]).error is None
        snap = de.snapshot(h, ["items", "audit"])
        assert snap["items"] == [{"id": 1, "qty": 15}, {"id": 2, "qty": 7}]
        assert snap["audit"] == [{"item": 1, "note": "restock"}]
        # a SQL error is data, so the comparison can match it against the carried routine
        assert de.call(h, "add_one", ["not a number"]).error
        assert h.endpoint.startswith("unix:")
        pid, folder = h.pid, h.workdir
    assert not folder.exists()
    assert pid is not None and _gone(pid)


def _each(engine: str) -> list:
    majors = de.available_majors(engine)
    if not majors:
        return [pytest.param(None, marks=pytest.mark.skip(reason=f"no {engine} binary found"))]
    return [pytest.param(m, id=m) for m in majors]


@pytest.mark.parametrize("major", _each("postgresql"))
def test_postgresql_start_load_call_snapshot_stop(major):
    """Once for every PostgreSQL major present (pgserver's 16, MODERNIZER_PG_BIN_<major>, PATH)."""
    if _refused_as_root("postgresql", major):
        return
    _round_trip("postgresql", PG_ROUTINES, major)


@pytest.mark.integration
@pytest.mark.parametrize("major", _each("mysql"))
def test_mysql_start_load_call_snapshot_stop(major):
    """Once for every MySQL major present (MODERNIZER_MYSQLD_<major>, PATH)."""
    if _refused_as_root("mysql", major):
        return
    _round_trip("mysql", MY_ROUTINES, major)


# --------------------------------------------------------------- the binary path, on pgserver's own bin

pg_binary_path = pytest.mark.skipif(_PG_BIN is None, reason="pgserver's bin folder is needed")


@pg_binary_path
def test_the_binary_path_runs_a_full_round_trip():
    """``initdb`` plus ``postgres``, not pgserver's own start, with pgserver's binaries."""
    major = de._pg_bin_major(_PG_BIN)
    if _refused_as_root("postgresql", major):
        return
    _round_trip("postgresql", PG_ROUTINES, major, pg_bin_dir=str(_PG_BIN))


@pg_binary_path
def test_the_binary_path_leaves_no_shared_memory_segment(monkeypatch):
    """A SIGKILLed postmaster leaks its System V segment, and macOS allows only 32."""
    if _refused_as_root("postgresql", de._pg_bin_major(_PG_BIN)):
        return
    sigs: list[signal.Signals] = []
    real = de._reap

    def spy(proc, first=signal.SIGTERM):
        sigs.append(first)
        return real(proc, first)

    monkeypatch.setattr(de, "_reap", spy)
    runs: list[list[str]] = []
    real_run = de.subprocess.run

    def run_spy(args, **kwargs):
        runs.append([str(a) for a in args])
        return real_run(args, **kwargs)

    monkeypatch.setattr(de.subprocess, "run", run_spy)
    with de.running("postgresql", de._pg_bin_major(_PG_BIN), pg_bin_dir=str(_PG_BIN)) as h:
        assert isinstance(h._backend._server, de._PgBinaryServer)
        segment = _segment_id(h.workdir)
        if _shm_ids() is not None:
            assert segment in _shm_ids()  # this server's segment exists while it runs
    assert any(a[0].endswith("pg_ctl") and "fast" in a and "stop" in a for a in runs)
    assert sigs == [signal.SIGINT], "fast shutdown first, never a plain SIGKILL"
    if _shm_ids() is not None:
        assert segment not in _shm_ids(), "this server's segment leaked"


@pg_binary_path
def test_the_binary_path_trusts_only_the_socket():
    if _refused_as_root("postgresql", de._pg_bin_major(_PG_BIN)):
        return
    major = de._pg_bin_major(_PG_BIN)
    with de.running("postgresql", major, pg_bin_dir=str(_PG_BIN)) as h:
        hba = (h.workdir / "pgdata" / "pg_hba.conf").read_text()
        assert not re.search(r"^host\s+\S+\s+\S+\s+\S+\s+trust", hba, re.M)
        assert re.search(r"^local\s+all\s+all\s+trust", hba, re.M)
        with h._backend._conn.cursor() as cur:
            cur.execute("SHOW listen_addresses")
            assert cur.fetchone()[0] == ""


# --------------------------------------------------------------- privileges


@needs_pg
def test_postgresql_routines_do_not_run_as_a_superuser():
    if _refused_as_root():
        return
    with de.running("postgresql", de.available_majors("postgresql")[0]) as h:
        with h._backend._conn.cursor() as cur:
            cur.execute("SELECT current_user, rolsuper FROM pg_roles WHERE rolname = current_user")
            user, is_super = cur.fetchone()
        assert user == de._RUNNER and is_super is False
        # COPY TO PROGRAM is local code execution as the OS user: it must be refused
        target = h.workdir / "pwned"
        routine = de.SourceRoutine(
            "escape",
            "function",
            "CREATE FUNCTION escape() RETURNS void LANGUAGE plpgsql AS $$ BEGIN "
            f"COPY (SELECT 1) TO PROGRAM 'touch {target}'; END $$",
        )
        de.load(h, [], {}, [routine])
        error = de.call(h, "escape").error
        assert error and ("permission denied" in error.lower() or "superuser" in error.lower())
        assert not target.exists()


@pytest.mark.integration
@pytest.mark.skipif(not _MY, reason="no mysqld binary")
def test_mysql_routines_do_not_run_as_an_admin_user():
    if _refused_as_root("mysql", de.available_majors("mysql")[0]):
        return
    with de.running("mysql", de.available_majors("mysql")[0]) as h:
        cur = h._backend._conn.cursor()
        cur.execute("SELECT current_user()")
        assert cur.fetchone()[0].startswith(de._RUNNER + "@")
        cur.execute("SHOW GRANTS FOR CURRENT_USER()")
        grants = " ".join(str(r[0]) for r in cur.fetchall())
        assert "FILE" not in grants and "SUPER" not in grants
        cur.execute("SELECT @@secure_file_priv")
        assert "no-export" in str(cur.fetchone()[0])
        cur.close()
        target = h.workdir / "pwned"
        routine = de.SourceRoutine(
            "escape",
            "procedure",
            f"CREATE PROCEDURE escape() BEGIN SELECT 1 INTO OUTFILE '{target}'; END",
        )
        error = None
        try:
            de.load(h, [], {}, [routine])
        except de.EngineError as exc:  # refused at CREATE time on some builds
            error = str(exc)
        else:
            error = de.call(h, "escape").error
        assert error and ("FILE" in error or "denied" in error.lower())
        assert not target.exists()


# --------------------------------------------------------------- no orphans


class _Boom(RuntimeError):
    pass


@pg_binary_path
@pytest.mark.parametrize("failure", [_Boom, KeyboardInterrupt])
def test_a_failure_after_the_server_is_up_leaves_no_process(monkeypatch, failure):
    """The review found a postmaster still running after a Ctrl-C in ``start``."""
    if _refused_as_root("postgresql", de._pg_bin_major(_PG_BIN)):
        return
    major = de._pg_bin_major(_PG_BIN)
    pids: list[int] = []
    real_wait = de._PgBinaryServer._wait

    def wait_then_fail(self, *args, **kwargs):
        real_wait(self, *args, **kwargs)  # the server is really up now
        pids.append(self.pid)
        raise failure()

    monkeypatch.setattr(de._PgBinaryServer, "_wait", wait_then_fail)
    with pytest.raises(failure):
        de.start("postgresql", major, pg_bin_dir=str(_PG_BIN))
    assert pids and _gone(pids[0])
    assert not _leftovers()
    assert not de._LIVE


@pg_binary_path
def test_a_failed_connection_after_the_server_is_up_leaves_no_process(monkeypatch):
    if _refused_as_root("postgresql", de._pg_bin_major(_PG_BIN)):
        return
    major = de._pg_bin_major(_PG_BIN)
    pids: list[int] = []
    real_init = de._PostgresBackend.__init__

    def fail_to_connect(self, server):
        pids.append(server.pid)
        raise _Boom("no connection")

    monkeypatch.setattr(de._PostgresBackend, "__init__", fail_to_connect)
    with pytest.raises(_Boom):
        de.start("postgresql", major, pg_bin_dir=str(_PG_BIN))
    monkeypatch.setattr(de._PostgresBackend, "__init__", real_init)
    assert pids and pids[0] is not None and _gone(pids[0])
    assert not _leftovers()


# --------------------------------------------------------------- the listener settings, with no server


class _FakeProc:
    """A process that is already gone, so nothing is signalled and nothing starts."""

    def __init__(self, args, **kwargs):
        self.args = args
        self.pid = os.getpid()  # never signalled: poll() reports it exited

    def poll(self):
        return 1

    def wait(self, timeout=None):
        return 1


def _record_popen(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_popen(args, **kwargs):
        calls.append([str(a) for a in args])
        return _FakeProc(args)

    monkeypatch.setattr(de.subprocess, "Popen", fake_popen)
    return calls


def test_postgres_is_started_with_no_tcp_listener(monkeypatch, tmp_path):
    monkeypatch.setattr(
        de.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, b"", b"")
    )
    calls = _record_popen(monkeypatch)
    with pytest.raises(de.EngineStartError):
        de._PgBinaryServer(tmp_path, Path("/nowhere/bin"), "16")
    args = calls[0]
    assert "listen_addresses=" in args
    assert f"unix_socket_directories={tmp_path}" in args
    assert not any("0.0.0.0" in a or a.endswith("*") or "listen_addresses=*" in a for a in args)
    assert "shared_memory_type=mmap" in args


def test_initdb_rejects_every_host_line(monkeypatch, tmp_path):
    seen: list[list[str]] = []

    def fake_run(args, **kwargs):
        seen.append([str(a) for a in args])
        raise subprocess.CalledProcessError(1, args, b"", b"initdb: error: stop here")

    monkeypatch.setattr(de.subprocess, "run", fake_run)
    with pytest.raises(de.EngineStartError):
        de._PgBinaryServer(tmp_path, Path("/nowhere/bin"), "16")
    assert "--auth-local=trust" in seen[0] and "--auth-host=reject" in seen[0]
    assert "-A" not in seen[0] and "trust" not in seen[0]
    assert "--no-sync" in seen[0]


def test_postgres_before_10_is_started_without_shared_memory_type(monkeypatch, tmp_path):
    monkeypatch.setattr(
        de.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, b"", b"")
    )
    calls = _record_popen(monkeypatch)
    with pytest.raises(de.EngineStartError):
        de._PgBinaryServer(tmp_path, Path("/nowhere/bin"), "9.6")
    assert not any("shared_memory_type" in a for a in calls[0])


def test_mysqld_is_started_without_networking_and_without_file_access(monkeypatch, tmp_path):
    monkeypatch.setattr(
        de.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, b"", b"")
    )
    calls = _record_popen(monkeypatch)
    with pytest.raises(de.EngineStartError):
        de._MySQLServer(tmp_path, "/nowhere/mysqld", "8.0")
    args = calls[0]
    assert "--skip-networking" in args
    assert f"--secure-file-priv={tmp_path / 'no-export'}" in args
    assert "--local-infile=0" in args and "--skip-log-bin" in args
    assert "--event-scheduler=OFF" in args
    assert not any("bind-address" in a and "127.0.0.1" not in a for a in args)


# --------------------------------------------------------------- the version is never guessed


def test_a_version_is_required():
    with pytest.raises(ValueError, match="needs the source's major version"):
        de.start("postgresql")
    with pytest.raises(ValueError, match="available_majors"):
        de.available("postgresql", "")


@needs_pg
def test_newest_is_explicit_and_the_picked_major_is_reported(caplog):
    if _refused_as_root():
        return
    newest = de.available_majors("postgresql")[-1]
    with caplog.at_level("INFO", logger=de.logger.name):
        with de.running("postgresql", newest=True) as h:
            assert h.version == newest
    assert f"postgresql {newest}" in caplog.text


@needs_pg
def test_matching_major_starts_and_other_major_is_unavailable():
    if _refused_as_root():
        return
    majors = de.available_majors("postgresql")
    major = "16" if "16" in majors else majors[0]
    with de.running("postgresql", f"{major}.2") as h:
        assert h.version == major
    other = "9.4"  # nothing offers PostgreSQL 9.4
    with pytest.raises(de.EngineUnavailable, match=f"no engine for postgresql {other}") as err:
        de.start("postgresql", other)
    assert "only " in str(err.value) and major in str(err.value)
    assert not _leftovers()


def test_running_as_root_is_refused(monkeypatch):
    monkeypatch.setattr(de, "_is_root", lambda: True)
    monkeypatch.setattr(de, "available_majors", lambda *a, **k: ["16"])  # one is present
    with pytest.raises(de.EngineStartError, match="running as root") as err:
        de.start("postgresql", "16")
    assert not isinstance(err.value, de.EngineUnavailable), "not a missing version"
    assert "no engine" not in str(err.value)
    assert not _leftovers()


def test_a_missing_major_is_unavailable_even_as_root(monkeypatch):
    """#2066: as root, a major nobody has is "no engine", not a start failure."""
    monkeypatch.setattr(de, "_is_root", lambda: True)
    monkeypatch.setattr(de, "available_majors", lambda *a, **k: ["16"])
    with pytest.raises(de.EngineUnavailable, match="no engine for postgresql 9.6"):
        de.start("postgresql", "9.6")


# --------------------------------------------------------------- binary discovery


def _fake_pg_bin(folder: Path, version: str) -> Path:
    folder.mkdir(parents=True)
    (folder / "postgres").write_text(f"#!/bin/sh\necho 'postgres (PostgreSQL) {version}'\n")
    (folder / "initdb").write_text("#!/bin/sh\nexit 1\n")
    for f in ("postgres", "initdb"):
        (folder / f).chmod(0o755)
    return folder


def _fake_mysqld(path: Path, version: str) -> Path:
    path.write_text(
        f"#!/bin/sh\necho 'mysqld  Ver {version} for Linux on x86_64 "
        "(MySQL Community Server - GPL)'\n"
    )
    path.chmod(0o755)
    return path


def _clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith(("MODERNIZER_PG_BIN", "MODERNIZER_MYSQLD")):
            monkeypatch.delenv(key)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")


def test_postgresql_majors_come_from_per_major_folders(tmp_path, monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("MODERNIZER_PG_BIN_14", str(_fake_pg_bin(tmp_path / "a", "14.9")))
    monkeypatch.setenv("MODERNIZER_PG_BIN_12", str(_fake_pg_bin(tmp_path / "b", "12.18")))
    extra = _fake_pg_bin(tmp_path / "c", "17.2")
    found = de.available_majors("postgresql", pg_bin_dir=str(extra))
    assert {"12", "14", "17"} <= set(found)
    assert set(found) - {"12", "14", "17"} <= {"16"}  # only pgserver's, when installed
    assert found == sorted(found, key=int)
    assert de.available("postgresql", "14.1", pg_bin_dir=str(extra))
    assert not de.available("postgresql", "15", pg_bin_dir=str(extra))
    assert de.pg_candidates()["14"][0] == "MODERNIZER_PG_BIN_14"


def test_a_folder_without_initdb_is_not_an_engine(tmp_path, monkeypatch, caplog):
    _clean_env(monkeypatch)
    folder = _fake_pg_bin(tmp_path / "x", "13.1")
    (folder / "initdb").unlink()
    with caplog.at_level("WARNING", logger=de.logger.name):
        assert "13" not in de.available_majors("postgresql", pg_bin_dir=str(folder))
    assert "no usable initdb and postgres" in caplog.text


def test_a_broken_per_major_folder_warns_before_pgserver_takes_over(tmp_path, monkeypatch, caplog):
    _clean_env(monkeypatch)
    broken = tmp_path / "broken"
    broken.mkdir()
    monkeypatch.setenv("MODERNIZER_PG_BIN_16", str(broken))
    with caplog.at_level("WARNING", logger=de.logger.name):
        found = de.pg_candidates()
    assert "MODERNIZER_PG_BIN_16" in caplog.text
    assert found.get("16", ("pgserver", None))[0] == "pgserver"


def test_a_per_major_variable_pointing_at_another_major_is_refused(tmp_path, monkeypatch, caplog):
    _clean_env(monkeypatch)
    monkeypatch.setenv("MODERNIZER_PG_BIN_14", str(_fake_pg_bin(tmp_path / "wrong", "16.4")))
    monkeypatch.setenv("MODERNIZER_MYSQLD_5_7", str(_fake_mysqld(tmp_path / "my", "8.0.36")))
    with caplog.at_level("WARNING", logger=de.logger.name):
        assert "14" not in de.available_majors("postgresql")
        assert "5.7" not in de.available_majors("mysql")
    assert "names major 14 but postgres --version reports 16" in caplog.text
    assert "names major 5.7 but mysqld --version reports 8.0" in caplog.text


def test_an_explicit_argument_wins_over_the_environment_for_both_engines(tmp_path, monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("MODERNIZER_PG_BIN_16", str(_fake_pg_bin(tmp_path / "env", "16.1")))
    monkeypatch.setenv("MODERNIZER_MYSQLD_8_0", str(_fake_mysqld(tmp_path / "env80", "8.0.1")))
    arg_pg = _fake_pg_bin(tmp_path / "arg", "16.4")
    arg_my = _fake_mysqld(tmp_path / "arg80", "8.0.9")
    assert de.pg_candidates(str(arg_pg))["16"] == ("pg_bin_dir", arg_pg)
    assert de.mysqld_candidates(str(arg_my))["8.0"] == str(arg_my)


def test_per_major_folder_beats_pgserver_for_the_same_major(tmp_path, monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("MODERNIZER_PG_BIN_16", str(_fake_pg_bin(tmp_path / "s", "16.4")))
    assert de.pg_candidates()["16"][1] == tmp_path / "s"


@pytest.mark.parametrize(
    ("engine", "version", "major"),
    [
        ("postgresql", "16.4", "16"),
        ("postgresql", "9.6.24", "9.6"),
        ("postgresql", "9.5", "9.5"),
        ("postgresql", "10.23", "10"),
        ("mysql", "8.0.36", "8.0"),
        ("mysql", "8.4", "8.4"),
        ("mysql", "5.7.44", "5.7"),
        ("mysql", "8.0.mysql_aurora.3.05.2", "8.0"),
    ],
)
def test_majors_are_parsed_per_engine(engine, version, major):
    assert de._major(engine, version) == major


def test_postgresql_9_majors_are_told_apart(tmp_path, monkeypatch):
    _clean_env(monkeypatch)
    assert de._pg_bin_major(_fake_pg_bin(tmp_path / "a", "9.6.24")) == "9.6"
    assert de._pg_bin_major(_fake_pg_bin(tmp_path / "b", "9.5.25")) == "9.5"
    monkeypatch.setenv("MODERNIZER_PG_BIN_9_5", str(tmp_path / "b"))
    assert "9.5" in de.available_majors("postgresql")
    assert not de.available("postgresql", "9.6", pg_bin_dir=str(tmp_path / "b"))


def test_mysql_majors_come_from_per_major_binaries(tmp_path, monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("MODERNIZER_MYSQLD_8_0", str(_fake_mysqld(tmp_path / "m80", "8.0.36")))
    monkeypatch.setenv("MODERNIZER_MYSQLD_5_7", str(_fake_mysqld(tmp_path / "m57", "5.7.44")))
    single = _fake_mysqld(tmp_path / "m84", "8.4.1")
    assert de.available_majors("mysql", mysqld_path=str(single)) == ["5.7", "8.0", "8.4"]
    assert de.available("mysql", "8.0.36", mysqld_path=str(single))
    assert not de.available("mysql", "9.0", mysqld_path=str(single))


def test_mariadb_is_not_taken_for_mysql(tmp_path, monkeypatch, not_root):
    _clean_env(monkeypatch)
    fake = _fake_mysqld(tmp_path / "mysqld", "10.11.6-MariaDB")
    fake.write_text("#!/bin/sh\necho 'mysqld  Ver 10.11.6-MariaDB for linux'\n")
    fake.chmod(0o755)
    assert not de.available("mysql", "10.11", mysqld_path=str(fake))
    with pytest.raises(de.EngineUnavailable, match="no engine for mysql"):
        de.start("mysql", "8.0", mysqld_path=str(fake))


def test_mysqld_path_comes_from_the_environment(tmp_path, monkeypatch):
    _clean_env(monkeypatch)
    fake = tmp_path / "mysqld"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv(de.MYSQLD_ENV, str(fake))
    assert de.find_mysqld() == str(fake)
    assert de.find_mysqld("/nonexistent/mysqld") is None


def test_missing_mysqld_reports_no_engine_and_leaves_nothing(tmp_path, monkeypatch, not_root):
    _clean_env(monkeypatch)
    with pytest.raises(de.EngineUnavailable, match="no engine for mysql 8.0"):
        de.start("mysql", "8.0", mysqld_path=str(tmp_path / "absent"))
    assert not _leftovers()


def test_unknown_engine_is_unavailable():
    with pytest.raises(de.EngineUnavailable, match="no engine for oracle 19"):
        de.start("oracle", "19")
    assert de.available_majors("oracle") == []


def test_no_binary_at_all_says_how_to_get_one(monkeypatch, not_root):
    _clean_env(monkeypatch)
    with pytest.raises(
        de.EngineUnavailable, match="conda install -c conda-forge mysql-server=<major>"
    ):
        de.start("mysql", "8.4")
    assert de.available_majors("mysql") == []


# --------------------------------------------------------------- an installed engine that fails to start


def test_a_present_engine_that_fails_to_start_is_not_no_engine(tmp_path, monkeypatch, not_root):
    """A full shared-memory table must never be reported as a missing version."""
    _clean_env(monkeypatch)
    folder = _fake_pg_bin(tmp_path / "pg14", "14.9")  # its initdb exits 1
    (folder / "initdb").write_text(
        "#!/bin/sh\necho 'initdb: error: could not create shared memory segment' >&2\nexit 1\n"
    )
    (folder / "initdb").chmod(0o755)
    with pytest.raises(de.EngineStartError) as err:
        de.start("postgresql", "14", pg_bin_dir=str(folder))
    message = str(err.value)
    assert "postgresql 14 is installed but did not start" in message
    assert "shared memory segment" in message and "no engine" not in message
    assert not _leftovers()


def test_a_mysql_start_failure_names_the_version(tmp_path, monkeypatch, not_root):
    _clean_env(monkeypatch)
    fake = _fake_mysqld(tmp_path / "mysqld", "8.0.36")
    fake.write_text(
        "#!/bin/sh\ncase \"$1\" in --version) echo 'mysqld  Ver 8.0.36 for Linux';; *) echo 'mysqld: out of memory' >&2; exit 1;; esac\n"
    )
    fake.chmod(0o755)
    with pytest.raises(de.EngineStartError, match=r"mysql 8\.0 is installed but did not start"):
        de.start("mysql", "8.0", mysqld_path=str(fake))
    assert not _leftovers()


def test_a_start_error_is_an_engine_error():
    assert issubclass(de.EngineStartError, de.EngineError)
    assert not issubclass(de.EngineStartError, de.EngineUnavailable)


# --------------------------------------------------------------- cleanup


@pytest.mark.parametrize("name", ["a;drop table x", "a b", "", "1abc", "a..b"])
def test_routine_names_are_checked_before_they_reach_sql(name):
    with pytest.raises(ValueError):
        de._parts(name)


@needs_pg
def test_cleanup_on_exception_and_ctrl_c():
    if _refused_as_root():
        return
    major = de.available_majors("postgresql")[0]
    seen = []
    for exc in (RuntimeError("boom"), KeyboardInterrupt()):
        with pytest.raises(type(exc)):
            with de.running("postgresql", major) as h:
                seen.append((h.pid, h.workdir))
                raise exc
    for pid, folder in seen:
        assert pid is not None and _gone(pid)
        assert not folder.exists()
    assert not _leftovers()
    assert not de._LIVE


@needs_pg
def test_failed_load_is_an_engine_error_and_stop_still_cleans():
    if _refused_as_root():
        return
    h = de.start("postgresql", de.available_majors("postgresql")[0])
    pid = h.pid
    try:
        with pytest.raises(de.EngineError):
            de.load(h, ["CREATE TABLE t (id int"])
    finally:
        de.stop(h)
    de.stop(h)  # twice is fine
    assert pid is not None and _gone(pid)
    assert not _leftovers()
    with pytest.raises(de.EngineError, match="stopped"):
        de.call(h, "add_one", [1])


@needs_pg
def test_atexit_hook_stops_a_forgotten_engine():
    if _refused_as_root():
        return
    h = de.start("postgresql", de.available_majors("postgresql")[0])
    pid, folder = h.pid, h.workdir
    de._stop_all()
    assert pid is not None and _gone(pid)
    assert not folder.exists()


@pytest.mark.integration
@pytest.mark.skipif(not _MY, reason="no mysqld binary")
def test_mysql_cleanup_on_ctrl_c():
    if _refused_as_root("mysql", de.available_majors("mysql")[0]):
        return
    with pytest.raises(KeyboardInterrupt):
        with de.running("mysql", de.available_majors("mysql")[0]) as h:
            pid = h.pid
            raise KeyboardInterrupt
    assert pid is not None and _gone(pid)
    assert not _leftovers()


# --------------------------------------------------------------- the sweep after crashed runs

_REPO = Path(__file__).resolve().parents[4]
_HOLD_AN_ENGINE = """
import sys, time
from src.tools.database import disposable_engine as de
kw = {"pg_bin_dir": sys.argv[1]} if sys.argv[1] else {}
h = de.start("postgresql", sys.argv[2], **kw)
print(h.workdir, h.pid, flush=True)
time.sleep(600)
"""
_IGNORE_SIGNALS = """
import signal, subprocess, sys, time
signal.signal(signal.SIGINT, signal.SIG_IGN)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
print(child.pid, flush=True)
time.sleep(120)
"""


def _plain_folder(root: Path, name: str, record: dict | None, old: bool = True) -> Path:
    folder = root / (de._TEMP_PREFIX + name)
    folder.mkdir()
    if record is not None:
        (folder / de._OWNER_FILE).write_text(
            "\n".join(
                [
                    f"owner_pid={record['owner_pid']}",
                    f"owner_start={record.get('owner_start') or ''}",
                    "servers=" + ",".join(str(p) for p in record.get("servers", [])),
                ]
            )
        )
    if old:
        os.utime(folder, (1.0, 1.0))  # 1970: older than any age gate, with no clock read
    return folder


@pytest.fixture
def sweep_root(tmp_path, monkeypatch):
    monkeypatch.setattr(de, "_temp_root", lambda: str(tmp_path))
    monkeypatch.setattr(de, "_SWEEP_MIN_AGE_S", 0.0)
    return tmp_path


def test_a_garbled_owner_record_is_no_owner(tmp_path):
    folder = tmp_path / "f"
    folder.mkdir()
    (folder / de._OWNER_FILE).write_text("owner_pid=not-a-number\nservers=1\n")
    assert de._read_owner(folder) is None
    assert de._read_owner(tmp_path / "absent") is None


def test_a_folder_still_starting_in_another_process_is_not_swept(sweep_root):
    """The race the first sweep had: a parallel start's folder has no server pid yet."""
    code = (
        "import sys, time\n"
        "from src.tools.database import disposable_engine as de\n"
        f"de._temp_root = lambda: {str(sweep_root)!r}\n"
        "print(de._short_tempdir(), flush=True)\n"
        "time.sleep(120)\n"
    )
    env = {**os.environ, "PYTHONPATH": str(_REPO)}
    proc = subprocess.Popen(
        [sys.executable, "-c", code], cwd=_REPO, env=env, stdout=subprocess.PIPE, text=True
    )
    try:
        folder = Path(proc.stdout.readline().strip())
        assert folder.exists() and not (folder / "pgdata").exists()  # no server yet
        assert de.sweep_stale() == []
        assert folder.exists()
        proc.kill()
        proc.wait()
        assert folder in de.sweep_stale()  # owner dead: now it goes
        assert not folder.exists()
    finally:
        proc.kill()
        proc.wait()


def test_a_folder_of_this_process_that_is_still_starting_is_not_swept(sweep_root):
    folder = de._short_tempdir()  # owner record written at once, server not up
    assert de.sweep_stale() == []
    assert folder.exists()


def test_a_dead_owners_folder_is_swept_and_a_live_one_is_left(sweep_root):
    start = de._proc_start(os.getpid())
    dead = _plain_folder(sweep_root, "dead", {"owner_pid": 999999999, "servers": []})
    live = _plain_folder(sweep_root, "live", {"owner_pid": os.getpid(), "owner_start": start})
    reused = _plain_folder(
        sweep_root,
        "reused",
        {"owner_pid": os.getpid(), "owner_start": 1.0},
    )
    removed = de.sweep_stale()
    assert dead in removed and reused in removed
    assert live.exists() and not dead.exists() and not reused.exists()


def test_a_folder_without_an_owner_record_is_swept_only_when_old(sweep_root, monkeypatch):
    monkeypatch.setattr(de, "_SWEEP_MIN_AGE_S", 300.0)
    young = _plain_folder(sweep_root, "young", None, old=False)
    old = _plain_folder(sweep_root, "old", None)
    removed = de.sweep_stale()
    assert old in removed and young.exists()


def test_a_young_folder_is_left_even_when_its_owner_is_dead(sweep_root, monkeypatch):
    monkeypatch.setattr(de, "_SWEEP_MIN_AGE_S", 300.0)
    young = _plain_folder(sweep_root, "young", {"owner_pid": 999999999}, old=False)
    assert de.sweep_stale() == []
    assert young.exists()


def test_the_sweep_never_follows_a_symlink_or_touches_a_foreign_folder(sweep_root, monkeypatch):
    target = sweep_root / "elsewhere"
    target.mkdir()
    (target / "precious").write_text("keep")
    link = sweep_root / (de._TEMP_PREFIX + "link")
    link.symlink_to(target, target_is_directory=True)
    plain_file = sweep_root / (de._TEMP_PREFIX + "file")
    plain_file.write_text("x")
    foreign = _plain_folder(sweep_root, "foreign", {"owner_pid": 999999999})
    monkeypatch.setattr(de.os, "getuid", lambda: os.stat(foreign).st_uid + 1)
    assert de.sweep_stale() == []
    assert link.is_symlink() and (target / "precious").read_text() == "keep"
    assert plain_file.exists() and foreign.exists()


def test_a_symlink_to_a_dead_owners_folder_is_not_followed(sweep_root, monkeypatch):
    """Following the link would stop a server and delete files: only the explicit
    check keeps them (``rmtree`` refusing a symlink would hide a missing check)."""
    monkeypatch.setattr(de, "_SHUTDOWN_TIMEOUT_S", 1.0)
    target = sweep_root / "elsewhere"
    target.mkdir()
    (target / "precious").write_text("keep")
    proc, sibling = _fake_engine(target)  # a "server" with the target in its command line
    try:
        (target / de._OWNER_FILE).write_text(
            f"owner_pid=999999999\nowner_start=\nservers={proc.pid}\n"
        )
        link = sweep_root / (de._TEMP_PREFIX + "link")
        link.symlink_to(target, target_is_directory=True)
        os.utime(link, (1.0, 1.0), follow_symlinks=False)
        assert not de._sweepable(link)
        assert de.sweep_stale() == []
        assert proc.poll() is None, "the sweep followed the link and stopped its target's server"
        assert (target / "precious").read_text() == "keep"
    finally:
        proc.kill()
        with contextlib.suppress(ProcessLookupError):
            os.kill(sibling, signal.SIGKILL)


def _hold_an_engine(pg_bin_dir: str, major: str, **env: str):
    """Start an engine in a child and wait for it, up to the module's own start budget.

    Waits on the child's output (an event), not on a fixed sleep, so a slow runner only
    takes longer; a child that dies says why.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLD_AN_ENGINE, pg_bin_dir, major],
        cwd=_REPO,
        env={**os.environ, "PYTHONPATH": str(_REPO), **env},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    ready, _, _ = select.select([proc.stdout], [], [], de._INIT_TIMEOUT_S + de._START_TIMEOUT_S)
    line = proc.stdout.readline().split() if ready else []
    if len(line) != 2:
        proc.kill()
        _, err = proc.communicate()
        raise AssertionError(f"the engine did not start in the child process: {err[-1500:]}")
    return proc, Path(line[0]), int(line[1])


def test_the_start_time_does_not_depend_on_the_timezone_or_locale(monkeypatch):
    first = de._proc_start(os.getpid())
    assert isinstance(first, float)
    monkeypatch.setenv("TZ", "Pacific/Kiritimati")
    monkeypatch.setenv("LC_ALL", "fr_FR.UTF-8")
    time.tzset()
    try:
        assert abs(de._proc_start(os.getpid()) - first) < 0.01
    finally:
        monkeypatch.undo()
        time.tzset()


def test_an_owner_recorded_under_one_timezone_is_alive_under_another(tmp_path, monkeypatch):
    """The review's repro: a run under TZ=UTC and a sweep under the default TZ."""
    holder = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        env={**os.environ, "TZ": "UTC", "LC_ALL": "fr_FR.UTF-8"},
    )
    try:
        time.sleep(0.3)
        record = {"owner_pid": holder.pid, "owner_start": de._proc_start(holder.pid)}
        monkeypatch.setenv("TZ", "Asia/Tokyo")
        monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
        time.tzset()
        assert de._owner_alive(record)
        assert not de._owner_alive({**record, "owner_start": record["owner_start"] - 3600})
    finally:
        holder.kill()
        holder.wait()
        monkeypatch.undo()
        time.tzset()


@pg_binary_path
def test_an_engine_held_under_another_timezone_survives_the_sweep(monkeypatch):
    """Two concurrent runs of one user in different environments (a shell and an IDE)."""
    if _refused_as_root("postgresql", de._pg_bin_major(_PG_BIN)):
        return
    monkeypatch.setattr(de, "_SWEEP_MIN_AGE_S", 0.0)
    major = de._pg_bin_major(_PG_BIN)
    proc, folder, server = _hold_an_engine(str(_PG_BIN), major, TZ="UTC")
    try:
        monkeypatch.setenv("TZ", "Asia/Tokyo")
        time.tzset()
        assert folder not in de.sweep_stale()  # other workers' dead folders may go too
        assert de._running(server) and folder.exists()
        proc.kill()
        proc.wait()
        assert folder in de.sweep_stale()  # the owner is dead now: it goes
        assert _gone(server) and not folder.exists()
    finally:
        proc.kill()
        proc.wait()
        monkeypatch.undo()
        time.tzset()
        de._stop_pid(server, folder, signal.SIGINT)
        shutil.rmtree(folder, ignore_errors=True)


@pg_binary_path
@pytest.mark.parametrize("path", ["binary", "pgserver"])
def test_a_sigkilled_owner_leaves_an_engine_that_the_next_sweep_stops(path, monkeypatch):
    """Nothing runs on SIGKILL or pytest-timeout's ``os._exit``: the sweep must."""
    if _refused_as_root("postgresql", de._pg_bin_major(_PG_BIN)):
        return
    monkeypatch.setattr(de, "_SWEEP_MIN_AGE_S", 0.0)
    major = de._pg_bin_major(_PG_BIN)
    proc, folder, server = _hold_an_engine(str(_PG_BIN) if path == "binary" else "", major)
    try:
        segment = _segment_id(folder)
        proc.send_signal(signal.SIGKILL)
        proc.wait()
        assert de._running(server), "the engine outlives its parent"
        assert folder in de.sweep_stale()
        assert _gone(server) and not folder.exists()
        if _shm_ids() is not None and segment is not None:
            assert segment not in _shm_ids(), "the segment leaked"
    finally:
        proc.kill()
        proc.wait()
        de._stop_pid(server, folder, signal.SIGINT)
        shutil.rmtree(folder, ignore_errors=True)


def _fake_engine(folder: Path):
    """A process with ``folder`` in its command line that ignores SIGINT and SIGTERM."""
    proc = subprocess.Popen(
        [sys.executable, "-c", _IGNORE_SIGNALS, str(folder)],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    sibling = int(proc.stdout.readline())
    return proc, sibling


def test_stop_pid_escalates_to_sigkill_of_that_pid_only(tmp_path, monkeypatch):
    """The fallback for a server that is not our child: one pid, never its group."""
    monkeypatch.setattr(de, "_SHUTDOWN_TIMEOUT_S", 1.0)
    folder = tmp_path / "modernizer-engine-fake"
    folder.mkdir()
    proc, sibling = _fake_engine(folder)
    try:
        de._stop_pid(proc.pid, folder, signal.SIGINT)
        assert proc.wait(timeout=10) == -signal.SIGKILL
        assert de._running(sibling), "a process group mate was killed with it"
    finally:
        proc.kill()
        with contextlib.suppress(ProcessLookupError):
            os.kill(sibling, signal.SIGKILL)


def test_stop_pid_rechecks_before_sigkill(tmp_path, monkeypatch):
    """The pid may have exited and been reused while we waited: no SIGKILL then."""
    monkeypatch.setattr(de, "_SHUTDOWN_TIMEOUT_S", 0.5)
    folder = tmp_path / "modernizer-engine-fake"
    folder.mkdir()
    proc, sibling = _fake_engine(folder)
    answers = iter([True, False])
    monkeypatch.setattr(de, "_ours", lambda pid, f: next(answers))
    try:
        de._stop_pid(proc.pid, folder, signal.SIGINT)  # ignores SIGINT, then "is reused"
        time.sleep(1.0)  # a SIGKILL would have landed by now
        assert proc.poll() is None, "SIGKILL was sent to a pid that was no longer ours"
    finally:
        proc.kill()
        with contextlib.suppress(ProcessLookupError):
            os.kill(sibling, signal.SIGKILL)


def test_stop_pid_never_signals_a_process_that_is_not_ours(tmp_path):
    folder = tmp_path / "modernizer-engine-mine"
    folder.mkdir()
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert not de._ours(other.pid, folder)
        de._stop_pid(other.pid, folder, signal.SIGKILL)
        assert other.poll() is None
    finally:
        other.kill()
        other.wait()


def test_reap_escalates_to_sigkill_for_our_own_child(monkeypatch):
    monkeypatch.setattr(de, "_SHUTDOWN_TIMEOUT_S", 1.0)
    proc = subprocess.Popen(
        [sys.executable, "-c", _IGNORE_SIGNALS],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    sibling = int(proc.stdout.readline())
    de._reap(proc, signal.SIGINT)
    assert proc.returncode == -signal.SIGKILL
    with contextlib.suppress(ProcessLookupError):
        os.kill(sibling, signal.SIGKILL)  # the group went with it, as documented


@needs_pg
def test_a_failure_inside_pgserver_after_its_server_is_up_leaves_no_process(monkeypatch):
    """The path CI uses: ``pgserver.get_server`` can fail after its ``pg_ctl start``
    (its own 10 s timeout under load, Ctrl-C in its readiness loop)."""
    if _refused_as_root():
        return
    import pgserver.postgres_server as ps

    pids: list[int] = []
    real = ps.PostgresServer.ensure_postgres_running

    def up_then_fail(self):
        real(self)  # the postmaster is really up now
        pids.append(self.get_pid())
        raise KeyboardInterrupt

    monkeypatch.setattr(ps.PostgresServer, "ensure_postgres_running", up_then_fail)
    with pytest.raises(KeyboardInterrupt):
        de.start("postgresql", "16")
    assert pids and _gone(pids[0])
    assert not _leftovers()
    assert not de._LIVE


@needs_pg
def test_pgserver_trusts_only_the_socket_after_start():
    if _refused_as_root():
        return
    with de.running("postgresql", "16") as h:
        assert isinstance(h._backend._server, de._PgserverServer)
        hba = (h.workdir / "pgdata" / "pg_hba.conf").read_text()
        assert not re.search(r"^host", hba, re.M)
        assert re.search(r"^local\s+all\s+all\s+trust", hba, re.M)


def test_the_timeouts_are_shorter_than_the_per_test_limit():
    """ci/test.sh kills a test at 300 s; the engine's own timeout must fire first."""
    assert de._INIT_TIMEOUT_S + de._START_TIMEOUT_S < 300
    assert de._SHUTDOWN_TIMEOUT_S < de._START_TIMEOUT_S


def test_the_owner_is_recorded_the_moment_the_folder_exists():
    """A parallel run's sweep must see an owner while the server is still starting."""
    folder = de._short_tempdir()
    record = de._read_owner(folder)
    assert record == {
        "owner_pid": os.getpid(),
        "owner_start": de._proc_start(os.getpid()),
        "servers": [],
    }
    de._record_server(folder, 4242)
    de._record_server(folder, 4242)
    assert de._read_owner(folder)["servers"] == [4242]
    assert de._read_owner(folder)["owner_pid"] == os.getpid()


def test_contributing_documents_the_binaries_and_the_root_rule():
    text = (Path(__file__).resolve().parents[4] / "CONTRIBUTING.md").read_text()
    assert "postgresql=<major>" in text and "mysql-server=<major>" in text
    assert "MODERNIZER_PG_BIN_<major>" in text and "MODERNIZER_MYSQLD_<major>" in text
    assert "root" in text


@pytest.mark.passes_on_main
def test_the_mysql_engine_tests_are_deselected_not_skipped_in_a_gated_run():
    """``--fail-on-skip`` turns a skip into a failure, so these carry ``integration``."""
    for test in (
        test_mysql_start_load_call_snapshot_stop,
        test_mysql_routines_do_not_run_as_an_admin_user,
        test_mysql_cleanup_on_ctrl_c,
        test_mysql_out_and_inout_arguments_come_back_as_the_last_row,
    ):
        marks = {m.name for m in getattr(test, "pytestmark", [])}
        assert "integration" in marks, test.__name__


def test_root_and_the_runner_never_share_a_password(monkeypatch):
    monkeypatch.setenv(de.PASSWORD_ENV, "chosen-by-the-caller")
    root, runner = de._passwords()
    assert root == "chosen-by-the-caller" and runner != root and len(runner) >= 24
    monkeypatch.delenv(de.PASSWORD_ENV)
    root, runner = de._passwords()
    assert root != runner and len(root) >= 24
    assert de._passwords() != (root, runner)  # fresh each time


def test_contributing_documents_the_password_variable():
    text = (Path(__file__).resolve().parents[4] / "CONTRIBUTING.md").read_text()
    assert "MODERNIZER_DISPOSABLE_ENGINE_PASSWORD" in text


# --------------------------------------------------------------- row changes and modes (#1942)


@pytest.mark.parametrize(("engine", "q"), [("postgresql", '"'), ("mysql", "`")])
def test_row_change_sql_quotes_every_name_and_binds_every_value(engine, q):
    assert de.row_change_sql(engine, "INSERT", "t", ["a", "b"]) == (
        f"INSERT INTO {q}t{q} ({q}a{q}, {q}b{q}) VALUES (%s, %s)"
    )
    assert de.row_change_sql(engine, "UPDATE", "s.t", ["a"], ["id", "k"]) == (
        f"UPDATE {q}s{q}.{q}t{q} SET {q}a{q} = %s WHERE {q}id{q} = %s AND {q}k{q} = %s"
    )
    assert de.row_change_sql(engine, "DELETE", "t", (), ["id"]) == (
        f"DELETE FROM {q}t{q} WHERE {q}id{q} = %s"
    )


@pytest.mark.parametrize(
    ("event", "table", "columns", "where"),
    [
        ("INSERT", "t;drop table x", ["a"], []),
        ("UPDATE", "t", ["a b"], ["id"]),
        ("DELETE", "t", [], ["1id"]),
        ("UPDATE", "t", ["a"], []),  # an UPDATE or DELETE needs the row it changes
        ("DELETE", "t", [], []),
        ("UPDATE", "t", [], ["id"]),
        ("MERGE", "t", ["a"], ["id"]),
    ],
)
def test_row_change_sql_refuses_names_that_are_not_plain_and_rows_it_cannot_match(
    event, table, columns, where
):
    with pytest.raises(ValueError):
        de.row_change_sql("postgresql", event, table, columns, where)


@needs_pg
def test_execute_runs_a_bound_row_change_and_an_error_is_data():
    if _refused_as_root():
        return
    with de.running("postgresql", de.available_majors("postgresql")[0]) as h:
        de.load(h, PG_DDL, ROWS)
        sql = de.row_change_sql("postgresql", "UPDATE", "items", ["qty"], ["id"])
        assert de.execute(h, sql, [9, 2]).error is None
        assert de.snapshot(h, ["items"])["items"][1] == {"id": 2, "qty": 9}
        insert = de.row_change_sql("postgresql", "INSERT", "items", ["id", "qty"])
        assert "UniqueViolation" in (de.execute(h, insert, [1, 1]).error or "")


@needs_pg
def test_postgresql_identity_and_serial_sequences_continue_past_the_loaded_rows():
    """A routine's own INSERT does not collide with a synthetic key (MySQL's
    AUTO_INCREMENT does this by itself)."""
    if _refused_as_root():
        return
    ddl = [
        "CREATE TABLE ident (id int GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, v int)",
        "CREATE TABLE ser (id serial PRIMARY KEY, v int)",
        "CREATE TABLE empty_ident (id int GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY)",
    ]
    rows = {"ident": [{"id": 1, "v": 0}, {"id": 7, "v": 0}], "ser": [{"id": 3, "v": 0}],
            "empty_ident": []}  # fmt: skip
    with de.running("postgresql", de.available_majors("postgresql")[0]) as h:
        de.load(h, ddl, rows)
        assert de.execute(h, "INSERT INTO ident (v) VALUES (%s) RETURNING id", [1]).rows == [[8]]
        assert de.execute(h, "INSERT INTO ser (v) VALUES (%s) RETURNING id", [1]).rows == [[4]]
        assert de.execute(h, "INSERT INTO empty_ident DEFAULT VALUES RETURNING id").rows == [[1]]


def test_call_needs_one_mode_per_argument_on_mysql():
    backend = de._MySQLBackend.__new__(de._MySQLBackend)
    backend._kinds = {}
    with pytest.raises(ValueError, match="one mode per argument"):
        backend.call("p", [1, 2], ["IN"])


@pytest.mark.integration
@pytest.mark.skipif(not _MY, reason="no mysqld binary")
def test_mysql_out_and_inout_arguments_come_back_as_the_last_row():
    major = de.available_majors("mysql")[0]
    if _refused_as_root("mysql", major):
        return
    proc = de.SourceRoutine(
        "bump", "procedure",
        "CREATE PROCEDURE bump(IN a INT, INOUT b INT, OUT c INT) BEGIN "
        "SET b = b + a; SET c = a * 2; END",
    )  # fmt: skip
    with de.running("mysql", major) as h:
        de.load(h, [], {}, [proc])
        assert de.call(h, "bump", [3, 4, None], ["IN", "INOUT", "OUT"]).rows == [[7, 6]]
        assert de.call(h, "bump", [3, None, None], ["IN", "INOUT", "OUT"]).rows == [[None, 6]]


@needs_pg
def test_a_sequence_at_its_types_highest_value_does_not_overflow_the_load():
    """``smallserial`` at 32767: the next value would be out of range, so the sequence
    is set to the highest value already used, not past it."""
    if _refused_as_root():
        return
    ddl = ["CREATE TABLE tiny (id smallserial PRIMARY KEY, v int)"]
    with de.running("postgresql", de.available_majors("postgresql")[0]) as h:
        de.load(h, ddl, {"tiny": [{"id": 32767, "v": 0}]})
        out = de.execute(h, "INSERT INTO tiny (v) VALUES (%s)", [1])
        assert "reached maximum value" in (out.error or "")  # the type is full: that is data


@needs_pg
def test_execute_runs_as_the_routine_runner_not_the_superuser():
    if _refused_as_root():
        return
    with de.running("postgresql", de.available_majors("postgresql")[0]) as h:
        assert de.execute(h, "SELECT current_user").rows == [[de._RUNNER]]
        assert "permission denied" in (de.execute(h, "CREATE ROLE intruder").error or "")


@pytest.mark.parametrize(
    "type_name",
    [
        "boolean and pg_sleep(3) is null",  # the verification's payload: a function call
        "pg_sleep(3)",
        "integer and pg_sleep(3) is null",
        "int); drop table x; --",
        "text'",
        "a b c d;",
        "int--",
        '"x"; select 1',
        '"unterminated',
        '""',
        "a.b.c",
        "mood[1]",
        "smallserial",
        "x" * 201,
        "",
    ],
)
def test_a_cast_type_is_parsed_and_anything_else_is_refused(type_name):
    with pytest.raises(ValueError):
        de.pg_cast_type(type_name)


@pytest.mark.parametrize(
    ("type_name", "written"),
    [
        ("integer", "integer"),
        ("character varying(12)", "character varying(12)"),
        ("numeric(10, 2)", "numeric(10, 2)"),
        ("timestamp(3) with time zone", "timestamp(3) with time zone"),
        ("integer[]", "integer[]"),
        ("double precision", "double precision"),
        ('"char"', '"char"'),
        ('public."Mood"', '"public"."Mood"'),
        ("App.Mood", '"app"."mood"'),  # unquoted names fold to lower case, as PostgreSQL does
        ('"a""b"[]', '"a""b"[]'),
    ],
)
def test_a_type_name_is_written_as_the_allowlist_reads_it_or_with_every_part_quoted(
    type_name, written
):
    assert de.pg_cast_type(type_name) == written


@needs_pg
def test_the_pg_sleep_payload_never_reaches_the_engine():
    """A crafted type in ``casts`` is refused while the statement is built, before any SQL
    runs: the call raises ``ValueError`` (a call that ran would return a ``CallResult``),
    and the same call with a plain type then runs."""
    if _refused_as_root():
        return
    fn = de.SourceRoutine(
        "flag",
        "function",
        "CREATE FUNCTION flag(b boolean) RETURNS int LANGUAGE sql AS $$ SELECT 1 $$",
    )
    with de.running("postgresql", de.available_majors("postgresql")[0]) as h:
        de.load(h, [], {}, [fn])
        with pytest.raises(ValueError, match="not a type name"):
            de.call(h, "flag", ["true"], casts=["boolean and pg_sleep(3) is null"])
        assert de.call(h, "flag", ["true"], casts=["boolean"]).rows == [[1]]


# ---------------------------------------------- a CALL's result sets, without a server (#1944)


class _FakeMyCursor:
    """A MySQL Connector 9.2+ cursor after ``CALL``: no stored results; each result set is
    read, then ``nextset`` moves to the next one, and the last status packet has none."""

    def __init__(self, sets):
        self._sets = [list(s) for s in sets] + [None]
        self.executed: list[str] = []

    @property
    def description(self):
        return [("v", 3)] if self._sets and self._sets[0] is not None else None  # 3: LONG

    def execute(self, sql, params=None):
        self.executed.append(sql)

    def stored_results(self):
        return iter(())

    def fetchall(self):
        return self._sets[0] or []

    def nextset(self):
        self._sets.pop(0)
        return bool(self._sets)

    def close(self):
        if self._sets and self._sets[0] is not None:
            raise RuntimeError("Unread result found")


class _FakeMyConnection:
    def __init__(self, sets):
        self.cursors: list[_FakeMyCursor] = []
        self._sets = sets

    def cursor(self):
        cur = _FakeMyCursor(self._sets)
        self.cursors.append(cur)
        return cur


def test_a_mysql_call_reads_every_result_set_through_nextset():
    """The #1942 regression, gated: with Connector 9.2 on, ``stored_results`` is empty
    after ``CALL`` and the result sets come through ``nextset``; before the fix the cursor
    was closed with them unread (``Unread result found``)."""
    import mysql.connector

    backend = object.__new__(de._MySQLBackend)
    backend._mc = mysql.connector
    backend._kinds = {"two_sets": "procedure"}
    backend._conn = _FakeMyConnection([[(1,)], [(2,), (3,)]])
    result = de._MySQLBackend.call(backend, "two_sets", [])
    assert result.error is None
    assert result.rows == [[1], [2], [3]] and result.result_sets == (1, 2)
    assert result.column_types == (("LONG",), ("LONG",))
