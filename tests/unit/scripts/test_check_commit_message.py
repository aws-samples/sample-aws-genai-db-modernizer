"""Unit tests for scripts/check_commit_message.py.

No test invokes a real git subprocess for the range mode -- ``check_range``
takes a ``run`` seam (same pattern as ``check_issue_assignment.run_gh_json``)
that the tests replace with a fake ``git log`` responder. See
``.pre-commit-config.yaml`` (the ``check-commit-message`` hook, stage
``commit-msg``) and ``ci/check_commits.sh`` for how this script is used.
"""

from __future__ import annotations

import subprocess

import pytest

from scripts import check_commit_message as mod

GOOD_BODY = "Explain the problem in one line.\nExplain the fix in one line."


def _msg(subject: str, body: str = GOOD_BODY, trailer: str = "Closes #123") -> str:
    return f"{subject}\n\n{body}\n\n{trailer}\n"


# ---------------------------------------------------------------------------
# check_message: the core rule set, given a message that is not skipped.
# ---------------------------------------------------------------------------


def test_well_formed_message_passes() -> None:
    assert mod.check_message(_msg("feat(scripts): add commit message checker")) == []


def test_subject_at_exactly_72_chars_passes() -> None:
    subject = "feat(scripts): " + "a" * (72 - len("feat(scripts): "))
    assert len(subject) == 72
    assert mod.check_message(_msg(subject)) == []


def test_subject_over_72_chars_fails() -> None:
    subject = "feat(scripts): " + "a" * 60
    assert len(subject) > 72
    violations = mod.check_message(_msg(subject))
    assert any("72" in v for v in violations)


def test_body_with_one_line_fails() -> None:
    violations = mod.check_message(_msg("fix(x): y", body="Only one line."))
    assert any("non-blank line" in v for v in violations)


def test_body_with_two_lines_passes() -> None:
    body = "First line of the problem.\nSecond line, the fix."
    assert mod.check_message(_msg("fix(x): y", body=body)) == []


def test_body_with_four_lines_passes() -> None:
    body = "One.\nTwo.\nThree.\nFour."
    assert mod.check_message(_msg("fix(x): y", body=body)) == []


def test_body_with_five_lines_fails() -> None:
    body = "One.\nTwo.\nThree.\nFour.\nFive."
    violations = mod.check_message(_msg("fix(x): y", body=body))
    assert any("non-blank line" in v for v in violations)


def test_blank_lines_inside_body_do_not_count_toward_the_limit() -> None:
    """Issue #570: 2-4 *non-blank* lines -- a stray blank line inside the body
    must not itself push the count over the limit."""
    body = "One.\n\nTwo.\n\nThree."
    assert mod.check_message(_msg("fix(x): y", body=body)) == []


def test_missing_trailer_fails() -> None:
    violations = mod.check_message(_msg("fix(x): y", trailer=""))
    assert any("Closes" in v and "Refs" in v for v in violations)


def test_refs_trailer_passes() -> None:
    assert mod.check_message(_msg("fix(x): y", trailer="Refs #42")) == []


def test_closes_trailer_passes() -> None:
    assert mod.check_message(_msg("fix(x): y", trailer="Closes #42")) == []


def test_trailer_is_case_insensitive() -> None:
    assert mod.check_message(_msg("fix(x): y", trailer="closes #42")) == []


def test_fixes_keyword_alone_does_not_satisfy_the_trailer_rule() -> None:
    """Only Closes/Refs are the accepted trailer keywords for this hook."""
    violations = mod.check_message(_msg("fix(x): y", trailer="Fixes #42"))
    assert any("Closes" in v and "Refs" in v for v in violations)


def test_closes_multiple_issues_on_one_line_is_rejected_with_a_clear_message() -> None:
    """'Closes #1, #2' is rejected (one commit per issue), with a specific
    explanation -- not folded into the body-line count as stray prose."""
    violations = mod.check_message(_msg("fix(x): y", trailer="Closes #1, #2"))
    assert any("more than one issue" in v for v in violations)
    assert not any("non-blank line" in v for v in violations)


