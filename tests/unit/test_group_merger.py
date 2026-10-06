"""Unit tests for the schema design group merger."""

import pytest

from src.agents.schema_design.group_merger import (
    MERGE_FAILURE_PREFIX,
    OVERLAP_PREFIX,
    merge_failures,
    merge_group_drafts,
    merge_schema_groups,
    merge_warnings,
)
from src.storage.local_store import LocalArtifactStore


class TestMergeGroupDrafts:
    def test_single_draft_returned_as_is(self):
        draft = {"table_definitions": [{"table_name": "a"}], "validation_passed": True}
        result = merge_group_drafts([draft], "dynamodb")
        assert result == draft

    def test_empty_drafts_raises(self):
        with pytest.raises(ValueError, match="No group drafts"):
            merge_group_drafts([], "dynamodb")

    def test_dynamodb_list_fields_concatenated(self):
        d1 = {
            "access_patterns": [{"name": "ap1"}],
            "table_definitions": [{"table_name": "t1"}],
            "trade_offs": [
                {
                    "description": "to1",
                    "impact": "i1",
                    "source_tables": [],
                    "target_tables": [],
                    "query_ids": [],
                    "engine": "dynamodb",
                }
            ],
            "validation_passed": True,
        }
        d2 = {
            "access_patterns": [{"name": "ap2"}],
            "table_definitions": [{"table_name": "t2"}],
            "trade_offs": [
                {
                    "description": "to2",
                    "impact": "i2",
                    "source_tables": [],
                    "target_tables": [],
                    "query_ids": [],
                    "engine": "dynamodb",
                }
            ],
            "validation_passed": True,
        }
        result = merge_group_drafts([d1, d2], "dynamodb")
        assert len(result["access_patterns"]) == 2
        assert len(result["table_definitions"]) == 2
        assert len(result["trade_offs"]) == 2

    def test_dynamodb_table_deduplication(self):
        d1 = {"table_definitions": [{"table_name": "users"}], "validation_passed": True}
        d2 = {
            "table_definitions": [{"table_name": "users"}, {"table_name": "orders"}],
            "validation_passed": True,
        }
        result = merge_group_drafts([d1, d2], "dynamodb")
        names = [t["table_name"] for t in result["table_definitions"]]
        assert names == ["users", "orders"]

    def test_opensearch_index_deduplication(self):
        d1 = {"index_designs": [{"index_name": "idx1"}], "validation_passed": True}
        d2 = {
            "index_designs": [{"index_name": "idx1"}, {"index_name": "idx2"}],
            "validation_passed": True,
        }
        result = merge_group_drafts([d1, d2], "opensearch")
        names = [i["index_name"] for i in result["index_designs"]]
        assert names == ["idx1", "idx2"]

    def test_documentdb_collection_deduplication(self):
        # Contract field is ``collections`` (not ``collection_designs``).
        d1 = {"collections": [{"collection_name": "c1"}], "validation_passed": True}
        d2 = {
            "collections": [{"collection_name": "c1"}, {"collection_name": "c2"}],
            "validation_passed": True,
        }
        result = merge_group_drafts([d1, d2], "documentdb")
        names = [c["collection_name"] for c in result["collections"]]
        assert names == ["c1", "c2"]

    def test_documentdb_access_patterns_concatenated(self):
        d1 = {
            "collections": [{"collection_name": "c1"}],
            "access_patterns": [{"name": "a1"}],
            "validation_passed": True,
        }
        d2 = {
            "collections": [{"collection_name": "c2"}],
            "access_patterns": [{"name": "a2"}],
            "validation_passed": True,
        }
        result = merge_group_drafts([d1, d2], "documentdb")
        assert len(result["access_patterns"]) == 2
        assert len(result["collections"]) == 2

    def test_opensearch_data_stream_deduplication(self):
        # Contract dedup key is ``data_stream_name`` (not ``stream_name``).
        d1 = {"data_stream_designs": [{"data_stream_name": "ds1"}], "validation_passed": True}
        d2 = {
            "data_stream_designs": [
                {"data_stream_name": "ds1"},
                {"data_stream_name": "ds2"},
            ],
            "validation_passed": True,
        }
        result = merge_group_drafts([d1, d2], "opensearch")
        names = [d["data_stream_name"] for d in result["data_stream_designs"]]
        assert names == ["ds1", "ds2"]

    def test_opensearch_access_patterns_concatenated(self):
        d1 = {
            "index_designs": [{"index_name": "i1"}],
            "access_patterns": [{"name": "a1"}],
            "validation_passed": True,
        }
        d2 = {
            "index_designs": [{"index_name": "i2"}],
            "access_patterns": [{"name": "a2"}],
            "validation_passed": True,
        }
        result = merge_group_drafts([d1, d2], "opensearch")
        assert len(result["access_patterns"]) == 2
        assert len(result["index_designs"]) == 2

    def test_elasticache_key_designs_merge_and_dedupe(self):
        d1 = {
            "key_designs": [{"key_pattern": "k1"}],
            "access_patterns": [{"name": "a1"}],
            "validation_passed": True,
        }
        d2 = {
            "key_designs": [{"key_pattern": "k1"}, {"key_pattern": "k2"}],
            "access_patterns": [{"name": "a2"}],
            "validation_passed": True,
        }
        result = merge_group_drafts([d1, d2], "elasticache")
        patterns = [k["key_pattern"] for k in result["key_designs"]]
        assert patterns == ["k1", "k2"]
        assert len(result["access_patterns"]) == 2

    def test_trade_offs_deduplication(self):
        dup = {
            "description": "dup",
            "impact": "i",
            "source_tables": [],
            "target_tables": [],
            "query_ids": [],
            "engine": "dynamodb",
        }
        u1 = {
            "description": "unique1",
            "impact": "i",
            "source_tables": [],
            "target_tables": [],
            "query_ids": [],
            "engine": "dynamodb",
        }
        u2 = {
            "description": "unique2",
            "impact": "i",
            "source_tables": [],
            "target_tables": [],
            "query_ids": [],
            "engine": "dynamodb",
        }
        d1 = {"trade_offs": [dup, u1], "validation_passed": True}
        d2 = {"trade_offs": [dup, u2], "validation_passed": True}
        result = merge_group_drafts([d1, d2], "dynamodb")
        descriptions = [t["description"] for t in result["trade_offs"]]
        assert descriptions == ["dup", "unique1", "unique2"]

    def test_validation_passed_all_true(self):
        d1 = {"validation_passed": True}
        d2 = {"validation_passed": True}
        result = merge_group_drafts([d1, d2], "dynamodb")
        assert result["validation_passed"] is True

    def test_validation_passed_one_false(self):
        d1 = {"validation_passed": True}
        d2 = {"validation_passed": False}
        result = merge_group_drafts([d1, d2], "dynamodb")
        assert result["validation_passed"] is False

    def test_unknown_engine_no_list_fields(self):
        d1 = {"foo": "bar"}
        d2 = {"foo": "baz"}
        result = merge_group_drafts([d1, d2], "unknown_engine")
        assert result["validation_passed"] is True


