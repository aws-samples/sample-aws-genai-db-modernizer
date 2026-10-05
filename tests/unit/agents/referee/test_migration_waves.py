"""Migration waves (#321, reordering #225): the relational move comes first.

Wave order: Aurora (the whole source database, carried over 1:1, skipped when
the source is already Aurora) -> cache (no migration, reversible, fronts
Aurora) -> key-value/point-lookup queries to DynamoDB, table group by table
group -> document-shaped data to DocumentDB -> any other direct migration
target the rule doesn't otherwise name -> search/analytics read models
(OpenSearch, synced, never system of record). Waves with nothing to move are
skipped and the rest are numbered consecutively.

Covers #321: the Aurora wave's homogeneity statement, skipping it when the
source is already Aurora, ``moves_from``/``fronts`` naming the retained Aurora
engine (not the legacy source) for every wave after wave 1, and that wave 1
always appears (unlike the old "retained" wave) regardless of whether any
query is still assigned to Aurora.

Also covers #225 (unchanged by the reorder): OpenSearch durable ownership,
unresolved OpenSearch tables, known_tables filtering parser noise, cross-wave
co-dependency gates (both directions), distinct DynamoDB-assigned query-id
counts per group (every query counted in exactly one group, or in neither if
it owns no table), and the cache/owner share overlap note.
"""

from __future__ import annotations

from src.agents.referee.migration_waves import _durable_owner, build_migration_waves

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
            ["aurora_mysql"],
            ["elasticache"],
            ["dynamodb"],
            ["opensearch"],
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
        assert [w["engines"] for w in waves] == [["aurora_mysql"], ["dynamodb"], ["opensearch"]]
        assert all(w["wave"] for w in waves)

    def test_empty_wave_is_skipped_not_left_blank(self):
        # No opensearch in ranking at all -> no search wave, not an empty one.
        ranking = [r for r in RANKING if r["target"] != "opensearch"]
        waves = _build(ranking=ranking)
        assert "opensearch" not in [e for w in waves for e in w["engines"]]
        assert [w["wave"] for w in waves] == list(range(1, len(waves) + 1))

    def test_known_tables_is_optional_for_back_compat(self):
        # No known_tables -> no filtering; wave 1 falls back to every table
        # named anywhere in table_assignments (the pre-#321 behaviour had no
        # equivalent since the old retained wave only listed owned tables).
        waves = _build(known_tables=None)
        assert waves is not None
        aurora = next(w for w in waves if w["engines"] == ["aurora_mysql"])
        assert sorted(aurora["tables"]) == ["wp_comments", "wp_options", "wp_postmeta", "wp_posts"]

    def test_dynamodb_and_documentdb_share_one_wave_when_both_routed(self):
        # #324 review (finding 10): the maintainer's plan is "cache +
        # DynamoDB or cache + DocumentDB" -- one wave 3 for both when both
        # are routed, not a separate wave each.
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
        engines_in_order = [w["engines"] for w in waves]
        assert engines_in_order == [
            ["aurora_mysql"],
            ["elasticache"],
            ["dynamodb", "documentdb"],
            ["opensearch"],
        ]
        shared_wave = waves[2]
        # moves_from is now the retained Aurora engine (#321), not the legacy source.
        assert shared_wave["moves_from"] == ["aurora_mysql"]
        assert shared_wave["query_count"] == 100  # 98 (dynamodb) + 2 (documentdb)
        assert "DynamoDB" in shared_wave["gate"] and "DocumentDB" in shared_wave["gate"]


