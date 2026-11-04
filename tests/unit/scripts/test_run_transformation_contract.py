# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``scripts/run_transformation_contract.py`` prints one JSON status line (#1764)."""

from __future__ import annotations

import json
import sys

import pytest

from scripts import run_transformation_contract as script
from tests.unit.tools.transformation_contract.test_write import _store_with_job


def _main(monkeypatch, capsys, *argv):
    monkeypatch.setattr(sys, "argv", ["run_transformation_contract.py", *argv])
    code = 0
    try:
        script.main()
    except SystemExit as e:
        code = int(e.code or 0)
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_a_finished_job_prints_complete(tmp_path, monkeypatch, capsys):
    _store_with_job(tmp_path)
    code, out = _main(
        monkeypatch, capsys, "--job-id", "j1", "--db", "wp", "--artifact-root", str(tmp_path)
    )
    assert code == 0 and out["status"] == "complete"
    assert len(out["sha256"]) == 64 and out["paths"][0].endswith("step-1.json")
    assert (tmp_path / out["paths"][0]).is_file()


def test_a_job_with_no_artifacts_is_an_error_with_exit_1(tmp_path, monkeypatch, capsys):
    code, out = _main(
        monkeypatch, capsys, "--job-id", "nope", "--db", "wp", "--artifact-root", str(tmp_path)
    )
    assert code == 1 and out["status"] == "error" and "synthesis" in out["message"]


def test_a_job_without_waves_is_skipped_and_exits_0(tmp_path, monkeypatch, capsys):
    _store_with_job(tmp_path, waves=False)
    code, out = _main(
        monkeypatch, capsys, "--job-id", "j1", "--db", "wp", "--artifact-root", str(tmp_path)
    )
    assert code == 0 and out["status"] == "skipped"


@pytest.mark.parametrize("job_id", ["../x", "a b"])
def test_a_bad_job_id_is_rejected(tmp_path, monkeypatch, capsys, job_id):
    code, out = _main(
        monkeypatch, capsys, "--job-id", job_id, "--db", "wp", "--artifact-root", str(tmp_path)
    )
    assert code == 1 and out["status"] == "error"
