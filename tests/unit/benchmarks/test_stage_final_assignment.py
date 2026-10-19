"""The final-assignment benchmark stage (#1074): a sample through the reality check.

Runs the real deterministic pipeline on the bundled wordpress sample (no
model, no network) once per module and checks the stage scores the
assignment the reality check leaves behind, not the resolver's first pass.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from benchmarks.runner import gate as gate_mod
from benchmarks.runner import run as run_mod
from benchmarks.runner.loader import Case, load_cases
from benchmarks.runner.run import run_benchmark, stage_runner
from benchmarks.runner.stage_final_assignment import (
    final_assignment,
    run_final_assignment_case,
    run_pipeline_to_final_assignment,
)
from src.agents.referee import assignment_resolver

REPO = Path(__file__).resolve().parents[3]
STAGE_DIR = REPO / "benchmarks" / "cases" / "final_assignment"


def _case(sample: str = "wordpress") -> Case:
    return Case(sample, STAGE_DIR / sample, "", [], False, {}, {}, {}, {}, sample=sample)


def _engines(doc: dict) -> dict[str, str]:
    return {qa["query_id"]: qa["assigned_engine"] for qa in doc["query_assignments"]}


@pytest.fixture(scope="module")
def wordpress_run(tmp_path_factory):
    store, prefix = run_pipeline_to_final_assignment(
        _case(), tmp_path_factory.mktemp("final-wordpress")
    )
    return store, prefix


def test_the_stage_scores_the_reality_checks_version_not_the_first_pass(wordpress_run):
    store, prefix = wordpress_run
    final = final_assignment(store, "wordpress")
    first = _engines(store.read_json(f"{prefix}/assignment/v1/assignment.json"))
    assert final["source"] == "reality_check"
    assert final["version"] > 1
    moved = [q for q, e in _engines(final).items() if first[q] != e]
    # the reality check consolidates some wordpress queries; scoring v1 would miss that
    assert moved
    assert set(_engines(final)) == set(first)


def test_run_final_assignment_case_maps_every_query(wordpress_run, tmp_path):
    store, _ = wordpress_run
    expected = _engines(final_assignment(store, "wordpress"))
    got = run_final_assignment_case(_case(), tmp_path)
    assert got == expected  # deterministic: a second run gives the same final engines
    collection = json.loads(
        (tmp_path / "artifacts/wordpress/case/collector/output.json").read_text()
    )
    assert len(got) == len(collection["queries"]["query_patterns"]) == 107


def test_without_a_reality_check_version_the_stage_reads_v1(tmp_path):
    """No consolidation writes no version; the final assignment is then v1."""

    class _Store:
        def __init__(self):
            self.docs = {"db/case/assignment/v1/assignment.json": {"version": 1}}

        def list_prefix(self, prefix):
            return [k for k in self.docs if k.startswith(prefix)]

        def read_json(self, key):
            return self.docs[key]

    assert final_assignment(_Store(), "db") == {"version": 1}


def _bias_toward(engine: str, bonus: int = 60):
    real = assignment_resolver.score_query_for_engine

    def biased(query, candidate, ctx):
        result = real(query, candidate, ctx)
        if candidate == engine and result.score is not None:
            result = dataclasses.replace(result, score=min(100, result.score + bonus))
        return result

    return biased


def test_a_deliberate_routing_regression_moves_the_final_assignment(
    wordpress_run, tmp_path, monkeypatch
):
    """Routing runs in-process, so a scorer change reaches the scored engines
    (and a reviewed key with a floor would fail the gate)."""
    store, _ = wordpress_run
    honest = _engines(final_assignment(store, "wordpress"))
    monkeypatch.setattr(assignment_resolver, "score_query_for_engine", _bias_toward("dynamodb"))
    biased = run_final_assignment_case(_case(), tmp_path)
    moved = [q for q in honest if biased[q] != honest[q]]
    assert moved

    # A test-only key (never committed: real keys come from review) that accepts
    # exactly the unbiased engines, gated at 1.0, fails on the biased run.
    stage = tmp_path / "stage"
    (stage / "wordpress").mkdir(parents=True)
    (stage / "index.json").write_text(
        json.dumps(
            {
                "stage": "final_assignment",
                "cases": [{"id": "wordpress", "path": "wordpress", "sample": "wordpress"}],
            }
        )
    )
    (stage / "wordpress" / "expected.json").write_text(
        json.dumps(
            {"reviewed": True, "expected": {q: {"acceptable": [e]} for q, e in honest.items()}}
        )
    )
    report = run_benchmark(stage, work_root=tmp_path / "work")
    assert report["complete"] is True
    gate = {
        "min_aggregate_acceptable_accuracy": 1.0,
        "min_case_acceptable_accuracy": {"wordpress": 1.0},
    }
    failures = gate_mod.check_gate(report, gate)
    assert any(f.startswith("wordpress: acceptable-accuracy") for f in failures)


# --- the committed stage ------------------------------------------------------


def test_the_committed_stage_lists_the_three_samples_unreviewed():
    cases = load_cases(STAGE_DIR)
    assert [c.case_id for c in cases] == ["wordpress", "discourse", "trailgear"]
    for c in cases:
        assert c.sample == c.case_id
        assert (REPO / "docs" / "examples" / c.sample / f"{c.sample}.zip").is_file()
        # keys are written with the maintainer from review (#1074), never by a tool
        assert c.reviewed is False
    assert stage_runner(STAGE_DIR) is run_final_assignment_case


def test_unreviewed_cases_are_skipped_without_running(monkeypatch, tmp_path):
    def boom(case, work_dir):
        raise AssertionError("an unreviewed case must not run by default")

    monkeypatch.setitem(run_mod.STAGE_RUNNERS, "final_assignment", boom)
    report = run_benchmark(STAGE_DIR, work_root=tmp_path)
    assert report["skipped_cases"] == 3
    assert report["scored_cases"] == 0
    assert all(c["assigned_queries"] is None for c in report["per_case"])


def test_run_unreviewed_runs_but_never_scores_or_gates(monkeypatch, tmp_path):
    monkeypatch.setitem(
        run_mod.STAGE_RUNNERS, "final_assignment", lambda case, work_dir: {"q1": "dynamodb"}
    )
    report = run_benchmark(STAGE_DIR, work_root=tmp_path, run_unreviewed=True)
    assert report["complete"] is True
    assert report["skipped_cases"] == 3
    assert report["scored_cases"] == 0
    assert report["total_scored_queries"] == 0
    assert [c["assigned_queries"] for c in report["per_case"]] == [1, 1, 1]
    assert all(c["acceptable_accuracy"] is None for c in report["per_case"])
    # a floor on an unreviewed case fails as "not scored", it never passes on a dry run
    gate = {
        "min_aggregate_acceptable_accuracy": 0.0,
        "min_case_acceptable_accuracy": {"wordpress": 0.0},
    }
    assert gate_mod.check_gate(report, gate) == [
        "wordpress: gated case was not scored (outcome skipped)"
    ]


def test_a_missing_sample_errors_the_case_not_the_batch(tmp_path):
    stage = tmp_path / "stage"
    (stage / "nope").mkdir(parents=True)
    (stage / "index.json").write_text(
        json.dumps(
            {
                "stage": "final_assignment",
                "cases": [{"id": "nope", "path": "nope", "sample": "nope"}],
            }
        )
    )
    (stage / "nope" / "expected.json").write_text(json.dumps({"reviewed": True, "expected": {}}))
    report = run_benchmark(stage, work_root=tmp_path / "work")
    assert report["errored_cases"] == 1
    assert report["complete"] is False


def test_an_unknown_stage_is_refused(tmp_path):
    (tmp_path / "index.json").write_text(json.dumps({"stage": "nope", "cases": []}))
    with pytest.raises(ValueError, match="unknown benchmark stage"):
        stage_runner(tmp_path)


def test_an_unreviewed_case_that_errors_stays_skipped_and_never_fails_the_gate(tmp_path):
    """With --run-unreviewed, a failing unreviewed case must not make the run
    incomplete (which fails the gate): it is skipped, like any unreviewed case."""
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "index.json").write_text(
        json.dumps(
            {
                "stage": "final_assignment",
                "cases": [
                    {"id": "good", "path": "good", "sample": "good"},
                    {"id": "draft", "path": "draft", "sample": "draft"},
                ],
            }
        )
    )
    for cid, reviewed in (("good", True), ("draft", False)):
        (stage / cid).mkdir()
        (stage / cid / "expected.json").write_text(
            json.dumps({"reviewed": reviewed, "expected": {"q1": {"acceptable": ["dynamodb"]}}})
        )

    def runner(case, work_dir):
        if case.case_id == "draft":
            raise RuntimeError("collect failed")
        return {"q1": "dynamodb"}

    gate = {"min_aggregate_acceptable_accuracy": 1.0, "min_case_acceptable_accuracy": {"good": 1.0}}
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(run_mod.STAGE_RUNNERS, "final_assignment", runner)
        for flag in (False, True):
            report = run_benchmark(stage, work_root=tmp_path / str(flag), run_unreviewed=flag)
            assert report["complete"] is True
            assert report["errored_cases"] == 0
            assert [c["outcome"] for c in report["per_case"]] == ["scored", "skipped"]
            assert gate_mod.check_gate(report, gate) == []


SNAPSHOT = REPO / "tests" / "e2e" / "baselines" / "routing_snapshot.json"


@pytest.mark.parametrize("sample", ["wordpress", "discourse", "trailgear"])
def test_the_stage_matches_the_e2e_pipelines_final_engines(sample, tmp_path):
    """ADR-030 Amendment 1: the stage scores what the deliverables show. The
    routing snapshot records the e2e pipeline's final engine per query (the
    reality check's ``r`` where it moved the query, else the first pass ``f``),
    and the e2e shards keep it current; the stage must agree on every query."""
    snapshot = json.loads(SNAPSHOT.read_text())["samples"][sample]["queries"]
    expected = {q: (e.get("r") or e["f"])["e"] for q, e in snapshot.items()}
    got = run_final_assignment_case(_case(sample), tmp_path)
    assert set(got) == set(expected)
    assert {q: e for q, e in got.items() if expected[q] != e} == {}
