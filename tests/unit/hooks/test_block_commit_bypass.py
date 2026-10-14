"""Hook 1: git commit --no-verify / -n / SKIP= / core.hooksPath."""

from __future__ import annotations

import pytest

from scripts.hooks.block_commit_bypass import violation
from tests.unit.hooks.conftest import bash

BLOCKED = [
    "git commit --no-verify -m 'x'",
    "git commit -m 'x' --no-verify",
    "git commit --no-verif -m x",
    "git commit -n -m x",
    "git commit -nm x",
    "git commit -anm x",
    "git -C /tmp/wt commit -n",
    "cd repo && git commit --no-verify",
    "SKIP=check-commit-message git commit -m x",
    "SKIP=mypy,ruff git commit -m x",
    "env SKIP=check-commit-message git commit -m x",
    "export SKIP=check-commit-message; git commit -m x",
    "git -c core.hooksPath=/dev/null commit -m x",
    "git -c core.hookspath=/dev/null commit -m x",
    "git -ccore.hooksPath=/dev/null commit -m x",
    "git --config-env=core.hooksPath=H commit -m x",
    "git -C /tmp/wt -c core.hooksPath=/dev/null commit -m x",
    "command git commit -n -m x",
    "sudo git commit --no-verify -m x",
    "sudo -u bob git commit -n",
    "env -u FOO git commit -n",
    "env FOO=1 git commit --no-verify",
    "nice -n 5 git commit -n",
    "time git commit --no-verify",
    "/usr/bin/git commit -n",
    "git commit --amend --no-verify --no-edit",
]
ALLOWED = [
    "git commit -m 'fix: thing'",
    "git commit -am 'fix: thing'",
    "git commit -m 'mention -n and --no-verify in the text'",
    "git commit -m -n",  # -n is the message here
    "git commit -F msg.txt --amend",
    "git status -n",
    "git log -n 5",
    "git push --no-verify origin x",  # not a commit; git push is denied elsewhere
    "gh issue comment 1 --body 'do not set SKIP=check-commit-message'",
    "git commit -uno -m x",
    "git commit -u -m x",
    "git commit -S -m x",
    "git commit -Skeyid -m x",
    "git -c user.name=x commit -m y",
    "git -c core.editor=true commit",
    "git config core.hooksPath",  # reading it; the persistent write form is the documented limit
    "SKIP= git commit -m x",
    "command git status",
    "sudo -u bob git commit -m x",
    "uv run python scripts/run_assessment.py --file x",
    "git commit -m 'a' -- --no-verify",
]


@pytest.mark.parametrize("command", BLOCKED)
def test_blocked(command: str, run_hook) -> None:
    assert violation(command)
    res = run_hook("block_commit_bypass.py", bash(command))
    assert res.blocked
    assert res.stderr.count("\n") == 1 and "Fix the cause" in res.stderr


@pytest.mark.parametrize("command", ALLOWED)
def test_allowed(command: str, run_hook) -> None:
    assert violation(command) is None
    assert run_hook("block_commit_bypass.py", bash(command)).code == 0


@pytest.mark.parametrize("event", ["", "not json", "[]", '{"tool_input": 3}', '{"tool_input": {}}'])
def test_bad_input_fails_open(event: str, run_hook) -> None:
    assert run_hook("block_commit_bypass.py", event).code == 0