# ---------------------------------------------------------------------------
# DynamoDB: several groups designing the same source table (issue #223)
# ---------------------------------------------------------------------------


def _key(name: str, type_: str = "S") -> dict:
    return {"attribute_name": name, "attribute_type": type_}


def _attr(name: str, source_table: str, column: str | None = None) -> dict:
    return {
        "name": name,
        "type": "S",
        "source_table": source_table,
        "source_column": column or name,
    }


def _entity(entity_type: str, source_table: str, pk: str, sk: str, *attrs: str) -> dict:
    return {
        "entity_type": entity_type,
        "source_table": source_table,
        "pk_template": pk,
        "sk_template": sk,
        "attributes": [_attr(a, source_table) for a in attrs or ("id",)],
    }


def _collection(name: str, pk: str, sk: str, *entities: dict, gsis=(), items=10) -> dict:
    sources = list(dict.fromkeys(e["source_table"] for e in entities))
    return {
        "table_name": name,
        "aggregate_pattern": "item_collection",
        "source_tables": sources,
        "partition_key": _key(pk, "N"),
        "sort_key": _key(sk),
        "entities": list(entities),
        "gsis": list(gsis),
        "item_count": items,
        "item_size_bytes": 100,
    }


def _single(name: str, source_table: str, pk: str, sk: str | None = None, items=10) -> dict:
    return {
        "table_name": name,
        "aggregate_pattern": "separate",
        "source_tables": [source_table],
        "partition_key": _key(pk, "N"),
        "sort_key": _key(sk) if sk else None,
        "attributes": [_attr(pk, source_table)],
        "gsis": [],
        "item_count": items,
        "item_size_bytes": 200,
    }


def _ap(pattern_id: str, table: str, *sources: str, gsi: str | None = None) -> dict:
    return {
        "pattern_id": pattern_id,
        "table_name": table,
        "gsi_name": gsi,
        "source_tables": list(sources),
        "query_ids": [f"q-{pattern_id}-{table}"],
    }


