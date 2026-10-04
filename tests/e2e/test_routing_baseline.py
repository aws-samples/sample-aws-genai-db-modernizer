"""Routing regression guard (see ``tests/e2e/routing_baseline.py``).

Fails when an owner share, the source-compatible share or the cache call share
moves by more than ``tolerance_pp`` from ``baselines/routing_baseline.json``, when
an engine's owned queries or the cached reads move by more than
``query_tolerance`` (so a small engine growing is caught), or when the number of
owner engines or the wave structure changes. A deliberate
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
        "owner_queries": {"aurora_mysql": 50, "dynamodb": 50},
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


def test_compare_catches_a_small_engine_growing() -> None:
    """discourse: OpenSearch 3 -> 83 of 1,654 queries is 0.2% -> 5.0%, under 5 pp."""
    base = {
        "owner_share_percent": {"aurora_postgresql": 76.5, "dynamodb": 23.3, "opensearch": 0.2},
        "owner_queries": {"aurora_postgresql": 1266, "dynamodb": 385, "opensearch": 3},
        "source_compatible_share_percent": 76.5,
        "owner_engines": 3,
        "cache_overlay": {"queries": 3, "call_share_percent": 25.1},
        "waves": [],
    }
    grown = json.loads(json.dumps(base))
    grown["owner_share_percent"] = {
        "aurora_postgresql": 71.7,
        "dynamodb": 23.3,
        "opensearch": 5.0,
    }
    grown["owner_queries"] = {"aurora_postgresql": 1186, "dynamodb": 385, "opensearch": 83}
    grown["source_compatible_share_percent"] = 71.7
    problems = compare(base, grown)
    assert any("queries owned by opensearch: 3 -> 83" in p for p in problems)
    assert not any("owner share of opensearch" in p for p in problems)  # share rule misses it

    more_cached = json.loads(json.dumps(base))
    more_cached["cache_overlay"] = {"queries": 12, "call_share_percent": 26.0}
    assert any("cached reads: 3 -> 12" in p for p in compare(base, more_cached))
    assert compare(base, json.loads(json.dumps(base))) == []
