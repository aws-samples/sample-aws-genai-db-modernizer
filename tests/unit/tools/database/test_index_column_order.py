# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""An index keeps its column order, and prefix lengths, from a collection (#1927).

MySQL's ``JSON_ARRAYAGG`` does not keep the collector subquery's ``ORDER BY`` (#1865), so a
collection can list an index's columns in another order than the index's. Every collector
writes each key's position; the parser sorts on it and keeps ``columns`` and
``prefix_lengths`` aligned.
"""

from __future__ import annotations

import json
import zipfile
from collections import defaultdict
from pathlib import Path

import pytest

from src.tools.database.offline_parser import parse_offline_collection

_ROOT = Path(__file__).resolve().parents[4]

_KEYS = [("tenant_id", None), ("region", 16), ("account_id", None)]  # (column, prefix)


def _collection(engine: str, *, position_key: str | None = "seq_in_index") -> dict:
    rows = []
    for position, (column, prefix) in enumerate(_KEYS, start=1):
        row: dict = {
            "table_name": "orders",
            "index_name": "idx_tenant_region_account",
            "column_name": column,
            "non_unique": 1,
            "index_type": "btree" if engine != "mysql" else "BTREE",
            "sub_part": prefix,
        }
        if position_key:
            row[position_key] = position
        rows.append(row)
    return {
        "metadata": {"database_name": "db", "engine": engine},
        "tables": [{"table_name": "orders", "row_count": 10}],
        "columns": [],
        "indexes": [rows[2], rows[0], rows[1]],  # JSON_ARRAYAGG's order, not the index's
        "foreign_keys": [],
        "primary_keys": [],
    }


@pytest.mark.parametrize("engine", ["mysql", "postgresql"])
def test_a_shuffled_composite_index_comes_out_in_key_order(engine):
    [table] = parse_offline_collection(_collection(engine))["tables"]

    [index] = table["indexes"]
    assert index["columns"] == [c for c, _ in _KEYS]


def test_prefix_lengths_stay_aligned_with_their_columns():
    [table] = parse_offline_collection(_collection("mysql"))["tables"]

    [index] = table["indexes"]
    assert list(zip(index["columns"], index["prefix_lengths"], strict=True)) == _KEYS


def test_sql_server_key_ordinal_is_the_position_and_included_columns_follow():
    data = _collection("sqlserver", position_key="key_ordinal")
    data["indexes"].append({**data["indexes"][0], "column_name": "note", "key_ordinal": 0})
    data["indexes"].insert(0, {**data["indexes"][0], "column_name": "note2", "key_ordinal": 0})

    [table] = parse_offline_collection(data)["tables"]

    [index] = table["indexes"]
    # #2227: the included columns are stored, not keyed: they leave the key list
    assert index["columns"] == ["tenant_id", "region", "account_id"]
    assert index["included_columns"] == ["note2", "note"]


def test_a_postgresql_position_counting_from_zero_keeps_the_first_column_first():
    data = _collection("postgresql")
    for row in data["indexes"]:
        row["seq_in_index"] -= 1  # array_position on indkey starts at 0

    [table] = parse_offline_collection(data)["tables"]

    [index] = table["indexes"]
    assert index["columns"] == [c for c, _ in _KEYS]


@pytest.mark.passes_on_main  # guards the order main already keeps for old collections
def test_an_old_collection_without_positions_keeps_its_row_order():
    data = _collection("mysql", position_key=None)

    [table] = parse_offline_collection(data)["tables"]

    [index] = table["indexes"]
    assert index["columns"] == ["account_id", "tenant_id", "region"]


def test_two_indexes_are_sorted_each_on_its_own_positions():
    data = _collection("mysql")
    other = [
        {"table_name": "orders", "index_name": "idx_other", "column_name": c, "seq_in_index": n}
        | {"non_unique": 1}
        for c, n in [("b", 2), ("a", 1)]
    ]
    data["indexes"] = other[:1] + data["indexes"] + other[1:]

    [table] = parse_offline_collection(data)["tables"]

    by_name = {i["index_name"]: i for i in table["indexes"]}
    assert by_name["idx_other"]["columns"] == ["a", "b"]
    assert by_name["idx_tenant_region_account"]["columns"] == [c for c, _ in _KEYS]


def _wordpress_collection() -> dict:
    with zipfile.ZipFile(_ROOT / "docs/examples/wordpress/wordpress.zip") as z:
        text = z.read("wordpress-collection.json").decode()
    collection: dict = json.loads(text.split("\n", 1)[1])  # the first line is a header
    return collection


def test_the_wordpress_samples_composite_indexes_come_out_in_key_order():
    raw = _wordpress_collection()
    expected = defaultdict(list)
    for row in sorted(raw["indexes"], key=lambda r: r["seq_in_index"]):
        expected[(row["table_name"], row["index_name"])].append(row["column_name"])
    composite = {key: cols for key, cols in expected.items() if len(cols) > 1}

    tables = {t["table_name"]: t for t in parse_offline_collection(raw)["tables"]}

    got = {
        (name, index["index_name"]): index["columns"]
        for name, table in tables.items()
        for index in table["indexes"]
        if len(index["columns"]) > 1
    }
    assert got == composite
    assert len(composite) == 24
    assert got[("wp_posts", "type_status_date")] == [
        "post_type",
        "post_status",
        "post_date",
        "ID",
    ]
    reordered = [
        key
        for key, cols in composite.items()
        if cols
        != [r["column_name"] for r in raw["indexes"] if (r["table_name"], r["index_name"]) == key]
    ]
    assert len(reordered) == 12


def _index_section(script: str) -> str:
    text = (_ROOT / "scripts" / script).read_text()
    start = text.index("Indexes")
    return text[start : text.index("Foreign Keys", start)]


@pytest.mark.passes_on_main  # guards positions these scripts already write
@pytest.mark.parametrize(
    ("script", "marker"),
    [
        ("collect-mysql.sql", "'seq_in_index', seq_in_index"),
        ("collect-postgresql.sql", "AS seq_in_index"),
        ("collect-sqlserver.sql", "ic.key_ordinal"),
        ("collect-mysql.sql", "'sub_part', sub_part"),
    ],
)
def test_the_collection_scripts_keep_writing_each_index_keys_position(script, marker):
    assert marker in _index_section(script)


def test_the_oracle_script_writes_each_index_keys_position():
    assert "'seq_in_index' VALUE ic.COLUMN_POSITION" in _index_section("collect-oracle.sql")
