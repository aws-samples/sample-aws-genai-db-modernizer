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
from src.report.renderers import render_engineering_report_md, resolve_migration_waves
from tests.executive_pdf import engineering_text

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


AURORA_FIRST_WAVES = [
    {
        "wave": 1,
        "title": "Move to Aurora MySQL",
        "engines": ["aurora_mysql"],
        "moves_from": ["mysql"],
        "serves_from": [],
        "fronts": None,
        "tables": [f"wordpress.wp_t{i}" for i in range(50)],
        "table_count": 50,
        "table_groups": None,
        "homogeneity": "homogeneous",
        "query_count": 5,
        "cutover_query_count": 107,
        "workload_share_percent": 4.7,
        "share_basis": "queries",
        "rationale": "moves to Aurora MySQL first, schema carried over 1:1",
        "gate": "parity validated",
    },
    {
        "wave": 2,
        "title": "Move key-value and point-lookup queries to DynamoDB",
        "engines": ["dynamodb"],
        "moves_from": ["aurora_mysql"],
        "serves_from": [],
        "tables": [f"wordpress.wp_t{i}" for i in range(19)],
        "table_count": 19,
        "table_groups": None,
        "query_count": 98,
        "workload_share_percent": 91.6,
        "share_basis": "queries",
        "rationale": "key-value queries to DynamoDB",
        "gate": "parity confirmed",
    },
]


def _report_aurora_first_no_schema_design() -> dict[str, Any]:
    """A #321-shaped report with no schema design, so the "migrated" table
    count can only come from the wave-derived fallback (#257/#258's
    schema-design figure is the primary source and would otherwise mask the
    fallback's own double-counting bug)."""
    return {
        "database_name": "wordpress",
        "job_id": "job-1",
        "timestamp": "2026-10-04T00:00:00Z",
        "ranking": [
            {"target": "dynamodb", "confidence_score": 90, "workload_percent": 91.6},
            {"target": "aurora_mysql", "confidence_score": 87, "workload_percent": 4.7},
        ],
        "recommended_architecture": {"databases": []},
        "schema_designs": {},
        "migration_waves": AURORA_FIRST_WAVES,
    }


class TestResolveMigrationWaves:
    def test_returns_stored_waves_verbatim(self):
        waves = resolve_migration_waves(_report_with_waves())
        assert waves == STORED_WAVES

    def test_empty_report_has_no_waves(self):
        assert resolve_migration_waves({}) == []

    def test_legacy_fallback_derives_an_aurora_wave_first(self):
        # #321: the relational move comes first, even in the legacy fallback.
        waves = resolve_migration_waves(_legacy_report())
        assert waves[0]["engines"] == ["aurora_mysql"]
        assert waves[0]["homogeneity"] == "homogeneous"
        assert waves[1]["engines"] == ["elasticache"]
        assert waves[1]["share_basis"] == "calls"
        assert waves[1]["query_count"] == 20

    def test_legacy_fallback_puts_dynamodb_and_aurora_in_separate_waves(self):
        waves = resolve_migration_waves(_legacy_report())
        engine_lists = [w["engines"] for w in waves]
        assert ["dynamodb"] in engine_lists
        assert ["aurora_mysql"] in engine_lists
        dynamo_i = engine_lists.index(["dynamodb"])
        aurora_i = engine_lists.index(["aurora_mysql"])
        assert aurora_i < dynamo_i  # #321: the retained engine migrates first

    def test_legacy_fallback_numbers_waves_consecutively(self):
        waves = resolve_migration_waves(_legacy_report())
        assert [w["wave"] for w in waves] == list(range(1, len(waves) + 1))


class TestPortedMdRoadmap:
    def test_shows_every_wave_with_its_rationale_and_gate(self):
        text = engineering_text(_report_with_waves())
        assert "Migration roadmap (3 waves)" in text
        assert "Wave 1: Cache hot reads with ElastiCache" in text
        assert "Wave 2: Move key-value and point-lookup queries to DynamoDB" in text
        assert "Wave 3: Keep the rest on Aurora MySQL" in text
        assert "Cache hit rate and invalidation verified" in text
        assert "91.6% of the workload" in text
        assert "83.4% of calls" in text

    def test_migrated_table_count_excludes_wave_1_aurora(self):
        # #324 review (finding 4): wave 1 (Aurora) carries the whole schema
        # (50 tables); wave 2 (DynamoDB) moves a 19-table subset of it, not
        # 19 additional tables. "N tables migrate" keeps its pre-#321 meaning
        # -- tables moving to a purpose-built engine -- so it reports 19
        # (DynamoDB's own count), never 50 + 19 = 69 (double-counted) and
        # never 50 (which would wrongly call the whole-schema Aurora move a
        # "purpose-built engine" migration).
        md = render_engineering_report_md(_report_aurora_first_no_schema_design())
        assert "| 19 tables migrate |" in md
        assert "69 tables" not in md
        assert "50 tables migrate" not in md

    def test_no_section_without_any_wave(self):
        rep = _report_with_waves()
        rep["migration_waves"] = []
        rep["cache_overlay"] = None
        rep["ranking"] = []
        assert "## Migration roadmap" not in render_engineering_report_md(rep)
        assert "Wave 1:" not in engineering_text(rep)


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

    def test_moves_from_names_aurora_directly_not_as_the_source(self):
        # #321: a wave moving from the retained Aurora engine (not the legacy
        # source) shows "Aurora MySQL" directly -- "the source Aurora MySQL
        # database" would wrongly call Aurora the original source.
        rep = _report_with_waves()
        rep["migration_waves"][1]["moves_from"] = ["aurora_mysql"]
        rep["migration_waves"][1]["fronts"] = "aurora_mysql"
        md = render_engineering_report_md(rep)
        assert "Moves from: Aurora MySQL" in md
        assert "Fronts: Aurora MySQL" in md
        assert "the source Aurora MySQL database" not in md

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
        # The owner is outside the wave (#615: an owner inside it is a table
        # the wave moves, never one it keeps in sync).
        rep["migration_waves"][1]["table_owners"] = [
            {"table": "wordpress.wp_comments", "owner": "aurora_mysql", "sync": "CDC"}
        ]
        md = render_engineering_report_md(rep)
        assert "2 table names in the assignment did not resolve to a table or view" in md
        assert "co-dependency" in md
        assert "co_dependency" not in md
        # #568 review, Suggestion 4: "N table owners" read oddly for a table
        # this wave does NOT own -- it names the real owner it keeps a
        # synced copy from instead.
        assert (
            "1 table kept in sync from their owner: "
            "`wordpress.wp_comments` -> Aurora MySQL (CDC)" in md
        )

    def test_unresolved_names_note_agrees_in_number_when_singular(self):
        # Grammar: "1 table name ... is not shown", not "... are not shown".
        rep = _report_with_waves()
        rep["unresolved_names"] = {"count": 1, "names": ["cte_alias"]}
        md = render_engineering_report_md(rep)
        assert "1 table name in the assignment did not resolve to a table or view" in md
        assert "is not shown in any wave" in md
        assert "are not shown in any wave" not in md


