# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""One API-driven change to a job at a time (#1991 review).

``POST .../resume``, the schema confirm routes and the assumptions panel's change all
write a job's artifacts. Each takes :func:`job_lock` for the job; a second caller gets
:class:`JobBusyError` at once (the routes answer 409) instead of waiting or interleaving.
The lock is reentrant within one thread, so a change that resumes a phase (the
orchestrator's ``resume`` takes it too) does not lock itself out. It guards this process
only: a ``scripts/run_*.py`` run on the same job is another process.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

_GUARD = threading.Lock()
#: job id -> (owning thread id, depth)
_HELD: dict[str, tuple[int, int]] = {}


class JobBusyError(RuntimeError):
    """Another change to this job is running."""

    def __init__(self, job_id: str) -> None:
        super().__init__(f"Another change to job {job_id} is running; try again when it finishes.")
        self.job_id = job_id


@contextmanager
def job_lock(job_id: str) -> Iterator[None]:
    """Hold the job's lock for the block; JobBusyError when another thread holds it."""
    me = threading.get_ident()
    with _GUARD:
        owner, depth = _HELD.get(job_id, (me, 0))
        if owner != me:
            raise JobBusyError(job_id)
        _HELD[job_id] = (me, depth + 1)
    try:
        yield
    finally:
        with _GUARD:
            owner, depth = _HELD[job_id]
            if depth <= 1:
                del _HELD[job_id]
            else:
                _HELD[job_id] = (owner, depth - 1)


__all__ = ["JobBusyError", "job_lock"]
