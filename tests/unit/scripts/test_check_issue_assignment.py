"""Unit tests for scripts/check_issue_assignment.py.

No test calls the network: every ``gh`` invocation goes through
``run_gh_json``/``GhRunner``, which the tests replace with a fake. See
``.github/workflows/issue-assignment-gate.yml`` for how this script is used.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence

import pytest

from scripts import check_issue_assignment as gate

OWNER = "aws-samples"
REPO = "sample-aws-genai-db-modernizer"


# ---------------------------------------------------------------------------
# extract_closing_issue_numbers: pure regex parsing, the body-text fallback.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("Closes #123", [123]),
        ("This closes #5 and fixes #6.", [5, 6]),  # each has its own keyword
        ("Fixes: #456", [456]),
        ("CLOSES #7", [7]),
        ("closed #8", [8]),
        ("Closes #1, closes #2, and closes #3", [1, 2, 3]),  # keyword repeated per GitHub syntax
        ("Closes #1\n\nFixes #1", [1]),  # dedup, order preserved
        ("No issue mentioned here.", []),
        ("", []),
    ],
)
def test_extract_closing_issue_numbers_matches(body: str, expected: list[int]) -> None:
    assert gate.extract_closing_issue_numbers(body, OWNER, REPO) == expected


def test_refs_keyword_does_not_count() -> None:
    """'Refs #12' is a reference, not a closing keyword -- must not count."""
    assert gate.extract_closing_issue_numbers("Refs #12", OWNER, REPO) == []


def test_see_also_does_not_count() -> None:
    assert gate.extract_closing_issue_numbers("See also #99", OWNER, REPO) == []


def test_keyword_not_adjacent_to_reference_does_not_count() -> None:
    """A closing keyword used as prose, not immediately followed by a reference."""
    body = "This change will fix the bug related to #12 eventually."
    assert gate.extract_closing_issue_numbers(body, OWNER, REPO) == []


def test_keyword_without_repeat_only_counts_the_first_reference() -> None:
    """GitHub requires the keyword before each reference: 'Closes #1, #2' only closes #1."""
    assert gate.extract_closing_issue_numbers("Closes #1, #2, and #3", OWNER, REPO) == [1]


def test_same_repo_url_counts() -> None:
    body = f"Closes https://github.com/{OWNER}/{REPO}/issues/42"
    assert gate.extract_closing_issue_numbers(body, OWNER, REPO) == [42]


def test_cross_repo_url_does_not_count() -> None:
    body = "Closes https://github.com/other-org/other-repo/issues/42"
    assert gate.extract_closing_issue_numbers(body, OWNER, REPO) == []


def test_cross_repo_url_case_insensitive_owner_repo_match() -> None:
    body = f"Closes https://github.com/{OWNER.upper()}/{REPO.upper()}/issues/9"
    assert gate.extract_closing_issue_numbers(body, OWNER, REPO) == [9]


def test_same_repo_shorthand_counts() -> None:
    body = f"Closes {OWNER}/{REPO}#9"
    assert gate.extract_closing_issue_numbers(body, OWNER, REPO) == [9]


def test_cross_repo_shorthand_does_not_count() -> None:
    body = "Closes other-org/other-repo#9"
    assert gate.extract_closing_issue_numbers(body, OWNER, REPO) == []


def test_multiple_distinct_issues() -> None:
    body = "Closes #1\nFixes #2\nResolves #3"
    assert gate.extract_closing_issue_numbers(body, OWNER, REPO) == [1, 2, 3]


def test_html_comment_is_ignored() -> None:
    body = "<!-- Closes #5 --> Fixes #6"
    assert gate.extract_closing_issue_numbers(body, OWNER, REPO) == [6]


def test_inline_code_span_is_ignored() -> None:
    body = "Closes `#5` fixes #6"
    assert gate.extract_closing_issue_numbers(body, OWNER, REPO) == [6]


def test_fenced_code_block_is_ignored() -> None:
    body = "```\nCloses #5\n```\nFixes #6"
    assert gate.extract_closing_issue_numbers(body, OWNER, REPO) == [6]


def test_double_backtick_code_span_is_ignored() -> None:
    body = "Closes ``#5`` fixes #6"
    assert gate.extract_closing_issue_numbers(body, OWNER, REPO) == [6]


