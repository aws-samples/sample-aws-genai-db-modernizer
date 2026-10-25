#!/usr/bin/env python3
"""PreToolUse hook (Bash): no push to another repository from a fork PR's worktree (#1165).

``gh pr checkout`` of a contributor's fork PR leaves the branch's ``remote`` and
``pushRemote`` set to the fork's URL while ``origin`` stays the upstream repository. In
such a worktree a plain ``git push`` goes to the fork (the maintainer's "allow edits"
path) and is allowed; ``git push origin HEAD``, ``git push -u origin <new-branch>`` or a push
to any other URL would create a branch on the upstream repository, which is refused.

The repository is the event's ``cwd``, followed through an earlier ``cd <dir>`` or ``pushd
<dir>`` in the same command line and the ``git -C <dir>``, ``--git-dir`` and ``--work-tree``
options. A command substitution on the push line (``git push $(echo origin) HEAD``) is treated
as an unknown remote and refused. Only the push target is checked, not
whether the branch already exists there. Stdin: the hook event JSON. Exit 0 allows,
exit 2 blocks. Fails open when the directory is not a repository, HEAD is detached, git is
missing or does not answer.

Not a shell parser. Out of reach: a worktree not made with ``gh pr checkout`` (a branch
fetched from ``pull/N/head`` has no fork URL in its config), a push hidden in ``bash -c
"..."`` or a script, and ``git push`` through an alias.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess  # nosec B404 -- runs git with a fixed argv; the branch name is passed as one argument
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hook_common  # noqa: E402

_WRAPPERS: dict[str, set[str]] = {
    "command": set(),
    "builtin": set(),
    "exec": set(),
    "nohup": set(),
    "time": set(),
    "env": {"-u", "-C", "-S"},
    "nice": {"-n"},
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-r", "-t", "-U", "-D", "-R"},
}
_GIT_VALUE_OPTIONS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"}
# git push options whose value is the next token (the `--opt=value` form is one token).
_PUSH_VALUE_OPTIONS = {"--repo", "-o", "--push-option", "--receive-pack", "--exec"}
_REASON = (
    "Blocked: this worktree is checked out for a contributor's fork PR (branch {branch}, "
    "pushing to {fork}); `git push {target}` would put a branch on a different repository. "
    "Push to the fork with a plain `git push`, or do your own work in a separate worktree."
)


# A command substitution on the same line as a push: its words are not the push's own.
_SUBSTITUTED_PUSH = re.compile(r"\bpush\b[^|&;\n]*(?:\$\(|`)")


def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


def _is_assignment(token: str) -> bool:
    return "=" in token and not token.startswith("-") and token.split("=")[0].isidentifier()


def _git_push(tokens: list[str]) -> tuple[list[str], str | None] | None:
    """``(push arguments, directory from git -C)`` for a ``git ... push`` segment, else None."""
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if _is_assignment(token):
            i += 1
        elif os.path.basename(token) in _WRAPPERS:
            takes_value = _WRAPPERS[os.path.basename(token)]
            i += 1
            while i < len(tokens) and (tokens[i].startswith("-") or _is_assignment(tokens[i])):
                i += 2 if tokens[i] in takes_value else 1
        else:
            break
    if i >= len(tokens) or os.path.basename(tokens[i]) != "git":
        return None
    i += 1
    directory: str | None = None
    while i < len(tokens) and tokens[i].startswith("-"):
        option = tokens[i]
        value = tokens[i + 1] if i + 1 < len(tokens) else None
        if option == "-C" and value is not None:
            directory = os.path.join(directory, value) if directory else value
        elif option in ("--git-dir", "--work-tree") and value is not None:
            directory = _repo_dir(value)
        elif option.startswith(("--git-dir=", "--work-tree=")):
            directory = _repo_dir(option.split("=", 1)[1])
        i += 2 if option in _GIT_VALUE_OPTIONS else 1
    if i < len(tokens) and tokens[i] == "push":
        return tokens[i + 1 :], directory
    return None


def _repo_dir(path: str) -> str:
    """The directory to ask git about for ``--git-dir`` / ``--work-tree`` ``path``."""
    path = os.path.expanduser(path).rstrip("/")
    return os.path.dirname(path) if os.path.basename(path) == ".git" else path


def _push_target(args: list[str]) -> str | None:
    """The remote a ``git push`` names (``--repo`` or the first positional), else None."""
    positional: list[str] = []
    repo: str | None = None
    skip = False
    for position, arg in enumerate(args):
        if skip:
            skip = False
            continue
        if arg == "--":
            positional.extend(args[position + 1 :])
            break
        if arg.startswith("--repo="):
            repo = arg.split("=", 1)[1]
        elif arg in _PUSH_VALUE_OPTIONS:
            if arg == "--repo" and position + 1 < len(args):
                repo = args[position + 1]
            skip = True
        elif arg.startswith("-") and len(arg) > 1:
            continue
        else:
            positional.append(arg)
    return repo or (positional[0] if positional else None)


def _git(directory: str, *args: str) -> str | None:
    try:
        proc = subprocess.run(  # nosec B603 B607 -- fixed argv
            ["git", "-C", directory, *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else None


def _is_url(value: str) -> bool:
    return "://" in value or re.match(r"^[\w.-]+@[\w.-]+:", value) is not None


def _normalised(url: str) -> str:
    """A comparable form of a git URL: no scheme or user, no ``.git``, lower case."""
    url = url.strip().lower()
    url = re.sub(r"^[a-z+]+://", "", url)
    url = re.sub(r"^[^@/]+@", "", url)
    url = url.replace(":", "/", 1) if re.match(r"^[\w.-]+:[^/]", url) else url
    url = url.rstrip("/")
    return url[:-4] if url.endswith(".git") else url


def _fork_of(directory: str) -> tuple[str, str] | None:
    """``(branch, fork URL)`` when the directory's branch was checked out for a fork PR."""
    branch = _git(directory, "symbolic-ref", "--short", "-q", "HEAD")
    if not branch:
        return None
    for key in ("pushRemote", "remote"):
        value = _git(directory, "config", "--get", f"branch.{branch}.{key}")
        if value and _is_url(value):
            return branch, value
    return None