def test_closes_closes_multiple_issues_variant_is_rejected() -> None:
    violations = mod.check_message(_msg("fix(x): y", trailer="Closes #1, closes #2"))
    assert any("more than one issue" in v for v in violations)


def test_refs_multiple_issues_on_one_line_is_also_rejected() -> None:
    violations = mod.check_message(_msg("fix(x): y", trailer="Refs #1, #2"))
    assert any("more than one issue" in v for v in violations)


def test_signed_off_by_trailer_is_not_counted_as_a_body_line() -> None:
    """Any 'Token: value' line is a trailer, not body text -- not just the
    tokens this hook has its own opinion about."""
    message = (
        f"fix(x): y\n\n{GOOD_BODY}\n\nSigned-off-by: Someone <someone@example.com>\nCloses #1\n"
    )
    assert mod.check_message(message) == []


def test_signed_off_by_alone_does_not_satisfy_the_closes_refs_requirement() -> None:
    message = f"fix(x): y\n\n{GOOD_BODY}\n\nSigned-off-by: Someone <someone@example.com>\n"
    violations = mod.check_message(message)
    assert any("Closes" in v and "Refs" in v for v in violations)


def test_co_authored_by_trailer_fails_even_with_a_valid_closes_trailer() -> None:
    body = GOOD_BODY
    message = f"fix(x): y\n\n{body}\n\nCo-authored-by: Someone <someone@example.com>\nCloses #1\n"
    violations = mod.check_message(message)
    assert any("Co-authored-by" in v for v in violations)


def test_co_authored_by_is_case_insensitive() -> None:
    message = f"fix(x): y\n\n{GOOD_BODY}\n\nCO-AUTHORED-BY: Someone <s@example.com>\nCloses #1\n"
    violations = mod.check_message(message)
    assert any("Co-authored-by" in v for v in violations)


def test_multiple_violations_are_all_reported() -> None:
    long_subject = "fix(x): " + "a" * 70
    violations = mod.check_message(_msg(long_subject, body="Only one line.", trailer=""))
    assert len(violations) >= 3


def test_empty_message_fails() -> None:
    assert mod.check_message("") != []


def test_comment_lines_are_stripped_before_counting_body_lines() -> None:
    """A commit-msg hook may see git's own '#'-prefixed template comments."""
    message = (
        "fix(x): y\n\n"
        f"{GOOD_BODY}\n\n"
        "Closes #1\n"
        "# Please enter the commit message for your changes.\n"
        "# Lines starting with '#' will be ignored.\n"
    )
    assert mod.check_message(message) == []


# ---------------------------------------------------------------------------
# `git commit -v`: the scissors line and everything below it must be
# ignored, using the repo's configured core.commentChar.
# ---------------------------------------------------------------------------


def test_scissors_line_and_diff_below_it_are_ignored() -> None:
    message = (
        "feat(x): add thing\n\n"
        f"{GOOD_BODY}\n\n"
        "Closes #1\n"
        "# ------------------------ >8 ------------------------\n"
        "diff --git a/foo b/foo\n"
        "index abc1234..def5678 100644\n"
        "--- a/foo\n"
        "+++ b/foo\n"
        "@@ -1,1 +1,1 @@\n"
        "-old\n"
        "+new\n"
    )
    assert mod.check_message(message) == []


def test_scissors_with_a_non_default_comment_char() -> None:
    message = (
        "feat(x): add thing\n\n"
        f"{GOOD_BODY}\n\n"
        "Closes #1\n"
        "; ------------------------ >8 ------------------------\n"
        "diff --git a/foo b/foo\n"
    )
    assert mod.check_message(message, comment_char=";") == []


