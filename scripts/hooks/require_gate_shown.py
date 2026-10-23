#!/usr/bin/env python3
"""PreToolUse hook (Bash, Agent): no schema design before the approval gate ran on its own (#1401).

After reality check, ``/modernize`` runs ``scripts/print_summary.py --gate --mode <mode>``; its
output (the gate block, ending with the summary page's URL unless the mode is chat) is what the
user sees at the gate, so the model does not have to retype it (maintainer decision on #1401).
What must hold is the order: the gate runs in an earlier assistant message than the first
schema-design call. The #1358 failures batched the two in one message, so the run moved on
before the gate was in front of anyone.

This hook refuses a Phase 6 call for a job unless a successful gate call for that job ran in an
earlier assistant message. A Phase 6 call is a real invocation of
``uv run python scripts/run_schema_design.py --job-id <id> ...`` (not ``--help``, not text
inside another command, a quoted body or a heredoc) or a dispatch whose prompt starts
``Run /design-schema-...`` or ``Follow /design-schema-...`` with ``job_id=<id>``. Only a
``/modernize`` run is held:

- a real ``print_summary.py --gate --job-id <id>`` call holds the job; once its result is in
  the transcript, it keeps holding only when that block has a ``Summary page`` line (chat mode
  has none and is not held);
- a real ``run_assessment.py --job-id <id> --resume-reality-check`` call holds the job only
  when the session's latest slash command is ``/modernize``, so skipping the gate is refused,
  while ``/reality-check`` then ``/design-schema-<engine>`` by hand is never held.

A gate call is successful when its result is recorded and is not an error (a JSON error line or
an error result). "Earlier message" compares the assistant message of the gate call with the
message of this call (``tool_use_id`` in the event, looked up in the transcript; the hook
re-reads for up to two seconds until that call is recorded). It reads the session transcript
(``transcript_path``) one line at a time; subagent records never count. A call whose event
carries ``agent_id`` runs inside a subagent and is not held. Without ``agent_id`` a subagent
call is judged against the same session, which is still safe: its dispatch passed only once
the gate had run. Stdin: the hook event JSON. Exit 0 allows, exit 2 blocks. A missing or
unreadable transcript allows (fail open); the e2e-llm transcript check stays the backstop.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import time
from collections.abc import Iterable, Iterator
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hook_common  # noqa: E402

# A copy of ``_PRINTED_SUMMARY_RE`` in ci/llm/run.py (that module imports third-party
# packages, which a hook must not); tests/unit/hooks/test_require_gate_shown.py checks it.
SUMMARY_LINE_RE = re.compile(r"^- Summary page[^:\n]*: (\S+)\s*$", re.MULTILINE)

_ID = re.compile(r"[A-Za-z0-9_-]+")
_DB = re.compile(r"[A-Za-z0-9_.-]+")
_SCRIPT = re.compile(r"(?:.*/)?scripts/(\w+)\.py")
_DISPATCH = re.compile(r"\s*(?:Run|Follow) /design-schema-[a-z-]+\b")
_DISPATCH_JOB = re.compile(r"\bjob_id=([A-Za-z0-9_-]+)")
_COMMAND_NAME = re.compile(r"<command-name>\s*/?([\w:-]+)\s*</command-name>")
_AGENT_TOOLS = ("Agent", "Task")
# The transcript may lag the call by a moment: re-read for up to this long before refusing.
_WAIT_ENV = "MODERNIZER_GATE_HOOK_WAIT_S"


def script_call(command: str) -> tuple[str, list[str]] | None:
    """``(script name, arguments)`` when ``command`` is one simple command running
    ``uv run python scripts/<name>.py ...``; ``None`` for anything else (a chain, a heredoc,
    another program, a script path inside quoted text)."""
    if hook_common.has_unquoted(command, "<<"):
        return None
    segments = hook_common.split_compound_command(command)
    if len(segments) != 1:
        return None
    try:
        tokens = shlex.split(segments[0])
    except ValueError:
        return None
    if tokens[:3] != ["uv", "run", "python"] or len(tokens) < 4:
        return None
    match = _SCRIPT.fullmatch(tokens[3])
    return (match.group(1), tokens[4:]) if match else None


def _flag(args: list[str], name: str, pattern: re.Pattern[str]) -> str | None:
    for i, arg in enumerate(args):
        value = None
        if arg == name and i + 1 < len(args):
            value = args[i + 1]
        elif arg.startswith(name + "="):
            value = arg[len(name) + 1 :]
        if value is not None:
            return value if pattern.fullmatch(value) else None
    return None


def phase6_job(event: dict[str, Any]) -> str | None:
    """The job a Phase 6 call names, or ``None`` when the call is not one (or names no job)."""
    tool = event.get("tool_name")
    data = hook_common.tool_input(event)
    if tool in (None, "Bash"):
        command = data.get("command")
        call = script_call(command) if isinstance(command, str) else None
        if call is None or call[0] != "run_schema_design":
            return None
        if any(arg in ("--help", "-h") for arg in call[1]):
            return None
        return _flag(call[1], "--job-id", _ID)
    if tool in _AGENT_TOOLS:
        prompt = data.get("prompt")
        if isinstance(prompt, str) and _DISPATCH.match(prompt):
            found = _DISPATCH_JOB.search(prompt)
            return found.group(1) if found else None
    return None


def gate_url(block: str) -> str | None:
    """The summary-page URL the gate block ends with (none in chat mode or on an error)."""
    match = SUMMARY_LINE_RE.search(block)
    return match.group(1) if match else None


def _is_subagent(rec: dict[str, Any]) -> bool:
    return rec.get("parent_tool_use_id") is not None or rec.get("isSidechain") is True


def _content(rec: dict[str, Any]) -> list[Any]:
    message = rec.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return content if isinstance(content, list) else []


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return ""


def _slash_command(block: dict[str, Any]) -> str | None:
    """The slash command a top-level user prompt invokes (``modernize``), if any."""
    text = str(block.get("text", ""))
    match = _COMMAND_NAME.search(text)
    if match:
        return match.group(1)
    stripped = text.lstrip()
    if stripped.startswith("/"):
        return stripped[1:].split(maxsplit=1)[0] if len(stripped) > 1 else None
    return None


def _message_key(rec: dict[str, Any], run: int) -> str:
    """The assistant message a record belongs to: its API message id, else the run of
    assistant records between two user records."""
    message = rec.get("message")
    mid = message.get("id") if isinstance(message, dict) else None
    return str(mid) if mid else f"run-{run}"


def gate_status(
    records: Iterable[dict[str, Any]], job: str, call_id: str | None = None
) -> tuple[str, str | None]:
    """``(status, db)`` for ``job`` and the call ``call_id``: ``"not_held"``, ``"missing"`` (held,
    and no successful gate ran for the job), ``"same_message"`` (the latest successful gate ran
    in the message of this call, or this call is not recorded yet) or ``"ran_before"``."""
    in_modernize = False
    resumed = False
    gate_keys: dict[str, str] = {}  # gate tool_use id -> its message key
    gate_pending = False
    held_by_gate = False
    ran_in: str | None = None  # message key of the latest successful gate
    call_key: str | None = None
    later_message = False  # an assistant message after the latest successful gate's result
    db: str | None = None
    run = 0
    for rec in records:
        if _is_subagent(rec):
            continue
        rtype = rec.get("type")
        if rtype == "user":
            run += 1
        elif rtype == "assistant" and ran_in is not None and _message_key(rec, run) != ran_in:
            later_message = True
        for block in _content(rec):
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if rtype == "user" and kind == "text":
                command = _slash_command(block)
                if command:
                    in_modernize = command == "modernize"
            elif rtype == "assistant" and kind == "tool_use":
                if call_id is not None and str(block.get("id")) == call_id:
                    call_key = _message_key(rec, run)
                command = (block.get("input") or {}).get("command")
                call = script_call(command) if isinstance(command, str) else None
                if call is None or _flag(call[1], "--job-id", _ID) != job:
                    continue
                name, args = call
                if name == "print_summary" and "--gate" in args:
                    gate_keys[str(block.get("id"))] = _message_key(rec, run)
                    gate_pending = True
                    db = _flag(args, "--db", _DB) or db
                elif name == "run_assessment" and "--resume-reality-check" in args and in_modernize:
                    resumed = True
                    db = _flag(args, "--db", _DB) or db
            elif rtype == "user" and kind == "tool_result":
                use_id = str(block.get("tool_use_id"))
                if use_id not in gate_keys:
                    continue
                text = _result_text(block.get("content"))
                gate_pending = False
                failed = block.get("is_error") is True or text.lstrip().startswith("{")
                if failed:
                    continue  # a failed gate is no gate: still held by the resume or the call
                held_by_gate = gate_url(text) is not None
                resumed = False  # a block printed: it decides (chat mode is not held)
                ran_in = gate_keys[use_id] if held_by_gate else None
                later_message = False
    if not (held_by_gate or gate_pending or resumed):
        return "not_held", db
    if ran_in is None:
        return ("same_message" if gate_pending else "missing"), db
    if call_key is None:  # this call is not recorded (yet): is a later message?
        return ("ran_before" if later_message else "same_message"), db
    return ("same_message" if call_key == ran_in else "ran_before"), db


def iter_records(path: str) -> Iterator[dict[str, Any]]:
    """The transcript's records, one line at a time (sessions can run to hundreds of MB)."""
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                yield rec


