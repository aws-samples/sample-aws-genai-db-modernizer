"""The cache layer in the deliverables (#296).

ElastiCache owns no query: every deliverable shows it as "Cache layer" with the
hot reads it fronts and their share of calls, never as an owner share of the
workload. The deck keeps it as the no-migration Wave 1. Reports written before
the overlay (ElastiCache owning queries) render as before.
"""

from __future__ import annotations

from typing import Any

from ci.llm import judge_facts
from src.report import pptx_report
from src.report.renderers import (
    _architecture_engines,
    architecture_svg,
    render_decision_report_html,
    render_engineering_report_md,
)


def _report() -> dict[str, Any]:
    return {
        "database_name": "wordpress",
        "job_id": "job-1",
        "timestamp": "2026-10-04T00:00:00Z",
        "ranking": [
            {
                "target": "dynamodb",
                "confidence_score": 50,
                "workload_percent": 80.4,
                "assigned_queries": 86,
            },
            {
                "target": "opensearch",
                "confidence_score": 60,
                "workload_percent": 19.6,
                "assigned_queries": 21,
            },
            {
                "target": "elasticache",
                "confidence_score": 48,
                "workload_percent": 0.0,
                "assigned_queries": 0,
                "role": "cache_layer",
                "cache_overlay_queries": 20,
                "cache_call_share_percent": 83.4,
                "cache_overlay_owners": {"dynamodb": 20},
            },
        ],
        "recommended_architecture": {
            "databases": [
                {"service": "dynamodb", "table_count": 19},
                {"service": "opensearch", "table_count": 2},
            ]
        },
        "schema_designs": {},
        "cache_overlay": {
            "engine": "elasticache",
            "query_count": 20,
            "calls_per_second": 185.07,
            "call_share_percent": 83.4,
            "owners": {"dynamodb": 20},
            "patterns": {"point_lookup": 19, "reference_read": 1},
            "min_calls_per_second": 1.0,
            "max_rows_avg": 100.0,
            "dropped_query_ids": ["q9"],
            "notes": ["1 cached query had no in-scope access pattern ... owner unchanged)."],
        },
    }


def _cache_row(report: dict[str, Any]) -> dict[str, Any]:
    return next(e for e in _architecture_engines(report) if e["engine"] == "elasticache")


class TestDecisionReport:
    def test_cache_row_has_no_workload_share(self):
        row = _cache_row(_report())
        assert row["role"] == "Cache layer"
        assert row["workload"] is None
        assert (row["cached_queries"], row["cached_call_share"]) == (20, 83.4)
        assert row["scope"] == "cache-aside"

    def test_html_shows_cached_reads_and_call_share(self):
        html = render_decision_report_html(_report())
        assert "20 cached reads · 83.4% of calls" in html
        assert "fronts 20 hot reads (83.4% of calls) cache-aside" in html
        assert "<td>100%</td>" in html  # owner shares still add up

    def test_svg(self):
        svg = architecture_svg(_report())
        assert "Cache layer  ·  20 reads · 83.4% of calls" in svg

    def test_legacy_report_keeps_the_workload_share(self):
        rep = _report()
        rep.pop("cache_overlay")
        legacy = {"target": "elasticache", "confidence_score": 48, "workload_percent": 31.8}
        rep["ranking"][2] = legacy
        row = _cache_row(rep)
        assert row["workload"] == 31.8 and row["cached_queries"] is None


class TestEngineeringReport:
    def test_cache_layer_section(self):
        md = render_engineering_report_md(_report())
        assert "## Cache layer (elasticache)" in md
        assert "fronts 20 hot reads (83.4% of calls, 185.1 calls/s) cache-aside" in md
        assert "- Owner engines: dynamodb 20" in md
        assert "≥ 1 calls/s" in md and "≤ 100 rows" in md
        assert "owner unchanged" in md

    def test_no_section_without_overlay(self):
        rep = _report()
        rep.pop("cache_overlay")
        assert "## Cache layer" not in render_engineering_report_md(rep)


