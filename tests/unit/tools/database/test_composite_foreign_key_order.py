# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A composite foreign key keeps the constraint's column order from a collection (#1868).

MySQL's ``JSON_ARRAYAGG`` does not keep the collector subquery's ``ORDER BY`` (#1865), so
a collection can list a foreign key's columns in another order than the constraint's.
Every collector writes the column's position in the constraint; the parser sorts on it and
keeps ``columns`` and ``referenced_columns`` aligned pair by pair.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.tools.database.offline_parser import parse_offline_collection

_ROOT = Path(__file__).resolve().parents[4]

# Constraint order (child column -> parent column) and the position of each pair.
_PAIRS = [("tenant_id", "t_id"), ("region", "reg"), ("account_id", "acct")]
_CHILD_ORDER = [c for c, _ in _PAIRS]
_PARENT_ORDER = [p for _, p in _PAIRS]


def _collection(engine: str, *, position_key: str | None = "ordinal_position") -> dict:
    rows = []
    for position, (child, parent) in enumerate(_PAIRS, start=1):
        row: dict = {
            "table_name": "orders",
            "constraint_name": "fk_orders_account",
            "column_name": child,
            "referenced_table_name": "accounts",
            "referenced_column_name": parent,
            "on_delete": "CASCADE",
            "on_update": "NO ACTION",
        }
        if position_key:
            row[position_key] = position
        rows.append(row)
    shuffled = [rows[2], rows[0], rows[1]]  # JSON_ARRAYAGG's order: not the constraint's
    return {
        "metadata": {"database_name": "db", "engine": engine},
        "tables": [{"table_name": "orders", "row_count": 10}],
        "columns": [],
        "indexes": [],
        "primary_keys": [],
        "foreign_keys": shuffled,
    }


@pytest.mark.parametrize("engine", ["mysql", "postgresql"])
def test_a_shuffled_composite_foreign_key_comes_out_in_constraint_order(engine):
    [table] = parse_offline_collection(_collection(engine))["tables"]

    [fk] = table["foreign_keys"]
    assert fk["columns"] == _CHILD_ORDER
    assert fk["referenced_columns"] == _PARENT_ORDER


@pytest.mark.parametrize("engine", ["mysql", "postgresql"])
def test_every_pair_stays_aligned(engine):
    [table] = parse_offline_collection(_collection(engine))["tables"]

    [fk] = table["foreign_keys"]
    assert list(zip(fk["columns"], fk["referenced_columns"], strict=True)) == _PAIRS


def test_sql_server_constraint_column_id_is_the_position():
    data = _collection("sqlserver", position_key="constraint_column_id")

    [table] = parse_offline_collection(data)["tables"]

    [fk] = table["foreign_keys"]
    assert list(zip(fk["columns"], fk["referenced_columns"], strict=True)) == _PAIRS


@pytest.mark.passes_on_main  # guards the order main already keeps for old collections
def test_an_old_collection_without_positions_keeps_its_row_order():
    data = _collection("mysql", position_key=None)

    [table] = parse_offline_collection(data)["tables"]

    [fk] = table["foreign_keys"]
    assert list(zip(fk["columns"], fk["referenced_columns"], strict=True)) == [
        _PAIRS[2],
        _PAIRS[0],
        _PAIRS[1],
    ]


def test_two_constraints_are_sorted_each_on_its_own_positions():
    data = _collection("mysql")
    other = [
        {
            "table_name": "orders",
            "constraint_name": "fk_orders_other",
            "column_name": col,
            "referenced_table_name": "other",
            "referenced_column_name": ref,
            "ordinal_position": pos,
        }
        for col, ref, pos in [("b", "y", 2), ("a", "x", 1)]
    ]
    data["foreign_keys"] = other[:1] + data["foreign_keys"] + other[1:]

    [table] = parse_offline_collection(data)["tables"]

    by_name = {fk["constraint_name"]: fk for fk in table["foreign_keys"]}
    assert by_name["fk_orders_other"]["columns"] == ["a", "b"]
    assert by_name["fk_orders_other"]["referenced_columns"] == ["x", "y"]
    assert by_name["fk_orders_account"]["columns"] == _CHILD_ORDER


def test_the_oracle_offline_script_writes_the_position():
    script = (_ROOT / "scripts/collect-oracle.sql").read_text()
    section = script.split("6. Foreign Keys", 1)[1].split("7.", 1)[0]

    assert "'ordinal_position' VALUE ac.POSITION" in section


@pytest.mark.parametrize("which", ["offline script", "live collector"])
def test_postgresql_reads_the_constraint_columns_with_their_ordinality(which):
    script = (_ROOT / "scripts/collect-postgresql.sql").read_text()
    offline = script.split("5. Foreign Keys", 1)[1].split("6. Primary Keys", 1)[0]
    source = (_ROOT / "src/tools/database/postgres_tools.py").read_text()
    live = source.split("def collect_foreign_keys", 1)[1].split("def collect_primary_key", 1)[0]
    query = {"offline script": offline, "live collector": live}[which]

    # The real-server cases are in test_postgres_foreign_keys_live_server.py.
    assert "unnest(c.conkey, c.confkey)" in query
    assert "WITH ORDINALITY" in query
    assert "c.contype = 'f'" in query
    assert "c.conparentid = 0" in query
