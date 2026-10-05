"""Migration waves (#225): the one deterministic incremental sequencing rule.

Wave order: cache (no migration, reversible) -> key-value/point-lookup queries to
DynamoDB, table group by table group -> any other direct migration target the
rule doesn't otherwise name -> search/analytics read models (OpenSearch, synced,
never system of record) -> document-shaped data (DocumentDB) -> whatever stays on
the source-compatible relational engine, carried over 1:1, always last. Waves
with nothing to move are skipped and the rest are numbered consecutively.
"""

from __future__ import annotations

from src.agents.referee.migration_waves import build_migration_waves

RANKING = [
    {"target": "dynamodb", "assigned_queries": 98, "workload_percent": 91.6},
    {"target": "aurora_mysql", "assigned_queries": 5, "workload_percent": 4.7},
    {"target": "opensearch", "assigned_queries": 4, "workload_percent": 3.7},
]

TABLE_ASSIGNMENTS = [
    {
        "table_id": "wp_posts",
        "primary_engine": "dynamodb",
        "engines": ["dynamodb"],
        "query_count": 40,
    },
    {
        "table_id": "wp_postmeta",
        "primary_engine": "dynamodb",
        "engines": ["dynamodb"],
        "query_count": 30,
    },
    {
        "table_id": "wp_comments",
        "primary_engine": "dynamodb",
        "engines": ["dynamodb", "opensearch"],
        "query_count": 20,
    },
    {
        "table_id": "wp_options",
        "primary_engine": "aurora_mysql",
        "engines": ["aurora_mysql"],
        "query_count": 5,
    },
]

QUERY_ASSIGNMENTS = [
    {"query_id": "q1", "assigned_engine": "dynamodb", "source_tables": ["wp_posts"]},
    {"query_id": "q2", "assigned_engine": "dynamodb", "source_tables": ["wp_posts", "wp_postmeta"]},
    {"query_id": "q3", "assigned_engine": "opensearch", "source_tables": ["wp_comments"]},
    {"query_id": "q4", "assigned_engine": "aurora_mysql", "source_tables": ["wp_options"]},
]

CO_DEPENDENCY_GROUPS = [["q1", "q2"]]

CACHE_OVERLAY = {
    "engine": "elasticache",
    "query_count": 20,
    "call_share_percent": 83.4,
    "owners": {"dynamodb": 20},
}


def _build(**overrides):
    kwargs = {
        "ranking": RANKING,
        "table_assignments": TABLE_ASSIGNMENTS,
        "query_assignments": QUERY_ASSIGNMENTS,
        "co_dependency_groups": CO_DEPENDENCY_GROUPS,
        "cache_overlay": CACHE_OVERLAY,
        "source_engine": "mysql",
    }
    kwargs.update(overrides)
    return build_migration_waves(**kwargs)


class TestOrderAndNumbering:
    def test_full_shape_wordpress_like(self):
        waves = _build()
        assert [w["wave"] for w in waves] == list(range(1, len(waves) + 1))
        assert [w["engines"] for w in waves] == [
            ["elasticache"],
            ["dynamodb"],
            ["opensearch"],
            ["aurora_mysql"],
        ]

    def test_no_ranking_means_no_waves(self):
        assert (
            build_migration_waves(
                ranking=[],
                table_assignments=[],
                query_assignments=[],
                co_dependency_groups=[],
                cache_overlay=None,
                source_engine="mysql",
            )
            is None
        )

    def test_cache_wave_skipped_without_a_cache_overlay(self):
        waves = _build(cache_overlay=None)
        assert [w["engines"] for w in waves] == [["dynamodb"], ["opensearch"], ["aurora_mysql"]]
        assert all(w["wave"] for w in waves)

    def test_empty_wave_is_skipped_not_left_blank(self):
        # No opensearch in ranking at all -> no search wave, not an empty one.
        ranking = [r for r in RANKING if r["target"] != "opensearch"]
        waves = _build(ranking=ranking)
        assert "opensearch" not in [e for w in waves for e in w["engines"]]
        assert [w["wave"] for w in waves] == list(range(1, len(waves) + 1))


class TestCacheWave:
    def test_cache_wave_is_first_and_moves_no_data(self):
        wave = _build()[0]
        assert wave["engines"] == ["elasticache"]
        assert wave["query_count"] == 20
        assert wave["workload_share_percent"] == 83.4
        assert wave["share_basis"] == "calls"
        assert wave["moves_from"] == ["aurora_mysql"]
        assert wave["table_count"] == 0  # the cache owns no table, only hot reads
        assert "no data migration" in wave["rationale"]
        assert "reversible" in wave["rationale"]


