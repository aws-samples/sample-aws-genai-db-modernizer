# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""`.claude/settings.ci.json` must allowlist every script a headless /modernize run invokes.

This is a tripwire for the CI permission allowlist: valid JSON, every
`uv run python scripts/<name>.py` referenced by the shipped commands (except
developer-scaffolding-only `add-modernizer-skill.md`) has a matching allow
rule, every referenced script exists on disk, and the deny list blocks the
obviously dangerous operations (network search/fetch, pushing, committing).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SETTINGS_PATH = REPO_ROOT / ".claude" / "settings.ci.json"
COMMANDS_DIR = REPO_ROOT / ".claude" / "commands"

# add-modernizer-skill.md is developer scaffolding (used to add a new pipeline
# phase to the repo itself) — it is never invoked during a /modernize run, so
# its script references don't need a CI allow rule.
SCAFFOLDING_ONLY_COMMANDS = {"add-modernizer-skill.md"}

SCRIPT_REF_PATTERN = re.compile(r"uv run python (scripts/[\w\-/{}]+\.py)")


def _load_settings() -> dict:
    data: dict = json.loads(SETTINGS_PATH.read_text())
    return data


def _referenced_scripts() -> set[str]:
    scripts: set[str] = set()
    for command_file in COMMANDS_DIR.glob("*.md"):
        if command_file.name in SCAFFOLDING_ONLY_COMMANDS:
            continue
        content = command_file.read_text()
        for match in SCRIPT_REF_PATTERN.finditer(content):
            script = match.group(1)
            if "{" in script:
                # Templated script name (e.g. run_{phase_name}.py) — only
                # ever appears in scaffolding docs, not a real run.
                continue
            scripts.add(script)
    return scripts


def test_settings_file_is_valid_json_with_allow_and_deny() -> None:
    settings = _load_settings()
    assert "permissions" in settings
    assert isinstance(settings["permissions"]["allow"], list)
    assert isinstance(settings["permissions"]["deny"], list)


def test_every_referenced_script_exists() -> None:
    for script in _referenced_scripts():
        assert (REPO_ROOT / script).exists(), f"referenced script missing: {script}"


def test_every_referenced_script_has_an_allow_rule() -> None:
    settings = _load_settings()
    allow = settings["permissions"]["allow"]

    for script in sorted(_referenced_scripts()):
        assert any(script in rule for rule in allow), (
            f"no Bash allow rule covers '{script}' " f"(referenced by a shipped /modernize command)"
        )


def test_deny_blocks_network_search_fetch_and_push() -> None:
    settings = _load_settings()
    deny = settings["permissions"]["deny"]

    assert "WebFetch" in deny
    assert "WebSearch" in deny
    assert any("git push" in rule for rule in deny)


def test_deny_blocks_destructive_and_remote_fetch_commands() -> None:
    # Defense in depth: nothing in the pipeline needs these, so they're
    # explicitly denied rather than just absent from allow (see ci/README.md).
    settings = _load_settings()
    deny = settings["permissions"]["deny"]

    for dangerous in ("rm", "sudo", "wget", "nc"):
        assert any(
            f"Bash({dangerous} " in rule for rule in deny
        ), f"no explicit deny rule blocks '{dangerous}'"


def test_start_local_ui_is_referenced_and_covered_by_an_allow_rule() -> None:
    # The UI-start block in modernize.md used to be an uncovered multi-command
    # shell pipeline (see scripts/start_local_ui.py's docstring). This is a
    # named tripwire -- on top of the generic coverage test above -- so a
    # future edit that removes the replacement script's reference, or its
    # allow rule, fails loudly and specifically.
    scripts = _referenced_scripts()
    assert "scripts/start_local_ui.py" in scripts

    settings = _load_settings()
    allow = settings["permissions"]["allow"]
    assert any("scripts/start_local_ui.py" in rule for rule in allow)


def test_search_artifacts_is_referenced_and_has_an_exact_allow_rule() -> None:
    # Issue #275: the headless session may have no Grep tool and Bash `grep`
    # is not allowed, so the commands' tool-use rule points at this script.
    # Same exact-prefix form as the other pipeline scripts, not a broader
    # `uv run python *` rule.
    assert "scripts/search_artifacts.py" in _referenced_scripts()
    allow = _load_settings()["permissions"]["allow"]
    assert "Bash(uv run python scripts/search_artifacts.py *)" in allow
    assert not any("grep" in rule for rule in allow), "no Bash grep allow rule"
    assert not any(rule in allow for rule in ("Bash(uv run python *)", "Bash(uv run *)"))


def test_graph_query_is_referenced_and_has_an_exact_allow_rule() -> None:
    # #307: /explore answers from the graph query catalog through this script only;
    # same exact-prefix form as the other pipeline scripts.
    assert "scripts/graph_query.py" in _referenced_scripts()
    allow = _load_settings()["permissions"]["allow"]
    assert "Bash(uv run python scripts/graph_query.py *)" in allow


def test_every_python_bash_allow_is_an_exact_script_prefix() -> None:
    allow = _load_settings()["permissions"]["allow"]
    for rule in allow:
        if rule.startswith("Bash(uv run"):
            assert re.fullmatch(r"Bash\(uv run python scripts/[a-z_]+\.py( \*)?\)", rule), rule


