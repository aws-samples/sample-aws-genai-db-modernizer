# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ci/llm/replay.py save: a redacted job archive never becomes a fixture unnoticed (#2053)."""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

from ci.llm import replay


def _run_folder(
    tmp_path: Path,
    assignment: str = '{"v": 1}',
    manifest: bool = False,
    marked: bool = False,
) -> Path:
    src = tmp_path / "llm-both-wordpress"
    synthesis = src / "deliverables" / "wordpress" / "synthesis"
    synthesis.mkdir(parents=True)
    (synthesis / "report.json").write_text('{"database_name": "wordpress"}')
    results: dict = {"pass": True, "db": "wordpress", "job_id": "job1", "mode": "both"}
    if marked:
        # What collect-job writes: the run failed by the scan, its own verdict kept.
        results["pass"] = False
        results["private_scan"] = {
            "findings": 2,
            "manifest": "job.findings.json",
            "pass_before_scan": True,
        }
    (src / "results.json").write_text(json.dumps(results))
    (src / "transcript.jsonl").write_text('{"type": "system"}\n')
    files = {
        "assignment/v1/assignment.json": assignment,
        "synthesis/v1/report.json": "{}",
    }
    with tarfile.open(src / replay.JOB_ARCHIVE, "w:gz") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo("wordpress/job1/" + name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    if manifest:
        (src / "job.findings.json").write_text(
            json.dumps(
                {
                    "archive": "job.tar.gz",
                    "redacted": True,
                    "findings": [{"file": "assignment/v1/assignment.json", "line": 1, "type": "x"}],
                }
            )
        )
    return src


@pytest.mark.parametrize(
    "kwargs",
    [
        {"assignment": '{"ip": "[REDACTED:public-ip-ipv4]"}'},
        {"manifest": True},
        {"marked": True},
    ],
    ids=["marker-in-archive", "manifest", "results-mark"],
)
def test_save_refuses_a_redacted_archive(tmp_path: Path, kwargs: dict) -> None:
    dest = tmp_path / "fixture"
    with pytest.raises(ValueError, match="--allow-redacted"):
        replay.save(_run_folder(tmp_path, **kwargs), dest)
    assert not dest.exists()


def test_save_keeps_a_redacted_archive_only_when_told_and_records_it(tmp_path: Path) -> None:
    dest = tmp_path / "fixture"
    source = _run_folder(tmp_path, '{"ip": "[REDACTED:public-ip-ipv4]"}', manifest=True)
    info = replay.save(source, dest, allow_redacted=True)
    assert info["redacted_findings"] == 1
    assert json.loads((dest / "run.json").read_text())["redacted_findings"] == 1


def test_save_keeps_a_run_the_scan_failed_only_when_told(tmp_path: Path) -> None:
    info = replay.save(_run_folder(tmp_path, marked=True), tmp_path / "f", allow_redacted=True)
    assert info["redacted_findings"] == 2


def test_allow_redacted_never_keeps_a_run_that_failed_on_its_own(tmp_path: Path) -> None:
    source = _run_folder(tmp_path, marked=True)
    results = json.loads((source / "results.json").read_text())
    results["private_scan"]["pass_before_scan"] = False
    (source / "results.json").write_text(json.dumps(results))
    with pytest.raises(ValueError, match="does not pass"):
        replay.save(source, tmp_path / "f", allow_redacted=True)


def test_the_save_command_takes_allow_redacted(tmp_path: Path, capsys) -> None:
    source = _run_folder(tmp_path, manifest=True)
    dest = tmp_path / "fixture"
    with pytest.raises(ValueError):
        replay.main(["save", "--from", str(source), "--to", str(dest)])
    assert replay.main(["save", "--from", str(source), "--to", str(dest), "--allow-redacted"]) == 0
    assert json.loads(capsys.readouterr().out)["redacted_findings"] == 1


@pytest.mark.passes_on_main
def test_save_keeps_a_clean_archive_without_a_redaction_record(tmp_path: Path) -> None:
    info = replay.save(_run_folder(tmp_path), tmp_path / "fixture")
    assert "redacted_findings" not in info
