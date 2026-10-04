"""Routing regression guard (see ``tests/e2e/routing_baseline.py``).

Fails when an owner share, the source-compatible share or the cache call share
moves by more than ``tolerance_pp`` from ``baselines/routing_baseline.json``, or
when the number of owner engines or the wave structure changes. A deliberate
change regenerates the baseline with a reason line.
"""

from __future__ import annotations

import json

import pytest

from tests.e2e.pipeline import PipelineResult
from tests.e2e.routing_baseline import BASELINE, compare, measure

pytestmark = pytest.mark.e2e


def test_routing_shape_matches_the_baseline(run: PipelineResult) -> None:
    if run.external:
        pytest.skip("the baseline describes the deterministic pipeline, not an external job")
    baseline = json.loads(BASELINE.read_text())
    assert baseline.get("reason"), "the baseline must say why it was last updated"
    actual = measure(run.job_dir())
    problems = compare(baseline["samples"][run.db], actual, baseline["tolerance_pp"])
    assert not problems, f"{run.db}: routing moved beyond the baseline:\n- " + "\n- ".join(
        problems
    ) + "\nIf intended, regenerate: uv run python -m tests.e2e.routing_baseline --write " '--reason "..."\nactual: ' + json.dumps(
        actual, indent=2
    )


def test_compare_flags_drift() -> None:
    base = {
        "owner_share_percent": {"aurora_mysql": 50.0, "dynamodb": 50.0},
        "source_compatible_share_percent": 50.0,
        "owner_engines": 2,
        "cache_overlay": {"queries": 3, "call_share_percent": 20.0},
        "waves": [
            {"engines": ["aurora_mysql"], "workload_percent": 50.0, "cached_call_share_percent": 0},
            {"engines": ["dynamodb"], "workload_percent": 50.0, "cached_call_share_percent": 0},
        ],
    }
    assert compare(base, json.loads(json.dumps(base))) == []
    moved = json.loads(json.dumps(base))
    moved["owner_share_percent"] = {"aurora_mysql": 44.0, "dynamodb": 56.0}
    moved["source_compatible_share_percent"] = 44.0
    moved["waves"][0]["workload_percent"] = 44.0
    moved["waves"][1]["workload_percent"] = 56.0
    problems = compare(base, moved)
    assert any("aurora_mysql" in p for p in problems)
    assert any("source-compatible" in p for p in problems)
    restructured = json.loads(json.dumps(base))
    restructured["waves"] = [{"engines": ["aurora_mysql", "dynamodb"], "workload_percent": 100.0}]
    assert any("wave structure" in p for p in compare(base, restructured))
    within = json.loads(json.dumps(base))
    within["owner_share_percent"] = {"aurora_mysql": 47.0, "dynamodb": 53.0}
    within["source_compatible_share_percent"] = 47.0
    assert compare(base, within) == []
