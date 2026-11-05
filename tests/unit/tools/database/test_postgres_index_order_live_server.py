# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""PostgreSQL index keys, read from a real server (#1927).

The index query of ``scripts/collect-postgresql.sql`` runs against a PostgreSQL started by
``pgserver`` on a Unix socket in a temp folder (no TCP listener). A composite index whose
key order differs from the column order must come out in key order after the rows are
shuffled, as the parser sees them. The tests skip when ``pgserver`` is not installed or
cannot start.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.tools.database.offline_parser import parse_offline_collection

pgserver = pytest.importorskip("pgserver")

_ROOT = Path(__file__).resolve().parents[4]

_SCHEMA = """
CREATE TABLE orders (a int, b int, c int, d int);
CREATE INDEX idx_cab ON orders (c, a, b);
CREATE UNIQUE INDEX idx_dcb_inc ON orders (d, c, b) INCLUDE (a);
CREATE INDEX idx_single ON orders (b);
"""


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    try:
        srv = pgserver.get_server(tmp_path_factory.mktemp("pgdata"), cleanup_mode="delete")
    except Exception as exc:  # no bundled server for this platform, or it cannot start
        pytest.skip(f"pgserver cannot start a PostgreSQL here: {exc}")
    srv.psql("\\set ON_ERROR_STOP on\n" + _SCHEMA)  # a bad schema fails, never skips
    yield srv
    srv.cleanup()


def _index_rows(server) -> list[dict]:
    script = (_ROOT / "scripts/collect-postgresql.sql").read_text()
    cte = script.split("indexes_data AS (", 1)[1].split("\n),\n", 1)[0]
    sql = (
        f"WITH indexes_data AS ({cte}\n) "
        "SELECT coalesce(jsonb_agg(e), '[]') FROM indexes_data, "
        "jsonb_array_elements(data::jsonb) e"
    )
    out = server.psql(f"\\set QUIET on\n\\t on\n\\a\n{sql};\n")
    rows: list[dict] = json.loads([ln for ln in out.splitlines() if ln.strip()][-1])
    return rows


def test_the_script_writes_a_position_for_every_key(server):
    rows = [r for r in _index_rows(server) if r["index_name"] == "idx_cab"]

    by_position = sorted(rows, key=lambda r: r["seq_in_index"])
    assert [r["column_name"] for r in by_position] == ["c", "a", "b"]
    assert len({r["seq_in_index"] for r in rows}) == 3


def test_the_parser_gives_the_key_order_from_shuffled_real_rows(server):
    rows = _index_rows(server)
    data = {
        "metadata": {"database_name": "db", "engine": "postgresql"},
        "tables": [{"table_name": "orders", "row_count": 1}],
        "columns": [],
        "indexes": rows[::-1],
        "foreign_keys": [],
        "primary_keys": [],
    }

    [table] = parse_offline_collection(data)["tables"]

    by_name = {i["index_name"]: i["columns"] for i in table["indexes"]}
    assert by_name["idx_cab"] == ["c", "a", "b"]
    assert by_name["idx_dcb_inc"] == ["d", "c", "b", "a"]  # the INCLUDE column follows the keys
    assert by_name["idx_single"] == ["b"]