def _ddb(tables: list[dict], aps: list[dict], trade_offs: list[dict] | None = None) -> dict:
    return {
        "table_definitions": tables,
        "access_patterns": aps,
        "hot_partition_analysis": [{"table_name": t["table_name"]} for t in tables],
        "trade_offs": trade_offs or [],
        "validation_passed": True,
        "validation_failures": [],
    }


TERM = _entity("TERM", "wordpress.wp_terms", "{term_id}", "TERM", "term_id", "name")
TAXONOMY = _entity("TAXONOMY", "wordpress.wp_term_taxonomy", "{term_id}", "TAXONOMY#{taxonomy}")
TERM_META = _entity("TERM_META", "wordpress.wp_termmeta", "{term_id}", "META#{meta_key}")
BY_TAXONOMY = {
    "gsi_name": "TermsByTaxonomy",
    "partition_key": [_key("taxonomy")],
    "sort_key": [_key("term_name")],
    "projection": "ALL",
    "item_count": 30,
    "item_size_bytes": 100,
}


class TestDynamoDBOverlappingDesigns:
    """Merge rule: one DynamoDB home per source table, or a recorded reason."""

    def _run3_terms(self) -> list[dict]:
        """Run-3 shape: WpTerms (group 0) and wp_terms_taxonomy (group 1)."""
        g0 = _ddb(
            [_collection("WpTerms", "term_id", "record_key", TERM, TAXONOMY, TERM_META, items=30)],
            [_ap("DDB-AP-13", "WpTerms", "wordpress.wp_terms")],
        )
        taxonomy_with_name = {
            **TAXONOMY,
            "attributes": [*TAXONOMY["attributes"], _attr("term_name", "wordpress.wp_terms")],
        }
        g1 = _ddb(
            [
                _collection(
                    "wp_terms_taxonomy",
                    "term_id",
                    "record_key",
                    TERM,
                    taxonomy_with_name,
                    gsis=[BY_TAXONOMY],
                    items=30,
                )
            ],
            [_ap("DDB-AP-4", "wp_terms_taxonomy", "wordpress.wp_terms", gsi="TermsByTaxonomy")],
            [
                {
                    "description": "term name copied onto taxonomy items",
                    "impact": "i",
                    "source_tables": ["wordpress.wp_terms"],
                    "target_tables": ["wp_terms_taxonomy", "wp_terms_taxonomy.TermsByTaxonomy"],
                    "query_ids": [],
                    "engine": "dynamodb",
                }
            ],
        )
        return [g0, g1]

    def test_compatible_item_collections_merge_into_first_table(self):
        result = merge_group_drafts(self._run3_terms(), "dynamodb")

        [table] = result["table_definitions"]
        assert table["table_name"] == "WpTerms"
        assert [e["entity_type"] for e in table["entities"]] == ["TERM", "TAXONOMY", "TERM_META"]
        assert [g["gsi_name"] for g in table["gsis"]] == ["TermsByTaxonomy"]
        # The GSI sort key attribute from the absorbed design is kept on the entity.
        taxonomy = table["entities"][1]
        assert "term_name" in [a["name"] for a in taxonomy["attributes"]]
        assert table["source_tables"] == [
            "wordpress.wp_terms",
            "wordpress.wp_term_taxonomy",
            "wordpress.wp_termmeta",
        ]
        assert result["validation_passed"] is True
        assert result["validation_failures"] == []

    def test_merge_repoints_access_patterns_hot_partitions_and_trade_offs(self):
        result = merge_group_drafts(self._run3_terms(), "dynamodb")

        assert {ap["table_name"] for ap in result["access_patterns"]} == {"WpTerms"}
        assert {h["table_name"] for h in result["hot_partition_analysis"]} == {"WpTerms"}
        copied = next(t for t in result["trade_offs"] if t["description"].startswith("term name"))
        assert copied["target_tables"] == ["WpTerms", "WpTerms.TermsByTaxonomy"]

    def test_merge_records_a_trade_off(self):
        result = merge_group_drafts(self._run3_terms(), "dynamodb")

        merged = [t for t in result["trade_offs"] if "wp_terms_taxonomy" in t["description"]]
        assert len(merged) == 1
        assert merged[0]["target_tables"] == ["WpTerms"]
        assert "wordpress.wp_terms" in merged[0]["source_tables"]
        assert merged[0]["query_ids"] == ["q-DDB-AP-4-wp_terms_taxonomy"]
        assert merged[0]["engine"] == "dynamodb"
        assert "group 0" in merged[0]["description"] and "group 1" in merged[0]["description"]

    def test_item_count_adds_only_new_entity_types(self):
        g0, g1 = self._run3_terms()
        # The absorbed table holds TERM + TAXONOMY, both already in WpTerms: no new items.
        g1["table_definitions"][0]["item_count"] = 45
        g1["table_definitions"][0]["item_size_bytes"] = 50

        [table] = merge_group_drafts([g0, g1], "dynamodb")["table_definitions"]

        assert table["item_count"] == 30
        assert table["item_size_bytes"] == 100

    def test_item_count_sums_the_share_of_new_entity_types(self):
        g0 = _ddb([_collection("WpTerms", "term_id", "record_key", TERM, items=30)], [])
        g1 = _ddb(
            [_collection("Terms", "term_id", "record_key", TERM, TERM_META, items=60)],
            [],
        )
        g1["table_definitions"][0]["item_size_bytes"] = 400

        [table] = merge_group_drafts([g0, g1], "dynamodb")["table_definitions"]

        assert table["item_count"] == 60  # 30 + 60 x (1 new type / 2 types)
        assert table["item_size_bytes"] == 250  # (100 x 30 + 400 x 30) / 60

    def test_hot_partition_load_is_re_aggregated_for_the_merged_table(self):
        g0, g1 = self._run3_terms()

        def hot(table, load, qid):
            return {
                "table_name": table,
                "gsi_name": None,
                "operation": "read",
                "rcu_or_wcu_per_second": load,
                "partition_limit": 3000,
                "utilization_pct": round(load / 30, 1),
                "at_risk": False,
                "contributing_patterns": [qid],
                "mitigation": None,
            }

        g0["hot_partition_analysis"] = [hot("WpTerms", 1500, "q1")]
        g1["hot_partition_analysis"] = [hot("wp_terms_taxonomy", 1200, "q2")]

        [entry] = merge_group_drafts([g0, g1], "dynamodb")["hot_partition_analysis"]

        assert entry["table_name"] == "WpTerms"
        assert entry["rcu_or_wcu_per_second"] == 2700
        assert entry["utilization_pct"] == 90.0
        assert entry["at_risk"] is True and entry["mitigation"]
        assert entry["contributing_patterns"] == ["q1", "q2"]

    def _posts_drafts(self) -> list[dict]:
        posts = _entity("POST", "wp_posts", "{ID}", "POST")
        g0 = _ddb(
            [_collection("WpPosts", "post_id", "record_key", posts)],
            [_ap("DDB-AP-4", "WpPosts", "wp_posts")],
        )
        g1 = _ddb(
            [_single("wp_posts", "wp_posts", "id")], [_ap("DDB-AP-2", "wp_posts", "wp_posts")]
        )
        return [g0, g1]

    def test_independent_homes_from_different_groups_are_a_warning(self):
        result = merge_group_drafts(self._posts_drafts(), "dynamodb")

        assert [t["table_name"] for t in result["table_definitions"]] == ["WpPosts", "wp_posts"]
        assert result["validation_passed"] is True
        assert merge_failures(result) == []
        [warning] = merge_warnings(result)
        assert warning.startswith(OVERLAP_PREFIX)
        assert "modelled independently by design groups" in warning
        assert "WpPosts (group 0; PK post_id, SK record_key)" in warning
        assert "wp_posts (group 1; PK id)" in warning
        assert "choose one write path or keep copies in sync — review before migration" in warning
        [note] = [t for t in result["trade_offs"] if t["description"] == warning]
        assert note["target_tables"] == ["WpPosts", "wp_posts"]
        assert note["source_tables"] == ["wp_posts"]
        assert note["engine"] == "dynamodb"

    def test_trade_off_naming_every_target_justifies_separate_tables(self):
        g0, g1 = self._posts_drafts()
        g1["trade_offs"] = [
            {
                "description": "BatchGetItem by id needs its own table; writes update both",
                "impact": "i",
                "source_tables": ["wordpress.wp_posts"],
                "target_tables": ["WpPosts", "wp_posts"],
                "query_ids": [],
                "engine": "dynamodb",
            }
        ]

        result = merge_group_drafts([g0, g1], "dynamodb")

        assert len(result["table_definitions"]) == 2
        assert result["validation_passed"] is True
        assert merge_warnings(result) == []

    def test_trade_off_naming_only_some_targets_does_not_justify(self):
        g0, g1 = self._posts_drafts()
        g0["trade_offs"] = [
            {
                "description": "posts in one collection",
                "impact": "i",
                "source_tables": ["wp_posts"],
                "target_tables": ["WpPosts"],
            }
        ]

        result = merge_group_drafts([g0, g1], "dynamodb")

        assert result["validation_passed"] is True
        assert len(merge_warnings(result)) == 1

    def test_review_note_copied_into_a_draft_is_regenerated_not_trusted(self):
        first = merge_group_drafts(self._posts_drafts(), "dynamodb")
        g0, g1 = self._posts_drafts()
        g1["trade_offs"] = [
            t for t in first["trade_offs"] if t["description"].startswith(OVERLAP_PREFIX)
        ]

        result = merge_group_drafts([g0, g1], "dynamodb")

        assert merge_warnings(result) == merge_warnings(first)

    def test_denormalized_copies_are_neither_failure_nor_warning(self):
        rel = _entity("POST_TERM", "wordpress.wp_term_relationships", "{object_id}", "TERM#{id}")
        posts = _collection("WpPosts", "post_id", "record_key", rel)
        posts["source_tables"] += ["wordpress.wp_terms", "wordpress.wp_term_taxonomy"]
        g0 = _ddb([posts], [])
        g1 = _ddb([_collection("Terms", "term_id", "record_key", TERM, TAXONOMY)], [])

        result = merge_group_drafts([g0, g1], "dynamodb")

        assert result["validation_passed"] is True
        assert merge_failures(result) == [] and merge_warnings(result) == []

    def test_overlap_inside_one_group_is_neither_failure_nor_warning(self):
        """Run-3 group 0 alone: WpPosts (with denormalized terms) and WpTerms."""
        post = _entity("POST", "wordpress.wp_posts", "{ID}", "POST")
        post_meta = _entity("POST_META", "wordpress.wp_postmeta", "{post_id}", "META#{meta_key}")
        post_term = _entity(
            "POST_TERM", "wordpress.wp_term_relationships", "{object_id}", "TERM#{id}"
        )
        posts = _collection("WpPosts", "post_id", "record_key", post, post_meta, post_term)
        posts["source_tables"] += ["wordpress.wp_term_taxonomy", "wordpress.wp_terms"]
        draft = _ddb(
            [
                posts,
                _collection("WpTerms", "term_id", "record_key", TERM, TAXONOMY, TERM_META),
                _single("TermNames", "wordpress.wp_terms", "name"),  # same group, own entity
            ],
            [],
        )

        result = merge_group_drafts([draft], "dynamodb")

        assert result["validation_passed"] is True
        assert merge_failures(result) == [] and merge_warnings(result) == []

    def test_single_entity_tables_for_different_entities_are_not_merged(self):
        """Same key names but a different base table: merging would mix entities."""
        users = _single("Users", "wp_users", "id")
        posts = _single("Posts", "wp_posts", "id")
        posts["source_tables"].append("wp_users")  # denormalized author

        result = merge_group_drafts([_ddb([users], []), _ddb([posts], [])], "dynamodb")

        assert [t["table_name"] for t in result["table_definitions"]] == ["Users", "Posts"]
        assert merge_failures(result) == [] and merge_warnings(result) == []

    def test_single_entity_tables_for_the_same_entity_merge(self):
        a = _single("Users", "wp_users", "id")
        b = _single("wp_users", "wp_users", "id")
        b["attributes"].append(_attr("user_login", "wp_users"))

        result = merge_group_drafts([_ddb([a], []), _ddb([b], [_ap("X", "wp_users")])], "dynamodb")

        [table] = result["table_definitions"]
        assert table["table_name"] == "Users"
        assert [a["name"] for a in table["attributes"]] == ["id", "user_login"]
        assert result["access_patterns"][0]["table_name"] == "Users"
        assert result["validation_passed"] is True

    def test_conflicting_entity_definitions_are_not_merged(self):
        other_taxonomy = _entity("TAXONOMY", "wordpress.wp_term_taxonomy", "{term_id}", "TAX#{id}")
        g0 = _ddb([_collection("A", "term_id", "record_key", TERM, TAXONOMY)], [])
        g1 = _ddb([_collection("B", "term_id", "record_key", TERM, other_taxonomy)], [])

        result = merge_group_drafts([g0, g1], "dynamodb")

        assert len(result["table_definitions"]) == 2
        assert result["validation_passed"] is False
        [failure] = merge_failures(result)
        assert "entity TAXONOMY" in failure and "contradictory key templates" in failure
        assert "TAXONOMY#{taxonomy}" in failure and "TAX#{id}" in failure

    def test_contradictory_entity_inside_one_group_is_not_a_failure(self):
        other_taxonomy = _entity("TAXONOMY", "wordpress.wp_term_taxonomy", "{term_id}", "TAX#{id}")
        draft = _ddb(
            [
                _collection("A", "term_id", "record_key", TERM, TAXONOMY),
                _collection("B", "term_id", "record_key", TERM, other_taxonomy),
            ],
            [],
        )

        assert merge_group_drafts([draft], "dynamodb")["validation_passed"] is True

    def test_colliding_sort_key_prefixes_are_not_merged(self):
        post_meta = _entity("POST_META", "wp_postmeta", "{post_id}", "META#{meta_key}")
        term_meta = _entity("TERM_META", "wp_termmeta", "{term_id}", "META#{meta_key}")
        post = _entity("POST", "wp_posts", "{ID}", "POST")
        g0 = _ddb([_collection("A", "id", "sk", post, post_meta)], [])
        g1 = _ddb([_collection("B", "id", "sk", post, term_meta)], [])

        result = merge_group_drafts([g0, g1], "dynamodb")

        assert len(result["table_definitions"]) == 2

    def test_conflicting_gsi_definitions_are_not_merged(self):
        other_gsi = {**BY_TAXONOMY, "partition_key": [_key("slug")]}
        g0 = _ddb([_collection("A", "term_id", "record_key", TERM, gsis=[BY_TAXONOMY])], [])
        g1 = _ddb([_collection("B", "term_id", "record_key", TERM, gsis=[other_gsi])], [])

        result = merge_group_drafts([g0, g1], "dynamodb")

        assert len(result["table_definitions"]) == 2

    def test_same_table_name_with_different_designs_fails_validation(self):
        g0 = _ddb([_single("Main", "users", "id")], [])
        g1 = _ddb([_single("Main", "orders", "order_id")], [])

        result = merge_group_drafts([g0, g1], "dynamodb")

        assert [t["source_tables"] for t in result["table_definitions"]] == [["users"]]
        [failure] = merge_failures(result)
        assert "'Main'" in failure and "group 1" in failure

    def test_group_indices_label_messages(self):
        g0 = _ddb([_single("Main", "users", "id")], [])
        g1 = _ddb([_single("Main", "orders", "order_id")], [])

        result = merge_group_drafts([g0, g1], "dynamodb", group_indices=[2, 5])

        assert "group 5" in merge_failures(result)[0]

    def test_unknown_source_table_is_ignored(self):
        a = _single("A", "unknown", "id")
        b = _single("B", "unknown", "x")

        result = merge_group_drafts([_ddb([a], []), _ddb([b], [])], "dynamodb")

        assert result["validation_passed"] is True

    def test_stale_merge_failures_from_drafts_are_dropped(self):
        g0 = _ddb([_single("A", "users", "id")], [])
        g0["validation_failures"] = [f"{MERGE_FAILURE_PREFIX}old"]
        g0["validation_passed"] = False
        g1 = _ddb([_single("B", "orders", "id")], [])

        result = merge_group_drafts([g0, g1], "dynamodb")

        assert result["validation_failures"] == []
        assert result["validation_passed"] is True


