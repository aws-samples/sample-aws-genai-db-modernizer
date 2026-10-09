"""Gate pull requests on a closing issue link assigned to the PR author.

Used by ``.github/workflows/issue-assignment-gate.yml`` (``on: pull_request``,
never ``pull_request_target``) to answer one question: does this pull request
link at least one issue with a closing keyword (``Closes #N``, ``Fixes #N``,
``Resolves #N``, ...), and is every linked issue assigned to the pull
request's author?

The workflow passes the pull request event fields it already has (number,
author, body, labels, repository) as environment variables, and checks out
the base commit (not the pull request's merge ref) before running this
script. That protects the *script*: a pull request's own changes to it never
run. It does **not** protect the *workflow file* -- with ``on: pull_request``,
GitHub reads ``.github/workflows/issue-assignment-gate.yml`` itself from the
pull request's merge ref, so a pull request could edit the workflow (e.g.
change what gets checked out, or add a step that leaks the token). What
actually guards against that: this repository's fork pull request policy
requires maintainer approval before *every* workflow run from an outside
contributor, not just the first, so a workflow edit is seen by a maintainer
before it ever runs; merging still needs one approving review; and
``.github/CODEOWNERS`` requests (does not require -- that's a separate
maintainer decision about the branch ruleset) a maintainer reviewer for
anything under ``.github/workflows/``. The only network calls this script
makes are read-only GitHub API calls through the ``gh`` CLI, authenticated
with the job's own ``GITHUB_TOKEN``.

By design, this means: the pull request that first adds this script fails
its own check (the script isn't on the base commit yet -- expected, and not
a reason to add it to the branch ruleset until it has passed on a later pull
request), and any future pull request that changes this script's behavior is
itself checked by the *previous* version of the script, not the new one.

Exempt from the gate (pass with a notice):

- the author has admin or write permission (or a custom role built on
  either) on the repository, or is assigned the built-in "maintain" role
- the author is ``dependabot[bot]``
- the pull request carries the ``scope: internal`` label

Linking is resolved two ways, preferring the first that gives an answer:

1. GitHub's own ``closingIssuesReferences`` (GraphQL on the ``PullRequest``
   node) -- the same data GitHub itself uses to auto-close issues on merge,
   and the same data behind the pull request's "Development" sidebar. Works
   for fork pull requests too, since it only needs read access. GitHub only
   populates this for pull requests opened against the repository's default
   branch, so it is legitimately empty for any other base branch.
2. If that call fails, or returns an empty list, a regex parse of the pull
   request body for closing keywords is used as well -- the empty list could
   mean "no closing keywords" or "wrong base branch", and only a body parse
   can tell those apart. Closed issues count the same as open ones, as long
   as they're assigned to the author. Cross-repo references, non-closing
   keywords (e.g. ``Refs #12``) and references to pull requests (not issues)
   never count, whichever path resolves them.

Exit codes:
    0 -- gate passes (closing link found and assigned, or exempt)
    1 -- gate fails (prints an ``::error::`` line explaining what's missing)
    2 -- the script could not get the data it needed (API/CLI failure)
"""

from __future__ import annotations

import json
import os
import re
import subprocess  # nosec B404 -- runs the `gh` CLI with fixed argv, no shell
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

# Built-in GitHub roles that exempt an author outright, matched against
# `role_name`. "triage" and "read" are deliberately absent.
EXEMPT_ROLE_NAMES = {"write", "maintain", "admin"}
# The legacy `permission` field only ever returns one of these four values,
# even for a custom role -- it reflects the closest of them, which is how a
# custom role built on top of write access (a `role_name` that isn't in
# EXEMPT_ROLE_NAMES) still gets exempted.
EXEMPT_LEGACY_PERMISSIONS = {"admin", "write"}
DEPENDABOT_LOGIN = "dependabot[bot]"
INTERNAL_LABEL = "scope: internal"

