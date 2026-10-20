"""The anti_pattern benchmark stage (#1084): a key's anti-pattern verdict against the pipeline's.

Runs the real deterministic pipeline on the bundled wordpress sample (no model,
no network) for the fixture-case tests. Keys written here are test-only and
never committed: real keys are written by the maintainer from the blind sheets.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.runner import gate as gate_mod
from benchmarks.runner import run as run_mod
from benchmarks.runner.anti_pattern_cases import PENALTY_SWEEP, penalised_patterns, penalty_set_to
from benchmarks.runner.loader import Case, load_cases
from benchmarks.runner.run import run_benchmark, stage_runner
from benchmarks.runner.scoring import score_anti_pattern
from benchmarks.runner.stage_anti_pattern import pipeline_flags, run_anti_pattern_case
from src.agents.referee import query_scoring
from src.agents.referee.redesign_findings import FIXABLE, AntiPatternFinding

REPO = Path(__file__).resolve().parents[3]
STAGE_DIR = REPO / "benchmarks" / "cases" / "anti_pattern"
SNAPSHOT = REPO / "tests" / "e2e" / "baselines" / "routing_snapshot.json"
CASE_ID = "wordpress-frequent-full-scan"


# --- scoring ------------------------------------------------------------------


def test_score_compares_the_verdict_and_the_engine_where_the_key_gives_one():
    actual = {
        "a": {"shows": True, "engine": "aurora_mysql"},
        "b": {"shows": False, "engine": "dynamodb"},
        "c": {"shows": True, "engine": "dynamodb"},
        "extra": {"shows": False, "engine": "dynamodb"},
    }
    expected = {
        "a": {"shows": True, "acceptable": ["aurora_mysql"], "ideal": "aurora_mysql"},
        "b": {"shows": True},  # the pipeline missed it
        "c": {"shows": True, "acceptable": ["aurora_mysql"]},  # right verdict, wrong engine
        "gone": {"shows": False},  # not returned: a miss
    }
    result = score_anti_pattern(actual, expected)
    assert result.scored_count == 4
    assert [(v.query_id, v.acceptable) for v in result.per_query] == [
        ("a", True),
        ("b", False),
        ("c", False),
        ("gone", False),
    ]
    assert result.acceptable_accuracy == 0.25
    assert result.metrics == {"verdict_accuracy": 0.5, "engine_accuracy": 0.5}
    assert (result.ideal_defined_count, result.ideal_count) == (1, 1)
    assert result.unmatched == ["extra"]


def test_a_verdict_only_key_reports_no_engine_accuracy():
    result = score_anti_pattern(
        {"a": {"shows": False, "engine": "x"}}, {"a": {"shows": False, "rationale": "r"}}
    )
    assert result.acceptable_accuracy == 1.0
    assert result.metrics == {"verdict_accuracy": 1.0}


@pytest.mark.parametrize("key", [{}, {"shows": "yes"}, {"shows": 1}, {"acceptable": ["x"]}])
def test_a_key_without_a_boolean_verdict_is_refused(key):
    with pytest.raises(ValueError, match="shows"):
        score_anti_pattern({"a": {"shows": True, "engine": "x"}}, {"a": key})


def test_a_redesign_answer_is_scored_and_picks_the_after_redesign_set():
    """#1231: a key may say whether the finding is intrinsic or fixable by redesign; a
    fixable query's engine is judged against the engines acceptable after the redesign,
    which routing assumes."""
    actual = {
        "a": {"shows": True, "engine": "dynamodb", "redesign": "fixable"},
        "b": {"shows": True, "engine": "aurora_mysql", "redesign": "fixable"},
        "c": {"shows": True, "engine": "aurora_mysql", "redesign": "intrinsic"},
    }
    expected = {
        "a": {
            "shows": True,
            "redesign": "fixable",
            "acceptable": ["aurora_mysql"],
            "acceptable_after_redesign": ["dynamodb", "aurora_mysql"],
        },
        "b": {"shows": True, "redesign": "intrinsic"},  # labels disagree
        "c": {
            "shows": True,
            "redesign": "intrinsic",
            "acceptable": ["aurora_mysql"],
            "acceptable_after_redesign": ["dynamodb"],
        },  # intrinsic: today's set
    }
    result = score_anti_pattern(actual, expected)
    assert [(v.query_id, v.acceptable) for v in result.per_query] == [
        ("a", True),
        ("b", False),
        ("c", True),
    ]
    assert result.metrics == {
        "verdict_accuracy": 1.0,
        "redesign_accuracy": 2 / 3,
        "engine_accuracy": 1.0,
    }


@pytest.mark.parametrize(
    "key",
    [
        {"shows": True, "redesign": "maybe"},
        {"shows": False, "redesign": "fixable"},
    ],
)
def test_a_redesign_answer_needs_a_known_label_and_a_shown_pattern(key):
    with pytest.raises(ValueError, match="redesign"):
        score_anti_pattern({"a": {"shows": True, "engine": "x"}}, {"a": key})


def test_pipeline_redesign_reads_the_label_of_the_pattern_and_its_aliases():
    from benchmarks.runner.stage_anti_pattern import pipeline_redesign

    record = {
        "queries": [
            {
                "query_id": "q1",
                "anti_pattern_findings": [
                    {"engine": "dynamodb", "pattern": "complex_aggregation", "kind": "fixable"}
                ],
            },
            {
                "query_id": "q2",
                "anti_pattern_findings": [
                    {"engine": "dynamodb", "pattern": "complex-aggregation", "kind": "intrinsic"}
                ],
            },
            {
                "query_id": "q3",
                "anti_pattern_findings": [
                    {"engine": "dynamodb", "pattern": "frequent-full-scan", "kind": "fixable"}
                ],
            },
            {"query_id": "q4"},  # a version 1 record row
        ]
    }
    assert pipeline_redesign(record, "complex-aggregation") == {"q1": "fixable", "q2": "intrinsic"}
    assert pipeline_redesign(None, "complex-aggregation") == {}


def test_pipeline_flags_reads_both_lists_and_the_aliases():
    analyses = {
        "dynamodb": {
            "workload_analysis": {
                "anti_patterns_detected": [
                    {"anti_pattern_type": "complex_aggregation", "query_ids": ["q1"]},
                    {"anti_pattern_type": "frequent-full-scan", "query_ids": ["q9"]},
                ]
            }
        },
        "aurora_mysql": {
            "workload_analysis": {
                "patterns_detected": [{"pattern_type": "complex-aggregation", "query_ids": ["q2"]}]
            }
        },
        "empty": {},
    }
    assert pipeline_flags(analyses, "complex-aggregation") == {"q1", "q2"}
    assert pipeline_flags(analyses, "wildcard-search") == set()


# --- a reviewed fixture case on the real pipeline --------------------------------


def _committed_case(case_id: str = CASE_ID) -> Case:
    return next(c for c in load_cases(STAGE_DIR) if c.case_id == case_id)


def _fixture_stage(root: Path, expected: dict, case: Case) -> Path:
    stage = root / "stage"
    (stage / case.case_id).mkdir(parents=True)
    (stage / "index.json").write_text(
        json.dumps(
            {
                "stage": "anti_pattern",
                "cases": [
                    {
                        "id": case.case_id,
                        "path": case.case_id,
                        "sample": case.sample,
                        "anti_pattern": case.anti_pattern,
                    }
                ],
            }
        )
    )
    (stage / case.case_id / "expected.json").write_text(
        json.dumps({"reviewed": True, "queries": case.queries, "expected": expected})
    )
    return stage


@pytest.fixture(scope="module")
def pipeline_answer(tmp_path_factory):
    """The pipeline's own verdict and engine for the committed case's queries."""
    return run_anti_pattern_case(_committed_case(), tmp_path_factory.mktemp("ap") / "run" / "c")


def test_the_stage_returns_a_verdict_and_an_engine_for_every_case_query(pipeline_answer):
    case = _committed_case()
    assert sorted(pipeline_answer) == sorted(case.queries)
    assert all(isinstance(v["shows"], bool) and v["engine"] for v in pipeline_answer.values())
    # the case mixes queries the pipeline flags with ones it does not
    assert {v["shows"] for v in pipeline_answer.values()} == {True, False}


def test_the_stage_scores_a_reviewed_fixture_case(pipeline_answer, tmp_path):
    case = _committed_case()
    flipped = sorted(pipeline_answer)[0]
    expected = {
        q: {
            "shows": (not v["shows"]) if q == flipped else v["shows"],
            "acceptable": [v["engine"]],
        }
        for q, v in pipeline_answer.items()
    }
    report = run_benchmark(_fixture_stage(tmp_path, expected, case), work_root=tmp_path / "w")
    row = report["per_case"][0]
    n = len(expected)
    assert row["outcome"] == "scored"
    assert row["acceptable_accuracy"] == round((n - 1) / n, 4)
    assert row["verdict_accuracy"] == round((n - 1) / n, 4)
    assert row["engine_accuracy"] == 1.0
    gate = {
        "min_aggregate_acceptable_accuracy": 0.0,
        "min_case_acceptable_accuracy": {case.case_id: 1.0},
    }
    assert gate_mod.check_gate(report, gate) == [
        f"{case.case_id}: acceptable-accuracy {row['acceptable_accuracy']} is below the floor 1.0"
    ]


def test_a_different_penalty_moves_the_scored_engines(pipeline_answer, tmp_path):
    """The stage reads the scorer in-process: some point of the penalty sweep moves
    a case query off the engine a key accepted, which is what lets the keys tell a
    right penalty from a wrong one (#1084)."""
    case = _committed_case()
    expected = {
        q: {"shows": v["shows"], "acceptable": [v["engine"]]} for q, v in pipeline_answer.items()
    }
    stage = _fixture_stage(tmp_path, expected, case)
    accuracies = []
    for points in PENALTY_SWEEP:
        with penalty_set_to(case.anti_pattern, points):
            report = run_benchmark(stage, work_root=tmp_path / f"w{points}")
        accuracies.append(report["per_case"][0]["engine_accuracy"])
    assert min(accuracies) < 1.0
    assert 1.0 in accuracies  # the catalog's own value is in the sweep
    # the verdict is the analysis's: the penalty never changes it
    with penalty_set_to(case.anti_pattern, 0):
        report = run_benchmark(stage, work_root=tmp_path / "v")
    assert report["per_case"][0]["verdict_accuracy"] == 1.0


def test_penalty_set_to_restores_the_catalog_value_the_aliases_and_the_labels():
    """The sweep prices the swept pattern's fixable findings too (#1231): inside it,
    ``query_scoring.fixable_keys`` leaves them out; on exit the scorer is restored."""
    before = dict(query_scoring.ANTI_PATTERN_PENALTIES)
    real_fixable = query_scoring.fixable_keys
    labelled = (
        AntiPatternFinding("q1", "dynamodb", "complex_aggregation", 30, FIXABLE, ""),
        AntiPatternFinding("q2", "dynamodb", "frequent-full-scan", 20, FIXABLE, ""),
    )
    with pytest.raises(RuntimeError):
        with penalty_set_to("complex-aggregation", 0):
            assert query_scoring.ANTI_PATTERN_PENALTIES["complex-aggregation"] == 0
            assert query_scoring.ANTI_PATTERN_PENALTIES["complex_aggregation"] == 0
            assert query_scoring.fixable_keys(labelled) == {
                ("q2", "dynamodb", "frequent-full-scan")
            }
            raise RuntimeError
    assert query_scoring.ANTI_PATTERN_PENALTIES == before
    assert query_scoring.fixable_keys is real_fixable
    assert len(query_scoring.fixable_keys(labelled)) == 2


# --- the committed stage ------------------------------------------------------


def test_the_committed_cases_are_unreviewed_with_no_key():
    cases = load_cases(STAGE_DIR)
    assert cases
    snapshot = json.loads(SNAPSHOT.read_text())["samples"]
    penalised = set(penalised_patterns())
    for c in cases:
        # keys are written by the maintainer from the sheets (ADR-030 Amendment 2)
        assert c.reviewed is False and c.expected == {}, c.case_id
        assert c.case_id == f"{c.sample}-{c.anti_pattern}"
        assert c.anti_pattern in penalised
        assert (REPO / "docs" / "examples" / c.sample / f"{c.sample}.zip").is_file()
        assert c.queries and c.queries == sorted(set(c.queries))
        assert set(c.queries) <= set(snapshot[c.sample]["queries"])
    assert stage_runner(STAGE_DIR) is run_anti_pattern_case
    assert run_mod.STAGE_SCORERS["anti_pattern"] is score_anti_pattern


def test_unreviewed_cases_are_skipped_without_running(monkeypatch, tmp_path):
    def boom(case, work_dir):
        raise AssertionError("an unreviewed case must not run by default")

    monkeypatch.setitem(run_mod.STAGE_RUNNERS, "anti_pattern", boom)
    report = run_benchmark(STAGE_DIR, work_root=tmp_path)
    assert report["skipped_cases"] == len(load_cases(STAGE_DIR))
    assert report["scored_cases"] == 0 and report["complete"] is True


def test_run_unreviewed_never_scores_or_gates(monkeypatch, tmp_path):
    monkeypatch.setitem(
        run_mod.STAGE_RUNNERS,
        "anti_pattern",
        lambda case, work_dir: {"q": {"shows": True, "engine": "dynamodb"}},
    )
    report = run_benchmark(STAGE_DIR, work_root=tmp_path, run_unreviewed=True)
    assert report["complete"] is True
    assert report["scored_cases"] == 0
    assert all(c["outcome"] == "skipped" for c in report["per_case"])
    gate = {
        "min_aggregate_acceptable_accuracy": 0.0,
        "min_case_acceptable_accuracy": {CASE_ID: 0.0},
    }
    assert gate_mod.check_gate(report, gate) == [
        f"{CASE_ID}: gated case was not scored (outcome skipped)"
    ]


def test_a_malformed_key_or_case_errors_the_case_not_the_batch(monkeypatch, tmp_path):
    case = _committed_case()
    monkeypatch.setitem(
        run_mod.STAGE_RUNNERS,
        "anti_pattern",
        lambda c, w: {q: {"shows": True, "engine": "x"} for q in c.queries},
    )
    stage = _fixture_stage(tmp_path, {case.queries[0]: {"acceptable": ["x"]}}, case)
    report = run_benchmark(stage, work_root=tmp_path / "w")
    assert report["errored_cases"] == 1 and report["complete"] is False

    no_pattern = Case("x", tmp_path, "", [], True, {}, {}, {}, {}, sample="wordpress")
    with pytest.raises(ValueError, match="anti_pattern"):
        run_anti_pattern_case(no_pattern, tmp_path / "r" / "x")
