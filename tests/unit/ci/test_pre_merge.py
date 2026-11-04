# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ci/pre_merge.py: the parsing and verdict logic behind ci/pre_merge.sh."""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404 -- builds scratch git repositories with fixed argv
import sys
from pathlib import Path

import pytest
import yaml

from ci.pre_merge import (
    check_contract,
    contract_versions,
    next_version,
    parse_merge_tree,
    pending_bumps,
    pick_remote,
    pick_stack_base,
    stack_notice,
)

PRE_MERGE_PY = Path(__file__).resolve().parents[3] / "ci" / "pre_merge.py"
PUBLIC = "https://github.com/aws-samples/sample-aws-genai-db-modernizer"


def _contract(version: str | None) -> str | None:
    if version is None:
        return None
    return f'class M:\n    contract_version: str = Field(\n        default="{version}", pattern=r"^\\d+\\.\\d+$"\n    )\n'


def test_pick_remote_prefers_the_public_repo():
    remotes = f"origin\tgit@host:me/fork.git (fetch)\nupstream\t{PUBLIC}.git (fetch)\n"
    assert pick_remote(remotes) == "upstream"


def test_pick_remote_prefers_origin_when_both_match():
    remotes = f"origin\t{PUBLIC} (fetch)\nupstream\t{PUBLIC} (fetch)\n"
    assert pick_remote(remotes) == "origin"


def test_pick_remote_falls_back_to_origin():
    assert pick_remote("mirror\thttps://example.com/x.git (fetch)\n") == "origin"
    assert pick_remote("") == "origin"


def test_parse_merge_tree_lists_conflicting_files_only():
    out = "deadbeef\nsrc/a.py\nsrc/b.py\n\nAuto-merging src/a.py\nCONFLICT (content): src/a.py\n"
    assert parse_merge_tree(out) == ["src/a.py", "src/b.py"]


def test_parse_merge_tree_clean_has_no_files():
    assert parse_merge_tree("deadbeef\n") == []


def test_contract_versions_reads_multi_line_defaults():
    assert contract_versions(_contract("2.10")) == [(2, 10)]
    assert contract_versions(None) == []
    assert contract_versions("x = 1") == []


def test_next_version_is_the_minor_bump():
    assert next_version((2, 9)) == "2.10"


@pytest.mark.parametrize(
    ("base", "main", "head", "level", "text"),
    [
        ("1.0", "1.0", "1.1", "ok", "higher"),
        ("1.0", "1.1", "1.2", "ok", "higher"),
        ("1.0", "1.1", "1.1", "error", "declare the bump instead"),
        ("1.0", "1.1", "1.1", "error", "or take 1.2 by hand"),
        ("1.0", "1.3", "1.1", "error", "lower than main's 1.3"),
        ("1.0", "1.0", "1.0", "warn", "still at 1.0"),
        ("1.0", "1.1", "1.0", "ok", "unchanged"),
        (None, None, "1.0", "ok", "higher"),
    ],
)
def test_check_contract(base, main, head, level, text):
    got_level, message = check_contract(
        "src/contracts/x.py", _contract(base), _contract(main), _contract(head)
    )
    assert got_level == level
    assert text in message


def test_check_contract_ignores_files_without_a_version():
    assert check_contract("src/contracts/x.py", "a", "b", "c")[0] == "ok"


def test_ci_workflow_reports_on_a_merge_group():
    workflow = Path(__file__).resolve().parents[3] / ".github/workflows/ci.yml"
    loaded = yaml.safe_load(workflow.read_text())
    triggers = loaded.get("on") or loaded[True]  # PyYAML reads a bare `on:` as True
    assert "merge_group" in triggers
    # Required checks must not read pull request context, which a merge group lacks.
    assert "github.event.pull_request" not in workflow.read_text().replace("\n#", "")


@pytest.mark.parametrize(
    ("base", "main", "head", "level", "text"),
    [
        ("1.0", "1.0", "1.0", "ok", "pending minor bump declared"),
        ("1.0", "1.1", "1.0", "ok", "pending minor bump declared"),
        ("1.0", "1.0", "1.1", "error", "keep one"),
    ],
)
def test_check_contract_with_a_pending_bump(base, main, head, level, text):
    """ADR-008 Decision 8 (#1158): a pending bump keeps main's version until it is
    resolved; a pending file next to a hand-written version is a mistake."""
    got_level, message = check_contract(
        "src/contracts/x.py", _contract(base), _contract(main), _contract(head), "minor"
    )
    assert got_level == level
    assert text in message


