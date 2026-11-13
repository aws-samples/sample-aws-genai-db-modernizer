# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``run_synthesis.py`` reports a refusal as one ``failed`` status line (#2154 review).

A headless run reads each phase's result from the script's JSON lines. A saved Move
that breaks the contract (``UnreadableMove``) and an engine with neither a design nor a
skip record (``MissingSchemaDesigns``) stop synthesis on purpose: the script prints
``{"status": "failed", "phase": "synthesis", "error": ..., "message": ...}``, exits 1,
and prints no traceback on stdout.
"""

from __future__ import annotations

import json
import subprocess  # nosec B404 -- runs the repo's own script
import sys
from pathlib import Path

from src.storage.local_store import LocalArtifactStore
from tests.unit.agents.referee.test_synthesis_needs_every_design import DB, JOB, _design

REPO = Path(__file__).resolve().parents[3]
PLANTED = "SELECT card_number FROM wp_secret_cards"


def _job(tmp_path: Path) -> LocalArtifactStore:
    from tests.unit.agents.referee import test_synthesis_needs_every_design as jobs

    store = LocalArtifactStore(base_dir=str(tmp_path))
    jobs.store.__wrapped__(tmp_path)  # type: ignore[attr-defined]
    return store


def _run(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603 -- fixed argv, the repo's own script
        [
            sys.executable,
            "scripts/run_synthesis.py",
            "--job-id",
            JOB,
            "--db",
            DB,
            "--artifact-root",
            str(tmp_path),
            "--llm-mode",
            "none",
            "--assignment-version",
            "1",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )


def _status(proc: subprocess.CompletedProcess[str]) -> dict:
    assert proc.returncode == 1
    assert "Traceback" not in proc.stdout
    [line] = [json.loads(x) for x in proc.stdout.splitlines() if x.startswith("{")]
    assert isinstance(line, dict)
    return line


def test_a_missing_design_is_one_failed_status_line(tmp_path: Path) -> None:
    store = _job(tmp_path)
    _design(store, "dynamodb")
    line = _status(_run(tmp_path))
    assert line == {
        "status": "failed",
        "phase": "synthesis",
        "error": "MissingSchemaDesigns",
        "message": (
            "Synthesis needs a schema design or a skip record for every engine of "
            "assignment v1: opensearch has neither. Run its schema design, or skip it, first."
        ),
    }


def test_an_unreadable_move_is_one_failed_status_line_with_no_stored_value(
    tmp_path: Path,
) -> None:
    store = _job(tmp_path)
    key = f"{DB}/{JOB}/assignment/v1/assignment.json"
    assignment = store.read_json(key)
    assignment["moves"] = [
        {
            "seq": 1,
            "query_id": "q1",
            "pass": "score",
            "to_engine": "dynamodb",
            "reason_code": "highest_score",
            "evidence": {"group_scores": {PLANTED: "high"}},
        }
    ]
    store.write_json(key, assignment)
    proc = _run(tmp_path)
    line = _status(proc)
    assert (line["status"], line["phase"], line["error"]) == (
        "failed",
        "synthesis",
        "UnreadableMove",
    )
    assert "evidence.group_scores.[*]" in line["message"]
    assert "card_number" not in proc.stdout + proc.stderr
