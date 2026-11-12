#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Stop hook (headless): a ``/modernize --auto`` run never ends its turn with nothing running
and no ``MODERNIZE_RESULT`` line (#1574).

A headless session ends as soon as a turn ends with nothing running. Twice a scoped discourse
run's orchestrator answered each DynamoDB part's report with "Still waiting on ..." and, after
the last part reported, ended its turn the same way: nothing was running, so the session ended
with no merge, no synthesis and no result line. The command text tells it to run the status
command once, after the last report (#2121), and once more whenever it is not sure every
subagent has reported; this hook makes the step deterministic.

Claude Code calls a Stop hook when the main agent finishes responding (a subagent's end is
``SubagentStop``, a separate event this hook is not wired to). Its input carries
``background_tasks``, the session's background work still running, and
``last_assistant_message``. Exit 2 blocks the stop: Claude gets stderr as the reason and
continues the turn. Claude Code itself ends the turn after eight blocks in a row, and resets
that count whenever a tool is called, so a block that asks for a tool call cannot loop.
Checked against the 2.1.288 CLI e2e-llm installs (its Stop input schema: ``background_tasks``
is an empty array when nothing is in flight). ``stop_hook_active`` is not an allow: a second
"Still waiting" after a block is the stall this hook exists for, and the cap bounds it.

Running subagents under ``--auto`` (#2175). A headless (``claude -p``) session whose turn
ends while subagents run is not paused forever: Claude Code stops every background task
still running 10 minutes after the session's last turn (``CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS``,
600000 by default) and ends the session. A wordpress run lost its result line that way: its
orchestrator ended five turns in a row while DynamoDB part 1 was still out, the part made
no progress for 13 minutes, and 600 s after the last turn the CLI killed it and interrupted
the session. So under ``--auto`` a stop while a subagent (``background_tasks`` entry of
type ``subagent`` or ``local_agent``) is still running is blocked too, and the reason names
the bounded wait, ``run_schema_design.py --wait-for-report 110``, which keeps the turn going
and returns as soon as a design or review lands. A subagent dispatched more than
``HUNG_AFTER_SECONDS`` ago (its launch time is the session log's ``timestamp`` on the Agent
result that names its ``agentId``) is hung: the reason then asks for the ``failed`` result
line. Interactive runs are not blocked: there the session waits for the next report.

The hook blocks only when all of these hold:

- the event is the main agent's ``Stop`` (an ``agent_id`` or another event allows);
- the session's latest slash command is ``/modernize`` with ``--auto`` (or ``-y``): an
  interactive run legitimately stops to ask the user;
- no top-level ``MODERNIZE_RESULT:`` line was written yet (the last message included);
- a subagent is still running (the wait or hung reason above), or no background task is
  running at all (a background shell alone is a legitimate wait). A CLI that sends no
  ``background_tasks`` gets the narrower artifact test below instead.

With no ``background_tasks`` field it blocks only when the job's latest DynamoDB split, with no
merge run on it, has a readable draft for every group and either no group review request written
after the drafts (every part's design is done, 6c has not started) or a recorded review for every
group (every part's review is done, 6c is over, #2121): nothing else can be running.

The reason names the next step: when every DynamoDB part of the latest split has a readable
draft and neither the group reviews nor the merge have started, the ``--status`` command and
then steps 6c and 6d; when every part's review is recorded and the merge has not run, the
``run_pe_review.py`` command and then step 6d.

Read-only, stdlib only, no repo imports. Any error, a missing state file or an unreadable
transcript allows (fail open); the e2e-llm transcript check stays the backstop.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hook_common  # noqa: E402

RESULT = "MODERNIZE_RESULT:"
_RESULT_LINE = re.compile(r"^\s*MODERNIZE_RESULT:\s*(complete|failed)\b", re.MULTILINE)
_COMMAND = re.compile(
    r"<command-name>\s*/?([\w:-]+)\s*</command-name>(?:.*?<command-args>(.*?)</command-args>)?",
    re.DOTALL,
)
_ID = re.compile(r"[A-Za-z0-9_-]+")
_DB = re.compile(r"[A-Za-z0-9_.-]+")
_AUTO = re.compile(r"(?:^|\s)(?:--auto|-y)(?:\s|$)")
_AGENT_ID = re.compile(r"agentId:\s*([A-Za-z0-9_-]+)")
# The task types of a subagent in the Stop input's ``background_tasks`` (the 2.1.288 CLI
# sends ``local_agent``, later ones the display name ``subagent``).
_SUBAGENT_TYPES = {"local_agent", "subagent"}
# A subagent that has not reported this long after its dispatch is hung (#2175). The
# slowest design seen in e2e-llm runs took about 13 minutes (DocumentDB on discourse).
HUNG_AFTER_SECONDS = 30 * 60
# The bounded wait a headless orchestrator runs instead of ending its turn (#2175).
WAIT_SECONDS = 110


def _is_subagent(rec: dict[str, Any]) -> bool:
    return rec.get("parent_tool_use_id") is not None or rec.get("isSidechain") is True


def _texts(rec: dict[str, Any]) -> Iterator[str]:
    message = rec.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        yield content
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                yield str(block.get("text", ""))


def _command(text: str) -> tuple[str, str] | None:
    """``(name, args)`` of the slash command a top-level prompt invokes, if any."""
    match = _COMMAND.search(text)
    if match:
        return match.group(1), match.group(2) or ""
    stripped = text.lstrip()
    if stripped.startswith("/") and len(stripped) > 1:
        name, _, args = stripped[1:].partition(" ")
        return name.split()[0] if name else "", args
    return None


def _launch(rec: dict[str, Any]) -> tuple[str, float] | None:
    """``(agent id, launch epoch seconds)`` of a top-level async Agent result, if it is one."""
    result = rec.get("toolUseResult")
    agent = result.get("agentId") if isinstance(result, dict) else None
    if not isinstance(agent, str):
        message = rec.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        found = _AGENT_ID.search(json.dumps(content)) if isinstance(content, list) else None
        agent = found.group(1) if found else None
    stamp = rec.get("timestamp")
    if not agent or not isinstance(stamp, str):
        return None
    try:
        return agent, datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def session_state(
    lines: Iterator[str], launches: dict[str, float] | None = None
) -> tuple[bool, bool]:
    """``(auto_modernize, terminal)`` from the session transcript's lines: whether the latest
    top-level slash command is ``/modernize --auto``, and whether a top-level assistant message
    already wrote a ``MODERNIZE_RESULT`` line. Only lines naming a command, the result or an
    agent launch are parsed, so a long transcript reads fast. ``launches``, when given, gets
    each async subagent's launch time by its agent id (#2175)."""
    auto, terminal = False, False
    for line in lines:
        launch_line = launches is not None and "agentId" in line
        if (
            not launch_line
            and "command-name" not in line
            and RESULT not in line
            and '"/modernize' not in line
        ):
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or _is_subagent(rec):
            continue
        if launch_line and rec.get("type") == "user" and launches is not None:
            launch = _launch(rec)
            if launch:
                launches.setdefault(launch[0], launch[1])
                continue
        for text in _texts(rec):
            if rec.get("type") == "user":
                command = _command(text)
                if command:
                    auto = command[0] == "modernize" and bool(_AUTO.search(" " + command[1]))
                    terminal = False  # a new run starts
            elif rec.get("type") == "assistant" and _RESULT_LINE.search(text):
                terminal = True
    return auto, terminal


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _job(project: Path) -> tuple[str, str] | None:
    state = _read_json(project / ".modernizer-state.json")
    if not isinstance(state, dict):
        return None
    job, db = state.get("job_id"), state.get("database_name")
    if isinstance(job, str) and isinstance(db, str) and _ID.fullmatch(job) and _DB.fullmatch(db):
        return job, db
    return None


def dynamodb_parts_done(job_dir: Path) -> tuple[int, int] | None:
    """``(drafts written, groups)`` for the latest DynamoDB split whose every group has a
    readable draft, no merge has run and no group review request is newer than its drafts:
    every part is done and the review and merge have not started. None otherwise."""
    base = job_dir / "schema-dynamodb"
    versions = sorted(
        (int(p.name[1:]), p) for p in base.glob("v*") if p.name[1:].isdigit() and p.is_dir()
    )
    if not versions:
        return None
    vdir = versions[-1][1]
    manifest = _read_json(vdir / "groups_manifest.json")
    groups = manifest.get("groups") if isinstance(manifest, dict) else None
    if not isinstance(groups, list) or not groups:
        return None
    if (vdir / "merge_record.json").exists() or (vdir / "schema_output.json").exists():
        return None
    newest = 0.0
    for i, group in enumerate(groups):
        index = group.get("group_index", i) if isinstance(group, dict) else i
        draft = vdir / f"schema_draft_group_{index}.json"
        if not isinstance(_read_json(draft), dict):
            return None
        newest = max(newest, draft.stat().st_mtime)
    for request in (job_dir / "llm_requests").glob("pe_review_dynamodb_group_*.json"):
        if request.stat().st_mtime >= newest:
            return None  # the reviews started (6c): their subagents may be running
    return len(groups), len(groups)


def dynamodb_reviews_done(job_dir: Path) -> int | None:
    """The group count of the latest DynamoDB split whose every group has a readable draft
    and a recorded review (its ``design_trace_group_<G>.json`` holds ``pe_review``, which
    ``run_pe_review.py --finalize`` writes), with no merge run: every part's review is done
    and 6d has not started (#2121). None otherwise."""
    base = job_dir / "schema-dynamodb"
    versions = sorted(
        (int(p.name[1:]), p) for p in base.glob("v*") if p.name[1:].isdigit() and p.is_dir()
    )
    if not versions:
        return None
    vdir = versions[-1][1]
    manifest = _read_json(vdir / "groups_manifest.json")
    groups = manifest.get("groups") if isinstance(manifest, dict) else None
    if not isinstance(groups, list) or not groups:
        return None
    if (vdir / "merge_record.json").exists() or (vdir / "schema_output.json").exists():
        return None
    for i, group in enumerate(groups):
        index = group.get("group_index", i) if isinstance(group, dict) else i
        if not isinstance(_read_json(vdir / f"schema_draft_group_{index}.json"), dict):
            return None
        trace = _read_json(vdir / f"design_trace_group_{index}.json")
        if not (isinstance(trace, dict) and isinstance(trace.get("pe_review"), dict)):
            return None
    return len(groups)


def _reason(job: str, db: str, parts: tuple[int, int] | None, reviewed: int | None = None) -> str:
    if parts:
        hint = (
            f"Every DynamoDB part has its draft ({parts[0]} of {parts[1]}) and no review or "
            "merge has started: run `uv run python scripts/run_schema_design.py --job-id "
            f"{job} --db {db} --engine dynamodb --status`, then the 6c review and the 6d merge."
        )
    elif reviewed:
        hint = (
            f"Every DynamoDB part's review is recorded ({reviewed} of {reviewed}) and the merge "
            f"has not run: run `uv run python scripts/run_pe_review.py --job-id {job} --db {db}`, "
            "then the 6d merge."
        )
    else:
        hint = "Run the documented status command of the current phase and its next step."
    return (
        "Blocked: nothing is running and this /modernize --auto run has no MODERNIZE_RESULT "
        f"line, so ending the turn ends the run. {hint} Continue now, or end with "
        "`MODERNIZE_RESULT: failed phase=<phase> reason=<one line>`."
    )


def _subagents(tasks: Any) -> list[dict[str, Any]]:
    """The running subagents among the Stop input's ``background_tasks``."""
    if not isinstance(tasks, list):
        return []
    return [
        t
        for t in tasks
        if isinstance(t, dict) and (t.get("type") in _SUBAGENT_TYPES or "agent_type" in t)
    ]


def _label(task: dict[str, Any]) -> str:
    text = " ".join(str(task.get("description") or task.get("id") or "a subagent").split())
    return text[:80]


def _waiting_reason(job: str, db: str, agents: list[dict[str, Any]]) -> str:
    names = "; ".join(_label(a) for a in agents[:3]) + (" ..." if len(agents) > 3 else "")
    return (
        f"Blocked: {len(agents)} subagent(s) you dispatched are still running ({names}) and "
        "this /modernize --auto run is headless: Claude Code stops a headless session's "
        "subagents 10 minutes after its last turn, so never end the turn while one runs. "
        f"Run `uv run python scripts/run_schema_design.py --job-id {job} --db {db} "
        f"--wait-for-report {WAIT_SECONDS}` on its own, then go on from its line and the "
        "reports that came in (Waiting Rule)."
    )


def _hung_reason(phase: str, task: dict[str, Any], minutes: int) -> str:
    label = _label(task)
    return (
        f"Blocked: the subagent '{label}' was dispatched {minutes} minutes ago and has not "
        f"reported; a headless run treats a subagent that has not reported within "
        f"{HUNG_AFTER_SECONDS // 60} minutes as hung. End the run now with "
        f"`MODERNIZE_RESULT: failed phase={phase} reason=subagent {label} did not report "
        f"within {HUNG_AFTER_SECONDS // 60} minutes`."
    )


def _phase(project: Path) -> str:
    state = _read_json(project / ".modernizer-state.json")
    phase = state.get("current_phase") if isinstance(state, dict) else None
    return phase if isinstance(phase, str) and _ID.fullmatch(phase) else "schema_design"


def decide(event: dict[str, Any], project: Path, now: float | None = None) -> str | None:
    """The block reason, or None to allow the stop."""
    if event.get("agent_id") or event.get("hook_event_name") not in (None, "Stop"):
        return None
    tasks = event.get("background_tasks")
    last = event.get("last_assistant_message")
    if isinstance(last, str) and _RESULT_LINE.search(last):
        return None
    path = event.get("transcript_path")
    if not isinstance(path, str) or not path:
        return None
    agents = _subagents(tasks)
    if isinstance(tasks, list) and tasks and not agents:
        return None  # only a background shell is running: a legitimate wait
    launches: dict[str, float] = {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        auto, terminal = session_state(iter(fh), launches)
    if not auto or terminal:
        return None
    job = _job(project)
    if job is None:
        return None
    if agents:
        # Headless, a turn that ends while subagents run can end the run (#2175).
        now = time.time() if now is None else now
        for agent in agents:
            launched = launches.get(str(agent.get("id")))
            if launched is not None and now - launched >= HUNG_AFTER_SECONDS:
                return _hung_reason(_phase(project), agent, int((now - launched) // 60))
        return _waiting_reason(job[0], job[1], agents)
    root = Path(os.environ.get("ARTIFACT_DIR") or project / "artifacts")
    if not root.is_absolute():
        root = project / root
    job_dir = root / job[1] / job[0]
    parts = dynamodb_parts_done(job_dir)
    reviewed = None if parts else dynamodb_reviews_done(job_dir)
    if not isinstance(tasks, list) and parts is None and reviewed is None:
        return None  # no background_tasks field: block only on the artifact tests
    return _reason(job[0], job[1], parts, reviewed)


def main() -> int:
    try:
        event = hook_common.read_event()
        project = Path(os.environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd") or os.getcwd())
        reason = decide(event, project)
    except Exception:  # noqa: BLE001 - a Stop hook fails open
        return 0
    return int(hook_common.block(reason)) if reason else 0


if __name__ == "__main__":
    sys.exit(main())
