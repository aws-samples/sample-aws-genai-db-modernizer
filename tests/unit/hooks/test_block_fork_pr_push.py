"""Hook 8: no push to the upstream repository from a worktree checked out for a fork PR (#1165)."""

from __future__ import annotations

import subprocess  # nosec B404 -- test fixtures run git with fixed argv
from pathlib import Path

import pytest

from tests.unit.hooks.conftest import bash

UPSTREAM = "https://github.com/example-org/sample.git"
FORK = "https://github.com/contributor/sample.git"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(  # nosec B603 B607 -- fixed argv
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )


def _repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "--template=", "-b", "main")
    _git(repo, "remote", "add", "origin", UPSTREAM)
    return repo


@pytest.fixture
def fork_pr(tmp_path: Path) -> Path:
    """What ``gh pr checkout`` leaves for a fork PR: the branch's remote and push remote
    are the contributor's fork URL, while ``origin`` stays the upstream repository."""
    repo = _repo(tmp_path, "fork-pr")
    _git(repo, "checkout", "-q", "-b", "contributor-fix")
    _git(repo, "config", "branch.contributor-fix.remote", FORK)
    _git(repo, "config", "branch.contributor-fix.pushRemote", FORK)
    _git(repo, "config", "branch.contributor-fix.merge", "refs/heads/fix")
    return repo


@pytest.fixture
def own_branch(tmp_path: Path) -> Path:
    repo = _repo(tmp_path, "own")
    _git(repo, "checkout", "-q", "-b", "feat/mine")
    _git(repo, "config", "branch.feat/mine.remote", "origin")
    return repo


def _event(command: str, cwd: Path | str) -> dict:
    event = bash(command)
    event["cwd"] = str(cwd)
    return event


BLOCKED = [
    "git push origin HEAD",
    "git push -u origin contributor-fix",
    "git push --set-upstream origin new-branch",
    "git push origin HEAD:refs/heads/new-branch",
    "git push origin contributor-fix:contributor-fix --force-with-lease",
    "git push --force origin HEAD",
    "git push --repo origin HEAD",
    "git push --repo=origin",
    "git push origin",
    "git push https://github.com/example-org/sample.git HEAD",
    "git push git@github.com:example-org/sample.git HEAD",
    "git push https://github.com/someone-else/sample.git HEAD",
    "git push --delete origin some-branch",
    "git -c push.default=current push origin HEAD",
    "command git push origin HEAD",
    "cd . && git push origin HEAD",
    "git add -A && git commit -m x && git push -u origin HEAD",
]
ALLOWED = [
    "git push",
    "git push --force-with-lease",
    f"git push {FORK} HEAD:fix",
    "git push https://github.com/contributor/sample HEAD:fix",
    "git push git@github.com:contributor/sample.git HEAD:fix",
    "git push --tags",
    "git status",
    "git fetch origin",
    "git pull origin main",
    "git log origin/main..HEAD",
    "gh pr comment 1 --body 'git push origin HEAD is blocked here'",
    "echo git push origin HEAD",
    "git commit -m 'git push origin HEAD'",
]


@pytest.mark.parametrize("command", BLOCKED)
def test_blocked_in_a_fork_pr_worktree(command: str, fork_pr: Path, run_hook) -> None:
    res = run_hook("block_fork_pr_push.py", _event(command, fork_pr))
    assert res.blocked, res.stderr
    assert res.stderr.count("\n") == 1 and "fork" in res.stderr and "contributor-fix" in res.stderr


@pytest.mark.parametrize("command", ALLOWED)
def test_allowed_in_a_fork_pr_worktree(command: str, fork_pr: Path, run_hook) -> None:
    assert run_hook("block_fork_pr_push.py", _event(command, fork_pr)).code == 0


def test_a_remote_that_points_at_the_fork_is_allowed(fork_pr: Path, run_hook) -> None:
    _git(fork_pr, "remote", "add", "contributor", FORK)
    assert (
        run_hook("block_fork_pr_push.py", _event("git push contributor HEAD:fix", fork_pr)).code
        == 0
    )


@pytest.mark.parametrize(
    "command", ["git push origin HEAD", "git push -u origin feat/mine", "git push"]
)
def test_an_ordinary_worktree_is_not_restricted(command: str, own_branch: Path, run_hook) -> None:
    assert run_hook("block_fork_pr_push.py", _event(command, own_branch)).code == 0


def test_git_dash_c_picks_the_repository(fork_pr: Path, own_branch: Path, run_hook) -> None:
    elsewhere = fork_pr.parent
    blocked = _event(f"git -C {fork_pr} push origin HEAD", elsewhere)
    res = run_hook("block_fork_pr_push.py", blocked)
    assert res.blocked and "fork PR" in res.stderr
    allowed = _event(f"git -C {own_branch} push origin HEAD", fork_pr)
    assert run_hook("block_fork_pr_push.py", allowed).code == 0


def test_cd_into_the_worktree_is_followed(fork_pr: Path, run_hook) -> None:
    event = _event(f"cd {fork_pr} && git push origin HEAD", fork_pr.parent)
    res = run_hook("block_fork_pr_push.py", event)
    assert res.blocked and "fork PR" in res.stderr


def test_pr_ref_is_found_through_the_push_remote_alone(tmp_path: Path, run_hook) -> None:
    repo = _repo(tmp_path, "push-remote-only")
    _git(repo, "checkout", "-q", "-b", "pr-7")
    _git(repo, "config", "branch.pr-7.pushRemote", FORK)
    res = run_hook("block_fork_pr_push.py", _event("git push origin HEAD", repo))
    assert res.blocked and "fork PR" in res.stderr


@pytest.mark.parametrize("cwd", ["", "/nonexistent/path/for/this/test"])
def test_not_a_repository_fails_open(cwd: str, run_hook) -> None:
    event = bash("git push origin HEAD")
    if cwd:
        event["cwd"] = cwd
    assert run_hook("block_fork_pr_push.py", event).code == 0


def test_detached_head_fails_open(fork_pr: Path, run_hook) -> None:
    _git(
        fork_pr,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "x",
    )
    _git(fork_pr, "checkout", "-q", "--detach")
    assert run_hook("block_fork_pr_push.py", _event("git push origin HEAD", fork_pr)).code == 0


@pytest.mark.parametrize("event", ["", "not json", "[]", '{"tool_input": 3}', '{"tool_input": {}}'])
def test_bad_input_fails_open(event: str, run_hook) -> None:
    assert run_hook("block_fork_pr_push.py", event).code == 0


def test_pushd_git_dir_and_substitution_bypasses_are_closed(fork_pr: Path, run_hook) -> None:
    for command in (
        f"pushd {fork_pr} && git push origin HEAD",
        f"git --git-dir={fork_pr}/.git push origin HEAD",
        f"git --git-dir {fork_pr}/.git push origin HEAD",
        f"git --work-tree={fork_pr} push origin HEAD",
        "git push $(echo origin) HEAD",
        "git push `echo origin` HEAD",
        'git push "$(echo origin)" HEAD',
    ):
        cwd = fork_pr if "$(" in command or "`" in command else fork_pr.parent
        res = run_hook("block_fork_pr_push.py", _event(command, cwd))
        assert res.blocked and "fork PR" in res.stderr, command


def test_a_substitution_elsewhere_is_not_a_push_target(fork_pr: Path, run_hook) -> None:
    event = _event("git log --oneline $(git rev-parse HEAD) && git push", fork_pr)
    assert run_hook("block_fork_pr_push.py", event).code == 0
