"""Tests for DynamoDB-specific load test models."""
from src.agents.load_test.dynamodb.models import DynamoDBTableSeedInfo


def test_table_seed_info_pk_only():
    info = DynamoDBTableSeedInfo(
        table_name="LoadTest_WpOptions",
        pk_attr="option_name",
        pk_type="S",
        pk_count=1000,
        pk_pad_width=4,
        sk_attr=None,
        sk_type=None,
        sk_count=None,
        sk_pad_width=None,
        items_seeded=1000,
    )
    assert info.table_name == "LoadTest_WpOptions"
    assert info.sk_attr is None


def test_table_seed_info_with_sk():
    info = DynamoDBTableSeedInfo(
        table_name="LoadTest_WpPostMeta",
        pk_attr="post_id",
        pk_type="N",
        pk_count=500,
        pk_pad_width=None,
        sk_attr="meta_key_id",
        sk_type="S",
        sk_count=20,
        sk_pad_width=4,
        items_seeded=10000,
    )
    assert info.pk_type == "N"
    assert info.pk_pad_width is None
    assert info.sk_count == 20


def test_table_seed_info_numeric_pk_no_padding():
    info = DynamoDBTableSeedInfo(
        table_name="LoadTest_WpPosts",
        pk_attr="ID",
        pk_type="N",
        pk_count=1000,
        pk_pad_width=None,
        sk_attr=None,
        sk_type=None,
        sk_count=None,
        sk_pad_width=None,
        items_seeded=1000,
    )
    assert info.pk_pad_width is None