def test_scissors_line_with_the_wrong_comment_char_is_not_recognized() -> None:
    """A ';' scissors line is only special when core.commentChar is ';'."""
    message = (
        "feat(x): add thing\n\n"
        f"{GOOD_BODY}\n\n"
        "Closes #1\n"
        "; ------------------------ >8 ------------------------\n"
        "diff --git a/foo b/foo\n"
    )
    assert mod.check_message(message, comment_char="#") != []


def test_get_comment_char_defaults_to_hash_when_unset() -> None:
    def run(args):
        raise subprocess.CalledProcessError(1, args)

    assert mod.get_comment_char(run=run) == "#"


def test_get_comment_char_defaults_to_hash_for_auto() -> None:
    assert mod.get_comment_char(run=lambda args: "auto\n") == "#"


def test_get_comment_char_uses_a_configured_single_character() -> None:
    assert mod.get_comment_char(run=lambda args: ";\n") == ";"


def test_get_comment_char_defaults_to_hash_for_a_multi_character_value() -> None:
    assert mod.get_comment_char(run=lambda args: "##\n") == "#"


def test_check_range_uses_the_configured_comment_char_for_every_commit() -> None:
    message = (
        "feat(x): add thing\n\n"
        f"{GOOD_BODY}\n\n"
        "Closes #1\n"
        "; ------------------------ >8 ------------------------\n"
        "diff --git a/foo b/foo\n"
    )
    run = _fake_git_log({"abc123": message}, comment_char=";")
    assert mod.check_range("origin/main..HEAD", run=run) == []


# ---------------------------------------------------------------------------
# Reverts: git's own default `git revert` message is exempt; a hand-edited
# one is not.
# ---------------------------------------------------------------------------


def test_default_revert_message_is_skipped() -> None:
    message = (
        'Revert "feat(x): add thing"\n\n'
        "This reverts commit abcdef1234567890abcdef1234567890abcdef12.\n"
    )
    assert mod.check_single_message(message) == []


def test_revert_of_a_revert_subject_is_skipped() -> None:
    message = (
        'Revert "Revert \\"feat(x): add thing\\""\n\n'
        "This reverts commit abcdef1234567890abcdef1234567890abcdef12.\n"
    )
    assert mod.check_single_message(message) == []


def test_hand_edited_revert_message_is_not_skipped() -> None:
    """A revert with any added explanation no longer matches the stock
    shape, so it is checked like any other commit (and fails: no trailer)."""
    message = (
        'Revert "feat(x): add thing"\n\n'
        "This reverts commit abcdef1234567890abcdef1234567890abcdef12.\n"
        "It broke staging; reverting until the root cause is fixed.\n"
    )
    assert mod.check_single_message(message) != []


def test_revert_message_without_the_this_reverts_line_is_not_skipped() -> None:
    message = 'Revert "feat(x): add thing"\n\nJust undoing this for now.\n'
    assert mod.check_single_message(message) != []


def test_is_revert_message_true_for_the_default_shape() -> None:
    message = 'Revert "feat(x): add thing"\n\nThis reverts commit abc1234.\n'
    assert mod.is_revert_message(message) is True


def test_is_revert_message_false_for_a_non_revert_subject() -> None:
    message = "feat(x): add thing\n\nThis reverts commit abc1234.\n"
    assert mod.is_revert_message(message) is False


def test_revert_in_check_range_is_skipped() -> None:
    message = (
        'Revert "feat(x): add thing"\n\n'
        "This reverts commit abcdef1234567890abcdef1234567890abcdef12.\n"
    )
    run = _fake_git_log({"abc123": message})
    assert mod.check_range("origin/main..HEAD", run=run) == []


# ---------------------------------------------------------------------------
# Skip rules: merge and fixup/squash/amend commits.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "subject",
    [
        "Merge branch 'main' into feat/x",
        "Merge pull request #1 from someone/branch",
        "Merge remote-tracking branch 'origin/main'",
    ],
)
def test_merge_commit_subjects_are_skipped(subject: str) -> None:
    # A bare merge subject with no body/trailer would otherwise fail every rule.
    assert mod.check_single_message(subject + "\n") == []


