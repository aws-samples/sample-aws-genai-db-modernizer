"""A wave's query share names what it counts (#1453).

The share is queries over queries ("of queries"); the distinct query patterns
figure (#1146) is a different count. The cache wave's share stays "of calls".
Saved reports keep the rationale they were written with.
"""

from __future__ import annotations

import re

from src.report.roadmap import _wave_headline_stats
from src.report.waves import resolve_migration_waves
from tests.unit.agents.referee.test_migration_waves import _build
from tests.unit.report.test_migration_waves_rendering import _legacy_report

OLD = "of query patterns"


def _share_phrases(text: str) -> list[str]:
    return re.findall(r"\(\d+(?:\.\d+)?% of [a-z ]+\)", text)


def test_built_waves_label_the_query_share_of_queries() -> None:
    waves = _build()
    labelled = [w for w in waves if w["share_basis"] == "queries"]
    assert labelled
    for w in labelled:
        assert OLD not in w["rationale"], w["rationale"]
        if _share_phrases(w["rationale"]):
            assert "% of queries)" in w["rationale"], w["rationale"]


def test_the_cache_wave_share_stays_of_calls() -> None:
    cache = next(w for w in _build() if w["share_basis"] == "calls")
    assert "% of calls)" in cache["rationale"]


def test_legacy_fallback_waves_label_the_share_of_queries() -> None:
    derived = [
        w
        for w in resolve_migration_waves(_legacy_report())
        if "still assigned to" in w["rationale"] or "build a read model" in w["rationale"]
    ]
    assert derived
    for w in derived:
        assert OLD not in w["rationale"], w["rationale"]
        assert "% of queries)" in w["rationale"], w["rationale"]


def test_the_headline_labels_the_share_by_its_basis() -> None:
    wave = {"query_count": 4, "workload_share_percent": 40, "share_basis": "queries"}
    count, _ = _wave_headline_stats(wave, {"served_tables": 0})
    assert "(40% of queries)" in count
    calls = {**wave, "share_basis": "calls"}
    count, _ = _wave_headline_stats(calls, {"served_tables": 0})
    assert "(40% of calls)" in count


def test_a_saved_report_with_the_old_rationale_still_renders_as_stored() -> None:
    stored = {
        "wave": 1,
        "engines": ["dynamodb"],
        "title": "t",
        "query_count": 40,
        "workload_share_percent": 37.4,
        "share_basis": "queries",
        "table_count": 0,
        "tables": [],
        "rationale": "40 queries (37.4% of query patterns) across 1 table group.",
    }
    report = {"migration_waves": [stored]}
    assert resolve_migration_waves(report) == [stored]
