#!/usr/bin/env python3
"""Stop hook (headless): a ``/modernize --auto`` run never ends its turn with nothing running
and no ``MODERNIZE_RESULT`` line (#1574).

A headless session ends as soon as a turn ends with nothing running. Twice a scoped discourse
run's orchestrator answered each DynamoDB part's report with "Still waiting on ..." and, after
the last part reported, ended its turn the same way: nothing was running, so the session ended
with no merge, no synthesis and no result line. The command text tells it to run the status
command after every report; this hook makes the step deterministic.

Claude Code calls a Stop hook when the main agent finishes responding (a subagent's end is
``SubagentStop``, a separate event this hook is not wired to). Its input carries
``background_tasks``, the session's background work still running, and
``last_assistant_message``. Exit 2 blocks the stop: Claude gets stderr as the reason and
continues the turn. Claude Code itself ends the turn after eight blocks in a row, and resets
that count whenever a tool is called, so a block that asks for a tool call cannot loop.
Checked against the 2.1.288 CLI e2e-llm installs (its Stop input schema: ``background_tasks``
is an empty array when nothing is in flight). ``stop_hook_active`` is not an allow: a second
"Still waiting" after a block is the stall this hook exists for, and the cap bounds it.

The hook blocks only when all of these hold:

- the event is the main agent's ``Stop`` (an ``agent_id`` or another event allows);
- no background task is running (a part still designing is a legitimate wait). A CLI that
  sends no ``background_tasks`` gets the narrower artifact test below instead;
- the session's latest slash command is ``/modernize`` with ``--auto`` (or ``-y``): an
  interactive run legitimately stops to ask the user;
- no top-level ``MODERNIZE_RESULT:`` line was written yet (the last message included).

With no ``background_tasks`` field it blocks only when the job's latest DynamoDB split has a
readable draft for every group, no merge has run on it and no group review request was written
after the drafts: every part is done and nothing else can be running.

Read-only, stdlib only, no repo imports. Any error, a missing state file or an unreadable
transcript allows (fail open); the e2e-llm transcript check stays the backstop.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Iterator
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


def session_state(lines: Iterator[str]) -> tuple[bool, bool]:
    """``(auto_modernize, terminal)`` from the session transcript's lines: whether the latest
    top-level slash command is ``/modernize --auto``, and whether a top-level assistant message
    already wrote a ``MODERNIZE_RESULT`` line. Only lines naming a command or the result are
    parsed, so a long transcript reads fast."""
    auto, terminal = False, False
    for line in lines:
        if "command-name" not in line and RESULT not in line and '"/modernize' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or _is_subagent(rec):
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


def _reason(job: str, db: str, parts: tuple[int, int] | None) -> str:
    hint = (
        f"Every DynamoDB part has its draft ({parts[0]} of {parts[1]}) and no review or merge "
        "has started: run `uv run python scripts/run_schema_design.py --job-id "
        f"{job} --db {db} --engine dynamodb --status`, then the 6c review and the 6d merge."
        if parts
        else "Run the documented status command of the current phase and its next step."
    )
    return (
        "Blocked: nothing is running and this /modernize --auto run has no MODERNIZE_RESULT "
        f"line, so ending the turn ends the run. {hint} Continue now, or end with "
        "`MODERNIZE_RESULT: failed phase=<phase> reason=<one line>`."
    )


def decide(event: dict[str, Any], project: Path) -> str | None:
    """The block reason, or None to allow the stop."""
    if event.get("agent_id") or event.get("hook_event_name") not in (None, "Stop"):
        return None
    tasks = event.get("background_tasks")
    if isinstance(tasks, list) and tasks:
        return None  # a subagent is still running: a legitimate wait
    last = event.get("last_assistant_message")
    if isinstance(last, str) and _RESULT_LINE.search(last):
        return None
    path = event.get("transcript_path")
    if not isinstance(path, str) or not path:
        return None
    with open(path, encoding="utf-8", errors="replace") as fh:
        auto, terminal = session_state(iter(fh))
    if not auto or terminal:
        return None
    job = _job(project)
    if job is None:
        return None
    root = Path(os.environ.get("ARTIFACT_DIR") or project / "artifacts")
    if not root.is_absolute():
        root = project / root
    parts = dynamodb_parts_done(root / job[1] / job[0])
    if not isinstance(tasks, list) and parts is None:
        return None  # no background_tasks field: block only on the artifact test
    return _reason(job[0], job[1], parts)


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
