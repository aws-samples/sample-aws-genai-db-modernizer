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
