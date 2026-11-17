#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""PreToolUse hook (Bash), headless runs only: enforce the tool-use rule at the call (#731).

Every pipeline command and dispatch carries one tool-use sentence (see
``.claude/commands/modernize.md``). This hook refuses, before it runs, a Bash
call that breaks it: anything that is not exactly one documented command, i.e.
a single simple command matching a ``Bash(...)`` allow rule in
``.claude/settings.ci.json`` (read at call time, so the allowlist stays the one
source of truth). Pipes, chains (``&&``, ``;``, ``||``, ``&``), subshells,
command substitution, heredocs and every other tool (``cat``, ``jq``,
``python3 -c``, ``sed``, ``ls``, ``cd``, ``grep``) are refused. So is a shell
expansion in an argument (``$name``, ``${...}``, a backtick), which Claude Code
never auto-allows, with a hint on how to quote it, and any redirection to or
from a file: the scripts write their own artifacts and file writes go through
Edit/Write, whose allow rules (``artifacts/**``) bound them; only ``2>&1``-style fd duplication and ``/dev/null`` are fine. Replaying 105 saved headless
transcripts (2679 Bash calls) through it refused 26 calls, all genuine violations (pipes into
``head``/``tail``/``cut``/``grep``, ``ls``, ``find``, a ``VAR=...;`` chain, ``echo``), and none of
the documented pipeline commands. It also refuses every ``run_dms_step.py proof-target-*`` call (#1946): split as the shell
splits it, no argument may start with ``proof-target`` or hold a character outside
``[A-Za-z0-9_./=:,@+-]``. Stdin: the hook event JSON. Exit 0 allows, exit 2
blocks.
"""

from __future__ import annotations

import json
import os
import re
import shlex
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

# Claude Code never auto-allows a command the shell would expand, so say how to quote it (#1982).
EXPANSION_HINT = (
    " The shell would expand a `$name`, `${`, `$(` or backtick in this command's arguments: "
    "put a regex in single quotes, or write a literal dollar sign as `\\$`."
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
    if hook_common.has_shell_expansion(command):
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


# The routine-proof test cluster's commands (#1946) never run headless. settings.ci.json denies
# them too, but a permission rule matches the command's text, so a quoted, escaped or expanded
# subcommand ("proof-target-deploy", proof\-target-deploy, $'...', {...,}, a glob) would slip
# past it. The hook splits the command as the shell does, then refuses a run_dms_step.py
# argument that starts with proof-target or holds any character outside PLAIN_ARGUMENT: with
# no quote, $, brace, glob or backslash left, the shell has nothing to expand into one.
PROOF_TARGET_SCRIPT = "run_dms_step.py"
PROOF_TARGET_PREFIX = "proof-target"
PLAIN_ARGUMENT = re.compile(r"[A-Za-z0-9_./=:,@+-]*")
PROOF_TARGET_BLOCK = (
    "Blocked: the routine-proof test cluster's commands (run_dms_step.py proof-target-*) never "
    "run in a headless session, and every run_dms_step.py argument must be plain "
    "(letters, digits and _ . / = : , @ + -) so none can expand into one (#1946)."
)


def is_proof_target_call(command: str) -> bool:
    """True when the command runs ``run_dms_step.py`` with an argument that, split as the shell
    splits it, starts with ``proof-target`` or holds a character outside ``PLAIN_ARGUMENT``.
    Fails closed: a command naming the script whose words cannot be read counts as one."""
    try:
        words = shlex.split(command, comments=False, posix=True)
    except ValueError:
        return PROOF_TARGET_SCRIPT in command
    for i, word in enumerate(words):
        if os.path.basename(word) == PROOF_TARGET_SCRIPT:
            return any(
                w.startswith(PROOF_TARGET_PREFIX) or not PLAIN_ARGUMENT.fullmatch(w)
                for w in words[i + 1 :]
            )
    return False


# The repair loop's rounds on the test cluster (#2384) never run headless either: the same
# splitting refuses a run_routine_repair.py argument that names test-cluster or holds any
# character outside PLAIN_ARGUMENT, so no quoted or expanded form can build one.
REPAIR_SCRIPT = "run_routine_repair.py"
TEST_CLUSTER_WORD = "test-cluster"
TEST_CLUSTER_BLOCK = (
    "Blocked: the routine repair loop's rounds on the test cluster (run_routine_repair.py "
    "--proof test-cluster) never run in a headless session, and every run_routine_repair.py "
    "argument must be plain (letters, digits and _ . / = : , @ + -) (#2384)."
)


def is_test_cluster_repair_call(command: str) -> bool:
    """True when the command runs ``run_routine_repair.py`` with an argument that, split as
    the shell splits it, holds ``test-cluster`` or a character outside ``PLAIN_ARGUMENT``.
    Fails closed like :func:`is_proof_target_call`."""
    try:
        words = shlex.split(command, comments=False, posix=True)
    except ValueError:
        return REPAIR_SCRIPT in command
    for i, word in enumerate(words):
        if os.path.basename(word) == REPAIR_SCRIPT:
            return any(
                TEST_CLUSTER_WORD in w or not PLAIN_ARGUMENT.fullmatch(w) for w in words[i + 1 :]
            )
    return False


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
    if is_proof_target_call(command):
        return int(hook_common.block(PROOF_TARGET_BLOCK))
    if is_test_cluster_repair_call(command):
        return int(hook_common.block(TEST_CLUSTER_BLOCK))
    if is_documented_command(command, rules):
        return 0
    hint = EXPANSION_HINT if hook_common.has_shell_expansion(command) else ""
    return int(hook_common.block(f"Blocked by the tool-use rule: {RULE}{hint}"))


if __name__ == "__main__":
    sys.exit(main())
