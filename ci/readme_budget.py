#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A length budget for ``ci/README.md`` (#1851).

Every sentence in ``ci/README.md`` is a claim a reviewer must check, and the page grew
to 630 lines with single table rows of 4,000 characters. The page says what each entry
point is for, how to run it and where its tests are; how a script works goes in that
script's docstring, next to the code it describes.

The budget, per section (a ``#``, ``##`` or ``###`` heading up to the next one; fenced
code counts toward the section, not toward a block):

* ``SECTION_CHARS`` characters in the section, and
* ``BLOCK_CHARS`` characters in any one block: a paragraph, a list item or a table row.

Claim markers (``<!-- claim: ... -->``, ``ci/claims.py``) are left out of both counts.

Sections that were over the budget when it landed have an allowance in
``ci/readme_budget.toml``, a ceiling at their size then. The check fails when a section
grows past its allowance, when an allowance is more than ``SLACK`` characters above the
section's size (lower it to the size printed), and when a section fits the default
budget and still has one (remove it). An allowance only goes down: the check also reads
the allowance file at the base ref and fails an allowance that is higher than the
base's, or that the base does not have, so a new or renamed heading gets the default
budget (``base_allowances`` says which copy is compared). The base ref is ``--base``,
else ``$README_BUDGET_BASE``, else ``origin/main``. A clone without that ref (a
single-branch CI clone, an upstream remote not named ``origin``) gets a warning and no
comparison, as ``ci/check_commits.sh`` skips there; ``README_BUDGET_REQUIRE_BASE=1``
(set in the GitHub ``lint`` job) makes a missing ref an error (exit 2). A base copy that
cannot be read or parsed, or a base whose allowance file is gone, is an error too.

Run: ``uv run python ci/readme_budget.py [--base REF]`` (``ci/lint.sh`` and the
``ci-readme-budget`` pre-commit hook do). Exit 1 on a problem.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess  # nosec B404 -- runs git with fixed argv
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
README = HERE / "README.md"
ALLOWANCE_FILE = HERE / "readme_budget.toml"
ALLOWANCE_PATH = "ci/readme_budget.toml"
SCRIPT_PATH = "ci/readme_budget.py"
BASE_ENV = "README_BUDGET_BASE"
REQUIRE_ENV = "README_BUDGET_REQUIRE_BASE"
SECTION_CHARS = 4000
BLOCK_CHARS = 1200
SLACK = 300

Allowances = dict[str, tuple[int, int]]


class BaseError(Exception):
    """The base could not be compared: an unreadable file, or a base that has the budget
    script but no allowance file. Exit 2."""


class BaseMissing(Exception):
    """The base ref does not resolve in this clone."""


def parse_allowances(text: str) -> Allowances:
    """``heading -> (section chars, largest block chars)`` from the allowance file."""
    table = tomllib.loads(text).get("allowance", [])
    return {a["heading"]: (int(a["chars"]), int(a["block"])) for a in table}


def load_allowances(path: Path = ALLOWANCE_FILE) -> Allowances:
    return parse_allowances(path.read_text(encoding="utf-8"))


def _git(cwd: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603 B607 -- fixed argv
        ["git", *argv], cwd=cwd, capture_output=True, text=True, check=False
    )


def _read(cwd: Path, rev: str, what: str) -> Allowances:
    shown = _git(cwd, "show", f"{rev}:{ALLOWANCE_PATH}")
    if shown.returncode != 0:
        raise BaseError(f"cannot read {ALLOWANCE_PATH} at {what}: {shown.stderr.strip()[-200:]}")
    try:
        return parse_allowances(shown.stdout)
    except (ValueError, KeyError, TypeError) as exc:
        raise BaseError(f"{ALLOWANCE_PATH} at {what} does not parse: {exc}") from exc


def base_allowances(base: str, cwd: Path = HERE.parent) -> tuple[Allowances | None, str]:
    """The allowances to compare with, and where they came from.

    * The base's copy of the allowance file when it has one (a failed read is a
      ``BaseError``, never a skip).
    * A base that has ``ci/readme_budget.py`` but no allowance file (it was moved or
      renamed) is a ``BaseError``.
    * A base that has neither predates the budget (the change that adds it, or a stale
      ref): the copy the file was first added with in HEAD's history, so an allowance
      still cannot rise above what landed. ``None`` only when no commit has the file yet.

    Raises ``BaseMissing`` when the ref does not resolve."""
    if _git(cwd, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}").returncode != 0:
        raise BaseMissing(base)
    if _git(cwd, "cat-file", "-e", f"{base}:{ALLOWANCE_PATH}").returncode == 0:
        return _read(cwd, base, base), f"{base}'s copy"
    if _git(cwd, "cat-file", "-e", f"{base}:{SCRIPT_PATH}").returncode == 0:
        raise BaseError(
            f"{base} has {SCRIPT_PATH} but no {ALLOWANCE_PATH}: the allowance file was moved "
            "or removed, so there is nothing to compare with"
        )
    added = _git(
        cwd, "log", "--diff-filter=A", "--format=%H", "--reverse", "HEAD", "--", ALLOWANCE_PATH
    ).stdout.split()
    if added:
        sha = added[0]
        where = f"the copy it was added with ({sha[:9]}), since {base} predates it"
        return _read(cwd, sha, sha[:9]), where
    return None, f"{base} predates {ALLOWANCE_PATH} and no commit has it yet"


