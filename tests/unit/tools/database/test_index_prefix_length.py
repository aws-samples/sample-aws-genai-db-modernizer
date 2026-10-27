"""An index's prefix length (MySQL ``SUB_PART``) reaches the collected contract (#1623)."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.agents.collector.mysql_collector import _build_tables
from src.contracts.collector_output import Index
from src.tools.database.mysql_tools import MySQLRemoteCollector
from src.tools.database.offline_parser import parse_offline_collection

REPO = Path(__file__).resolve().parents[3].parent


def _collection(index_rows: list[dict]) -> dict:
    return {
        "metadata": {"database_name": "blog", "engine": "mysql"},
        "tables": [{"table_name": "posts", "row_count": 1}],
        "columns": [
            {"table_name": "posts", "column_name": "id", "data_type": "bigint",
             "column_type": "bigint", "is_nullable": "NO", "ordinal_position": 1},
            {"table_name": "posts", "column_name": "body", "data_type": "text",
             "column_type": "text", "is_nullable": "YES", "ordinal_position": 2},
        ],  # fmt: skip
        "indexes": index_rows,
        "queries": [],
    }


def _row(name: str, col: str, seq: int, **extra) -> dict:
    return {"table_name": "posts", "index_name": name, "column_name": col,
            "seq_in_index": seq, "non_unique": 1, "index_type": "BTREE", **extra}  # fmt: skip


def test_the_mysql_collection_script_selects_sub_part():
    sql = (REPO / "scripts" / "collect-mysql.sql").read_text().lower()
    assert "'sub_part', sub_part" in sql


def test_offline_parser_keeps_prefix_lengths_per_column():
    parsed = parse_offline_collection(
        _collection([_row("ix", "id", 1, sub_part=None), _row("ix", "body", 2, sub_part=100)])
    )
    (index,) = parsed["tables"][0]["indexes"]
    assert index["prefix_lengths"] == [None, 100]


@pytest.mark.passes_on_main
def test_offline_parser_writes_no_prefix_key_for_an_old_collection():
    parsed = parse_offline_collection(_collection([_row("ix", "body", 1)]))
    (index,) = parsed["tables"][0]["indexes"]
    assert "prefix_lengths" not in index or index["prefix_lengths"] is None


def test_old_saved_collection_without_the_field_still_loads():
    old = {"index_name": "ix", "columns": ["body"], "is_unique": False}
    assert Index.model_validate(old).prefix_lengths is None
    parsed = parse_offline_collection(_collection([_row("ix", "body", 1)]))
    (table,) = _build_tables(parsed["tables"], "blog")
    assert table.indexes is not None and table.indexes[0].prefix_lengths is None


def test_prefix_lengths_reach_the_contract_index():
    parsed = parse_offline_collection(_collection([_row("ix", "body", 1, sub_part=100)]))
    (table,) = _build_tables(parsed["tables"], "blog")
    assert table.indexes is not None and table.indexes[0].prefix_lengths == [100]


def test_live_collector_keeps_prefix_lengths():
    collector = MySQLRemoteCollector.__new__(MySQLRemoteCollector)
    collector._statistics_expression = False
    rows = [
        {"index_name": "ix", "column_name": "id", "seq_in_index": 1, "non_unique": 1,
         "index_type": "BTREE", "sub_part": None},
        {"index_name": "ix", "column_name": "body", "seq_in_index": 2, "non_unique": 1,
         "index_type": "BTREE", "sub_part": 100},
    ]  # fmt: skip
    collector._query = lambda sql: rows  # type: ignore[method-assign]
    (index,) = collector.collect_indexes("posts")
    assert index["prefix_lengths"] == [None, 100]


def test_ddl_upload_parses_key_prefixes_including_multi_column_lists():
    from src.tools.database.ddl_parser import parse_ddl

    tables = parse_ddl(
        "CREATE TABLE posts (\n"
        "  id bigint NOT NULL,\n"
        "  slug varchar(200),\n"
        "  body text,\n"
        "  PRIMARY KEY (id),\n"
        "  UNIQUE KEY u_slug (slug(100)),\n"
        "  KEY ix (slug(10), body(50)),\n"
        "  KEY plain (id, slug)\n"
        ");",
        "blog",
    )
    by_name = {i["index_name"]: i for i in tables[0]["indexes"]}
    assert by_name["u_slug"]["columns"] == ["slug"] and by_name["u_slug"]["prefix_lengths"] == [100]
    assert by_name["ix"]["columns"] == ["slug", "body"]
    assert by_name["ix"]["prefix_lengths"] == [10, 50]
    assert (
        by_name["plain"]["columns"] == ["id", "slug"] and "prefix_lengths" not in by_name["plain"]
    )


def test_prefix_lengths_stay_aligned_with_columns_around_an_expression_key():
    collector = MySQLRemoteCollector.__new__(MySQLRemoteCollector)
    collector._statistics_expression = True
    rows = [
        {"index_name": "ix", "column_name": "slug", "seq_in_index": 1, "non_unique": 1,
         "index_type": "BTREE", "sub_part": 10, "expression": None},
        {"index_name": "ix", "column_name": None, "seq_in_index": 2, "non_unique": 1,
         "index_type": "BTREE", "sub_part": None, "expression": "lower(`name`)"},
        {"index_name": "ix", "column_name": "body", "seq_in_index": 3, "non_unique": 1,
         "index_type": "BTREE", "sub_part": 50, "expression": None},
    ]  # fmt: skip
    collector._query = lambda sql: rows  # type: ignore[method-assign]
    (index,) = collector.collect_indexes("posts")
    assert index["columns"] == ["slug", "body"] and index["prefix_lengths"] == [10, 50]
    assert index["has_expressions"] is True


def test_ddl_upload_keeps_prefixes_next_to_an_expression_key():
    from src.tools.database.ddl_parser import parse_ddl

    (table,) = parse_ddl(
        "CREATE TABLE posts (id bigint NOT NULL, slug varchar(200), name text, body text,\n"
        "  KEY ix (slug(10), (lower(name)), body(50)));",
        "blog",
    )
    (index,) = table["indexes"]
    assert index["columns"] == ["slug", "body"] and index["prefix_lengths"] == [10, 50]
    assert index["has_expressions"] is True
