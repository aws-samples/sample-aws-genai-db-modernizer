"""Tests for DynamoDB contract-aware seeder."""

from unittest.mock import MagicMock, patch

import pytest

from src.agents.load_test.dynamodb.seeder import DynamoDBSeeder


@pytest.fixture
def table_def_numeric_pk():
    return {
        "table_name": "WpPosts",
        "partition_key": {"attribute_name": "ID", "attribute_type": "N"},
        "sort_key": None,
        "item_count": 5000,
    }


@pytest.fixture
def table_def_string_pk():
    return {
        "table_name": "WpOptions",
        "partition_key": {"attribute_name": "option_name", "attribute_type": "S"},
        "sort_key": None,
        "item_count": 1000,
    }


@pytest.fixture
def table_def_numeric_pk_string_sk():
    return {
        "table_name": "WpPostMeta",
        "partition_key": {"attribute_name": "post_id", "attribute_type": "N"},
        "sort_key": {"attribute_name": "meta_key_id", "attribute_type": "S"},
        "item_count": 50000,
    }


class TestDynamoDBSeeder:
    def test_generates_numeric_pk_items(self, table_def_numeric_pk):
        seeder = DynamoDBSeeder(region="us-east-1")
        items = seeder._generate_items(table_def_numeric_pk, max_items=10)

        assert len(items) == 10
        assert items[0]["ID"] == 1
        assert items[9]["ID"] == 10
        assert isinstance(items[0]["ID"], int)

    def test_generates_string_pk_with_padding(self, table_def_string_pk):
        seeder = DynamoDBSeeder(region="us-east-1")
        items = seeder._generate_items(table_def_string_pk, max_items=10)

        assert len(items) == 10
        assert items[0]["option_name"] == "0001"
        assert items[9]["option_name"] == "0010"
        assert isinstance(items[0]["option_name"], str)

    def test_generates_pk_and_sk_items(self, table_def_numeric_pk_string_sk):
        seeder = DynamoDBSeeder(region="us-east-1")
        items = seeder._generate_items(table_def_numeric_pk_string_sk, max_items=100)

        assert len(items) == 100
        assert isinstance(items[0]["post_id"], int)
        assert isinstance(items[0]["meta_key_id"], str)
        # String SK should be zero-padded
        assert items[0]["meta_key_id"].startswith("0")

    @patch("boto3.resource")
    def test_seed_returns_manifest(self, mock_resource, table_def_numeric_pk):
        mock_table = MagicMock()
        mock_resource.return_value.Table.return_value = mock_table
        mock_batch_writer = MagicMock()
        mock_table.batch_writer.return_value.__enter__ = MagicMock(return_value=mock_batch_writer)
        mock_table.batch_writer.return_value.__exit__ = MagicMock(return_value=False)

        seeder = DynamoDBSeeder(region="us-east-1")
        schema_output = {
            "table_definitions": [table_def_numeric_pk],
            "access_patterns": [{"table_name": "WpPosts", "in_scope": True, "design_rps": 100}],
        }
        manifest = seeder.seed(schema_output, max_items_per_table=100)

        assert manifest.total_items == 100
        assert "WpPosts" in manifest.resources
        seed_info = manifest.resources["WpPosts"]
        assert seed_info["pk_attr"] == "ID"
        assert seed_info["pk_type"] == "N"
        assert seed_info["pk_count"] == 100
        assert seed_info["pk_pad_width"] is None

    @patch("boto3.resource")
    def test_caps_at_max_items(self, mock_resource, table_def_numeric_pk_string_sk):
        mock_table = MagicMock()
        mock_resource.return_value.Table.return_value = mock_table
        mock_batch_writer = MagicMock()
        mock_table.batch_writer.return_value.__enter__ = MagicMock(return_value=mock_batch_writer)
        mock_table.batch_writer.return_value.__exit__ = MagicMock(return_value=False)

        seeder = DynamoDBSeeder(region="us-east-1")
        schema_output = {
            "table_definitions": [table_def_numeric_pk_string_sk],
            "access_patterns": [{"table_name": "WpPostMeta", "in_scope": True, "design_rps": 200}],
        }
        manifest = seeder.seed(schema_output, max_items_per_table=500)
        seed_info = manifest.resources["WpPostMeta"]
        assert seed_info["items_seeded"] <= 500
