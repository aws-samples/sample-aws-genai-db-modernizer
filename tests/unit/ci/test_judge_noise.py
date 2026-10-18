"""How much the reported judge score moves on unchanged content, single
sample against the median of three (#952).

The fixture holds the recorded per-criterion scores of 93 saved wordpress
e2e-llm runs, in two groups whose deliverables carry the same routing and
risk content. Every unordered triple of runs within a group stands in for
one median-of-three judging, through ``judge.aggregate_samples`` (the code
``run_judge`` uses). That is exhaustive and deterministic: no sampling, no
model. Runs in a group differ a little in wording (the schema-design phase
is a model call), so this over-states the judge's own noise rather than
under-stating it.
"""

from __future__ import annotations

import itertools
import json
import random
import statistics
from pathlib import Path

import pytest

from ci.llm import judge

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/judge-noise/wordpress-repeat-scores.json").read_text()
)
CRITERIA = FIXTURE["criteria"]
GROUPS = FIXTURE["groups"]


def _mean(scores: dict) -> float:
    return statistics.fmean(scores[c] for c in CRITERIA)


def _triple_means(runs: list[dict]) -> list[float]:
    return [
        _mean(judge.aggregate_samples([a["scores"], b["scores"], c["scores"]])["scores"])
        for a, b, c in itertools.combinations(runs, 3)
    ]


@pytest.mark.parametrize(
    "group", GROUPS, ids=lambda g: f"{g['open_risks']}open-{len(g['runs'])}runs"
)
def test_median_of_three_keeps_the_mean_within_a_tenth(group: dict) -> None:
    runs = group["runs"]
    single = [_mean(r["scores"]) for r in runs]
    median3 = _triple_means(runs)
    sd_single = statistics.pstdev(single)
    sd_median3 = statistics.pstdev(median3)
    assert sd_median3 <= 0.1, (sd_single, sd_median3)
    assert sd_median3 < sd_single


@pytest.mark.parametrize(
    "group", GROUPS, ids=lambda g: f"{g['open_risks']}open-{len(g['runs'])}runs"
)
def test_median_of_three_never_flips_a_criterion_more_often(group: dict) -> None:
    runs = group["runs"]
    for crit in CRITERIA:
        values = [r["scores"][crit] for r in runs]
        centre = statistics.median(values)
        single_flips = sum(v != centre for v in values) / len(values)
        medians = [
            judge.aggregate_samples([a["scores"], b["scores"], c["scores"]])["scores"][crit]
            for a, b, c in itertools.combinations(runs, 3)
        ]
        median_flips = sum(m != centre for m in medians) / len(medians)
        assert median_flips <= single_flips + 1e-9, (crit, single_flips, median_flips)


def test_a_coin_flip_criterion_stays_a_coin_flip_under_the_median() -> None:
    """Why tone is split and re-anchored as well (#952 item 4): in the second
    group tone is 3 in 22 runs and 4 in 23, and the median of three still
    lands on either side about half the time. Sampling more cannot fix an
    anchor the judge reads both ways."""
    runs = GROUPS[1]["runs"]
    tones = [r["scores"]["tone"] for r in runs]
    assert sorted(set(tones)) == [3, 4]
    medians = [
        judge.aggregate_samples([a["scores"], b["scores"], c["scores"]])["scores"]["tone"]
        for a, b, c in itertools.combinations(runs, 3)
    ]
    share_of_threes = sum(m == 3 for m in medians) / len(medians)
    assert 0.4 < share_of_threes < 0.6


@pytest.mark.parametrize(
    "group", GROUPS, ids=lambda g: f"{g['open_risks']}open-{len(g['runs'])}runs"
)
def test_the_baseline_rule_rarely_fails_unchanged_content(group: dict) -> None:
    """Two median-of-3 judgings of the same content, one as the baseline: the
    regression rule (2 points on one criterion, or 1 point on two) fails under
    2% of such pairs, where "any one-point drop" failed about a fifth (#952
    review). 20,000 pairs drawn with a fixed seed."""
    runs = group["runs"]
    medians = [
        judge.aggregate_samples([runs[i]["scores"], runs[j]["scores"], runs[k]["scores"]])["scores"]
        for i, j, k in itertools.combinations(range(len(runs)), 3)
    ]
    rng = random.Random(952)
    pairs = 20_000
    rule = any_drop = 0
    for _ in range(pairs):
        base, cand = rng.sample(medians, 2)
        deltas = {c: cand[c] - base[c] for c in CRITERIA}
        rule += bool(judge.regressions(deltas))
        any_drop += any(d <= -1 for d in deltas.values())
    assert rule / pairs < 0.02, rule / pairs
    assert any_drop / pairs > 0.15  # the rule this replaced
