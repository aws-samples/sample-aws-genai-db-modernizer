#!/usr/bin/env python3
"""Check that every commit in a range has an acceptable author (#1408).

    uv run python scripts/check_commit_authors.py --range origin/main..HEAD

Run by ``ci/check_commits.sh`` (CI) and ``ci/pre_merge.sh`` (locally). Only the
commits the branch adds are judged (the range, ``origin/main..HEAD``); history
already on main is never re-checked. Each non-merge commit's author is judged
by these rules, in order:

- a name of one ASCII character, or none, is refused (``x`` once landed on main);
  a single non-ASCII letter, such as a one-character CJK name, is a name;
- an identity listed in ``ci/allowed_commit_authors.txt`` passes (exact name,
  email in any case);
- a commit under a maintainer address (``MAINTAINER_EMAILS``) that is not listed
  is refused, in every mode, so a pull request run and the merge queue agree;
- on a pull request opened by the maintainer account (``MAINTAINER_LOGINS``),
  nothing else passes but that account's GitHub noreply address
  (``<login>@users.noreply.github.com`` or ``<id>+<login>@users.noreply.github.com``);
- everywhere else (a contributor's or Dependabot's pull request, a
  ``merge_group`` run, a local run with no pull request known) any well-formed
  author passes: a name that is not an ASCII placeholder (``PLACEHOLDER_NAMES``)
  and a well-formed email that is not a placeholder address (``example.com``,
  ``localhost``, a ``.local`` host).

How the pull request author is learned:

- in GitHub Actions, from the event file (``GITHUB_EVENT_PATH``) of a
  ``pull_request`` or ``pull_request_target`` run: ``pull_request.user.login``;
- outside GitHub Actions, from ``gh pr view`` for the current branch, when
  ``gh`` is installed, signed in and the branch has a pull request;
- otherwise it is unknown, and the well-formed rule applies (as in the queue).

Exit codes: 0 (every commit passes, or nothing to check), 1 (one line per
commit that does not).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess  # nosec B404 -- fixed argv, no shell
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST = REPO_ROOT / "ci" / "allowed_commit_authors.txt"
ALLOWLIST_REL = "ci/allowed_commit_authors.txt"

_LINE = re.compile(r"^(?P<name>[^<>]*\S)\s*<(?P<email>[^<>\s]+@[^<>\s]+)>$")
_NOREPLY = re.compile(r"^(?:\d+\+)?(?P<login>[^@+\s]+)@users\.noreply\.github\.com$", re.IGNORECASE)
_PR_EVENTS = ("pull_request", "pull_request_target")
_EMAIL = re.compile(r"^[^@\s<>]+@(?P<host>[^@\s<>]+\.[^@\s<>.]+)$")

# The maintainer's GitHub account and commit address: a pull request the account
# opens, and a commit under the address in any mode, must use a listed identity.
MAINTAINER_LOGINS = frozenset({"tebanieo"})
MAINTAINER_EMAILS = frozenset({"tebanieo@gmail.com"})
# Names and hosts git falls back to, or that tutorials tell people to type.
PLACEHOLDER_NAMES = frozenset(
    {"x", "root", "user", "unknown", "your name", "name", "admin", "nobody", "none", "test"}
)
_PLACEHOLDER_HOSTS = frozenset({"example.com", "example.org", "example.net", "localhost"})
_PLACEHOLDER_SUFFIXES = (".local", ".localdomain", ".example.com")

Runner = Callable[[Sequence[str]], str]
Identity = tuple[str, str]  # (name, lower-cased email)


def load_allowlist(path: Path = ALLOWLIST) -> set[Identity]:
    """The ``Name <email>`` lines of ``path``; ``#`` starts a comment."""
    allowed: set[Identity] = set()
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.split(" #", 1)[0].strip() if not raw.lstrip().startswith("#") else ""
        if not line:
            continue
        match = _LINE.match(line)
        if match is None:
            raise ValueError(f"{path.name}:{number}: expected 'Name <email>', got {raw!r}")
        allowed.add((match.group("name"), match.group("email").lower()))
    return allowed


def well_formed(name: str, email: str) -> str | None:
    """Why ``name <email>`` is not a well-formed identity, or None when it is."""
    if name.strip().lower() in PLACEHOLDER_NAMES:
        return "has a placeholder name; set user.name to your full name"
    match = _EMAIL.match(email)
    if match is None:
        return "has a malformed email; set user.email"
    host = match.group("host").lower()
    if host in _PLACEHOLDER_HOSTS or host.endswith(_PLACEHOLDER_SUFFIXES):
        return "has a placeholder email; set user.email"
    return None


