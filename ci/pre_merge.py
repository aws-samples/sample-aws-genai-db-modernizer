#!/usr/bin/env python3
"""Parsing and verdict logic behind ci/pre_merge.sh.

The shell script runs git and the checks; the small decisions live here so
they have unit tests (tests/unit/ci/test_pre_merge.py). Standard library only.

Subcommands (all print to stdout; a non-zero exit means a problem):

* ``pick-remote``: read ``git remote -v`` on stdin, print the remote name that
  points at the public repository (``origin`` when none does).
* ``merge-tree-files``: read ``git merge-tree --write-tree --name-only``
  output on stdin, print the conflicting paths one per line.
* ``contracts BASE_REF FILE...``: compare each changed contract file's
  ``contract_version`` with the one on BASE_REF (exit 1 on a version problem).
* ``pending``: list the pending contract bumps HEAD carries
  (``src/contracts/pending/*.toml``, ADR-008 Decision 8), one ``contract kind`` per line.
"""

from __future__ import annotations

import re
import subprocess  # nosec B404 -- runs git with fixed argv
import sys
import tomllib

PUBLIC_REPO = "aws-samples/sample-aws-genai-db-modernizer"
PENDING_DIR = "src/contracts/pending"
RESOLVE_COMMAND = "uv run python scripts/resolve_contract_versions.py"
BUMP_HINT = (
    "uv run python scripts/bump_contract.py <contract> minor|major --reason ..., "
    "then resolve after the last rebase; ADR-008 Decision 8"
)

# `contract_version: str = Field(default="1.0", ...)`, possibly over several lines.
_VERSION_RE = re.compile(
    r"contract_version\s*:\s*str\s*=\s*Field\(\s*(?:default\s*=\s*)?\"(\d+)\.(\d+)\""
)

Version = tuple[int, int]


def pick_remote(remote_v: str) -> str:
    """Name of the remote whose URL is the public repo; ``origin`` as the fallback.

    ``origin`` wins when both ``origin`` and ``upstream`` match, because a fork
    workflow's ``upstream`` is the only match there and the public clone's
    ``origin`` is the other.
    """
    matches: list[str] = []
    for line in remote_v.splitlines():
        parts = line.split()
        if len(parts) >= 2 and PUBLIC_REPO in parts[1].removesuffix(".git").lower():
            if parts[0] not in matches:
                matches.append(parts[0])
    if not matches:
        return "origin"
    return "origin" if "origin" in matches else matches[0]


def parse_merge_tree(output: str) -> list[str]:
    """Conflicting paths from ``git merge-tree --write-tree --name-only``.

    The first line is the tree id; the conflicted paths follow up to the first
    blank line (informational messages, if any, come after it).
    """
    lines = output.splitlines()
    files: list[str] = []
    for line in lines[1:]:
        if not line.strip():
            break
        if line not in files:
            files.append(line)
    return files


def contract_versions(text: str | None) -> list[Version]:
    """Every ``contract_version`` default declared in a contract module."""
    if text is None:
        return []
    return [(int(a), int(b)) for a, b in _VERSION_RE.findall(text)]


def fmt(version: Version) -> str:
    return f"{version[0]}.{version[1]}"


def next_version(version: Version) -> str:
    """The MINOR bump of ``version``; a MAJOR bump is a maintainer call."""
    return fmt((version[0], version[1] + 1))


