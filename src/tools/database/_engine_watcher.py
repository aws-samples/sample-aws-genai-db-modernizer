# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Stops a disposable engine when the process that started it is gone (#2350).

``disposable_engine.start`` runs this file as its own process, with a pipe from the
owner as standard input. The owner never writes to it: the operating system closes the
write end when the owner exits, however it exits (``SIGKILL``, a test timeout's
``os._exit``, a killed xdist worker), and that end-of-file is the signal. Then every
process whose command line names the engine's folder is stopped the engine's own way
(``SIGINT`` for PostgreSQL's fast shutdown, ``SIGTERM`` for MySQL), and ``SIGKILL``
only when it has not gone in time, because a killed postmaster leaks its System V
shared-memory segment.

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


def _processes() -> list[tuple[int, str]]:
    out = subprocess.run(  # nosec B603 B607 - fixed arguments, no shell
        ["ps", "-axww", "-o", "pid=,command="],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    ).stdout
    found: list[tuple[int, str]] = []
    for line in out.splitlines():
        pid_text, _, command = line.strip().partition(" ")
        if pid_text.isdigit():
            found.append((int(pid_text), command.strip()))
    return found


def _ours(pattern: re.Pattern[str], pid: int) -> str | None:
    """The command line of ``pid`` when it still names the folder, else None."""
    for other, command in _processes():
        if other == pid:
            return command if pattern.search(command) else None
    return None


def _signal_for(command: str) -> signal.Signals:
    program = os.path.basename(command.split(" ", 1)[0])
    return signal.SIGINT if program.startswith(("postgres", "postmaster")) else signal.SIGTERM


def stop_folder_servers(folder: str) -> None:
    """Stop every process, but this one, that has ``folder`` in its command line."""
    names = {folder, os.path.realpath(folder)}
    pattern = re.compile("|".join(re.escape(n) + r"(?![\w.-])" for n in sorted(names)))
    mine = os.getpid()
    victims = {
        pid: command for pid, command in _processes() if pid != mine and pattern.search(command)
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
    stop_folder_servers(argv[1])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
