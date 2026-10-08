"""Regression tests: ElastiCache and OpenSearch analysis output must not depend
on Python's per-process string hash seed (PYTHONHASHSEED) (issue #449).

``analyze_redis_use_cases`` built each pattern's ``table_ids`` with
``list({...})`` and ``build_opensearch_decision_trace`` built the classification
``reason`` with ``', '.join(<set intersection>)``. Set iteration order for
strings depends on PYTHONHASHSEED, so the same input gave a different order on
each run. Both now sort.

Each test runs a fixed input in separate subprocesses under several hash seeds
and asserts the outputs are identical (and sorted).
"""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404 -- runs a fixed, module-level -c script, never external input
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]

SEEDS = ("0", "1", "2", "3")

# Enough distinct tables that a set's iteration order is very unlikely to match
# the sorted order by chance under every seed.
_TABLES = [f"table_{c}" for c in "abcdefgh"]

_REDIS_CHILD = textwrap.dedent(f"""
    import json

    from src.tools.analysis.redis_analysis_tools import analyze_redis_use_cases

    tables = {_TABLES!r}
    queries = [
        {{
            "query_id": f"q{{i}}",
            "query_type": "SELECT",
            "calls_per_second": 5.0,
            "rows_returned_avg": 50000,
            "tables_accessed": [t],
            "query_text": (
                f"SELECT date_trunc('day', created_at), st_distance(geom, ?) FROM {{t}} "
                "WHERE session = ? GROUP BY 1 ORDER BY 1 LIMIT 10"
            ),
        }}
        for i, t in enumerate(tables)
    ]
    analysis = analyze_redis_use_cases({{"queries": {{"query_patterns": queries}}}})
    out = {{p.pattern_id: p.table_ids for p in analysis.patterns_detected}}
    out.update({{ap.anti_pattern_id: ap.table_ids for ap in analysis.anti_patterns_detected or []}})
    print(json.dumps(out))
    """)

_OPENSEARCH_CHILD = textwrap.dedent("""
    import json

    from src.contracts.analysis_output import Confidence, Pattern, WorkloadAnalysis
    from src.tools.analysis.opensearch_analysis_tools import (
        WorkloadType,
        build_opensearch_decision_trace,
    )

    pattern_types = [
        "full-text-search",
        "wildcard-search",
        "regex-search",
        "fuzzy-search",
        "time-range-query",
        "time-aggregation",
        "high-ingest",
    ]
    analysis = WorkloadAnalysis(
        patterns_detected=[
            Pattern(
                pattern_id=f"p{i}",
                pattern_type=ptype,
                confidence=Confidence.HIGH,
                table_ids=["posts"],
            )
            for i, ptype in enumerate(pattern_types)
        ]
    )
    trace = build_opensearch_decision_trace(
        {"database_schema": {"tables": [{"table_id": "posts"}]}, "queries": {}},
        analysis,
        [],
        {"posts": WorkloadType.SEARCH},
        {},
    )
    print(json.dumps([c["reason"] for c in trace["workload_classifications"]]))
    """)


def _run_child(script: str, hashseed: str) -> object:
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = hashseed
    proc = subprocess.run(  # nosec B603 -- fixed interpreter plus a fixed, module-level -c script
        [sys.executable, "-c", script],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, (
        f"child process failed (PYTHONHASHSEED={hashseed}):\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestRedisTableIdsOrderIsHashSeedIndependent:
    def test_table_ids_identical_and_sorted_across_seeds(self):
        results = {seed: _run_child(_REDIS_CHILD, seed) for seed in SEEDS}
        first = results[SEEDS[0]]
        assert isinstance(first, dict)
        # Every accumulator that used to build table_ids from a set must be covered.
        assert {
            "redis-caching-001",
            "redis-session-001",
            "redis-leaderboard-001",
            "redis-timeseries-001",
            "redis-geospatial-001",
            "redis-large-results-001",
        } <= first.keys()
        for pattern_id, table_ids in first.items():
            assert table_ids == sorted(_TABLES), pattern_id
        for seed, result in results.items():
            assert result == first, f"PYTHONHASHSEED={seed} differs from {SEEDS[0]}"


class TestOpenSearchReasonOrderIsHashSeedIndependent:
    def test_reason_identical_and_sorted_across_seeds(self):
        results = {seed: _run_child(_OPENSEARCH_CHILD, seed) for seed in SEEDS}
        first = results[SEEDS[0]]
        assert first == [
            "Matched search patterns: full-text-search, fuzzy-search, high-ingest, "
            "regex-search, time-aggregation, time-range-query, wildcard-search"
        ]
        for seed, result in results.items():
            assert result == first, f"PYTHONHASHSEED={seed} differs from {SEEDS[0]}"
