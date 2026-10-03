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