class TestAuroraWave:
    def test_whole_schema_moves_first_and_is_homogeneous(self):
        wave = _build()[0]
        assert wave["engines"] == ["aurora_mysql"]
        assert wave["moves_from"] == ["mysql"]
        assert wave["fronts"] is None
        # Every collected table, not just the ones aurora_mysql owns (#321).
        assert sorted(wave["tables"]) == ["wp_comments", "wp_options", "wp_postmeta", "wp_posts"]
        assert wave["table_count"] == 4
        assert wave["homogeneity"] == "homogeneous"
        # query_count/share: the share still on Aurora once every later wave
        # has moved its own share away (the old "retained" wave's figures).
        assert wave["query_count"] == 5
        assert wave["workload_share_percent"] == 4.7
        assert wave["share_basis"] == "queries"
        # #324 review (finding 8): shorter wording, no claimed check beyond
        # engine + version, no "->".
        assert "same engine family" in wave["rationale"]
        assert "moves 1:1 to Aurora MySQL" in wave["rationale"]
        assert "feature compatibility is not assessed" in wave["rationale"]
        assert "->" not in wave["rationale"]
        assert "4 tables and views" in wave["rationale"]
        # #324 review (finding 2): the end-state framing lives on the wave's
        # own cutover_query_count field, not just buried in prose.
        assert wave["cutover_query_count"] == 107

    def test_mariadb_source_is_called_mysql_compatible_not_same_family(self):
        # #324 review (finding 8): MariaDB is a fork, not literally MySQL --
        # avoid implying a stronger check than engine-family matching did.
        wave = _build(source_engine="mariadb")[0]
        assert wave["engines"] == ["aurora_mysql"]
        assert wave["homogeneity"] == "homogeneous"
        assert "MySQL-compatible" in wave["rationale"]
        assert "same engine family" not in wave["rationale"]

    def test_homogeneity_note_names_the_version_when_the_collector_has_one(self):
        wave = _build(source_version="8.0.45")[0]
        assert "8.0.45" in wave["rationale"]

    def test_homogeneity_note_says_nothing_invented_when_the_collector_has_no_version(self):
        # #324 review (finding 8): no version is reported, not "assumed
        # compatible" or any other invented claim -- the sentence just omits
        # a version number it does not have.
        wave = _build(source_version=None)[0]
        assert "(MySQL)" in wave["rationale"]
        assert "feature compatibility is not assessed" in wave["rationale"]

    def test_version_banner_is_trimmed_to_its_leading_text(self):
        # #321: never show a full server banner verbatim in a one-line wave.
        wave = _build(
            source_engine="postgresql", source_version="PostgreSQL 16.10 on x86_64-pc-linux-gnu"
        )[0]
        assert "PostgreSQL 16.10" in wave["rationale"]
        assert "x86_64" not in wave["rationale"]

    def test_version_banner_leading_with_the_engine_name_is_not_duplicated(self):
        # #321: the collector's version banner for PostgreSQL leads with the
        # engine name itself ("PostgreSQL 16.10 on ..."); showing
        # "PostgreSQL PostgreSQL 16.10" would be an obviously duplicated
        # rendering bug, not a faithful statement of what was collected.
        wave = _build(
            source_engine="postgresql",
            source_version="PostgreSQL 16.10 on x86_64-pc-linux-gnu",
        )[0]
        assert "PostgreSQL PostgreSQL" not in wave["rationale"]
        assert "PostgreSQL 16.10" in wave["rationale"]

    def test_appears_even_when_no_query_is_still_assigned_to_aurora(self):
        # #321: unlike the pre-#321 "retained" wave, wave 1 is the
        # schema-carryover step itself, not a leftover accounting -- it must
        # appear even when nothing is left on Aurora at the end.
        ranking = [r for r in RANKING if r["target"] != "aurora_mysql"]
        table_assignments = [t for t in TABLE_ASSIGNMENTS if t["primary_engine"] != "aurora_mysql"]
        waves = _build(
            ranking=ranking,
            table_assignments=table_assignments,
            known_tables=KNOWN_TABLES - {"wp_options"},
        )
        aurora = waves[0]
        assert aurora["engines"] == ["aurora_mysql"]
        assert aurora["query_count"] == 0
        assert aurora["workload_share_percent"] == 0.0
        assert sorted(aurora["tables"]) == ["wp_comments", "wp_postmeta", "wp_posts"]

    def test_postgresql_source_is_homogeneous_to_aurora_postgresql(self):
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
        aurora = waves[0]
        assert aurora["engines"] == ["aurora_postgresql"]
        assert aurora["moves_from"] == ["postgresql"]
        assert aurora["homogeneity"] == "homogeneous"

    def test_unreferenced_known_tables_are_carried_in_wave_1(self):
        # A table the collector saw but no query ever touched is not dropped
        # -- it is still part of "every collected table and view" (#321).
        waves = _build(known_tables=KNOWN_TABLES | {"wp_never_queried"})
        aurora = waves[0]
        assert "wp_never_queried" in aurora["tables"]
        assert aurora["table_count"] == 5

    def test_heterogeneous_source_has_no_engine_and_flags_a_risk(self):
        waves = _build(source_engine="sqlserver", known_tables=KNOWN_TABLES)
        aurora = waves[0]
        assert aurora["engines"] == []
        assert aurora["homogeneity"] == "heterogeneous"
        assert aurora["moves_from"] == ["sqlserver"]
        assert "heterogeneous" in aurora["rationale"]
        assert "Risk" in aurora["gate"]
        assert "no Aurora-compatible target engine" in aurora["gate"]
        # #324 review (finding 7): titled by what it moves *off*, since it
        # moves to no named engine; the display name is "SQL Server", not
        # the title-cased fallback "Sqlserver".
        assert aurora["title"] == "Move off SQL Server (heterogeneous)"
        assert "Sqlserver" not in aurora["rationale"]
        assert "SQL Server" in aurora["rationale"]
        # #324 review (finding 7): no share or "moves on from Aurora"
        # sentence -- there is no Aurora here for either to be true of.
        # ("no source-compatible Aurora engine" is fine; it says there isn't
        # one, not that something moves on from it.)
        assert "%" not in aurora["rationale"]
        assert "moves on from Aurora" not in aurora["rationale"]
        assert "remain on" not in aurora["rationale"]
        assert aurora["query_count"] == 0
        assert aurora["workload_share_percent"] == 0.0
        assert "cutover_query_count" not in aurora

    def test_heterogeneous_oracle_and_db2_get_real_display_names(self):
        # #324 review (finding 7): sqlserver/oracle/db2 added to
        # SOURCE_ENGINE_DISPLAY_NAMES so they don't fall back to
        # display_engine's title-cased guess ("Oracle" is already correct by
        # coincidence; "Db2" is not -- the generic fallback would say "Db2"
        # too since it's already one word, but verify explicitly anyway).
        oracle = _build(source_engine="oracle", known_tables=KNOWN_TABLES)[0]
        assert oracle["title"] == "Move off Oracle (heterogeneous)"
        db2 = _build(source_engine="db2", known_tables=KNOWN_TABLES)[0]
        assert db2["title"] == "Move off Db2 (heterogeneous)"

    def test_skipped_when_the_source_is_already_aurora(self):
        waves = _build(source_engine="aurora_mysql", known_tables=KNOWN_TABLES)
        # No wave carries a homogeneity verdict: wave 1 (Aurora) was skipped
        # entirely because there is nothing to move.
        assert all(w.get("homogeneity") is None for w in waves)
        # Cache remains wave 1 (fronting the "source" aurora_mysql itself,
        # since there is no earlier wave to have moved it anywhere else).
        assert waves[0]["engines"] == ["elasticache"]

    def test_absent_without_a_source_engine(self):
        waves = _build(source_engine="", known_tables=KNOWN_TABLES)
        assert all(w.get("homogeneity") is None for w in waves)
        # Cache remains wave 1 since there is nothing to say about Aurora.
        assert waves[0]["engines"] == ["elasticache"]