def test_no_bare_read_glob_grep_allow() -> None:
    # In-cwd reads need no rule (docs: dontAsk "file reads in your working
    # directories ... still run"); a bare Read/Glob/Grep allow would extend
    # that to the whole filesystem.
    allow = _load_settings()["permissions"]["allow"]
    for tool in ("Read", "Glob", "Grep"):
        assert tool not in allow, f"bare '{tool}' allow grants reads outside the repo"


def test_deny_blocks_credential_and_system_paths() -> None:
    deny = set(_load_settings()["permissions"]["deny"])
    required = {
        "Read(//proc/**)",
        "Read(//sys/**)",
        "Read(//var/run/secrets/**)",
        "Read(//run/secrets/**)",
        "Read(//root/**)",
        "Read(~/.aws/**)",
        "Read(~/.ssh/**)",
        "Read(~/.config/**)",
        "Read(~/.docker/**)",
        "Read(~/.npmrc)",
        "Read(**/.npmrc)",
        "Read(~/.netrc)",
        "Read(~/.claude.json)",
        "Read(.env)",
        "Read(**/.env)",
        "Read(**/.env.*)",
    }
    missing = required - deny
    assert not missing, f"missing deny rules: {sorted(missing)}"


def test_deny_blocks_writes_to_local_ui_state() -> None:
    deny = set(_load_settings()["permissions"]["deny"])
    for tool in ("Edit", "Write"):
        assert f"{tool}(.local-ui/**)" in deny
        assert f"{tool}(artifacts/.local-ui/**)" in deny


def test_deny_blocks_curl_like_wget() -> None:
    deny = _load_settings()["permissions"]["deny"]
    assert "Bash(curl *)" in deny


def test_every_dispatched_subcommand_has_a_skill_allow() -> None:
    from tests.unit.scripts.test_modernize_command import DISPATCHED_SUBCOMMANDS

    allow = _load_settings()["permissions"]["allow"]
    for filename in DISPATCHED_SUBCOMMANDS:
        name = filename.removesuffix(".md")
        assert f"Skill({name})" in allow, f"no Skill({name}) allow rule"


def test_bash_timeouts_cover_long_pipeline_phases() -> None:
    env = _load_settings()["env"]
    assert int(env["BASH_DEFAULT_TIMEOUT_MS"]) >= 600_000
    assert int(env["BASH_MAX_TIMEOUT_MS"]) >= int(env["BASH_DEFAULT_TIMEOUT_MS"])


@pytest.mark.passes_on_main
def test_no_allow_rule_reaches_into_the_home_directory() -> None:
    """#1593 review: the blanket Read(//root/**) deny stays (the CI runner's home),
    and no allow rule opens anything under ~/.claude, ~ or /root. A headless script
    keeps its line short and writes any detail under artifacts/ instead."""
    settings = _load_settings()["permissions"]
    assert "Read(//root/**)" in settings["deny"]
    for rule in settings["allow"]:
        _, _, spec = rule.partition("(")
        assert not spec.startswith(("~", "//root", "/root")), rule
        assert ".claude/projects" not in spec and "tool-results" not in spec, rule


@pytest.mark.passes_on_main
def test_the_headless_run_never_adds_a_directory() -> None:
    """--add-dir would make reads outside the repo 'working directory' reads that
    dontAsk allows with no rule; ci/e2e-llm.sh never passes it."""
    script = (REPO_ROOT / "ci" / "e2e-llm.sh").read_text()
    assert "--add-dir" not in script


def _bash_rule_matches(command: str, rules: list[str]) -> bool:
    """Claude Code's Bash rule glob: ``*`` matches any run of characters, spaces included."""
    import fnmatch

    patterns = [r[len("Bash(") : -1] for r in rules if r.startswith("Bash(") and r.endswith(")")]
    return any(fnmatch.fnmatchcase(command, p) for p in patterns)


@pytest.mark.parametrize(
    "command",
    [
        "uv run python scripts/run_dms_step.py proof-target-plan --job-id j --db d",
        "uv run python scripts/run_dms_step.py proof-target-deploy --job-id j --db d --approve",
        "uv run python scripts/run_dms_step.py proof-target-status --job-id j --db d",
        "uv run python scripts/run_dms_step.py proof-target-teardown --job-id j --db d --approve",
        # argparse takes the options before the subcommand too
        "uv run python scripts/run_dms_step.py --job-id j --db d proof-target-deploy --approve",
    ],
)
def test_a_headless_run_is_denied_every_proof_target_command(command: str) -> None:
    """#1946: the routine-proof test cluster is never deployed, read or deleted headless. A deny
    wins over the ``run_dms_step.py *`` allow, whatever the caller passes (``--auto`` or not)."""
    permissions = _load_settings()["permissions"]
    assert _bash_rule_matches(command, permissions["allow"])  # the allow alone would let it run
    assert _bash_rule_matches(command, permissions["deny"])


@pytest.mark.parametrize("subcommand", ["status --until stack", "plan", "routines", "teardown"])
def test_the_other_run_dms_step_subcommands_stay_allowed(subcommand: str) -> None:
    permissions = _load_settings()["permissions"]
    command = f"uv run python scripts/run_dms_step.py {subcommand} --job-id j --db d"
    assert _bash_rule_matches(command, permissions["allow"])
    assert not _bash_rule_matches(command, permissions["deny"])
