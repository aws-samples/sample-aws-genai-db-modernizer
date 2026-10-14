"""Helpers: run a hook script the way Claude Code does (stdin JSON, `python -I`)."""

from __future__ import annotations

import json
import subprocess  # nosec B404 -- runs this repo's own hook scripts with fixed argv
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
HOOKS = REPO_ROOT / "scripts" / "hooks"


class HookResult:
    def __init__(self, code: int, stdout: str, stderr: str) -> None:
        self.code, self.stdout, self.stderr = code, stdout, stderr

    @property
    def blocked(self) -> bool:
        return self.code == 2


@pytest.fixture
def run_hook():
    def _run(script: str, event: dict | str, env: dict | None = None) -> HookResult:
        payload = event if isinstance(event, str) else json.dumps(event)
        res = subprocess.run(  # nosec B603 -- fixed argv
            [sys.executable, "-I", str(HOOKS / script)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
            check=False,
        )
        return HookResult(res.returncode, res.stdout, res.stderr)

    return _run


def bash(command: str) -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }


def edit(path: str, tool: str = "Edit") -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": {"file_path": path},
        "cwd": str(REPO_ROOT),
    }
