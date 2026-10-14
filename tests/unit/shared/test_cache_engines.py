"""One cache-engine set and one hot-read floor for every consumer (#398).

Based on the analysis in #430 by @proton0210. ``src/shared/cache_engines.py``
is the single declaration; the cache overlay (routing), the migration-wave
buckets, the report's role labels and the UI's ``cacheLayer.js`` all read it,
and the Redis analysis tools use the same inclusive hot-read floor.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.agents.referee import cache_overlay
from src.contracts.analysis_output import ScoreBreakdown
from src.report import engine_roles
from src.shared import cache_engines, migration_wave_engines
from src.tools.analysis import redis_analysis_tools
from src.tools.analysis.scoring import TableProfile

ROOT = Path(__file__).resolve().parents[3]
JS_PATH = ROOT / "src" / "ui" / "src" / "utils" / "cacheLayer.js"


def test_python_consumers_share_one_set() -> None:
    assert cache_engines.CACHE_ENGINES == frozenset({"elasticache"})
    assert cache_overlay.CACHE_OVERLAY_ENGINES is cache_engines.CACHE_ENGINES
    assert migration_wave_engines.CACHE_ENGINES is cache_engines.CACHE_ENGINES
    assert engine_roles._CACHE_ENGINES is cache_engines.CACHE_ENGINES


def _js_cache_engines() -> set[str]:
    js = JS_PATH.read_text(encoding="utf-8")
    match = re.search(r"export const CACHE_ENGINES = new Set\(\[(.*?)\]\);", js, re.S)
    assert match is not None, "could not parse CACHE_ENGINES in cacheLayer.js"
    return set(re.findall(r"['\"]([^'\"]+)['\"]", match.group(1)))


def test_ui_cache_set_matches_python() -> None:
    assert _js_cache_engines() == set(cache_engines.CACHE_ENGINES)


def test_cache_role_label_follows_the_shared_set(monkeypatch: pytest.MonkeyPatch) -> None:
    def label(engine: str) -> str:
        return engine_roles._engine_role(engine, {engine}, {}, workload=10)

    assert label("elasticache") == "Cache layer"
    monkeypatch.setattr(engine_roles, "_CACHE_ENGINES", frozenset({"fakecache"}))
    assert label("fakecache") == "Cache layer"
    assert label("elasticache") != "Cache layer"


def _caching_detected(rate: float) -> bool:
    query = {
        "query_id": "q1",
        "query_text": "SELECT value FROM settings WHERE id = 1",
        "query_type": "SELECT",
        "calls_per_second": rate,
        "tables_accessed": ["settings"],
        "rows_returned_avg": 1,
    }
    analysis = redis_analysis_tools.analyze_redis_use_cases(
        {"queries": {"query_patterns": [query]}}
    )
    return any(p.pattern_type == "caching" for p in analysis.patterns_detected)


@pytest.mark.parametrize("rate,expected", [(0.99, False), (1.0, True), (1.01, True)])
def test_redis_hot_read_floor_is_inclusive(rate: float, expected: bool) -> None:
    assert _caching_detected(rate) is expected


def test_redis_analysis_uses_the_shared_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    assert (
        cache_overlay.HOT_READ_MIN_CALLS_PER_SECOND == cache_engines.HOT_READ_MIN_CALLS_PER_SECOND
    )
    assert (
        redis_analysis_tools.HOT_READ_MIN_CALLS_PER_SECOND
        == cache_engines.HOT_READ_MIN_CALLS_PER_SECOND
    )
    monkeypatch.setattr(redis_analysis_tools, "HOT_READ_MIN_CALLS_PER_SECOND", 5.0)
    assert not _caching_detected(2.0)
    scores = ScoreBreakdown(
        pattern_match_score=50, complexity_score=50, performance_score=50, cost_score=50
    )
    profile = TableProfile(
        table_id="settings",
        row_count=10,
        size_mb=10,
        column_count=2,
        has_primary_key=True,
        foreign_key_count=0,
        total_calls_per_second=2.0,
    )
    assert redis_analysis_tools._apply_redis_adjustments(scores, profile).cost_score == 50


def test_memorydb_is_not_a_modernizer_engine() -> None:
    """MemoryDB was removed (#398): ElastiCache for Valkey 9.0 covers durability."""
    from src.report import decision_report
    from src.shared.engine_names import ENGINE_DISPLAY_NAMES

    assert "memorydb" not in ENGINE_DISPLAY_NAMES
    assert "memorydb" not in decision_report._ENGINE_BADGE
    assert "memorydb" not in engine_roles._CACHE_ENGINES
