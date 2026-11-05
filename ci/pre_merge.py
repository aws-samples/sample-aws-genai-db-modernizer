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
* ``stacked --main-ref REF`` (local) or ``stacked --api-repo OWNER/NAME --head-sha SHA
  --head-ref NAME [--number N] [--main-name main]`` (CI): when the branch carries the commits of
  another open pull request (it is stacked on it), print ``stacked N OID`` for that pull request
  and its head commit, else nothing (#1846). The contract checks then compare with OID, not main.
* ``stack-notice N``: print the "stacked on #N: re-check after #N merges" line;
  ``stack-caveat N`` prints what the stacked comparison does not check.
* ``adr BASE_REF``: a branch that changes ``src/agents/referee/`` or ``src/shared/`` edits
  an ADR or says ``No ADR change: <reason>`` in a commit message or the pull request
  body (``PR_BODY_FILE``, #1154; exit 1 when it does neither).
* ``docs-impact BASE_REF``: a branch that changes a path in ``ci/docs_impact_map.toml`` (#1162)
  edits one of that entry's pages, or says ``Docs: <reason>`` in a commit message or the pull
  request body (``PR_BODY_FILE``); warns and lists the pages, exit 1 without the line.
* ``hooks``: the hooks git runs on commit are the repository's (#1843): the clone's
  ``pre-commit`` and ``commit-msg`` hooks are pre-commit's, or ``core.hooksPath`` has a hook
  of each type and the local ones it reaches are pre-commit's (exit 1 otherwise, and on a
  hooks path set to an empty value, under which git runs no hooks). ``warn`` (exit 0) when
  git is too old to read the hooks path's scope (``--show-scope``, git 2.26).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess  # nosec B404 -- runs git with fixed argv
import sys
import tomllib
from pathlib import Path
from typing import Any

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


def stack_notice(number: int) -> str:
    """The line the contract checks print when they compare with a stacked-on pull request."""
    return f"stacked on #{number}: re-check after #{number} merges"


def stack_caveat(number: int) -> str:
    """What the stacked comparison does not check, printed beside the notice."""
    return (
        f"a hand bump or a missing bump inherited from #{number} is not checked here: "
        f"#{number}'s own check and the merge queue (which compares with main) catch it"
    )


def pick_stack_base(
    ahead: list[str],
    prs: list[dict],
    own_ref: str | None = None,
    own_number: int | None = None,
) -> tuple[int, str] | None:
    """The open pull request this branch is stacked on, as ``(number, head commit)``, or None.

    ``ahead`` is the branch's commits that main lacks, oldest first; ``prs`` are the open
    pull requests (``number``, ``headRefName``, ``headRefOid``). A pull request whose head
    commit is among ``ahead`` is one the branch carries; the one nearest the branch's tip is
    the base. Only a pull request that is open, not a draft and from this repository (not a
    fork) counts: anyone can open a throwaway pull request, and a branch that merges in its
    head must not have the contract diff inside it skipped. The branch's own pull request,
    found by number or head name, is never the base.
    A pull request whose head has moved on since the branch took it no longer counts (its new
    head is not among the commits), so the branch is compared with main.
    """
    others = [
        p
        for p in prs
        if p.get("number") != own_number
        and (own_ref is None or p.get("headRefName") != own_ref)
        and p.get("headRefOid")
        and p.get("isDraft") is False
        and p.get("isCrossRepository") is False
    ]
    position = {sha: i for i, sha in enumerate(ahead)}
    carried = [p for p in others if p["headRefOid"] in position]
    if not carried:
        return None
    nearest = max(carried, key=lambda p: position[p["headRefOid"]])
    return nearest["number"], nearest["headRefOid"]


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


def _gh_json(*args: str) -> Any:
    try:
        proc = subprocess.run(  # nosec B603 B607 -- fixed argv
            ["gh", *args], capture_output=True, text=True, check=False
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def _stacked(argv: list[str]) -> int:
    """Print ``stacked N OID`` when the branch is stacked on an open pull request (#1846).

    Fails open: with no ``gh``, no login or no network it prints nothing, so the contract
    checks compare with main as before.
    """
    parser = argparse.ArgumentParser(prog="pre_merge.py stacked")
    parser.add_argument("--main-ref", help="local: main's git ref (e.g. origin/main)")
    parser.add_argument("--api-repo", help="CI: OWNER/NAME, read through the GitHub API")
    parser.add_argument("--head-sha", help="CI: the pull request's head commit")
    parser.add_argument("--head-ref", help="CI: the pull request's head branch")
    parser.add_argument("--number", type=int, help="CI: the pull request's number")
    parser.add_argument("--main-name", default="main")
    args = parser.parse_args(argv)
    own_ref, own_number = args.head_ref, args.number
    if args.api_repo:
        if not args.head_sha:
            parser.error("--api-repo needs --head-sha")
        compare = _gh_json(
            "api", f"repos/{args.api_repo}/compare/{args.main_name}...{args.head_sha}"
        )
        if not isinstance(compare, dict):
            return 0
        ahead = [c["sha"] for c in compare.get("commits", [])]
        repo_args = ["--repo", args.api_repo]
    else:
        if not args.main_ref:
            parser.error("give --main-ref or --api-repo")
        rev = _git_out("rev-list", "--reverse", f"{args.main_ref}..HEAD")
        ahead = rev.split() if rev else []
        repo_args = []
        own_ref = own_ref or _git_out("rev-parse", "--abbrev-ref", "HEAD")
        mine = _gh_json("pr", "view", "--json", "number")
        if isinstance(mine, dict):
            own_number = mine.get("number")
    prs = _gh_json(
        "pr",
        "list",
        "--state",
        "open",
        "--limit",
        "300",
        "--json",
        "number,headRefName,headRefOid,isDraft,isCrossRepository",
        *repo_args,
    )
    if not isinstance(prs, list):
        return 0
    found = pick_stack_base(ahead, prs, own_ref, own_number)
    if found:
        print(f"stacked {found[0]} {found[1]}")
    return 0


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


# ---- Commit hooks (#1843) ----

HOOK_TYPES = ("pre-commit", "commit-msg")
_PRE_COMMIT_MARK = "# File generated by pre-commit: https://pre-commit.com"
INSTALL_HOOKS = "uv run pre-commit install --hook-type pre-commit --hook-type commit-msg"
# pre-commit refuses to install while core.hooksPath is set; an empty value for this one
# command lets it write the clone's own hooks, which a chaining hooks path then reaches.
NO_HOOKS_PATH = "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0="


def is_pre_commit_hook(path: Path, hook_type: str) -> bool:
    """An executable hook script that pre-commit wrote for ``hook_type``."""
    if not path.is_file() or not os.access(path, os.X_OK):
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return _PRE_COMMIT_MARK in text and f"--hook-type={hook_type}" in text


def check_hooks(local_dir: Path, hooks_path: Path | None, scope: str = "") -> tuple[str, str]:
    """``(level, message)``: will a commit run the repository's hooks?

    ``local_dir`` is the clone's own hooks folder (the common git dir's ``hooks``, shared
    by every worktree); ``hooks_path`` is ``core.hooksPath`` when set, from config
    ``scope``. Without a hooks path git runs the local hooks, which must be pre-commit's.
    With one (even one naming ``local_dir``) git runs only its hooks: each type must be
    there, and unless it is pre-commit's own, the local pre-commit hook it is expected to
    chain to must be installed. That a hooks path's hook really chains is not checked."""
    via = hooks_path
    where = f"core.hooksPath ({scope + ': ' if scope else ''}{hooks_path})"
    problems: list[str] = []
    for kind in HOOK_TYPES:
        local = local_dir / kind
        if via is None:
            if not is_pre_commit_hook(local, kind):
                problems.append(f"{kind}: {local} is not pre-commit's {kind} hook")
            continue
        run = via / kind
        if not run.is_file() or not os.access(run, os.X_OK):
            problems.append(f"{kind}: {where} has no executable {kind} hook, so git runs none")
        elif not is_pre_commit_hook(run, kind) and not is_pre_commit_hook(local, kind):
            problems.append(
                f"{kind}: git runs {run} from {where}, and {local}, the pre-commit hook "
                "it chains to, is not installed"
            )
    if not problems:
        if via is None:
            return "ok", f"the pre-commit and commit-msg hooks in {local_dir} are pre-commit's"
        return "ok", (
            f"git runs the pre-commit and commit-msg hooks from {where}; each is pre-commit's "
            f"or has pre-commit's hook in {local_dir} to chain to"
        )
    hint = f"run ./scripts/setup_dev.sh, or {INSTALL_HOOKS}"
    if via is not None:
        hint = (
            f"run ./scripts/setup_dev.sh, or {NO_HOOKS_PATH} {INSTALL_HOOKS} (pre-commit "
            f"refuses to install while core.hooksPath is set; this writes the hooks to "
            f"{local_dir} without changing it)"
        )
    return "error", (
        "; ".join(problems)
        + f". Commits skip the commit-message, secret and scan hooks until they run: {hint}"
    )


def _git_run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603 B607 -- fixed argv
        ["git", *args], capture_output=True, text=True, check=False
    )


def _git_out(*args: str) -> str | None:
    result = _git_run(*args)
    return result.stdout.strip() if result.returncode == 0 else None


def _hooks() -> int:
    common = _git_out("rev-parse", "--git-common-dir")
    top = _git_out("rev-parse", "--show-toplevel")
    if not common:
        print("error: not inside a git clone")
        return 1
    local_dir = Path(common).resolve() / "hooks"
    # Exit 0: set (the value may be empty); 1: unset; anything else: this git cannot
    # answer, e.g. --show-scope needs git 2.26 or later.
    found = _git_run("config", "--show-scope", "--path", "--get", "core.hooksPath")
    if found.returncode not in (0, 1):
        level, message = check_hooks(local_dir, None)
        if level == "ok":
            level, message = "warn", (
                "could not read core.hooksPath (git config --show-scope needs git 2.26 or "
                f"later), so a hooks path, if one is set, was not checked; {message}"
            )
        print(f"{level}: {message}")
        return 1 if level == "error" else 0
    hooks_path, scope = None, ""
    if found.returncode == 0:
        scope, _, value = found.stdout.rstrip("\n").partition("\t")
        if not value.strip():
            # git runs no hook at all under an empty hooks path, not the local ones.
            print(
                f"error: core.hooksPath is set to an empty value ({scope} config), so git "
                f"runs no hooks; remove it (git config --{scope} --unset core.hooksPath), "
                "then rerun"
            )
            return 1
        hooks_path = Path(value)
        if not hooks_path.is_absolute():  # git runs hooks from the top level
            hooks_path = Path(top or os.getcwd()) / hooks_path
    level, message = check_hooks(local_dir, hooks_path, scope)
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
    if cmd == "stack-notice" and rest:
        print(stack_notice(int(rest[0])))
        return 0
    if cmd == "stack-caveat" and rest:
        print(stack_caveat(int(rest[0])))
        return 0
    if cmd == "stacked":
        return _stacked(rest)
    if cmd == "hooks":
        return _hooks()
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
