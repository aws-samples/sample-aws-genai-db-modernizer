# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Stops a disposable engine when the process that started it is gone (#2350).

``disposable_engine.start`` runs this file as its own process, with a pipe from the
owner as standard input. The owner never writes to it: the operating system closes the
write end when the owner exits, however it exits (``SIGKILL``, a test timeout's
``os._exit``, a killed xdist worker), and that end-of-file is the signal. Then every
process whose command line names the engine's folder is stopped the engine's own way
(``SIGINT`` for PostgreSQL's fast shutdown, ``SIGTERM`` for MySQL), and ``SIGKILL``
only when it has not gone in time, because a ``SIGKILL``ed postmaster leaks its System V
shared-memory segment. Only server programs (postgres, postmaster, mysqld, mariadbd)
that name the folder are signalled. Errors go to standard error, which the owner points
at ``watcher.log`` in the folder.

Standard library only and run by path with ``-I``, so it starts fast and does not
depend on the owner's import path. Usage: ``_engine_watcher.py <folder>``.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess  # nosec B404 - runs ps, fixed arguments
import sys
import time

_GRACE_S = 20.0
_SERVERS = re.compile(r"^(?:.*/)?(postgres|postmaster|mysqld|mariadbd)(?:\s|$)")
_ATTEMPTS = 3


def _processes() -> list[tuple[int, str]]:
    """Every process's pid and command line: ``/proc`` where there is one, else ``ps``."""
    found: list[tuple[int, str]] = []
    if os.path.isdir("/proc/self"):
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                with open(f"/proc/{name}/cmdline", "rb") as handle:
                    raw = handle.read()
            except OSError:  # gone meanwhile, or not ours to read
                continue
            command = raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()
            if command:
                found.append((int(name), command))
        return found
    out = subprocess.run(  # nosec B603 B607 - fixed arguments, no shell
        ["ps", "-axww", "-o", "pid=,command="],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    ).stdout
    for line in out.splitlines():
        pid_text, _, command = line.strip().partition(" ")
        if pid_text.isdigit():
            found.append((int(pid_text), command.strip()))
    return found


def _ours(pattern: re.Pattern[str], pid: int) -> bool:
    """Whether ``pid`` still runs a server naming the folder."""
    return any(other == pid and _victim(pattern, c) for other, c in _processes())


def _victim(pattern: re.Pattern[str], command: str) -> bool:
    # Only a server program: a ``tail -f`` of its log or a ``psql -h <folder>`` also names
    # the folder, and is not the watcher's to signal.
    return bool(_SERVERS.match(command) and pattern.search(command))


def _signal_for(command: str) -> signal.Signals:
    match = _SERVERS.match(command)
    # PostgreSQL's fast shutdown is SIGINT; its SIGTERM is a smart shutdown that waits
    # for clients. mysqld shuts down cleanly on SIGTERM.
    postgres = match is not None and match.group(1) in ("postgres", "postmaster")
    return signal.SIGINT if postgres else signal.SIGTERM


def stop_folder_servers(folder: str) -> None:
    """Stop every server process, but this one, that has ``folder`` in its command line.

    Found by the command line alone, because in #2350 the folder (and so every pid file
    in it) was already gone.
    """
    names = {folder, os.path.realpath(folder)}
    pattern = re.compile("|".join(re.escape(n) + r"(?![\w.-])" for n in sorted(names)))
    mine = os.getpid()
    victims = {
        pid: command for pid, command in _processes() if pid != mine and _victim(pattern, command)
    }
    for pid, command in victims.items():
        try:
            os.kill(pid, _signal_for(command))
        except (ProcessLookupError, PermissionError):
            continue
    deadline = time.monotonic() + _GRACE_S
    while victims and time.monotonic() < deadline:
        time.sleep(0.2)
        victims = {pid: c for pid, c in victims.items() if _ours(pattern, pid)}
    for pid in victims:  # still there, and still ours: the pid is checked again just above
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            continue


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        return 2
    # Not the owner's terminal's business: a Ctrl-C or hangup meant for the tests must
    # not take the watcher down before it has done its one job.
    for sig in (signal.SIGINT, signal.SIGHUP, signal.SIGPIPE):
        signal.signal(sig, signal.SIG_IGN)
    try:
        while os.read(0, 4096):
            pass
    except OSError:
        pass
    for attempt in range(_ATTEMPTS):
        try:
            stop_folder_servers(argv[1])
            return 0
        except (OSError, subprocess.SubprocessError) as exc:  # no ps, ps timed out
            print(f"watcher: attempt {attempt + 1} failed: {exc!r}", file=sys.stderr, flush=True)
            time.sleep(1.0)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