class TestDynamoDBPatternIds:
    """Group-local DDB-AP-n IDs must stay unique after the merge (issue #229)."""

    def test_colliding_pattern_ids_are_renumbered_in_merged_order(self):
        g0 = _ddb([_single("A", "users", "id")], [_ap("DDB-AP-1", "A"), _ap("DDB-AP-2", "A")])
        g1 = _ddb([_single("B", "orders", "id")], [_ap("DDB-AP-1", "B")])

        result = merge_group_drafts([g0, g1], "dynamodb")

        assert [(ap["pattern_id"], ap["table_name"]) for ap in result["access_patterns"]] == [
            ("DDB-AP-1", "A"),
            ("DDB-AP-2", "A"),
            ("DDB-AP-3", "B"),
        ]

    def test_unique_pattern_ids_are_kept(self):
        g0 = _ddb([_single("A", "users", "id")], [_ap("DDB-AP-7", "A")])
        g1 = _ddb([_single("B", "orders", "id")], [_ap("DDB-AP-2", "B")])

        result = merge_group_drafts([g0, g1], "dynamodb")

        assert [ap["pattern_id"] for ap in result["access_patterns"]] == ["DDB-AP-7", "DDB-AP-2"]

    def test_single_draft_ids_are_not_touched(self):
        draft = _ddb([_single("A", "users", "id")], [_ap("DDB-AP-1", "A"), _ap("DDB-AP-1", "A")])

        result = merge_group_drafts([draft], "dynamodb")

        assert [ap["pattern_id"] for ap in result["access_patterns"]] == ["DDB-AP-1", "DDB-AP-1"]

    def test_renumbering_rewrites_references_in_the_drafts_text(self):
        g0 = _ddb([_single("A", "users", "id")], [_ap("DDB-AP-1", "A"), _ap("DDB-AP-2", "A")])
        g1 = _ddb([_single("B", "orders", "id")], [_ap("DDB-AP-1", "B"), _ap("DDB-AP-2", "B")])
        attr = {
            **_attr("total", "orders"),
            "denormalized": True,
            "justification": "Copied for DDB-AP-2 (and DDB-AP-1); DDB-AP-10 is unrelated",
        }
        g1["table_definitions"][0]["attributes"].append(attr)
        g1["trade_offs"] = [
            {"description": "DDB-AP-1 reads by id", "impact": "DDB-AP-2 too", "engine": "dynamodb"}
        ]
        g1["migration_notes"] = [{"object_name": "t", "application_logic_required": "DDB-AP-2"}]
        g1["hot_partition_analysis"] = [{"table_name": "B", "mitigation": "see DDB-AP-1"}]

        result = merge_group_drafts([g0, g1], "dynamodb")

        assert [ap["pattern_id"] for ap in result["access_patterns"]] == [
            "DDB-AP-1",
            "DDB-AP-2",
            "DDB-AP-3",
            "DDB-AP-4",
        ]
        orders = result["table_definitions"][1]
        assert orders["attributes"][1]["justification"] == (
            "Copied for DDB-AP-4 (and DDB-AP-3); DDB-AP-10 is unrelated"
        )
        trade_off = next(t for t in result["trade_offs"] if "reads by id" in t["description"])
        assert trade_off["description"] == "DDB-AP-3 reads by id"
        assert trade_off["impact"] == "DDB-AP-4 too"
        assert result["migration_notes"][0]["application_logic_required"] == "DDB-AP-4"
        assert result["hot_partition_analysis"][1]["mitigation"] == "see DDB-AP-3"
        # Group 0 keeps its own references.
        assert result["hot_partition_analysis"][0] == {"table_name": "A"}


