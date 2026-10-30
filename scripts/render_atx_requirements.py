#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Render the AWS Transform image's pinned requirements from ``uv.lock`` (#1725).

``src/atx_orchestrator/requirements.in`` lists the image's direct dependencies, with the
lower bounds the code needs. ``requirements.txt`` is generated from it: every direct
dependency and everything it pulls in, pinned (``name==version``) to the version in
``uv.lock``. The image therefore installs what the tests ran against, and a scanner
reads the versions that ship.

The set is the dependency closure of ``requirements.in`` inside the lock, so the image
does not gain the API, load-test or other packages the project installs. Lines keep their
environment markers (``uv export`` writes them).

The AWS Transform SDK packages are not in the lock; they are pinned by hand in
``requirements-sdk.txt``.

``requirements.txt`` is a generated file: never edit it by hand. ``--check`` (run in
``ci/lint.sh``) fails when it differs from a fresh render, so a ``uv.lock`` or
``requirements.in`` change without a regeneration is caught.

Usage:
    uv run python scripts/render_atx_requirements.py --write    # write requirements.txt
    uv run python scripts/render_atx_requirements.py --check    # exit 1 if it is stale
"""

from __future__ import annotations

import argparse
import re
import subprocess  # nosec B404 -- fixed argv only
import sys
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

REPO_ROOT = Path(__file__).resolve().parent.parent
IMAGE_DIR = REPO_ROOT / "src" / "atx_orchestrator"
SOURCE = IMAGE_DIR / "requirements.in"
OUTPUT = IMAGE_DIR / "requirements.txt"
LOCK = REPO_ROOT / "uv.lock"

HEADER = """\
# GENERATED from uv.lock by scripts/render_atx_requirements.py: do not edit by hand.
# To change the image's dependencies, edit requirements.in and run
#   uv run python scripts/render_atx_requirements.py --write
# Every package is pinned to the version in uv.lock (#1725). The AWS Transform SDK
# packages are pinned in requirements-sdk.txt: they are not in the lock.
"""


def direct_requirements(text: str) -> list[Requirement]:
    """The requirements in ``requirements.in``, comments and blank lines skipped."""
    lines = (line.split(" #")[0].strip() for line in text.splitlines())
    return [Requirement(line) for line in lines if line and not line.startswith("#")]


def closure(lock: dict, roots: list[Requirement]) -> set[str]:
    """Canonical names of ``roots`` and everything they depend on, per the lock.

    Markers are ignored (a package needed only on one platform is kept, with its marker
    from ``uv export``), and an extra such as ``pymongo[aws]`` adds that extra's packages.
    """
    packages = {canonicalize_name(p["name"]): p for p in lock["package"]}
    seen: set[str] = set()
    todo = [(canonicalize_name(r.name), tuple(r.extras)) for r in roots]
    while todo:
        name, extras = todo.pop()
        if name not in packages:
            raise SystemExit(f"{name} is in requirements.in but not in uv.lock")
        package = packages[name]
        first_visit = name not in seen
        seen.add(name)
        deps = list(package.get("dependencies", [])) if first_visit else []
        optional = package.get("optional-dependencies", {})
        for extra in extras:
            deps += optional.get(extra, [])
        for dep in deps:
            todo.append((canonicalize_name(dep["name"]), tuple(dep.get("extra", []))))
    return seen


def render() -> str:
    """The full text of ``requirements.txt``."""
    lock = tomllib.loads(LOCK.read_text(encoding="utf-8"))
    needed = closure(lock, direct_requirements(SOURCE.read_text(encoding="utf-8")))
    exported = subprocess.run(  # nosec B603 -- fixed argv
        ["uv", "export", "--frozen", "--no-dev", "--no-hashes", "--no-emit-project"]
        + ["--no-annotate", "--no-header"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    lines = []
    for line in exported.splitlines():
        match = re.match(r"([A-Za-z0-9._-]+)==", line)
        if match and canonicalize_name(match[1]) in needed:
            lines.append(line)
    found = {canonicalize_name(re.match(r"[A-Za-z0-9._-]+", ln)[0]) for ln in lines}
    missing = needed - found
    if missing:
        raise SystemExit(f"uv export has no pin for: {', '.join(sorted(missing))}")
    return HEADER + "\n".join(sorted(lines, key=str.lower)) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="write requirements.txt")
    mode.add_argument("--check", action="store_true", help="exit 1 if it is stale")
    args = parser.parse_args(argv)
    text = render()
    if args.write:
        OUTPUT.write_text(text, encoding="utf-8")
        print(f"wrote {OUTPUT.relative_to(REPO_ROOT)}")
        return 0
    if not OUTPUT.exists() or OUTPUT.read_text(encoding="utf-8") != text:
        print(
            "src/atx_orchestrator/requirements.txt is stale against uv.lock or requirements.in: "
            "run `uv run python scripts/render_atx_requirements.py --write`",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