# Keywords GitHub recognizes as closing keywords. "ref"/"refs"/"see" are
# deliberately absent -- they reference an issue without closing it.
_CLOSING_KEYWORDS = (
    "close",
    "closes",
    "closed",
    "fix",
    "fixes",
    "fixed",
    "resolve",
    "resolves",
    "resolved",
)
_KEYWORD_ALTERNATION = "|".join(_CLOSING_KEYWORDS)

# A single issue reference: a full "https://github.com/<owner>/<repo>/issues/<n>"
# URL, a same-repo "owner/repo#n" shorthand, or a bare "#n" (always same-repo).
# Order matters: the URL and shorthand forms must be tried before the bare
# "#n" form, since "owner/repo#5" would otherwise be matched (wrongly) as
# just "#5" starting partway through the shorthand text.
_REF = (
    r"https://github\.com/(?P<url_owner>[\w.-]+)/(?P<url_repo>[\w.-]+)/issues/(?P<url_num>\d+)"
    r"|(?P<short_owner>[\w.-]+)/(?P<short_repo>[\w.-]+)#(?P<short_num>\d+)"
    r"|#(?P<num>\d+)"
)

# Matching GitHub's own syntax: the keyword is required immediately before
# *each* reference -- "Closes #1, closes #2", not "Closes #1, #2" or
# "Closes #1 and #2". The separator is "[ \t]+" (optionally after a colon),
# not "\s*": a plain newline between the keyword and the reference does not
# count either (unconfirmed whether GitHub accepts "Closes#5" with no space
# at all, so whitespace is required rather than optional, erring toward a
# stricter match). "(?!\w)" after the number rejects "#19abc" outright --
# without it, "\d+" would greedily match "19" and silently treat it as a
# reference to issue 19.
_KEYWORD_REF = re.compile(rf"(?i)\b(?:{_KEYWORD_ALTERNATION})\b:?[ \t]+(?:{_REF})(?!\w)")

# Stripped from the body before parsing, so references inside them never
# count -- an approximation of GitHub's own Markdown-aware parsing, not a
# full Markdown parser; some bodies GitHub itself would parse differently
# may still slip through one of these.
_HTML_COMMENT = re.compile(r"<!--.*?(?:-->|\Z)", re.DOTALL)  # "\Z": an unterminated comment
#  runs to the end of the text, not just to the next "-->" anywhere later.
_FENCED_CODE_BLOCK = re.compile(r"```.*?```", re.DOTALL)
_TILDE_FENCED_CODE_BLOCK = re.compile(r"~~~.*?~~~", re.DOTALL)
_DOUBLE_BACKTICK_SPAN = re.compile(r"``.*?``", re.DOTALL)
_INLINE_CODE_SPAN = re.compile(r"`[^`\n]*`")
_INDENTED_CODE_LINE = re.compile(r"^(?:[ ]{4}|\t).*$", re.MULTILINE)


def _strip_comments_and_code(body: str) -> str:
    # Order matters: strip comments and the longest code delimiters first,
    # so a single backtick that is actually part of a ``` fence or a ``
    # double-backtick span is never mistaken for the start of a single-
    # backtick span.
    body = _HTML_COMMENT.sub(" ", body)
    body = _FENCED_CODE_BLOCK.sub(" ", body)
    body = _TILDE_FENCED_CODE_BLOCK.sub(" ", body)
    body = _DOUBLE_BACKTICK_SPAN.sub(" ", body)
    body = _INLINE_CODE_SPAN.sub(" ", body)
    body = _INDENTED_CODE_LINE.sub(" ", body)
    return body


