#!/usr/bin/env python3
"""PostToolUse hook (Bash): after ``print_summary.py --gate`` or ``--final``, remind the model to
paste the block into its reply (#1605).

Claude Code's interactive UI shows a tool call collapsed ("Ran 1 shell command"), so the user
never sees what ``scripts/print_summary.py`` printed unless the orchestrator's next chat
message carries it. An interactive ``/modernize`` run once wrote "The block above is the
approval gate" and asked for approval over a block nobody could see. ``/modernize`` says to
quote the stdout verbatim; this hook adds the same instruction right next to the output, as
``hookSpecificOutput.additionalContext`` (the PostToolUse output field the 2.1.288 CLI that
e2e-llm installs accepts, next to ``updatedToolOutput``; it reaches the model with the tool
result). The e2e-llm transcript check (``check_printed_blocks_shown`` in ``ci/llm/run.py``) is
the backstop.

It speaks only for a real top-level call, one simple command
``uv run python scripts/print_summary.py ... --gate|--final ...`` whose stdout is the Markdown
block (it starts ``## ``; a JSON error line or an empty output gets nothing). A call inside a
subagent (the event carries ``agent_id``: ``/synthesize`` prints the final summary into its own
report) gets nothing. A reminder only: it never blocks, always exits 0, and any error prints
nothing. Stdlib only, no repo imports.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hook_common  # noqa: E402

GATE_REMINDER = (
    "The user cannot see this tool output: Claude Code shows the call collapsed. Your next "
    "chat message must contain this stdout verbatim, the whole Markdown block (including its "
    "Summary page line, if any), and only then ask the approval question. Never refer to "
    '"the block above", and add no figure of your own.'
)
FINAL_REMINDER = (
    "The user cannot see this tool output: Claude Code shows the call collapsed. Paste this "
    "stdout verbatim in your next chat message (in ui mode, its Results page line), then end "
    'with the MODERNIZE_RESULT line. Never refer to "the summary above", and add no figure '
    "of your own."
)


def printed_kind(command: str) -> str | None:
    """``"gate"`` or ``"final"`` for one simple ``uv run python scripts/print_summary.py``
    command, else ``None`` (a chain, a heredoc, another script, ``--help``)."""
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
    if tokens[3] not in ("scripts/print_summary.py", "./scripts/print_summary.py") and not tokens[
        3
    ].endswith("/scripts/print_summary.py"):
        return None
    args = tokens[4:]
    if any(arg in ("--help", "-h") for arg in args):
        return None
    if "--gate" in args:
        return "gate"
    if "--final" in args:
        return "final"
    return None


def _stdout(response: Any) -> str:
    if isinstance(response, dict):
        out = response.get("stdout")
        return out if isinstance(out, str) else ""
    return response if isinstance(response, str) else ""


def reminder(event: dict[str, Any]) -> str | None:
    """The reminder for this event, or ``None`` when it needs none."""
    if event.get("agent_id") or event.get("tool_name") not in (None, "Bash"):
        return None
    command = hook_common.tool_input(event).get("command")
    kind = printed_kind(command) if isinstance(command, str) else None
    if kind is None or not _stdout(event.get("tool_response")).lstrip().startswith("## "):
        return None
    return GATE_REMINDER if kind == "gate" else FINAL_REMINDER


def main() -> int:
    message = reminder(hook_common.read_event())
    if message:
        output = {
            "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": message}
        }
        print(json.dumps(output))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 -- a reminder must never break a session
        sys.exit(0)
