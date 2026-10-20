"""The routing-quality gate on the answer-key benchmark (#952, ADR-030).

The judge grades the deliverables, not the engine choice; this gate is what
fails a routing regression. It runs the real assignment stage on the
committed, human-reviewed cases and checks each case's acceptable-accuracy
against the floor in ``benchmarks/cases/assignment/gate.json``. No model,
no network.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from benchmarks.runner import gate as gate_mod
from benchmarks.runner.run import main, run_benchmark
from src.agents.referee import assignment_resolver

STAGE_DIR = Path(__file__).resolve().parents[3] / "benchmarks" / "cases" / "assignment"


def test_committed_cases_pass_the_gate(tmp_path: Path) -> None:
    report = run_benchmark(STAGE_DIR, work_root=tmp_path)
    gate = gate_mod.load_gate(STAGE_DIR)
    assert gate_mod.check_gate(report, gate) == []


def test_every_reviewed_case_has_a_floor() -> None:
    """A reviewed case without a floor would be scored but never gate anything."""
    gate = gate_mod.load_gate(STAGE_DIR)
    index = json.loads((STAGE_DIR / "index.json").read_text())
    reviewed = []
    for case in index["cases"]:
        expected = json.loads((STAGE_DIR / case["path"] / "expected.json").read_text())
        if expected.get("reviewed"):
            reviewed.append(case["id"])
    assert reviewed
    assert sorted(gate["min_case_acceptable_accuracy"]) == sorted(reviewed)


CASES_DIR = STAGE_DIR.parent


@pytest.mark.parametrize("stage", sorted(p.name for p in CASES_DIR.iterdir() if p.is_dir()))
def test_every_stage_gates_exactly_its_reviewed_cases(stage: str) -> None:
    """Every stage, not only ``assignment`` (#1074): a reviewed case needs a
    floor in that stage's gate.json, and an unreviewed case never has one. A
    stage with no reviewed case yet may have no gate.json at all."""
    stage_dir = CASES_DIR / stage
    index = json.loads((stage_dir / "index.json").read_text())
    reviewed = sorted(
        c["id"]
        for c in index["cases"]
        if json.loads((stage_dir / c["path"] / "expected.json").read_text()).get("reviewed")
    )
    if not reviewed:
        # load_gate refuses empty floors, so a stage with no reviewed case has no gate.json.
        assert not (stage_dir / "gate.json").exists(), f"{stage}: gate.json but no reviewed case"
    else:
        # load_gate raises when gate.json is missing.
        assert sorted(gate_mod.load_gate(stage_dir)["min_case_acceptable_accuracy"]) == reviewed


def _bias_toward(engine: str, bonus: int = 60):
    """A deliberate routing regression: an engine preference added on top of
    the shared scorer (exactly what tests/unit/agents/referee/test_no_engine_bias.py
    forbids), applied where the resolver calls it."""
    real = assignment_resolver.score_query_for_engine

    def biased(query, candidate, ctx):
        result = real(query, candidate, ctx)
        if candidate == engine and result.score is not None:
            result = dataclasses.replace(result, score=min(100, result.score + bonus))
        return result

    return biased


def test_a_deliberate_routing_regression_fails_the_gate(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(assignment_resolver, "score_query_for_engine", _bias_toward("aurora_mysql"))
    report = run_benchmark(STAGE_DIR, work_root=tmp_path)
    # a wrong but valid assignment, not a crash: the run is complete and scored
    assert report["complete"] is True
    failures = gate_mod.check_gate(report, gate_mod.load_gate(STAGE_DIR))
    assert "wordpress-trimmed: acceptable-accuracy 0.6 is below the floor 1.0" in failures


def test_the_cli_exits_non_zero_on_a_gate_failure(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(assignment_resolver, "score_query_for_engine", _bias_toward("aurora_mysql"))
    monkeypatch.setattr("sys.argv", ["run", "--stage", "assignment", "--gate"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    assert "GATE FAILED" in capsys.readouterr().out


def test_the_cli_passes_the_gate_on_current_code(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.argv", ["run", "--stage", "assignment", "--gate"])
    main()  # no SystemExit
    assert "gate: pass" in capsys.readouterr().out


# --- check_gate on synthetic reports ---------------------------------------


def _report(per_case: dict[str, float | None], *, complete: bool = True, agg: float = 1.0) -> dict:
    return {
        "complete": complete,
        "aggregate_acceptable_accuracy": agg,
        "per_case": [
            {
                "case_id": cid,
                "outcome": "scored" if acc is not None else "skipped",
                "acceptable_accuracy": acc,
            }
            for cid, acc in per_case.items()
        ],
    }


GATE = {
    "min_aggregate_acceptable_accuracy": 0.9,
    "min_case_acceptable_accuracy": {"a": 1.0, "b": 0.8},
}


def test_check_gate_passes_at_the_floor() -> None:
    assert gate_mod.check_gate(_report({"a": 1.0, "b": 0.8}, agg=0.9), GATE) == []


def test_check_gate_names_each_case_below_its_floor() -> None:
    failures = gate_mod.check_gate(_report({"a": 0.8, "b": 0.6}, agg=0.7), GATE)
    assert len(failures) == 3
    assert any(f.startswith("a:") for f in failures)
    assert any(f.startswith("b:") for f in failures)
    assert any(f.startswith("aggregate") for f in failures)


def test_a_gated_case_that_was_not_scored_fails() -> None:
    """Flipping a case to reviewed: false, deleting it, or an errored run must
    not pass the gate by dropping the case."""
    assert any("b:" in f for f in gate_mod.check_gate(_report({"a": 1.0, "b": None}), GATE))
    assert any("b:" in f for f in gate_mod.check_gate(_report({"a": 1.0}), GATE))
    failures = gate_mod.check_gate(_report({"a": 1.0, "b": 1.0}, complete=False), GATE)
    assert any("incomplete" in f for f in failures)


def test_a_case_with_no_floor_is_not_gated() -> None:
    assert gate_mod.check_gate(_report({"a": 1.0, "b": 0.9, "new": 0.0}, agg=0.9), GATE) == []


def test_load_gate_rejects_a_bad_file(tmp_path: Path) -> None:
    with pytest.raises(gate_mod.GateError):
        gate_mod.load_gate(tmp_path)  # no gate.json
    (tmp_path / "gate.json").write_text(
        json.dumps(
            {"min_aggregate_acceptable_accuracy": 1.0, "min_case_acceptable_accuracy": {"a": 2}}
        )
    )
    with pytest.raises(gate_mod.GateError):
        gate_mod.load_gate(tmp_path)
    # a missing aggregate floor, or no case floors, would leave part of the run ungated
    for doc in (
        {"min_case_acceptable_accuracy": {"a": 1.0}},
        {"min_aggregate_acceptable_accuracy": 1.0},
        {"min_aggregate_acceptable_accuracy": 1.0, "min_case_acceptable_accuracy": {}},
    ):
        (tmp_path / "gate.json").write_text(json.dumps(doc))
        with pytest.raises(gate_mod.GateError):
            gate_mod.load_gate(tmp_path)
    (tmp_path / "gate.json").write_text("not json")
    with pytest.raises(gate_mod.GateError):
        gate_mod.load_gate(tmp_path)
