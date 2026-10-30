# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Static-analysis configuration stays single and its suppressions stay explained (#1726, #1727).

- Bandit reads one config, ``[tool.bandit]`` in ``pyproject.toml``. A leftover
  ``.bandit`` file is read first by tools that prefer it, and its globs do not match
  repository-relative paths, so they scan ``tests/`` and report test-only findings.
- A ``# nosec`` comment gives a reason, after ``--``, an em dash or `` - ``.
- A ``nosemgrep`` comment names its rule id and gives a reason: after ``--``, in a
  trailing comment, in a ``nosec`` reason earlier on the line, or in a comment on the
  line above.
- The private-scan test builds its fake API key at run time, so no scan of the working
  tree or of a branch finds a credential-shaped literal there (#1727).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCANNED = ("src", "scripts", "ci")
SKIPPED_PARTS = {"node_modules", "build"}

# ``# nosec`` with only rule ids (or ids followed by a comment that has no reason).
BARE_NOSEC = re.compile(r"#\s*nosec(?:[\s,]+B\d{3})*\s*(?:#(?!.*\s(?:--|—|-)\s).*)?$")
_IDS = r"[A-Za-z0-9_.-]+(?:\s*,\s*[A-Za-z0-9_.-]+)*"
NOSEMGREP = re.compile(rf"nosemgrep(?::\s*(?P<ids>{_IDS}))?(?P<rest>.*)$")
DASH_REASON = re.compile(r"\s(?:--|—)\s+\S")
TRAILING_COMMENT = re.compile(r"#\s*(?!fmt:|noqa|pragma)[A-Za-z]")
NOSEC_REASON_BEFORE = re.compile(r"nosec(?:\s+B\d{3})+\s+(?:--|—|-)\s+\S")
COMMENT_ABOVE = re.compile(r"(?:#|//|/\*|\{/\*)\s*[A-Za-z]")


def _files(suffixes: set[str]) -> list[Path]:
    found = []
    for top in SCANNED:
        for path in sorted((REPO / top).rglob("*")):
            if path.suffix in suffixes and not SKIPPED_PARTS & set(path.parts):
                found.append(path)
    return found


def bare_nosec(lines: list[str]) -> list[int]:
    """1-based numbers of lines whose ``nosec`` marker gives no reason."""
    return [n for n, line in enumerate(lines, 1) if BARE_NOSEC.search(line)]


def unexplained_nosemgrep(lines: list[str]) -> list[int]:
    """1-based numbers of lines whose ``nosemgrep`` has no rule id or no reason."""
    bad = []
    for n, line in enumerate(lines, 1):
        match = NOSEMGREP.search(line)
        if not match:
            continue
        rest = match["rest"]
        above = lines[n - 2].strip() if n > 1 else ""
        explained = (
            DASH_REASON.search(rest)
            or TRAILING_COMMENT.search(rest)
            or NOSEC_REASON_BEFORE.search(line[: match.start()])
            or (COMMENT_ABOVE.match(above) and "nosemgrep" not in above)
        )
        if not match["ids"] or not explained:
            bad.append(n)
    return bad


def test_no_stale_bandit_file_beside_pyproject_config() -> None:
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert "bandit" in pyproject["tool"], "pyproject.toml must carry the one Bandit config"
    assert not (
        REPO / ".bandit"
    ).exists(), ".bandit shadows [tool.bandit] in pyproject.toml; delete it and keep one config"


@pytest.mark.passes_on_main
def test_nosec_guard_accepts_the_three_reason_styles_and_flags_bare_markers() -> None:
    assert bare_nosec(["x = 1  # nosec B311 -- reason"]) == []
    assert bare_nosec(["x = 1  # nosec B311 — reason"]) == []
    assert bare_nosec(["x = 1  # nosec B311 - reason"]) == []
    assert bare_nosec(["except Exception:  # noqa: BLE001  # nosec B110 -- reason"]) == []
    assert bare_nosec(["x = 1  # nosec B311"]) == [1]
    assert bare_nosec(["x = 1  # nosec"]) == [1]
    assert bare_nosec(["r = f(  # nosec B603 # nosemgrep: some-rule"]) == [1]
    assert bare_nosec(["x = 1  # nosec B105  # pragma: allowlist secret"]) == [1]


@pytest.mark.passes_on_main
def test_nosemgrep_guard_needs_a_rule_id_and_a_reason() -> None:
    assert unexplained_nosemgrep(["f()  # nosemgrep: some-rule -- reason"]) == []
    assert unexplained_nosemgrep(["f()  # nosemgrep: some-rule  # reason"]) == []
    assert unexplained_nosemgrep(["# why", "f()  # nosemgrep: some-rule"]) == []
    assert unexplained_nosemgrep(["f()  # nosec B603 -- why  # nosemgrep: some-rule"]) == []
    assert unexplained_nosemgrep(["f()  # nosemgrep"]) == [1]
    assert unexplained_nosemgrep(["f()  # nosemgrep -- reason"]) == [1]
    assert unexplained_nosemgrep(["f()  # nosemgrep: some-rule"]) == [1]


def test_every_nosec_gives_a_reason() -> None:
    bare = [
        f"{path.relative_to(REPO)}:{n}"
        for path in _files({".py"})
        for n in bare_nosec(path.read_text(encoding="utf-8").splitlines())
    ]
    assert not bare, f"'# nosec' without a reason (add '-- why'): {bare}"


def test_every_nosemgrep_names_its_rule_and_gives_a_reason() -> None:
    bad = [
        f"{path.relative_to(REPO)}:{n}"
        for path in _files({".py", ".sh", ".yml", ".yaml", ".js", ".jsx"})
        for n in unexplained_nosemgrep(path.read_text(encoding="utf-8").splitlines())
    ]
    assert not bad, f"'nosemgrep' without a rule id and a reason: {bad}"


@pytest.mark.passes_on_main
def test_private_scan_fixture_keeps_its_fake_credential_split() -> None:
    text = (REPO / "tests/unit/ci/test_private_scan.py").read_text(encoding="utf-8")
    assert (
        "abcd1234efgh5678" not in text
    ), "build the fake key at run time, as the file's other fakes"
