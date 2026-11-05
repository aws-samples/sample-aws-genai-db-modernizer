#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The Python files ruff, black and isort check, for ``ci/lint.sh`` and the hooks alike (#1844).

The pre-commit hooks of those names pick tracked Python files with their ``files`` and
``exclude`` patterns (and the config's top-level ``exclude``). This prints the tracked
``.py`` and ``.pyi`` files those same patterns select, one per line, and ``ci/lint.sh``
runs the three tools on them, so both check the same files on any checkout, whatever
extra files it carries. Exit 2 when the three hooks select differently or nothing matches.

    uv run python ci/python_files.py [--repo DIR]
"""

from __future__ import annotations

import argparse
import re
import subprocess  # nosec B404 -- runs git with fixed argv
import sys
from pathlib import Path

import yaml

HOOK_IDS = ("black", "isort", "ruff")
PYTHON_GLOBS = ("*.py", "*.pyi")
REPO = Path(__file__).resolve().parents[1]


class SelectionError(Exception):
    """The hooks' patterns can't give one Python file set."""


def hook_patterns(config_path: Path) -> tuple[str, tuple[str, ...]]:
    """The ``files`` pattern and the exclude patterns the three hooks share."""
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    top_exclude = config.get("exclude")
    found: dict[str, tuple[str, tuple[str, ...]]] = {}
    for repo in config["repos"]:
        for hook in repo["hooks"]:
            if hook["id"] in HOOK_IDS:
                excludes = tuple(e for e in (hook.get("exclude"), top_exclude) if e)
                found[hook["id"]] = (hook.get("files", ""), excludes)
    missing = [h for h in HOOK_IDS if h not in found]
    if missing:
        raise SelectionError(f"no pre-commit hook for {', '.join(missing)}")
    if len(set(found.values())) != 1:
        raise SelectionError(f"the {', '.join(HOOK_IDS)} hooks select different files: {found}")
    return found[HOOK_IDS[0]]


def select(files: list[str], include: str, excludes: tuple[str, ...]) -> list[str]:
    """The files pre-commit would pass a hook with these patterns (it uses ``re.search``)."""
    inc = re.compile(include)
    exc = [re.compile(e) for e in excludes]
    return [f for f in files if inc.search(f) and not any(e.search(f) for e in exc)]


def tracked_python(repo: Path) -> list[str]:
    out = subprocess.run(  # nosec B603 B607 -- fixed argv
        ["git", "ls-files", "-z", "--", *PYTHON_GLOBS],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return sorted(f for f in out.split("\0") if f)


def python_files(repo: Path = REPO) -> list[str]:
    include, excludes = hook_patterns(repo / ".pre-commit-config.yaml")
    return select(tracked_python(repo), include, excludes)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--repo", type=Path, default=REPO, help="repository root (default: this one)"
    )
    args = parser.parse_args(argv)
    try:
        files = python_files(args.repo)
    except SelectionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not files:
        print("error: no tracked Python files match the hooks' patterns", file=sys.stderr)
        return 2
    sys.stdout.write("".join(f"{f}\n" for f in files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
