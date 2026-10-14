"""Shared helpers for the Claude Code hook scripts in this directory (#731).

Stdlib only and no repo imports: hooks run on every tool call, under
``python -I``, so they must start fast and never load project code. Each hook
puts this directory on ``sys.path`` itself (``-I`` drops the script directory)
and imports ``hook_common``.

Protocol (Claude Code hooks): the event JSON arrives on stdin; exit 0 allows;
exit 2 blocks and shows stderr to the model; any other exit is a non-blocking
error. A hook that cannot read its input exits 0 (fail open) rather than wedge
a session on its own bug.
"""

from __future__ import annotations

import json
import sys
from typing import Any

BLOCK_EXIT = 2


def read_event(stream: Any = None) -> dict[str, Any]:
    """The hook event from stdin, or ``{}`` when it is missing or not an object."""
    try:
        data = json.loads((stream or sys.stdin).read())
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def tool_input(event: dict[str, Any]) -> dict[str, Any]:
    value = event.get("tool_input")
    return value if isinstance(value, dict) else {}


def block(reason: str) -> int:
    """Print a one-line reason on stderr and return the blocking exit code."""
    print(" ".join(reason.split()), file=sys.stderr)
    return BLOCK_EXIT


def split_compound_command(command: str) -> list[str]:
    """Split a shell command line into its simple commands.

    A tested copy of ``_split_compound_command`` in ``ci/llm/run.py`` (#683):
    ``ci/llm/run.py`` imports third-party modules, which a hook must not load.
    ``tests/unit/hooks/test_hook_common.py`` asserts both copies agree.

    Splits on ``|``, ``||``, ``&``, ``&&``, ``;``, newlines, subshell
    parentheses, ``$(`` and backticks, outside single- and double-quoted text.
    Empty segments are dropped."""
    segments: list[str] = []
    current: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(command):
        ch = command[i]
        if quote is not None:
            current.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < len(command):
                current.append(command[i + 1])
                i += 1
            elif ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            current.append(ch)
        elif ch == "\\" and i + 1 < len(command):
            current.append(ch)
            current.append(command[i + 1])
            i += 1
        elif ch == "&" and (command[i - 1 : i] in (">", "<") or command[i + 1 : i + 2] == ">"):
            current.append(ch)  # a redirection (2>&1, &>file), not an operator
        elif ch in "|&;\n()`":
            if ch == "(" and current and current[-1] == "$":
                current.pop()
            segments.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
        i += 1
    segments.append("".join(current).strip())
    return [seg for seg in segments if seg]


def mask_quoted(command: str) -> str:
    """``command`` with the text inside single and double quotes replaced by ``x``."""
    out: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(command):
        ch = command[i]
        if quote is not None:
            if ch == "\\" and quote == '"' and i + 1 < len(command):
                out.append("xx")
                i += 1
            elif ch == quote:
                quote = None
                out.append(ch)
            else:
                out.append("x")
        elif ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == "\\" and i + 1 < len(command):
            out.append("xx")
            i += 1
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def has_unquoted(command: str, needle: str) -> bool:
    """True when ``needle`` appears outside single- and double-quoted text."""
    quote: str | None = None
    i = 0
    while i < len(command):
        ch = command[i]
        if quote is not None:
            if ch == "\\" and quote == '"':
                i += 1
            elif ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "\\":
            i += 1
        elif command.startswith(needle, i):
            return True
        i += 1
    return False