class TestCacheWave:
    def test_cache_wave_fronts_aurora_now_that_wave_1_has_migrated_it(self):
        wave = _build()[1]
        assert wave["engines"] == ["elasticache"]
        assert wave["query_count"] == 20
        assert wave["workload_share_percent"] == 83.4
        assert wave["share_basis"] == "calls"
        assert wave["moves_from"] == []
        assert wave["fronts"] == "aurora_mysql"
        assert wave["table_count"] == 0  # the cache owns no table, only hot reads
        assert "in front of Aurora MySQL" in wave["rationale"]
        assert "no data migration" in wave["rationale"]
        assert "reversible" in wave["rationale"]

    def test_cache_wave_names_postgresql_too(self):
        waves = _build(
            source_engine="postgresql",
            ranking=[
                {"target": "dynamodb", "assigned_queries": 10, "workload_percent": 50.0},
                {"target": "aurora_postgresql", "assigned_queries": 10, "workload_percent": 50.0},
            ],
            table_assignments=[
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
            ],
            known_tables={"t1", "t2"},
        )
        cache = next(w for w in waves if w["engines"] == ["elasticache"])
        assert "in front of Aurora PostgreSQL" in cache["rationale"]
        assert cache["fronts"] == "aurora_postgresql"

    def test_falls_back_to_generic_phrasing_without_a_known_source_engine(self):
        wave = _build(source_engine="")[0]
        assert wave["engines"] == ["elasticache"]
        assert "in front of the current source database" in wave["rationale"]
        assert "(" not in wave["rationale"].split("in front of")[1].split(":")[0]

    def test_cache_overlap_note_names_the_owner_wave(self):
        # Finding 11 (#225): the cache share (of calls) and the owner shares
        # (of query patterns) are different bases; say so and name the
        # overlapping wave -- now wave 3 (DynamoDB) since Aurora is wave 1.
        wave = _build()[1]
        assert "wave 3 (DynamoDB)" in wave["rationale"]
        assert "overlaps the owner shares" in wave["rationale"]

    def test_no_fronts_without_a_source_engine(self):
        waves = _build(source_engine="")
        assert waves[0]["fronts"] is None
        assert waves[0]["moves_from"] == []