class TestDynamoDBPerDraftRenames:
    def test_absorbed_name_is_re_pointed_only_in_its_own_draft(self):
        """Users (g0) absorbs Accounts (g1); g2's unrelated Accounts keeps its references."""
        users = _single("Users", "wp_users", "id")
        accounts_same_entity = _single("Accounts", "wp_users", "id")
        accounts_other = _single("Accounts", "billing_accounts", "account_id")
        g0 = _ddb([users], [_ap("DDB-AP-1", "Users")])
        g1 = _ddb([accounts_same_entity], [_ap("DDB-AP-2", "Accounts")])
        g2 = _ddb(
            [accounts_other],
            [_ap("DDB-AP-3", "Accounts")],
            [{"description": "billing", "impact": "i", "target_tables": ["Accounts"]}],
        )

        result = merge_group_drafts([g0, g1, g2], "dynamodb")

        assert [t["table_name"] for t in result["table_definitions"]] == ["Users", "Accounts"]
        assert result["table_definitions"][1]["source_tables"] == ["billing_accounts"]
        assert [(ap["pattern_id"], ap["table_name"]) for ap in result["access_patterns"]] == [
            ("DDB-AP-1", "Users"),
            ("DDB-AP-2", "Users"),
            ("DDB-AP-3", "Accounts"),
        ]
        # g1's entry is re-pointed to Users and aggregated with g0's; g2's stays on Accounts.
        assert [h["table_name"] for h in result["hot_partition_analysis"]] == ["Users", "Accounts"]
        billing = next(t for t in result["trade_offs"] if t["description"] == "billing")
        assert billing["target_tables"] == ["Accounts"]
        assert result["validation_passed"] is True


