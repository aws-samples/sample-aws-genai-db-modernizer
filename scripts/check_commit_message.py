#!/usr/bin/env python3
"""Check a commit message against this repo's house rules.

Enforces the rules documented in CONTRIBUTING.md's Commit Message Format and
AGENTS.md's Conventions:

- the subject line is at most 72 characters;
- the body (everything between the subject and the trailer block, blank
  lines excluded) is 2 to 4 non-blank lines;
- the message ends with exactly one ``Closes #N`` or ``Refs #N`` trailer
  (one commit per issue -- ``Closes #1, #2`` or two separate trailer lines
  for two issues is rejected, with a message explaining why, not folded
  into the body-line count);
- there is no ``Co-authored-by`` trailer.

Any line that looks like a git trailer (``Token: value``, e.g.
``Signed-off-by:``) is treated as a trailer, not a body line, whether or not
this hook has an opinion on that particular token.

Skipped entirely (exit 0, nothing checked):

- merge commits (a ``Merge ...`` subject, or an in-progress merge via
  ``.git/MERGE_HEAD``);
- fixup/squash/amend commits (a ``fixup!``/``squash!``/``amend!`` subject
  prefix, git's own ``commit --fixup``/``--squash`` convention);
- default ``git revert`` messages: subject ``Revert "..."`` with a body of
  only ``This reverts commit <sha>.`` line(s). A hand-edited revert message
  (extra explanation, a different body) is checked normally -- only the
  stock shape is exempt.

None of the three above follows the normal subject/body/trailer shape, and
``conventional-pre-commit`` already lets merge and fixup/squash/amend
through unchecked.

Before any of the above, the message is truncated at ``git commit -v``'s
scissors line (``# ------------------------ >8 ------------------------``,
using the repo's configured ``core.commentChar``, default/``auto`` treated
as ``#``) -- everything from that line down is the diff `git commit -v`
appends for reference, not part of the message, and must never be read as
extra body lines.

Two entry points:

- a single drafted message, read from a file (the local ``commit-msg``
  pre-commit hook passes the path pre-commit gives it as the sole argument);
- ``--range <rev-range>`` (used by ``ci/check_commits.sh`` over
  ``origin/main..HEAD``): every non-merge commit in the range is checked
  independently, except commits committed before ``RULE_EFFECTIVE_DATE``
  (grandfathered in; see that constant).

Exit codes: 0 (pass, nothing to check, or skipped), 1 (one or more
violations, each printed as a single line).
"""

from __future__ import annotations

import argparse
import re
import subprocess  # nosec B404 -- fixed argv, no shell
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

MAX_SUBJECT_LENGTH = 72
MIN_BODY_LINES = 2
MAX_BODY_LINES = 4
DEFAULT_COMMENT_CHAR = "#"

# Grandfathers every commit already on `main` (and on any PR branched before
# this hook existed) out of the CI *range* check -- otherwise merging this
# hook would retroactively turn every open PR red for commits nobody could
# have made compliant, since the rule did not exist when they were written.
# Only `check_range` (the `--range` / `ci/check_commits.sh` CI path) applies
# this cutoff; the local `commit-msg` hook has none, since it only ever
# validates the message being written right now.
RULE_EFFECTIVE_DATE = datetime(2026, 10, 13, 0, 0, 0, tzinfo=UTC)

_FIXUP_PREFIXES = ("fixup!", "squash!", "amend!")
_MERGE_SUBJECT = re.compile(r"^Merge\b")
_REVERT_SUBJECT = re.compile(r'^Revert ".*"$')
_REVERT_BODY_LINE = re.compile(r"^This reverts commit [0-9a-f]{4,40}\.$", re.IGNORECASE)

# This repo's own issue-reference convention: a single "Closes #N" / "Refs
# #N" line, nothing else on it.
_CLOSES_REFS_TRAILER = re.compile(r"^(Closes|Refs)\s+#\d+\s*$", re.IGNORECASE)
# The same keyword but referencing two or more issues on one line ("Closes
# #1, #2", "Closes #1, closes #2") -- recognized so it can be pulled out of
# the body-line count and rejected with a specific explanation, rather than
# silently miscounted as prose and reported as a generic missing trailer.
_MULTI_ISSUE_TRAILER = re.compile(r"^(Closes|Refs)\b.*#\d+.*#\d+", re.IGNORECASE)
_CO_AUTHORED_BY = re.compile(r"^Co-authored-by:\s*\S", re.IGNORECASE)
# Git's own generic trailer syntax ("Token: value", e.g. "Signed-off-by:",
# "Reviewed-by:", "Change-Id:") -- treated as a trailer line regardless of
# whether this hook itself has a rule about that token.
_GENERIC_TRAILER = re.compile(r"^[A-Za-z][\w-]*:\s+\S")

