#!/usr/bin/env python3
"""PreToolUse hook (Bash, Agent): no schema design before the approval gate is in the chat (#1401, #1626).

After reality check, ``/modernize`` runs ``scripts/print_summary.py --gate --mode <mode>`` and
pastes its output (the gate block, ending with the summary page's URL unless the mode is chat)
into its own chat text: Claude Code collapses tool output, so the user sees only the quote
(#1605). ``remind_quote_printed_summary.py`` asks for the paste next to the output; the prompt
and the reminder alone did not hold: two scoped discourse runs went straight from the gate's
output to ``run_schema_design.py --split`` with no chat text at all (#1626). So this hook holds
both the order and the quote.

It refuses a Phase 6 call for a job unless a successful gate call for that job ran in an
earlier assistant message and the orchestrator's own top-level ``text`` blocks between the
gate's result and this call carry the block's key lines: the heading, the engine table rows,
the ``Total:`` and scope lines and, unless chat mode, the ``Summary page`` line with its URL
(``gate_lines_to_show``, a copy of ``_gate_lines_to_show`` in ci/llm/run.py, which the e2e-llm
transcript check applies). Tool results, tool inputs, thinking and subagent records never count.
A text block in the same message as the call, before it, counts: the transcript records each
content block as its own line, in order, so the text is on disk before the call is. A Phase 6
call is a real invocation of ``uv run python scripts/run_schema_design.py --job-id <id> ...``
(not ``--help``, not text inside another command, a quoted body or a heredoc) or a dispatch
whose prompt starts ``Run /design-schema-...`` or ``Follow /design-schema-...`` with
``job_id=<id>``. Only a ``/modernize`` run is held:

- a real ``print_summary.py --gate --job-id <id>`` call holds the job, in every mode (chat
  mode's block has no ``Summary page`` line, so it needs no URL);
- a real ``run_assessment.py --job-id <id> --resume-reality-check`` call holds the job only
  when the session's latest slash command is ``/modernize``, so skipping the gate is refused,
  while ``/reality-check`` then ``/design-schema-<engine>`` by hand is never held.

A gate call is successful when its result is recorded and is not an error (a JSON error line or
an error result); the latest successful one decides. "Earlier message" compares the assistant
message of the gate call with the message of this call (``tool_use_id`` in the event, looked up
in the transcript; the hook re-reads for up to two seconds until that call is recorded). The
refusal carries the gate block itself, so the model knows exactly what to paste.

Input, checked against the 2.1.288 CLI e2e-llm installs (the hook input schema in its binary):
a PreToolUse event carries ``session_id``, ``transcript_path``, ``cwd``, ``hook_event_name``,
``tool_name``, ``tool_input`` and ``tool_use_id``, plus ``agent_id`` only when the call runs
inside a subagent. It has no ``last_assistant_message`` (only ``Stop`` and ``SubagentStop``
do), so the hook reads the session transcript (``transcript_path``) one line at a time. A call
whose event carries ``agent_id`` is not held; without it a subagent call is judged against the
same session, which is still safe: its dispatch passed only once the gate was shown. Stdin: the
hook event JSON. Exit 0 allows, exit 2 blocks. A missing or unreadable transcript, or any error
in the hook, allows (fail open); the e2e-llm transcript check stays the backstop.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import time
from collections.abc import Iterable, Iterator
from typing import Any, NamedTuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hook_common  # noqa: E402

# A copy of ``_PRINTED_SUMMARY_RE`` in ci/llm/run.py (that module imports third-party
# packages, which a hook must not); tests/unit/hooks/test_require_gate_shown.py checks it.
SUMMARY_LINE_RE = re.compile(r"^- Summary page[^:\n]*: (\S+)\s*$", re.MULTILINE)
# Copies of ``_GATE_HEADING`` and ``_GATE_SHOWN_LINE_RE`` in ci/llm/run.py: the gate lines the
# chat must carry. tests/unit/hooks/test_require_gate_shown.py checks they agree.
GATE_HEADING = "## Assignment for approval:"
GATE_SHOWN_LINE_RE = re.compile(
    r"^(?:Total: |Engines in scope: |- Summary page)|no engine in the customer's engine scope"
)
# The longest gate block the refusal quotes whole; a longer one is quoted by its key lines.
_MAX_QUOTED_BLOCK = 8000

_ID = re.compile(r"[A-Za-z0-9_-]+")
_DB = re.compile(r"[A-Za-z0-9_.-]+")
_SCRIPT = re.compile(r"(?:.*/)?scripts/(\w+)\.py")
_DISPATCH = re.compile(r"\s*(?:Run|Follow) /design-schema-[a-z-]+\b")
_DISPATCH_JOB = re.compile(r"\bjob_id=([A-Za-z0-9_-]+)")
_COMMAND_NAME = re.compile(r"<command-name>\s*/?([\w:-]+)\s*</command-name>")
_COMMAND_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)
_AUTO = re.compile(r"(?:^|\s)(?:--auto|-y)(?:\s|$)")
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


def _norm(text: str) -> str:
    return " ".join(text.split())


def gate_lines_to_show(block: str) -> list[str]:
    """The gate block's lines the user must see in chat: the heading, the engine table (its
    separator row aside), the total, the scope lines and, unless chat mode, the summary page.
    A copy of ``_gate_lines_to_show`` in ci/llm/run.py."""
    lines = []
    for raw in block.splitlines():
        line = raw.strip()
        if not line or re.fullmatch(r"\|[\s:|-]*\|", line):
            continue
        if line.startswith((GATE_HEADING, "|")) or GATE_SHOWN_LINE_RE.search(line):
            lines.append(line)
    return lines


def missing_lines(block: str, texts: list[str]) -> list[str]:
    """The key lines of ``block`` that no chat text carries (whitespace-insensitive)."""
    said = _norm("\n".join(texts))
    return [line for line in gate_lines_to_show(block) if _norm(line) not in said]


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


def _slash_args(block: dict[str, Any]) -> str:
    """The arguments of the slash command a top-level user prompt invokes (``""`` if none)."""
    text = str(block.get("text", ""))
    if _COMMAND_NAME.search(text):
        match = _COMMAND_ARGS.search(text)
        return match.group(1) if match else ""
    parts = text.lstrip().split(maxsplit=1)
    return parts[1] if len(parts) > 1 else ""


def _message_key(rec: dict[str, Any], run: int) -> str:
    """The assistant message a record belongs to: its API message id, else the run of
    assistant records between two user records."""
    message = rec.get("message")
    mid = message.get("id") if isinstance(message, dict) else None
    return str(mid) if mid else f"run-{run}"


class GateCheck(NamedTuple):
    """What the transcript says about ``job`` and this call: ``status`` is ``"not_held"``,
    ``"missing"`` (held, and no successful gate ran for the job), ``"same_message"`` (the latest
    successful gate ran in the message of this call, or this call is not recorded yet and no
    later message came), ``"not_quoted"`` (the gate ran before, but the chat text since its
    result lacks ``missing``, its key lines) or ``"ran_before"`` (the call may run)."""

    status: str
    db: str | None
    block: str | None = None
    missing: tuple[str, ...] = ()
    # The latest ``/modernize`` prompt's ``--auto``/``-y``: ``True``, ``False`` (a run that asks
    # the user) or ``None`` (no ``/modernize`` prompt recorded: treated as unattended).
    auto: bool | None = None


def gate_check(
    records: Iterable[dict[str, Any]], job: str, call_id: str | None = None
) -> GateCheck:
    """The verdict for ``job`` and the call ``call_id`` (see ``GateCheck``)."""
    in_modernize = False
    auto: bool | None = None
    resumed = False
    gate_keys: dict[str, str] = {}  # gate tool_use id -> its message key
    gate_pending = False
    held_by_gate = False
    ran_in: str | None = None  # message key of the latest successful gate
    block_text: str | None = None  # the latest successful gate's block
    texts: list[str] = []  # top-level chat text after that block, before this call
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
                    if in_modernize:
                        auto = bool(_AUTO.search(_slash_args(block)))
            elif rtype == "assistant" and kind == "text":
                if block_text is not None and call_key is None:
                    texts.append(str(block.get("text", "")))
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
                # Deliberately looser than check_printed_blocks_shown (which needs the heading
                # first): a warning printed before the heading must not make the gate look failed.
                failed = block.get("is_error") is True or text.lstrip().startswith("{")
                if failed:
                    continue  # a failed gate is no gate: still held by the resume or the call
                held_by_gate = True  # every mode: chat mode's block has no URL to quote
                resumed = False  # a block printed: it decides
                ran_in = gate_keys[use_id]
                block_text, texts = text, []
                later_message = False
    if not (held_by_gate or gate_pending or resumed):
        return GateCheck("not_held", db)
    if ran_in is None:
        return GateCheck("same_message" if gate_pending else "missing", db)
    if call_key is None and not later_message:  # this call is not recorded (yet)
        return GateCheck("same_message", db, block_text)
    if call_key is not None and call_key == ran_in:
        return GateCheck("same_message", db, block_text)
    missing = tuple(missing_lines(block_text or "", texts))
    if missing:
        return GateCheck("not_quoted", db, block_text, missing, auto)
    return GateCheck("ran_before", db, block_text)


def gate_status(
    records: Iterable[dict[str, Any]], job: str, call_id: str | None = None
) -> tuple[str, str | None]:
    """``(status, db)`` of ``gate_check``."""
    check = gate_check(records, job, call_id)
    return check.status, check.db


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


def _gate_command(job: str, db: str | None) -> str:
    return (
        f"uv run python scripts/print_summary.py --job-id {job} "
        f"--db {db or '{database_name}'} --gate --mode {{experience_mode}}"
    )


def _reason(job: str, db: str | None) -> str:
    return (
        "Blocked: schema design waits for the approval gate: run the gate on its own first, "
        f"then start schema design (`{_gate_command(job, db)}`, in a message before this call)."
    )


def _quote_reason(check: GateCheck) -> str:
    """The refusal for a gate that ran but is not in the chat: what to paste, where, and (in a
    run that asks the user) that the approval question comes again before Phase 6."""
    block = (check.block or "").strip("\n")
    if len(block) > _MAX_QUOTED_BLOCK:
        block = "\n".join(gate_lines_to_show(block))
    then: tuple[str, ...]
    if check.auto is False:
        then = (
            "then ask the approval question again and end your turn: this run is not --auto,",
            "so the user approves only after seeing the block. Make this call again once the",
            "user approves.",
        )
    else:
        then = ("then make this call again after that text.",)
    missing = "\n".join(f"- {line}" for line in check.missing)
    head = " ".join(
        (
            "Blocked: schema design waits for the approval gate: the user has not seen it.",
            "The gate block is only in the tool result of `print_summary.py --gate`, which",
            "Claude Code shows collapsed; thinking and tool inputs are not shown either.",
            "Write the block below, verbatim, as the visible text of your reply (not in",
            "thinking, not inside a tool call),",
            *then,
            f"Missing from your chat text ({len(check.missing)} line(s)):",
        )
    )
    return f"{head}\n{missing}\n\nThe block to paste:\n\n{block}\n"


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
        check = gate_check(iter_records(path), job, call_id)
        waited, step = 0.0, 0.25
        while check.status in ("same_message", "not_quoted") and waited < _wait_seconds():
            time.sleep(step)  # this call, its text or the gate's result may land late
            waited += step
            check = gate_check(iter_records(path), job, call_id)
    except OSError:
        return 0
    if check.status in ("not_held", "ran_before"):
        return 0
    if check.status == "not_quoted":
        print(_quote_reason(check), file=sys.stderr, end="")
        return int(hook_common.BLOCK_EXIT)
    return int(hook_common.block(_reason(job, check.db)))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 -- fail open: the transcript check is the backstop
        sys.exit(0)
