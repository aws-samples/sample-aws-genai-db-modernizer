"""Hook 4: the tool-use rule at the call, derived from .claude/settings.ci.json."""

from __future__ import annotations

import json

import pytest

from scripts.hooks import enforce_tool_use_rule as hook
from tests.unit.hooks.conftest import REPO_ROOT, bash

RULES = hook.allowed_rules()

ALLOWED = [
    "uv run python scripts/run_assessment.py --file x.json --all -y",
    "uv run python scripts/run_collect.py --job-id j 2>&1",
    "uv run python scripts/run_collect.py --job-id j >/dev/null 2>&1",
    "uv run python scripts/run_collect.py --job-id j 2> /dev/null",
    "uv run python scripts/run_collect.py --job-id j --note 'a > b < c'",
    "uv run python scripts/run_reality_check.py --job-id j --resume-reality-check",
    "uv run python scripts/search_artifacts.py 'a|b; c' artifacts/db/job",
    "uv run python scripts/start_local_ui.py",
    "uv run python scripts/start_local_ui.py --stop",
    'uv run python scripts/check_decision.py --note "a && b"',
]
BLOCKED = [
    "cat artifacts/x/output.json",
    "jq . artifacts/x/output.json",
    "python3 -c 'print(1)'",
    "uv run python -c 'print(1)'",
    "sed -n 1,5p file",
    "ls artifacts",
    "cd artifacts && ls",
    "grep -r foo artifacts",
    "git status",
    "uv run python scripts/run_assessment.py --all | head",
    "uv run python scripts/run_assessment.py --all > out.txt",
    "uv run python scripts/run_assessment.py --all >> artifacts/x/log.txt",
    "uv run python scripts/run_assessment.py --all 2> /etc/x",
    "uv run python scripts/run_assessment.py --all &> out.txt",
    "uv run python scripts/run_assessment.py --all > /dev/nullx",
    "uv run python scripts/run_assessment.py --all < input.json",
    "uv run python scripts/run_assessment.py --all && echo done",
    "uv run python scripts/run_assessment.py --all; ls",
    "uv run python scripts/run_assessment.py --all || true",
    "uv run python scripts/run_assessment.py --all &",
    "; uv run python scripts/run_assessment.py --all",
    "uv run python scripts/run_assessment.py --all;",
    "uv run python scripts/run_assessment.py $(cat args)",
    "uv run python scripts/run_assessment.py `cat args`",
    "cd /repo\nuv run python scripts/run_assessment.py --all",
    "uv run python scripts/run_assessment.py <<EOF\nx\nEOF",
    "cat <<'EOF' > f\nx\nEOF",
    "uv run python scripts/run_other.py",  # not allowlisted
    "uv run python scripts/start_local_ui.pyx",
    "uv run python scripts/search_artifacts.py x . | grep y",
    "",
]


@pytest.mark.parametrize("command", ALLOWED)
def test_documented_commands_allowed(command: str, run_hook) -> None:
    assert hook.is_documented_command(command, RULES)
    assert run_hook("enforce_tool_use_rule.py", bash(command)).code == 0


@pytest.mark.parametrize("command", BLOCKED)
def test_rule_breaking_commands_blocked(command: str, run_hook) -> None:
    assert not hook.is_documented_command(command, RULES)
    res = run_hook("enforce_tool_use_rule.py", bash(command))
    assert res.blocked
    assert hook.RULE in res.stderr and res.stderr.count("\n") == 1


def test_every_allowlisted_bash_rule_is_allowed() -> None:
    settings = json.loads((REPO_ROOT / ".claude" / "settings.ci.json").read_text())
    rules = [r for r in settings["permissions"]["allow"] if r.startswith("Bash(")]
    assert rules and len(rules) == len(RULES)
    for base, takes_args in RULES:
        assert hook.is_documented_command(base, RULES) or takes_args
        if takes_args:
            assert hook.is_documented_command(f"{base} --x 1", RULES)


def test_allowlist_is_read_from_the_settings_file(tmp_path) -> None:
    path = tmp_path / "s.json"
    path.write_text(
        json.dumps({"permissions": {"allow": ["Bash(echo hi)", "Bash(foo *)", "Read"]}})
    )
    rules = hook.allowed_rules(str(path))
    assert rules == [("echo hi", False), ("foo", True)]
    assert hook.is_documented_command("echo hi", rules)
    assert not hook.is_documented_command("echo hi there", rules)
    assert hook.is_documented_command("foo bar", rules)


def test_rule_text_is_the_sentence_in_every_command() -> None:
    commands = (REPO_ROOT / ".claude" / "commands").glob("*.md")
    carrying = [c for c in commands if "never use `cat`" in c.read_text()]
    assert carrying
    for path in carrying:
        assert hook.RULE in path.read_text(), path.name


def test_non_bash_tools_and_bad_input_are_ignored(run_hook) -> None:
    assert run_hook("enforce_tool_use_rule.py", {"tool_name": "Read", "tool_input": {}}).code == 0
    assert run_hook("enforce_tool_use_rule.py", "").code == 0
    assert run_hook("enforce_tool_use_rule.py", {"tool_name": "Bash", "tool_input": {}}).code == 0
