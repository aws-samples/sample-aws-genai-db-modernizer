#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Fail a pull request that moves the ``assess()`` contract version without a maintainer's
approval (#1648, #2176).

The released-contract guard (``tests/contract/test_assess_released_contract.py``) lets a
change through when ``CONTRACT_VERSION`` moves, so the move is itself the thing to approve.
This check compares the version of the request and of the result with the base revision's,
read from every source that states it: ``CONTRACT_VERSION`` in ``src/assessment/models.py``
and the ``x-contract-version`` of the highest ``docs/contracts/schemas/assess_<request|
result>.v<N>.schema.json`` (a MAJOR adds the next file). A MAJOR needs the
``contract-major`` label (``Contract Major Approved``, its old name, still counts); a MINOR
needs ``contract-minor``; a change with both needs both, and ``contract-minor`` never
approves a MAJOR. A PATCH, or no move, needs nothing, and the check then makes no GitHub
call. Internal contracts (``src/contracts/``) need no label: their bumps are pending files
the release resolves (ADR-008 Decision 8).

A label counts only when it is on the pull request now and the actor of its latest
``labeled`` event has the ``maintain`` or ``admin`` role on the repository (the issue's
events and ``collaborators/<user>/permission`` from the API, never the event payload),
so a label removed after approval drops it, and one a non-maintainer applied fails with
who applied it. Agents never apply either label (AGENTS.md).

In the merge queue (``--merge-group-ref``), the pull request comes from the queue's
ref; when the group moved a version, only the entry's own commits count (the queue
rebases them on the entries ahead of it, so they are the last ``commits`` ones), so
an earlier entry's approved move does not fail a later entry. This relies on the
queue's REBASE merge method: a squash queue makes ``HEAD~<commits>`` wrong. When that
revision is not at or after the group's base, the check stops (exit 2) rather than guess.

Standard library only (plus ``gh`` on the runner for the API)::

    python3 ci/contract_major_gate.py --base <rev> --head <rev> [--pr N | --merge-group-ref REF]

Exit 0 passes, 1 fails (an unapproved move), 2 when the check could not run. The
``contract-major-gate`` required check (``.github/workflows/contract-major-gate.yml``)
runs it with the runner's ``python3`` on ``pull_request`` opened, reopened, synchronize,
labeled and unlabeled, and on ``merge_group``; on a pull request the base is ``HEAD^1``
of the merge checkout. Locally: ``uv run python ci/contract_major_gate.py --base
origin/main --head HEAD``. Tests: ``tests/unit/ci/test_contract_major_gate.py``.
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

