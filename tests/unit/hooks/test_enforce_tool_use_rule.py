# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Hook 4: the tool-use rule at the call, derived from .claude/settings.ci.json."""

from __future__ import annotations

import json

import pytest

from scripts.hooks import enforce_tool_use_rule as hook
from tests.unit.hooks.conftest import REPO_ROOT, bash
from tests.unit.hooks.test_hook_common import DENIED_SEARCH

RULES = hook.allowed_rules()

ALLOWED = [
    "uv run python scripts/run_assessment.py --file x.json --all -y",
    "uv run python scripts/run_collect.py --job-id j 2>&1",
    "uv run python scripts/run_collect.py --job-id j >/dev/null 2>&1",
    "uv run python scripts/run_collect.py --job-id j 2> /dev/null",
    "uv run python scripts/run_collect.py --job-id j --note 'a > b < c'",
    "uv run python scripts/run_reality_check.py --job-id j --resume-reality-check",
    "uv run python scripts/search_artifacts.py 'a|b; c' artifacts/db/job",
    "uv run python scripts/search_artifacts.py '\\$lookup|\"version\"' artifacts/db/job",
    'uv run python scripts/search_artifacts.py "\\"version\\"|x$|\\\\\\$defs" artifacts/db/job',
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


@pytest.mark.parametrize(
    "command",
    [
        DENIED_SEARCH,
        'uv run python scripts/search_artifacts.py "$(cat /etc/hostname)" artifacts/db/job',
        'uv run python scripts/search_artifacts.py "`id`" artifacts/db/job',
        "uv run python scripts/search_artifacts.py x $HOME",
        'uv run python scripts/run_collect.py --job-id "${JOB}"',
    ],
)
def test_documented_command_with_a_shell_expansion_is_blocked_with_a_hint(
    command: str, run_hook
) -> None:
    """Claude Code never auto-allows an argument the shell would expand, so the hook refuses
    it first and says how to quote a regex instead of leaving a bare permission denial."""
    assert not hook.is_documented_command(command, RULES)
    res = run_hook("enforce_tool_use_rule.py", bash(command))
    assert res.blocked
    assert hook.RULE in res.stderr and hook.EXPANSION_HINT.strip() in res.stderr
    assert res.stderr.count("\n") == 1


@pytest.mark.passes_on_main
def test_other_refusals_carry_no_expansion_hint(run_hook) -> None:
    res = run_hook("enforce_tool_use_rule.py", bash("cat artifacts/x/output.json"))
    assert res.blocked and "single quotes" not in res.stderr


PROOF_TARGET_FORMS = [
    "uv run python scripts/run_dms_step.py proof-target-deploy --job-id j --db d --approve",
    'uv run python scripts/run_dms_step.py "proof-target-deploy" --job-id j --db d --approve',
    "uv run python scripts/run_dms_step.py 'proof-target-teardown' --job-id j --db d --approve",
    "uv run python scripts/run_dms_step.py proof\\-target-status --job-id j --db d",
    'uv run python scripts/run_dms_step.py "proof"-target-plan --job-id j --db d',
    "uv run python scripts/run_dms_step.py --job-id j --db d proof-target-deploy --approve",
    "uv run python scripts/run_dms_step.py --job-id j --db d 'proof-target-plan'",
    # Expansions shlex does not perform, which bash and zsh do: ANSI-C quoting, braces, globs.
    "uv run python scripts/run_dms_step.py $'proof-target-deploy' --job-id j --db d --approve",
    "uv run python scripts/run_dms_step.py {proof-target-deploy,} --job-id j --db d --approve",
    "uv run python scripts/run_dms_step.py pr{oof-target-deploy,} --job-id j --db d --approve",
    "uv run python scripts/run_dms_step.py proof-target-depl?y --job-id j --db d --approve",
    "uv run python scripts/run_dms_step.py pro*-deploy --job-id j --db d --approve",
]


@pytest.mark.parametrize("command", PROOF_TARGET_FORMS)
def test_a_proof_target_call_is_blocked_quoted_escaped_expanded_or_after_the_options(
    command: str, run_hook
) -> None:
    """#1946: the deny rules match the command text, so a quoted, escaped or expanded subcommand
    slips past them. The hook splits the command as the shell does and refuses a
    ``run_dms_step.py`` argument that starts with ``proof-target`` or holds any character
    outside ``[A-Za-z0-9_./=:,@+-]``, so no expansion is left to build one."""
    assert hook.is_proof_target_call(command)
    res = run_hook("enforce_tool_use_rule.py", bash(command))
    assert res.blocked and "routine-proof test cluster" in res.stderr


@pytest.mark.parametrize(
    "command",
    [
        "uv run python scripts/run_dms_step.py status --job-id j --db d --until stack",
        "uv run python scripts/run_dms_step.py plan --job-id j --db d",
        'uv run python scripts/run_dms_step.py "teardown" --job-id j --db d --approve',
        "uv run python scripts/run_dms_step.py --job-id j --db d routines",
    ],
)
def test_the_other_run_dms_step_subcommands_still_pass(command: str, run_hook) -> None:
    assert not hook.is_proof_target_call(command)
    assert run_hook("enforce_tool_use_rule.py", bash(command)).code == 0


def _documented_run_dms_step_calls() -> list[str]:
    """Every distinct ``run_dms_step.py`` call in the shipped commands and skills, with its
    ``{placeholder}`` and ``<placeholder>`` values filled in as a run fills them."""
    import re

    found: set[str] = set()
    for folder in (REPO_ROOT / ".claude" / "commands", REPO_ROOT / "src" / "skills"):
        for path in folder.rglob("*.md"):
            for call in re.findall(
                r"uv run python scripts/run_dms_step\.py[^`\n]*", path.read_text()
            ):
                filled = re.sub(r"\{[^}]*\}|<[^>]*>", "job-1a2b", call.strip())
                found.add(filled)
    return sorted(found)


@pytest.mark.passes_on_main  # the documented calls on main hold no proof-target call; #1945 adds them, refused by design
def test_every_documented_run_dms_step_call_still_passes_the_hook(run_hook) -> None:
    """The character rule must never refuse a call the pipeline makes: a new command that does
    not fit it fails here, not in a headless run."""
    calls = _documented_run_dms_step_calls()
    assert len(calls) >= 30, calls
    refused = [c for c in calls if hook.is_proof_target_call(c)]
    # /move's routine proof (#1945) documents the proof-target-* calls: a headless run is meant
    # to be refused them, and nothing else.
    assert refused == [c for c in calls if " proof-target-" in c]
    calls = [c for c in calls if c not in refused]
    for call in calls[:3]:  # and through the hook itself, end to end
        assert run_hook("enforce_tool_use_rule.py", bash(call)).code == 0, call


TEST_CLUSTER_FORMS = [
    "uv run python scripts/run_routine_repair.py --job-id j --db d --proof test-cluster --approve",
    "uv run python scripts/run_routine_repair.py --job-id j --db d --proof=test-cluster",
    'uv run python scripts/run_routine_repair.py --job-id j --db d --proof "test-cluster"',
    "uv run python scripts/run_routine_repair.py --job-id j --db d --proof test\\-cluster",
    "uv run python scripts/run_routine_repair.py --job-id j --db d --proof $'test-cluster'",
    "uv run python scripts/run_routine_repair.py --job-id j --db d --proof test-clus?er",
    "uv run python scripts/run_routine_repair.py --job-id j --db d --proof {test-cluster,}",
]


@pytest.mark.parametrize("command", TEST_CLUSTER_FORMS)
def test_the_repair_loop_on_the_test_cluster_is_blocked_headless(command: str, run_hook) -> None:
    """#2384: the repair rounds on the test cluster run only in an attended /test-routines;
    quoted, escaped or expanded forms are refused like the proof-target-* commands."""
    assert hook.is_test_cluster_repair_call(command)
    res = run_hook("enforce_tool_use_rule.py", bash(command))
    assert res.blocked and "test cluster" in res.stderr


@pytest.mark.parametrize(
    "command",
    [
        "uv run python scripts/run_routine_repair.py --job-id j --db d --llm-mode external --proof local",
        "uv run python scripts/run_routine_repair.py --job-id j --db d --finalize --proof none",
    ],
)
def test_the_other_repair_loop_calls_still_pass(command: str, run_hook) -> None:
    assert not hook.is_test_cluster_repair_call(command)
    assert run_hook("enforce_tool_use_rule.py", bash(command)).code == 0


def test_the_ci_settings_deny_the_repair_loop_on_the_test_cluster() -> None:
    import json

    deny = json.loads((REPO_ROOT / ".claude/settings.ci.json").read_text())["permissions"]["deny"]
    assert "Bash(uv run python scripts/run_routine_repair.py *test-cluster*)" in deny