def test_pending_bumps_reads_each_file_and_flags_invalid_ones():
    texts = {
        "src/contracts/pending/a.C.x.toml": 'contract = "a.C"\nkind = "minor"\nreason = "r"\n',
        "src/contracts/pending/b.y.toml": 'contract = "b"\nkind = "major"\nreason = "r"\n',
        "src/contracts/pending/c.z.toml": 'contract = "c"\nkind = "patch"\n',
        "src/contracts/pending/d.z.toml": "contract = ",
    }
    assert pending_bumps(texts) == [
        ("a.C", "minor"),
        ("b", "major"),
        ("src/contracts/pending/c.z.toml", "invalid"),
        ("src/contracts/pending/d.z.toml", "invalid"),
    ]


# --- ADR touch rule (#1154) ---------------------------------------------------

REFEREE = "src/agents/referee/routing.py"
ADR = "docs/architecture/decisions/ADR-031-query-first-routing-and-engine-rule-catalog.md"


def test_adr_rule_ignores_branches_that_change_no_behaviour():
    from ci.pre_merge import check_adr

    changed = ["tests/unit/agents/referee/test_x.py", "ci/lib.sh", "src/agents/collector/a.py"]
    assert check_adr(changed, "")[0] == "ok"


def test_adr_rule_fails_on_referee_or_shared_behaviour_without_an_adr_or_a_statement():
    from ci.pre_merge import check_adr

    for path in (REFEREE, "src/shared/engine_names.py"):
        level, message = check_adr([path, "tests/unit/x.py"], "fix: something\n")
        assert level == "error", path
        assert path in message


def test_adr_rule_passes_when_an_adr_is_edited():
    from ci.pre_merge import check_adr

    assert check_adr([REFEREE, ADR], "")[0] == "ok"


def test_adr_rule_passes_on_a_stated_reason_in_text():
    from ci.pre_merge import check_adr

    assert check_adr([REFEREE], "body\n\nNo ADR change: pure refactor, same decisions\n")[0] == "ok"


def test_adr_rule_rejects_the_unfilled_template_placeholder_and_an_empty_reason():
    from ci.pre_merge import check_adr

    assert check_adr([REFEREE], "No ADR change: <reason>")[0] == "error"
    assert check_adr([REFEREE], "No ADR change:   \n")[0] == "error"


def test_adr_rule_does_not_count_non_python_files_under_the_paths():
    from ci.pre_merge import check_adr

    assert check_adr(["src/shared/README.md"], "")[0] == "ok"


def test_the_pr_template_has_an_adr_field_that_pre_merge_accepts():
    template = (
        Path(__file__).resolve().parents[3] / ".github" / "pull_request_template.md"
    ).read_text(encoding="utf-8")
    assert "## ADR" in template
    assert "No ADR change: <reason>" in template


# ---- Docs-impact map (#1162) ----

_MAP = [
    {
        "name": "report",
        "paths": ["src/report/**", "scripts/run_report.py"],
        "docs": ["docs/understand-the-results.md", "README.md"],
    },
    {
        "name": "launcher",
        "paths": ["scripts/start_local_ui.py"],
        "docs": ["docs/use-claude-code.md"],
    },
]


def test_docs_glob_star_stays_in_one_folder_and_double_star_crosses_folders():
    from ci.pre_merge import docs_glob_matches

    assert docs_glob_matches("src/report/*.py", "src/report/facts.py")
    assert not docs_glob_matches("src/report/*.py", "src/report/assets/x.py")
    assert docs_glob_matches("src/report/**", "src/report/assets/x.png")
    assert docs_glob_matches("scripts/run_report.py", "scripts/run_report.py")
    assert not docs_glob_matches("scripts/run_report.py", "scripts/run_report.pyc")


def test_docs_impact_is_quiet_when_no_mapped_path_changed():
    from ci.pre_merge import check_docs_impact

    level, _ = check_docs_impact(["src/api/main.py", "tests/unit/x.py"], False, _MAP)
    assert level == "ok"


def test_docs_impact_is_quiet_when_a_mapped_page_changed_too():
    from ci.pre_merge import check_docs_impact

    level, message = check_docs_impact(["src/report/facts.py", "README.md"], False, _MAP)
    assert level == "ok"
    assert "report" in message


def test_docs_impact_warns_and_lists_the_pages_when_the_branch_has_a_reason():
    from ci.pre_merge import check_docs_impact

    level, message = check_docs_impact(["src/report/facts.py"], True, _MAP)
    assert level == "warn"
    assert "docs/understand-the-results.md" in message and "README.md" in message
    assert "src/report/facts.py" in message