class TestMergeSchemaGroupsCarriesExcludedQueries:
    """#276/#369: a query --split left out of every group (no source table)
    must still be visible on the merged schema output, not silently dropped
    once drafts are merged."""

    BASE = "wordpress/job-001/schema-dynamodb/v1"

    def test_excluded_queries_appear_on_the_merged_output(self, tmp_path) -> None:
        store = LocalArtifactStore(str(tmp_path))
        store.write_json(
            f"{self.BASE}/groups_manifest.json",
            {
                "job_id": "job-001",
                "database_name": "wordpress",
                "target_engine": "dynamodb",
                "total_queries": 7,
                "total_groups": 1,
                "groups": [
                    {
                        "group_index": 0,
                        "group_name": "wp_posts",
                        "primary_tables": ["wordpress.wp_posts"],
                        "query_count": 6,
                        "table_count": 1,
                        "input_file": "input_group_0.json",
                        "input_pages": [],
                    }
                ],
                "excluded_queries": [
                    {"query_id": "q-catalog-0", "reason": "not designed: no source table"}
                ],
            },
        )
        store.write_json(
            f"{self.BASE}/schema_draft_group_0.json",
            {
                "table_definitions": [],
                "access_patterns": [],
                "unsupported_patterns": [],
                "migration_notes": [],
                "hot_partition_analysis": [],
                "trade_offs": [],
                "validation_failures": [],
            },
        )

        merged = merge_schema_groups("job-001", "wordpress", "dynamodb", store)

        assert merged["excluded_queries"] == [
            {"query_id": "q-catalog-0", "reason": "not designed: no source table"}
        ]
        written = store.read_json(f"{self.BASE}/schema_output.json")
        assert written["excluded_queries"] == merged["excluded_queries"]

    def test_no_excluded_queries_key_when_manifest_has_none(self, tmp_path) -> None:
        """No regression: a manifest without excluded_queries (the common
        case) must not add the key to the merged output at all."""
        store = LocalArtifactStore(str(tmp_path))
        store.write_json(
            f"{self.BASE}/groups_manifest.json",
            {
                "job_id": "job-001",
                "database_name": "wordpress",
                "target_engine": "dynamodb",
                "total_queries": 6,
                "total_groups": 1,
                "groups": [
                    {
                        "group_index": 0,
                        "group_name": "wp_posts",
                        "primary_tables": ["wordpress.wp_posts"],
                        "query_count": 6,
                        "table_count": 1,
                        "input_file": "input_group_0.json",
                        "input_pages": [],
                    }
                ],
            },
        )
        store.write_json(
            f"{self.BASE}/schema_draft_group_0.json",
            {
                "table_definitions": [],
                "access_patterns": [],
                "unsupported_patterns": [],
                "migration_notes": [],
                "hot_partition_analysis": [],
                "trade_offs": [],
                "validation_failures": [],
            },
        )

        merged = merge_schema_groups("job-001", "wordpress", "dynamodb", store)

        assert "excluded_queries" not in merged


