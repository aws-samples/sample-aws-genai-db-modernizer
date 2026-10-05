"""Migration waves (#225): the one deterministic incremental sequencing rule.

Wave order: cache (no migration, reversible) -> key-value/point-lookup queries to
DynamoDB, table group by table group -> any other direct migration target the
rule doesn't otherwise name -> search/analytics read models (OpenSearch, synced,
never system of record) -> document-shaped data (DocumentDB) -> whatever stays on
the source-compatible relational engine, carried over 1:1, always last. Waves
with nothing to move are skipped and the rest are numbered consecutively.

Covers #225: OpenSearch durable ownership, unresolved OpenSearch tables, the
cache fronting the source database, moves_from being the source engine,
known_tables filtering parser noise, the retained wave carrying unreferenced
tables, cross-wave co-dependency gates (both directions), distinct
DynamoDB-assigned query-id counts per group (every query counted in exactly
one group, or in neither if it owns no table), and the cache/owner share
overlap note.
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

KNOWN_TABLES = {"wp_posts", "wp_postmeta", "wp_comments", "wp_options"}


def _build(**overrides):
    kwargs = {
        "ranking": RANKING,
        "table_assignments": TABLE_ASSIGNMENTS,
        "query_assignments": QUERY_ASSIGNMENTS,
        "co_dependency_groups": CO_DEPENDENCY_GROUPS,
        "cache_overlay": CACHE_OVERLAY,
        "source_engine": "mysql",
        "known_tables": KNOWN_TABLES,
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

    def test_known_tables_is_optional_for_back_compat(self):
        # No known_tables -> no filtering, no unreferenced-table accounting; the
        # caller gets the pre-#5/#6 behaviour rather than an error.
        waves = _build(known_tables=None)
        assert waves is not None


class TestCacheWave:
    def test_cache_wave_fronts_the_source_database_and_moves_nothing(self):
        wave = _build()[0]
        assert wave["engines"] == ["elasticache"]
        assert wave["query_count"] == 20
        assert wave["workload_share_percent"] == 83.4
        assert wave["share_basis"] == "calls"
        # Finding 3: the cache moves nothing, and the end-state owner (DynamoDB,
        # not even built yet in wave 1) is never named as what it moves from.
        assert wave["moves_from"] == []
        assert wave["fronts"] == "mysql"
        assert wave["table_count"] == 0  # the cache owns no table, only hot reads
        # The actual source database is named (not the generic "(MySQL/PostgreSQL)").
        assert "in front of the current source database (MySQL)" in wave["rationale"]
        front_sentence = wave["rationale"].split("no data migration")[0]
        assert "DynamoDB" not in front_sentence
        assert "no data migration" in wave["rationale"]
        assert "reversible" in wave["rationale"]

    def test_cache_wave_names_postgresql_too(self):
        wave = _build(source_engine="postgresql")[0]
        assert "in front of the current source database (PostgreSQL)" in wave["rationale"]

    def test_falls_back_to_generic_phrasing_without_a_known_source_engine(self):
        wave = _build(source_engine="")[0]
        assert "in front of the current source database" in wave["rationale"]
        assert "(" not in wave["rationale"].split("in front of")[1].split(":")[0]

    def test_cache_overlap_note_names_the_owner_wave(self):
        # Finding 11: the cache share (of calls) and the owner shares (of query
        # patterns) are different bases; say so and name the overlapping wave.
        wave = _build()[0]
        assert "wave 2 (DynamoDB)" in wave["rationale"]
        assert "overlaps the owner shares" in wave["rationale"]

    def test_no_fronts_without_a_source_engine(self):
        waves = _build(source_engine="")
        assert waves[0]["fronts"] is None
        assert waves[0]["moves_from"] == []


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
        # Finding 4: data moves from the source database, not the end-state engine.
        assert wave["moves_from"] == ["mysql"]
        groups = wave["table_groups"]
        # wp_posts + wp_postmeta share a JOIN co-dependency group (q1, q2 — 2
        # distinct DynamoDB-assigned query IDs, finding 8); wp_comments does not,
        # so it forms its own, independent group with no DynamoDB-assigned query.
        assert groups == [
            {"tables": ["wp_postmeta", "wp_posts"], "query_count": 2, "kind": "co_dependency"},
            {"tables": ["wp_comments"], "query_count": 0, "kind": "independent"},
        ]

    def test_tables_with_no_co_dependency_group_form_their_own_independent_group(self):
        waves = _build(co_dependency_groups=[])
        dynamo = waves[1]
        assert dynamo["table_groups"] == [
            {
                "tables": ["wp_comments", "wp_postmeta", "wp_posts"],
                "query_count": 2,
                "kind": "independent",
            }
        ]

    def test_cross_wave_table_is_named_in_the_gate(self):
        # wp_options would normally be Aurora-only, but suppose an Aurora-routed
        # query also reads a DynamoDB table (wp_posts): that table is still
        # dual-read until Aurora's wave finishes, and the KV wave's gate says so
        # (finding 7) instead of silently claiming co-dependent tables never split.
        query_assignments = [
            *QUERY_ASSIGNMENTS,
            {"query_id": "q5", "assigned_engine": "aurora_mysql", "source_tables": ["wp_posts"]},
        ]
        wave = _build(query_assignments=query_assignments)[1]
        assert "wp_posts" in wave["gate"]
        assert "Aurora MySQL queries until wave 4" in wave["gate"]
        assert "CDC" in wave["gate"]

    def test_a_non_member_query_touching_group_tables_is_still_counted_in_it(self):
        # #225: q6 is not a member of the co-dependency group [q1, q2], but it
        # reads one of the group's tables (wp_posts) -- it must be counted in
        # that group, not left out of every group's count.
        query_assignments = [
            *QUERY_ASSIGNMENTS,
            {"query_id": "q6", "assigned_engine": "dynamodb", "source_tables": ["wp_posts"]},
        ]
        wave = _build(query_assignments=query_assignments)[1]
        co_dep = next(g for g in wave["table_groups"] if g["kind"] == "co_dependency")
        assert co_dep["query_count"] == 3  # q1, q2, q6

    def test_a_query_touching_several_groups_goes_to_the_first_one(self):
        # q7 touches both the co-dependency group's table (wp_posts) and the
        # independent table (wp_comments): it is counted once, in the first
        # bucket (by order) whose tables it touches -- never twice.
        query_assignments = [
            *QUERY_ASSIGNMENTS,
            {
                "query_id": "q7",
                "assigned_engine": "dynamodb",
                "source_tables": ["wp_posts", "wp_comments"],
            },
        ]
        wave = _build(query_assignments=query_assignments)[1]
        groups = wave["table_groups"]
        co_dep = next(g for g in groups if g["kind"] == "co_dependency")
        independent = next(g for g in groups if g["kind"] == "independent")
        assert co_dep["query_count"] == 3  # q1, q2, q7
        assert independent["query_count"] == 0
        assert sum(g["query_count"] for g in groups) == 3

    def test_group_counts_plus_queries_with_no_owned_table_equal_wave_query_count(self):
        # #225: the group counts and the wave's own query_count must reconcile
        # for a reader -- the gap is exactly the DynamoDB-assigned queries that
        # touch none of DynamoDB's owned tables (resolvable elsewhere, or not
        # resolvable at all).
        query_assignments = [
            *QUERY_ASSIGNMENTS,
            # Reads only an Aurora-owned table: not in any DynamoDB group.
            {"query_id": "q8", "assigned_engine": "dynamodb", "source_tables": ["wp_options"]},
            # No resolvable table at all.
            {"query_id": "q9", "assigned_engine": "dynamodb", "source_tables": ["unknown"]},
        ]
        ranking = [
            {**r, "assigned_queries": 4} if r["target"] == "dynamodb" else r for r in RANKING
        ]
        wave = _build(ranking=ranking, query_assignments=query_assignments)[1]
        owned = set(wave["tables"])
        dynamo_qids_touching_owned = {
            qa["query_id"]
            for qa in query_assignments
            if qa.get("assigned_engine") == "dynamodb"
            and set(qa.get("source_tables") or []) & owned
        }
        no_owned_table = sum(
            1 for qa in query_assignments if qa.get("assigned_engine") == "dynamodb"
        ) - len(dynamo_qids_touching_owned)
        assert (
            sum(g["query_count"] for g in wave["table_groups"]) + no_owned_table
            == wave["query_count"]
        )
        # And both halves of the gap are named in the gate, not just implied.
        assert "wp_options" in wave["gate"]  # reverse: Aurora owns it, DynamoDB still reads it
        assert "Aurora MySQL" in wave["gate"]
        assert "1 query could not be resolved" in wave["gate"]

    def test_reverse_dual_read_names_the_owner_and_its_wave(self):
        # #225: the reverse of the existing forward case -- a table a LATER
        # wave owns that a DynamoDB-assigned query still reads.
        query_assignments = [
            *QUERY_ASSIGNMENTS,
            {"query_id": "q8", "assigned_engine": "dynamodb", "source_tables": ["wp_options"]},
        ]
        wave = _build(query_assignments=query_assignments)[1]
        assert "1 query read 1 table (wp_options) Aurora MySQL owns (wave 4)" in wave["gate"]
        assert "CDC" in wave["gate"]


class TestSearchReadModelWave:
    def test_wave_is_served_not_owned_and_names_the_durable_owners(self):
        wave = _build()[2]
        assert wave["engines"] == ["opensearch"]
        assert wave["tables"] == ["wp_comments"]  # served via the multi-engine `engines` list
        assert wave["serves_from"] == ["dynamodb"]
        assert wave["moves_from"] == []
        assert wave["table_owners"] == [
            {"table": "wp_comments", "owner": "dynamodb", "sync": "OpenSearch Ingestion"}
        ]
        assert "never" in wave["rationale"] and "system of record" in wave["rationale"]
        assert "re-index" in wave["rationale"]

    def test_table_whose_primary_engine_is_opensearch_gets_a_durable_owner(self):
        # Finding 1: an upstream bug (#317) can leave primary_engine=opensearch.
        # The wave must still name a real owner — the first non-search engine in
        # `engines`, here dynamodb — and that table must show up in DynamoDB's
        # wave, not vanish from every wave.
        table_assignments = [
            *TABLE_ASSIGNMENTS,
            {
                "table_id": "wp_users",
                "primary_engine": "opensearch",
                "engines": ["opensearch", "dynamodb"],
                "query_count": 15,
            },
        ]
        waves = _build(
            table_assignments=table_assignments,
            known_tables=KNOWN_TABLES | {"wp_users"},
        )
        dynamo = waves[1]
        search = waves[2]
        assert "wp_users" in dynamo["tables"]
        owner = next(o for o in search["table_owners"] if o["table"] == "wp_users")
        assert owner["owner"] == "dynamodb"

    def test_table_whose_only_engine_is_opensearch_falls_back_to_retained(self):
        table_assignments = [
            *TABLE_ASSIGNMENTS,
            {
                "table_id": "wp_usermeta",
                "primary_engine": "opensearch",
                "engines": ["opensearch"],
                "query_count": 15,
            },
        ]
        waves = _build(
            table_assignments=table_assignments,
            known_tables=KNOWN_TABLES | {"wp_usermeta"},
        )
        retained = waves[-1]
        search = next(w for w in waves if w["engines"] == ["opensearch"])
        assert "wp_usermeta" in retained["tables"]
        owner = next(o for o in search["table_owners"] if o["table"] == "wp_usermeta")
        assert owner["owner"] == "aurora_mysql"
        assert owner["sync"] == "zero-ETL"

    def test_unresolved_indexed_tables_fall_back_to_the_retained_engine(self):
        # Finding 2: discourse-like case — every query's source_tables is
        # "unknown"/pseudo, so no table can be resolved from the SQL at all.
        ranking = [
            {"target": "opensearch", "assigned_queries": 3, "workload_percent": 2.0},
            {"target": "aurora_postgresql", "assigned_queries": 50, "workload_percent": 98.0},
        ]
        table_assignments = [
            {
                "table_id": "t1",
                "primary_engine": "aurora_postgresql",
                "engines": ["aurora_postgresql"],
                "query_count": 50,
            }
        ]
        waves = build_migration_waves(
            ranking=ranking,
            table_assignments=table_assignments,
            query_assignments=[],
            co_dependency_groups=[],
            cache_overlay=None,
            source_engine="postgresql",
            known_tables={"t1"},
        )
        search = next(w for w in waves if w["engines"] == ["opensearch"])
        assert search["tables"] == []
        assert search["table_count"] == 0
        assert search["serves_from"] == ["aurora_postgresql"]
        assert "could not be resolved from the SQL" in search["rationale"]
        assert "aurora_postgresql" not in search["rationale"]  # the display name, not the key
        assert "Aurora PostgreSQL" in search["rationale"]
        assert "index/mapping definitions" in search["gate"]


class TestRetainedWave:
    def test_last_wave_is_the_source_compatible_engine_carried_over(self):
        wave = _build()[-1]
        assert wave["engines"] == ["aurora_mysql"]
        assert wave["tables"] == ["wp_options"]
        assert wave["moves_from"] == []
        assert "homogeneous migration" in wave["rationale"]
        assert "schema carried over 1:1" in wave["rationale"]

    def test_retained_wave_absent_when_nothing_stays(self):
        ranking = [r for r in RANKING if r["target"] != "aurora_mysql"]
        table_assignments = [t for t in TABLE_ASSIGNMENTS if t["primary_engine"] != "aurora_mysql"]
        waves = _build(
            ranking=ranking,
            table_assignments=table_assignments,
            known_tables=KNOWN_TABLES - {"wp_options"},
        )
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
            known_tables={"t1", "t2"},
        )
        assert waves[-1]["engines"] == ["aurora_postgresql"]
        assert waves[-1]["moves_from"] == []

    def test_unreferenced_known_tables_are_carried_in_the_retained_wave(self):
        # Finding 6: a table the collector saw but no query ever touched is not
        # dropped — it stays on the retained engine, "no observed queries".
        waves = _build(known_tables=KNOWN_TABLES | {"wp_never_queried"})
        retained = waves[-1]
        assert "wp_never_queried" in retained["tables"]
        assert retained["table_count"] == 2
        assert "no observed query" in retained["rationale"]

    def test_unreferenced_tables_still_produce_a_wave_with_zero_queries(self):
        ranking = [r for r in RANKING if r["target"] != "aurora_mysql"]
        table_assignments = [t for t in TABLE_ASSIGNMENTS if t["primary_engine"] != "aurora_mysql"]
        waves = _build(
            ranking=ranking,
            table_assignments=table_assignments,
            known_tables=(KNOWN_TABLES - {"wp_options"}) | {"wp_never_queried"},
        )
        retained = waves[-1]
        assert retained["engines"] == ["aurora_mysql"]
        assert retained["tables"] == ["wp_never_queried"]
        assert retained["query_count"] == 0


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
        waves = _build(
            ranking=ranking,
            table_assignments=table_assignments,
            known_tables=KNOWN_TABLES | {"wp_docs"},
        )
        engines_in_order = [w["engines"][0] for w in waves]
        # search read models before document data, both before the retained wave
        assert engines_in_order == [
            "elasticache",
            "dynamodb",
            "opensearch",
            "documentdb",
            "aurora_mysql",
        ]
        document_wave = waves[3]
        assert document_wave["moves_from"] == ["mysql"]


class TestPseudoAndUnknownTables:
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

    def test_known_tables_filters_parser_noise(self):
        # Finding 5: a name the parser mistook for a table (not pseudo, but not
        # in the collected schema either) never reaches a wave.
        table_assignments = [
            *TABLE_ASSIGNMENTS,
            {
                "table_id": "CURRENT_TIMESTAMP",
                "primary_engine": "dynamodb",
                "engines": ["dynamodb"],
                "query_count": 7,
            },
        ]
        waves = _build(table_assignments=table_assignments)
        dynamo = waves[1]
        assert "CURRENT_TIMESTAMP" not in dynamo["tables"]
        assert dynamo["table_count"] == 3


class TestOwnerWaveCoverage:
    def test_every_table_opensearch_serves_has_an_owner_wave(self):
        """Review finding 1's regression test: no wave table is left without an
        owner wave somewhere in the roadmap (the product rule in the task body:
        "Every table [OpenSearch] serves has a durable owner ... in an owner
        wave")."""
        table_assignments = [
            *TABLE_ASSIGNMENTS,
            {
                "table_id": "wp_users",
                "primary_engine": "opensearch",
                "engines": ["opensearch", "dynamodb"],
                "query_count": 15,
            },
            {
                "table_id": "wp_usermeta",
                "primary_engine": "opensearch",
                "engines": ["opensearch"],
                "query_count": 15,
            },
        ]
        known = KNOWN_TABLES | {"wp_users", "wp_usermeta"}
        waves = _build(table_assignments=table_assignments, known_tables=known)
        search_wave = next(w for w in waves if w["engines"] == ["opensearch"])
        owner_waves_tables: set[str] = set()
        for w in waves:
            if w["engines"] != ["opensearch"] and w["engines"] != ["elasticache"]:
                owner_waves_tables.update(w.get("tables") or [])
        for served_table in search_wave["tables"]:
            assert (
                served_table in owner_waves_tables
            ), f"{served_table} is served by OpenSearch but has no owner wave"