class TestDynamoDbWave:
    def test_wave_lists_tables_and_respects_co_dependency_groups(self):
        wave = _build()[2]
        assert wave["engines"] == ["dynamodb"]
        # wp_posts, wp_postmeta, wp_comments: dynamodb is the primary engine for all three
        # (wp_comments is also served by opensearch — the multi-engine rule, AGENTS.md).
        assert wave["table_count"] == 3
        assert sorted(wave["tables"]) == ["wp_comments", "wp_postmeta", "wp_posts"]
        assert wave["query_count"] == 98
        assert wave["workload_share_percent"] == 91.6
        assert wave["share_basis"] == "queries"
        # #321: data moves from the retained Aurora engine, not the legacy source.
        assert wave["moves_from"] == ["aurora_mysql"]
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
        dynamo = waves[2]
        assert dynamo["table_groups"] == [
            {
                "tables": ["wp_comments", "wp_postmeta", "wp_posts"],
                "query_count": 2,
                "kind": "independent",
            }
        ]

    def test_cross_wave_table_is_named_in_the_gate(self):
        # wp_options would normally be Aurora-only, but suppose an Aurora-routed
        # query also reads a DynamoDB table (wp_posts): Aurora is wave 1, which
        # already ran, so this table is permanently dual-read, not "until" a
        # wave that is actually in the past (#324 review finding 1) -- the KV
        # wave's gate says so instead of silently claiming co-dependent tables
        # never split.
        query_assignments = [
            *QUERY_ASSIGNMENTS,
            {"query_id": "q5", "assigned_engine": "aurora_mysql", "source_tables": ["wp_posts"]},
        ]
        wave = _build(query_assignments=query_assignments)[2]
        assert "wp_posts" in wave["gate"]
        assert "Aurora MySQL queries, which remain on Aurora MySQL" in wave["gate"]
        assert "Aurora MySQL queries until wave" not in wave["gate"]
        assert "CDC" in wave["gate"]

    def test_cross_wave_table_still_future_names_the_later_wave(self):
        # The flip side of the above: a table DynamoDB owns that a query
        # assigned to a *later* wave (OpenSearch, wave 4) still reads -- that
        # dual read really does resolve once wave 4 runs, so "until wave 4"
        # is correct here (#324 review finding 1).
        wave = _build()[2]
        assert "OpenSearch queries until wave 4" in wave["gate"]

    def test_a_non_member_query_touching_group_tables_is_still_counted_in_it(self):
        # #225: q6 is not a member of the co-dependency group [q1, q2], but it
        # reads one of the group's tables (wp_posts) -- it must be counted in
        # that group, not left out of every group's count.
        query_assignments = [
            *QUERY_ASSIGNMENTS,
            {"query_id": "q6", "assigned_engine": "dynamodb", "source_tables": ["wp_posts"]},
        ]
        wave = _build(query_assignments=query_assignments)[2]
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
        wave = _build(query_assignments=query_assignments)[2]
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
        wave = _build(ranking=ranking, query_assignments=query_assignments)[2]
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
        # wave owns that a DynamoDB-assigned query still reads. Aurora is now
        # wave 1 (#321), so the gate names wave 1, not the last wave.
        query_assignments = [
            *QUERY_ASSIGNMENTS,
            {"query_id": "q8", "assigned_engine": "dynamodb", "source_tables": ["wp_options"]},
        ]
        wave = _build(query_assignments=query_assignments)[2]
        assert "1 query read 1 table (wp_options) Aurora MySQL owns (wave 1)" in wave["gate"]
        assert "CDC" in wave["gate"]