def test_tilde_fenced_code_block_is_ignored() -> None:
    body = "~~~\nCloses #5\n~~~\nFixes #6"
    assert gate.extract_closing_issue_numbers(body, OWNER, REPO) == [6]


def test_four_space_indented_code_line_is_ignored() -> None:
    body = "    Closes #5\nFixes #6"
    assert gate.extract_closing_issue_numbers(body, OWNER, REPO) == [6]


def test_tab_indented_code_line_is_ignored() -> None:
    body = "\tCloses #5\nFixes #6"
    assert gate.extract_closing_issue_numbers(body, OWNER, REPO) == [6]


def test_unterminated_html_comment_strips_to_end_of_text() -> None:
    """An unclosed '<!--' swallows the rest of the body, including real-looking links."""
    body = "Closes #1\n<!-- unterminated\nFixes #2"
    assert gate.extract_closing_issue_numbers(body, OWNER, REPO) == [1]


def test_keyword_immediately_followed_by_hash_with_no_space_does_not_count() -> None:
    """Unconfirmed whether GitHub accepts 'Closes#5' with no space; require whitespace."""
    assert gate.extract_closing_issue_numbers("Closes#5", OWNER, REPO) == []


def test_keyword_then_line_break_does_not_count() -> None:
    """A newline between the keyword and the reference must not join them."""
    assert gate.extract_closing_issue_numbers("Closes\n#5", OWNER, REPO) == []


def test_trailing_word_characters_reject_the_whole_reference() -> None:
    """'#19abc' must not be silently treated as a reference to issue 19."""
    assert gate.extract_closing_issue_numbers("Closes #19abc", OWNER, REPO) == []


# ---------------------------------------------------------------------------
# evaluate_linked_issues: the pass/fail decision given already-resolved issues.
# ---------------------------------------------------------------------------


def test_no_linked_issues_fails() -> None:
    result = gate.evaluate_linked_issues("alice", [])
    assert result.passed is False
    assert "closing keyword" in result.message


def test_all_linked_issues_assigned_to_author_passes() -> None:
    issues = [
        gate.LinkedIssue(number=1, assignee_logins=["alice", "bob"]),
        gate.LinkedIssue(number=2, assignee_logins=["alice"]),
    ]
    result = gate.evaluate_linked_issues("alice", issues)
    assert result.passed is True
    assert "#1" in result.message and "#2" in result.message


def test_author_login_comparison_is_case_insensitive() -> None:
    issues = [gate.LinkedIssue(number=1, assignee_logins=["Alice"])]
    result = gate.evaluate_linked_issues("alice", issues)
    assert result.passed is True


def test_one_unassigned_issue_fails_even_if_others_are_assigned() -> None:
    issues = [
        gate.LinkedIssue(number=1, assignee_logins=["alice"]),
        gate.LinkedIssue(number=2, assignee_logins=["bob"]),
    ]
    result = gate.evaluate_linked_issues("alice", issues)
    assert result.passed is False
    assert "#2" in result.message
    assert "#1" not in result.message


def test_issue_with_no_assignees_fails() -> None:
    issues = [gate.LinkedIssue(number=1, assignee_logins=[])]
    result = gate.evaluate_linked_issues("alice", issues)
    assert result.passed is False


# ---------------------------------------------------------------------------
# check_exemption: maintainers, Dependabot, "scope: internal".
# ---------------------------------------------------------------------------


def test_dependabot_is_exempt() -> None:
    assert gate.check_exemption("dependabot[bot]", [], None) is not None


def test_internal_label_is_exempt() -> None:
    assert gate.check_exemption("alice", ["scope: internal"], None) is not None


@pytest.mark.parametrize("role_name", ["write", "maintain", "admin"])
def test_write_or_above_role_name_is_exempt(role_name: str) -> None:
    permission = gate.AuthorPermission(role_name=role_name, legacy="write")
    assert gate.check_exemption("alice", [], permission) is not None


@pytest.mark.parametrize("legacy", ["admin", "write"])
def test_admin_or_write_legacy_permission_is_exempt_even_without_matching_role_name(
    legacy: str,
) -> None:
    """A custom role (e.g. "custom-writer") built on write/admin access must still exempt."""
    permission = gate.AuthorPermission(role_name="custom-writer", legacy=legacy)
    assert gate.check_exemption("alice", [], permission) is not None


