#!/usr/bin/env python3
"""PreToolUse hook (Bash), headless runs only: enforce the tool-use rule at the call (#731).

Every pipeline command and dispatch carries one tool-use sentence (see
``.claude/commands/modernize.md``). This hook refuses, before it runs, a Bash
call that breaks it: anything that is not exactly one documented command, i.e.
a single simple command matching a ``Bash(...)`` allow rule in
``.claude/settings.ci.json`` (read at call time, so the allowlist stays the one
source of truth). Pipes, chains (``&&``, ``;``, ``||``, ``&``), subshells,
command substitution, heredocs and every other tool (``cat``, ``jq``,
``python3 -c``, ``sed``, ``ls``, ``cd``, ``grep``) are refused. So is any
redirection to or from a file: the scripts write their own artifacts and file
writes go through Edit/Write, whose allow rules (``artifacts/**``) bound them;
only ``2>&1``-style fd duplication and ``/dev/null`` are fine. Stdin: the hook event JSON. Exit 0 allows, exit 2
blocks.
"""

from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hook_common  # noqa: E402

SETTINGS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    ".claude",
    "settings.ci.json",
)
# Must match, word for word, the sentences in .claude/commands/*.md
# (tests/unit/hooks/test_enforce_tool_use_rule.py checks it).
RULE = (
    "Use Bash only for the documented `uv run python scripts/…` commands; never use `cat`, `jq`, "
    "`python3 -c`, `sed`, `ls`, `cd` chains, heredocs or `grep`. "
    "Use a search's output as is: never pipe, chain or post-process it."
)


def allowed_rules(settings_path: str = SETTINGS_PATH) -> list[tuple[str, bool]]:
    """``(command, takes_arguments)`` for each ``Bash(...)`` allow rule in the settings file."""
    with open(settings_path, encoding="utf-8") as handle:
        allow = json.load(handle).get("permissions", {}).get("allow", [])
    rules: list[tuple[str, bool]] = []
    for rule in allow if isinstance(allow, list) else []:
        if not (isinstance(rule, str) and rule.startswith("Bash(") and rule.endswith(")")):
            continue
        inner = rule[len("Bash(") : -1]
        wildcard = inner.endswith(" *")
        inner = inner[:-2] if wildcard else inner
        if inner:
            rules.append((inner, wildcard))
    return rules


def is_documented_command(command: str, rules: list[tuple[str, bool]]) -> bool:
    """True when ``command`` is one simple command that an allow rule covers."""
    if hook_common.has_unquoted(command, "<<"):
        return False
    stripped = command.strip()
    if stripped[:1] in "&|;" or stripped[-1:] in "&|;":  # dangling operator: still a chain
        return False
    segments = hook_common.split_compound_command(command)
    if len(segments) != 1:
        return False
    if has_file_redirection(command):
        return False
    segment = segments[0]
    return any(
        segment == base or (takes_args and segment.startswith(base + " "))
        for base, takes_args in rules
    )


# File descriptor duplication (2>&1, >&2) and /dev/null are the only redirections allowed.
_SAFE_REDIRECT = re.compile(r"\d*>&(?:\d+|-)|\d*(?:>>?|<)\s*/dev/null(?![\w/.-])")


def has_file_redirection(command: str) -> bool:
    """True when the command redirects to or from anything but ``/dev/null`` or another fd.

    Output goes through the scripts' own artifact writers and the Edit/Write tools, whose allow
    rules (``artifacts/**``) bound what can be written; a shell ``> file`` would sidestep them.
    Redirection inside quotes is data and ignored."""
    masked = hook_common.mask_quoted(command)
    rest = _SAFE_REDIRECT.sub("", masked)
    return ">" in rest or "<" in rest


def main() -> int:
    event = hook_common.read_event()
    if event.get("tool_name") not in (None, "Bash"):
        return 0
    command = hook_common.tool_input(event).get("command")
    if not isinstance(command, str):
        return 0
    try:
        rules = allowed_rules()
    except (OSError, ValueError):
        return 0  # no allowlist to enforce; the permission rules still apply
    if is_documented_command(command, rules):
        return 0
    return int(hook_common.block(f"Blocked by the tool-use rule: {RULE}"))


if __name__ == "__main__":
    sys.exit(main())
