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
        "moves_from": [],
        "fronts": "mysql",
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
        "moves_from": ["mysql"],
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
        assert "Moves from: the source MySQL database" in md
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

    def test_shows_table_owners_and_the_readable_group_kind(self):
        # #225: table_owners (owner + sync per table) and the
        # unresolved-name count surface in the Markdown at least; a group's
        # "kind" renders readably ("co-dependency"), not the raw enum value.
        rep = _report_with_waves()
        rep["unresolved_names"] = {"count": 2, "names": ["CURRENT_TIMESTAMP", "the"]}
        rep["migration_waves"][1]["table_groups"] = [
            {
                "tables": ["wordpress.wp_postmeta", "wordpress.wp_posts"],
                "query_count": 98,
                "kind": "co_dependency",
            }
        ]
        rep["migration_waves"][1]["table_owners"] = [
            {"table": "wordpress.wp_comments", "owner": "dynamodb", "sync": "OpenSearch Ingestion"}
        ]
        md = render_engineering_report_md(rep)
        assert "2 table names in the assignment did not resolve to a table or view" in md
        assert "co-dependency" in md
        assert "co_dependency" not in md
        assert "1 table owner: `wordpress.wp_comments` -> DynamoDB (OpenSearch Ingestion)" in md

    def test_unresolved_names_note_agrees_in_number_when_singular(self):
        # Grammar: "1 table name ... is not shown", not "... are not shown".
        rep = _report_with_waves()
        rep["unresolved_names"] = {"count": 1, "names": ["cte_alias"]}
        md = render_engineering_report_md(rep)
        assert "1 table name in the assignment did not resolve to a table or view" in md
        assert "is not shown in any wave" in md
        assert "are not shown in any wave" not in md


