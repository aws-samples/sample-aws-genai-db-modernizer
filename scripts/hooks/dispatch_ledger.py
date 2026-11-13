# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The dispatch ledger of a headless ``/modernize --auto`` run (#2215).

``run_schema_design.py --wait-for-report`` cannot tell from the artifacts whether a
subagent is still running: a design between two ``--finalize`` attempts, a retry not
started yet and a synthesis just dispatched all look idle. So each dispatch and each
report is recorded here, and the wait prints ``nothing_pending`` only when the ledger has
no outstanding dispatch of the current session; with no readable ledger, or no known
session, it keeps its bounded wait (fail safe).

Who writes it: the orchestrator, through ``run_schema_design.py --mark-dispatched`` /
``--mark-reported`` (required by the Waiting Rule); the hooks ``record_dispatch.py``
(each Agent launch, and a ``SendMessage`` that resumes a recorded subagent) and
``record_report.py`` (``SubagentStop``), at the source; and the Stop hook
(``require_modernize_result.py``), which reconciles it with ``background_tasks``.
Recording an event twice changes nothing.

Sessions: each entry keeps the session that recorded it (a hook event's ``session_id``,
or ``CLAUDE_CODE_SESSION_ID`` for the script). Only the current session's entries are
outstanding for it, so an entry left by a session that crashed, or by a ``/wave-edit``
with no Stop hook, never holds a later run's wait or reads as hung there. The ledger
itself never calls an entry hung: only the Stop hook does, when it sees the subagent
running in ``background_tasks`` past its limit.

One file, ``<job>/_logs/dispatch_ledger.json``: ``{"subagents": {<id>: {"dispatched_at",
"reported_at", "session", "by"}}}``, epoch seconds. Every read-modify-write holds an
exclusive ``flock`` on ``dispatch_ledger.lock`` next to it (parallel hooks, as for 6b's
second message, would otherwise drop each other's entries), and the file is replaced
atomically (temp file + ``os.replace``). Stdlib only: the hooks import it.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - not POSIX: no lock, the replace stays atomic
    fcntl = None  # type: ignore[assignment]

LEDGER = "_logs/dispatch_ledger.json"
LOCK = "_logs/dispatch_ledger.lock"
_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_SESSION = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")


def valid_id(value: str) -> bool:
    return bool(_ID.match(value))


def valid_session(value: Any) -> bool:
    return isinstance(value, str) and bool(_SESSION.match(value))


def current_session(environ: Any = None) -> str | None:
    """This process's Claude Code session, from its environment, or None."""
    env = os.environ if environ is None else environ
    value = env.get("CLAUDE_CODE_SESSION_ID") or env.get("CLAUDE_SESSION_ID")
    return value if valid_session(value) else None


def read(job_dir: Path) -> dict[str, dict[str, Any]] | None:
    """The ledger's entries by id, or None when there is no readable ledger."""
    try:
        data = json.loads((job_dir / LEDGER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entries = data.get("subagents") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        return None
    return {str(k): v for k, v in entries.items() if isinstance(v, dict)}


@contextlib.contextmanager
def _locked(job_dir: Path) -> Iterator[None]:
    lock = job_dir / LOCK
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _write(job_dir: Path, entries: dict[str, dict[str, Any]]) -> None:
    path = job_dir / LEDGER
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"subagents": entries}, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def outstanding(
    entries: dict[str, dict[str, Any]], session: str | None = None
) -> dict[str, dict[str, Any]]:
    """The entries dispatched and not reported yet; with ``session``, only its own."""
    return {
        k: v
        for k, v in entries.items()
        if v.get("dispatched_at") is not None
        and v.get("reported_at") is None
        and (session is None or v.get("session") == session)
    }


def mark_dispatched(
    job_dir: Path,
    ids: list[str],
    now: float | None = None,
    *,
    session: str | None = None,
    reopen: bool = False,
    by: str = "",
) -> dict[str, dict[str, Any]]:
    """Record ``ids`` as dispatched by ``session``. A report recorded first (it can come in
    before the dispatch is recorded) keeps the entry settled, unless ``reopen`` (the Stop
    hook, which sees it still running, or a ``SendMessage`` that resumes it)."""
    now = time.time() if now is None else now
    with _locked(job_dir):
        entries = read(job_dir) or {}
        for agent in ids:
            entry = entries.get(agent) or {}
            if entry.get("reported_at") is not None and not reopen:
                continue
            entries[agent] = {
                "dispatched_at": now if reopen else (entry.get("dispatched_at") or now),
                "reported_at": None,
                "session": session or entry.get("session"),
                **({"by": by} if by else {}),
            }
        _write(job_dir, entries)
    return entries


def mark_reported(
    job_dir: Path,
    ids: list[str],
    now: float | None = None,
    *,
    session: str | None = None,
    by: str = "",
) -> dict[str, dict[str, Any]]:
    """Record ``ids`` as reported (an id never recorded as dispatched is kept settled)."""
    now = time.time() if now is None else now
    with _locked(job_dir):
        entries = read(job_dir) or {}
        for agent in ids:
            entry = entries.get(agent) or {}
            entries[agent] = {
                "dispatched_at": entry.get("dispatched_at") or now,
                "reported_at": now,
                "session": entry.get("session") or session,
                **({"by": by} if by else {}),
            }
        _write(job_dir, entries)
    return entries


def log(job_dir: Path, line: str) -> None:
    """Append one line to the job's ``_logs/dispatch_hooks.log`` (best effort)."""
    try:
        path = job_dir / "_logs" / "dispatch_hooks.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {' '.join(line.split())[:300]}\n")
    except OSError:
        pass
