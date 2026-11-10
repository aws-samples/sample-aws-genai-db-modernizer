# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The Bedrock smoke's pass check (#2085): every normal ending of the wave redesign
loop passes, read from the loop's own status lines; a failure still fails."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ci import bedrock_smoke
from ci.bedrock_smoke import check, ended_well, main, status_lines
from src.agents.schema_design import wave_redesign as wr
from src.agents.schema_design.wave_redesign import run_rounds
from src.storage.local_store import LocalArtifactStore
from tests.unit.agents.schema_design.test_wave_redesign import (
    DB,
    DESIGN,
    JOB,
    REPORT,
    V,
    _hold,
    _report,
)


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalArtifactStore:
    """The redesign loop's store, as ``test_wave_redesign`` sets it up."""
    s = LocalArtifactStore(base_dir=str(tmp_path))
    s.write_json(DESIGN, {"design": "first"})
    s.write_json(REPORT, {"wave_redesign": None})
    monkeypatch.setattr(
        wr,
        "_write_report_log",
        lambda store, db, job_id, version, log: store.write_json(
            REPORT, {"wave_redesign": dict(log)}
        ),
    )
    return s


def _run(**phases: dict[str, Any]) -> dict[str, dict[str, Any]]:
    base: dict[str, dict[str, Any]] = {
        "schema_design": {"status": "complete"},
        "synthesis": {"status": "complete"},
        "report": {"status": "complete", "errors": []},
    }
    lines = [json.dumps({"phase": p, **d}) for p, d in {**base, **phases}.items()]
    return status_lines(["Running ...", *lines, '{"log": "x"}'])


def _loop(store: LocalArtifactStore, outcome: str) -> dict[str, Any]:
    """The status ``run_rounds`` returns for each way the loop can end."""
    reports = {
        "complete": lambda: _report([]),
        "exhausted": lambda: _report([_hold()]),
        "stopped": lambda: _report([_hold()]),
        "failed": lambda: _report([_hold()]),
    }

    def design(engine: str) -> None:
        if outcome == "failed":
            raise RuntimeError("model timed out")
        store.write_json(DESIGN, {"design": "again"})

    return run_rounds(
        store,
        DB,
        JOB,
        version=V,
        read_report=reports[outcome],
        design=design,
        synthesize=lambda: None,
        budget=(
            (lambda: "the run's tokens ceiling leaves no room")
            if outcome == "stopped"
            else (lambda: None)
        ),
    )


@pytest.mark.parametrize("outcome", ["complete", "exhausted", "stopped"])
def test_each_normal_loop_ending_passes(store: LocalArtifactStore, outcome: str) -> None:
    status = _loop(store, outcome)
    assert status["status"] == outcome
    assert check(_run(wave_redesign=status)) is None


def test_the_accepted_endings_are_the_loops_own() -> None:
    assert bedrock_smoke.PHASE_ENDINGS["wave_redesign"] is wr.LOOP_ENDINGS
    assert wr.LOOP_ENDINGS == {wr.COMPLETE, wr.EXHAUSTED, wr.STOPPED}
    assert wr.FAILED not in wr.LOOP_ENDINGS


def test_a_design_that_raised_fails_the_smoke(store: LocalArtifactStore) -> None:
    status = _loop(store, "failed")
    assert status["status"] == "failed"
    assert check(_run(wave_redesign=status)) == (
        "bedrock-smoke failed: phases not complete ['wave_redesign'], missing [], "
        "report errors []"
    )


def test_a_skip_passes_only_with_its_reason() -> None:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    import scripts.run_assessment as ra

    skipped = ra.phase_wave_redesign(None, JOB, DB, "none")
    assert skipped["status"] == wr.SKIPPED and skipped["reason"]
    assert check(_run(wave_redesign=skipped)) is None
    assert not ended_well("wave_redesign", {"status": "skipped"})
    assert not ended_well("wave_redesign", {"status": "skipped", "reason": " "})
    # Only the redesign phase may end so: another phase's skip still fails.
    assert not ended_well("synthesis", {"status": "skipped", "reason": "x"})
    assert not ended_well("synthesis", {"status": "exhausted"})


@pytest.mark.parametrize(
    ("phases", "failure"),
    [
        ({"wave_redesign": {"status": "error"}}, "phases not complete ['wave_redesign']"),
        ({"wave_redesign": {"status": "awaiting_redesign"}}, "not complete ['wave_redesign']"),
        ({"synthesis": {"status": "failed"}}, "phases not complete ['synthesis']"),
        ({"report": {"status": "complete", "errors": ["no PDF"]}}, "report errors ['no PDF']"),
    ],
)
def test_a_real_failure_still_fails(phases: dict, failure: str) -> None:
    assert failure in (check(_run(**phases)) or "")


def test_a_missing_phase_fails() -> None:
    phases = _run()
    del phases["synthesis"]
    assert "missing ['synthesis']" in (check(phases) or "")


def test_the_file_the_ci_job_reads(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """The line that failed on main (#2085) now passes; a failed loop does not."""
    lines = [
        '{"phase": "schema_design", "status": "complete"}',
        '{"phase": "synthesis", "status": "complete"}',
        '{"phase": "wave_redesign", "status": "exhausted", "rounds": 2, "max_rounds": 2, '
        '"held": ["dynamodb"]}',
        '{"phase": "report", "status": "complete", "errors": []}',
    ]
    status = tmp_path / "status.jsonl"
    status.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert main([str(status)]) == 0
    assert capsys.readouterr().out.endswith("bedrock-smoke: pass\n")
    status.write_text(lines[2].replace("exhausted", "failed") + "\n", encoding="utf-8")
    assert main([str(status)]) == 1
    assert main([str(tmp_path / "absent.jsonl")]) == 2


def test_every_status_the_loop_returns_is_an_ending_a_failure_or_a_step() -> None:
    """A status a later change adds to ``plan_round`` or ``run_rounds`` must be placed:
    a normal ending (``LOOP_ENDINGS``, which the smoke accepts), ``failed``, or a step a
    caller continues from (``LOOP_STEPS``: ``awaiting_review`` only on a headless check,
    #2081). The per-engine round outcomes (``not_reviewed`` and the like, #2077) are not
    status lines."""
    import ast
    import inspect

    steps = wr.LOOP_STEPS
    found: set[str] = set()
    for fn in (wr.plan_round, wr.run_rounds):
        for node in ast.walk(ast.parse(inspect.getsource(fn).lstrip())):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values, strict=True):
                if isinstance(key, ast.Constant) and key.value == "status":
                    if isinstance(value, ast.Constant):
                        found.add(str(value.value))
                    elif isinstance(value, ast.Name):
                        found.add(getattr(wr, value.id))
    assert found == wr.LOOP_ENDINGS | {wr.FAILED} | steps