def _deck_text(rep: dict[str, Any]) -> str:
    f = pptx_report.derive(rep, {})
    prs = pptx_report.open_deck(keep=1)
    for build in pptx_report.SLIDES:
        build(prs, f)
    out = []
    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                out.append(shape.text_frame.text)
            if shape.has_table:
                out.extend(c.text for r in shape.table.rows for c in r.cells)
    return " ".join(" ".join(out).split())


class TestDeck:
    def test_cache_layer_is_the_no_migration_wave_without_a_workload_share(self):
        f = pptx_report.derive(_report(), {})
        wave1 = f["waves"][0]
        assert wave1["engines"] == ["elasticache"]
        assert wave1["workload"] == 0
        assert (wave1["cached_queries"], wave1["cached_share"]) == (20, 83.4)
        assert wave1["no_migration"] is True
        start = next(d for d in f["decisions"] if d["question"].startswith("Start"))
        assert start["badge"] == "83% of calls cached"

    def test_deck_text(self):
        text = _deck_text(_report())
        assert (
            "Wave 1 puts the cache layer for 20 hot reads (83.4% of calls) in front of the "
            "current source database, with no data migration"
        ) in text
        assert "cache-aside in front of the current source database (MySQL/PostgreSQL)" in text
        assert "Cache hit rate and invalidation verified against the source database" in text
        assert "ElastiCache caches 20 hot reads (83.4% of calls) and owns none of the workload" in (
            text
        )
        assert "ElastiCache cache layer · 20 cached reads · 83.4% of calls" in text
        assert "ElastiCache keeps" not in text
        assert "Wave 1 covers 0.0%" not in text
        assert "83.4% calls" in text


class TestJudgeFacts:
    def test_cache_layer_is_not_a_workload_share(self):
        assignment = {
            "query_assignments": [
                {
                    "query_id": "q1",
                    "assigned_engine": "dynamodb",
                    "source_tables": ["wp_options"],
                    "cache_engine": "elasticache",
                }
            ]
        }
        facts = judge_facts.build_facts(_report(), None, assignment)
        cache = next(e for e in facts["engines"] if e["engine"] == "elasticache")
        assert cache["role"] == "cache_layer"
        assert cache["workload_percent"] is None
        assert cache["cached_queries"] == 20
        assert cache["cached_call_share_percent"] == 83.4
        assert cache["tables_served"] == ["wp_options"]
        owner = next(e for e in facts["engines"] if e["engine"] == "dynamodb")
        assert "role" not in owner and owner["workload_percent"] == 80.4
        assert facts["cache_overlay"]["cached_queries"] == 20
        assert facts["cache_overlay"]["owners"] == {"dynamodb": 20}

    def test_no_overlay(self):
        rep = _report()
        rep.pop("cache_overlay")
        assert "cache_overlay" not in judge_facts.build_facts(rep)


class TestAnalysisReport:
    def test_cache_layer_stat_card(self):
        from src.report.analysis_report import _ASSIGNMENT_FIELDS, _cache_layer_stat

        card = _cache_layer_stat({"cache_overlay": _report()["cache_overlay"]})
        assert "Cache layer · 20 cached reads · 83.4% of calls" in card
        assert _cache_layer_stat({}) == ""
        assert {"cache_engine", "cache_reason"} <= set(_ASSIGNMENT_FIELDS)