@pytest.mark.parametrize("prefix", ["fixup!", "squash!", "amend!"])
def test_fixup_squash_amend_commits_are_skipped(prefix: str) -> None:
    message = f"{prefix} fix(x): y\n"
    assert mod.check_single_message(message) == []


def test_merge_in_progress_skips_even_a_non_merge_looking_subject(monkeypatch) -> None:
    monkeypatch.setattr(mod, "_merge_in_progress", lambda: True)
    assert mod.check_single_message("some odd message\n") == []


def test_normal_message_is_not_skipped() -> None:
    assert mod.check_single_message(_msg("fix(x): y")) == []
    bad = mod.check_single_message(_msg("fix(x): y", trailer=""))
    assert bad != []


# ---------------------------------------------------------------------------
# check_range: iterates commits via an injected git runner.
# ---------------------------------------------------------------------------


# Well after RULE_EFFECTIVE_DATE (2026-10-13), so tests that don't care
# about the cutoff aren't accidentally grandfathered out of being checked.
_AFTER_CUTOFF = "2026-11-01T00:00:00+00:00"


def _fake_git_log(
    shas_to_messages: dict[str, str],
    *,
    comment_char: str | None = None,
    committer_dates: dict[str, str] | None = None,
    author_emails: dict[str, str] | None = None,
):
    """A fake ``run`` seam: ``comment_char=None`` simulates ``core.commentChar`` unset."""
    committer_dates = committer_dates or {}
    author_emails = author_emails or {}

    def run(args):
        if args[:2] == ["config", "--get"]:
            if comment_char is None:
                raise subprocess.CalledProcessError(1, args)
            return comment_char + "\n"
        if args[:2] == ["log", "--no-merges"]:
            lines = [
                f"{sha}\x1f{committer_dates.get(sha, _AFTER_CUTOFF)}"
                f"\x1f{author_emails.get(sha, 'dev@example.com')}"
                for sha in shas_to_messages
            ]
            return "\n".join(lines) + "\n"
        assert args[0] == "log" and args[1] == "-1"
        sha = args[3]
        return shas_to_messages[sha]

    return run


def test_check_range_passes_when_every_commit_is_clean() -> None:
    run = _fake_git_log({"abc123": _msg("feat(x): add thing")})
    assert mod.check_range("origin/main..HEAD", run=run) == []


def test_check_range_reports_violations_with_sha_prefix() -> None:
    run = _fake_git_log({"abc123def": _msg("feat(x): add thing", trailer="")})
    violations = mod.check_range("origin/main..HEAD", run=run)
    assert len(violations) == 1
    assert violations[0].startswith("abc123de")


def test_check_range_skips_fixup_commits() -> None:
    run = _fake_git_log({"abc123": "fixup! feat(x): add thing\n"})
    assert mod.check_range("origin/main..HEAD", run=run) == []


def test_check_range_with_no_commits_returns_no_violations() -> None:
    run = _fake_git_log({})
    assert mod.check_range("origin/main..HEAD", run=run) == []


def test_check_range_checks_multiple_commits_independently() -> None:
    run = _fake_git_log(
        {
            "sha1": _msg("feat(x): good one"),
            "sha2": _msg("feat(x): bad one", trailer=""),
        }
    )
    violations = mod.check_range("origin/main..HEAD", run=run)
    assert len(violations) == 1
    assert violations[0].startswith("sha2")


# ---------------------------------------------------------------------------
# check_range: commits before RULE_EFFECTIVE_DATE are grandfathered in (the
# CI range path only -- the local hook has no such cutoff).
# ---------------------------------------------------------------------------


