# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A redesign round never stays pending at the end of a run (#2122).

A discourse ``/modernize --auto`` run printed ``awaiting_redesign`` for round 2 and went on
to its report: the round's DynamoDB design stayed set aside under ``wave-redesign/``, the
log kept round 2 open with no outcome, and the report never said so. A round the run does
not run is closed ``stopped`` (``stopped_by`` ``closed``) with the earlier design put back,
the loop ends there, and the waves state it; the in-process loop does the same when
anything but a design stops it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from src.agents.schema_design import wave_redesign as wr
from src.agents.schema_design.wave_redesign import (
    plan_round,
    read_log,
    request_key,
    run_rounds,
    stop_round,
)
from src.contracts.synthesis_output import WaveRedesignLog
from src.shared.wave_readiness import redesign_facts, wave_readiness
from src.storage.local_store import LocalArtifactStore

DB, JOB, V = "shop", "job-1", 2
REPO = Path(__file__).resolve().parents[4]
FIXTURE = Path(__file__).parent / "fixtures" / "wave_redesign_round_2_pending.json"
DESIGN = f"{DB}/{JOB}/schema-dynamodb/v{V}/schema_output.json"
REPORT = f"{DB}/{JOB}/synthesis/v{V}/report.json"
LOG = f"{DB}/{JOB}/wave-redesign/v{V}/rounds.json"

HOT = {
    "risk_id": "RISK-001",
    "risk_type": "PERFORMANCE_DEGRADATION",
    "severity": "HIGH",
    "rule": "design.hot_partition",
    "description": "[dynamodb] Every order read lands on one key.",
    "mitigation": "Spread the orders over more key values.",
    "query_ids": ["q1", "q2"],
}


def _hold(check: str = "open_high_risk", engine: str = "dynamodb") -> dict[str, Any]:
    return {
        "check": check,
        "engine": engine,
        "risk_id": "RISK-001",
        "severity": "HIGH",
        "line": f"a {check} hold on {engine}",
    }


def _report(holds: list[dict[str, Any]], *, engine: str = "dynamodb") -> dict[str, Any]:
    """A report whose wave 2 (``engine``) carries ``holds``; every engine designed and
    reviewed."""
    return {
        "schema_designs": {
            e: {
                "status": "completed",
                "reviewed_count": 1,
                "open_review_count": 0,
                "failed_review_count": 0,
                "unsupported_query_count": 0,
            }
            for e in ("dynamodb", "documentdb")
        },
        "risk_assessment": {"risks": [HOT]},
        "facts": {
            "waves": [
                {"wave": 1, "engines": ["aurora_mysql"], "readiness": "suggested", "holds": []},
                {"wave": 2, "engines": [engine], "readiness": "held", "holds": holds},
            ]
        },
    }


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalArtifactStore:
    s = LocalArtifactStore(base_dir=str(tmp_path))
    s.write_json(DESIGN, {"design": "first"})
    s.write_json(REPORT, {"wave_redesign": None})
    # As in test_wave_redesign.py: the report write records only the log it is given.
    monkeypatch.setattr(
        wr,
        "_write_report_log",
        lambda store, db, job_id, version, log: store.write_json(
            REPORT, {"wave_redesign": dict(log)}
        ),
    )
    return s


def test_round_two_left_pending_is_closed_stopped_with_the_earlier_design_back(
    store: LocalArtifactStore,
) -> None:
    """The stall: round 1 improves the design, round 2 opens, and the run goes on to its
    report with round 2 never designed."""
    two_holds = _report([_hold(), _hold("open_review")])
    assert plan_round(store, DB, JOB, two_holds, V)["round"] == 1
    store.write_json(DESIGN, {"design": "round 1"})
    status = plan_round(store, DB, JOB, _report([_hold()]), V)
    assert status["status"] == "awaiting_redesign" and status["round"] == 2
    # Round 2's design is set aside: the job has no DynamoDB design while it is open.
    assert not store.exists(DESIGN)

    # The run reaches its report: the close puts the round 1 design back.
    closed = stop_round(store, DB, JOB, V, "the run ended before the round ran")
    assert closed["status"] == "awaiting_synthesis" and closed["restored"] == ["dynamodb"]
    assert store.read_json(DESIGN) == {"design": "round 1"}
    assert not store.exists(request_key(DB, JOB, "dynamodb", V))
    log = read_log(store, DB, JOB, V)
    assert [r["outcome"] for r in log["rounds"]] == [
        {"dynamodb": "improved"},
        {"dynamodb": "stopped"},
    ]
    assert log["rounds"][1]["stopped_by"] == "closed"
    assert log["rounds"][1]["stopped"] == "the run ended before the round ran"
    # The report states it, and no round is left without an outcome.
    assert store.read_json(REPORT)["wave_redesign"] == log
    assert all(r.outcome is not None for r in WaveRedesignLog.model_validate(log).rounds)
    # Synthesized again, the check ends the loop: held, with why no further round ran.
    status = plan_round(store, DB, JOB, _report([_hold()]), V)
    assert status["status"] == "exhausted" and status["held"] == ["dynamodb"]
    assert status["stopped"] == "the run ended before the round ran"
    # Closing again changes nothing.
    assert stop_round(store, DB, JOB, V)["status"] == "no_open_round"
    assert store.read_json(DESIGN) == {"design": "round 1"}