def extract_closing_issue_numbers(body: str, owner: str, repo: str) -> list[int]:
    """Return same-repo issue numbers the body closes, via keyword parsing.

    Case-insensitive on the keyword; a closing keyword is required
    immediately before each individual reference, on the same line (see
    ``_KEYWORD_REF``). HTML comments (including an unterminated ``<!--``,
    which runs to the end of the text), fenced code blocks (``` ``` ``` or
    ``~~~``), and code spans (double- or single-backtick) are stripped
    first, so references inside them never count; so are 4-space/tab
    indented code lines. A bare ``#N`` reference is always same-repo; a full
    URL or ``owner/repo#N`` shorthand only counts when its owner/repo
    matches the given repo (cross-repo links never close an issue here, the
    same as on GitHub). Order is preserved, duplicates removed. This is an
    approximation of GitHub's own parsing, not a full Markdown parser, and
    does not reject pull request numbers -- callers that can tell issues
    from pull requests (the REST fallback in ``resolve_linked_issues``) must
    do that themselves.
    """
    seen: list[int] = []
    if not body:
        return seen

    cleaned = _strip_comments_and_code(body)
    repo_full_name = f"{owner}/{repo}".lower()
    for match in _KEYWORD_REF.finditer(cleaned):
        if match.group("num") is not None:
            number = int(match.group("num"))
        elif match.group("url_num") is not None:
            if f"{match.group('url_owner')}/{match.group('url_repo')}".lower() != repo_full_name:
                continue
            number = int(match.group("url_num"))
        else:
            if (
                f"{match.group('short_owner')}/{match.group('short_repo')}".lower()
                != repo_full_name
            ):
                continue
            number = int(match.group("short_num"))
        if number not in seen:
            seen.append(number)
    return seen


class GhApiError(RuntimeError):
    """Raised when a ``gh`` CLI invocation fails or returns unparsable output."""


class GhNotFoundError(GhApiError):
    """Raised when ``gh`` reports the requested resource doesn't exist (HTTP 404/410).

    A mistyped or deleted issue number falls in this category. Callers that
    can tell "this isn't a real issue" apart from "the API call itself
    failed" -- the body-text fallback in ``resolve_linked_issues`` -- should
    treat this one specifically as "not an issue" rather than as an
    infrastructure failure.
    """


GhRunner = Callable[[Sequence[str]], Any]

# `gh api` prints a message like "gh: Not Found (HTTP 404)" or
# "gh: Gone (HTTP 410)" to stderr on a non-2xx response.
_NOT_FOUND_STATUS = re.compile(r"\bHTTP (404|410)\b")