_TRAILER_LINE = re.compile(
    rf"({_CLOSES_REFS_TRAILER.pattern}|{_MULTI_ISSUE_TRAILER.pattern}|{_GENERIC_TRAILER.pattern})",
    re.IGNORECASE,
)


def _scissors_pattern(comment_char: str) -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(comment_char)}\s*-+\s*>8\s*-+\s*$")


def _comment_line_pattern(comment_char: str) -> re.Pattern[str]:
    return re.compile(rf"^\s*{re.escape(comment_char)}")


def _truncate_at_scissors(lines: Sequence[str], comment_char: str) -> list[str]:
    """Drop the ``git commit -v`` scissors line and everything after it.

    ``git commit -v`` (``commit.verbose``, a common default) appends the
    diff below a line like ``# ---------------- >8 ----------------`` as a
    reference for the person writing the message; it is never part of the
    message itself, including the diff lines underneath, which do not start
    with the comment character and would otherwise be miscounted as body
    text.
    """
    scissors = _scissors_pattern(comment_char)
    for index, line in enumerate(lines):
        if scissors.match(line.strip()):
            return list(lines[:index])
    return list(lines)


def _strip_comment_lines(
    lines: Sequence[str], comment_char: str = DEFAULT_COMMENT_CHAR
) -> list[str]:
    """Drop git's own comment-char-prefixed template lines (``--cleanup=strip``)."""
    comment_line = _comment_line_pattern(comment_char)
    return [line for line in lines if not comment_line.match(line)]


def is_merge_message(message: str, *, merge_in_progress: bool = False) -> bool:
    """True if ``message`` belongs to a merge commit."""
    if merge_in_progress:
        return True
    lines = message.splitlines()
    subject = lines[0] if lines else ""
    return bool(_MERGE_SUBJECT.match(subject))


def is_fixup_message(message: str) -> bool:
    """True if ``message`` belongs to a fixup/squash/amend commit."""
    lines = message.splitlines()
    subject = lines[0] if lines else ""
    return subject.startswith(_FIXUP_PREFIXES)


def is_revert_message(message: str, *, comment_char: str = DEFAULT_COMMENT_CHAR) -> bool:
    """True if ``message`` is git's own default revert message, untouched.

    Only the stock shape -- subject ``Revert "..."``, body made up solely of
    one or more ``This reverts commit <sha>.`` lines -- is exempt. A revert
    with any hand-added explanation is checked like any other commit.
    """
    prepared = _prepare_lines(message, comment_char)
    if prepared is None:
        return False
    subject, rest = prepared
    if not _REVERT_SUBJECT.match(subject):
        return False
    body_lines = [line for line in rest if line.strip()]
    return bool(body_lines) and all(_REVERT_BODY_LINE.match(line.strip()) for line in body_lines)


def _split_trailers(lines: Sequence[str]) -> tuple[list[str], list[str]]:
    """Split body lines from the trailer block.

    The trailer block is the maximal suffix of consecutive, non-blank lines
    that each look like a recognized trailer (see ``_TRAILER_LINE``).
    Everything above that suffix is the body.
    """
    trimmed = list(lines)
    while trimmed and trimmed[-1].strip() == "":
        trimmed.pop()

    trailer_lines: list[str] = []
    index = len(trimmed)
    while index > 0:
        candidate = trimmed[index - 1].strip()
        if candidate == "" or not _TRAILER_LINE.match(candidate):
            break
        trailer_lines.insert(0, candidate)
        index -= 1

    body_lines = trimmed[:index]
    return body_lines, trailer_lines


def _prepare_lines(message: str, comment_char: str) -> tuple[str, list[str]] | None:
    """Return ``(subject, cleaned-rest-lines)``, or ``None`` if empty.

    "Cleaned" means: truncated at the ``git commit -v`` scissors line (if
    any), then with comment lines stripped from everything after the
    subject.
    """
    raw_lines = message.splitlines()
    raw_lines = _truncate_at_scissors(raw_lines, comment_char)
    if not raw_lines or not raw_lines[0].strip():
        return None
    subject = raw_lines[0]
    rest = _strip_comment_lines(raw_lines[1:], comment_char)
    return subject, rest


