"""Unit tests for scripts/check_commit_authors.py (#1408).

No test runs git or gh: ``check_range`` takes a ``run`` seam for ``git log`` and
``pr_author_login`` takes the event, its name and a ``gh`` runner.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts import check_commit_authors as mod

MAINTAINER = ("Esteban Serna Parra", "tebanieo@gmail.com")
DEPENDABOT = ("dependabot[bot]", "49699333+dependabot[bot]@users.noreply.github.com")


@pytest.fixture
def allowed() -> set[tuple[str, str]]:
    return mod.load_allowlist()


def _check(name: str, email: str, allowed, pr_login=None):
    return mod.check_identity(name, email, allowed, pr_login=pr_login)


# ---------------------------------------------------------------------------
# check_identity
# ---------------------------------------------------------------------------


def test_the_maintainer_is_allowed(allowed) -> None:
    assert _check(*MAINTAINER, allowed) is None


def test_the_maintainer_email_is_matched_case_insensitively(allowed) -> None:
    assert _check("Esteban Serna Parra", "Tebanieo@Gmail.com", allowed) is None


@pytest.mark.parametrize(
    "identity",
    [
        ("Norbert Funke", "norbert@funke.com"),
        ("Dinesh Kumar", "94637609+happycontribute@users.noreply.github.com"),
        DEPENDABOT,
    ],
)
def test_listed_authors_are_allowed(identity: tuple[str, str], allowed) -> None:
    assert _check(*identity, allowed) is None


def test_a_one_character_name_is_refused_even_with_the_maintainer_email(allowed) -> None:
    violation = _check("x", "tebanieo@gmail.com", allowed)
    assert violation is not None and "one character" in violation


def test_a_one_character_name_is_refused_for_the_pr_author_too(allowed) -> None:
    violation = _check("x", "octo@users.noreply.github.com", allowed, pr_login="octo")
    assert violation is not None and "one character" in violation


@pytest.mark.parametrize("name", ["Esteban Serna", "tebanieo"])
@pytest.mark.parametrize("pr_login", [None, "tebanieo", "octo"])
def test_the_maintainer_aliases_are_allowed(name: str, pr_login, allowed) -> None:
    assert _check(name, "tebanieo@gmail.com", allowed, pr_login=pr_login) is None


@pytest.mark.parametrize("name", ["李", "é", "Ж"])
@pytest.mark.parametrize("pr_login", [None, "octo"])
def test_one_non_ascii_letter_is_a_name(name: str, pr_login, allowed) -> None:
    assert _check(name, "li@gmail.com", allowed, pr_login=pr_login) is None


@pytest.mark.parametrize("name", ["x", "X", "1", "-", " ", "", " x ", "·"])
@pytest.mark.parametrize("pr_login", [None, "octo", "tebanieo"])
def test_one_ascii_character_or_symbol_is_refused_everywhere(name: str, pr_login, allowed) -> None:
    violation = _check(name, "li@gmail.com", allowed, pr_login=pr_login)
    assert violation is not None and "one character" in violation


@pytest.mark.parametrize("pr_login", [None, "octo", "tebanieo"])
def test_the_maintainer_address_needs_a_listed_name_in_every_mode(pr_login, allowed) -> None:
    """A contributor PR's run and the merge queue agree, so the PR fails first."""
    violation = _check("Esteban S", "tebanieo@gmail.com", allowed, pr_login=pr_login)
    assert violation is not None and "maintainer's address" in violation
    violation = _check("Esteban S", "TEBANIEO@gmail.com", allowed, pr_login=pr_login)
    assert violation is not None


@pytest.mark.parametrize("pr_login", ["tebanieo", "TEBANIEO"])
@pytest.mark.parametrize(
    "identity",
    [
        ("Someone Else", "someone@gmail.com"),
        ("Esteban Serna Parra", "someone@gmail.com"),  # a listed name, another email
        ("octo", "octo@users.noreply.github.com"),  # noreply of someone else
    ],
)
def test_an_unknown_identity_is_refused_on_a_maintainer_pr(
    identity: tuple[str, str], pr_login, allowed
) -> None:
    """A pull request the maintainer account opened: the allowlist applies."""
    violation = _check(*identity, allowed, pr_login=pr_login)
    assert violation is not None and "not an allowed commit author" in violation


def test_the_maintainer_accounts_noreply_address_is_allowed(allowed) -> None:
    assert (
        _check("Esteban", "7132977+tebanieo@users.noreply.github.com", allowed, pr_login="tebanieo")
        is None
    )


