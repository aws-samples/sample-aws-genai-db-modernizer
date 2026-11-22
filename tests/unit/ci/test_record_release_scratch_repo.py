# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#2472: release step 6 records the contract and catalog with no test edits.

A scratch clone of this repository (the ``Scratch`` helper of the #1846 test) gets a local
tag, and the real ``scripts/record_release.py`` runs against it. The files it writes are
the record: the released contract shape and version, the catalog format snapshot and the
tag's catalog fixture. No file of any test changes. No network, and no tag in this repository.
"""

from __future__ import annotations

import json
import sys

import pytest

from tests.unit.ci.test_stacked_contract_check_scratch_repo import Scratch

TAG = "v9.9.9"
RECORD = {
    "src/assessment/released_contract.json",
    "tests/contract/assess_released_since_1_3.json",
    "tests/contract/catalog_format_snapshot.json",
}
FIXTURE = f"tests/contract/fixtures/catalog_releases/{TAG}/"


@pytest.fixture()
def scratch(tmp_path_factory):
    s = Scratch(tmp_path_factory.mktemp("record"))
    s.git("tag", "-a", TAG, "-m", "scratch tag")
    return s


def _changed(s: Scratch) -> list[str]:
    out = s.run(["git", "status", "--porcelain", "--untracked-files=all"]).stdout
    return sorted(line[3:] for line in out.splitlines())


def test_step_6_writes_the_record_and_changes_no_test_file(scratch):
    s = scratch
    proc = s.py("scripts/record_release.py", TAG)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    changed = _changed(s)
    assert changed, "the record wrote nothing"
    for path in changed:
        assert path in RECORD or path.startswith(FIXTURE), path
    assert not [p for p in changed if p.startswith("tests/") and p.endswith(".py")]
    assert any(p.startswith(FIXTURE) for p in changed)
    catalog = json.loads((s.repo / "tests/contract/catalog_format_snapshot.json").read_text())
    assert catalog["released"] == TAG
    contract = json.loads((s.repo / "tests/contract/assess_released_since_1_3.json").read_text())
    assert contract["released"] == TAG
    assert (
        json.loads((s.repo / "src/assessment/released_contract.json").read_text())["released"]
        == TAG
    )


def test_the_tests_pass_on_the_recorded_tree_with_no_edit(scratch):
    s = scratch
    assert s.py("scripts/record_release.py", TAG).returncode == 0
    before = _changed(s)
    run = s.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:xdist",
            "-p",
            "no:cacheprovider",
            "tests/contract/test_catalog_old_releases_load.py",
            "tests/contract/test_catalog_format_snapshot.py",
            "tests/contract/test_assess_contract_1_4.py",
            "tests/contract/test_assess_retired_versions.py",
        ]
    )
    assert run.returncode == 0, run.stdout[-3000:] + run.stderr[-1000:]
    assert _changed(s) == before


def test_an_unknown_tag_is_refused_and_writes_nothing(scratch):
    s = scratch
    proc = s.py("scripts/record_release.py", "v0.0.0-nope")
    assert proc.returncode == 2
    assert "does not exist" in proc.stderr
    assert _changed(s) == []


def test_a_dirty_record_tree_is_refused(scratch):
    s = scratch
    (s.repo / "tests/contract/test_catalog_format_snapshot.py").write_text("# edited\n")
    proc = s.py("scripts/record_release.py", TAG)
    assert proc.returncode != 0
    assert "uncommitted" in (proc.stderr + proc.stdout)
    assert not (s.repo / FIXTURE).exists()