@pytest.mark.parametrize(
    "permission",
    [None, gate.AuthorPermission(role_name="read", legacy="read")],
)
def test_read_or_no_permission_is_not_exempt(permission: gate.AuthorPermission | None) -> None:
    assert gate.check_exemption("alice", [], permission) is None


def test_triage_role_with_read_legacy_permission_is_not_exempt() -> None:
    permission = gate.AuthorPermission(role_name="triage", legacy="read")
    assert gate.check_exemption("alice", [], permission) is None


def test_unrelated_label_is_not_exempt() -> None:
    permission = gate.AuthorPermission(role_name="read", legacy="read")
    assert gate.check_exemption("alice", ["bug", "good first issue"], permission) is None


# ---------------------------------------------------------------------------
# fetch_closing_issues_graphql: GraphQL path, gh invocation mocked.
# ---------------------------------------------------------------------------


def _graphql_response(nodes: list[dict]) -> dict:
    return {"data": {"repository": {"pullRequest": {"closingIssuesReferences": {"nodes": nodes}}}}}


def test_graphql_path_returns_number_and_assignees() -> None:
    response = _graphql_response(
        [
            {
                "number": 42,
                "repository": {"nameWithOwner": f"{OWNER}/{REPO}"},
                "assignees": {"nodes": [{"login": "alice"}]},
            }
        ]
    )
    issues = gate.fetch_closing_issues_graphql(OWNER, REPO, 7, run=lambda args: response)
    assert issues == [gate.LinkedIssue(number=42, assignee_logins=["alice"])]


def test_graphql_path_filters_cross_repo_nodes() -> None:
    response = _graphql_response(
        [
            {
                "number": 1,
                "repository": {"nameWithOwner": "other-org/other-repo"},
                "assignees": {"nodes": [{"login": "alice"}]},
            },
            {
                "number": 2,
                "repository": {"nameWithOwner": f"{OWNER}/{REPO}"},
                "assignees": {"nodes": []},
            },
        ]
    )
    issues = gate.fetch_closing_issues_graphql(OWNER, REPO, 7, run=lambda args: response)
    assert issues == [gate.LinkedIssue(number=2, assignee_logins=[])]


def test_graphql_path_returns_none_on_api_error() -> None:
    def fail(args: object) -> None:
        raise gate.GhApiError("boom")

    assert gate.fetch_closing_issues_graphql(OWNER, REPO, 7, run=fail) is None


def test_graphql_path_returns_empty_list_is_not_a_failure() -> None:
    """An empty, successful GraphQL answer must not look like an API failure."""
    response = _graphql_response([])
    issues = gate.fetch_closing_issues_graphql(OWNER, REPO, 7, run=lambda args: response)
    assert issues == []


def test_graphql_path_returns_none_on_malformed_response() -> None:
    issues = gate.fetch_closing_issues_graphql(OWNER, REPO, 7, run=lambda args: {"data": None})
    assert issues is None


# ---------------------------------------------------------------------------
# resolve_linked_issues: prefers GraphQL, falls back to the regex body parse.
# ---------------------------------------------------------------------------


def test_resolve_parses_body_when_graphql_list_is_empty() -> None:
    """An empty GraphQL list could mean "wrong base branch" -- always try the body too."""
    calls: list[Sequence[str]] = []

    def run(args: Sequence[str]) -> dict:
        calls.append(args)
        if args[:2] == ["api", "graphql"]:
            return _graphql_response([])
        return {"assignees": [{"login": "alice"}]}

    issues = gate.resolve_linked_issues(OWNER, REPO, 7, "Closes #1", run=run)
    assert issues == [gate.LinkedIssue(number=1, assignee_logins=["alice"])]
    assert len(calls) == 2  # one empty graphql call, one REST issue lookup for #1


def test_resolve_body_parse_finds_nothing_when_graphql_list_is_empty_and_no_link() -> None:
    """An empty GraphQL list plus a body with no closing keyword is simply "no issues"."""
    issues = gate.resolve_linked_issues(
        OWNER, REPO, 7, "No link here.", run=lambda args: _graphql_response([])
    )
    assert issues == []


