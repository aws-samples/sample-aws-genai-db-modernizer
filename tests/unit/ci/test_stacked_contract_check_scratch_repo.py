# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#1846: the contract checks on a stacked history, with the real scripts.

A scratch clone of this repository gets branch A (a resolved bump of one contract plus a
field) and branch B on top of A with its own pending file for the same contract, the
#1640 shape. Since #2176 only a MAJOR is resolved in its pull request (a MINOR waits for
the release), so A's bump is a MAJOR. A fake ``gh`` lists A as an open pull request. The real
``ci/pre_merge.py stacked``, ``ci/pre_merge.py contracts`` and
``resolve_contract_versions.py --check --against`` run, and so does the ``run:`` text of
the ``Check pending contract bumps`` step read from ``.github/workflows/ci.yml``, on a
merge commit like ``refs/pull/N/merge`` and on the merge group's shape. No network.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
CONTRACT = "reality_check_marker.RealityCheckMarker"
MODULE = "src/contracts/reality_check_marker.py"
STEP = "Check pending contract bumps"

FAKE_GH = """#!/bin/sh
echo "$@" >> "$GH_LOG"
case "$1 $2" in
  "pr list") cat "$GH_PRS" ;;
  "pr view") exit 1 ;;
  "api repos/o/r/compare/main"*)
    sha="${2##*...}"
    printf '{"commits": ['
    git rev-list --reverse "main..$sha" | sed 's/.*/{"sha": "&"}/' | paste -sd, -
    printf ']}'
    ;;
  *) exit 1 ;;
esac
"""
# `uv run python X` -> this interpreter, so the step's text runs unchanged and offline.
FAKE_UV = '#!/bin/sh\nshift\nshift\nexec "$PY" "$@"\n'


