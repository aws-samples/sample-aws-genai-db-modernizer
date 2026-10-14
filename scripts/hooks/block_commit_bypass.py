#!/usr/bin/env python3
"""PreToolUse hook (Bash): refuse a ``git commit`` that skips the commit hooks (#731).

Blocks, in any simple command of the line:

* ``git commit --no-verify`` (and its unique abbreviations) and ``-n``, alone or
  inside a short-flag cluster (``-an``);
* pre-commit's ``SKIP=<hook-id>`` environment variable (inline, via ``env`` or
  ``export``);
* ``git -c core.hooksPath=... commit`` (and ``--config-env``);
* any of the above reached through ``command``, ``sudo``, ``env``, ``nice``,
  ``nohup``, ``time`` or ``exec`` in front of ``git``.

The fix is the cause the hook reported, not a bypass. Stdin: the hook event
JSON. Exit 0 allows, exit 2 blocks.

Not a shell parser. Out of reach: a bypass wrapped in ``bash -c "..."`` or built
from variables, and the persistent ``git config core.hooksPath`` form. The
commit-message hook and ``ci/check_commits.sh`` remain the backstops.
"""

from __future__ import annotations

import os
import shlex
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hook_common  # noqa: E402

# git commit short options whose value is the next token (when the flag ends the cluster).
_VALUE_FLAGS = set("mFcCt")
# git commit short options with an optional value that is only ever attached (-uno, -S<keyid>).
_ATTACHED_FLAGS = set("uS")
# Words that run the next command: skipped to find `git`. Value is the options that take an argument.
_WRAPPERS: dict[str, set[str]] = {
    "command": set(),
    "builtin": set(),
    "exec": set(),
    "nohup": set(),
    "time": set(),
    "env": {"-u", "-C", "-S"},
    "nice": {"-n"},
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-r", "-t", "-U", "-D", "-R"},
}
_GIT_VALUE_OPTIONS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"}
_REASON = (
    "Blocked: {what} skips the commit hooks. Fix the cause the hook reported "
    "and commit again; never bypass the hooks."
)


def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


def _is_assignment(token: str) -> bool:
    return "=" in token and not token.startswith("-") and token.split("=")[0].isidentifier()


def _hookspath(setting: str) -> bool:
    return setting.split("=", 1)[0].strip().lower() == "core.hookspath"


def _git_args(tokens: list[str]) -> tuple[list[str], bool] | None:
    """``(commit arguments, core.hooksPath set on the git command line)`` for a
    ``git ... commit`` segment, or None when this segment is not a git commit."""
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if _is_assignment(token):
            i += 1
        elif os.path.basename(token) in _WRAPPERS:
            takes_value = _WRAPPERS[os.path.basename(token)]
            i += 1
            while i < len(tokens) and (tokens[i].startswith("-") or _is_assignment(tokens[i])):
                i += 2 if tokens[i] in takes_value else 1
        else:
            break
    if i >= len(tokens) or os.path.basename(tokens[i]) != "git":
        return None
    i += 1
    hooks_path = False
    while i < len(tokens) and tokens[i].startswith("-"):  # git -C dir / -c k=v / --no-pager
        option = tokens[i]
        value = tokens[i + 1] if i + 1 < len(tokens) else ""
        if option in ("-c", "--config-env"):
            hooks_path = hooks_path or _hookspath(value)
        elif option.startswith("-c") and len(option) > 2 and not option.startswith("--"):
            hooks_path = hooks_path or _hookspath(option[2:])
        elif option.startswith("--config-env="):
            hooks_path = hooks_path or _hookspath(option.split("=", 1)[1])
        i += 2 if option in _GIT_VALUE_OPTIONS else 1
    if i < len(tokens) and tokens[i] == "commit":
        return tokens[i + 1 :], hooks_path
    return None


def violation(command: str) -> str | None:
    """What the command bypasses, or None when it is fine."""
    for segment in hook_common.split_compound_command(command):
        tokens = _tokens(segment)
        if any(t.startswith("SKIP=") and len(t) > len("SKIP=") for t in tokens):
            return "pre-commit's SKIP=<hook-id>"
        parsed = _git_args(tokens)
        if parsed is None:
            continue
        args, hooks_path = parsed
        if hooks_path:
            return "core.hooksPath"
        skip_next = False
        for arg in args:
            if skip_next:
                skip_next = False
                continue
            if arg == "--":
                break
            if arg.startswith("--"):
                if len(arg) >= 6 and "--no-verify".startswith(arg.split("=")[0]):
                    return "--no-verify"
                continue
            if arg.startswith("-") and len(arg) > 1:
                for pos, flag in enumerate(arg[1:]):
                    if flag == "n":
                        return "-n (short for --no-verify)"
                    if flag in _ATTACHED_FLAGS:
                        break
                    if flag in _VALUE_FLAGS:
                        skip_next = pos == len(arg) - 2
                        break
    return None


def main() -> int:
    event = hook_common.read_event()
    if event.get("tool_name") not in (None, "Bash"):
        return 0
    command = hook_common.tool_input(event).get("command")
    if not isinstance(command, str):
        return 0
    what = violation(command)
    return int(hook_common.block(_REASON.format(what=what))) if what else 0


if __name__ == "__main__":
    sys.exit(main())