def test_resolve_falls_back_to_regex_when_graphql_fails() -> None:
    calls: list[Sequence[str]] = []

    def run(args: Sequence[str]) -> dict:
        calls.append(args)
        if args[:2] == ["api", "graphql"]:
            raise gate.GhApiError("graphql unavailable")
        return {"assignees": [{"login": "alice"}]}

    issues = gate.resolve_linked_issues(OWNER, REPO, 7, "Closes #5", run=run)
    assert issues == [gate.LinkedIssue(number=5, assignee_logins=["alice"])]
    assert len(calls) == 2  # one failed graphql call, one REST issue lookup


def test_resolve_fallback_raises_clear_error_on_rest_failure() -> None:
    def run(args: Sequence[str]) -> dict:
        if args[:2] == ["api", "graphql"]:
            raise gate.GhApiError("graphql unavailable")
        raise gate.GhApiError("not found")

    with pytest.raises(gate.GhApiError, match="could not read issue #5"):
        gate.resolve_linked_issues(OWNER, REPO, 7, "Closes #5", run=run)


def test_resolve_fallback_rejects_pull_request_numbers() -> None:
    """A referenced number that is actually a pull request never counts, like on GitHub."""

    def run(args: Sequence[str]) -> dict:
        if args[:2] == ["api", "graphql"]:
            raise gate.GhApiError("graphql unavailable")
        return {"pull_request": {"url": "..."}, "assignees": [{"login": "alice"}]}

    issues = gate.resolve_linked_issues(OWNER, REPO, 7, "Closes #5", run=run)
    assert issues == []


def test_resolve_fallback_treats_404_issue_as_not_an_issue() -> None:
    """A mistyped or deleted issue number is excluded, not a hard failure."""

    def run(args: Sequence[str]) -> dict:
        if args[:2] == ["api", "graphql"]:
            raise gate.GhApiError("graphql unavailable")
        raise gate.GhNotFoundError("gh api ... failed: gh: Not Found (HTTP 404)")

    issues = gate.resolve_linked_issues(OWNER, REPO, 7, "Closes #999999", run=run)
    assert issues == []


def test_resolve_fallback_treats_410_issue_as_not_an_issue() -> None:
    def run(args: Sequence[str]) -> dict:
        if args[:2] == ["api", "graphql"]:
            raise gate.GhApiError("graphql unavailable")
        raise gate.GhNotFoundError("gh api ... failed: gh: Gone (HTTP 410)")

    issues = gate.resolve_linked_issues(OWNER, REPO, 7, "Closes #999999", run=run)
    assert issues == []


def test_main_shows_normal_no_link_message_when_only_reference_is_a_404(
    monkeypatch, capsys
) -> None:
    """A mistyped issue number must surface the ordinary 'no closing keyword' message,
    not a vague infrastructure error."""
    _set_pr_env(monkeypatch, author="alice", body="Closes #999999", labels=[])

    def fake_run(args: list[str]) -> dict:
        if "permission" in args[-1]:
            return {"permission": "read"}
        if args[:2] == ["api", "graphql"]:
            return _graphql_response([])
        raise gate.GhNotFoundError("gh api ... failed: gh: Not Found (HTTP 404)")

    monkeypatch.setattr(gate, "run_gh_json", fake_run)
    assert gate.main() == 1
    out = capsys.readouterr().out
    assert "::error::" in out
    assert "closing keyword" in out


# ---------------------------------------------------------------------------
# run_gh_json: the real subprocess seam, mocked at the subprocess.run level.
# ---------------------------------------------------------------------------


def _called_process_error(stderr: str) -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(returncode=1, cmd=["gh", "api", "..."], stderr=stderr)


def test_run_gh_json_raises_not_found_on_http_404(monkeypatch) -> None:
    def fake_run(*args: object, **kwargs: object) -> None:
        raise _called_process_error("gh: Not Found (HTTP 404)\n")

    monkeypatch.setattr(gate.subprocess, "run", fake_run)
    with pytest.raises(gate.GhNotFoundError):
        gate.run_gh_json(["api", "repos/x/y/issues/999999"])


def test_run_gh_json_raises_not_found_on_http_410(monkeypatch) -> None:
    def fake_run(*args: object, **kwargs: object) -> None:
        raise _called_process_error("gh: Gone (HTTP 410)\n")

    monkeypatch.setattr(gate.subprocess, "run", fake_run)
    with pytest.raises(gate.GhNotFoundError):
        gate.run_gh_json(["api", "repos/x/y/issues/999999"])