def run_gh_json(args: Sequence[str]) -> Any:
    """Run ``gh`` with the given arguments and parse stdout as JSON.

    The sole seam for every network call this script makes -- tests replace
    this function rather than hitting the real API.
    """
    try:
        result = subprocess.run(  # nosec B603 B607 -- fixed argv, gh resolved from PATH like CI's own install
            ["gh", *args],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise GhApiError(f"gh {' '.join(args)} failed: {exc}") from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip() or str(exc)
        message = f"gh {' '.join(args)} failed: {stderr}"
        if _NOT_FOUND_STATUS.search(stderr):
            raise GhNotFoundError(message) from exc
        raise GhApiError(message) from exc
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise GhApiError(f"gh {' '.join(args)} returned non-JSON output: {exc}") from exc


_CLOSING_ISSUES_QUERY = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      closingIssuesReferences(first: 50) {
        nodes {
          number
          repository { nameWithOwner }
          assignees(first: 20) { nodes { login } }
        }
      }
    }
  }
}
"""


@dataclass
class LinkedIssue:
    number: int
    assignee_logins: list[str] = field(default_factory=list)


def fetch_closing_issues_graphql(
    owner: str, repo: str, pr_number: int, run: GhRunner = run_gh_json
) -> list[LinkedIssue] | None:
    """Return the pull request's closing issues (number + assignees) via GraphQL.

    Returns ``None`` only when the API call itself fails or returns an
    unexpected shape, so the caller can fall back to a body-text parse. An
    empty list is a *successful* answer, but not necessarily a complete one:
    GitHub only populates this field for pull requests against the
    repository's default branch, so a pull request against any other base
    always gets an empty list here regardless of what its body says -- the
    caller falls back to a body parse in that case too (see
    ``resolve_linked_issues``).
    """
    try:
        response = run(
            [
                "api",
                "graphql",
                "-f",
                f"query={_CLOSING_ISSUES_QUERY}",
                "-F",
                f"owner={owner}",
                "-F",
                f"repo={repo}",
                "-F",
                f"number={pr_number}",
            ]
        )
    except GhApiError:
        return None

    try:
        nodes = response["data"]["repository"]["pullRequest"]["closingIssuesReferences"]["nodes"]
    except (KeyError, TypeError):
        return None

    repo_full_name = f"{owner}/{repo}".lower()
    issues: list[LinkedIssue] = []
    for node in nodes:
        if node.get("repository", {}).get("nameWithOwner", "").lower() != repo_full_name:
            continue  # cross-repo closing reference: never counts
        logins = [a["login"] for a in node.get("assignees", {}).get("nodes", [])]
        issues.append(LinkedIssue(number=node["number"], assignee_logins=logins))
    return issues


def fetch_issue_rest(
    owner: str, repo: str, number: int, run: GhRunner = run_gh_json
) -> dict[str, Any]:
    """Return the raw issue (or pull request) JSON via the REST API.

    The REST "issue" endpoint also returns pull requests (every pull request
    is an issue under the hood); the response carries a ``pull_request`` key
    only when ``number`` actually refers to a pull request. Used by the
    body-text fallback in ``resolve_linked_issues``, which must reject those
    the same way GitHub's own closing keywords would.
    """
    data: dict[str, Any] = run(["api", f"repos/{owner}/{repo}/issues/{number}"])
    return data


@dataclass
class AuthorPermission:
    """The author's access level, in both of GitHub's shapes.

    ``role_name`` is the actual assigned role -- one of GitHub's five
    built-in roles ("admin"/"maintain"/"write"/"triage"/"read") by name, or a
    repository-defined custom role name. ``legacy`` is the older
    ``permission`` field: always exactly one of "admin"/"write"/"read"/"none",
    even for a custom role, where it reflects the closest of those four. That
    is what still exempts a custom role built on top of write access even
    though its ``role_name`` isn't one of the built-in names this script
    checks directly.
    """

    role_name: str | None
    legacy: str


def get_author_permission(
    owner: str, repo: str, author: str, run: GhRunner = run_gh_json
) -> AuthorPermission:
    """Return the author's permission level on the repository, in both shapes."""
    data = run(["api", f"repos/{owner}/{repo}/collaborators/{author}/permission"])
    return AuthorPermission(
        role_name=data.get("role_name") or None,
        legacy=str(data.get("permission", "none")),
    )


@dataclass
class GateResult:
    passed: bool
    message: str


def check_exemption(
    author: str, labels: Sequence[str], permission: AuthorPermission | None
) -> str | None:
    """Return a notice string if the author/PR is exempt, else ``None``."""
    if author == DEPENDABOT_LOGIN:
        return f"Author '{author}' is exempt (Dependabot)."
    if INTERNAL_LABEL in labels:
        return f"Pull request is exempt (labelled '{INTERNAL_LABEL}')."
    if permission is not None and (
        permission.role_name in EXEMPT_ROLE_NAMES or permission.legacy in EXEMPT_LEGACY_PERMISSIONS
    ):
        shown = (
            permission.role_name if permission.role_name in EXEMPT_ROLE_NAMES else permission.legacy
        )
        return f"Author '{author}' is exempt ({shown} permission on this repository)."
    return None


# Printed in every "unassigned"/"no link" failure: assigning an issue does not
# itself trigger this check, and re-running it needs write access the
# contributor doesn't have -- so the next step must be something they can do.
_NEXT_STEPS = (
    "Ask on the issue to be assigned. Once you are, edit the pull request "
    "description or push a commit (a maintainer approves the run for outside "
    "contributors), or ask a maintainer to re-run this check."
)


