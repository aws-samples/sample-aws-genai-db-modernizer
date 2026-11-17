# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``run_assessment.py`` runs the routine repair loop in process (#2385).

``--llm-mode bedrock`` runs every round of the loop (``src.agents.routine_repair.loop``)
before synthesis, on the proof target this machine has (``--proof auto``); ``none``
skips it with the reason the loop gives, and ``external`` leaves the rounds to
``/modernize``. The model is a fake (``handler._ask_bedrock`` is patched) and engine
presence is patched, so no model is called and no engine starts. The routines are the
synthetic fixtures of ``tests/unit/agents/routine_repair/repair_fixtures.py``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from ci.bedrock_smoke import check, ended_well, status_lines
from scripts import run_assessment
from src.agents.routine_repair.handler import LLM_MODE_NONE_REASON
from src.tools.database import disposable_engine as de
from tests.unit.agents.routine_repair import repair_fixtures as fx

NOTE = "Calls pg_current_wal_lsn, the name PostgreSQL 10 gave the function."


def _answer() -> dict[str, Any]:
    return {
        "repairs": [
            {
                "kind": "function",
                "routine_id": fx.WAL,
                "repaired_create": fx.WAL_REPAIRED,
                "change_note": NOTE,
            }
        ]
    }


@pytest.fixture
def model(monkeypatch):
    """The Bedrock entry point: each call returns the set answer and is counted."""
    from src.agents.routine_repair import handler
    from src.contracts.routine_repair import RoutineRepairResponse

    calls: list[dict[str, Any]] = []

    def ask(request: dict[str, Any]) -> RoutineRepairResponse:
        calls.append(request)
        return RoutineRepairResponse.model_validate(_answer())

    monkeypatch.setattr(handler, "_ask_bedrock", ask)
    return calls


@pytest.fixture
def no_model(monkeypatch):
    from src.agents.routine_repair import handler

    def forbid(request: dict[str, Any]) -> None:
        raise AssertionError("the repair model was called")

    monkeypatch.setattr(handler, "_ask_bedrock", forbid)


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(run_assessment, "_CONSOLE", None)
    monkeypatch.setattr(run_assessment, "_CURRENT_PHASE", None)


def _lines(out: str) -> list[dict[str, Any]]:
    return [json.loads(x) for x in out.splitlines() if x.startswith('{"phase"')]


def _repair_file(tmp_path: Path) -> Path:
    return tmp_path / fx.DB / fx.JOB / "routines" / "v1" / "routine_repair.json"


def test_bedrock_runs_the_loop_in_process_and_prints_its_line(tmp_path, monkeypatch, capsys, model):
    monkeypatch.setattr(de, "available", lambda *a, **k: False)
    store = fx.job(tmp_path)
    status = run_assessment.phase_routine_repair(store, fx.JOB, fx.DB, "bedrock")
    assert len(model) == 1  # one round: the repair passes every check
    assert status["status"] == "complete"
    assert status["proof"] == "none"  # no disposable engine here: it waits for the proof
    assert status["counts"]["repaired_pending_proof"] == 1
    assert status["counts"]["model_repaired"] == 0
    (line,) = _lines(capsys.readouterr().out)
    assert line == {"phase": "routine_repair", **status}
    saved = json.loads(_repair_file(tmp_path).read_text())
    assert saved["llm_mode"] == "bedrock" and saved["proof"] == "none"


@pytest.mark.parametrize("present, proof", [(True, "local"), (False, "none")])
def test_bedrock_proves_on_the_engines_this_machine_has(
    tmp_path, monkeypatch, no_model, present, proof
):
    from src.agents.routine_repair import handler

    monkeypatch.setattr(de, "available", lambda *a, **k: present)
    seen: dict[str, Any] = {}

    def loop(store, db, job_id, **kwargs):
        seen.update(kwargs)
        return {"status": "complete", "proof": kwargs["proof"]}

    monkeypatch.setattr(handler, "run_routine_repair", loop)
    run_assessment.phase_routine_repair(fx.job(tmp_path), fx.JOB, fx.DB, "bedrock")
    assert seen == {"llm_mode": "bedrock", "proof": proof}