def one_character(name: str) -> bool:
    """A name of one ASCII character (``x``) or none; one non-ASCII letter (a CJK name) passes."""
    stripped = name.strip()
    return len(stripped) == 0 or (
        len(stripped) == 1 and (stripped.isascii() or not stripped.isalpha())
    )


def check_identity(
    name: str, email: str, allowed: set[Identity], *, pr_login: str | None = None
) -> str | None:
    """A one-line reason ``name <email>`` is refused, or None when it is allowed."""
    who = f"'{name} <{email}>'"
    if one_character(name):
        return f"author {who} has a name of one character; set user.name to your full name"
    if (name, email.lower()) in allowed:
        return None
    if email.lower() in MAINTAINER_EMAILS:
        return f"author {who} uses the maintainer's address under a name that is not listed"
    if pr_login is not None and pr_login.lower() in MAINTAINER_LOGINS:
        noreply = _NOREPLY.match(email)
        if not (noreply and noreply.group("login").lower() == pr_login.lower()):
            return f"author {who} is not an allowed commit author"
    problem = well_formed(name, email)
    return f"author {who} {problem}" if problem else None


def read_event(env: Mapping[str, str]) -> tuple[str | None, dict[str, Any] | None]:
    """The GitHub Actions event name and payload, ``(None, None)`` outside Actions."""
    name, path = env.get("GITHUB_EVENT_NAME"), env.get("GITHUB_EVENT_PATH")
    if not name:
        return None, None
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8")) if path else {}
    except (OSError, ValueError):
        payload = {}
    return name, payload if isinstance(payload, dict) else {}


def _run_gh(args: Sequence[str]) -> str:
    return subprocess.run(  # nosec B603 B607 -- fixed argv, gh resolved from PATH
        ["gh", *args], capture_output=True, text=True, check=True, timeout=20, cwd=REPO_ROOT
    ).stdout


def pr_author_login(
    event_name: str | None, event: dict[str, Any] | None, *, run_gh: Runner = _run_gh
) -> tuple[str | None, str]:
    """The pull request author's login and where it came from (``""`` when unknown)."""
    if event_name is not None:
        if event_name not in _PR_EVENTS:
            return None, ""
        pull = (event or {}).get("pull_request")
        user = pull.get("user") if isinstance(pull, dict) else None
        login = user.get("login") if isinstance(user, dict) else None
        return (login, "event") if isinstance(login, str) and login else (None, "")
    try:
        login = run_gh(["pr", "view", "--json", "author", "--jq", ".author.login"]).strip()
    except (OSError, subprocess.SubprocessError):
        return None, ""
    return (login, "gh pr view") if login else (None, "")


def _run_git(args: Sequence[str]) -> str:
    return subprocess.run(  # nosec B603 B607 -- fixed argv, git resolved from PATH
        ["git", *args], capture_output=True, text=True, check=True, cwd=REPO_ROOT
    ).stdout


def check_range(
    rev_range: str,
    allowed: set[Identity],
    *,
    pr_login: str | None = None,
    run: Runner = _run_git,
) -> list[str]:
    """One ``<sha8>: <reason>`` line per non-merge commit in ``rev_range`` that is refused."""
    out = run(["log", "--no-merges", "--format=%H%x1f%an%x1f%ae", rev_range])
    violations: list[str] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        sha, name, email = (line.split("\x1f") + ["", ""])[:3]
        reason = check_identity(name, email, allowed, pr_login=pr_login)
        if reason:
            violations.append(f"{sha[:8]}: {reason}")
    return violations


def failure_help() -> str:
    return (
        "\nAllowed: an identity in "
        f"{ALLOWLIST_REL}; on a pull request the maintainer account opens, also its "
        "GitHub noreply address; anywhere else, any well-formed name and email, except "
        "that a commit under the maintainer's address must use a listed name. Fix the "
        "last commit with: git commit --amend --no-edit --reset-author (after setting "
        "git config user.name and user.email)."
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--range", dest="rev_range", required=True, help="e.g. origin/main..HEAD")
    args = parser.parse_args(argv)

    event_name, event = read_event(os.environ)
    login, source = pr_author_login(event_name, event)
    if login and login.lower() in MAINTAINER_LOGINS:
        print(
            f"commit authors: maintainer pull request ({login}, from {source}), allowlist applies"
        )
    elif login:
        print(f"commit authors: contributor pull request ({login}, from {source})")
    else:
        where = "merge group" if event_name == "merge_group" else "no pull request author known"
        print(f"commit authors: {where}, any well-formed author")
    violations = check_range(args.rev_range, load_allowlist(), pr_login=login, run=_run_git)
    for violation in violations:
        print(f"commit author check failed: {violation}")
    if violations:
        print(failure_help())
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
