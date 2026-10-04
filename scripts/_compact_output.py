"""Compact stdout for a script whose progress output is large (issue #278).

A headless Claude Code session keeps a Bash result inline only while it is
small (about 20 KB); a larger one is saved to a file outside the repository
and the session sees a 2 KB preview. ``scripts/run_assessment.py`` prints
~108 KB of progress for the discourse sample, so the ``{"phase": ...}`` status
lines /modernize reads were lost in that file.

:class:`CompactConsole` takes over ``sys.stdout`` and ``sys.stderr``: every
progress line goes to a log file (buffered in memory until the script knows
the job directory and calls :meth:`CompactConsole.attach`), and only the
status lines passed to :meth:`CompactConsole.status` reach the real stdout.
When the script ends, :meth:`CompactConsole.close` prints one last JSON line
that points at the log::

    {"log": "artifacts/<db>/<job>/_logs/run_assessment.log", "log_offset": 1, "log_lines": 1371}

``log_offset``/``log_lines`` are the Read tool ``offset``/``limit`` of the
lines this run appended (the log is appended to across runs of the same job).
If the job directory was never known (an error before it existed), the line is
``{"log": null, "output_tail": "<last few KB of the output>"}`` instead.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import threading
from typing import IO, Any

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
TAIL_CHARS = 4000


def display_path(path: str) -> str:
    """``path`` relative to the cwd when it is under it (the form Read allows), else absolute."""
    absolute = os.path.abspath(path)
    rel = os.path.relpath(absolute)
    return absolute if rel == ".." or rel.startswith(".." + os.sep) else rel


def _count_lines(path: str) -> int:
    if not os.path.exists(path):
        return 0
    with open(path, "rb") as f:
        return sum(1 for _ in f)


class CompactConsole:
    """Stands in for ``sys.stdout``/``sys.stderr``; see the module docstring."""

    def __init__(self, stdout: IO[str], stderr: IO[str]) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.path: str | None = None
        self._lock = threading.RLock()
        self._buffer = io.StringIO()
        self._file: IO[str] | None = None
        self._offset = 0
        self._closed = False

    # -- file-like interface (what print() and tracebacks use) ---------------
    @property
    def encoding(self) -> str:
        return "utf-8"

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return False

    def write(self, text: str) -> int:
        clean = _ANSI_RE.sub("", text)
        with self._lock:
            target = self._file if self._file is not None else self._buffer
            target.write(clean)
        return len(text)

    def flush(self) -> None:
        with self._lock:
            if self._file is not None:
                self._file.flush()

    # -- compact-mode API ----------------------------------------------------
    def attach(self, path: str) -> None:
        """Start writing to ``path`` (appending), first flushing what was buffered."""
        with self._lock:
            if self._file is not None:
                return
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            self._offset = _count_lines(path) + 1
            self._file = open(path, "a", encoding="utf-8")  # noqa: SIM115 - closed in close()
            self._file.write(self._buffer.getvalue())
            self._buffer = io.StringIO()
            self.path = path

    def status(self, line: str) -> None:
        """Print ``line`` to the real stdout and record it in the log too."""
        with self._lock:
            self.stdout.write(line + "\n")
            self.stdout.flush()
            self.write(line + "\n")

    def pointer(self) -> dict[str, Any]:
        """The final stdout line's payload (also usable before :meth:`close`)."""
        if self.path is None:
            return {"log": None, "output_tail": self._buffer.getvalue()[-TAIL_CHARS:]}
        self.flush()
        total = _count_lines(self.path)
        return {
            "log": display_path(self.path),
            "log_offset": self._offset,
            "log_lines": total - self._offset + 1,
        }

    def close(self) -> None:
        """Print the pointer line and close the log. Idempotent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            info = self.pointer()
            if self._file is not None:
                self._file.close()
            self.stdout.write(json.dumps(info) + "\n")
            self.stdout.flush()


def want_verbose(flag: bool, stdout: IO[str] | None = None) -> bool:
    """Verbose (the old full stdout) with ``--verbose`` or when stdout is a terminal."""
    stream = sys.stdout if stdout is None else stdout
    try:
        return flag or bool(stream.isatty())
    except (AttributeError, ValueError):
        return flag
