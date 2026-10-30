# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ci/contract_major_gate.py: an assess() contract MAJOR move needs a maintainer decision (#1648)."""

from __future__ import annotations

import json
import subprocess  # nosec B404 -- builds a scratch git repository with fixed argv
from pathlib import Path
from typing import Any

import pytest
import yaml

from ci import contract_major_gate as gate
from src.assessment import CONTRACT_VERSION

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "contract-major-gate.yml"
LABEL = "Contract Major Approved"
MESSAGE = (
    "moving the assess() contract MAJOR breaks callers; it needs a maintainer's approval "
    "(label Contract Major Approved)"
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


class _GitHub:
    """A fake GitHub client: the PR's current labels and its commit count."""

    def __init__(self, labels: list[str] | None = None, commits: int = 1) -> None:
        self._labels = labels or []
        self._commits = commits
        self.calls: list[tuple[str, int]] = []

    def labels(self, repo: str, pr: int) -> list[str]:
        self.calls.append(("labels", pr))
        return list(self._labels)

    def commit_count(self, repo: str, pr: int) -> int:
        self.calls.append(("commits", pr))
        return self._commits


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


def test_the_label_is_read_now_and_no_history_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    # A label removed earlier, or any other event, never counts: the client asks only
    # for the issue's current labels (and, in the merge queue, its commit count).
    calls: list[tuple[str, ...]] = []

    def api(*args: str) -> str:
        calls.append(args)
        return f"bug\n{LABEL}\n" if args[-1] == ".labels[].name" else "2\n"

    monkeypatch.setattr(gate.GhCli, "_api", staticmethod(api))
    client = gate.GhCli()
    assert gate.decision(client.labels("o/r", 7)) is not None
    assert client.commit_count("o/r", 7) == 2
    assert calls == [
        ("repos/o/r/issues/7", "--jq", ".labels[].name"),
        ("repos/o/r/pulls/7", "--jq", ".commits"),
    ]
    assert not hasattr(client, "label_events")
    assert gate.decision([]) is None and gate.decision(["Maintainer Decision"]) is None


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


def test_a_minor_move_passes() -> None:
    github = _GitHub()
    code = _run(
        {"base": _tree("1.3"), "head": _tree("1.4")},
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
