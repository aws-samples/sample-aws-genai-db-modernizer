from benchmarks.runner.models import Outcome, QueryVerdict, ScoreResult
from benchmarks.runner.report import CaseRun, build_report


def _scored(acc):
    n = 4
    ok = int(acc * n)
    verdicts = [QueryVerdict(f"q{i}", "dynamodb", i < ok, False) for i in range(n)]
    return ScoreResult(n, ok, 0, 0, verdicts, [])


def test_aggregate_over_scored_only():
    runs = [
        CaseRun("a", Outcome.SCORED, _scored(1.0)),
        CaseRun("b", Outcome.SCORED, _scored(0.5)),
        CaseRun("c", Outcome.THROTTLED, None),
    ]
    rep = build_report(runs)
    # throttled excluded from denominator
    assert rep["scored_cases"] == 2
    assert rep["throttled_cases"] == 1
    assert rep["aggregate_acceptable_accuracy"] == 0.75  # (1.0 + 0.5)/2
    assert rep["complete"] is False  # a throttle makes the run incomplete


def test_complete_when_all_scored():
    runs = [CaseRun("a", Outcome.SCORED, _scored(1.0))]
    rep = build_report(runs)
    assert rep["complete"] is True


def test_banner_lists_throttled():
    runs = [CaseRun("a", Outcome.THROTTLED, None)]
    rep = build_report(runs)
    assert "INCOMPLETE RUN" in rep["banner"]
    assert "a" in rep["banner"]
