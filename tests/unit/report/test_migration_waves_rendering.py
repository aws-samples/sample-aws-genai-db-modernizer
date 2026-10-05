"""Migration waves across the deliverables (#225).

Synthesis writes ``migration_waves`` to report.json deterministically from the
assignment. Every deliverable (deck, decision report, engineering report)
shows the same roadmap: ``resolve_migration_waves`` returns the stored waves
verbatim when present, and falls back to deriving the same shape from
``ranking``/``cache_overlay``/``table_mappings`` for a report synthesized
before this field existed.
"""

from __future__ import annotations

from typing import Any

from ci.llm import judge_facts
from src.report import pptx_report
from src.report.renderers import (
    render_decision_report_html,
    render_engineering_report_md,
    resolve_migration_waves,
)

STORED_WAVES = [
    {
        "wave": 1,
        "title": "Cache hot reads with ElastiCache",
        "engines": ["elasticache"],
        "moves_from": ["aurora_mysql"],
        "serves_from": [],
        "tables": [],
        "table_count": 0,
        "table_groups": None,
        "query_count": 20,
        "workload_share_percent": 83.4,
        "share_basis": "calls",
        "rationale": "20 hot reads (83.4% of calls), cache-aside: no data migration, fully reversible.",
        "gate": "Cache hit rate and invalidation verified against the source database.",
    },
    {
        "wave": 2,
        "title": "Move key-value and point-lookup queries to DynamoDB",
        "engines": ["dynamodb"],
        "moves_from": ["aurora_mysql"],
        "serves_from": [],
        "tables": ["wordpress.wp_posts", "wordpress.wp_postmeta"],
        "table_count": 2,
        "table_groups": [
            {"tables": ["wordpress.wp_postmeta", "wordpress.wp_posts"], "query_count": 98}
        ],
        "query_count": 98,
        "workload_share_percent": 91.6,
        "share_basis": "queries",
        "rationale": "98 key-value and point-lookup queries (91.6% of the workload) across 1 table group.",
        "gate": "Dual-write/backfill validated and query parity confirmed per table group.",
    },
    {
        "wave": 3,
        "title": "Keep the rest on Aurora MySQL",
        "engines": ["aurora_mysql"],
        "moves_from": [],
        "serves_from": [],
        "tables": ["wordpress.wp_options"],
        "table_count": 1,
        "table_groups": None,
        "query_count": 5,
        "workload_share_percent": 4.7,
        "share_basis": "queries",
        "rationale": "5 queries (4.7% of the workload) stay on Aurora MySQL, carried over 1:1 with no migration.",
        "gate": "End state: every earlier wave's gate has passed.",
    },
]


def _report_with_waves() -> dict[str, Any]:
    return {
        "database_name": "wordpress",
        "job_id": "job-1",
        "timestamp": "2026-10-04T00:00:00Z",
        "ranking": [
            {
                "target": "dynamodb",
                "confidence_score": 90,
                "workload_percent": 91.6,
                "assigned_queries": 98,
            },
            {
                "target": "aurora_mysql",
                "confidence_score": 87,
                "workload_percent": 4.7,
                "assigned_queries": 5,
            },
            {
                "target": "elasticache",
                "confidence_score": 76,
                "workload_percent": 0.0,
                "assigned_queries": 0,
                "role": "cache_layer",
                "cache_overlay_queries": 20,
                "cache_call_share_percent": 83.4,
                "cache_overlay_owners": {"dynamodb": 20},
            },
        ],
        "recommended_architecture": {"databases": [{"service": "dynamodb", "table_count": 2}]},
        "schema_designs": {},
        "cache_overlay": {
            "engine": "elasticache",
            "query_count": 20,
            "call_share_percent": 83.4,
            "owners": {"dynamodb": 20},
        },
        "migration_waves": STORED_WAVES,
    }


def _legacy_report() -> dict[str, Any]:
    """Same shape, no stored roadmap: synthesized before #225."""
    rep = _report_with_waves()
    del rep["migration_waves"]
    rep["table_mappings"] = [
        {
            "source_table": "wordpress.wp_posts",
            "recommended_database": "dynamodb",
            "confidence_score": 90,
        },
    ]
    return rep