def test_run_gh_json_raises_plain_api_error_on_other_status(monkeypatch) -> None:
    def fake_run(*args: object, **kwargs: object) -> None:
        raise _called_process_error("gh: Forbidden (HTTP 403)\n")

    monkeypatch.setattr(gate.subprocess, "run", fake_run)
    with pytest.raises(gate.GhApiError) as exc_info:
        gate.run_gh_json(["api", "repos/x/y/collaborators/z/permission"])
    assert not isinstance(exc_info.value, gate.GhNotFoundError)


# ---------------------------------------------------------------------------
# get_author_permission / fetch_issue_rest: thin wrappers over run().
# ---------------------------------------------------------------------------


def test_get_author_permission_returns_both_role_name_and_legacy_fields() -> None:
    permission = gate.get_author_permission(
        OWNER,
        REPO,
        "alice",
        run=lambda args: {"permission": "write", "role_name": "maintain"},
    )
    assert permission == gate.AuthorPermission(role_name="maintain", legacy="write")


def test_get_author_permission_role_name_is_none_when_absent() -> None:
    permission = gate.get_author_permission(
        OWNER, REPO, "alice", run=lambda args: {"permission": "write"}
    )
    assert permission == gate.AuthorPermission(role_name=None, legacy="write")


def test_get_author_permission_role_name_is_none_when_empty() -> None:
    permission = gate.get_author_permission(
        OWNER, REPO, "alice", run=lambda args: {"permission": "read", "role_name": ""}
    )
    assert permission == gate.AuthorPermission(role_name=None, legacy="read")


def test_get_author_permission_custom_role_name_with_write_legacy_permission() -> None:
    """A GitHub Enterprise custom role based on write access: role_name isn't built-in."""
    permission = gate.get_author_permission(
        OWNER,
        REPO,
        "alice",
        run=lambda args: {"permission": "write", "role_name": "custom-writer"},
    )
    assert permission == gate.AuthorPermission(role_name="custom-writer", legacy="write")
    # And check_exemption still exempts it, via the legacy field.
    assert gate.check_exemption("alice", [], permission) is not None


def test_fetch_issue_rest_returns_raw_json() -> None:
    data = gate.fetch_issue_rest(
        OWNER, REPO, 5, run=lambda args: {"assignees": [{"login": "alice"}, {"login": "bob"}]}
    )
    assert [a["login"] for a in data["assignees"]] == ["alice", "bob"]


# ---------------------------------------------------------------------------
# main(): end-to-end, every gh call mocked, via monkeypatched env + run_gh_json.
# ---------------------------------------------------------------------------


def _set_pr_env(monkeypatch, *, author: str, body: str, labels: list[str], number: int = 7) -> None:
    monkeypatch.setenv("REPO", f"{OWNER}/{REPO}")
    monkeypatch.setenv("PR_NUMBER", str(number))
    monkeypatch.setenv("PR_AUTHOR", author)
    monkeypatch.setenv("PR_BODY", body)
    monkeypatch.setenv("PR_LABELS", json.dumps(labels))


def test_main_passes_when_linked_issue_assigned_to_author(monkeypatch) -> None:
    _set_pr_env(monkeypatch, author="alice", body="Closes #1", labels=[])

    def fake_run(args: list[str]) -> dict:
        if "permission" in args[-1]:
            return {"permission": "read"}
        if args[:2] == ["api", "graphql"]:
            return _graphql_response(
                [
                    {
                        "number": 1,
                        "repository": {"nameWithOwner": f"{OWNER}/{REPO}"},
                        "assignees": {"nodes": [{"login": "alice"}]},
                    }
                ]
            )
        raise AssertionError(f"unexpected gh call: {args}")

    monkeypatch.setattr(gate, "run_gh_json", fake_run)
    assert gate.main() == 0


def test_main_fails_when_no_closing_link(monkeypatch, capsys) -> None:
    _set_pr_env(monkeypatch, author="alice", body="No link here.", labels=[])

    def fake_run(args: list[str]) -> dict:
        if "permission" in args[-1]:
            return {"permission": "read"}
        if args[:2] == ["api", "graphql"]:
            return _graphql_response([])
        raise AssertionError(f"unexpected gh call: {args}")

    monkeypatch.setattr(gate, "run_gh_json", fake_run)
    assert gate.main() == 1
    assert "::error::" in capsys.readouterr().out


