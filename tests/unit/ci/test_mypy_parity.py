# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ci/lint.sh and the pre-commit mypy hook run one mypy check (#2245).

The hook used to run in its own environment (pydantic only, ``--ignore-missing-imports``)
over the staged files, while ``ci/lint.sh`` ran ``mypy src/`` in the project environment,
so a type error in ``tests/`` or behind an untyped import passed lint and ``pre_merge`` and
failed at commit time. Both now run plain ``uv run mypy`` and read the paths and the
per-tree rules from ``[tool.mypy]``; these tests keep it that way.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[3]
MYPY_COMMAND = "uv run mypy"


def _mypy_hooks() -> list[dict]:
    config = yaml.safe_load((REPO / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    hooks = [h for r in config["repos"] for h in r["hooks"]]
    return [h for h in hooks if "mypy" in h["id"] or "mypy" in h.get("entry", "")]


def _lint_mypy_lines() -> list[str]:
    text = (REPO / "ci" / "lint.sh").read_text(encoding="utf-8")
    found = [ln for ln in text.splitlines() if re.search(r"\buv run mypy\b", ln)]
    return [ln.split(";", 1)[1].strip() for ln in found]


def _config() -> dict:
    config = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    return dict(config["tool"]["mypy"])


def test_the_hook_runs_the_project_mypy_with_no_arguments_of_its_own() -> None:
    hooks = _mypy_hooks()
    assert len(hooks) == 1, "one pre-commit hook runs mypy"
    hook = hooks[0]
    assert hook["entry"] == MYPY_COMMAND, "the hook runs the project's mypy, not its own copy"
    assert hook["language"] == "system", "the project environment, not a hook-private one"
    assert "args" not in hook, "flags belong in [tool.mypy], where lint.sh reads them too"
    assert hook["pass_filenames"] is False, "paths come from [tool.mypy] `files`"


def test_lint_sh_runs_the_same_command_with_no_arguments_of_its_own() -> None:
    assert _lint_mypy_lines() == [MYPY_COMMAND]


def test_the_configured_files_are_every_tracked_python_tree_but_contrib() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "*.py"], cwd=REPO, check=True, capture_output=True, text=True
    ).stdout.split()
    trees = {p.split("/", 1)[0] for p in tracked if "/" in p} - {"contrib"}
    assert not [p for p in tracked if "/" not in p], "a top-level .py file is outside `files`"
    assert set(_config()["files"]) == trees
    assert _config()["exclude"] == ["^contrib/"]


def test_src_keeps_strict_optional_and_the_other_trees_that_have_a_package_do_not() -> None:
    relaxed = {
        m.split(".")[0]
        for o in _config()["overrides"]
        if o.get("strict_optional") is False
        for m in o["module"]
    }
    assert "src" not in relaxed
    assert relaxed == {"tests", "scripts", "ci", "benchmarks"}