def ratchet(current: Allowances, base: Allowances) -> list[str]:
    """Allowances that rose against the base, or that the base does not have."""
    errors = []
    for heading, (chars, block) in current.items():
        if heading not in base:
            errors.append(
                f"allowance for {heading!r} is not in the base: a new or renamed section gets "
                f"the default budget ({SECTION_CHARS} characters, {BLOCK_CHARS} a block)"
            )
            continue
        for what, now, was in (
            ("characters", chars, base[heading][0]),
            ("block", block, base[heading][1]),
        ):
            if now > was:
                errors.append(
                    f"allowance for {heading!r}: {what} {now} is above the base's {was}; "
                    "an allowance only goes down"
                )
    return errors


_HEADING = re.compile(r"^#{1,3} ")
_CLAIM = re.compile(r"[ \t]*<!--\s*claim:.*?-->", re.DOTALL)
_FENCE = re.compile(r"^[ \t]{0,3}(```|~~~)")
_BLOCK_START = re.compile(r"^[ \t]*(?:\||[-*+][ \t]|\d+\.[ \t])")


@dataclass
class Section:
    heading: str
    chars: int = 0
    largest_block: int = 0


def sections(text: str) -> list[Section]:
    """Each section's size and its largest block; text before the first heading is the
    section ``""``. Claim markers (``ci/claims.py``) do not count: they are not prose."""
    text = _CLAIM.sub("", text)
    out = [Section("")]
    block = 0
    fence: str | None = None

    def close() -> None:
        nonlocal block
        out[-1].largest_block = max(out[-1].largest_block, block)
        block = 0

    for line in text.replace("\r\n", "\n").split("\n"):
        m = _FENCE.match(line)
        if fence is not None or m:
            if fence is None:
                close()
                fence = m.group(1) if m else None
            elif m and m.group(1) == fence:
                fence = None
            out[-1].chars += len(line) + 1
            continue
        if _HEADING.match(line):
            close()
            out.append(Section(line.strip()))
            continue
        out[-1].chars += len(line) + 1
        if not line.strip() or _BLOCK_START.match(line):
            close()
        block += len(line.strip()) + (1 if block else 0)
    close()
    return [s for s in out if s.heading or s.chars > 1]


def check(found: list[Section], allowances: Allowances) -> list[str]:
    """Problems with the page's sections against the allowances in this tree."""
    errors: list[str] = []
    seen = {s.heading for s in found}
    for heading in allowances:
        if heading not in seen:
            errors.append(f"allowance for {heading!r}: no such section; remove it")
    for s in found:
        name = s.heading or "(before the first heading)"
        limit = allowances.get(s.heading)
        if limit is None:
            if s.chars > SECTION_CHARS:
                errors.append(
                    f"{name}: {s.chars} characters, over the {SECTION_CHARS} budget; move "
                    "how-it-works detail into the script's docstring"
                )
            if s.largest_block > BLOCK_CHARS:
                errors.append(
                    f"{name}: a block (paragraph, list item or table row) of "
                    f"{s.largest_block} characters, over the {BLOCK_CHARS} budget"
                )
            continue
        sec_limit, block_limit = limit
        if s.chars <= SECTION_CHARS and s.largest_block <= BLOCK_CHARS:
            errors.append(f"{name}: within the default budget now; remove its allowance")
            continue
        for what, size, ceiling, default in (
            ("characters", s.chars, sec_limit, SECTION_CHARS),
            ("largest block", s.largest_block, block_limit, BLOCK_CHARS),
        ):
            if size > ceiling:
                errors.append(
                    f"{name}: {what} {size}, over its allowance of {ceiling}; trim it, "
                    "the allowance only goes down"
                )
            elif ceiling - max(size, default) > SLACK:
                errors.append(
                    f"{name}: {what} {size}, allowance {ceiling}; lower the allowance to "
                    f"{max(size, default)}"
                )
    return errors


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0] if __doc__ else None)
    ap.add_argument(
        "--base",
        help=f"ref whose allowances are the ceiling (default: ${BASE_ENV}, else origin/main)",
    )
    ap.add_argument("--page", default=str(README), help=argparse.SUPPRESS)
    ap.add_argument("--allowances", default=str(ALLOWANCE_FILE), help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    base = args.base or os.environ.get(BASE_ENV) or "origin/main"
    found = sections(Path(args.page).read_text(encoding="utf-8"))
    allowances = load_allowances(Path(args.allowances))
    errors = check(found, allowances)
    try:
        compared, where = base_allowances(base, Path(args.allowances).resolve().parents[1])
    except BaseMissing:
        if os.environ.get(REQUIRE_ENV) == "1":
            print(
                f"ci/README.md budget: error: the base ref {base!r} does not resolve and "
                f"{REQUIRE_ENV}=1 requires the comparison; fetch it or set ${BASE_ENV}",
                file=sys.stderr,
            )
            return 2
        print(
            f"ci/README.md budget: warning: the base ref {base!r} does not resolve in this "
            f"clone, so the allowances are not compared with it (fetch it, or set ${BASE_ENV}"
            " or --base)"
        )
        compared, where = None, f"{base} is not in this clone"
    except BaseError as exc:
        print(f"ci/README.md budget: error: {exc}", file=sys.stderr)
        return 2
    if compared is not None:
        errors += ratchet(allowances, compared)
        note = f"; allowances compared with {where}"
    else:
        note = f"; allowances not compared: {where}"
    for err in errors:
        print(f"ci/README.md budget: {err}")
    if errors:
        return 1
    total = sum(s.chars for s in found)
    over = sum(1 for s in found if s.heading in allowances)
    print(
        f"ci/README.md budget: {len(found)} sections, {total} characters, "
        f"{over} on an allowance{note}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