LABEL = "contract-major"
LEGACY_LABEL = "Contract Major Approved"
MINOR_LABEL = "contract-minor"
LABELS = {"major": (LABEL, LEGACY_LABEL), "minor": (MINOR_LABEL,)}
MAINTAINER_ROLES = ("admin", "maintain")
MESSAGE = (
    "moving the assess() contract MAJOR breaks callers; it needs a maintainer's approval "
    f"(label {LABEL})"
)
MINOR_MESSAGE = (
    f"an assess() contract MINOR bump needs a maintainer's approval (label {MINOR_LABEL})"
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
    kind: str = "major"  # "major" or "minor"


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


def _bump_kind(old: str, new: str, where: str) -> str | None:
    """``major``, ``minor``, or None for a PATCH or no move."""
    _major(old, where)
    _major(new, where)
    a, b = old.split("."), new.split(".")
    if a[0] != b[0]:
        return "major"
    if a[1] != b[1]:
        return "minor"
    return None


def all_moves(base: Tree, head: Tree) -> list[Move]:
    return major_moves(base, head)


def _source_kind(source: str) -> str:
    return SCHEMA_FILE.sub(r"assess_\1 schema", source)


def major_moves(base: Tree, head: Tree) -> list[Move]:
    """Each ``assess()`` source whose MAJOR or MINOR differs between the two revisions (a
    schema file renamed for a MAJOR is matched by surface)."""
    before = {(s, _source_kind(src)): v for (s, src), v in contract_versions(base).items()}
    moves = []
    for (surface, source), version in contract_versions(head).items():
        old = before.get((surface, _source_kind(source)))
        kind = _bump_kind(old, version, source) if old is not None else None
        if kind:
            moves.append(Move(surface, source, old or "", version, kind))
    return moves


def pr_from_merge_group_ref(ref: str) -> int | None:
    match = QUEUE_REF.search(ref or "")
    return int(match.group(1)) if match else None


Event = tuple[str, str, str, str]  # (event, label, actor, ISO 8601 time), oldest first


def last_labeler(events: list[Event], label: str) -> tuple[str, str] | None:
    """``(actor, time)`` of ``label``'s latest ``labeled`` event, or None."""
    found = None
    for event, name, who, when in events:
        if name == label and event == "labeled":
            found = (who, when)
    return found


def approval_problems(
    kinds: set[str],
    labels: list[str],
    events: list[Event],
    role: Callable[[str], str],
    head_changed: str = "",
) -> tuple[dict[str, str], list[str]]:
    """``({kind: approval}, problems)`` for the bump kinds a change needs approved.

    A label approves only while it is on the pull request, applied by a maintainer, and
    after ``head_changed`` (the time of the pull request's latest push): an approval
    resets on every new push (maintainer decision on #2183). Times are GitHub's ISO 8601
    UTC strings, which compare in order."""
    approvals: dict[str, str] = {}
    problems: list[str] = []
    for kind in sorted(kinds, reverse=True):  # major first
        found = None
        refused = []
        for label in LABELS[kind]:
            if label not in labels:
                continue
            last = last_labeler(events, label)
            actor = last[0] if last else None
            if last is not None and last[1] <= head_changed:
                refused.append(
                    f"the '{label}' label was applied at {last[1]}, before the pull request's "
                    f"latest push at {head_changed}; an approval resets on every push, so a "
                    "maintainer re-applies it to approve the new head"
                )
                continue
            if actor is not None and role(actor) in MAINTAINER_ROLES:
                found = f"the '{label}' label, applied by maintainer {actor}"
                break
            refused.append(
                f"the '{label}' label was applied by {actor or 'an unknown actor'}, who is not "
                "a maintainer (maintain or admin role); a maintainer must apply it"
            )
        if found:
            approvals[kind] = found
        else:
            problems.extend(refused)
    return approvals, problems


class GitHub(Protocol):
    def labels(self, repo: str, pr: int) -> list[str]: ...

    def commit_count(self, repo: str, pr: int) -> int: ...

    def label_events(self, repo: str, pr: int) -> list[Event]: ...

    def head_changed_at(self, repo: str, pr: int) -> str: ...

    def role(self, repo: str, user: str) -> str: ...


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

    def __init__(self) -> None:
        self._timeline: dict[tuple[str, int], list[Event]] = {}

    def timeline(self, repo: str, pr: int) -> list[Event]:
        """The pull request's label and head events, oldest first, read once: a
        ``committed`` event's time is its committer date, the others' their creation."""
        if (repo, pr) not in self._timeline:
            out = self._api(
                "--paginate",
                f"repos/{repo}/issues/{pr}/timeline",
                "--jq",
                '.[] | select(.event == "labeled" or .event == "unlabeled" or .event == '
                '"committed" or .event == "head_ref_force_pushed") | [.event, '
                '(.label.name // ""), (.actor.login // ""), (.created_at // .committer.date '
                '// "")] | @tsv',
            )
            rows = [line.split("\t") for line in out.splitlines() if line]
            self._timeline[(repo, pr)] = [(r[0], r[1], r[2], r[3]) for r in rows if len(r) == 4]
        return self._timeline[(repo, pr)]

    def label_events(self, repo: str, pr: int) -> list[Event]:
        return [e for e in self.timeline(repo, pr) if e[0] in ("labeled", "unlabeled")]

    def head_changed_at(self, repo: str, pr: int) -> str:
        """The time of the pull request's latest push: the newest ``committed`` or
        ``head_ref_force_pushed`` event. With none (or no time), the check stops."""
        times = [
            e[3]
            for e in self.timeline(repo, pr)
            if e[0] in ("committed", "head_ref_force_pushed") and e[3]
        ]
        if not times:
            raise GateError("could not determine when the pull request's head last changed")
        return max(times)

    def role(self, repo: str, user: str) -> str:
        try:
            out = self._api(f"repos/{repo}/collaborators/{user}/permission", "--jq", ".role_name")
        except GateError as exc:
            if "404" in str(exc):
                return "none"  # not a collaborator: never a maintainer
            # 403, a rate limit, a 5xx: the role is unknown, so the check stops (exit 2).
            raise GateError(f"could not verify the labeler's role (API error: {exc})") from exc
        return out.strip()


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
        moves = all_moves(tree_at(args.base), tree_at(args.head))
        if not moves:
            print("The assess() contract MAJOR and MINOR did not move (request and result).")
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
            moves = all_moves(tree_at(own_base), tree_at(args.head))
            if not moves:
                print(
                    f"The assess() contract version moved in this merge group, not in pull request "
                    f"#{pr}'s own commits: an earlier entry's run checked it."
                )
                return 0
        for move in moves:
            print(f"{move.kind} {move.surface} ({move.source}): {move.base} -> {move.head}")
        kinds = {move.kind for move in moves}
        approvals: dict[str, str] = {}
        refused: list[str] = []
        if pr is not None:
            labels = client.labels(args.repo, pr)
            wanted = {label for kind in kinds for label in LABELS[kind]} & set(labels)
            events = client.label_events(args.repo, pr) if wanted else []
            head_changed = client.head_changed_at(args.repo, pr) if wanted else ""
            roles: dict[str, str] = {}

            def role(user: str) -> str:
                if user not in roles:
                    roles[user] = client.role(args.repo, user)
                return roles[user]

            approvals, refused = approval_problems(kinds, labels, events, role, head_changed)
    except GateError as exc:
        print(f"::error::contract version gate could not run: {exc}")
        return 2
    for approval in approvals.values():
        print(f"approved: {approval}.")
    if set(approvals) == kinds:
        return 0
    if pr is None:
        print("No pull request to read labels from.")
    for problem in refused:
        print(f"::error::{problem}")
    if "major" in kinds and "major" not in approvals:
        print(f"::error::{MESSAGE}")
    if "minor" in kinds and "minor" not in approvals:
        print(f"::error::{MINOR_MESSAGE}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