def test_docs_impact_fails_without_a_reason_and_lists_the_pages():
    from ci.pre_merge import check_docs_impact

    level, message = check_docs_impact(["scripts/start_local_ui.py"], False, _MAP)
    assert level == "error"
    assert "docs/use-claude-code.md" in message
    assert "Docs:" in message and "no PR body" not in message


def test_docs_impact_without_a_pr_body_says_so_and_names_the_trailer():
    from ci.pre_merge import check_docs_impact

    _, message = check_docs_impact(["scripts/start_local_ui.py"], False, _MAP, body_missing=True)
    assert "no PR body found" in message and "commit trailer" in message


def test_docs_impact_counts_each_mapped_group_on_its_own():
    from ci.pre_merge import check_docs_impact

    # The report pages changed; the launcher's page did not.
    changed = ["src/report/facts.py", "README.md", "scripts/start_local_ui.py"]
    level, message = check_docs_impact(changed, False, _MAP)
    assert level == "error"
    assert "docs/use-claude-code.md" in message
    assert "docs/understand-the-results.md" not in message


def test_a_visible_docs_line_in_the_body_counts():
    from ci.pre_merge import has_docs_line

    assert has_docs_line("## Docs\n\nDocs: internal refactor, no wording changed\n", [])
    assert has_docs_line("Docs: reason\r\nmore\r\n", [])


def test_bulleted_docs_lines_count():
    from ci.pre_merge import has_docs_line

    assert has_docs_line("- Docs: no page describes it\n", [])
    assert has_docs_line("* Docs: no page describes it\n", [])


@pytest.mark.parametrize(
    "body",
    [
        "```\nDocs: hidden in a fence\n```\n",
        "    Docs: hidden in an indented block\n",
        "<!--\nDocs: hidden in a comment\n-->\n",
        "<!-- Docs: one-line comment -->\n",
        "`Docs: code span`\n",
        "Docs: <reason>\n",
        "Docs:   \n",
        "see Docs: later\n",
        "docs: lower case\n",
    ],
)
def test_hidden_or_empty_docs_lines_do_not_count(body):
    from ci.pre_merge import has_docs_line

    assert not has_docs_line(body, [])


def test_a_commit_trailer_counts_but_a_docs_subject_does_not():
    from ci.pre_merge import has_docs_line

    assert has_docs_line(None, ["fix: x\n\nbody\n\nDocs: no page describes it"])
    assert not has_docs_line(None, ["docs: fix a typo in the README"])
    assert not has_docs_line(None, ["Docs: subject line only"])
    assert not has_docs_line(None, ["fix: x\n\ndocs: lower case in the body"])


def test_the_pr_template_has_a_docs_field_that_pre_merge_accepts():
    template = (
        Path(__file__).resolve().parents[3] / ".github" / "pull_request_template.md"
    ).read_text(encoding="utf-8")
    assert "## Docs" in template
    assert "Docs: <reason>" in template
    assert "not affected" not in template


# ---- Commit hooks installed (#1843) ----

_PC_HOOK = """#!/usr/bin/env bash
# File generated by pre-commit: https://pre-commit.com
# ID: 138fd403232d2ddd5efb44317e38bf03

# start templated
INSTALL_PYTHON=/x/.venv/bin/python
ARGS=(hook-impl --config=.pre-commit-config.yaml --hook-type={kind})
# end templated
"""


def _hook(path: Path, text: str, executable: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755 if executable else 0o644)
    return path


def _pc_hooks(folder: Path, kinds=("pre-commit", "commit-msg")) -> Path:
    for kind in kinds:
        _hook(folder / kind, _PC_HOOK.format(kind=kind))
    return folder


def _chaining_hooks(folder: Path, kinds=("pre-commit", "commit-msg")) -> Path:
    for kind in kinds:
        _hook(
            folder / kind,
            f'#!/bin/sh\nexec "$(git rev-parse --git-common-dir)/hooks/{kind}" "$@"\n',
        )
    return folder


def test_hooks_ok_when_both_local_hooks_are_pre_commits(tmp_path):
    from ci.pre_merge import check_hooks

    level, message = check_hooks(_pc_hooks(tmp_path / "hooks"), None)
    assert level == "ok"
    assert "pre-commit" in message and "commit-msg" in message