@pytest.mark.parametrize(
    "identity",
    [
        ("Omar Abdeen", "omar@gmail.com"),  # a real address
        ("Octo Cat", "1234+octo@users.noreply.github.com"),
        ("pramithi25r@gmail.com", "pramithi25r@gmail.com"),  # the email as the name
        ("Someone Else", "someone@gmail.com"),  # not the PR author, still well-formed
        ("Octo Cat", "7132977+tebanieo@users.noreply.github.com"),
    ],
)
@pytest.mark.parametrize("pr_login", ["octo", None])
def test_a_contributor_or_unknown_pr_accepts_any_well_formed_author(
    identity: tuple[str, str], pr_login, allowed
) -> None:
    """A contributor's PR, a merge group, or a local run before a PR exists (or without gh)."""
    assert _check(*identity, allowed, pr_login=pr_login) is None


@pytest.mark.parametrize(
    ("name", "email", "reason"),
    [
        ("x", "octo@gmail.com", "one character"),
        ("root", "octo@gmail.com", "placeholder name"),
        ("Unknown", "octo@gmail.com", "placeholder name"),
        ("user", "octo@gmail.com", "placeholder name"),
        ("Your Name", "you@gmail.com", "placeholder name"),
        ("Octo Cat", "you@example.com", "placeholder email"),
        ("Octo Cat", "octo@laptop.local", "placeholder email"),
        ("Octo Cat", "root@localhost", "malformed email"),  # no dot in the host
        ("Octo Cat", "octo", "malformed email"),
        ("Octo Cat", "octo@gmail", "malformed email"),
        ("Octo Cat", "", "malformed email"),
    ],
)
@pytest.mark.parametrize("pr_login", ["octo", None])
def test_a_contributor_pr_refuses_a_malformed_author(
    name, email, reason, pr_login, allowed
) -> None:
    violation = _check(name, email, allowed, pr_login=pr_login)
    assert violation is not None and reason in violation


def test_a_dependabot_pr_passes(allowed) -> None:
    assert _check(*DEPENDABOT, allowed, pr_login="dependabot[bot]") is None


def test_the_maintainer_noreply_on_a_maintainer_pr_still_needs_a_real_name(allowed) -> None:
    email = "7132977+tebanieo@users.noreply.github.com"
    assert _check("root", email, allowed, pr_login="tebanieo") is not None


# ---------------------------------------------------------------------------
# load_allowlist
# ---------------------------------------------------------------------------


def test_allowlist_skips_comments_and_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "authors.txt"
    path.write_text("# a comment\n\nAda Lovelace <Ada@Example.com>  # trailing\n")
    assert mod.load_allowlist(path) == {("Ada Lovelace", "ada@example.com")}


def test_allowlist_refuses_a_malformed_line(tmp_path: Path) -> None:
    path = tmp_path / "authors.txt"
    path.write_text("Ada Lovelace ada@example.com\n")
    with pytest.raises(ValueError, match="authors.txt:1"):
        mod.load_allowlist(path)


def test_the_committed_allowlist_starts_with_the_maintainer() -> None:
    lines = [
        line
        for line in mod.ALLOWLIST.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert lines[0] == "Esteban Serna Parra <tebanieo@gmail.com>"


# ---------------------------------------------------------------------------
# pr_author_login
# ---------------------------------------------------------------------------


def _no_gh(_args):
    raise AssertionError("gh must not be called")


def test_pr_author_comes_from_the_pull_request_event() -> None:
    event = {"pull_request": {"user": {"login": "octo"}}}
    assert mod.pr_author_login("pull_request", event, run_gh=_no_gh) == ("octo", "event")


def test_a_merge_group_has_no_pr_author() -> None:
    assert mod.pr_author_login("merge_group", {"merge_group": {}}, run_gh=_no_gh) == (None, "")


def test_a_push_has_no_pr_author() -> None:
    assert mod.pr_author_login("push", {}, run_gh=_no_gh) == (None, "")


def test_locally_the_pr_author_comes_from_gh() -> None:
    assert mod.pr_author_login(None, None, run_gh=lambda _a: "octo\n") == ("octo", "gh pr view")


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError(),
        subprocess.CalledProcessError(1, "gh"),
        subprocess.TimeoutExpired("gh", 1),
    ],
)
def test_locally_without_gh_only_the_allowlist_applies(error) -> None:
    def failing(_args):
        raise error

    assert mod.pr_author_login(None, None, run_gh=failing) == (None, "")


def test_read_event_reads_the_event_file(tmp_path: Path) -> None:
    path = tmp_path / "event.json"
    path.write_text(json.dumps({"pull_request": {"user": {"login": "octo"}}}))
    env = {"GITHUB_EVENT_NAME": "pull_request", "GITHUB_EVENT_PATH": str(path)}
    assert mod.read_event(env) == ("pull_request", {"pull_request": {"user": {"login": "octo"}}})
    assert mod.read_event({}) == (None, None)