def _resolves_to(directory: str, target: str, fork: str) -> bool:
    url = target
    if not _is_url(target):
        url = _git(directory, "config", "--get", f"remote.{target}.pushurl") or ""
        url = url or _git(directory, "config", "--get", f"remote.{target}.url") or ""
    return bool(url) and _normalised(url) == _normalised(fork)


def violation(command: str, cwd: str) -> str | None:
    """The reason to refuse ``command`` run from ``cwd``, or None when it is fine."""
    directory = cwd or os.getcwd()
    for segment in hook_common.split_compound_command(command):
        tokens = _tokens(segment)
        if tokens[:1] in (["cd"], ["pushd"]) and len(tokens) == 2:
            directory = os.path.normpath(os.path.join(directory, os.path.expanduser(tokens[1])))
            continue
        parsed = _git_push(tokens)
        if parsed is None:
            continue
        args, option_dir = parsed
        where = os.path.join(directory, option_dir) if option_dir else directory
        target = _push_target(args)
        if target is None:
            if _SUBSTITUTED_PUSH.search(command):
                target = "$(...)"  # `git push $(echo origin) HEAD`: the split leaves a plain push
            else:
                continue  # a plain `git push` goes where the branch is configured to push: the fork
        fork = _fork_of(where)
        if fork is None or _resolves_to(where, target, fork[1]):
            continue
        return _REASON.format(branch=fork[0], fork=fork[1], target=target)
    return None


def main() -> int:
    event = hook_common.read_event()
    if event.get("tool_name") not in (None, "Bash"):
        return 0
    command = hook_common.tool_input(event).get("command")
    if not isinstance(command, str) or "push" not in command:
        return 0
    cwd = event.get("cwd") if isinstance(event.get("cwd"), str) else ""
    reason = violation(command, cwd)
    return int(hook_common.block(reason)) if reason else 0


if __name__ == "__main__":
    sys.exit(main())