@pytest.mark.parametrize("missing", ["pre-commit", "commit-msg"])
def test_hooks_fail_when_a_local_hook_is_missing_and_say_how_to_install(tmp_path, missing):
    from ci.pre_merge import check_hooks

    kinds = [k for k in ("pre-commit", "commit-msg") if k != missing]
    level, message = check_hooks(_pc_hooks(tmp_path / "hooks", kinds), None)
    assert level == "error"
    assert missing in message
    assert "./scripts/setup_dev.sh" in message
    assert "pre-commit install --hook-type pre-commit --hook-type commit-msg" in message
    assert "GIT_CONFIG" not in message  # no hooks path: the plain install works


def test_hooks_fail_on_a_hook_that_is_not_pre_commits_or_not_executable(tmp_path):
    from ci.pre_merge import check_hooks

    local = _pc_hooks(tmp_path / "a")
    _hook(local / "commit-msg", "#!/bin/sh\nexit 0\n")
    assert check_hooks(local, None)[0] == "error"
    local = _pc_hooks(tmp_path / "b")
    (local / "pre-commit").chmod(0o644)
    level, message = check_hooks(local, None)
    assert level == "error" and "pre-commit" in message


def test_hooks_fail_on_pre_commits_hook_of_the_wrong_type(tmp_path):
    """A pre-commit hook script copied in as commit-msg runs the wrong stage."""
    from ci.pre_merge import check_hooks

    local = _pc_hooks(tmp_path / "hooks")
    _hook(local / "commit-msg", _PC_HOOK.format(kind="pre-commit"))
    level, message = check_hooks(local, None)
    assert level == "error" and "commit-msg" in message


def test_hooks_through_a_global_hooks_path_need_the_local_hooks_too(tmp_path):
    """A global hooksPath that chains to the local hooks runs nothing of ours when
    pre-commit never installed them: the check must fail there."""
    from ci.pre_merge import check_hooks

    global_dir = _chaining_hooks(tmp_path / "global")
    level, message = check_hooks(tmp_path / "local", global_dir, "global")
    assert level == "error"
    assert "core.hooksPath" in message and "global" in message
    assert "./scripts/setup_dev.sh" in message
    # pre-commit refuses to install while core.hooksPath is set: the hint gets around it.
    assert "GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0=" in message
    assert "pre-commit install --hook-type pre-commit --hook-type commit-msg" in message


def test_hooks_through_a_global_hooks_path_pass_with_the_local_hooks(tmp_path):
    from ci.pre_merge import check_hooks

    local = _pc_hooks(tmp_path / "local")
    level, message = check_hooks(local, _chaining_hooks(tmp_path / "global"), "global")
    assert level == "ok"
    assert "core.hooksPath" in message


def test_hooks_fail_when_the_hooks_path_has_no_hook_of_a_type(tmp_path):
    """git runs only the hooks path's hooks: one without commit-msg runs none, whatever
    the local folder holds."""
    from ci.pre_merge import check_hooks

    local = _pc_hooks(tmp_path / "local")
    global_dir = _chaining_hooks(tmp_path / "global", ["pre-commit"])
    level, message = check_hooks(local, global_dir, "global")
    assert level == "error"
    assert "commit-msg" in message and "pre-commit:" not in message


def test_hooks_pass_when_the_hooks_path_holds_pre_commits_own_hooks(tmp_path):
    from ci.pre_merge import check_hooks

    level, _ = check_hooks(tmp_path / "local", _pc_hooks(tmp_path / "shared"), "local")
    assert level == "ok"


def test_hooks_path_naming_the_local_folder_still_needs_the_install_override(tmp_path):
    """pre-commit refuses to install under any core.hooksPath, even one naming .git/hooks."""
    from ci.pre_merge import check_hooks

    local = tmp_path / "hooks"
    _pc_hooks(local, ["pre-commit"])
    level, message = check_hooks(local, local, "local")
    assert level == "error" and "commit-msg" in message
    assert "GIT_CONFIG_KEY_0=core.hooksPath" in message
    assert check_hooks(_pc_hooks(local), local, "local")[0] == "ok"