def test_check_range_skips_a_commit_before_the_cutoff(capsys) -> None:
    run = _fake_git_log(
        {"abc12345": _msg("feat(x): bad one", trailer="")},
        committer_dates={"abc12345": "2026-10-01T00:00:00+00:00"},
    )
    violations = mod.check_range("origin/main..HEAD", run=run)
    assert violations == []
    assert "skipped abc12345 (before the commit-message rule)" in capsys.readouterr().out


def test_check_range_checks_a_commit_after_the_cutoff(capsys) -> None:
    run = _fake_git_log(
        {"abc12345": _msg("feat(x): bad one", trailer="")},
        committer_dates={"abc12345": "2026-10-14T00:00:00+00:00"},
    )
    violations = mod.check_range("origin/main..HEAD", run=run)
    assert len(violations) == 1
    assert "skipped" not in capsys.readouterr().out


def test_check_range_checks_a_commit_exactly_at_the_cutoff(capsys) -> None:
    """RULE_EFFECTIVE_DATE itself is not grandfathered -- only strictly earlier commits are."""
    run = _fake_git_log(
        {"abc12345": _msg("feat(x): bad one", trailer="")},
        committer_dates={"abc12345": mod.RULE_EFFECTIVE_DATE.isoformat()},
    )
    violations = mod.check_range("origin/main..HEAD", run=run)
    assert len(violations) == 1
    assert "skipped" not in capsys.readouterr().out


def test_check_range_mixes_grandfathered_and_checked_commits(capsys) -> None:
    run = _fake_git_log(
        {
            "old0001": _msg("feat(x): old bad one", trailer=""),
            "new0001": _msg("feat(x): new bad one", trailer=""),
        },
        committer_dates={
            "old0001": "2026-01-01T00:00:00+00:00",
            "new0001": "2026-11-01T00:00:00+00:00",
        },
    )
    violations = mod.check_range("origin/main..HEAD", run=run)
    assert len(violations) == 1
    assert violations[0].startswith("new0001")
    assert "skipped old0001 (before the commit-message rule)" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# main(): CLI entry points.
# ---------------------------------------------------------------------------


def test_main_with_message_file_passes(tmp_path) -> None:
    msg_file = tmp_path / "COMMIT_EDITMSG"
    msg_file.write_text(_msg("feat(x): add thing"))
    assert mod.main([str(msg_file)]) == 0


def test_main_with_message_file_fails_and_prints_reason(tmp_path, capsys) -> None:
    msg_file = tmp_path / "COMMIT_EDITMSG"
    msg_file.write_text(_msg("feat(x): add thing", trailer=""))
    assert mod.main([str(msg_file)]) == 1
    out = capsys.readouterr().out
    assert "Closes" in out and "Refs" in out


def test_main_with_range_delegates_to_check_range(monkeypatch) -> None:
    monkeypatch.setattr(mod, "check_range", lambda rev_range, **kw: [])
    assert mod.main(["--range", "origin/main..HEAD"]) == 0


def test_main_with_range_reports_failures(monkeypatch, capsys) -> None:
    monkeypatch.setattr(mod, "check_range", lambda rev_range, **kw: ["abc1234: bad"])
    assert mod.main(["--range", "origin/main..HEAD"]) == 1
    assert "abc1234: bad" in capsys.readouterr().out


def test_main_requires_either_file_or_range() -> None:
    with pytest.raises(SystemExit):
        mod.main([])


_DEPENDABOT = "49699333+dependabot[bot]@users.noreply.github.com"
_DEPENDABOT_MESSAGE = (
    "chore(deps): bump mcp from 1.28.1 to 1.30.0\n\n"
    + "\n".join(f"Release notes line {i}." for i in range(12))
    + "\n"
)


def test_check_range_skips_dependabot_commits(capsys) -> None:
    run = _fake_git_log(
        {"bot123456": _DEPENDABOT_MESSAGE}, author_emails={"bot123456": _DEPENDABOT}
    )
    assert mod.check_range("origin/main..HEAD", run=run) == []
    assert "skipped bot12345 (Dependabot commit)" in capsys.readouterr().out


