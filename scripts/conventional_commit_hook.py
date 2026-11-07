#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Run conventional-pre-commit's check, except on merge commits (#1968).

conventional-pre-commit (pinned in ``.pre-commit-config.yaml``) has no
exception for merge commits, so ``git merge`` would fail its ``commit-msg``
hook. This wrapper skips the check when a merge is being concluded (found with
``git rev-parse --git-path MERGE_HEAD``, which also works in a worktree) or
the message is git's own ``Merge ...`` subject, and otherwise hands the
arguments to conventional-pre-commit unchanged.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from pathlib import Path

try:
    from scripts.check_commit_message import is_merge_message, merge_in_progress
except ImportError:  # run as a file: only scripts/ is on sys.path
    from check_commit_message import is_merge_message, merge_in_progress  # type: ignore[no-redef]


def _conventional_main(argv: Sequence[str]) -> int:
    from conventional_pre_commit.hook import main  # type: ignore[import-not-found]

    return int(main(list(argv)))


def run(
    argv: Sequence[str],
    *,
    delegate: Callable[[Sequence[str]], int] = _conventional_main,
    repo: Path | None = None,
) -> int:
    """Return 0 for a merge commit, else the exit code of ``delegate(argv)``."""
    message_files = [a for a in argv if not a.startswith("-")]
    message = ""
    if message_files:
        try:
            message = Path(message_files[-1]).read_text()
        except OSError:
            message = ""
    in_progress = merge_in_progress(repo) if repo else merge_in_progress()
    if is_merge_message(message, merge_in_progress=in_progress):
        return 0
    return delegate(argv)


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
