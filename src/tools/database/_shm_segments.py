# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Removes the System V shared-memory segment a stopped test PostgreSQL left behind (#2400).

PostgreSQL creates one small System V segment per server even with
``shared_memory_type=mmap`` (the postmaster uses it to detect a second server on the same
data folder), and only a clean shutdown frees it. macOS allows 32 segments in total, so
servers killed with ``SIGKILL`` fill the table and the next start fails with
``No space left on device``.

The server's own ``postmaster.pid`` names its segment (key and id on line 7). ``note``
copies that, with the server's pid, into ``shm.txt`` in the engine's folder while the
server runs. ``reap`` removes the segment, and only when ``ipcs`` shows the same id and
key, created by that pid, owned by this user, with no process attached and the pid gone.
Anything else, such as a segment of another user or of a server of the maintainer's,
never matches and is never touched.

Standard library only: the engine watcher loads this file by path.
"""

from __future__ import annotations

import getpass
import os
import subprocess  # nosec B404 - runs ipcs and ipcrm, fixed arguments
import time
from pathlib import Path

NOTE_FILE = "shm.txt"
_DETACH_WAIT_S = 10.0


def _run(args: list[str]) -> str | None:
    try:
        done = subprocess.run(  # nosec B603 B607 - fixed arguments, no shell
            args, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None  # no ipcs here, or it hung: nothing to do
    return done.stdout if done.returncode == 0 else None


def _bsd_segments(out: str) -> list[dict[str, str]]:
    """Rows of macOS and BSD ``ipcs -m -a``: one table with the creator pid in it."""
    header: list[str] | None = None
    rows: list[dict[str, str]] = []
    for line in out.splitlines():
        words = line.split()
        if words[:3] == ["T", "ID", "KEY"]:
            header = words
        elif header and len(words) == len(header) and words[0] == "m":
            rows.append(dict(zip(header, words, strict=True)))
    if header is None or not {"ID", "KEY", "OWNER", "CREATOR", "NATTCH", "CPID"} <= set(header):
        return []
    return rows


def _linux_segments(listing: str, pids: str) -> list[dict[str, str]]:
    """Rows of Linux ``ipcs -m`` joined with ``ipcs -m -p`` on the segment id.

    Linux lists no creator apart from the owner, so the owner stands for it.
    """
    creators: dict[str, str] = {}
    for line in pids.splitlines():
        words = line.split()
        if len(words) == 4 and words[0].isdigit() and words[2].isdigit():
            creators[words[0]] = words[2]
    rows: list[dict[str, str]] = []
    for line in listing.splitlines():
        words = line.split()
        if len(words) >= 6 and words[0].startswith("0x") and words[1] in creators:
            rows.append(
                {
                    "ID": words[1],
                    "KEY": words[0],
                    "OWNER": words[2],
                    "CREATOR": words[2],
                    "NATTCH": words[5],
                    "CPID": creators[words[1]],
                }
            )
    return rows


def segments() -> list[dict[str, str]]:
    """Every System V segment ``ipcs`` lists, as ID, KEY (hex), OWNER, CREATOR, NATTCH, CPID.

    Empty when there is no ``ipcs`` or its layout is not one of the two known ones
    (BSD and macOS, Linux), so ``reap`` then does nothing.
    """
    out = _run(["ipcs", "-m", "-a"])
    if out is None:
        return []
    if "CPID" in out.split():
        return _bsd_segments(out)
    pids = _run(["ipcs", "-m", "-p"])
    return _linux_segments(out, pids) if pids is not None else []


def segments_supported() -> bool:
    """Whether ``ipcs`` is here and ``reap`` can read its layout."""
    out = _run(["ipcs", "-m", "-a"])
    if out is None:
        return False
    return "CPID" in out.split() or "cpid" in (_run(["ipcs", "-m", "-p"]) or "")


def note(workdir: Path) -> None:
    """Record the running server's segment from its ``postmaster.pid``, if it names one."""
    try:
        lines = (workdir / "pgdata" / "postmaster.pid").read_text().splitlines()
        pid = int(lines[0].strip())
        key, ident = (int(part) for part in lines[6].split())
    except (OSError, ValueError, IndexError):
        return
    if key <= 0 or ident < 0:
        return
    try:
        (workdir / NOTE_FILE).write_text(f"{pid} {key} {ident}\n")
    except OSError:
        return


def _read_note(workdir: Path) -> tuple[int, int, int] | None:
    try:
        pid, key, ident = (int(part) for part in (workdir / NOTE_FILE).read_text().split())
    except (OSError, ValueError):
        return None
    return pid, key, ident


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def reap(workdir: Path) -> bool:
    """Remove the segment ``note`` recorded for this folder when it is ours and unused.

    Call it after the server is stopped. Returns whether a segment was removed.
    """
    recorded = _read_note(workdir)
    if recorded is None:
        return False
    pid, key, ident = recorded
    if _alive(pid):
        return False
    try:
        me = getpass.getuser()
    except (KeyError, OSError):
        return False
    deadline = time.monotonic() + _DETACH_WAIT_S
    while True:
        row = _find(pid, key, ident)
        if row is None:
            (workdir / NOTE_FILE).unlink(missing_ok=True)  # a clean stop already freed it
            return False
        if row["OWNER"] != me or row["CREATOR"] != me:
            return False  # not ours, whatever else matches
        if row["NATTCH"] == "0":
            removed = _run(["ipcrm", "-m", str(ident)]) is not None
            if removed:
                (workdir / NOTE_FILE).unlink(missing_ok=True)
            return removed
        if time.monotonic() >= deadline:
            return False  # a child of the dead postmaster still holds it: leave it
        time.sleep(0.2)  # its children exit a moment after the postmaster is gone


def _find(pid: int, key: int, ident: int) -> dict[str, str] | None:
    """The segment with this id and key that this pid created, or None."""
    for row in segments():
        try:
            if int(row["ID"]) == ident and int(row["KEY"], 16) == key and int(row["CPID"]) == pid:
                return row
        except ValueError:
            continue
    return None