class TestMergeSchemaGroupsSkipsWhenEveryQueryIsPseudoOnly:
    """#276/#369 (reviewer suggestion 1): when every assigned query touches
    no source table, --split's manifest has zero groups and only
    excluded_queries. Merging that must write a skipped schema_output.json,
    not raise "No group drafts found" -- there is nothing wrong, there is
    just nothing to design."""

    BASE = "discourse/job-001/schema-dynamodb/v1"

    def test_writes_a_skipped_output_carrying_excluded_queries(self, tmp_path) -> None:
        store = LocalArtifactStore(str(tmp_path))
        excluded = [
            {"query_id": "q-catalog-0", "reason": "not designed: no source table"},
            {"query_id": "q-catalog-1", "reason": "not designed: no source table"},
        ]
        store.write_json(
            f"{self.BASE}/groups_manifest.json",
            {
                "job_id": "job-001",
                "database_name": "discourse",
                "target_engine": "dynamodb",
                "total_queries": 2,
                "total_groups": 0,
                "groups": [],
                "excluded_queries": excluded,
            },
        )

        merged = merge_schema_groups("job-001", "discourse", "dynamodb", store)

        assert merged["status"] == "skipped"
        assert merged["target_type"] == "dynamodb"
        assert merged["excluded_queries"] == excluded
        written = store.read_json(f"{self.BASE}/schema_output.json")
        assert written == merged

    def test_still_raises_when_zero_groups_and_no_excluded_queries(self, tmp_path) -> None:
        """A manifest with zero groups and nothing excluded either is a
        different, genuine failure (e.g. a malformed manifest) -- it must
        keep raising, not silently write an empty skip."""
        store = LocalArtifactStore(str(tmp_path))
        store.write_json(
            f"{self.BASE}/groups_manifest.json",
            {
                "job_id": "job-001",
                "database_name": "discourse",
                "target_engine": "dynamodb",
                "total_queries": 0,
                "total_groups": 0,
                "groups": [],
            },
        )

        with pytest.raises(ValueError, match="No group drafts found"):
            merge_schema_groups("job-001", "discourse", "dynamodb", store)
