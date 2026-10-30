# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Expensive sample runs, built once per test session and shared (#956).

Several unit tests run the whole deterministic pipeline (or
``run_assessment.py``) on a bundled sample. Module-scoped fixtures alone
do not stop the repeats: under pytest-xdist every worker that receives one
of a module's tests sets the fixture up again, and two modules that need
the same run each build their own.

``cached_run(tmp_path_factory, key, build)`` runs ``build(directory)`` at
most once per session, across every xdist worker, behind a file lock in the
session's shared temporary directory. Without xdist the cache lives in the
session's own base temp directory. Either way it never outlives the session,
so a code change can never be tested against a stale run.

The cached directory is shared: treat it as read-only. A test that writes to
a job (a resume, a finalize, a new assignment version) takes its own copy
with ``copy_run`` first.
"""

from __future__ import annotations

import fcntl
import os
import shutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

_COMPLETE = ".complete"


def _cache_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    base: Path = tmp_path_factory.getbasetemp()
    if os.environ.get("PYTEST_XDIST_WORKER"):
        # Each worker's basetemp is <session dir>/popen-gwN; the session dir
        # is shared by every worker of this run and by no other run.
        base = base.parent
    root = base / "shared-runs"
    root.mkdir(parents=True, exist_ok=True)
    return root


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    """An exclusive lock on ``path`` shared by every process of the session."""
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def cached_run(
    tmp_path_factory: pytest.TempPathFactory, key: str, build: Callable[[Path], object]
) -> Path:
    """Return the directory ``build`` filled for ``key``, building it at most once.

    ``build``'s return value is ignored: everything a test needs must be on disk.
    """
    root = _cache_root(tmp_path_factory)
    directory = root / key
    with _locked(root / f"{key}.lock"):
        if not (directory / _COMPLETE).exists():
            # A partial directory is what a failed build leaves; start over.
            shutil.rmtree(directory, ignore_errors=True)
            directory.mkdir()
            build(directory)
            (directory / _COMPLETE).touch()
    return directory


def copy_run(cached: Path, dest: Path) -> Path:
    """A private, writable copy of a cached run at ``dest`` (which must not exist)."""
    shutil.copytree(cached, dest, symlinks=True, ignore=shutil.ignore_patterns(_COMPLETE))
    return dest
