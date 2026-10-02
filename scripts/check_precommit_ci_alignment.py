#!/usr/bin/env python3
"""Fail if pre-commit linter versions drift from the ones CI installs.

Pre-commit pins each linter to a frozen git tag (`rev:` in
``.pre-commit-config.yaml``). CI installs linters from ``uv.lock`` via
``uv sync --locked``. When someone bumps dependencies, the lockfile moves but the
pre-commit `rev`s do not — so the hook and CI silently run different tool
versions, and a green hook stops meaning a green CI. That exact drift (isort 5 vs
9, mypy 1.8 vs 2.3, ...) shipped once already.

This compares the `rev` of each linter's pre-commit repo against the version
installed in the current (locked) environment and exits non-zero on any
mismatch, with the one-line fix. Run it in CI and as a pre-commit hook so the
drift cannot land.

Only the linters that BOTH systems run are checked: black, isort, ruff, mypy,
bandit. General-purpose hooks (whitespace, secrets, etc.) have no CI counterpart
and are ignored.
"""

from __future__ import annotations

import importlib.metadata as md
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / ".pre-commit-config.yaml"

# pre-commit repo URL fragment -> the PyPI distribution name it installs.
_REPO_TO_DIST = {
    "psf/black": "black",
    "pycqa/isort": "isort",
    "astral-sh/ruff-pre-commit": "ruff",
    "pre-commit/mirrors-mypy": "mypy",
    "PyCQA/bandit": "bandit",
}


def _installed(dist: str) -> str:
    return md.version(dist)


def _parse_config_revs() -> dict[str, str]:
    """Map dist name -> rev tag (leading 'v' stripped) from the pre-commit config."""
    text = CONFIG.read_text()
    revs: dict[str, str] = {}
    # Each repo block: "- repo: https://github.com/<repo>\n    rev: <tag>"
    for m in re.finditer(r"-\s*repo:\s*https://github\.com/(\S+)\s*\n\s*rev:\s*(\S+)", text):
        repo, tag = m.group(1), m.group(2).strip().strip("'\"")
        dist = _REPO_TO_DIST.get(repo)
        if dist:
            revs[dist] = tag.lstrip("v")
    return revs


def main() -> int:
    config_revs = _parse_config_revs()
    missing = sorted(set(_REPO_TO_DIST.values()) - set(config_revs))
    if missing:
        print(f"ERROR: linters not found in {CONFIG.name}: {', '.join(missing)}")
        return 1

    drift: list[str] = []
    for dist, rev in sorted(config_revs.items()):
        installed = _installed(dist)
        if rev != installed:
            drift.append(f"  {dist}: pre-commit rev={rev}  !=  locked/CI={installed}")

    if drift:
        print("Pre-commit linter versions have drifted from the locked (CI) versions:")
        print("\n".join(drift))
        print(
            "\nFix: update the matching `rev:` in .pre-commit-config.yaml to the locked "
            "version (prefix with 'v' where that repo tags with one), so the hook and CI "
            "run identical tools. Then `pre-commit install --install-hooks`."
        )
        return 1

    print("pre-commit linter revs match the locked/CI versions:")
    for dist, rev in sorted(config_revs.items()):
        print(f"  {dist} == {rev}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