def _discourse_shape() -> dict[str, Any]:
    """The PR #304 discourse run: OpenSearch owns 3 text-search queries, has no tables
    or target objects ($240.96), and its schema design was skipped."""
    return {
        "database_name": "discourse",
        "job_id": "cc263d38",
        "timestamp": "2026-10-04T16:35:17Z",
        "ranking": [
            {
                "target": "aurora_postgresql",
                "confidence_score": 67,
                "workload_percent": 76.7,
                "assigned_queries": 1268,
            },
            {
                "target": "dynamodb",
                "confidence_score": 60,
                "workload_percent": 23.1,
                "assigned_queries": 383,
            },
            {
                "target": "opensearch",
                "confidence_score": 2,
                "workload_percent": 0.2,
                "assigned_queries": 3,
            },
            {
                "target": "elasticache",
                "confidence_score": 56,
                "workload_percent": 0.0,
                "assigned_queries": 0,
                "role": "cache_layer",
                "cache_overlay_queries": 3,
                "cache_call_share_percent": 25.1,
            },
        ],
        "recommended_architecture": {
            "databases": [
                {"service": "aurora_postgresql", "table_count": 158},
                {"service": "dynamodb", "table_count": 115},
            ]
        },
        "schema_designs": {
            "aurora_postgresql": {"status": "completed", "tables": [{}] * 158},
            "dynamodb": {"status": "completed", "tables": [{}] * 117},
            "opensearch": {"status": "skipped"},
            "elasticache": {"status": "completed", "tables": [{}] * 3},
        },
        "tco_analysis": {
            "cost_breakdown": [
                {"database": "aurora_postgresql", "monthly_cost_usd": 121.06},
                {"database": "dynamodb", "monthly_cost_usd": 15.20},
                {"database": "opensearch", "monthly_cost_usd": 240.96},
                {"database": "elasticache", "monthly_cost_usd": 165.10},
            ]
        },
        "cache_overlay": {"engine": "elasticache", "query_count": 3, "call_share_percent": 25.1},
    }


class TestSearchReadModel:
    def test_opensearch_without_tables_is_a_read_model_not_retained(self):
        rows = {e["engine"]: e for e in _architecture_engines(_discourse_shape())}
        assert rows["opensearch"]["role"] == "Search read model"
        assert rows["opensearch"]["scope"] == "synced from owners"
        assert rows["opensearch"]["migrates"] == 0

    def test_decision_report_never_calls_it_the_relational_core(self):
        html = render_decision_report_html(_discourse_shape())
        assert "retained as the relational core" not in html
        assert "<b>opensearch</b> is a search read model" in html
        assert "source schema retained" not in html

    def test_wave_plan_puts_it_after_its_owners_never_in_the_no_migration_wave(self):
        f = pptx_report.derive(_discourse_shape(), {})
        waves = [w["engines"] for w in f["waves"]]
        assert waves[0] == ["elasticache"]
        # #225's deterministic rule always finishes on the retained engine
        # (aurora_postgresql); the search read model comes right before it --
        # after the engines that own its tables, never in the no-migration wave.
        assert waves[-1] == ["aurora_postgresql"]
        opensearch_wave = next(w for w in f["waves"] if w["engines"] == ["opensearch"])
        assert opensearch_wave is not f["waves"][0]
        assert all("opensearch" not in w for w in waves[:1])
        # This fixture has no table_mappings, so the indexed tables cannot be
        # resolved -- review finding 2's fallback: say so, and fall back to the
        # retained engine as the owner of record rather than showing no owner.
        assert "could not be resolved" in opensearch_wave["note"]
        assert "Aurora PostgreSQL" in opensearch_wave["note"]
        start = next(d for d in f["decisions"] if d["question"].startswith("Start"))
        assert "OpenSearch" not in start["against"]

    def test_retained_is_only_the_source_compatible_relational_engine(self):
        rep = _discourse_shape()
        rep["recommended_architecture"]["databases"] = [{"service": "dynamodb", "table_count": 115}]
        rep["schema_designs"]["aurora_postgresql"] = {"status": "skipped"}
        rows = {e["engine"]: e["role"] for e in _architecture_engines(rep)}
        assert rows["aurora_postgresql"] == "Retained"
        assert rows["opensearch"] == "Search read model"

    def test_decision_report_cache_note_says_it_fronts_the_current_source(self):
        html = render_decision_report_html(_discourse_shape())
        assert "in front of the current source database, with no data migration" in html
        assert "invalidation follows the engine that owns each cached table" in html