def test_main_fails_when_linked_issue_not_assigned_to_author(monkeypatch, capsys) -> None:
    _set_pr_env(monkeypatch, author="alice", body="Closes #1", labels=[])

    def fake_run(args: list[str]) -> dict:
        if "permission" in args[-1]:
            return {"permission": "read"}
        if args[:2] == ["api", "graphql"]:
            return _graphql_response(
                [
                    {
                        "number": 1,
                        "repository": {"nameWithOwner": f"{OWNER}/{REPO}"},
                        "assignees": {"nodes": [{"login": "bob"}]},
                    }
                ]
            )
        raise AssertionError(f"unexpected gh call: {args}")

    monkeypatch.setattr(gate, "run_gh_json", fake_run)
    assert gate.main() == 1
    out = capsys.readouterr().out
    assert "::error::" in out
    assert "#1" in out


def test_main_passes_for_dependabot_without_any_closing_link(monkeypatch, capsys) -> None:
    _set_pr_env(monkeypatch, author="dependabot[bot]", body="", labels=[])

    def fake_run(args: list[str]) -> dict:
        raise AssertionError("Dependabot exemption must short-circuit before any gh call")

    monkeypatch.setattr(gate, "run_gh_json", fake_run)
    assert gate.main() == 0


def test_main_passes_for_scope_internal_label_without_any_gh_call(monkeypatch) -> None:
    _set_pr_env(monkeypatch, author="alice", body="", labels=["scope: internal"])

    def fake_run(args: list[str]) -> dict:
        raise AssertionError("scope: internal exemption must short-circuit before any gh call")

    monkeypatch.setattr(gate, "run_gh_json", fake_run)
    assert gate.main() == 0


def test_main_passes_for_maintainer_without_any_closing_link(monkeypatch) -> None:
    _set_pr_env(monkeypatch, author="maintainer", body="", labels=[])

    def fake_run(args: list[str]) -> dict:
        if "permission" in args[-1]:
            return {"permission": "write"}
        raise AssertionError(f"unexpected gh call: {args}")

    monkeypatch.setattr(gate, "run_gh_json", fake_run)
    assert gate.main() == 0


def test_main_continues_as_non_exempt_when_permission_lookup_fails(monkeypatch, capsys) -> None:
    """A transient permission-lookup failure must not silently exempt the author."""
    _set_pr_env(monkeypatch, author="alice", body="No link here.", labels=[])

    def fake_run(args: list[str]) -> dict:
        if "permission" in args[-1]:
            raise gate.GhApiError("timeout")
        if args[:2] == ["api", "graphql"]:
            return _graphql_response([])
        raise AssertionError(f"unexpected gh call: {args}")

    monkeypatch.setattr(gate, "run_gh_json", fake_run)
    assert gate.main() == 1
    assert "::warning::" in capsys.readouterr().out


def test_main_errors_out_when_linked_issue_data_cannot_be_resolved(monkeypatch, capsys) -> None:
    _set_pr_env(monkeypatch, author="alice", body="Closes #1", labels=[])

    def fake_run(args: list[str]) -> dict:
        if "permission" in args[-1]:
            return {"permission": "read"}
        raise gate.GhApiError("API is down")

    monkeypatch.setattr(gate, "run_gh_json", fake_run)
    assert gate.main() == 2
    assert "::error::" in capsys.readouterr().out


def test_main_rejects_malformed_repo_env(monkeypatch) -> None:
    monkeypatch.setenv("REPO", "not-a-repo-slug")
    monkeypatch.setenv("PR_NUMBER", "7")
    monkeypatch.setenv("PR_AUTHOR", "alice")
    monkeypatch.setenv("PR_BODY", "")
    monkeypatch.setenv("PR_LABELS", "[]")
    assert gate.main() == 2


def test_main_rejects_non_integer_pr_number(monkeypatch) -> None:
    monkeypatch.setenv("REPO", f"{OWNER}/{REPO}")
    monkeypatch.setenv("PR_NUMBER", "not-a-number")
    monkeypatch.setenv("PR_AUTHOR", "alice")
    monkeypatch.setenv("PR_BODY", "")
    monkeypatch.setenv("PR_LABELS", "[]")
    assert gate.main() == 2