def test_a_closed_round_ends_the_loop_with_rounds_left(store: LocalArtifactStore) -> None:
    plan_round(store, DB, JOB, _report([_hold()]), V)
    stop_round(store, DB, JOB, V)
    assert read_log(store, DB, JOB, V)["rounds"][0]["stopped"] == ("its design was never checked")
    # Another engine held now, one round left: still no round starts.
    status = plan_round(
        store, DB, JOB, _report([_hold(engine="documentdb")], engine="documentdb"), V
    )
    assert status["status"] == "exhausted" and status["held"] == ["documentdb"]
    assert len(read_log(store, DB, JOB, V)["rounds"]) == 1
    # Nothing a redesign can fix held: complete.
    assert plan_round(store, DB, JOB, _report([]), V)["status"] == "complete"


def test_a_synthesis_that_raises_mid_round_closes_it_stopped(store: LocalArtifactStore) -> None:
    calls = {"synthesis": 0}

    def design(engine: str) -> None:
        store.write_json(DESIGN, {"design": "second"})

    def synthesize() -> None:
        calls["synthesis"] += 1
        raise RuntimeError("synthesis failed")

    with pytest.raises(RuntimeError):
        run_rounds(
            store,
            DB,
            JOB,
            version=V,
            read_report=lambda: _report([_hold()]),
            design=design,
            synthesize=synthesize,
        )
    assert calls["synthesis"] == 1
    assert store.read_json(DESIGN) == {"design": "first"}
    rnd = read_log(store, DB, JOB, V)["rounds"][0]
    assert rnd["outcome"] == {"dynamodb": "stopped"} and rnd["stopped_by"] == "closed"
    assert rnd["stopped"] == "the run stopped before its design was checked (RuntimeError)"
    assert not store.exists(request_key(DB, JOB, "dynamodb", V))


def test_a_saved_log_with_round_two_pending_loads_and_closes(tmp_path: Path) -> None:
    """The evidence job's log as it was left (assignment v4, round 2 open, saved before
    ``stopped_by``) and its round 2 design set aside: it loads, and the close puts the
    design back."""
    store = LocalArtifactStore(base_dir=str(tmp_path))
    db, job, version = "discourse", "4acab5ab", 4
    saved = json.loads(FIXTURE.read_text())
    assert {r["assignment_version"] for r in saved["rounds"]} == {version}
    store.write_json(wr.log_key(db, job, version), saved)
    WaveRedesignLog.model_validate(read_log(store, db, job, version))
    design = f"{db}/{job}/schema-dynamodb/v{version}/schema_output.json"
    archived = f"{db}/{job}/wave-redesign/v{version}/round_2/schema-dynamodb/schema_output.json"
    store.write_json(archived, {"design": "round 1"})
    assert not store.exists(design)  # as the evidence job was left
    status = stop_round(store, db, job, version)
    assert status["status"] == "awaiting_synthesis" and status["round"] == 2
    assert store.read_json(design) == {"design": "round 1"}
    log = WaveRedesignLog.model_validate(read_log(store, db, job, version))
    assert [r.outcome for r in log.rounds] == [{"dynamodb": "improved"}, {"dynamodb": "stopped"}]
    assert log.rounds[0].stopped_by is None and log.rounds[1].stopped_by == "closed"
    facts = redesign_facts(log.model_dump(mode="json"))["dynamodb"]
    assert facts["rounds"] == 1 and facts["last"] == "stopped"
    assert facts["note"] == (
        "redesign round 2 was not completed: its design was never checked; the earlier "
        "design is kept"
    )
    assert facts["short_note"] == "redesign round 2 was not completed; the earlier design is kept"