class TestDynamoDbWave:
    def test_wave_lists_tables_and_respects_co_dependency_groups(self):
        wave = _build()[1]
        assert wave["engines"] == ["dynamodb"]
        # wp_posts, wp_postmeta, wp_comments: dynamodb is the primary engine for all three
        # (wp_comments is also served by opensearch — the multi-engine rule, AGENTS.md).
        assert wave["table_count"] == 3
        assert sorted(wave["tables"]) == ["wp_comments", "wp_postmeta", "wp_posts"]
        assert wave["query_count"] == 98
        assert wave["workload_share_percent"] == 91.6
        assert wave["share_basis"] == "queries"
        assert wave["moves_from"] == ["aurora_mysql"]
        groups = wave["table_groups"]
        # wp_posts + wp_postmeta share a JOIN co-dependency group; wp_comments does not,
        # so it forms its own, independent group.
        assert groups == [
            {"tables": ["wp_postmeta", "wp_posts"], "query_count": 70},
            {"tables": ["wp_comments"], "query_count": 20},
        ]

    def test_tables_with_no_co_dependency_group_form_their_own_group(self):
        waves = _build(co_dependency_groups=[])
        dynamo = waves[1]
        assert dynamo["table_groups"] == [
            {"tables": ["wp_comments", "wp_postmeta", "wp_posts"], "query_count": 90}
        ]


class TestSearchReadModelWave:
    def test_wave_is_served_not_owned_and_names_the_durable_owners(self):
        wave = _build()[2]
        assert wave["engines"] == ["opensearch"]
        assert wave["tables"] == ["wp_comments"]  # served via the multi-engine `engines` list
        assert wave["serves_from"] == ["dynamodb"]
        assert wave["moves_from"] == []
        assert "never" in wave["rationale"] and "system of record" in wave["rationale"]
        assert "re-index" in wave["rationale"]


class TestRetainedWave:
    def test_last_wave_is_the_source_compatible_engine_carried_over(self):
        wave = _build()[-1]
        assert wave["engines"] == ["aurora_mysql"]
        assert wave["tables"] == ["wp_options"]
        assert wave["moves_from"] == []
        assert "carried over 1:1" in wave["rationale"]
        assert "no migration" in wave["rationale"]

    def test_retained_wave_absent_when_nothing_stays(self):
        ranking = [r for r in RANKING if r["target"] != "aurora_mysql"]
        table_assignments = [t for t in TABLE_ASSIGNMENTS if t["primary_engine"] != "aurora_mysql"]
        waves = _build(ranking=ranking, table_assignments=table_assignments)
        assert "aurora_mysql" not in [e for w in waves for e in w["engines"]]

    def test_postgresql_source_retains_aurora_postgresql(self):
        ranking = [
            {"target": "dynamodb", "assigned_queries": 10, "workload_percent": 50.0},
            {"target": "aurora_postgresql", "assigned_queries": 10, "workload_percent": 50.0},
        ]
        table_assignments = [
            {
                "table_id": "t1",
                "primary_engine": "dynamodb",
                "engines": ["dynamodb"],
                "query_count": 10,
            },
            {
                "table_id": "t2",
                "primary_engine": "aurora_postgresql",
                "engines": ["aurora_postgresql"],
                "query_count": 10,
            },
        ]
        waves = _build(
            ranking=ranking,
            table_assignments=table_assignments,
            query_assignments=[],
            co_dependency_groups=[],
            cache_overlay=None,
            source_engine="postgresql",
        )
        assert waves[-1]["engines"] == ["aurora_postgresql"]


class TestOtherMigrationTarget:
    def test_an_unnamed_engine_still_gets_a_wave(self):
        ranking = [
            *RANKING,
            {"target": "documentdb", "assigned_queries": 2, "workload_percent": 1.0},
        ]
        table_assignments = [
            *TABLE_ASSIGNMENTS,
            {
                "table_id": "wp_docs",
                "primary_engine": "documentdb",
                "engines": ["documentdb"],
                "query_count": 2,
            },
        ]
        waves = _build(ranking=ranking, table_assignments=table_assignments)
        engines_in_order = [w["engines"][0] for w in waves]
        # search read models before document data, both before the retained wave
        assert engines_in_order == [
            "elasticache",
            "dynamodb",
            "opensearch",
            "documentdb",
            "aurora_mysql",
        ]


class TestPseudoTables:
    def test_dual_and_unknown_pseudo_tables_are_dropped(self):
        table_assignments = [
            *TABLE_ASSIGNMENTS,
            {
                "table_id": "DUAL",
                "primary_engine": "dynamodb",
                "engines": ["dynamodb"],
                "query_count": 1,
            },
            {
                "table_id": "unknown",
                "primary_engine": "dynamodb",
                "engines": ["dynamodb"],
                "query_count": 1,
            },
        ]
        waves = _build(table_assignments=table_assignments)
        dynamo = waves[1]
        assert "DUAL" not in dynamo["tables"]
        assert "unknown" not in dynamo["tables"]
