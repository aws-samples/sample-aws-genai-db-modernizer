#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Release step 6: record the public contract's and the rule catalog's shipped shape (#2472).

Run it once the release tag exists, on a branch from main::

    uv run python scripts/record_release.py v0.5.0

It runs the two recorders in order, each reading the tag itself (``git archive``), never
the working tree:

- ``scripts/contract_schema_snapshot.py --write-released <tag>`` writes
  ``tests/contract/assess_released_since_1_3.json`` and ``src/assessment/released_contract.json``;
- ``scripts/catalog_format_snapshot.py --write-released <tag>`` writes
  ``tests/contract/catalog_format_snapshot.json`` and the tag's catalog files under
  ``tests/contract/fixtures/catalog_releases/<tag>/``.

Then it checks that nothing else changed: no test file, and no other path. The tests read
the record, so this step needs no test edit. It refuses to start with uncommitted changes
to those record paths or to a test, and writes nothing for a tag that does not exist. It
never creates, moves or pushes a tag.
"""

from __future__ import annotations

import argparse
import subprocess  # nosec B404 -- git and this repository's scripts, fixed arguments, no shell
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RECORD_FILES = (
    "src/assessment/released_contract.json",
    "tests/contract/assess_released_since_1_3.json",
    "tests/contract/catalog_format_snapshot.json",
)
FIXTURES = "tests/contract/fixtures/catalog_releases"


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603 B607 -- fixed arguments, no shell
        ["git", *args], cwd=REPO, capture_output=True, text=True, check=False
    )


def changed_paths() -> list[str]:
    """Every path the working tree changes or adds, one per file."""
    out = _git("status", "--porcelain", "--untracked-files=all").stdout
    return sorted(line[3:].split(" -> ")[-1] for line in out.splitlines() if line.strip())


def is_record_path(path: str, tag: str) -> bool:
    return path in RECORD_FILES or path.startswith(f"{FIXTURES}/{tag}/")


def _run(script: str, tag: str) -> int:
    proc = subprocess.run(  # nosec B603 -- this repository's script, fixed arguments
        [sys.executable, str(REPO / "scripts" / script), "--write-released", tag],
        cwd=REPO,
        check=False,
    )
    return proc.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tag", help="the release tag, which must already exist")
    tag = parser.parse_args(argv).tag.strip()
    if not tag:
        parser.error("a release tag is required")
    if _git("rev-parse", "--verify", "--quiet", f"refs/tags/{tag}").returncode != 0:
        print(f"error: tag {tag!r} does not exist; run git fetch --tags", file=sys.stderr)
        return 2
    dirty = [p for p in changed_paths() if p.startswith("tests/") or is_record_path(p, tag)]
    if dirty:
        print(
            "error: uncommitted changes under tests/ or to the record files; commit or "
            "discard them first:\n  " + "\n  ".join(dirty),
            file=sys.stderr,
        )
        return 2
    before = set(changed_paths())
    for script in ("contract_schema_snapshot.py", "catalog_format_snapshot.py"):
        code = _run(script, tag)
        if code != 0:
            print(f"error: {script} --write-released {tag} exited {code}", file=sys.stderr)
            return code
    stray = [p for p in changed_paths() if p not in before and not is_record_path(p, tag)]
    if stray:
        print(
            "error: the record changed files outside it:\n  " + "\n  ".join(stray), file=sys.stderr
        )
        return 1
    written = [p for p in changed_paths() if is_record_path(p, tag)]
    print(f"recorded {tag}: {len(written)} files, no test file edited")
    for path in written:
        print(f"  {path}")
    print("next: run the contract and catalog tests, then open the pull request to main")
    return 0


if __name__ == "__main__":
    sys.exit(main())
