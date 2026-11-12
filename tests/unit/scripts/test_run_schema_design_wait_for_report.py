# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``run_schema_design.py --wait-for-report`` (#2175).

A headless ``/modernize --auto`` run lost its result line: its orchestrator ended its
turn while DynamoDB part 1 was still running, and 600 s after that last turn Claude Code
stopped the part and ended the session. Headless, the orchestrator now runs this bounded
wait instead of ending the turn: it returns as soon as a design or review lands, and
otherwise after at most 110 seconds, with one line.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from scripts import run_schema_design

DB, JOB = "wordpress", "job2175"


def _job(root: Path) -> Path:
    vdir = root / DB / JOB / "schema-dynamodb" / "v2"
    vdir.mkdir(parents=True)
    (vdir / "schema_draft_group_1.json").write_text("{}")
    (root / DB / JOB / "schema-elasticache" / "v2").mkdir(parents=True)
    return root / DB / JOB


def _run(monkeypatch, capsys, root: Path, *extra: str):
    argv = ["run_schema_design.py", "--job-id", JOB, "--db", DB, "--artifact-root", str(root)]
    monkeypatch.setattr(sys, "argv", [*argv, *extra])
    code = 0
    try:
        run_schema_design.main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    return code, lines


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now


def test_it_returns_as_soon_as_a_part_draft_lands(monkeypatch, capsys, tmp_path):
    job = _job(tmp_path)
    clock = _Clock()

    def sleep(seconds: float) -> None:
        clock.sleeps.append(seconds)
        clock.now += seconds
        if len(clock.sleeps) == 3:  # part 1 writes its draft during the third poll
            (job / "schema-dynamodb" / "v2" / "schema_draft_group_0.json").write_text("{}")

    monkeypatch.setattr("time.sleep", sleep)
    monkeypatch.setattr("time.monotonic", clock)
    code, lines = _run(monkeypatch, capsys, tmp_path, "--wait-for-report", "110")

    assert code == 0 and len(lines) == 1, lines  # one compact line, never one per poll
    line = json.loads(lines[0])
    assert line["status"] == "landed"
    assert line["landed"] == ["schema-dynamodb/v2/schema_draft_group_0.json"]
    assert line["waited_s"] == 6.0 and len(clock.sleeps) == 3


@pytest.mark.parametrize(
    ("relative", "content", "mark"),
    [
        (
            "schema-elasticache/v2/schema_output.json",
            {},
            "schema-elasticache/v2/schema_output.json",
        ),
        (
            "schema-elasticache/v2/design_trace.json",
            {"pe_review": {"verdict": "APPROVED"}},
            "schema-elasticache/v2/design_trace.json#pe_review",
        ),
    ],
)
def test_a_finalized_design_or_a_recorded_review_lands(
    monkeypatch, capsys, tmp_path, relative, content, mark
):
    job = _job(tmp_path)
    clock = _Clock()

    def sleep(seconds: float) -> None:
        clock.now += seconds
        (job / relative).write_text(json.dumps(content))

    monkeypatch.setattr("time.sleep", sleep)
    monkeypatch.setattr("time.monotonic", clock)
    _, lines = _run(monkeypatch, capsys, tmp_path, "--wait-for-report", "110")
    assert json.loads(lines[0])["landed"] == [mark]


def test_an_edit_or_a_trace_with_no_review_never_ends_the_wait(monkeypatch, capsys, tmp_path):
    """A part edits its draft after --check-costs, and a design writes its trace before
    any review: neither is a report, so the wait runs to its bound."""
    job = _job(tmp_path)
    clock = _Clock()

    def sleep(seconds: float) -> None:
        clock.sleeps.append(seconds)
        clock.now += seconds
        (job / "schema-dynamodb" / "v2" / "schema_draft_group_1.json").write_text('{"x": 1}')
        (job / "schema-elasticache" / "v2" / "design_trace.json").write_text("{}")

    monkeypatch.setattr("time.sleep", sleep)
    monkeypatch.setattr("time.monotonic", clock)
    code, lines = _run(monkeypatch, capsys, tmp_path, "--wait-for-report", "30")
    line = json.loads(lines[0])
    assert code == 0 and line["status"] == "no_change" and "landed" not in line
    assert line["waited_s"] == 30.0 and sum(clock.sleeps) == 30.0


def test_the_wait_is_capped_under_the_bash_tool_timeout(monkeypatch, capsys, tmp_path):
    _job(tmp_path)
    clock = _Clock()

    def sleep(seconds: float) -> None:
        clock.now += seconds

    monkeypatch.setattr("time.sleep", sleep)
    monkeypatch.setattr("time.monotonic", clock)
    _, lines = _run(monkeypatch, capsys, tmp_path, "--wait-for-report", "900")
    assert json.loads(lines[0])["waited_s"] == 110.0


@pytest.mark.parametrize(
    "extra",
    [
        ("--wait-for-report", "0"),
        ("--wait-for-report", "-5"),
        ("--wait-for-report", "10", "--status", "--engine", "dynamodb"),
        ("--wait-for-report", "10", "--merge", "--engine", "dynamodb"),
    ],
)
def test_a_bad_wait_is_refused_without_waiting(monkeypatch, capsys, tmp_path, extra):
    _job(tmp_path)
    monkeypatch.setattr("time.sleep", lambda _s: pytest.fail("slept"))
    code, lines = _run(monkeypatch, capsys, tmp_path, *extra)
    assert code != 0 and json.loads(lines[-1])["status"] == "error"


def test_a_job_with_no_artifacts_is_refused(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr("time.sleep", lambda _s: pytest.fail("slept"))
    code, lines = _run(monkeypatch, capsys, tmp_path, "--wait-for-report", "10")
    assert code != 0 and "no artifacts" in json.loads(lines[-1])["message"]


@pytest.mark.passes_on_main
def test_every_other_mode_still_needs_an_engine(monkeypatch, capsys, tmp_path):
    _job(tmp_path)
    code, _ = _run(monkeypatch, capsys, tmp_path, "--status")
    assert code != 0