class Scratch:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.repo = root / "repo"
        self.bin = root / "bin"
        self.bin.mkdir()
        for name, text in (("gh", FAKE_GH), ("uv", FAKE_UV)):
            path = self.bin / name
            path.write_text(text)
            path.chmod(path.stat().st_mode | stat.S_IEXEC)
        self.log = root / "gh.log"
        self.log.write_text("")
        self.prs = root / "prs.json"
        self.set_prs([])
        self.git("clone", "-q", "--local", "--no-hardlinks", str(REPO), str(self.repo), cwd=root)
        self.git("checkout", "-q", "-B", "main")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")
        # The clone has the committed tree; overlay the working tree's scripts so an
        # uncommitted edit is what runs (a no-op on a clean checkout).
        for folder in ("ci", "scripts", "src/contracts", "tests/contract"):
            shutil.copytree(
                REPO / folder,
                self.repo / folder,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__", "pending"),
            )
        self.git("add", "-A")
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=self.repo).returncode:
            self.git("commit", "-q", "-m", "overlay")
        # A branch that commits a pending bump would carry it into this scratch main, and
        # the tests would resolve it with their own (#2203). Start from main as a release
        # leaves it instead: no pending file, versions and snapshots resolved (a no-op on
        # main, which never holds a pending file at release time).
        if list((self.repo / "src/contracts/pending").glob("*.toml")):
            proc = self.py("scripts/resolve_contract_versions.py", "--release", "--issue", "1")
            assert proc.returncode == 0, proc.stderr + proc.stdout
        assert not list((self.repo / "src/contracts/pending").glob("*.toml"))

    def git(self, *args: str, cwd: Path | None = None) -> str:
        proc = subprocess.run(
            ["git", *args], cwd=cwd or self.repo, capture_output=True, text=True, check=False
        )
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.strip()

    def env(self, **extra: str) -> dict[str, str]:
        return {
            **os.environ,
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "PY": sys.executable,
            "GH_LOG": str(self.log),
            "GH_PRS": str(self.prs),
            "PYTHONPATH": str(self.repo),
            **extra,
        }

    def run(self, argv: list[str], **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv, cwd=self.repo, env=self.env(**env), capture_output=True, text=True, check=False
        )

    def py(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self.run([sys.executable, *args])

    def set_prs(self, rows: list[tuple[int, str, str]], **flags: bool) -> None:
        self.prs.write_text(
            json.dumps(
                [
                    {
                        "number": n,
                        "headRefName": ref,
                        "headRefOid": oid,
                        "isDraft": flags.get("draft", False),
                        "isCrossRepository": flags.get("fork", False),
                    }
                    for n, ref, oid in rows
                ]
            )
        )

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def add_field(self, name: str) -> None:
        path = self.repo / MODULE
        path.write_text(
            path.read_text() + f'    {name}: str | None = Field(default=None, description="x")\n'
        )

    def declare(self, reason: str, kind: str = "minor") -> None:
        proc = self.py("scripts/bump_contract.py", CONTRACT, kind, "--reason", reason)
        assert proc.returncode == 0, proc.stderr + proc.stdout

    def resolve(self) -> None:
        proc = self.py("scripts/resolve_contract_versions.py", "--base", "main")
        assert proc.returncode == 0, proc.stderr + proc.stdout


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    """main, A (resolved MAJOR + field) and B on A (its own pending file for the same
    contract)."""
    s = Scratch(tmp_path_factory.mktemp("stacked"))
    s.git("checkout", "-q", "-b", "a")
    s.add_field("scratch_a")
    s.declare("Added scratch_a.", "major")
    s.resolve()
    s.a = s.commit("a: bump resolved")
    s.git("checkout", "-q", "-b", "b")
    s.declare("Added scratch_b.")
    s.b = s.commit("b: pending bump")
    s.main = s.git("rev-parse", "main")
    return s


def _against(s: Scratch, ref: str) -> subprocess.CompletedProcess[str]:
    return s.py("scripts/resolve_contract_versions.py", "--check", "--against", ref)


def _step_text() -> str:
    workflow = yaml.safe_load((REPO / ".github/workflows/ci.yml").read_text())
    steps = workflow["jobs"]["validate-contracts"]["steps"]
    return str(next(st["run"] for st in steps if st.get("name") == STEP))


def _merge(s: Scratch, branch: str) -> str:
    """The shape of refs/pull/N/merge: main with the branch merged in, detached."""
    s.git("checkout", "-q", "--detach", "main")
    s.git("merge", "-q", "--no-ff", "-m", "merge", branch)
    return s.git("rev-parse", "HEAD")


def _run_step(s: Scratch, **env: str) -> subprocess.CompletedProcess[str]:
    base = {"REPO": "o/r", "HEAD_REF": "b", "PR_NUMBER": "102", "DETECT_TOKEN": "t"}
    return s.run(["bash", "-e", "-c", _step_text()], **{**base, **env})


def test_local_checks_pass_against_the_stack_and_fail_against_main(stack):
    """The #1640 case: B carries A's resolved version next to its own pending file."""
    s = stack
    s.git("checkout", "-q", "b")
    s.set_prs([(101, "a", s.a)])
    found = s.py("ci/pre_merge.py", "stacked", "--main-ref", "main")
    assert found.stdout.strip() == f"stacked 101 {s.a}"
    assert s.py("ci/pre_merge.py", "contracts", s.a, MODULE).returncode == 0
    assert _against(s, s.a).returncode == 0
    wrong = s.py("ci/pre_merge.py", "contracts", "main", MODULE)
    assert wrong.returncode == 1 and "keep one" in wrong.stdout
    wrong = _against(s, "main")
    assert wrong.returncode == 1 and "moved the version by hand" in wrong.stdout


@pytest.mark.passes_on_main  # #2176 only renames the step's env flag
def test_the_ci_step_compares_a_pull_request_with_the_stacked_on_head(stack):
    s = stack
    merge = _merge(s, "b")
    s.set_prs([(101, "a", s.a)])
    got = _run_step(s, EVENT="pull_request", PENDING_MAJOR="")
    assert got.returncode == 0, got.stdout + got.stderr
    assert "stacked on #101: re-check after #101 merges" in got.stdout
    assert "not checked here" in got.stdout
    assert s.git("rev-parse", "HEAD") == merge


@pytest.mark.passes_on_main
def test_the_merge_queue_always_compares_with_main(stack):
    """A merge group never looks for a stack, whatever gh would say: it asks gh nothing,
    compares with main and refuses the pending file."""
    s = stack
    _merge(s, "b")
    s.set_prs([(101, "a", s.a)])
    s.log.write_text("")
    got = _run_step(
        s, EVENT="merge_group", MERGE_GROUP_BASE=s.main, PENDING_MAJOR="--refuse-pending-major"
    )
    assert got.returncode == 1
    assert "moved the version by hand" in got.stdout  # compared with main, not with A
    assert "stacked on" not in got.stdout + got.stderr
    assert s.log.read_text() == ""  # no gh call at all


@pytest.mark.passes_on_main
def test_a_push_compares_with_the_commit_before_it(stack):
    s = stack
    _merge(s, "b")
    s.set_prs([(101, "a", s.a)])
    s.log.write_text("")
    got = _run_step(s, EVENT="push", PUSH_BEFORE=s.main, PENDING_MAJOR="--refuse-pending-major")
    assert got.returncode == 1 and s.log.read_text() == ""


@pytest.mark.passes_on_main
def test_when_a_moves_there_is_no_stack_and_the_check_compares_with_main(stack):
    """B took A's old head; A has since moved on, so its open head is not among B's commits."""
    s = stack
    s.git("checkout", "-q", "b")
    s.set_prs([(101, "a", "f" * 40)])
    assert s.py("ci/pre_merge.py", "stacked", "--main-ref", "main").stdout == ""
    _merge(s, "b")
    got = _run_step(s, EVENT="pull_request", PENDING_MAJOR="")
    assert got.returncode == 1 and "moved the version by hand" in got.stdout
    assert "stacked on" not in got.stdout


@pytest.mark.passes_on_main
@pytest.mark.parametrize("flags", [{"draft": True}, {"fork": True}])
def test_a_draft_or_fork_pr_is_not_a_stack_base(stack, flags):
    s = stack
    s.git("checkout", "-q", "b")
    s.set_prs([(101, "a", s.a)], **flags)
    assert s.py("ci/pre_merge.py", "stacked", "--main-ref", "main").stdout == ""
    _merge(s, "b")
    got = _run_step(s, EVENT="pull_request", PENDING_MAJOR="")
    assert got.returncode == 1


@pytest.mark.passes_on_main
def test_a_throwaway_pr_cannot_hide_a_shape_change(stack):
    """D is a throwaway draft pull request holding a shape change with no bump; C merges it.
    Were D the base, the change inside its head would be skipped; as a draft it is not."""
    s = stack
    s.git("checkout", "-q", "-b", "d", "main")
    s.add_field("scratch_d")
    d = s.commit("d: shape change, no bump")
    s.git("checkout", "-q", "-b", "c", "main")
    s.git("merge", "-q", "--no-ff", "-m", "merge d", "d")
    c = s.git("rev-parse", "HEAD")
    s.set_prs([(1999, "d", d)], draft=True)
    assert s.py("ci/pre_merge.py", "stacked", "--main-ref", "main").stdout == ""
    assert _against(s, "main").returncode == 1
    s.git("checkout", "-q", "--detach", "main")
    s.git("merge", "-q", "--no-ff", "-m", "merge", c)
    got = _run_step(s, EVENT="pull_request", PENDING_MAJOR="", HEAD_REF="c")
    assert got.returncode == 1 and "stacked on" not in got.stdout
    assert "error:" in got.stdout


@pytest.mark.passes_on_main  # #2176 only renames the step's env flag
def test_a_shape_change_with_no_bump_on_the_stack_still_fails(stack):
    """What the stacked comparison enforces: a change on top of A with neither a version
    move nor a pending bump fails against A as it does against main."""
    s = stack
    s.git("checkout", "-q", "-b", "e", s.a)
    s.add_field("scratch_e")
    e = s.commit("e: shape change, no bump")
    s.set_prs([(101, "a", s.a)])
    assert s.py("ci/pre_merge.py", "stacked", "--main-ref", "main").stdout.split()[1] == "101"
    got = _against(s, s.a)
    assert got.returncode == 1 and "version" in got.stdout
    s.git("checkout", "-q", "--detach", "main")
    s.git("merge", "-q", "--no-ff", "-m", "merge", e)
    got = _run_step(s, EVENT="pull_request", PENDING_MAJOR="", HEAD_REF="e")
    assert got.returncode == 1


@pytest.mark.passes_on_main  # #2176 only renames the step's env flag
def test_a_stacked_on_head_that_cannot_be_fetched_warns_and_uses_main(stack):
    s = stack
    s.git("checkout", "-q", "b")
    _merge(s, "b")
    s.set_prs([(101, "a", s.a)])
    # Hide A's commit from the scratch repo, as a head the job cannot fetch.
    unreachable = "0" * 40
    fake = s.root / "bin" / "gh2"
    fake.write_text(
        (s.bin / "gh")
        .read_text()
        .replace('git rev-list --reverse "main..$sha"', f"echo {unreachable}")
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    shutil.move(str(fake), str(s.bin / "gh"))
    s.prs.write_text(
        json.dumps(
            [
                {
                    "number": 101,
                    "headRefName": "a",
                    "headRefOid": unreachable,
                    "isDraft": False,
                    "isCrossRepository": False,
                }
            ]
        )
    )
    got = _run_step(s, EVENT="pull_request", PENDING_MAJOR="")
    assert "::warning::stacked on #101" in got.stdout
    assert "comparing with main" in got.stdout


def test_a_problem_inherited_from_the_stacked_on_pr_is_not_seen_on_the_stack(stack):
    """F changes a shape with no bump (and regenerates its snapshots, as a branch does); G sits
    on F and adds nothing. Against F, G passes:
    F's own check and the merge queue (which compares with main) are what catch it."""
    s = stack
    s.git("checkout", "-q", "-b", "f", "main")
    s.add_field("scratch_f")
    for script in ("contract_schema_snapshot.py", "contract_value_snapshot.py"):
        assert s.py(f"scripts/{script}", "--write").returncode == 0  # as a branch regenerates
    f = s.commit("f: shape change, no bump, snapshots regenerated")
    s.git("checkout", "-q", "-b", "g")
    (s.repo / "NOTE").write_text("g")
    s.commit("g: nothing contract-related")
    s.set_prs([(1500, "f", f)])
    assert s.py("ci/pre_merge.py", "stacked", "--main-ref", "main").stdout.split()[1] == "1500"
    assert _against(s, f).returncode == 0
    assert _against(s, "main").returncode == 1