class TestResolveMigrationWaves:
    def test_returns_stored_waves_verbatim(self):
        waves = resolve_migration_waves(_report_with_waves())
        assert waves == STORED_WAVES

    def test_empty_report_has_no_waves(self):
        assert resolve_migration_waves({}) == []

    def test_legacy_fallback_derives_a_cache_wave_first(self):
        waves = resolve_migration_waves(_legacy_report())
        assert waves[0]["engines"] == ["elasticache"]
        assert waves[0]["share_basis"] == "calls"
        assert waves[0]["query_count"] == 20

    def test_legacy_fallback_puts_dynamodb_and_aurora_in_separate_waves(self):
        waves = resolve_migration_waves(_legacy_report())
        engine_lists = [w["engines"] for w in waves]
        assert ["dynamodb"] in engine_lists
        assert ["aurora_mysql"] in engine_lists
        dynamo_i = engine_lists.index(["dynamodb"])
        aurora_i = engine_lists.index(["aurora_mysql"])
        assert dynamo_i < aurora_i  # DynamoDB migrates before the retained engine

    def test_legacy_fallback_numbers_waves_consecutively(self):
        waves = resolve_migration_waves(_legacy_report())
        assert [w["wave"] for w in waves] == list(range(1, len(waves) + 1))


class TestDecisionReportRoadmap:
    def test_shows_every_wave_with_its_rationale_and_gate(self):
        html = render_decision_report_html(_report_with_waves())
        assert "Migration roadmap" in html
        assert "Wave 1: Cache hot reads with ElastiCache" in html
        assert "Wave 2: Move key-value and point-lookup queries to DynamoDB" in html
        assert "Wave 3: Keep the rest on Aurora MySQL" in html
        assert "Gate: Cache hit rate and invalidation verified" in html
        assert "91.6% of the workload" in html
        assert "83.4% of calls" in html

    def test_no_section_without_any_wave(self):
        rep = _report_with_waves()
        rep["migration_waves"] = []
        rep["cache_overlay"] = None
        rep["ranking"] = []
        assert "Migration roadmap" not in render_decision_report_html(rep)


class TestEngineeringReportRoadmap:
    def test_lists_tables_and_table_groups_per_wave(self):
        md = render_engineering_report_md(_report_with_waves())
        assert "## Migration roadmap (3 waves)" in md
        assert "### Wave 2: Move key-value and point-lookup queries to DynamoDB" in md
        assert "`wordpress.wp_posts`" in md
        assert "`wordpress.wp_postmeta`" in md
        assert "1 table group" in md
        assert "Moves from: Aurora MySQL" in md
        assert "### Wave 3: Keep the rest on Aurora MySQL" in md
        assert "Gate before the next wave: End state" in md

    def test_search_wave_shows_synced_from(self):
        rep = _report_with_waves()
        rep["migration_waves"] = [
            {
                "wave": 1,
                "title": "Sync search and analytics read models to OpenSearch",
                "engines": ["opensearch"],
                "moves_from": [],
                "serves_from": ["dynamodb"],
                "tables": ["wordpress.wp_comments"],
                "table_count": 1,
                "table_groups": None,
                "query_count": 4,
                "workload_share_percent": 3.7,
                "share_basis": "queries",
                "rationale": "synced from dynamodb, never the system of record",
                "gate": "sync lag inside SLA",
            }
        ]
        md = render_engineering_report_md(rep)
        assert "Synced from (durable owner): DynamoDB" in md


class TestDeckUsesStoredWaves:
    def test_dynamodb_and_aurora_are_separate_waves_when_stored(self):
        f = pptx_report.derive(_report_with_waves(), {})
        engine_lists = [[e["engine"] for e in w["engines"]] for w in f["waves"]]
        assert engine_lists == [["elasticache"], ["dynamodb"], ["aurora_mysql"]]

    def test_falls_back_to_the_on_the_fly_algorithm_without_a_stored_roadmap(self):
        rep = _legacy_report()
        f = pptx_report.derive(rep, {})
        # The legacy algorithm merges the no-migration roles (cache + retained)
        # into one wave 1, unlike the stored #225 roadmap above.
        assert [e["engine"] for e in f["waves"][0]["engines"]] == ["elasticache"]

    def test_last_wave_is_retained_when_stored(self):
        f = pptx_report.derive(_report_with_waves(), {})
        assert f["waves"][-1]["workload"] == 4.7
        assert f["waves"][-1]["cached_queries"] == 0


class TestJudgeFactsPassthrough:
    def test_migration_waves_present(self):
        facts = judge_facts.build_facts(_report_with_waves())
        assert facts["migration_waves"] == STORED_WAVES

    def test_migration_waves_absent_marker(self):
        rep = _legacy_report()
        facts = judge_facts.build_facts(rep)
        assert facts["migration_waves"] == judge_facts.MIGRATION_WAVES_ABSENT
