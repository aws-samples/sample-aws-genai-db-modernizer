"""Every in-scope query is in exactly one owner query group, or the report says so (#226).

Across the bundled samples (wordpress, discourse, trailgear, deterministic) and the
designed wordpress job (a schema design for every kept engine, the cache layer
included): no in-scope query is served by two groups, the cache layer's groups list
only cached reads, and the queries no group of their own engine serves are exactly
``facts.query_group_counts.ungrouped_queries``. On the deterministic samples only
the engines whose schema design has not run are left ungrouped.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

import pytest

from src.shared.cache_engines import CACHE_ENGINES
from tests.e2e.pipeline import PipelineResult


def _report(r: PipelineResult) -> dict[str, Any]:
    report: dict[str, Any] = json.loads(r.report_json().read_text())
    return report


def _in_scope(report: dict[str, Any]) -> dict[str, str]:
    """In-scope query id -> its owner engine (queries with no engine left out)."""
    return {
        str(m["query_id"]): str(m["engine"])
        for p in report["query_pattern_groups"]
        for m in p["members"]
        if m.get("engine")
    }


def _served_by_own_engine(report: dict[str, Any]) -> set[tuple[str, str]]:
    return {
        (str(ap["engine"]), str(q))
        for g in report["query_groups"]
        if not g.get("cache_layer")
        for ap in g["access_patterns"]
        for q in ap.get("query_ids") or []
    }


def _check_cache_groups_apart(report: dict[str, Any]) -> None:
    for g in report["query_groups"]:
        cache_patterns = [ap for ap in g["access_patterns"] if ap["engine"] in CACHE_ENGINES]
        if g.get("cache_layer"):
            assert len(cache_patterns) == len(g["access_patterns"]), g["group_name"]
            assert all(sq.get("cached_read") is True for sq in g["source_queries"])
        else:
            assert not cache_patterns, f"owner group {g['group_name']!r} lists a cache pattern"
            assert not any(sq.get("cached_read") for sq in g["source_queries"])


def _check_ungrouped_is_the_fact(report: dict[str, Any]) -> dict[str, int]:
    served = _served_by_own_engine(report)
    missing = Counter(
        e for q, e in _in_scope(report).items() if e not in CACHE_ENGINES and (e, q) not in served
    )
    stated = {
        row["engine"]: row["query_count"]
        for row in report["facts"]["query_group_counts"]["ungrouped_queries"]
    }
    assert stated == dict(missing)
    return stated


def _check_served_once(report: dict[str, Any]) -> None:
    in_scope = _in_scope(report)
    served = Counter(
        sq["query_id"]
        for g in report["query_groups"]
        for sq in g["source_queries"]
        if not sq.get("cached_read") and sq["query_id"] in in_scope
    )
    twice = sorted(q for q, n in served.items() if n > 1)
    assert not twice, f"{len(twice)} in-scope queries are served by two groups: {twice[:5]}"


@pytest.mark.deterministic
def test_query_groups_partition_the_in_scope_queries(run: PipelineResult) -> None:
    """Deterministic only: the replayed model run's report.json was synthesized before
    the cache layer's groups were split (#226), so it still shares them."""
    if run.external:
        pytest.skip("deterministic samples only")
    report = _report(run)
    _check_cache_groups_apart(report)
    _check_ungrouped_is_the_fact(report)


@pytest.mark.deterministic
def test_no_query_is_served_twice_and_only_undesigned_engines_are_ungrouped(
    run: PipelineResult,
) -> None:
    if run.external:
        pytest.skip("deterministic samples only")
    report = _report(run)
    _check_served_once(report)
    stated = _check_ungrouped_is_the_fact(report)
    undesigned = {
        r["target"]
        for r in report["ranking"]
        if not r.get("schema_design_available") and r["target"] not in CACHE_ENGINES
    }
    owners = set(_in_scope(report).values()) - CACHE_ENGINES
    # The deterministic pipeline runs no schema design: the relational engine's
    # queries are grouped from the assignment, every other owner's wait for its design.
    assert set(stated) == owners & (undesigned - {"aurora_mysql", "aurora_postgresql"})
    assert stated, "a deterministic sample with only Aurora queries checks nothing here"


@pytest.mark.deterministic
def test_the_designed_wordpress_job_groups_every_query_once(designed_run: PipelineResult) -> None:
    report = _report(designed_run)
    _check_cache_groups_apart(report)
    _check_served_once(report)
    assert _check_ungrouped_is_the_fact(report) == {}
    in_scope = _in_scope(report)
    served = {
        sq["query_id"]
        for g in report["query_groups"]
        for sq in g["source_queries"]
        if not sq.get("cached_read")
    }
    assert set(in_scope) <= served
    cached = [g for g in report["query_groups"] if g.get("cache_layer")]
    assert cached, "the designed job carries a cache-layer design"
