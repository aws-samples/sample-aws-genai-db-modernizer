# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A test PostgreSQL leaves no System V shared-memory segment behind (#2400).

PostgreSQL keeps one small System V segment per server even with
``shared_memory_type=mmap``, and macOS has room for 32. A clean stop frees it; a server
killed without one does not, so the engine's stop removes the segment it can prove is its
own: same id and key, created by that server's pid, owned by this user, nothing attached.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
from pathlib import Path

import pytest

from src.tools.database import _shm_segments as shm
from tests import pg_server

live = pytest.mark.skipif(
    shutil.which("ipcs") is None or not shm.segments_supported(),
    reason="needs ipcs (macOS, BSD or Linux)",
)

_BSD = """IPC status from <running system> as of Sat Nov 21 13:19:35 EST 2026
T     ID     KEY        MODE       OWNER    GROUP  CREATOR   CGROUP NATTCH  SEGSZ  CPID  LPID   ATIME    DTIME    CTIME
Shared Memory:
m 885784577 0x29581aaa --rw------- me    staff me    staff      0     56  58513  58513 no-entry no-entry 13:19:35
"""
_LINUX_M = """
------ Shared Memory Segments --------
key        shmid      owner      perms      bytes      nattch     status
0x29581aaa 885784577  me         600        56         0
"""
_LINUX_P = """
------ Shared Memory Creator/Last-op PIDs --------
shmid      owner      cpid       lpid
885784577  me         58513      58513
"""
_ROW = {
    "ID": "885784577",
    "KEY": "0x29581aaa",
    "OWNER": "me",
    "CREATOR": "me",
    "NATTCH": "0",
    "CPID": "58513",
}


def test_the_bsd_layout_is_read():
    (row,) = shm._bsd_segments(_BSD)
    assert {k: row[k] for k in _ROW} == _ROW


def test_the_linux_layout_is_read():
    assert shm._linux_segments(_LINUX_M, _LINUX_P) == [_ROW]


def test_an_unknown_layout_reads_as_no_segments():
    assert shm._bsd_segments("nothing useful") == []
    assert shm._linux_segments("nothing useful", "nothing useful") == []


def _ids() -> set[str]:
    return {row["ID"] for row in shm.segments()}


def _own_segment(srv: pg_server.PgTestServer) -> str:
    line = (srv.workdir / shm.NOTE_FILE).read_text().split()
    return line[2]


@live
def test_a_start_and_stop_leaves_ipcs_unchanged():
    pytest.importorskip("pgserver")
    before = _ids()
    srv = pg_server.start_pg_server()
    ident = _own_segment(srv)
    assert ident in _ids(), "the server's segment is what the test watches"
    srv.cleanup()
    assert ident not in _ids()
    # Segments that were there before are the maintainer's or another worker's: untouched.
    assert before & _ids() == before - {ident}


@live
def test_a_server_killed_with_sigkill_leaves_no_segment():
    pytest.importorskip("pgserver")
    srv = pg_server.start_pg_server()
    ident = _own_segment(srv)
    os.kill(srv.get_pid(), signal.SIGKILL)
    srv._server._proc.wait()  # the postmaster is our child: wait for it to be gone
    srv.cleanup()
    assert ident not in _ids()


def _row(**fields: str) -> dict[str, str]:
    row = {
        "ID": "77",
        "KEY": "0x1f",
        "OWNER": "me",
        "CREATOR": "me",
        "NATTCH": "0",
        "CPID": "999999",
    }
    row.update(fields)
    return row


def _setup(monkeypatch, tmp_path: Path, rows: list[dict[str, str]]) -> list[list[str]]:
    removed: list[list[str]] = []
    (tmp_path / shm.NOTE_FILE).write_text("999999 31 77\n")
    monkeypatch.setattr(shm, "segments", lambda: rows)
    monkeypatch.setattr(shm, "_alive", lambda pid: False)
    monkeypatch.setattr(shm.getpass, "getuser", lambda: "me")
    monkeypatch.setattr(shm, "_DETACH_WAIT_S", 0.0)

    def run(args):
        removed.append(args)
        return ""

    monkeypatch.setattr(shm, "_run", run)
    return removed


def test_the_matching_segment_is_removed(monkeypatch, tmp_path):
    removed = _setup(monkeypatch, tmp_path, [_row()])
    assert shm.reap(tmp_path) is True
    assert removed == [["ipcrm", "-m", "77"]]


@pytest.mark.parametrize(
    "other",
    [
        {"OWNER": "someone"},
        {"CREATOR": "someone"},
        {"CPID": "123"},  # another process's segment under the same id
        {"KEY": "0x20"},  # the id was reused for another key
        {"NATTCH": "2"},  # still attached
    ],
)
def test_a_segment_that_is_not_ours_is_never_removed(monkeypatch, tmp_path, other):
    removed = _setup(monkeypatch, tmp_path, [_row(**other)])
    assert shm.reap(tmp_path) is False
    assert removed == []


def test_a_running_server_s_segment_is_never_removed(monkeypatch, tmp_path):
    removed = _setup(monkeypatch, tmp_path, [_row()])
    monkeypatch.setattr(shm, "_alive", lambda pid: True)
    assert shm.reap(tmp_path) is False
    assert removed == []


def test_no_note_removes_nothing(monkeypatch, tmp_path):
    removed = _setup(monkeypatch, tmp_path, [_row()])
    (tmp_path / shm.NOTE_FILE).unlink()
    assert shm.reap(tmp_path) is False
    assert removed == []


def test_a_full_segment_table_is_named_in_the_start_error(monkeypatch, tmp_path):
    from src.tools.database import disposable_engine as de

    def spawn(self, workdir, bin_dir, major):
        self._log.write_text(
            "FATAL:  could not create shared memory segment: No space left on device\n"
        )
        self._proc = subprocess.Popen(["true"])
        self._proc.wait()

    monkeypatch.setattr(de._PgBinaryServer, "_initdb", lambda *a: None)
    monkeypatch.setattr(de._PgBinaryServer, "_spawn", spawn)
    with pytest.raises(de.EngineStartError, match="shared memory table is full"):
        de._PgBinaryServer(tmp_path, tmp_path / "bin", "16")
