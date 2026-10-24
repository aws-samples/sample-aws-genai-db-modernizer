#!/usr/bin/env python3
"""PreToolUse hook (Edit/Write): refuse hand edits of contract versions (#1408).

A contract's ``contract_version`` default and its Version History entries are
written by ``scripts/resolve_contract_versions.py`` after the last rebase, from
the pending bumps ``scripts/bump_contract.py`` declares (ADR-008 Decision 8). A
hand edit collides with main's next number and was sent back in review, so an
Edit, MultiEdit or Write of ``src/contracts/<module>.py`` that changes an existing
model's default, or a Version History line, is refused. The resolver runs as a
script, not an Edit, so it is not affected.

Allowed:

- every other change to the file, including adding or removing a model with its
  own ``contract_version`` (a new model starts at its own version, and
  ``bump_contract.py`` cannot declare a model the snapshot does not list yet);
- a contract ``bump_contract.py`` sends to a hand bump, because the resolver
  cannot edit it: its default, and the module's Version History lines when the
  module has such a contract. The set comes from the resolver itself
  (``resolve_contract_versions.hand_bumped_contracts``), never a list kept here;
- a new contract file, and a file with merge conflict markers (resolve the
  conflict, then re-run the resolver).

Only a call that moves a version line loads that repo code; every other call
stays stdlib-only. The patterns are copies of ``scripts/contract_bumps.py``'s;
``tests/unit/hooks/test_block_contract_version_edit.py`` asserts they agree.
Stdin: the hook event JSON. Exit 0 allows, exit 2 blocks.
"""

from __future__ import annotations

import ast
import os
import re
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hook_common  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULT_RE = re.compile(
    r"(contract_version\s*:\s*str\s*=\s*Field\(\s*(?:default\s*=\s*)?\")(\d+\.\d+)(\")"
)
HEADING_RE = re.compile(r"^\s*Version [Hh]istory:\s*$")
ENTRY_RE = re.compile(r"^(\s*)-\s+(v?)(\d+)\.(\d+)\b")

_CONTRACT_PATH = re.compile(r"(?:^|/)src/contracts/[^/]+\.py$")
_CONFLICT_RE = re.compile(r"^(<<<<<<<|>>>>>>>) ", re.MULTILINE)

MESSAGE = (
    "Blocked: {name}: do not change {what} by hand. Declare the bump with: "
    'uv run python scripts/bump_contract.py <contract> minor|major --reason "..." '
    "--issue N; after the last rebase, uv run python scripts/resolve_contract_versions.py "
    "writes the version and the history entry. Adding or removing a versioned model, "
    "and a contract bump_contract.py says to bump by hand, are allowed."
)


def _list_goes_on(lines: list[str], start: int, indent: str) -> bool:
    for line in lines[start:]:
        if not line.strip():
            continue
        entry = ENTRY_RE.match(line)
        if entry and entry.group(1) == indent:
            return True
        return len(line) - len(line.lstrip()) > len(indent) and '"""' not in line
    return False


def _history_lines(lines: list[str]) -> tuple[str, ...]:
    """Every Version History list in ``lines``: its entries and their continuations."""
    found: list[str] = []
    i = 0
    while i < len(lines):
        match = ENTRY_RE.match(lines[i + 1]) if i + 1 < len(lines) else None
        if not HEADING_RE.match(lines[i]) or match is None:
            i += 1
            continue
        indent = match.group(1)
        j = end = i + 1
        while j < len(lines):
            line = lines[j]
            entry = ENTRY_RE.match(line)
            if (entry and entry.group(1) == indent) or (
                line.strip() and len(line) - len(line.lstrip()) > len(indent)
            ):
                end = j
            elif not (not line.strip() and _list_goes_on(lines, j + 1, indent)):
                break
            j += 1
        found.extend(lines[i + 1 : end + 1])
        i = j
    return tuple(found)


