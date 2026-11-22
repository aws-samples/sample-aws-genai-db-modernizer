# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The proof target a run's repair loop uses (#2385): ``--proof auto`` is ``local`` when a
disposable engine of the source's major is on this machine, else ``none``.

Engine presence is patched (``disposable_engine.available``); no engine starts and no
model is called. The routines are the synthetic fixtures of ``repair_fixtures``.
"""

from __future__ import annotations

import json
import sys

import pytest

from src.agents.routine_repair.loop import proof_for_run
from src.tools.database import disposable_engine as de
from tests.unit.agents.routine_repair import repair_fixtures as fx


@pytest.fixture
def engines(monkeypatch):
    """Which ``(engine, version)`` pairs the patched availability check was asked about."""
    asked: list[tuple[str, str]] = []

    def install(present: bool) -> list[tuple[str, str]]:
        def available(engine: str, version: str, **_: object) -> bool:
            asked.append((engine, version))
            return present

        monkeypatch.setattr(de, "available", available)
        return asked

    return install


def test_auto_is_local_when_the_sources_engine_is_here(tmp_path, engines):
    asked = engines(True)
    assert proof_for_run(fx.job(tmp_path), fx.DB, fx.JOB, "auto") == "local"
    assert asked == [("postgresql", "9.6.2")]


def test_auto_is_none_when_no_engine_of_the_major_is_here(tmp_path, engines):
    engines(False)
    assert proof_for_run(fx.job(tmp_path), fx.DB, fx.JOB, "auto") == "none"


def test_auto_is_none_without_a_routine_migration_artifact(tmp_path, engines):
    from src.storage.local_store import LocalArtifactStore

    asked = engines(True)
    assert proof_for_run(LocalArtifactStore(base_dir=str(tmp_path)), fx.DB, fx.JOB) == "none"
    assert asked == []


def test_auto_is_none_when_the_collection_names_no_source_version(tmp_path, engines):
    asked = engines(True)
    built = fx.migration().model_copy(update={"source_version": None})
    assert proof_for_run(fx.job(tmp_path, built=built), fx.DB, fx.JOB, "auto") == "none"
    assert asked == []


@pytest.mark.parametrize("named", ["local", "none"])
def test_a_named_proof_is_kept_as_given(tmp_path, engines, named):
    asked = engines(named == "none")
    assert proof_for_run(fx.job(tmp_path), fx.DB, fx.JOB, named) == named
    assert asked == []


def test_an_unknown_proof_is_refused(tmp_path):
    with pytest.raises(ValueError):
        proof_for_run(fx.job(tmp_path), fx.DB, fx.JOB, "cloud")


@pytest.mark.parametrize("present, proof", [(True, "local"), (False, "none")])
def test_the_script_resolves_auto_before_the_loop_runs(
    tmp_path, monkeypatch, capsys, engines, present, proof
):
    from scripts import run_routine_repair

    engines(present)
    fx.job(tmp_path)
    monkeypatch.delenv("MODERNIZER_CI_SANDBOX", raising=False)
    monkeypatch.setattr(run_routine_repair, "_JOB", {})
    monkeypatch.setattr(sys, "argv", ["run_routine_repair.py", "--job-id", fx.JOB, "--db", fx.DB,
                                      "--llm-mode", "external", "--proof", "auto",
                                      "--artifact-root", str(tmp_path)])  # fmt: skip
    run_routine_repair.main()
    (line,) = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.startswith("{")]
    assert line["status"] == "awaiting_llm", line
    assert line["proof"] == proof
    saved = json.loads(
        (tmp_path / fx.DB / fx.JOB / "routines" / "v1" / "repair" / "loop_state.json").read_text()
    )
    assert saved["proof"] == proof


def test_a_candidate_that_changes_what_the_comparison_reads_fails_as_its_own_fault():
    """#2337: a repair candidate whose call changed what the comparison reads (a cast, a
    policy, a setup table swapped for a view) fails as the candidate's own fault, so the
    next attempt hears why, never ``pending`` as a proof that could not run."""
    from src.tools.routines.comparison import RoutineComparison
    from src.tools.routines.repair_proof import _verdict

    found = RoutineComparison(routine_id="public.f(integer)", outcome="could_not_compare",
                              reason="call 1 changed what the comparison reads",
                              reason_code="harness_tampered")  # fmt: skip
    check = _verdict(found, "a disposable postgresql 16 engine")
    assert (check.outcome, check.code) == ("failed", "harness_tampered")
