# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The help site's command table names every shipped command, and nothing else.

``docs/use-claude-code.md`` "The individual commands" is where a user looks up a command.
A command added under ``.claude/commands/`` without a row there is invisible on the help
site (``/repair-routines`` shipped that way), and a row for a command that is gone sends the
user to nothing. A per-engine family (``/analyze-<engine>``, ``/design-schema-<engine>``)
has one row. ``/modernize`` has its own sections, and ``/add-modernizer-skill`` is a
contributor tool for adding a pipeline phase, so neither needs a row.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
PAGE = REPO / "docs" / "use-claude-code.md"
COMMANDS = REPO / ".claude" / "commands"
NO_ROW = {"modernize", "add-modernizer-skill"}
FAMILIES = ("analyze-", "design-schema-")


def _table_rows() -> set[str]:
    section = PAGE.read_text().split("## The individual commands", 1)[1].split("\n## ", 1)[0]
    return set(re.findall(r"^\| `/([a-z<>-]+)` \|", section, re.MULTILINE))


def _shipped() -> set[str]:
    return {p.stem for p in COMMANDS.glob("*.md")}


def _row_for(command: str) -> str:
    for family in FAMILIES:
        if command.startswith(family):
            return f"{family}<engine>"
    return command


def test_every_shipped_command_has_a_row() -> None:
    rows = _table_rows()
    missing = sorted(c for c in _shipped() - NO_ROW if _row_for(c) not in rows)
    assert missing == [], f"commands with no row in The individual commands: {missing}"


def test_every_row_names_a_shipped_command() -> None:
    shipped = _shipped()
    for row in _table_rows():
        if row.endswith("<engine>"):
            prefix = row.removesuffix("<engine>")
            assert any(c.startswith(prefix) for c in shipped), row
        else:
            assert row in shipped, row