# ---------------------------------------------------------------------------
# check_range
# ---------------------------------------------------------------------------


def _git(rows: list[tuple[str, str, str]]):
    def run(args):
        assert args[0] == "log" and "--no-merges" in args
        return "".join(f"{sha}\x1f{name}\x1f{email}\n" for sha, name, email in rows)

    return run


def test_check_range_reports_each_bad_commit(allowed) -> None:
    rows = [
        ("a" * 40, *MAINTAINER),
        ("b" * 40, "x", "tebanieo@gmail.com"),
        ("c" * 40, "Someone Else", "someone@example.com"),
        ("d" * 40, *DEPENDABOT),
    ]
    violations = mod.check_range("origin/main..HEAD", allowed, run=_git(rows))
    assert [v[:8] for v in violations] == ["bbbbbbbb", "cccccccc"]


def test_check_range_on_a_contributor_pr(allowed) -> None:
    rows = [("a" * 40, "Omar Abdeen", "omar@gmail.com"), ("b" * 40, "x", "omar@gmail.com")]
    violations = mod.check_range("origin/main..HEAD", allowed, pr_login="octo", run=_git(rows))
    assert [v[:8] for v in violations] == ["bbbbbbbb"]


def test_check_range_with_no_commits_passes(allowed) -> None:
    assert mod.check_range("origin/main..HEAD", allowed, run=_git([])) == []


def test_main_exits_one_and_prints_the_fix(monkeypatch, capsys) -> None:
    monkeypatch.setattr(mod, "_run_git", _git([("b" * 40, "x", "tebanieo@gmail.com")]))
    monkeypatch.setattr(mod, "pr_author_login", lambda *a, **k: (None, ""))
    assert mod.main(["--range", "origin/main..HEAD"]) == 1
    out = capsys.readouterr().out
    assert "commit author check failed: bbbbbbbb" in out
    assert "--reset-author" in out and "ci/allowed_commit_authors.txt" in out


def test_main_passes_for_the_maintainer(monkeypatch, capsys) -> None:
    monkeypatch.setattr(mod, "_run_git", _git([("a" * 40, *MAINTAINER)]))
    monkeypatch.setattr(mod, "pr_author_login", lambda *a, **k: (None, ""))
    assert mod.main(["--range", "origin/main..HEAD"]) == 0


def test_check_commits_sh_runs_the_author_check() -> None:
    text = (mod.REPO_ROOT / "ci" / "check_commits.sh").read_text()
    assert 'scripts/check_commit_authors.py --range "$RANGE"' in text


def test_pre_merge_sh_runs_the_author_check() -> None:
    text = (mod.REPO_ROOT / "ci" / "pre_merge.sh").read_text()
    assert 'scripts/check_commit_authors.py --range "$base..HEAD"' in text


@pytest.mark.parametrize(
    ("event_name", "payload", "rows", "code"),
    [
        # a contributor PR: a real address passes, the maintainer address needs a listed name
        (
            "pull_request",
            {"pull_request": {"user": {"login": "octo"}}},
            [("Omar Abdeen", "omar@gmail.com")],
            0,
        ),
        (
            "pull_request",
            {"pull_request": {"user": {"login": "octo"}}},
            [("Esteban S", "tebanieo@gmail.com")],
            1,
        ),
        (
            "pull_request",
            {"pull_request": {"user": {"login": "octo"}}},
            [("x", "omar@gmail.com")],
            1,
        ),
        # a maintainer PR: the allowlist
        (
            "pull_request",
            {"pull_request": {"user": {"login": "tebanieo"}}},
            [("Omar Abdeen", "omar@gmail.com")],
            1,
        ),
        ("pull_request", {"pull_request": {"user": {"login": "tebanieo"}}}, [MAINTAINER], 0),
        # the merge queue and a push: the well-formed rule, maintainer address listed
        (
            "merge_group",
            {"merge_group": {}},
            [("Omar Abdeen", "omar@gmail.com"), MAINTAINER, DEPENDABOT],
            0,
        ),
        ("merge_group", {"merge_group": {}}, [("x", "tebanieo@gmail.com")], 1),
        ("push", {}, [("Omar Abdeen", "omar@gmail.com")], 0),
    ],
)
def test_main_reads_the_actions_event(
    event_name, payload, rows, code, tmp_path: Path, monkeypatch, capsys
) -> None:
    path = tmp_path / "event.json"
    path.write_text(json.dumps(payload))
    monkeypatch.setenv("GITHUB_EVENT_NAME", event_name)
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(path))
    numbered = [(f"{i:040d}", name, email) for i, (name, email) in enumerate(rows)]
    monkeypatch.setattr(mod, "_run_git", _git(numbered))
    assert mod.main(["--range", "origin/main..HEAD"]) == code