def check_contract(
    path: str,
    merge_base: str | None,
    main: str | None,
    head: str | None,
    pending: str | None = None,
) -> tuple[str, str]:
    """Judge one changed contract file; returns ``(level, message)``.

    ``level`` is ``ok``, ``warn`` or ``error``. Texts are the file content at the
    merge base, at main and on the branch (``None`` when absent there). ``pending``
    is the bump kind a pending file on the branch declares for this module.
    """
    if head is None:
        return "ok", f"{path}: deleted on this branch"
    mine = contract_versions(head)
    if not mine:
        return "ok", f"{path}: no contract_version, nothing to compare"
    base_v, main_v = contract_versions(merge_base), contract_versions(main)
    mine_max = max(mine)
    base_max = max(base_v) if base_v else None
    main_max = max(main_v) if main_v else None
    if pending is not None and base_max is not None and mine_max == base_max:
        return (
            "ok",
            f"{path}: pending {pending} bump declared, still at main's version; "
            f"run {RESOLVE_COMMAND} --base <main> after the last rebase",
        )
    if pending is not None and mine_max != base_max:
        return (
            "error",
            f"{path}: a pending bump and a hand-written version ({fmt(mine_max)}); keep one",
        )
    if base_max is not None and mine_max == base_max:
        if main_max in (None, mine_max):
            return (
                "warn",
                f"{path}: changed but still at {fmt(mine_max)} with no pending bump; if the "
                f"shape changed, declare it ({BUMP_HINT})",
            )
        return "ok", f"{path}: version unchanged at {fmt(mine_max)} (main is at {fmt(main_max)})"  # type: ignore[arg-type]
    if main_max is None or mine_max > main_max:
        return (
            "ok",
            f"{path}: {fmt(mine_max)} is higher than main's {fmt(main_max) if main_max else 'none'}",
        )
    if mine_max == main_max:
        return (
            "error",
            f"{path}: {fmt(mine_max)} collides with a version already on main; declare the bump "
            f"instead ({BUMP_HINT}), or take {next_version(main_max)} by hand",
        )
    return (
        "error",
        f"{path}: {fmt(mine_max)} is lower than main's {fmt(main_max)}; declare the bump "
        f"instead ({BUMP_HINT}), or take {next_version(main_max)} by hand",
    )


def pending_bumps(texts: dict[str, str]) -> list[tuple[str, str]]:
    """``(contract, kind)`` for each pending file text, by file name; unreadable
    files are reported as ``(name, "invalid")``."""
    found: list[tuple[str, str]] = []
    for name in sorted(texts):
        try:
            data = tomllib.loads(texts[name])
        except tomllib.TOMLDecodeError:
            found.append((name, "invalid"))
            continue
        contract, kind = data.get("contract"), data.get("kind")
        if not isinstance(contract, str) or kind not in ("minor", "major"):
            found.append((name, "invalid"))
            continue
        found.append((contract, kind))
    return found


def _head_pending() -> list[tuple[str, str]]:
    proc = subprocess.run(  # nosec B603 B607 -- fixed argv
        ["git", "ls-tree", "--name-only", "HEAD", f"{PENDING_DIR}/"],
        capture_output=True,
        text=True,
        check=False,
    )
    names = [n for n in proc.stdout.splitlines() if n.endswith(".toml")]
    return pending_bumps({n: _git_show("HEAD", n) or "" for n in names})


def _module_kinds(bumps: list[tuple[str, str]]) -> dict[str, str]:
    kinds: dict[str, str] = {}
    for contract, kind in bumps:
        path = f"src/contracts/{contract.split('.', 1)[0]}.py"
        if kind != "invalid" and kinds.get(path) != "major":
            kinds[path] = kind
    return kinds


def _git_show(ref: str, path: str) -> str | None:
    proc = subprocess.run(  # nosec B603 B607 -- fixed argv
        ["git", "show", f"{ref}:{path}"], capture_output=True, text=True, check=False
    )
    return proc.stdout if proc.returncode == 0 else None


def _contracts(base_ref: str, files: list[str]) -> int:
    proc = subprocess.run(  # nosec B603 B607 -- fixed argv
        ["git", "merge-base", "HEAD", base_ref], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        print(f"error: no merge base between HEAD and {base_ref}", file=sys.stderr)
        return 2
    base = proc.stdout.strip()
    worst = 0
    pending = _module_kinds(_head_pending())
    for path in files:
        level, message = check_contract(
            path,
            _git_show(base, path),
            _git_show(base_ref, path),
            _git_show("HEAD", path),
            pending.get(path),
        )
        print(f"{level}: {message}")
        if level == "error":
            worst = 1
    return worst


def main(argv: list[str]) -> int:
    cmd, rest = (argv[0], argv[1:]) if argv else ("", [])
    if cmd == "pick-remote":
        print(pick_remote(sys.stdin.read()))
        return 0
    if cmd == "merge-tree-files":
        for path in parse_merge_tree(sys.stdin.read()):
            print(path)
        return 0
    if cmd == "pending":
        for contract, kind in _head_pending():
            print(f"{contract} {kind}")
        return 0
    if cmd == "contracts" and rest:
        return _contracts(rest[0], rest[1:])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
