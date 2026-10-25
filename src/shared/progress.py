"""The pipeline phases' progress lines: printed for the command line, logged for a library call.

The phases report their progress with :func:`say`, which takes ``print``'s arguments.
By default it prints, so every script's stdout is exactly what ``print`` wrote (a
script that swaps ``sys.stdout`` for a log file or a compact console still gets every
line). Inside :func:`to_logger` the lines go to a logger instead, one record per line:
``assess()`` uses it so a library call writes nothing to its caller's stdout.

The destination is a context variable, so it applies only to the code that
:func:`to_logger` wraps and to the work it hands to other threads through
:func:`in_context`. Any other thread's ``print`` (or ``say``) is untouched.
"""

from __future__ import annotations

import contextvars
import logging
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

_SINK: contextvars.ContextVar[_LineLogger | None] = contextvars.ContextVar(
    "progress_sink", default=None
)


class _LineLogger:
    """Logs each complete line it is given; an unfinished line waits for the rest."""

    def __init__(self, logger: logging.Logger, level: int, extra: dict[str, Any] | None) -> None:
        self._logger = logger
        self._level = level
        self._extra = extra
        self._pending = ""
        self._lock = threading.Lock()

    def write(self, text: str) -> None:
        with self._lock:
            self._pending += text
            *lines, self._pending = self._pending.split("\n")
        self._log(lines)

    def flush(self) -> None:
        with self._lock:
            lines, self._pending = [self._pending], ""
        self._log(lines)

    def _log(self, lines: list[str]) -> None:
        for line in lines:
            if line.strip():
                self._logger.log(self._level, line, extra=self._extra)


def say(*values: Any, sep: str | None = " ", end: str | None = "\n", **kwargs: Any) -> None:
    """``print(*values, ...)``, or one log record per line inside :func:`to_logger`.

    A call that names a ``file`` other than ``sys.stdout`` (``file=sys.stderr``, a log
    file) always prints there.
    """
    sink = _SINK.get()
    target = kwargs.get("file")
    if sink is None or (target is not None and target is not sys.stdout):
        print(*values, sep=sep, end=end, **kwargs)
        return
    text = (" " if sep is None else sep).join(str(v) for v in values)
    sink.write(text + ("\n" if end is None else end))


@contextmanager
def to_logger(
    logger: logging.Logger, level: int = logging.INFO, extra: dict[str, Any] | None = None
) -> Iterator[None]:
    """Inside the block, :func:`say` logs each line to ``logger`` instead of printing.
    ``extra`` is set on every record (``assess()`` names its job: ``record.job_id``)."""
    sink = _LineLogger(logger, level, extra)
    token = _SINK.set(sink)
    try:
        yield
    finally:
        _SINK.reset(token)
        sink.flush()


def in_context[T](fn: Callable[..., T]) -> Callable[..., T]:
    """``fn`` bound to the caller's context, for a thread pool: its progress goes where
    the caller's goes (a pool's worker threads do not inherit context variables)."""
    context = contextvars.copy_context()

    def run(*args: Any, **kwargs: Any) -> T:
        # One copy per call: a context cannot be entered by two threads at once.
        return context.copy().run(fn, *args, **kwargs)

    return run
