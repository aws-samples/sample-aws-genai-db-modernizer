# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ci/contract_major_gate.py: an assess() contract version move needs a maintainer's label
(#1648, #2176).

An ``assess()`` MAJOR needs ``contract-major`` (``Contract Major Approved`` still counts),
an ``assess()`` MINOR needs ``contract-minor``, and a label counts only when a maintainer
applied it. Internal contracts (``src/contracts/``) need no label."""

from __future__ import annotations

import json
import subprocess  # nosec B404 -- builds a scratch git repository with fixed argv
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

from ci import contract_major_gate as gate
from src.assessment import CONTRACT_VERSION

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "contract-major-gate.yml"
LABEL = "contract-major"
LEGACY = "Contract Major Approved"
MINOR = "contract-minor"
MESSAGE = (
    "moving the assess() contract MAJOR breaks callers; it needs a maintainer's approval "
    "(label contract-major)"
)
MINOR_MESSAGE = (
    "an assess() contract MINOR bump needs a maintainer's approval (label contract-minor)"
)
MODELS = "src/assessment/models.py"
SCHEMAS = "docs/contracts/schemas"


def _models(version: str) -> str:
    return f'from typing import Literal\n\nCONTRACT_VERSION: Literal["{version}"] = "{version}"\n'


def _schema(title: str, version: str) -> str:
    return json.dumps({"title": title, "x-contract-version": version})


def _tree(
    version: str = "1.3",
    request: str | None = None,
    result: str | None = None,
    schema_major: int = 1,
) -> gate.DictTree:
    """A revision whose models and both schemas say ``version`` unless overridden."""
    files = {
        MODELS: _models(version),
        f"{SCHEMAS}/assess_request.v1.schema.json": _schema("AssessmentRequest", "1.3"),
        f"{SCHEMAS}/assess_result.v1.schema.json": _schema("AssessmentResult", "1.3"),
        f"{SCHEMAS}/README.md": "# schemas\n",
    }
    files[f"{SCHEMAS}/assess_request.v{schema_major}.schema.json"] = _schema(
        "AssessmentRequest", request or version
    )
    files[f"{SCHEMAS}/assess_result.v{schema_major}.schema.json"] = _schema(
        "AssessmentResult", result or version
    )
    return gate.DictTree(files)


HEAD_CHANGED = "2026-11-12T09:00:00Z"  # the pull request's latest push, in the fakes
LABELED = "2026-11-12T10:00:00Z"  # a label applied after it


class _GitHub:
    """A fake GitHub client: the PR's current labels, its commit count, the label events
    (every current label applied by ``by``, unless ``events`` is given) and each user's
    repository role."""

    def __init__(
        self,
        labels: list[str] | None = None,
        commits: int = 1,
        by: str = "maintainer",
        events: Sequence[tuple[str, ...]] | None = None,
        roles: dict[str, str] | None = None,
        head_changed: str | None = HEAD_CHANGED,
    ) -> None:
        self._labels = labels or []
        self._commits = commits
        given = events if events is not None else [("labeled", n, by) for n in self._labels]
        # (event, label, actor[, when]); an event with no time is after the head change.
        self._events = [tuple(e) if len(e) == 4 else (*e, LABELED) for e in given]
        self._head_changed = head_changed
        self._roles = {"maintainer": "maintain", "owner": "admin", **(roles or {})}
        self.calls: list[tuple[str, Any]] = []

    def labels(self, repo: str, pr: int) -> list[str]:
        self.calls.append(("labels", pr))
        return list(self._labels)

    def commit_count(self, repo: str, pr: int) -> int:
        self.calls.append(("commits", pr))
        return self._commits

    def label_events(self, repo: str, pr: int) -> list[tuple[str, str, str, str]]:
        self.calls.append(("events", pr))
        return list(self._events)  # type: ignore[arg-type]

    def head_changed_at(self, repo: str, pr: int) -> str:
        self.calls.append(("head", pr))
        if self._head_changed is None:
            raise gate.GateError("could not determine when the pull request's head last changed")
        return self._head_changed

    def role(self, repo: str, user: str) -> str:
        self.calls.append(("role", user))
        return self._roles.get(user, "write")


def _run(trees: dict[str, gate.DictTree], github: _GitHub, *args: str, rebased: bool = True) -> int:
    return gate.main(
        ["--repo", "owner/repo", *args],
        tree_at=lambda rev: trees[rev],
        github=github,
        is_ancestor=lambda ancestor, rev: rebased,
    )


# --- the version sources --------------------------------------------------------------


def test_reads_the_request_and_result_versions_from_every_source() -> None:
    versions = gate.contract_versions(_tree("1.3"))
    assert versions == {
        ("request", "CONTRACT_VERSION"): "1.3",
        ("request", "assess_request.v1.schema.json"): "1.3",
        ("result", "CONTRACT_VERSION"): "1.3",
        ("result", "assess_result.v1.schema.json"): "1.3",
    }


def test_the_repository_sources_are_found_and_agree_with_the_models() -> None:
    # Guards the gate against going blind: a move or rename of a source leaves it
    # comparing nothing.
    versions = gate.contract_versions(gate.WorkTree(REPO_ROOT))
    assert {surface for surface, _ in versions} == {"request", "result"}
    assert len(versions) == 4
    assert set(versions.values()) == {CONTRACT_VERSION}


def test_a_major_adds_the_next_schema_file_and_the_highest_one_counts() -> None:
    versions = gate.contract_versions(_tree("2.0", schema_major=2))
    assert versions[("request", "assess_request.v2.schema.json")] == "2.0"
    assert versions[("result", "assess_result.v2.schema.json")] == "2.0"


def test_a_source_missing_on_one_side_is_not_compared() -> None:
    base = gate.DictTree({MODELS: _models("1.3")})
    assert gate.major_moves(base, _tree("2.0", schema_major=2)) == [
        gate.Move("request", "CONTRACT_VERSION", "1.3", "2.0"),
        gate.Move("result", "CONTRACT_VERSION", "1.3", "2.0"),
    ]


def test_an_unreadable_version_stops_the_check() -> None:
    with pytest.raises(gate.GateError):
        gate.contract_versions(_tree("one.three"))


# --- the decision ---------------------------------------------------------------------


def test_moved_without_the_label_fails(capsys) -> None:
    github = _GitHub()
    code = _run(
        {"base": _tree("1.3"), "head": _tree("2.0", schema_major=2)},
        github,
        "--base",
        "base",
        "--head",
        "head",
        "--pr",
        "7",
    )
    out = capsys.readouterr().out
    assert code == 1
    assert f"::error::{MESSAGE}" in out
    assert "request" in out and "result" in out and "1.3 -> 2.0" in out


def test_moved_with_the_label_passes(capsys) -> None:
    code = _run(
        {"base": _tree("1.3"), "head": _tree("2.0", schema_major=2)},
        _GitHub([LABEL]),
        "--base",
        "base",
        "--head",
        "head",
        "--pr",
        "7",
    )
    assert code == 0
    assert "::error::" not in capsys.readouterr().out


@pytest.mark.parametrize("labels", [["Maintainer Decision"], ["bug", "contracts"], []])
def test_moved_with_any_other_label_fails(labels: list[str]) -> None:
    # Only the dedicated label approves: the Maintainer Decision label (on, or removed
    # earlier) never does.
    code = _run(
        {"base": _tree("1.3"), "head": _tree("2.0")},
        _GitHub(labels),
        "--base",
        "base",
        "--head",
        "head",
        "--pr",
        "7",
    )
    assert code == 1


def test_the_client_reads_labels_the_timeline_and_roles_from_the_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The current labels, the timeline (who applied a label and when, and when the head
    last changed) and that user's repository role, all from the API, never the payload."""
    calls: list[tuple[str, ...]] = []
    timeline = (
        f"committed\t\t\t2026-11-12T08:00:00Z\n"
        f"labeled\t{LABEL}\tmaintainer\t2026-11-12T09:30:00Z\n"
        f"head_ref_force_pushed\t\tauthor\t2026-11-12T09:00:00Z\n"
        f"unlabeled\tbug\tsomeone\t2026-11-12T09:40:00Z\n"
    )

    def api(*args: str) -> str:
        calls.append(args)
        if args[-1] == ".labels[].name":
            return f"bug\n{LABEL}\n"
        if "timeline" in " ".join(args):
            return timeline
        if args[-1] == ".role_name":
            return "maintain\n"
        return "2\n"

    monkeypatch.setattr(gate.GhCli, "_api", staticmethod(api))
    client = gate.GhCli()
    assert client.labels("o/r", 7) == ["bug", LABEL]
    assert client.commit_count("o/r", 7) == 2
    assert client.label_events("o/r", 7) == [
        ("labeled", LABEL, "maintainer", "2026-11-12T09:30:00Z"),
        ("unlabeled", "bug", "someone", "2026-11-12T09:40:00Z"),
    ]
    assert client.head_changed_at("o/r", 7) == "2026-11-12T09:00:00Z"
    assert client.role("o/r", "maintainer") == "maintain"
    assert calls[2][:2] == ("--paginate", "repos/o/r/issues/7/timeline")
    assert len([c for c in calls if "timeline" in " ".join(c)]) == 1  # read once
    assert calls[-1][0] == "repos/o/r/collaborators/maintainer/permission"


def test_a_timeline_with_no_head_change_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(gate.GhCli, "_api", staticmethod(lambda *a: f"labeled\t{LABEL}\tm\tT\n"))
    with pytest.raises(gate.GateError, match="could not determine when the pull request's head"):
        gate.GhCli().head_changed_at("o/r", 7)


def test_a_user_the_api_does_not_know_is_never_a_maintainer(monkeypatch) -> None:
    def api(*args: str) -> str:
        raise gate.GateError("404 Not Found")

    monkeypatch.setattr(gate.GhCli, "_api", staticmethod(api))
    assert gate.GhCli().role("o/r", "stranger") == "none"


def test_a_major_move_of_the_result_alone_fails_without_the_label(capsys) -> None:
    head = _tree("1.3", result="2.0")
    code = _run(
        {"base": _tree("1.3"), "head": head},
        _GitHub(),
        "--base",
        "base",
        "--head",
        "head",
        "--pr",
        "7",
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "result" in out and "assess_result.v1.schema.json" in out
    assert "assess_request" not in out


def test_unmoved_passes_without_asking_github() -> None:
    github = _GitHub()
    code = _run(
        {"base": _tree("1.3"), "head": _tree("1.3")},
        github,
        "--base",
        "base",
        "--head",
        "head",
        "--pr",
        "7",
    )
    assert code == 0
    assert github.calls == []


def test_an_assess_minor_move_needs_the_minor_label(capsys) -> None:
    """#2176: every version move needs a maintainer's label, the assess() MINOR too."""
    trees = {"base": _tree("1.3"), "head": _tree("1.4")}
    args = ("--base", "base", "--head", "head", "--pr", "7")
    assert _run(trees, _GitHub(), *args) == 1
    assert f"::error::{MINOR_MESSAGE}" in capsys.readouterr().out
    assert _run(trees, _GitHub([MINOR]), *args) == 0


def test_moved_with_no_pull_request_to_read_fails() -> None:
    code = _run(
        {"base": _tree("1.3"), "head": _tree("2.0")}, _GitHub(), "--base", "base", "--head", "head"
    )
    assert code == 1


def test_a_git_error_exits_2() -> None:
    def broken(rev: str) -> gate.DictTree:
        raise gate.GateError(f"bad revision {rev}")

    assert gate.main(["--base", "x", "--head", "y"], tree_at=broken, github=_GitHub()) == 2


# --- the merge queue ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ref", "number"),
    [
        ("refs/heads/gh-readonly-queue/main/pr-1648-" + "ab12" * 10, 1648),
        ("gh-readonly-queue/main/pr-7-abc", 7),
        ("refs/heads/main", None),
        ("", None),
    ],
)
def test_the_pull_request_comes_from_the_merge_group_ref(ref: str, number: int | None) -> None:
    assert gate.pr_from_merge_group_ref(ref) == number


QUEUE_REF = "refs/heads/gh-readonly-queue/main/pr-9-" + "cd34" * 10


def test_merge_group_checks_the_entrys_own_commits_only() -> None:
    # An earlier entry in the same group moved the MAJOR (its own run checked it);
    # this entry's two rebased commits move nothing.
    trees = {"base": _tree("1.3"), "head": _tree("2.0"), "head~2": _tree("2.0")}
    github = _GitHub(commits=2)
    code = _run(trees, github, "--base", "base", "--head", "head", "--merge-group-ref", QUEUE_REF)
    assert code == 0
    assert github.calls == [("commits", 9)]


def test_merge_group_entry_that_moved_it_needs_the_label_now() -> None:
    trees = {"base": _tree("1.3"), "head": _tree("2.0"), "head~1": _tree("1.3")}
    assert (
        _run(trees, _GitHub(), "--base", "base", "--head", "head", "--merge-group-ref", QUEUE_REF)
        == 1
    )
    assert (
        _run(
            trees,
            _GitHub([LABEL]),
            "--base",
            "base",
            "--head",
            "head",
            "--merge-group-ref",
            QUEUE_REF,
        )
        == 0
    )


def test_merge_group_with_nothing_moved_asks_nothing() -> None:
    github = _GitHub()
    trees = {"base": _tree("1.3"), "head": _tree("1.3")}
    assert (
        _run(trees, github, "--base", "base", "--head", "head", "--merge-group-ref", QUEUE_REF) == 0
    )
    assert github.calls == []


def test_merge_group_stops_when_the_entrys_commits_reach_below_the_base(capsys) -> None:
    # A squash queue (one commit for a PR of three) would make HEAD~3 older than the
    # group's base: the check stops instead of comparing the wrong range.
    trees = {"base": _tree("1.3"), "head": _tree("2.0"), "head~3": _tree("1.3")}
    github = _GitHub([LABEL], commits=3)
    code = _run(
        trees,
        github,
        "--base",
        "base",
        "--head",
        "head",
        "--merge-group-ref",
        QUEUE_REF,
        rebased=False,
    )
    assert code == 2
    assert "REBASE" in capsys.readouterr().out


# --- reading git revisions ------------------------------------------------------------


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(  # nosec B603 B607 -- fixed argv in a scratch repository
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def test_git_tree_reads_each_revision(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    for version, schema_major in (("1.3", 1), ("2.0", 2)):
        for rel, text in _tree(version, schema_major=schema_major).files.items():
            path = tmp_path / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-q", "-m", version)
    moves = gate.major_moves(gate.GitTree("HEAD~1", tmp_path), gate.GitTree("HEAD", tmp_path))
    assert {(m.surface, m.base, m.head) for m in moves} == {
        ("request", "1.3", "2.0"),
        ("result", "1.3", "2.0"),
    }
    with pytest.raises(gate.GateError):
        gate.contract_versions(gate.GitTree("no-such-rev", tmp_path))


def test_git_is_ancestor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _git(tmp_path, "init", "-q")
    for n in range(2):
        (tmp_path / "f").write_text(str(n))
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-q", "-m", str(n))
    monkeypatch.chdir(tmp_path)
    assert gate.git_is_ancestor("HEAD~1", "HEAD")
    assert not gate.git_is_ancestor("HEAD", "HEAD~1")
    with pytest.raises(gate.GateError):
        gate.git_is_ancestor("no-such-rev", "HEAD")


# --- the workflow ---------------------------------------------------------------------


def _workflow() -> dict[Any, Any]:
    loaded: dict[Any, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return loaded


def _triggers() -> dict[str, Any]:
    wf = _workflow()
    triggers: dict[str, Any] = wf.get("on") or wf[True]  # PyYAML reads `on:` as True
    return triggers


def test_runs_on_every_push_and_label_change_and_in_the_merge_queue() -> None:
    triggers = _triggers()
    assert {"opened", "reopened", "synchronize", "labeled", "unlabeled"} <= set(
        triggers["pull_request"]["types"]
    )
    assert "merge_group" in triggers
    # No path filter: a required check must report on every pull request.
    assert "paths" not in (triggers["pull_request"] or {})


def test_the_required_check_has_a_stable_job_name_and_runs_the_script() -> None:
    jobs = _workflow()["jobs"]
    assert list(jobs) == ["contract-major-gate"]
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "ci/contract_major_gate.py" in text
    assert "--merge-group-ref" in text and "--pr" in text


def test_reads_labels_with_a_read_only_token_not_the_stale_payload() -> None:
    # A rerun replays the original payload; the script asks the API for the
    # current labels, so a rerun after a label change sees the change.
    wf = _workflow()
    assert wf["permissions"] == {"contents": "read", "issues": "read", "pull-requests": "read"}
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "GH_TOKEN" in text
    assert "pull_request.labels" not in text


def test_only_the_newest_run_per_pr_counts() -> None:
    concurrency = _workflow()["concurrency"]
    assert "github.event.pull_request.number" in concurrency["group"]
    assert "github.ref" in concurrency["group"]
    assert concurrency["cancel-in-progress"] is True


# --- assess() MAJOR and MINOR approved by a maintainer; internal contracts free (#2176)

CONTRACT = '''"""A contract.

Version History:
- 1.0 (2026-01-01): Initial version.
- {version} (2026-02-01): Changed.
"""

from pydantic import BaseModel, Field


class Thing(BaseModel):
    contract_version: str = Field(default="{version}", pattern=r"^\\d+\\.\\d+$")
'''
PENDING = 'contract = "m.Thing"\nkind = "{kind}"\nreason = "r"\n'
ARGS = ("--base", "base", "--head", "head", "--pr", "7")


def _internal(version: str = "1.4", pending: dict[str, str] | None = None) -> gate.DictTree:
    """``_tree()`` (assess() unmoved) plus an internal contract at ``version``."""
    files = dict(_tree().files)
    files["src/contracts/m.py"] = CONTRACT.format(version=version)
    for name, kind in (pending or {}).items():
        files[f"src/contracts/pending/{name}"] = PENDING.format(kind=kind)
    return gate.DictTree(files)


INTERNAL_HEADS = {
    "resolved-minor": _internal("1.5"),
    "resolved-major": _internal("2.0"),
    "pending-minor": _internal(pending={"m.Thing.x.toml": "minor"}),
    "pending-major": _internal(pending={"m.Thing.x.toml": "major"}),
}
ASSESS_MINOR = {"base": _tree("1.3"), "head": _tree("1.4")}
ASSESS_MAJOR = {"base": _tree("1.3"), "head": _tree("2.0", schema_major=2)}


@pytest.mark.passes_on_main  # main's gate never read internal contracts either
@pytest.mark.parametrize("head", list(INTERNAL_HEADS.values()), ids=list(INTERNAL_HEADS))
def test_an_internal_contract_bump_needs_no_label(head) -> None:
    """Internal contracts' bumps are pending files the release resolves (ADR-008
    Decision 8); no label, and no GitHub call."""
    github = _GitHub()
    assert _run({"base": _internal(), "head": head}, github, *ARGS) == 0
    assert github.calls == []


def test_an_assess_minor_with_the_minor_label_by_a_maintainer_passes(capsys) -> None:
    assert _run(ASSESS_MINOR, _GitHub([MINOR]), *ARGS) == 0
    out = capsys.readouterr().out
    assert "approved: the 'contract-minor' label, applied by maintainer maintainer" in out


@pytest.mark.parametrize("label", [MINOR, LABEL, LEGACY])
def test_a_label_a_non_maintainer_applied_fails(capsys, label) -> None:
    trees = ASSESS_MINOR if label == MINOR else ASSESS_MAJOR
    assert _run(trees, _GitHub([label], by="contributor"), *ARGS) == 1
    out = capsys.readouterr().out
    assert f"the '{label}' label was applied by contributor, who is not a maintainer" in out


def test_the_latest_labeled_event_decides_who_applied_it() -> None:
    """A maintainer's label removed and re-applied by a contributor no longer counts; a
    contributor's label re-applied by a maintainer does."""
    readded_by_contributor = [
        ("labeled", MINOR, "maintainer"),
        ("unlabeled", MINOR, "maintainer"),
        ("labeled", MINOR, "contributor"),
    ]
    assert _run(ASSESS_MINOR, _GitHub([MINOR], events=readded_by_contributor), *ARGS) == 1
    readded_by_maintainer = [("labeled", MINOR, "contributor"), ("labeled", MINOR, "owner")]
    assert _run(ASSESS_MINOR, _GitHub([MINOR], events=readded_by_maintainer), *ARGS) == 0


def test_a_label_removed_after_approval_drops_it() -> None:
    events = [("labeled", MINOR, "maintainer"), ("unlabeled", MINOR, "maintainer")]
    assert _run(ASSESS_MINOR, _GitHub([], events=events), *ARGS) == 1


def test_an_assess_major_needs_the_major_label_and_the_minor_label_is_not_enough(capsys) -> None:
    assert _run(ASSESS_MAJOR, _GitHub([MINOR]), *ARGS) == 1
    assert f"::error::{MESSAGE}" in capsys.readouterr().out
    assert _run(ASSESS_MAJOR, _GitHub([LABEL]), *ARGS) == 0


def test_the_legacy_label_still_approves_an_assess_major(capsys) -> None:
    assert _run(ASSESS_MAJOR, _GitHub([LEGACY]), *ARGS) == 0
    assert f"approved: the '{LEGACY}' label, applied by maintainer" in capsys.readouterr().out


def test_an_assess_major_and_minor_need_both_labels() -> None:
    trees = {"base": _tree("1.3"), "head": _tree("1.3", request="2.0", result="1.4")}
    assert _run(trees, _GitHub([LABEL]), *ARGS) == 1
    assert _run(trees, _GitHub([MINOR]), *ARGS) == 1
    assert _run(trees, _GitHub([LABEL, MINOR]), *ARGS) == 0


@pytest.mark.passes_on_main  # what needs no label stays so
def test_a_patch_passes_without_asking_github() -> None:
    github = _GitHub()
    trees = {"base": _tree("1.3.0"), "head": _tree("1.3.1")}
    assert _run(trees, github, *ARGS) == 0
    assert github.calls == []


def test_a_removed_major_label_does_not_approve_while_another_label_is_on(capsys) -> None:
    """Review round 2: a maintainer applied and removed ``contract-major``; ``contract-minor``
    is on. The MAJOR must stay unapproved, so the label has to be on the PR now, not
    only in the events."""
    trees = {"base": _tree("1.3"), "head": _tree("1.3", request="2.0", result="1.4")}
    events = [
        ("labeled", LABEL, "maintainer"),
        ("unlabeled", LABEL, "maintainer"),
        ("labeled", MINOR, "maintainer"),
    ]  # applied, then removed: a recorded label history
    assert _run(trees, _GitHub([MINOR], events=events), *ARGS) == 1
    assert f"::error::{MESSAGE}" in capsys.readouterr().out


def test_a_role_lookup_error_fails_closed_and_says_so(monkeypatch, capsys) -> None:
    """A 404 is a user with no role; any other API error (403, rate limit, 5xx) cannot
    tell, so the check stops (exit 2) and says it could not verify, not that a real
    maintainer is not one."""

    def api(*args: str) -> str:
        raise gate.GateError("gh api ...: HTTP 403: API rate limit exceeded")

    monkeypatch.setattr(gate.GhCli, "_api", staticmethod(api))
    with pytest.raises(gate.GateError, match="could not verify the labeler's role"):
        gate.GhCli().role("o/r", "maintainer")

    class Flaky(_GitHub):
        def role(self, repo: str, user: str) -> str:
            raise gate.GateError("could not verify the labeler's role (API error: HTTP 502)")

    assert _run(ASSESS_MINOR, Flaky([MINOR]), *ARGS) == 2
    out = capsys.readouterr().out
    assert "could not verify the labeler's role (API error: HTTP 502)" in out
    assert "is not a maintainer" not in out


# --- an approval resets on every new push (maintainer decision, #2183) ------------------


def test_a_label_applied_before_the_latest_push_no_longer_counts(capsys) -> None:
    before = [("labeled", MINOR, "maintainer", "2026-11-12T08:00:00Z")]
    assert _run(ASSESS_MINOR, _GitHub([MINOR], events=before), *ARGS) == 1
    out = capsys.readouterr().out
    assert "before the pull request's latest push" in out and "re-applies it" in out
    after = [("labeled", MINOR, "maintainer", "2026-11-12T09:00:01Z")]
    assert _run(ASSESS_MINOR, _GitHub([MINOR], events=after), *ARGS) == 0


def test_a_label_re_applied_after_the_push_counts_again(capsys) -> None:
    events = [
        ("labeled", MINOR, "maintainer", "2026-11-12T08:00:00Z"),
        ("unlabeled", MINOR, "maintainer", "2026-11-12T09:30:00Z"),
        ("labeled", MINOR, "maintainer", "2026-11-12T09:31:00Z"),
    ]
    assert _run(ASSESS_MINOR, _GitHub([MINOR], events=events), *ARGS) == 0
    assert "approved: the 'contract-minor' label, applied by maintainer" in capsys.readouterr().out


def test_an_unknown_head_change_time_fails_closed(capsys) -> None:
    assert _run(ASSESS_MINOR, _GitHub([MINOR], head_changed=None), *ARGS) == 2
    assert "could not determine when the pull request's head" in capsys.readouterr().out


def test_the_workflow_runs_the_base_branchs_copy_of_the_gate() -> None:
    """A pull request cannot change what the gate decides: the step takes the script
    from the base commit and never runs the checked-out copy."""
    steps = _workflow()["jobs"]["contract-major-gate"]["steps"]
    (run,) = [s["run"] for s in steps if "contract_major_gate.py" in s.get("run", "")]
    assert 'git show "$base:ci/contract_major_gate.py" > "$gate"' in run
    assert 'python3 "$gate"' in run and "python3 ci/contract_major_gate.py" not in run
    assert 'base="$(git rev-parse HEAD^1)"' in run and 'base="$MERGE_GROUP_BASE"' in run


@pytest.mark.passes_on_main  # the gate was standard library only already
def test_the_gate_imports_only_the_standard_library() -> None:
    """The workflow copies this one file from the base, so it can import nothing of the
    repository's."""
    import ast as _ast
    import sys as _sys

    tree = _ast.parse((REPO_ROOT / "ci/contract_major_gate.py").read_text())
    names = {
        a.name.split(".")[0] for n in _ast.walk(tree) if isinstance(n, _ast.Import) for a in n.names
    }
    names |= {
        (n.module or "").split(".")[0] for n in _ast.walk(tree) if isinstance(n, _ast.ImportFrom)
    }
    assert names - {"__future__"} <= set(_sys.stdlib_module_names)