class TestSearchReadModelWave:
    def test_wave_is_served_not_owned_and_names_the_durable_owners(self):
        wave = _build()[3]
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
        dynamo = waves[2]
        search = waves[3]
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
        # #321: wave 1 (Aurora) carries the whole schema, so the table shows
        # up there, not in a separate trailing "retained" wave.
        aurora = waves[0]
        search = next(w for w in waves if w["engines"] == ["opensearch"])
        assert "wp_usermeta" in aurora["tables"]
        owner = next(o for o in search["table_owners"] if o["table"] == "wp_usermeta")
        assert owner["owner"] == "aurora_mysql"
        assert owner["sync"] == "zero-ETL"

    def test_durable_owner_skips_an_elasticache_primary_engine(self):
        # #318 review: _durable_owner was generalized from skipping only
        # SEARCH_ENGINES to skipping NON_OWNER_ENGINES, so a cache engine as
        # primary_engine (never legitimate, but defense in depth) is also
        # passed over for the first real owner in `engines`.
        table = {
            "table_id": "wp_sessions",
            "primary_engine": "elasticache",
            "engines": ["elasticache", "dynamodb"],
            "query_count": 10,
        }
        assert _durable_owner(table, retained_engine="aurora_mysql") == "dynamodb"

    def test_durable_owner_falls_back_to_retained_when_only_elasticache(self):
        table = {
            "table_id": "wp_sessions",
            "primary_engine": "elasticache",
            "engines": ["elasticache"],
            "query_count": 10,
        }
        assert _durable_owner(table, retained_engine="aurora_mysql") == "aurora_mysql"

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


class TestOtherMigrationTarget:
    def test_an_unnamed_engine_still_gets_a_wave(self):
        ranking = [
            *RANKING,
            {"target": "neptune", "assigned_queries": 2, "workload_percent": 1.0},
        ]
        table_assignments = [
            *TABLE_ASSIGNMENTS,
            {
                "table_id": "wp_graph",
                "primary_engine": "neptune",
                "engines": ["neptune"],
                "query_count": 2,
            },
        ]
        waves = _build(
            ranking=ranking,
            table_assignments=table_assignments,
            known_tables=KNOWN_TABLES | {"wp_graph"},
        )
        engines_in_order = [w["engines"][0] for w in waves]
        # "any other target" slots in after DynamoDB/DocumentDB, before search.
        assert engines_in_order == [
            "aurora_mysql",
            "elasticache",
            "dynamodb",
            "neptune",
            "opensearch",
        ]
        other_wave = waves[3]
        assert other_wave["moves_from"] == ["aurora_mysql"]


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
        dynamo = waves[2]
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
        dynamo = waves[2]
        assert "CURRENT_TIMESTAMP" not in dynamo["tables"]
        assert dynamo["table_count"] == 3


class TestOwnerWaveCoverage:
    def test_every_table_opensearch_serves_has_an_owner_wave(self):
        """#225: no wave table is left without an
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