def test_a_close_that_fails_never_hides_the_error_that_stopped_the_loop(
    store: LocalArtifactStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The synthesis error propagates even when putting the design back fails, and the
    log does not claim the round closed: it stays open, for a later --stop."""

    def design(engine: str) -> None:
        store.write_json(DESIGN, {"design": "second"})

    def synthesize() -> None:
        raise RuntimeError("synthesis failed")

    def put_back(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full during put back")

    monkeypatch.setattr(wr, "_put_back", put_back)
    with pytest.raises(RuntimeError, match="synthesis failed") as raised:
        run_rounds(
            store,
            DB,
            JOB,
            version=V,
            read_report=lambda: _report([_hold()]),
            design=design,
            synthesize=synthesize,
        )
    notes = getattr(raised.value, "__notes__", [])
    assert any("not closed (OSError: disk full during put back)" in n for n in notes)
    rnd = read_log(store, DB, JOB, V)["rounds"][0]
    assert rnd["outcome"] is None and "stopped_by" not in rnd
    assert wr._open_round(read_log(store, DB, JOB, V)) is not None


def test_the_close_leaves_an_open_wave_edit_round_to_its_command(
    store: LocalArtifactStore,
) -> None:
    """``/wave-edit`` checks or rolls back its own round (#2067): /modernize's close
    leaves it open, with its design set aside, and says so."""
    archived = f"{DB}/{JOB}/wave-redesign/v{V}/round_1/schema-dynamodb/schema_output.json"
    store.write_json(archived, {"design": "first"})
    store.delete(DESIGN)
    rnd = {"round": 1, "engines": ["dynamodb"], "outcome": None, "wave_edit": 2}
    store.write_json(LOG, {"max_rounds": 2, "rounds": [rnd]})
    status = stop_round(store, DB, JOB, V)
    assert status == {"status": "no_open_round", "rounds": 1, "open_wave_edit_round": 1}
    assert read_log(store, DB, JOB, V)["rounds"] == [rnd]
    assert not store.exists(DESIGN)


@pytest.mark.passes_on_main
def test_a_saved_ceiling_stop_without_stopped_by_still_reads_as_a_ceiling() -> None:
    saved = {
        "max_rounds": 2,
        "rounds": [
            {
                "round": 1,
                "engines": ["dynamodb"],
                "outcome": {"dynamodb": "stopped"},
                "stopped": "the run's tokens ceiling leaves no room",
            }
        ],
    }
    WaveRedesignLog.model_validate(saved)
    facts = redesign_facts(saved)["dynamodb"]
    assert facts["rounds"] == 0
    assert facts["short_note"] == "no further redesign round ran: a run ceiling stopped it"


def _closed_log() -> dict[str, Any]:
    return {
        "max_rounds": 2,
        "rounds": [
            {"round": 1, "engines": ["dynamodb"], "outcome": {"dynamodb": "improved"}},
            {
                "round": 2,
                "engines": ["dynamodb"],
                "outcome": {"dynamodb": "stopped"},
                "stopped": "the run ended before the round ran",
                "stopped_by": "closed",
            },
        ],
    }


def _wave_rows(*, risks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return wave_readiness(
        [{"wave": 1, "engines": ["dynamodb"]}],
        risks=risks,
        designed_engines=["dynamodb"],
        redesign_by_engine=redesign_facts(_closed_log()),
    )


def test_a_closed_round_never_reads_as_a_ceiling_and_keeps_its_wave_held() -> None:
    risk = {
        "risk_id": "RISK-001",
        "severity": "HIGH",
        "risk_type": "PERFORMANCE_DEGRADATION",
        "rule": "design.hot_partition",
        "engine": "dynamodb",
        "wave": 1,
        "receiving_engine_flags": True,
    }
    (row,) = _wave_rows(risks=[risk])
    assert row["readiness"] == "held" and row["suggested"] is False
    assert row["redesign_rounds"] == 1
    assert "redesign round 2 was not completed" in row["readiness_line"]
    assert "the run ended before the round ran" in row["readiness_line"]
    assert "ceiling" not in row["readiness_line_short"]
    assert "redesign round 2 was not completed; the earlier design is kept" in (
        row["readiness_line_short"]
    )


@pytest.mark.passes_on_main
def test_a_closed_round_suggests_its_wave_only_when_no_check_holds_it() -> None:
    (row,) = _wave_rows(risks=[])
    assert row["readiness"] == "suggested" and row["holds"] == []
    assert row["readiness_line"] == "Suggested: no check holds it after it was redesigned once."


def _script(root: Path, *args: str) -> dict[str, Any]:
    done = subprocess.run(
        [sys.executable, "scripts/run_wave_redesign.py", "--job-id", JOB, "--db", DB]
        + ["--artifact-root", str(root), *args],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    line: dict[str, Any] = json.loads(done.stdout.strip().splitlines()[-1])
    return line


def test_the_script_closes_the_open_round_and_is_a_no_op_after(tmp_path: Path) -> None:
    s = LocalArtifactStore(base_dir=str(tmp_path))
    s.write_json(f"{DB}/{JOB}/collector/output.json", {})
    s.write_json(f"{DB}/{JOB}/assignment/v{V}/assignment.json", {})
    s.write_json(DESIGN, {"design": "first"})
    plan_round(s, DB, JOB, _report([_hold()]), V)
    status = _script(tmp_path, "--stop", "--reason", "the user asked to stop the redesign")
    assert status["status"] == "awaiting_synthesis" and status["restored"] == ["dynamodb"]
    assert s.read_json(DESIGN) == {"design": "first"}
    rnd = read_log(s, DB, JOB, V)["rounds"][0]
    assert rnd["stopped"] == "the user asked to stop the redesign"
    assert _script(tmp_path, "--stop")["status"] == "no_open_round"
    assert _script(tmp_path, "--stop", "--rollback")["status"] == "error"
