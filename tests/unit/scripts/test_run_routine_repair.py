# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``scripts/run_routine_repair.py`` (#1940): one JSON status line per run, in each mode
the command and a headless session use."""

from __future__ import annotations

import json
import subprocess  # nosec B404 -- runs this repo's own script with fixed arguments
import sys
from pathlib import Path

from tests.unit.agents.routine_repair import repair_fixtures as fx

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "run_routine_repair.py"


def _run(tmp_path: Path, *args: str) -> tuple[int, list[dict]]:
    proc = subprocess.run(  # nosec B603 -- fixed argv
        [sys.executable, str(SCRIPT), "--job-id", fx.JOB, "--db", fx.DB,
         "--artifact-root", str(tmp_path), *args],
        capture_output=True, text=True, cwd=REPO, check=False,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )  # fmt: skip
    lines = [json.loads(ln) for ln in proc.stdout.splitlines() if ln.startswith("{")]
    return proc.returncode, lines


def test_mode_none_prints_one_skipped_line(tmp_path):
    fx.job(tmp_path)
    code, lines = _run(tmp_path, "--llm-mode", "none")
    assert code == 0
    (line,) = lines
    assert line["phase"] == "routine_repair" and line["status"] == "skipped"
    assert line["counts"]["not_repaired"] == 1


def test_external_then_finalize(tmp_path):
    store = fx.job(tmp_path)
    code, (line,) = _run(tmp_path, "--llm-mode", "external")
    assert code == 0 and line["status"] == "awaiting_llm"
    request = store.read_json(line["llm_request"])
    assert fx.LITERAL_MARK not in json.dumps(request)
    store.write_json(
        line["llm_response"],
        {
            "repairs": [
                {
                    "kind": "function",
                    "routine_id": fx.WAL,
                    "repaired_create": fx.WAL_REPAIRED,
                    "change_note": "Calls the function by its PostgreSQL 10 name.",
                }
            ]
        },
    )
    code, (done,) = _run(tmp_path, "--finalize")
    assert code == 0 and done["status"] == "complete"
    assert done["counts"]["repaired_pending_proof"] == 1


def test_finalize_with_an_answer_off_the_contract_is_an_error_naming_where(tmp_path):
    store = fx.job(tmp_path)
    _, (line,) = _run(tmp_path, "--llm-mode", "external")
    store.write_json(line["llm_response"], {"repairs": [], "status": "proved"})
    code, (err,) = _run(tmp_path, "--finalize")
    assert code == 1 and err["status"] == "error"
    assert "RoutineRepairResponse" in err["message"]
    assert "proved" not in err["message"]