def check_message(message: str, *, comment_char: str = DEFAULT_COMMENT_CHAR) -> list[str]:
    """Return one-line violations for ``message``; an empty list means it passes.

    Does not apply the merge/fixup/revert skip rules -- call
    ``check_single_message`` for that.
    """
    prepared = _prepare_lines(message, comment_char)
    if prepared is None:
        return ["commit message is empty"]
    subject, rest = prepared

    violations: list[str] = []
    if len(subject) > MAX_SUBJECT_LENGTH:
        violations.append(
            f"subject is {len(subject)} characters, must be at most {MAX_SUBJECT_LENGTH}"
        )

    body_lines, trailer_lines = _split_trailers(rest)
    non_blank_body = [line for line in body_lines if line.strip()]
    if not (MIN_BODY_LINES <= len(non_blank_body) <= MAX_BODY_LINES):
        violations.append(
            f"body has {len(non_blank_body)} non-blank line(s), "
            f"must be {MIN_BODY_LINES}-{MAX_BODY_LINES}"
        )

    multi_issue_line = next(
        (line for line in trailer_lines if _MULTI_ISSUE_TRAILER.match(line)), None
    )
    if multi_issue_line:
        violations.append(
            f"trailer '{multi_issue_line}' references more than one issue; this hook "
            "accepts exactly one 'Closes #N' or 'Refs #N' trailer per commit (one "
            "commit per issue) -- split into separate commits"
        )
    elif not any(_CLOSES_REFS_TRAILER.match(line) for line in trailer_lines):
        violations.append("message must end with a 'Closes #N' or 'Refs #N' trailer")

    if any(_CO_AUTHORED_BY.match(line) for line in rest):
        violations.append("message must not contain a 'Co-authored-by' trailer")

    return violations


def _merge_in_progress() -> bool:
    return (REPO_ROOT / ".git" / "MERGE_HEAD").exists()


def check_single_message(message: str, *, comment_char: str = DEFAULT_COMMENT_CHAR) -> list[str]:
    """Validate one message, honoring the merge/fixup/squash/amend/revert skip rules."""
    if is_merge_message(message, merge_in_progress=_merge_in_progress()):
        return []
    if is_fixup_message(message):
        return []
    if is_revert_message(message, comment_char=comment_char):
        return []
    return check_message(message, comment_char=comment_char)


GitRunner = Callable[[Sequence[str]], str]


def _run_git(args: Sequence[str]) -> str:
    result = subprocess.run(  # nosec B603 B607 -- fixed argv, git resolved from PATH
        ["git", *args],
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO_ROOT,
    )
    return result.stdout


def get_comment_char(*, run: GitRunner = _run_git) -> str:
    """Return the repo's configured ``core.commentChar``, defaulting to ``#``.

    ``core.commentChar`` may be unset (``git config`` exits non-zero),
    ``auto`` (git picks a character per message that isn't already used in
    it -- which one it picked cannot be recovered after the fact, so this
    falls back to the default), or any other single character.
    """
    try:
        value = run(["config", "--get", "core.commentChar"]).strip()
    except subprocess.CalledProcessError:
        return DEFAULT_COMMENT_CHAR
    if not value or value == "auto" or len(value) != 1:
        return DEFAULT_COMMENT_CHAR
    return value


def check_range(rev_range: str, *, run: GitRunner = _run_git) -> list[str]:
    """Validate every non-merge commit in ``rev_range``, each independently.

    Merge commits are excluded by ``git log --no-merges`` itself, not by
    ``is_merge_message`` -- a merge commit already in history has no
    reliable "Merge ..." subject guarantee, but it always has 2+ parents,
    which ``--no-merges`` checks directly. Commits committed before
    ``RULE_EFFECTIVE_DATE`` are grandfathered in: skipped, with one line
    printed per skipped commit, rather than counted as violations.
    """
    comment_char = get_comment_char(run=run)
    log_output = run(["log", "--no-merges", "--format=%H%x1f%cI", rev_range])

    violations: list[str] = []
    for line in log_output.splitlines():
        if not line.strip():
            continue
        sha, _, committer_date_iso = line.partition("\x1f")
        committer_date = datetime.fromisoformat(committer_date_iso)
        if committer_date < RULE_EFFECTIVE_DATE:
            print(f"skipped {sha[:8]} (before the commit-message rule)")
            continue
        message = run(["log", "-1", "--format=%B", sha])
        for violation in check_single_message(message, comment_char=comment_char):
            violations.append(f"{sha[:8]}: {violation}")
    return violations


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "message_file",
        nargs="?",
        help="path to a drafted commit message (as pre-commit's commit-msg stage passes it)",
    )
    parser.add_argument(
        "--range",
        dest="rev_range",
        help="a git rev-range to check instead, e.g. origin/main..HEAD",
    )
    args = parser.parse_args(argv)

    if args.rev_range:
        violations = check_range(args.rev_range)
    elif args.message_file:
        message = Path(args.message_file).read_text()
        violations = check_single_message(message, comment_char=get_comment_char())
    else:
        parser.error("either a message file or --range is required")
        return 2  # pragma: no cover -- parser.error always exits first

    for violation in violations:
        print(f"commit message check failed: {violation}")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