def _git(cwd: Path, *args: str) -> str:
    import subprocess

    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def isolated_git(tmp_path, monkeypatch):
    """A repo whose git sees no system config and a global config of the test's own."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / "gitconfig").write_text("")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q",
         "--allow-empty", "--no-verify", "-m", "init")  # fmt: skip
    return repo


def test_hooks_subcommand_finds_the_hooks_in_the_common_dir_from_a_worktree(
    isolated_git, tmp_path, monkeypatch, capsys
):
    from ci.pre_merge import main

    _pc_hooks(isolated_git / ".git" / "hooks")
    _git(isolated_git, "worktree", "add", "-q", "--detach", str(tmp_path / "wt"))
    monkeypatch.chdir(tmp_path / "wt")
    assert main(["hooks"]) == 0
    assert capsys.readouterr().out.startswith("ok: ")


def test_hooks_subcommand_fails_a_clone_with_a_global_hooks_path_and_no_local_hooks(
    isolated_git, tmp_path, monkeypatch, capsys
):
    from ci.pre_merge import main

    global_dir = _chaining_hooks(tmp_path / "global")
    _git(isolated_git, "config", "--global", "core.hooksPath", str(global_dir))
    monkeypatch.chdir(isolated_git)
    assert main(["hooks"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("error: ") and "global" in out
    _pc_hooks(isolated_git / ".git" / "hooks")
    assert main(["hooks"]) == 0


def test_hooks_subcommand_reads_a_relative_hooks_path_from_the_top_level(
    isolated_git, monkeypatch, capsys
):
    from ci.pre_merge import main

    _pc_hooks(isolated_git / ".githooks")
    _git(isolated_git, "config", "core.hooksPath", ".githooks")
    (isolated_git / "sub").mkdir()
    monkeypatch.chdir(isolated_git / "sub")
    assert main(["hooks"]) == 0, capsys.readouterr().out


def test_pre_merge_checks_the_hooks_in_every_mode():
    sh = (Path(__file__).resolve().parents[3] / "ci" / "pre_merge.sh").read_text(encoding="utf-8")
    step = sh.split("# a2. ")[1].split("# b. ")[0]
    assert "helper hooks" in step
    assert '"$quick"' not in step
    assert 'fail "hooks:' in step
    assert sh.index("# a2. ") < sh.index("# i. ")


def test_setup_dev_installs_both_hook_types_even_under_a_hooks_path():
    sh = (Path(__file__).resolve().parents[3] / "scripts" / "setup_dev.sh").read_text(
        encoding="utf-8"
    )
    assert "pre-commit install --hook-type pre-commit --hook-type commit-msg" in sh
    assert "GIT_CONFIG_KEY_0=core.hooksPath" in sh


def test_hooks_subcommand_fails_an_empty_hooks_path(isolated_git, monkeypatch, capsys):
    """Round 1: git runs no hook at all when core.hooksPath is set to an empty value,
    whatever the clone's own hooks folder holds."""
    from ci.pre_merge import main

    _pc_hooks(isolated_git / ".git" / "hooks")
    _git(isolated_git, "config", "core.hooksPath", "")
    monkeypatch.chdir(isolated_git)
    assert main(["hooks"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("error: ") and "empty value" in out and "local" in out
    assert "git config --local --unset core.hooksPath" in out


def test_hooks_subcommand_warns_when_git_cannot_show_the_scope(isolated_git, monkeypatch, capsys):
    """Round 1: `git config --show-scope` needs git 2.26; without it the hooks path is
    unknown, so a pass on the local hooks is a warning, not an ok."""
    import subprocess as sp

    from ci import pre_merge

    _pc_hooks(isolated_git / ".git" / "hooks")
    real = sp.run

    def old_git(cmd, *a, **k):
        if "--show-scope" in cmd:
            return sp.CompletedProcess(cmd, 129, "", "error: unknown option `show-scope'")
        return real(cmd, *a, **k)

    monkeypatch.setattr(pre_merge.subprocess, "run", old_git)
    monkeypatch.chdir(isolated_git)
    assert pre_merge.main(["hooks"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("warn: ") and "2.26" in out
    (isolated_git / ".git" / "hooks" / "commit-msg").unlink()
    assert pre_merge.main(["hooks"]) == 1  # a local problem still fails
    assert capsys.readouterr().out.startswith("error: ")


def test_hooks_subcommand_prints_the_hooks_folder_as_an_absolute_path(
    isolated_git, monkeypatch, capsys
):
    from ci.pre_merge import main

    _pc_hooks(isolated_git / ".git" / "hooks")
    monkeypatch.chdir(isolated_git)
    assert main(["hooks"]) == 0
    assert str((isolated_git / ".git" / "hooks").resolve()) in capsys.readouterr().out


@pytest.mark.parametrize(
    ("printed", "code", "want"),
    [
        ("ok: fine", 0, "ok   hooks: fine"),
        ("warn: too old", 0, "warn hooks: too old"),
        ("error: missing", 1, "FAIL hooks: missing"),
    ],
)
def test_pre_merge_hooks_step_prints_each_level(printed, code, want):
    """The a2 step itself, run in bash with the helper stubbed."""
    import subprocess

    sh = (Path(__file__).resolve().parents[3] / "ci" / "pre_merge.sh").read_text(encoding="utf-8")
    step = "# a2. " + sh.split("# a2. ")[1].split("# b. ")[0]
    script = (
        "set -euo pipefail\n"
        f"helper() {{ echo '{printed}'; return {code}; }}\n"
        'note() { printf "%s\\n" "$*"; }\nfail() { note "FAIL $*"; }\n' + step
    )
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == want


def test_setup_dev_leaves_a_system_hooks_path_in_place():
    """Round 1: other tooling owns a system hooks path; setup never removes it (no sudo),
    and the docs no longer send anyone to a sudo step."""
    root = Path(__file__).resolve().parents[3]
    sh = (root / "scripts" / "setup_dev.sh").read_text(encoding="utf-8")
    assert "sudo" not in sh
    assert "--unset-all core.hookspath" not in sh.lower()
    # Set at all (even empty) takes the one-command blank, as pre-commit refuses otherwise.
    assert "git config --get core.hooksPath >/dev/null" in sh
    contributing = (root / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "sudo git config" not in contributing


# ---- Version pins with --quick (#1845) ----


def test_quick_runs_the_version_pin_check_on_what_the_branch_touched():
    sh = (Path(__file__).resolve().parents[3] / "ci" / "pre_merge.sh").read_text(encoding="utf-8")
    assert "skip version-pins" not in sh
    step = sh.split("# i. ")[1].split("# j. ")[0]
    assert 'pins_args=(--base "$base")' in step
    assert 'scripts/check_version_pins.py ${pins_args[@]+"${pins_args[@]}"}' in step
    # The full run keeps every candidate test: --base is set only under --quick.
    assert step.index('if [ "$quick" -eq 1 ]') < step.index("pins_args=(--base")


# --- stacked branches (#1846) ------------------------------------------------------


def _prs(*rows, draft=False, fork=False):
    return [
        {
            "number": n,
            "headRefName": ref,
            "headRefOid": oid,
            "isDraft": draft,
            "isCrossRepository": fork,
        }
        for n, ref, oid in rows
    ]


def test_stack_notice_names_the_pull_request_twice():
    assert stack_notice(1637) == "stacked on #1637: re-check after #1637 merges"


def test_a_branch_carrying_another_pr_head_is_stacked_on_it():
    prs = _prs((1637, "fix/a", "c2"), (1640, "fix/b", "c4"))
    assert pick_stack_base(["c1", "c2", "c3", "c4"], prs, "fix/b", 1640) == (1637, "c2")


def test_the_nearest_of_several_carried_pull_requests_is_the_base():
    prs = _prs((10, "a", "c1"), (11, "b", "c3"), (12, "own", "c4"))
    assert pick_stack_base(["c1", "c2", "c3", "c4"], prs, "own", 12) == (11, "c3")


@pytest.mark.parametrize(
    ("ahead", "prs", "own_ref", "own_number"),
    [
        (["c1", "c2"], [], "own", 5),  # no other open pull request
        (["c1", "c2"], _prs((7, "other", "zz")), "own", 5),  # its head is not carried
        ([], _prs((7, "other", "c9")), "own", 5),  # nothing ahead of main
        (["c1", "c2"], _prs((5, "own", "c2")), "own", 5),  # only its own pull request
        (["c1", "c2"], _prs((6, "own", "c1")), "own", None),  # its own earlier push, by name
        (["c1", "c2"], _prs((5, "renamed", "c1")), "own", 5),  # its own pull request, by number
        (["c1"], [{"number": 7, "headRefName": "x", "headRefOid": ""}], "own", 5),
    ],
)
def test_a_branch_that_carries_no_other_pr_is_not_stacked(ahead, prs, own_ref, own_number):
    assert pick_stack_base(ahead, prs, own_ref, own_number) is None


@pytest.mark.parametrize("flags", [{"draft": True}, {"fork": True}, {"draft": True, "fork": True}])
def test_a_draft_or_fork_pr_is_never_the_stack_base(flags):
    """A throwaway pull request (a fork's, or a draft) must not make the contract diff
    inside its head commit skip the check on a branch that merges it in."""
    prs = _prs((1999, "dummy", "d1"), **flags)
    assert pick_stack_base(["a1", "a2", "b1", "d1", "merge"], prs, "own", 5) is None
    # The same pull request, open and from this repository, is the base.
    assert pick_stack_base(["a1", "d1", "merge"], _prs((1999, "dummy", "d1")), "own", 5) == (
        1999,
        "d1",
    )


def test_an_ineligible_pr_does_not_hide_an_eligible_one_further_down():
    prs = _prs((1, "a", "a2")) + _prs((2, "dummy", "d1"), draft=True)
    assert pick_stack_base(["a1", "a2", "d1"], prs, "own", 5) == (1, "a2")


def test_a_pr_missing_the_draft_or_fork_fields_is_not_trusted():
    prs = [{"number": 3, "headRefName": "a", "headRefOid": "a1"}]
    assert pick_stack_base(["a1"], prs, "own", 5) is None


def test_a_stacked_on_pr_that_moved_on_is_not_a_stack():
    """Its new head is not among the branch's commits, so main is the base again."""
    assert pick_stack_base(["a1", "b1"], _prs((1, "a", "a2")), "own", 5) is None


FAKE_GH = """#!/bin/sh
case "$1 $2" in
  "pr view") echo '{"number": 1640}' ;;
  "pr list") cat "$FAKE_GH_PRS" ;;
  "api repos/o/r/compare/main...HEAD1") echo '{"commits": [{"sha": "c1"}, {"sha": "c2"}]}' ;;
  *) exit 1 ;;
esac
"""


def _run_stacked(tmp_path, args, prs, env_extra=None, with_gh=True):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    if with_gh:
        gh = bindir / "gh"
        gh.write_text(FAKE_GH)
        gh.chmod(0o755)
    (tmp_path / "prs.json").write_text(json.dumps(prs))
    env = {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}/usr/bin{os.pathsep}/bin",
        "FAKE_GH_PRS": str(tmp_path / "prs.json"),
        **(env_extra or {}),
    }
    return subprocess.run(
        [sys.executable, str(PRE_MERGE_PY), "stacked", *args],
        cwd=tmp_path / "repo" if (tmp_path / "repo").exists() else tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*a):
        return subprocess.run(
            ["git", "-c", "user.email=t@e.x", "-c", "user.name=t", *a],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    git("init", "-q", "-b", "main")
    (repo / "f").write_text("0")
    git("add", "f")
    git("commit", "-qm", "root")
    git("checkout", "-q", "-b", "fix/own")
    shas = []
    for i in (1, 2):
        (repo / "f").write_text(str(i))
        git("commit", "-qam", f"c{i}")
        shas.append(git("rev-parse", "HEAD"))
    return shas


def test_the_stacked_command_prints_the_pr_whose_head_the_branch_carries(tmp_path):
    first, second = _git_repo(tmp_path)
    prs = _prs((1637, "fix/base", first), (1640, "fix/own", second))
    got = _run_stacked(tmp_path, ["--main-ref", "main"], prs)
    assert got.stdout.strip() == f"stacked 1637 {first}"
    # The shell script's notice comes from the same function.
    notice = subprocess.run(
        [sys.executable, str(PRE_MERGE_PY), "stack-notice", "1637"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert notice.stdout.strip() == "stacked on #1637: re-check after #1637 merges"


def test_the_stacked_command_prints_nothing_when_no_pr_is_carried(tmp_path):
    first, second = _git_repo(tmp_path)
    got = _run_stacked(tmp_path, ["--main-ref", "main"], _prs((1640, "fix/own", second)))
    assert got.returncode == 0 and got.stdout == ""


def test_the_stacked_command_fails_open_without_gh(tmp_path):
    _git_repo(tmp_path)
    got = _run_stacked(tmp_path, ["--main-ref", "main"], [], with_gh=False)
    assert got.returncode == 0 and got.stdout == ""


def test_the_stacked_command_reads_the_commits_ahead_from_the_api_in_ci(tmp_path):
    prs = _prs((1637, "fix/base", "c1"), (1640, "fix/own", "c2"))
    got = _run_stacked(
        tmp_path,
        ["--api-repo", "o/r", "--head-sha", "HEAD1", "--head-ref", "fix/own", "--number", "1640"],
        prs,
    )
    assert got.stdout.strip() == "stacked 1637 c1"


# The false positive of #1640: the stacked branch carries the base's resolved 1.1 and a
# pending file. Against main (1.0) the pair reads as a hand bump; against the base's head
# (1.1) it is the resolved state. A hand bump on top of the stack still differs from it.
@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    ("compare_text_version", "head", "level", "text"),
    [
        ("1.0", "1.1", "error", "keep one"),  # compared with main: the false positive
        ("1.1", "1.1", "ok", "pending minor bump declared"),  # compared with the stack base
        ("1.1", "1.2", "error", "keep one"),  # a real hand bump on the stack still fails
    ],
)
def test_check_contract_on_a_stacked_branch_compares_with_the_stack_base(
    compare_text_version, head, level, text
):
    def module(v):
        return f'contract_version: str = Field(default="{v}")'

    got_level, message = check_contract(
        "src/contracts/x.py",
        module(compare_text_version),
        module(compare_text_version),
        module(head),
        pending="minor",
    )
    assert got_level == level and text in message


def test_the_ci_contract_step_looks_for_a_stack_without_the_pull_request_payload():
    text = (PRE_MERGE_PY.parents[1] / ".github/workflows/ci.yml").read_text()
    step = text[text.index("- name: Check pending contract bumps") :]
    step = step[: step.index("- name: Check no test pins")]
    assert "ci/pre_merge.py stacked --api-repo" in step
    assert "ci/pre_merge.py stack-notice" in step
    assert "HEAD^2" in step  # the pull request's own head, not event.pull_request
    assert "event.pull_request" not in step  # the payload a merge group lacks
    assert "stack-caveat" in step and "::warning::" in step
    assert 'GH_TOKEN="$DETECT_TOKEN" uv run python ci/pre_merge.py stacked' in step
    assert "\n          GH_TOKEN:" not in text  # the token is not in the step's env


def test_pre_merge_sh_compares_the_contract_checks_with_the_stack_base():
    script = (PRE_MERGE_PY.parent / "pre_merge.sh").read_text()
    assert 'helper stacked --main-ref "$base"' in script
    assert 'helper contracts "$compare_ref"' in script
    assert 'merge-base HEAD "$compare_ref"' in script
    assert "resolve_contract_versions.py --base $compare_ref" in script


@pytest.mark.passes_on_main
@pytest.mark.parametrize("compare", ["1.0", "1.1"])
def test_a_plain_hand_bump_is_allowed_against_main_and_against_a_stack_base(compare):
    """What the stacked comparison does not add: a version higher than the compared
    commit's, with no pending file, is ok (a contract the resolver cannot edit is bumped by
    hand, and the message offers it). The docs claim no more than this."""

    def module(v):
        return f'contract_version: str = Field(default="{v}")'

    level, message = check_contract(
        "src/contracts/x.py", module(compare), module(compare), module("1.5")
    )
    assert level == "ok" and "higher than" in message


def test_docs_impact_reviews_the_pages_a_changed_claim_test_proves(tmp_path: Path):
    """#1851: a changed test that a claim marker cites lists the pages that cite it."""
    from ci.pre_merge import check_docs_impact, docs_impact_entries

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_g.py").write_text("def test_guard():\n    pass\n", "utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "page.md").write_text(
        "It refuses. <!-- claim: tests/test_g.py::test_guard -->\n", encoding="utf-8"
    )
    (tmp_path / "docs" / "other.md").write_text("No markers here.\n", encoding="utf-8")
    for argv in (["git", "init", "-q"], ["git", "add", "-A"]):
        subprocess.run(argv, cwd=tmp_path, check=True)  # nosec B603 B607 -- fixed argv
    entries = [e for e in docs_impact_entries(tmp_path) if e["name"].startswith("claims:")]
    assert [(e["paths"], e["docs"]) for e in entries] == [(["tests/test_g.py"], ["docs/page.md"])]
    level, message = check_docs_impact(["tests/test_g.py"], False, entries)
    assert level == "error" and "docs/page.md" in message
    assert check_docs_impact(["tests/test_g.py", "docs/page.md"], False, entries)[0] == "ok"
    assert check_docs_impact(["tests/test_other.py"], False, entries)[0] == "ok"


def test_the_repo_docs_impact_entries_include_the_claim_pages():
    from ci.pre_merge import docs_impact_entries, load_docs_impact_map

    entries = docs_impact_entries()
    derived = [e for e in entries if e["name"].startswith("claims:")]
    assert len(entries) == len(load_docs_impact_map()) + len(derived)
    assert any("AGENTS.md" in e["docs"] for e in derived)


def test_docs_impact_by_default_reads_the_claim_markers_too():
    """#1851: the step itself (no entries passed) lists the pages a changed claim test proves."""
    from ci.pre_merge import check_docs_impact

    cited = "tests/unit/agents/referee/test_only_the_ledger_writes_engines.py"
    level, message = check_docs_impact([cited], False)
    assert level == "error" and f"claims:{cited}" in message and "AGENTS.md" in message
