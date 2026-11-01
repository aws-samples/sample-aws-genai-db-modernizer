#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

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
* ``adr BASE_REF``: a branch that changes ``src/agents/referee/`` or ``src/shared/`` edits
  an ADR or says ``No ADR change: <reason>`` in a commit message or the pull request
  body (``PR_BODY_FILE``, #1154; exit 1 when it does neither).
* ``docs-impact BASE_REF``: a branch that changes a path in ``ci/docs_impact_map.toml`` (#1162)
  edits one of that entry's pages, or says ``Docs: <reason>`` in a commit message or the pull
  request body (``PR_BODY_FILE``); warns and lists the pages, exit 1 without the line.
"""

from __future__ import annotations

import os
import re
import subprocess  # nosec B404 -- runs git with fixed argv
import sys
import tomllib
from pathlib import Path

try:  # as ``ci.pre_merge`` (tests) or as a script run from ci/ (pre_merge.sh)
    from ci.pr_evidence import _BULLET, _visible
except ImportError:  # pragma: no cover -- the script form
    from pr_evidence import _BULLET, _visible  # type: ignore[no-redef]

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


# Behaviour that the ADRs describe (#1154). Tests and docs under these names do not count.
ADR_BEHAVIOUR_PREFIXES = ("src/agents/referee/", "src/shared/")
ADR_DIR = "docs/architecture/decisions/"
# "No ADR change: <reason>" with a real reason; the template's `<reason>` placeholder is not one.
_NO_ADR_CHANGE = re.compile(r"No ADR change:[ \t]*(?!<)\S", re.IGNORECASE)


def check_adr(changed: list[str], text: str) -> tuple[str, str]:
    """The ADR touch rule: ``(level, message)`` for a branch's changed files and its
    commit messages plus pull request body."""
    behaviour = [f for f in changed if f.startswith(ADR_BEHAVIOUR_PREFIXES) and f.endswith(".py")]
    if not behaviour:
        return "ok", "no behaviour under src/agents/referee/ or src/shared/ changed"
    edited = [f for f in changed if f.startswith(ADR_DIR)]
    if edited:
        return "ok", f"{len(behaviour)} behaviour file(s) changed, ADR edited: {', '.join(edited)}"
    if _NO_ADR_CHANGE.search(text):
        return "ok", "behaviour changed, the PR states 'No ADR change: <reason>'"
    return "error", (
        f"{len(behaviour)} file(s) under src/agents/referee/ or src/shared/ changed "
        f"(first: {behaviour[0]}) and no ADR in {ADR_DIR} was edited. Edit the ADR it touches, "
        "or write 'No ADR change: <reason>' in the PR body (the ADR field) or a commit message"
    )


def _adr(base_ref: str) -> int:
    changed, messages, body = _branch_text(base_ref)
    text = "\n".join(messages) + "\n" + (body or "")
    level, message = check_adr(changed, text)
    print(f"{level}: {message}")
    return 1 if level == "error" else 0


# The docs-impact map (#1162): changed source paths -> the pages to review.
DOCS_IMPACT_MAP = Path(__file__).resolve().parent / "docs_impact_map.toml"
# "Docs: <reason>" at the start of a visible line (a "- " or "* " bullet prefix is fine, as
# for the Evidence parser), case-sensitive, with a real reason (the template's `<reason>` is not one).
_DOCS_LINE = re.compile(_BULLET + r"Docs:[ \t]*(?!<)\S", re.MULTILINE)


def has_docs_line(body: str | None, messages: list[str]) -> bool:
    """Whether the PR body or a commit message gives a ``Docs: <reason>`` line.

    The body is read as ``pr_evidence`` reads it (comments, code spans, fenced and indented
    code removed), so a line a reader cannot see does not count. A commit message counts
    through its body or trailers only: a Conventional Commits ``docs: ...`` subject is not a
    reason.
    """
    texts = [_visible(body)] if body else []
    texts += ["\n".join(m.replace("\r\n", "\n").split("\n")[1:]) for m in messages]
    return any(_DOCS_LINE.search(t) for t in texts)


def load_docs_impact_map(path: Path = DOCS_IMPACT_MAP) -> list[dict]:
    """The map's entries: ``name``, ``paths`` (globs), ``docs`` (pages) and ``why``."""
    entries: list[dict] = tomllib.loads(path.read_text(encoding="utf-8")).get("entry", [])
    return entries


def docs_glob_matches(pattern: str, path: str) -> bool:
    """``*`` matches within one folder, ``**`` across folders; no other wildcard."""
    regex = "".join(
        ".*" if part == "**" else "[^/]*" if part == "*" else re.escape(part)
        for part in re.split(r"(\*\*|\*)", pattern)
    )
    return re.fullmatch(regex, path) is not None


def check_docs_impact(
    changed: list[str],
    reasoned: bool,
    entries: list[dict] | None = None,
    body_missing: bool = False,
) -> tuple[str, str]:
    """The docs-impact rule: ``(level, message)`` for a branch's changed files and whether it
    gives a ``Docs:`` reason (``has_docs_line``); ``body_missing`` is for the error text. An entry is *unreviewed* when a changed file matches
    its paths and no changed file is one of its pages."""
    entries = load_docs_impact_map() if entries is None else entries
    touched, unreviewed = [], []
    for entry in entries:
        hits = [f for f in changed if any(docs_glob_matches(g, f) for g in entry["paths"])]
        if not hits:
            continue
        touched.append(entry["name"])
        if not any(page in changed for page in entry["docs"]):
            unreviewed.append((entry, hits))
    if not touched:
        return "ok", "no path in the docs-impact map changed"
    if not unreviewed:
        return "ok", f"mapped path(s) changed with a page edit too ({', '.join(touched)})"
    lines = []
    for entry, hits in unreviewed:
        first = hits[0] + (f" (+{len(hits) - 1} more)" if len(hits) > 1 else "")
        lines.append(f"{entry['name']}: {first} changed; review {', '.join(entry['docs'])}")
    detail = "; ".join(lines)
    if reasoned:
        return "warn", f"{detail}. The PR states 'Docs: <reason>'"
    hint = (
        "no PR body found; add a 'Docs: <reason>' commit trailer, or rerun after opening the PR"
        if body_missing
        else "write 'Docs: <reason>' in the PR body (the Docs field) or as a commit trailer"
    )
    return "error", f"{detail}. Edit a page, or {hint}, saying why the pages need no change"


def _branch_text(base_ref: str) -> tuple[list[str], list[str], str | None]:
    """Changed files, commit messages (oldest first) and the PR body (``None`` without one)."""
    changed = subprocess.run(  # nosec B603 B607 -- fixed argv
        ["git", "diff", "--name-only", f"{base_ref}...HEAD"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.splitlines()
    log = subprocess.run(  # nosec B603 B607 -- fixed argv
        ["git", "log", "--format=%B%x00", f"{base_ref}..HEAD"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    messages = [m.strip("\n") for m in log.split("\0") if m.strip()]
    body_file = os.environ.get("PR_BODY_FILE")
    body = None
    if body_file and os.path.exists(body_file):
        with open(body_file, encoding="utf-8") as fh:
            body = fh.read() or None
    return changed, messages, body


def _docs_impact(base_ref: str) -> int:
    changed, messages, body = _branch_text(base_ref)
    level, message = check_docs_impact(
        changed, has_docs_line(body, messages), body_missing=body is None
    )
    print(f"{level}: {message}")
    return 1 if level == "error" else 0


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
    if cmd == "adr" and rest:
        return _adr(rest[0])
    if cmd == "docs-impact" and rest:
        return _docs_impact(rest[0])
    if cmd == "contracts" and rest:
        return _contracts(rest[0], rest[1:])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