def version_lines(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The ``contract_version`` defaults and the Version History lines of ``text``."""
    defaults = tuple(m.group(2) for m in DEFAULT_RE.finditer(text))
    return defaults, _history_lines(text.splitlines())


def class_defaults(text: str) -> dict[str, str]:
    """Each top-level class's literal ``contract_version`` default."""
    lines = text.splitlines(keepends=True)
    found: dict[str, str] = {}
    for node in ast.parse(text).body:
        if isinstance(node, ast.ClassDef):
            start = sum(len(line) for line in lines[: node.lineno - 1])
            end = sum(len(line) for line in lines[: (node.end_lineno or node.lineno)])
            match = DEFAULT_RE.search(text, start, end)
            if match:
                found[node.name] = match.group(2)
    return found


def hand_bumped(module: str, source: str) -> set[str]:
    """The module's contracts ``bump_contract.py`` sends to a hand bump (repo code)."""
    if REPO not in sys.path:
        sys.path.insert(0, REPO)
    from scripts.resolve_contract_versions import hand_bumped_contracts

    return hand_bumped_contracts(module, source)


def refusal(module: str, current: str, after: str) -> str | None:
    """What the edit changes by hand that the resolver owns, or None to allow it."""
    before_defaults, after_defaults = class_defaults(current), class_defaults(after)
    moved = sorted(
        c
        for c in before_defaults.keys() & after_defaults.keys()
        if before_defaults[c] != after_defaults[c]
    )
    history_moved = _history_lines(current.splitlines()) != _history_lines(after.splitlines())
    if not moved and not history_moved:
        return None
    hand = hand_bumped(module, current)
    owned = [c for c in moved if f"{module}.{c}" not in hand]
    if owned:
        return f"the contract_version default of {', '.join(owned)}"
    if history_moved and not hand:
        return "a Version History entry"
    return None


def _replace(text: str, edit: Any) -> str | None:
    if not isinstance(edit, dict):
        return None
    old, new = edit.get("old_string"), edit.get("new_string")
    if not isinstance(old, str) or not isinstance(new, str) or not old or old not in text:
        return None
    return text.replace(old, new) if edit.get("replace_all") else text.replace(old, new, 1)


def edited_text(tool: Any, tool_input: dict[str, Any], current: str) -> str | None:
    """The file's text after the call, or None when it cannot be worked out."""
    if tool == "Write":
        content = tool_input.get("content")
        return content if isinstance(content, str) else None
    if tool == "MultiEdit":
        edits = tool_input.get("edits")
        if not isinstance(edits, list):
            return None
        text: str | None = current
        for edit in edits:
            text = _replace(text, edit) if text is not None else None
        return text
    return _replace(current, tool_input)


def main() -> int:
    event = hook_common.read_event()
    tool_input = hook_common.tool_input(event)
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return 0
    cwd = event.get("cwd") if isinstance(event.get("cwd"), str) else ""
    full = os.path.normpath(os.path.join(cwd or "", file_path.replace("\\", "/")))
    if not _CONTRACT_PATH.search(full.replace("\\", "/")):
        return 0
    try:
        with open(full, encoding="utf-8") as handle:
            current = handle.read()
    except (OSError, UnicodeDecodeError):
        return 0  # a new contract starts at its own version
    if _CONFLICT_RE.search(current):
        return 0  # resolving a rebase conflict; the resolver re-runs afterwards
    after = edited_text(event.get("tool_name"), tool_input, current)
    if after is None or version_lines(after) == version_lines(current):
        return 0
    module = os.path.splitext(os.path.basename(full))[0]
    try:
        what = refusal(module, current, after)
    except Exception:  # noqa: BLE001 -- a hook fails open on its own bug or unparsable text
        return 0
    if what is None:
        return 0
    return int(hook_common.block(MESSAGE.format(name=os.path.basename(full), what=what)))


if __name__ == "__main__":
    sys.exit(main())
