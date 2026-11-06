# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for scripts/conventional_commit_hook.py (#1968)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from scripts import conventional_commit_hook as mod


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _worktree_mid_merge(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a").write_text("a\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "b").write_text("b\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "side")
    _git(repo, "checkout", "-q", "main")
    (repo / "c").write_text("c\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "main change")
    worktree = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "wt-branch", str(worktree), "main")
    return worktree


def _recording_delegate(calls: list[list[str]], code: int = 1):
    def delegate(argv):
        calls.append(list(argv))
        return code

    return delegate


def test_merge_in_progress_in_a_worktree_skips_the_conventional_check(tmp_path: Path) -> None:
    worktree = _worktree_mid_merge(tmp_path)
    _git(worktree, "merge", "--no-commit", "--no-ff", "side")
    message = tmp_path / "msg"
    message.write_text("Merge branch 'side' into wt-branch\n")
    calls: list[list[str]] = []
    assert mod.run([str(message)], delegate=_recording_delegate(calls), repo=worktree) == 0
    assert calls == []


def test_non_merge_message_in_a_worktree_is_delegated(tmp_path: Path) -> None:
    worktree = _worktree_mid_merge(tmp_path)
    message = tmp_path / "msg"
    message.write_text("not conventional\n")
    calls: list[list[str]] = []
    assert mod.run([str(message)], delegate=_recording_delegate(calls), repo=worktree) == 1
    assert calls == [[str(message)]]


def test_merge_subject_alone_skips_the_check(tmp_path: Path) -> None:
    worktree = _worktree_mid_merge(tmp_path)
    message = tmp_path / "msg"
    message.write_text("Merge branch 'x'\n")
    calls: list[list[str]] = []
    assert mod.run([str(message)], delegate=_recording_delegate(calls), repo=worktree) == 0
    assert calls == []
