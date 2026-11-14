#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Record every subagent dispatch in the job's dispatch ledger, at its source (#2215).

``run_schema_design.py --wait-for-report`` prints ``nothing_pending`` only when the job's
dispatch ledger (``dispatch_ledger.py``) lists no outstanding subagent of the current
session, so each dispatch is recorded here as well as by the orchestrator's (required)
``--mark-dispatched``:

- ``PostToolUse`` on the ``Agent`` (or older ``Task``) tool: the tool result of an async
  launch carries the subagent's ``agentId`` (``"status": "async_launched"``), the id its
  report's task id, ``background_tasks`` and ``TaskStop`` use. It is marked dispatched. A
  synchronous call has already finished when the hook runs (``"status": "completed"``):
  it is marked reported, so it never holds the wait.
- ``PostToolUse`` on ``SendMessage`` to an id the ledger holds: the message resumes that
  subagent, so its entry is opened again.
- ``SubagentStop`` (``record_report.py``): when its input names the subagent
  (``agent_id``), the entry is marked reported; when it does not, nothing is written and
  the orchestrator's ``--mark-reported`` records the report.

Scope: the job is the cursor in ``.modernizer-state.json`` (``job_id``, ``database_name``)
under ``$ARTIFACT_DIR`` or ``./artifacts``, and only the session running it writes: the
event's ``session_id`` must be the cursor's ``session_id`` (``run_assessment.py`` records
it). Any other session, a cursor with no session, or no folder for the job: no write.

Never blocks: any error exits 0 (fail open). Each event it acts on, and each tool result it
cannot read, gets a line in the job's ``_logs/dispatch_hooks.log``, written before the
ledger: a ``received`` line with no outcome after it is a hook that timed out. Stdlib
only, no repo imports.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dispatch_ledger  # noqa: E402
import hook_common  # noqa: E402

_ID = re.compile(r"[A-Za-z0-9_-]+")
_DB = re.compile(r"[A-Za-z0-9_.-]+")
_AGENT_ID = re.compile(r"agentId:\s*([A-Za-z0-9_-]+)")
_AGENT_TOOLS = {"Agent", "Task"}


def _cursor(project: Path) -> tuple[Path, str | None] | None:
    """The current job's artifact folder and the session running it, or None."""
    try:
        state = json.loads((project / ".modernizer-state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict):
        return None
    job, db = state.get("job_id"), state.get("database_name")
    if not (isinstance(job, str) and isinstance(db, str)):
        return None
    if not (_ID.fullmatch(job) and _DB.fullmatch(db)):
        return None
    root = Path(os.environ.get("ARTIFACT_DIR") or project / "artifacts")
    if not root.is_absolute():
        root = project / root
    folder = root / db / job
    if not folder.is_dir():
        return None
    session = state.get("session_id")
    return folder, session if dispatch_ledger.valid_session(session) else None


def job_dir(project: Path, session: Any = None) -> Path | None:
    """The current job's artifact folder when ``session`` is the one running it, or None."""
    found = _cursor(project)
    if found is None:
        return None
    folder, owner = found
    if owner is None or session != owner:
        return None
    return folder


def launched(response: Any) -> tuple[str, bool] | None:
    """``(agent id, still running)`` from an Agent tool result, or None."""
    status = None
    agent = None
    if isinstance(response, dict):
        agent = response.get("agentId")
        status = response.get("status")
        if not isinstance(agent, str):
            agent = None
    if agent is None:
        found = _AGENT_ID.search(response if isinstance(response, str) else json.dumps(response))
        agent = found.group(1) if found else None
    if not agent or not dispatch_ledger.valid_id(agent):
        return None
    return agent, status != "completed"


def record(event: dict[str, Any], project: Path) -> str | None:
    """Write the event's dispatch or report to the ledger; return what it wrote."""
    name = event.get("hook_event_name")
    session = event.get("session_id")
    tool = event.get("tool_name")
    if name == "PostToolUse" and not event.get("agent_id") and tool in _AGENT_TOOLS:
        folder = job_dir(project, session)
        if folder is None:
            return None
        dispatch_ledger.log(folder, f"received PostToolUse {tool} {event.get('tool_use_id')}")
        found = launched(event.get("tool_response"))
        if not found:
            shape = type(event.get("tool_response")).__name__
            dispatch_ledger.log(folder, f"no agent id in the {tool} result ({shape})")
            return None
        agent, running = found
        if running:
            dispatch_ledger.mark_dispatched(folder, [agent], session=session, by="dispatch hook")
            dispatch_ledger.log(folder, f"dispatched {agent}")
            return f"dispatched {agent}"
        dispatch_ledger.mark_reported(folder, [agent], session=session, by="dispatch hook")
        dispatch_ledger.log(folder, f"reported {agent} (a synchronous call)")
        return f"reported {agent}"
    if name == "PostToolUse" and not event.get("agent_id") and tool == "SendMessage":
        target = hook_common.tool_input(event).get("to")
        folder = job_dir(project, session)
        if folder is None or not isinstance(target, str) or not dispatch_ledger.valid_id(target):
            return None
        entries = dispatch_ledger.read(folder) or {}
        if entries.get(target, {}).get("session") != session:
            return None  # not a subagent this session recorded
        dispatch_ledger.mark_dispatched(
            folder, [target], session=session, reopen=True, by="resume hook"
        )
        dispatch_ledger.log(folder, f"reopened {target} (SendMessage resumes it)")
        return f"reopened {target}"
    if name == "SubagentStop":
        stopped = event.get("agent_id")
        folder = job_dir(project, session)
        if folder is None:
            return None
        if not isinstance(stopped, str) or not dispatch_ledger.valid_id(stopped):
            dispatch_ledger.log(folder, "SubagentStop with no agent id: nothing recorded")
            return None
        dispatch_ledger.mark_reported(folder, [stopped], session=session, by="subagent stop hook")
        dispatch_ledger.log(folder, f"reported {stopped}")
        return f"reported {stopped}"
    return None


def main() -> int:
    try:
        event = hook_common.read_event()
        project = Path(os.environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd") or os.getcwd())
        record(event, project)
    except Exception:  # noqa: BLE001 - this hook never blocks
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