def evaluate_linked_issues(author: str, linked_issues: list[LinkedIssue]) -> GateResult:
    """Check that at least one issue is linked and every linked issue is assigned to author."""
    if not linked_issues:
        return GateResult(
            passed=False,
            message=(
                "This pull request does not link an issue with a closing keyword. "
                "Add 'Closes #N' (or Fixes/Resolves #N) to the pull request description, "
                f"naming the issue you were assigned to work on. {_NEXT_STEPS}"
            ),
        )

    unassigned = [
        issue
        for issue in linked_issues
        if author.lower() not in {login.lower() for login in issue.assignee_logins}
    ]
    if unassigned:
        numbers = ", ".join(f"#{issue.number}" for issue in unassigned)
        return GateResult(
            passed=False,
            message=(
                f"Issue(s) {numbers} are linked by this pull request but are not assigned to "
                f"'{author}'. {_NEXT_STEPS}"
            ),
        )

    numbers = ", ".join(f"#{issue.number}" for issue in linked_issues)
    return GateResult(passed=True, message=f"Linked and assigned issue(s): {numbers}.")


def resolve_linked_issues(
    owner: str,
    repo: str,
    pr_number: int,
    body: str,
    run: GhRunner = run_gh_json,
) -> list[LinkedIssue]:
    """Resolve closing issues: GraphQL first, plus a body parse when it's empty.

    An empty GraphQL result can mean "no closing keywords" or "this pull
    request's base isn't the default branch, so GitHub never recorded any" --
    only a body parse can tell those apart, so it always runs when the
    GraphQL list is empty, not only when the GraphQL call itself failed.
    Pull request numbers, and mistyped or deleted issue numbers (HTTP 404/410
    from the lookup), are silently excluded -- the same as GitHub's own
    closing keywords, neither closes anything, so if that was the only
    reference the body parse comes back empty rather than raising, and the
    gate fails with its normal "no closing keyword" message instead of a
    vague infrastructure error.
    """
    graphql_issues = fetch_closing_issues_graphql(owner, repo, pr_number, run=run)
    if graphql_issues:
        return graphql_issues

    numbers = extract_closing_issue_numbers(body, owner, repo)
    issues: list[LinkedIssue] = []
    for number in numbers:
        try:
            data = fetch_issue_rest(owner, repo, number, run=run)
        except GhNotFoundError:
            continue  # mistyped or deleted issue number: simply not an issue
        except GhApiError as exc:
            raise GhApiError(f"could not read issue #{number}: {exc}") from exc
        if "pull_request" in data:
            continue  # a pull request number was referenced, not an issue
        assignees = [a["login"] for a in data.get("assignees", [])]
        issues.append(LinkedIssue(number=number, assignee_logins=assignees))
    return issues


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def main() -> int:
    repo_full = _env("REPO")
    if "/" not in repo_full:
        print(f"::error::REPO must be 'owner/repo', got {repo_full!r}")
        return 2
    owner, repo = repo_full.split("/", 1)

    try:
        pr_number = int(_env("PR_NUMBER"))
    except ValueError:
        print(f"::error::PR_NUMBER must be an integer, got {_env('PR_NUMBER')!r}")
        return 2

    author = _env("PR_AUTHOR")
    body = _env("PR_BODY")
    try:
        labels = json.loads(_env("PR_LABELS", "[]"))
    except json.JSONDecodeError:
        labels = []

    # Dependabot and the "scope: internal" label are free (no API call) --
    # check them before spending a call on the author's permission level.
    exemption = check_exemption(author, labels, None)
    if exemption:
        print(exemption)
        return 0

    permission_endpoint = f"repos/{owner}/{repo}/collaborators/{author}/permission"
    try:
        permission: AuthorPermission | None = get_author_permission(
            owner, repo, author, run=run_gh_json
        )
    except GhApiError as exc:
        # Named explicitly (not just via the exception text) so the first
        # real run's logs make it obvious which endpoint to check if the
        # default GITHUB_TOKEN ever can't read it (e.g. a 403).
        print(
            f"::warning::Permission lookup via {permission_endpoint} failed for '{author}': {exc}"
        )
        permission = None

    exemption = check_exemption(author, labels, permission)
    if exemption:
        print(exemption)
        return 0

    try:
        linked_issues = resolve_linked_issues(owner, repo, pr_number, body, run=run_gh_json)
    except GhApiError as exc:
        print(f"::error::Could not verify linked issues: {exc}")
        return 2

    result = evaluate_linked_issues(author, linked_issues)
    if result.passed:
        print(result.message)
        return 0

    print(f"::error::{result.message}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