def test_check_range_still_checks_a_human_commit_next_to_a_dependabot_one() -> None:
    run = _fake_git_log(
        {"bot123456": _DEPENDABOT_MESSAGE, "humansha1": _msg("feat(x): add thing", trailer="")},
        author_emails={"bot123456": _DEPENDABOT},
    )
    violations = mod.check_range("origin/main..HEAD", run=run)
    assert len(violations) == 1 and violations[0].startswith("humansha")


def test_a_human_cannot_skip_the_check_with_a_bot_like_name() -> None:
    run = _fake_git_log(
        {"humansha1": _msg("feat(x): add thing", trailer="")},
        author_emails={"humansha1": "dependabot@example.com"},
    )
    assert len(mod.check_range("origin/main..HEAD", run=run)) == 1


# ---------------------------------------------------------------------------
# Failure help: a copyable example that itself passes (#1156).
# ---------------------------------------------------------------------------


def test_example_message_passes_the_checker() -> None:
    assert mod.check_message(mod.EXAMPLE_MESSAGE) == []
    assert len(mod.EXAMPLE_MESSAGE.splitlines()[0]) <= mod.MAX_SUBJECT_LENGTH


def test_failure_output_names_the_rule_and_prints_a_passing_example(
    tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    msg_file = tmp_path / "msg"
    msg_file.write_text("feat: no body and no trailer\n")
    assert mod.main([str(msg_file)]) == 1
    out = capsys.readouterr().out
    assert "body has 0 non-blank line(s)" in out
    assert "must end with a 'Closes #N' or 'Refs #N' trailer" in out
    printed = [
        line[4:] for line in out.split("passes", 1)[1].splitlines() if line.startswith("    ")
    ]
    assert mod.check_message("\n".join(printed) + "\n") == []


def test_passing_message_prints_no_example(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    msg_file = tmp_path / "msg"
    msg_file.write_text(_msg("feat: ok"))
    assert mod.main([str(msg_file)]) == 0
    assert capsys.readouterr().out == ""


def test_contributing_md_shows_the_same_example() -> None:
    contributing = (mod.REPO_ROOT / "CONTRIBUTING.md").read_text()
    assert mod.EXAMPLE_MESSAGE.strip() in contributing


def _main_output(tmp_path, capsys, message: str) -> tuple[int, str]:
    msg_file = tmp_path / "msg"
    msg_file.write_text(message)
    code = mod.main([str(msg_file)])
    return code, capsys.readouterr().out


def test_subject_of_72_chars_passes_without_help(tmp_path, capsys) -> None:
    code, out = _main_output(tmp_path, capsys, _msg("a" * 72))
    assert (code, out) == (0, "")


def test_subject_of_73_chars_fails_and_prints_the_example(tmp_path, capsys) -> None:
    code, out = _main_output(tmp_path, capsys, _msg("a" * 73))
    assert code == 1
    assert "subject is 73 characters" in out
    assert "Closes #123" in out


def test_refs_trailer_message_passes_without_help(tmp_path, capsys) -> None:
    code, out = _main_output(tmp_path, capsys, _msg("feat: ok", trailer="Refs #5"))
    assert (code, out) == (0, "")


@pytest.mark.parametrize("subject", ["Merge branch 'x' into main", "fixup! feat: earlier"])
def test_skipped_messages_print_nothing(tmp_path, capsys, subject: str) -> None:
    code, out = _main_output(tmp_path, capsys, f"{subject}\n")
    assert (code, out) == (0, "")


def test_range_failures_print_the_example_once_after_all_violations(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        mod, "check_range", lambda rev_range, **kw: ["aaa1111: bad", "bbb2222: bad"]
    )
    assert mod.main(["--range", "origin/main..HEAD"]) == 1
    out = capsys.readouterr().out
    assert out.count("A message that passes") == 1
    assert out.rindex("bbb2222: bad") < out.index("A message that passes")
