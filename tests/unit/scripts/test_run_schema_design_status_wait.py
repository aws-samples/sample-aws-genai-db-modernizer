# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``run_schema_design.py --status --wait`` (#2121).

The ``/modernize`` orchestrator ran ``--status`` after every DynamoDB part's report,
11 times in a row in one discourse run, each one more turn that read its whole
context again. It now checks the status once, after every subagent it dispatched has
reported. ``--wait`` covers the one gap that leaves: a part that reported a moment
before its draft is readable. It re-reads a pending line for a bounded time and
prints one line.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from scripts import run_schema_design
from src.storage.local_store import LocalArtifactStore
from tests.unit.scripts.test_run_schema_design_status import BASE, DB, JOB, _drafts, _manifest


def _run(monkeypatch, capsys, root: Path, *extra: str, engine: str = "dynamodb"):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_schema_design.py",
            "--job-id",
            JOB,
            "--db",
            DB,
            "--engine",
            engine,
            "--artifact-root",
            str(root),
            *extra,
        ],
    )
    code = 0
    try:
        run_schema_design.main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    return code, lines


def _no_sleep(_seconds: float) -> None:
    raise AssertionError("--status slept with nothing to wait for")


def test_wait_returns_once_the_last_draft_is_readable(monkeypatch, capsys, tmp_path):
    store = LocalArtifactStore(base_dir=str(tmp_path))
    _manifest(store, groups=3)
    store.write_json(f"{BASE}/schema_draft_group_0.json", {"group": 0})
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        # The parts' drafts land while --status waits: one per poll.
        sleeps.append(seconds)
        store.write_json(f"{BASE}/schema_draft_group_{len(sleeps)}.json", {"group": 1})

    monkeypatch.setattr("time.sleep", sleep)
    code, lines = _run(monkeypatch, capsys, tmp_path, "--status", "--wait", "60")

    assert code == 0
    assert len(lines) == 1  # one compact line, never one per poll
    status = json.loads(lines[0])
    assert status["status"] == "merge_pending"
    assert "wait_timed_out" not in status
    assert len(sleeps) == 2


def test_a_wait_that_runs_out_prints_the_pending_line_and_says_so(monkeypatch, capsys, tmp_path):
    store = LocalArtifactStore(base_dir=str(tmp_path))
    _manifest(store, groups=2)
    monkeypatch.setattr(run_schema_design, "_WAIT_POLL_SECONDS", 0.01)

    code, lines = _run(monkeypatch, capsys, tmp_path, "--status", "--wait", "0.05")

    assert code == 0 and len(lines) == 1
    status = json.loads(lines[0])
    assert status["status"] == "drafts_pending"
    assert status["drafts_missing"] == [0, 1]
    assert status["wait_timed_out"] is True


def test_a_line_that_is_not_pending_never_waits(monkeypatch, capsys, tmp_path):
    store = LocalArtifactStore(base_dir=str(tmp_path))
    _manifest(store, groups=2)
    _drafts(store, groups=2)
    monkeypatch.setattr("time.sleep", _no_sleep)

    code, lines = _run(monkeypatch, capsys, tmp_path, "--status", "--wait", "60")

    assert code == 0
    status = json.loads(lines[0])
    assert status["status"] == "merge_pending" and "wait_timed_out" not in status


@pytest.mark.passes_on_main  # guards main's no-wait --status, unchanged without --wait
def test_status_without_wait_never_waits_on_a_pending_line(monkeypatch, capsys, tmp_path):
    store = LocalArtifactStore(base_dir=str(tmp_path))
    _manifest(store, groups=2)
    monkeypatch.setattr("time.sleep", _no_sleep)

    _, lines = _run(monkeypatch, capsys, tmp_path, "--status")

    status = json.loads(lines[0])
    assert status["status"] == "drafts_pending" and "wait_timed_out" not in status


def test_the_wait_is_capped_under_the_bash_tool_timeout(monkeypatch, capsys, tmp_path):
    store = LocalArtifactStore(base_dir=str(tmp_path))
    _manifest(store, groups=2)
    now = [0.0]

    def sleep(seconds: float) -> None:
        assert seconds > 0
        now[0] += seconds

    run_schema_design.run_status(
        store, JOB, DB, "dynamodb", 1, wait=100_000, sleep=sleep, clock=lambda: now[0]
    )

    status = json.loads(capsys.readouterr().out)
    assert status["wait_timed_out"] is True
    assert now[0] == pytest.approx(run_schema_design._MAX_WAIT_SECONDS)
    assert run_schema_design._MAX_WAIT_SECONDS < 120  # the Bash tool's default timeout


def test_a_single_pass_engine_waits_for_its_finalize(monkeypatch, capsys, tmp_path):
    lines = iter(
        [
            {"status": "not_finalized", "next": "run /design-schema-documentdb, then --finalize"},
            {"status": "finalize_pending", "next": "run --finalize"},
            {"status": "complete", "next": "the finalized output is current"},
        ]
    )
    monkeypatch.setattr(run_schema_design, "_engine_status", lambda *a, **k: next(lines))
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    run_schema_design.run_status(
        None, JOB, DB, "documentdb", 1, wait=30, sleep=sleep, clock=lambda: now[0]
    )

    assert json.loads(capsys.readouterr().out)["status"] == "complete"


def test_a_failed_finalize_is_final_and_not_waited_out(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(
        run_schema_design,
        "_engine_status",
        lambda *a, **k: {"status": "validation_failed", "errors": ["x"]},
    )

    run_schema_design.run_status(None, JOB, DB, "documentdb", 1, wait=30, sleep=_no_sleep)

    assert json.loads(capsys.readouterr().out)["status"] == "validation_failed"


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (("--merge", "--wait", "10"), "--wait only goes with --status"),
        (("--status", "--wait", "-1"), "--wait takes a number of seconds"),
    ],
)
def test_wait_is_refused_outside_status_or_below_zero(
    monkeypatch, capsys, tmp_path, extra, message
):
    code, lines = _run(monkeypatch, capsys, tmp_path, *extra)

    assert code != 0
    status = json.loads(lines[-1])
    assert status["status"] == "error" and message in status["message"]
