# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#2176: pending bumps wait on main and the release resolves them, with the real scripts.

A scratch clone of this repository (the ``Scratch`` helper of the #1846 stacked test)
gets two pull requests merged onto its main, each with a field and a pending MINOR for the
same contract (main never holds a pending MAJOR: the merge queue refuses one). The real ``resolve_contract_versions.py`` runs:
``--check --against`` on each pull request, the ``Check pending contract bumps`` step of
``.github/workflows/ci.yml`` on a merge group, and ``--release`` on main, which writes the
version, the Version History and both snapshots in one commit. No network.
"""

from __future__ import annotations

import json
import re

import pytest

from tests.unit.ci.test_stacked_contract_check_scratch_repo import (
    CONTRACT,
    MODULE,
    Scratch,
    _run_step,
)

PENDING = "src/contracts/pending"
SNAPSHOTS = ("tests/contract/schema_snapshot.json", "tests/contract/value_set_snapshot.json")


def _version(s: Scratch) -> str:
    match = re.search(r'contract_version: str = Field\(\s*(?:default=)?"(\d+\.\d+)"', _text(s))
    assert match
    return match.group(1)


def _text(s: Scratch) -> str:
    return (s.repo / MODULE).read_text()


def _check(s: Scratch, ref: str, *flags: str):
    return s.py("scripts/resolve_contract_versions.py", "--check", "--against", ref, *flags)


def _declare(s: Scratch, kind: str, reason: str, slug: str) -> None:
    proc = s.py("scripts/bump_contract.py", CONTRACT, kind, "--reason", reason, "--slug", slug)
    assert proc.returncode == 0, proc.stderr + proc.stdout


@pytest.fixture(scope="module")
def released(tmp_path_factory):
    """main, PR 1 and PR 2 (each a field + a pending MINOR) merged onto it."""
    s = Scratch(tmp_path_factory.mktemp("release"))
    s.start = s.git("rev-parse", "main")
    s.before = _version(s)
    s.add_field("release_one")
    _declare(s, "minor", "Added ``release_one``.", "one")
    s.one = s.commit("pr 1: field and pending minor")
    s.add_field("release_two")
    _declare(s, "minor", "Added ``release_two``.", "two")
    s.two = s.commit("pr 2: field and pending minor")
    return s


@pytest.mark.passes_on_main  # a PR event always accepted pending files; kept as a guard
def test_each_pull_request_passes_with_only_its_pending_file(released):
    s = released
    s.git("checkout", "-q", "--detach", s.one)
    got = _check(s, s.start)
    assert got.returncode == 0, got.stdout + got.stderr
    s.git("checkout", "-q", "--detach", s.two)
    got = _check(s, s.one)
    assert got.returncode == 0, got.stdout + got.stderr
    assert _version(s) == s.before  # nothing resolved in a pull request


def test_the_merge_queue_accepts_a_pending_minor_and_refuses_a_pending_major(released):
    """The step's own text, with the flag its env gives every event but a pull request."""
    s = released
    flag = {"PENDING_MAJOR": "--refuse-pending-major"}
    s.git("checkout", "-q", "--detach", s.start)
    s.git("merge", "-q", "--no-ff", "-m", "merge group", s.one)
    got = _run_step(s, EVENT="merge_group", MERGE_GROUP_BASE=s.start, **flag)
    assert got.returncode == 0, got.stdout + got.stderr
    s.git("checkout", "-q", "-B", "mq-major", s.one)
    _declare(s, "major", "Changed ``release_one``.", "mq-major")
    s.commit("mq: pending major")
    got = _run_step(s, EVENT="merge_group", MERGE_GROUP_BASE=s.one, **flag)
    assert got.returncode == 1 and "pending MAJOR" in got.stdout
    got = _run_step(s, EVENT="pull_request", PENDING_MAJOR="")
    assert "pending MAJOR" not in got.stdout


def test_a_further_change_without_its_own_pending_file_fails(released):
    """Main holds PR 1's pending MINOR; PR 3 adds another field and declares nothing. The
    committed-snapshot comparison is covered by main's pending file; the comparison with
    main's code is not."""
    s = released
    s.git("checkout", "-q", "-B", "three", s.one)
    s.add_field("release_three")
    s.commit("pr 3: field, no pending file")
    got = _check(s, s.one)
    assert got.returncode == 1
    assert "schema changed since main" in got.stdout and "release_three" in got.stdout
    _declare(s, "minor", "Added ``release_three``.", "three")
    s.commit("pr 3: declare it")
    assert _check(s, s.one).returncode == 0


def test_a_version_resolved_in_a_pull_request_is_refused(released):
    """The old per-PR resolution of a MINOR: the version, the history and the snapshots
    written by the PR. The release step is the only writer now."""
    s = released
    s.git("checkout", "-q", "-B", "hand", s.start)
    s.add_field("release_hand")
    _declare(s, "minor", "Added ``release_hand``.", "hand")
    s.commit("hand: field and pending minor")
    # What the per-PR resolve used to do here, done with the release step's code path.
    got = s.py("scripts/resolve_contract_versions.py", "--release", "--no-commit")
    assert got.returncode == 0, got.stdout + got.stderr
    s.commit("hand: resolved in the pull request")
    got = _check(s, s.start)
    assert got.returncode == 1
    assert f"{CONTRACT}: contract_version moved {s.before} -> " in got.stdout
    assert "by hand" in got.stdout


def test_a_snapshot_regenerated_for_a_pending_contract_is_refused(released):
    s = released
    s.git("checkout", "-q", "-B", "regen", s.start)
    s.add_field("release_regen")
    _declare(s, "minor", "Added ``release_regen``.", "regen")
    assert s.py("scripts/contract_schema_snapshot.py", "--write").returncode == 0
    s.commit("regen: snapshot written in the pull request")
    got = _check(s, s.start)
    assert got.returncode == 1
    assert f"tests/contract/schema_snapshot.json: {CONTRACT}" in got.stdout


def test_the_release_resolves_every_pending_file_in_one_commit(released):
    s = released
    s.git("checkout", "-q", "-B", "rel", s.two)
    got = s.py(
        "scripts/resolve_contract_versions.py",
        "--release",
        "--issue",
        "2176",
        "--date",
        "2026-11-12",
    )
    assert got.returncode == 0, got.stdout + got.stderr
    assert s.git("rev-parse", "HEAD~1") == s.two  # one commit on top of main
    major, minor = s.before.split(".")
    after = f"{major}.{int(minor) + 1}"
    assert _version(s) == after  # two MINORs for one contract: one MINOR
    assert not list((s.repo / PENDING).glob("*.toml"))
    entry = f"- {after} (2026-11-12): Added ``release_one``. Added ``release_two``."
    assert entry in " ".join(_text(s).split())
    changed = set(s.git("diff", "--name-only", "HEAD~1", "HEAD").splitlines())
    assert changed == {MODULE, *SNAPSHOTS} | {
        f"{PENDING}/{CONTRACT}.one.toml",
        f"{PENDING}/{CONTRACT}.two.toml",
    }
    message = s.git("log", "-1", "--format=%B")
    assert message.splitlines()[0] == "chore(contracts): resolve pending contract bumps at release"
    assert message.rstrip().endswith("Refs #2176")
    built = json.loads(s.py("scripts/contract_schema_snapshot.py").stdout)
    committed = json.loads((s.repo / SNAPSHOTS[0]).read_text())
    assert committed == built and committed[CONTRACT]["version"] == after
    # The guard reads the release commit as the release resolve (the release rule, not
    # the MAJOR one), not as a hand bump.
    got = _check(s, s.two, "--refuse-pending-major", "--require-resolved")
    assert got.returncode == 0, got.stdout + got.stderr
    nothing = s.py("scripts/resolve_contract_versions.py", "--release", "--issue", "1")
    assert nothing.returncode == 0 and "no pending contract bumps" in nothing.stdout


def test_the_release_refuses_a_dirty_contract_tree(released):
    s = released
    s.git("checkout", "-q", "-B", "dirty", s.two)
    (s.repo / MODULE).write_text(_text(s) + "\n")
    got = s.py("scripts/resolve_contract_versions.py", "--release", "--issue", "1")
    assert got.returncode == 1 and "uncommitted" in got.stderr
    s.git("checkout", "--", MODULE)


def test_an_unbumped_change_cannot_ride_on_a_release_resolve(released):
    """The review's attack: a PR commits a field with no pending file of its own, then runs
    the release resolve of main's pending MINORs in the same PR. The version rises, but a
    contract on the release path is compared at main's version, so the field fails."""
    s = released
    s.git("checkout", "-q", "-B", "ride", s.two)
    s.add_field("release_ride")
    s.commit("ride: field, no pending file")
    got = s.py("scripts/resolve_contract_versions.py", "--release", "--no-commit")
    assert got.returncode == 0, got.stdout + got.stderr
    s.commit("ride: release resolve in the pull request")
    got = _check(s, s.two)
    assert got.returncode == 1
    assert "schema changed since main" in got.stdout and "release_ride" in got.stdout
