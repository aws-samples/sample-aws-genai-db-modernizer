#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Fail a pull request that moves the ``assess()`` contract MAJOR without approval (#1648).

The released-contract guard (``tests/contract/test_assess_released_contract.py``) lets
a breaking change through when ``CONTRACT_VERSION``'s MAJOR rises, so a MAJOR move is
itself the thing to approve. This check compares the contract MAJOR of the request
and of the result with the base revision's, read from every source that states it:
``CONTRACT_VERSION`` in ``src/assessment/models.py`` and the ``x-contract-version`` of
the highest ``docs/contracts/schemas/assess_<request|result>.v<N>.schema.json`` (a
MAJOR adds the next file). A MINOR move, or none, passes without a GitHub call.

A MAJOR move passes only while the pull request carries the ``Contract Major Approved``
label, which a maintainer adds to approve it (only maintainers can change labels). The
label does not block merging, so it does not clash with the ``maintainer-decision-gate``
check. Only the labels at the time of the run count: a label removed earlier, or any
other history, never does. Labels are read from the API, not the event payload, so a
rerun after a label change, and a merge group, see the current labels.

In the merge queue (``--merge-group-ref``), the pull request comes from the queue's
ref; when the group moved the MAJOR, only the entry's own commits count (the queue
rebases them on the entries ahead of it, so they are the last ``commits`` ones), so
an earlier entry's approved move does not fail a later entry. This relies on the
queue's REBASE merge method: a squash queue makes ``HEAD~<commits>`` wrong. When that
revision is not at or after the group's base, the check stops (exit 2) rather than guess.

Standard library only (plus ``gh`` on the runner for the API)::

    python3 ci/contract_major_gate.py --base <rev> --head <rev> [--pr N | --merge-group-ref REF]

Exit 0 passes, 1 fails (an unapproved MAJOR move), 2 when the check could not run.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess  # nosec B404 -- runs git and gh with fixed argv
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

LABEL = "Contract Major Approved"
MESSAGE = (
    "moving the assess() contract MAJOR breaks callers; it needs a maintainer's approval "
    f"(label {LABEL})"
)
MODELS = "src/assessment/models.py"
CONSTANT = "CONTRACT_VERSION"
SCHEMAS = "docs/contracts/schemas"
SCHEMA_FILE = re.compile(r"^assess_(request|result)\.v(\d+)\.schema\.json$")
SURFACES = ("request", "result")
QUEUE_REF = re.compile(r"(?:^|/)gh-readonly-queue/[^/].*/pr-(\d+)-[0-9a-f]+$")


class GateError(Exception):
    """The check could not read what it compares."""


class Tree(Protocol):
    """One revision's files."""

    def read(self, path: str) -> str | None: ...

    def names(self, directory: str) -> list[str]: ...


@dataclass(frozen=True)
class DictTree:
    """A revision held in memory (for tests)."""

    files: dict[str, str]

    def read(self, path: str) -> str | None:
        return self.files.get(path)

    def names(self, directory: str) -> list[str]:
        prefix = directory.rstrip("/") + "/"
        return sorted(
            p[len(prefix) :]
            for p in self.files
            if p.startswith(prefix) and "/" not in p[len(prefix) :]
        )


@dataclass(frozen=True)
class WorkTree:
    """The working tree's files."""

    root: Path

    def read(self, path: str) -> str | None:
        file = self.root / path
        return file.read_text(encoding="utf-8") if file.is_file() else None

    def names(self, directory: str) -> list[str]:
        folder = self.root / directory
        return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


@dataclass(frozen=True)
class GitTree:
    """A git revision's files."""

    rev: str
    cwd: Path | None = None

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # nosec B603 B607 -- fixed argv
            ["git", *args], cwd=self.cwd, capture_output=True, text=True, check=False
        )

    def _check_rev(self) -> None:
        if self._git("rev-parse", "--verify", "--quiet", f"{self.rev}^{{commit}}").returncode:
            raise GateError(f"git: no commit {self.rev!r} (is the checkout deep enough?)")

    def read(self, path: str) -> str | None:
        self._check_rev()
        shown = self._git("show", f"{self.rev}:{path}")
        return shown.stdout if shown.returncode == 0 else None

    def names(self, directory: str) -> list[str]:
        self._check_rev()
        listed = self._git("ls-tree", "--name-only", f"{self.rev}", f"{directory.rstrip('/')}/")
        return sorted(Path(line).name for line in listed.stdout.splitlines() if line)


@dataclass(frozen=True)
class Move:
    surface: str
    source: str
    base: str
    head: str


def _major(version: str, where: str) -> int:
    match = re.fullmatch(r"(\d+)\.\d+(?:\.\d+)?", version)
    if not match:
        raise GateError(f"{where}: {version!r} is not a MAJOR.MINOR version")
    return int(match.group(1))