class TestDeckUsesStoredWaves:
    def test_dynamodb_and_aurora_are_separate_waves_when_stored(self):
        f = pptx_report.derive(_report_with_waves(), {})
        engine_lists = [w["engines"] for w in f["waves"]]
        assert engine_lists == [["elasticache"], ["dynamodb"], ["aurora_mysql"]]

    def test_deck_matches_resolve_migration_waves_without_a_stored_roadmap(self):
        # #225: the deck's old on-the-fly algorithm is gone -- it
        # now always matches resolve_migration_waves, the same fallback the
        # decision report and engineering report use (4 waves: cache, DynamoDB
        # and Aurora separate, not merged the way the deleted algorithm did).
        from src.report.renderers import resolve_migration_waves

        rep = _legacy_report()
        f = pptx_report.derive(rep, {})
        engine_lists = [w["engines"] for w in f["waves"]]
        assert engine_lists == [w["engines"] for w in resolve_migration_waves(rep)]
        assert engine_lists[0] == ["elasticache"]

    def test_last_wave_is_retained_when_stored(self):
        f = pptx_report.derive(_report_with_waves(), {})
        assert f["waves"][-1]["workload"] == 4.7
        assert f["waves"][-1]["cached_queries"] == 0
        assert f["waves"][-1]["no_migration"] is True

    def test_wave_1_no_migration_claim_matches_the_resolved_wave(self):
        # #225: "no data migration" is only claimed when wave 1
        # really is the cache or retained wave, not whichever wave happens to
        # come first (e.g. DynamoDB with no cache overlay).
        rep = _report_with_waves()
        rep["migration_waves"] = [
            {
                "wave": 1,
                "title": "Move key-value and point-lookup queries to DynamoDB",
                "engines": ["dynamodb"],
                "moves_from": ["mysql"],
                "serves_from": [],
                "tables": ["wordpress.wp_posts"],
                "table_count": 1,
                "table_groups": None,
                "query_count": 98,
                "workload_share_percent": 91.6,
                "share_basis": "queries",
                "rationale": "moves to DynamoDB",
                "gate": "parity confirmed",
            }
        ]
        rep["cache_overlay"] = None
        f = pptx_report.derive(rep, {})
        assert f["waves"][0]["no_migration"] is False

    # A realistic long rationale/gate, matching the length the review measured
    # on the real wordpress/discourse decks (270-370 and up to several hundred
    # characters respectively, once the #225 gate text is
    # included) -- not the single-character "r"/"g" a shape-bounds test alone
    # would miss overflowing text with.
    _LONG_RATIONALE = (
        "98 key-value and point-lookup queries (91.6% of query patterns) across 2 table "
        "groups, respecting co-dependent tables where possible: the pattern DynamoDB fits "
        "best, and the smallest-blast-radius data migration available once Wave 1's cache "
        "has absorbed the read pressure."
    )
    _LONG_GATE = (
        "Dual-write/backfill validated and query parity confirmed per table group. 7 tables "
        "(wordpress.wp_actionscheduler_actions, wordpress.wp_actionscheduler_groups, "
        "wordpress.wp_posts, wordpress.wp_term_relationships, wordpress.wp_term_taxonomy, "
        "wordpress.wp_terms, wordpress.wp_users) stay dual-read by Aurora MySQL queries "
        "until wave 4; keep them in sync via CDC until then. 2 queries read 2 tables "
        "(wordpress.wp_options, wordpress.wp_postmeta) Aurora MySQL owns (wave 4); keep a "
        "copy in sync via CDC. 3 queries could not be resolved to a table in "
        "either wave and are not counted in any table group."
    )

    def _waves_with_long_text(self, n: int) -> list[dict[str, Any]]:
        engines = [
            "elasticache",
            "dynamodb",
            "aurora_mysql",
            "opensearch",
            "documentdb",
            "aurora_mysql",
        ]
        return [
            {
                "wave": i + 1,
                "title": f"Wave {i + 1}",
                "engines": [eng],
                "moves_from": ["mysql"] if eng not in ("elasticache", "opensearch") else [],
                "serves_from": ["dynamodb"] if eng == "opensearch" else [],
                "tables": [],
                "table_count": 0,
                "table_groups": None,
                "query_count": 1,
                "workload_share_percent": 10.0,
                "share_basis": "calls" if eng == "elasticache" else "queries",
                "rationale": self._LONG_RATIONALE,
                "gate": self._LONG_GATE,
            }
            for i, eng in enumerate(engines[:n])
        ]

    def test_six_waves_fit_above_the_constraints_card(self):
        # #225: the sizing must not blow past the slide for up
        # to 6 waves (cache, KV, other, search, document, retained), with
        # realistic long rationale/gate text in every card.
        from pptx import Presentation

        rep = _report_with_waves()
        rep["migration_waves"] = self._waves_with_long_text(6)
        f = pptx_report.derive(rep, {})
        assert len(f["waves"]) == 6
        prs = pptx_report.open_deck()
        s = pptx_report.slide_sequencing(prs, f)
        # Every shape must stay within the 7.5" slide height.
        max_bottom = max(shape.top + shape.height for shape in s.shapes)
        assert max_bottom <= Presentation(str(pptx_report.TEMPLATE)).slide_height

    def test_long_rationale_and_gate_text_fit_the_card_at_4_to_6_waves(self):
        # A shape-bounds check alone can't tell whether the *text inside* a
        # card overflows it: estimate the text height against the card's text
        # box (no vertical inset) at 4 (unshrunk) to 6 waves.
        from src.report.pptx_report import _estimated_text_height_in, _wave_card_text

        for n in (4, 5, 6):
            rep = _report_with_waves()
            rep["migration_waves"] = self._waves_with_long_text(n)
            f = pptx_report.derive(rep, {})
            step = 1.00 if n <= 4 else max(0.62, (7.50 - 1.90 - 1.06 - 0.18) / n)
            card_h = min(0.92, step - 0.08)
            box_w, box_h = 9.6, card_h - 0.44
            for w in f["waves"]:
                rationale_text, gate_text = _wave_card_text(w["note"], w["gate"], box_w, box_h)
                total_h = _estimated_text_height_in(
                    rationale_text, 10.0, box_w
                ) + _estimated_text_height_in(gate_text, 9.0, box_w)
                assert total_h <= box_h + 1e-9, (
                    f"n={n} wave {w['names']}: estimated text height {total_h:.3f} exceeds "
                    f"the card's text box {box_h:.3f}"
                )


class TestJudgeFactsPassthrough:
    def test_migration_waves_present(self):
        facts = judge_facts.build_facts(_report_with_waves())
        assert facts["migration_waves"] == STORED_WAVES

    def test_migration_waves_absent_marker(self):
        rep = _legacy_report()
        facts = judge_facts.build_facts(rep)
        assert facts["migration_waves"] == judge_facts.MIGRATION_WAVES_ABSENT
