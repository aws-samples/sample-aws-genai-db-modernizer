"""Which hooks .claude/settings.json (interactive) and settings.ci.json (headless) wire."""

from __future__ import annotations

import json
import re

from tests.unit.hooks.conftest import REPO_ROOT


def _hooks(name: str) -> list[tuple[str, str, str]]:
    data = json.loads((REPO_ROOT / ".claude" / name).read_text())
    return [
        (event, group.get("matcher", ""), hook["command"])
        for event, groups in data["hooks"].items()
        for group in groups
        for hook in group["hooks"]
    ]


def _scripts(entries: list[tuple[str, str, str]]) -> dict[str, tuple[str, str]]:
    found = {}
    for event, matcher, command in entries:
        match = re.fullmatch(
            r'uv run --no-project python -I "\$CLAUDE_PROJECT_DIR/scripts/hooks/(\w+\.py)"', command
        )
        assert match, command
        assert (REPO_ROOT / "scripts" / "hooks" / match.group(1)).exists()
        found[match.group(1)] = (event, matcher)
    return found


def test_interactive_settings() -> None:
    assert _scripts(_hooks("settings.json")) == {
        "block_commit_bypass.py": ("PreToolUse", "Bash"),
        "block_credential_output.py": ("PreToolUse", "Bash"),
        "block_fork_pr_push.py": ("PreToolUse", "Bash"),
        "block_baseline_edit.py": ("PreToolUse", "Edit|Write|MultiEdit"),
        "block_contract_version_edit.py": ("PreToolUse", "Edit|Write|MultiEdit"),
        "remind_llm_run.py": ("Stop", ""),
        "require_gate_shown.py": ("PreToolUse", "Bash|Agent|Task"),
    }


def test_headless_settings() -> None:
    assert _scripts(_hooks("settings.ci.json")) == {
        "block_commit_bypass.py": ("PreToolUse", "Bash"),
        "enforce_tool_use_rule.py": ("PreToolUse", "Bash"),
        "block_credential_output.py": ("PreToolUse", "Bash"),
        "block_fork_pr_push.py": ("PreToolUse", "Bash"),
        "block_baseline_edit.py": ("PreToolUse", "Edit|Write|MultiEdit"),
        "block_contract_version_edit.py": ("PreToolUse", "Edit|Write|MultiEdit"),
        "require_gate_shown.py": ("PreToolUse", "Bash|Agent|Task"),
    }
