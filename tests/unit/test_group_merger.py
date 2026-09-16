"""Unit tests for the schema design group merger."""

import pytest

from src.agents.schema_design.group_merger import merge_group_drafts


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