def _cache_section(md: str) -> str:
    start = md.index("## Cache layer")
    return md[start : md.index("\n## ", start + 1)]


class TestReviewFindings324:
    """Fixes from the independent review of PR #321's merge."""

    def test_md_states_wave_1_share_as_the_end_state(self):
        # Finding 2: wave 1's headline says the whole workload runs on
        # Aurora at cutover, and names the end-state remainder explicitly --
        # not just "5 queries (4.7%)", which reads as if only 5 queries ever
        # touch Aurora.
        text = engineering_text(_report_aurora_first_no_schema_design())
        assert "all 107 queries at cutover" in text
        assert "5 (4.7%) remain on Aurora MySQL at the end" in text

    def test_md_says_tables_and_views_for_wave_1(self):
        # Finding 4: wave 1's table count includes views; say so.
        text = engineering_text(_report_aurora_first_no_schema_design())
        assert "50 source tables and views" in text

    def test_engineering_md_states_wave_1_share_as_the_end_state(self):
        md = render_engineering_report_md(_report_aurora_first_no_schema_design())
        assert "all 107 queries at cutover" in md
        assert "remain on Aurora MySQL at the end" in md

    def test_engineering_md_table_list_says_tables_and_views_for_wave_1(self):
        md = render_engineering_report_md(_report_aurora_first_no_schema_design())
        assert "source tables and views:" in md

    def test_engineering_md_pointer_falls_back_without_completed_designs(self):
        # Finding 5: "Target schemas by engine" is only written when a
        # design completed; the pointer must not promise a section that
        # isn't there.
        rep = _report_aurora_first_no_schema_design()
        md = render_engineering_report_md(rep)
        assert "Target schemas by engine" not in md
        assert "the target architecture in the Target engines table above" in md

    def test_engineering_md_pointer_names_target_schemas_when_present(self):
        rep = _report_aurora_first_no_schema_design()
        rep["schema_designs"] = {
            "dynamodb": {"status": "completed", "tables": [{"table_name": "t"}]}
        }
        md = render_engineering_report_md(rep)
        assert "## Target schemas by engine" in md
        assert 'the target schemas in "Target schemas by engine" below' in md

    def test_suggested_path_note_uses_an_em_dash_not_ascii_double_hyphen(self):
        # Finding 9.
        text = engineering_text(_report_aurora_first_no_schema_design())
        md = render_engineering_report_md(_report_aurora_first_no_schema_design())
        assert "One suggested adoption path, not the only one — to modernize" in text
        assert "One suggested adoption path, not the only one — to modernize" in md
        note_md = md.split("## Migration roadmap")[1].split("\n\n")[1]
        assert "--" not in note_md

    def test_cache_note_fronts_the_resolved_cache_waves_engine(self):
        # Finding 6: the engineering report's cache layer note must agree
        # with the roadmap's own cache wave, not re-derive the fronted
        # engine from architecture roles -- which disagreed with a 1.4-shaped
        # report (cache-first, stored waves predate #321 and still front the
        # legacy source, not Aurora). ``_report_with_waves()``'s stored cache
        # wave fronts "mysql" (the legacy source, per STORED_WAVES above).
        md = _cache_section(render_engineering_report_md(_report_with_waves()))
        assert "It fronts the source MySQL database" in md
        assert "It fronts Aurora MySQL" not in md

    def test_cache_note_fronts_aurora_when_the_cache_wave_says_so(self):
        rep = _report_with_waves()
        rep["migration_waves"][0]["fronts"] = "aurora_mysql"
        md = _cache_section(render_engineering_report_md(rep))
        assert "It fronts Aurora MySQL" in md
        assert "It fronts the source MySQL database" not in md


class TestDeckUsesStoredWaves:
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


class TestJudgeFactsPassthrough:
    def test_migration_waves_present(self):
        facts = judge_facts.build_facts(_report_with_waves())
        assert facts["migration_waves"] == STORED_WAVES

    def test_migration_waves_absent_marker(self):
        rep = _legacy_report()
        facts = judge_facts.build_facts(rep)
        assert facts["migration_waves"] == judge_facts.MIGRATION_WAVES_ABSENT