def _wait_seconds() -> float:
    try:
        return min(max(float(os.environ.get(_WAIT_ENV, "2")), 0.0), 5.0)
    except ValueError:
        return 2.0


def _reason(job: str, db: str | None) -> str:
    gate = (
        f"uv run python scripts/print_summary.py --job-id {job} "
        f"--db {db or '{database_name}'} --gate --mode {{experience_mode}}"
    )
    return (
        "Blocked: schema design waits for the approval gate: run the gate on its own first, "
        f"then start schema design (`{gate}`, in a message before this call)."
    )


def main() -> int:
    event = hook_common.read_event()
    if event.get("agent_id"):
        return 0  # a call inside a subagent: the orchestrator's dispatch was the gated call
    job = phase6_job(event)
    path = event.get("transcript_path")
    if job is None or not isinstance(path, str) or not path:
        return 0
    call_id = event.get("tool_use_id")
    call_id = str(call_id) if call_id else None
    try:
        status, db = gate_status(iter_records(path), job, call_id)
        waited, step = 0.0, 0.25
        while status == "same_message" and waited < _wait_seconds():
            time.sleep(step)  # this call, or the gate's result, may land in the transcript late
            waited += step
            status, db = gate_status(iter_records(path), job, call_id)
    except OSError:
        return 0
    if status in ("not_held", "ran_before"):
        return 0
    return int(hook_common.block(_reason(job, db)))


if __name__ == "__main__":
    sys.exit(main())