def _constant(text: str) -> str | None:
    try:
        module = ast.parse(text)
    except SyntaxError as exc:
        raise GateError(f"{MODELS}: {exc}") from exc
    for node in module.body:
        target: ast.expr | None = node.target if isinstance(node, ast.AnnAssign) else None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        value = getattr(node, "value", None)
        if (
            isinstance(target, ast.Name)
            and target.id == CONSTANT
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            return value.value
    return None


def contract_versions(tree: Tree) -> dict[tuple[str, str], str]:
    """``(surface, source) -> version`` for every source a revision has."""
    versions: dict[tuple[str, str], str] = {}
    models = tree.read(MODELS)
    constant = _constant(models) if models is not None else None
    newest: dict[str, tuple[int, str]] = {}
    for name in tree.names(SCHEMAS):
        match = SCHEMA_FILE.match(name)
        if match and int(match.group(2)) > newest.get(match.group(1), (-1, ""))[0]:
            newest[match.group(1)] = (int(match.group(2)), name)
    for surface in SURFACES:
        if constant is not None:
            versions[(surface, CONSTANT)] = constant
        if surface in newest:
            name = newest[surface][1]
            text = tree.read(f"{SCHEMAS}/{name}") or ""
            try:
                version = json.loads(text).get("x-contract-version")
            except (ValueError, AttributeError) as exc:
                raise GateError(f"{name}: not a JSON object ({exc})") from exc
            if isinstance(version, str):
                versions[(surface, name)] = version
    for (surface, source), version in versions.items():
        _major(version, f"{surface} {source}")
    return versions


def _source_kind(source: str) -> str:
    return SCHEMA_FILE.sub(r"assess_\1 schema", source)


def major_moves(base: Tree, head: Tree) -> list[Move]:
    """Each source whose MAJOR differs between the two revisions (a schema file
    renamed for a MAJOR is matched by surface)."""
    before = {(s, _source_kind(src)): v for (s, src), v in contract_versions(base).items()}
    moves = []
    for (surface, source), version in contract_versions(head).items():
        old = before.get((surface, _source_kind(source)))
        if old is not None and _major(old, source) != _major(version, source):
            moves.append(Move(surface, source, old, version))
    return moves


def pr_from_merge_group_ref(ref: str) -> int | None:
    match = QUEUE_REF.search(ref or "")
    return int(match.group(1)) if match else None


def decision(labels: list[str]) -> str | None:
    """The approval the pull request's current labels carry, or None."""
    if LABEL in labels:
        return f"the '{LABEL}' label is on the pull request"
    return None


class GitHub(Protocol):
    def labels(self, repo: str, pr: int) -> list[str]: ...

    def commit_count(self, repo: str, pr: int) -> int: ...


class GhCli:
    """GitHub's REST API through ``gh`` (``GH_TOKEN`` in the environment)."""

    @staticmethod
    def _api(*args: str) -> str:
        done = subprocess.run(  # nosec B603 B607 -- fixed argv
            ["gh", "api", *args], capture_output=True, text=True, check=False
        )
        if done.returncode:
            raise GateError(f"gh api {' '.join(args)}: {done.stderr.strip()}")
        return done.stdout

    def labels(self, repo: str, pr: int) -> list[str]:
        out = self._api(f"repos/{repo}/issues/{pr}", "--jq", ".labels[].name")
        return [line for line in out.splitlines() if line]

    def commit_count(self, repo: str, pr: int) -> int:
        return int(self._api(f"repos/{repo}/pulls/{pr}", "--jq", ".commits").strip())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", required=True, help="the base revision")
    parser.add_argument("--head", required=True, help="the revision under test")
    parser.add_argument("--pr", type=int, help="the pull request number")
    parser.add_argument("--merge-group-ref", default="", help="a merge group's head ref")
    parser.add_argument(
        "--repo", default=os.environ.get("GITHUB_REPOSITORY", ""), help="owner/name"
    )
    return parser


def git_is_ancestor(ancestor: str, rev: str) -> bool:
    """Whether ``ancestor`` is ``rev`` or one of its ancestors."""
    done = subprocess.run(  # nosec B603 B607 -- fixed argv
        ["git", "merge-base", "--is-ancestor", ancestor, rev], capture_output=True, check=False
    )
    if done.returncode not in (0, 1):
        raise GateError(f"git merge-base --is-ancestor {ancestor} {rev}: {done.stderr!r}")
    return done.returncode == 0


def main(
    argv: list[str] | None = None,
    *,
    tree_at: Callable[[str], Tree] = GitTree,
    github: GitHub | None = None,
    is_ancestor: Callable[[str, str], bool] = git_is_ancestor,
) -> int:
    args = _parser().parse_args(argv)
    client: GitHub = github or GhCli()
    try:
        moves = major_moves(tree_at(args.base), tree_at(args.head))
        if not moves:
            print("The assess() contract MAJOR did not move (request and result).")
            return 0
        pr = args.pr or pr_from_merge_group_ref(args.merge_group_ref)
        if args.merge_group_ref and pr is not None:
            # The queue merges by REBASE: the entry's commits are the last N (#1648).
            own_base = f"{args.head}~{client.commit_count(args.repo, pr)}"
            if not is_ancestor(args.base, own_base):
                raise GateError(
                    f"{own_base} is not at or after the merge group's base {args.base}: "
                    "the entry's own commits are not the last ones (does the merge queue "
                    "still merge by REBASE?)"
                )
            moves = major_moves(tree_at(own_base), tree_at(args.head))
            if not moves:
                print(
                    f"The assess() contract MAJOR moved in this merge group, not in pull "
                    f"request #{pr}'s own commits: an earlier entry's run checked it."
                )
                return 0
        for move in moves:
            print(f"{move.surface} ({move.source}): {move.base} -> {move.head}")
        recorded = decision(client.labels(args.repo, pr)) if pr is not None else None
    except GateError as exc:
        print(f"::error::contract MAJOR gate could not run: {exc}")
        return 2
    if recorded:
        print(f"MAJOR move approved: {recorded}.")
        return 0
    if pr is None:
        print("No pull request to read labels from.")
    print(f"::error::{MESSAGE}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