def test_none_skips_the_loop_with_its_reason(tmp_path, capsys, no_model):
    status = run_assessment.phase_routine_repair(fx.job(tmp_path), fx.JOB, fx.DB, "none")
    assert status == {"status": "skipped", "reason": LLM_MODE_NONE_REASON}
    assert _lines(capsys.readouterr().out) == [{"phase": "routine_repair", **status}]
    assert not _repair_file(tmp_path).exists()


def test_external_leaves_the_rounds_to_modernize(tmp_path, capsys, no_model):
    status = run_assessment.phase_routine_repair(fx.job(tmp_path), fx.JOB, fx.DB, "external")
    assert status["status"] == "skipped"
    assert "/modernize" in status["reason"]
    assert not _repair_file(tmp_path).exists()


def test_a_failed_loop_is_a_line_without_routine_text_and_the_run_goes_on(
    tmp_path, monkeypatch, capsys
):
    from src.agents.routine_repair import handler

    def boom(*a, **k):
        raise RuntimeError(f"cannot repair {fx.WAL_CREATE}")

    monkeypatch.setattr(handler, "run_routine_repair", boom)
    monkeypatch.setattr(de, "available", lambda *a, **k: False)
    status = run_assessment.phase_routine_repair(fx.job(tmp_path), fx.JOB, fx.DB, "bedrock")
    assert status["status"] == "failed"
    assert "RuntimeError" in status["reason"]
    out = capsys.readouterr()
    assert fx.LITERAL_MARK not in out.out + out.err and "CREATE" not in out.out
    assert ended_well("routine_repair", status) is False


def test_the_smoke_passes_a_skip_with_a_reason_and_fails_a_failure():
    base = {
        "schema_design": {"status": "complete"},
        "synthesis": {"status": "complete"},
        "report": {"status": "complete", "errors": []},
    }

    def run(line: dict[str, Any]) -> str | None:
        rows = {**base, "routine_repair": line}
        return check(status_lines(json.dumps({"phase": p, **d}) for p, d in rows.items()))

    assert run({"status": "skipped", "reason": "the job has no routine-migration artifact"}) is None
    assert run({"status": "complete", "counts": {}}) is None
    assert run({"status": "skipped"}) is not None
    assert run({"status": "failed", "reason": "routine repair failed: RuntimeError"}) is not None


def test_an_all_run_repairs_the_last_version_before_synthesis(tmp_path, monkeypatch):
    # --all -y: the repair runs after the routine migration of the last version the run
    # writes (after schema design's in-process reroute) and before synthesis reads it.
    from tests.unit.scripts.test_run_assessment_routine_migration import (
        _new_version,
        _pg_collection,
    )

    (tmp_path / "collection.json").write_text(json.dumps(_pg_collection()))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(run_assessment, "_JOB", {})
    order: list[Any] = []
    root: Path = tmp_path / "a" / "shop"

    def job() -> Path:
        (only,) = root.iterdir()
        return only

    def schema_design(*a, **k):
        _new_version(job(), 2)
        return {"status": "skipped", "reason": "stub"}

    def repair(store, job_id, db, llm_mode):
        order.append(("repair", llm_mode, sorted(p.name for p in (job() / "routines").iterdir())))
        return {"status": "skipped", "reason": LLM_MODE_NONE_REASON}

    monkeypatch.setattr(run_assessment, "phase_engine_choices", lambda *a, **k: None)
    monkeypatch.setattr(run_assessment, "phase_schema_design", schema_design)
    monkeypatch.setattr(run_assessment, "phase_routine_repair", repair)
    monkeypatch.setattr(
        run_assessment, "phase_synthesis", lambda *a, **k: order.append("synthesis")
    )
    monkeypatch.setattr(run_assessment, "phase_report", lambda *a, **k: {"status": "complete"})
    monkeypatch.setattr(sys, "argv", ["run_assessment.py", "--file", str(tmp_path / "collection.json"),
                                      "--db", "shop", "--artifact-root", str(tmp_path / "a"),
                                      "--llm-mode", "none", "--all", "-y", "--verbose"])  # fmt: skip
    run_assessment.main()
    assert order == [("repair", "none", ["v1", "v2"]), "synthesis"]
    state = json.loads((tmp_path / ".modernizer-state.json").read_text())
    assert state["phase_status"]["routine_repair"] == "skipped"
