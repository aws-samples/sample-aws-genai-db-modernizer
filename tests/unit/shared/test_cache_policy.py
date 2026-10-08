"""Keep cache roles and the hot-read floor consistent across consumers (#398)."""

import re
from pathlib import Path

import pytest

from src.agents.referee import cache_overlay
from src.contracts.analysis_output import ScoreBreakdown
from src.report import renderers
from src.shared import migration_wave_engines
from src.tools.analysis import redis_analysis_tools
from src.tools.analysis.scoring import TableProfile


def test_all_python_cache_sets_share_one_definition() -> None:
    assert migration_wave_engines.CACHE_ENGINES is cache_overlay.CACHE_OVERLAY_ENGINES
    assert renderers._CACHE_ENGINES is cache_overlay.CACHE_OVERLAY_ENGINES


def test_javascript_cache_set_matches_python() -> None:
    root = Path(__file__).resolve().parents[3]
    js = (root / "src/ui/src/utils/cacheLayer.js").read_text()
    match = re.search(r"export const CACHE_ENGINES = new Set\(\[(.*?)\]\);", js)
    assert match is not None, "Could not parse the UI cache engine set"
    engines = set(re.findall(r"['\"]([^'\"]+)['\"]", match.group(1)))
    assert engines == cache_overlay.CACHE_OVERLAY_ENGINES


def test_memorydb_is_not_automatically_labelled_as_a_cache() -> None:
    assert renderers._engine_role("memorydb", {"memorydb"}, {}, workload=10) == "Migration target"
    assert cache_overlay.can_own("memorydb")


@pytest.mark.parametrize("rate,expected", [(0.99, False), (1.0, True), (1.01, True)])
def test_hot_read_floor_includes_the_boundary(rate: float, expected: bool) -> None:
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
    assert any(p.pattern_type == "caching" for p in analysis.patterns_detected) is expected


def test_analysis_uses_the_shared_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    assert (
        redis_analysis_tools.HOT_READ_MIN_CALLS_PER_SECOND
        == cache_overlay.HOT_READ_MIN_CALLS_PER_SECOND
    )
    monkeypatch.setattr(redis_analysis_tools, "HOT_READ_MIN_CALLS_PER_SECOND", 5.0)
    query = {
        "query_id": "q1",
        "query_text": "SELECT * FROM settings",
        "query_type": "SELECT",
        "calls_per_second": 2.0,
        "tables_accessed": ["settings"],
    }
    analysis = redis_analysis_tools.analyze_redis_use_cases(
        {"queries": {"query_patterns": [query]}}
    )
    assert not any(p.pattern_type == "caching" for p in analysis.patterns_detected)
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
    adjusted = redis_analysis_tools._apply_redis_adjustments(scores, profile)
    assert adjusted.cost_score == 50
