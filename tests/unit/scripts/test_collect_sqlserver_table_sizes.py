# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""SQL Server's ``index_size_mb`` sums the nonclustered indexes' pages (#1873).

Both table queries joined ``sys.partitions`` for the heap or clustered index only
(``index_id IN (0, 1)``) and then summed pages ``WHERE index_id > 1``, which no
joined row could match: every table read 0. There is no SQL Server here, so each
query (the offline script's and the live collector's, as written) is parsed as T-SQL
with sqlglot and run on SQLite over a small synthetic catalog (an attached ``sys``
database), with rewrites that change no figure: ``DECIMAL(p, s)`` reads as
``ROUND(x, s)``, ``DB_ID()`` as this database's id, and the ``db[_]%`` schema filter
(no ``db_`` schema here) as true.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import sqlglot
from sqlglot import exp

from src.tools.database.sqlserver_tools import SQLServerRemoteCollector

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "collect-sqlserver.sql"
_DB_ID = 5


def _partition(object_id: int, index_id: int, partition_id: int, rows: int) -> dict[str, Any]:
    return {
        "object_id": object_id,
        "index_id": index_id,
        "partition_id": partition_id,
        "rows": rows,
    }


def _unit(container_id: int, pages: int) -> dict[str, Any]:
    return {"container_id": container_id, "total_pages": pages}


def _usage(object_id: int, index_id: int, scans: int, seeks: int, lookups: int) -> dict[str, Any]:
    return {
        "object_id": object_id,
        "index_id": index_id,
        "database_id": _DB_ID,
        "user_scans": scans,
        "user_seeks": seeks,
        "user_lookups": lookups,
    }


# 128 pages of 8 KB are 1 MB.
# dbo.Orders: clustered (2 partitions, one with a LOB unit) + nonclustered 2 (2 partitions)
#   and 3; data 128 + 128 + 128 = 384 pages (3 MB), indexes 64 + 64 + 128 = 256 (2 MB).
# dbo.Log: a heap of 512 pages (4 MB) with one 32-page nonclustered index (0.25 MB).
# dbo.Bare: a clustered index of 64 pages (0.5 MB), no other index.
CATALOG: dict[str, Any] = {
    "sys": {
        "schemas": [{"schema_id": 1, "name": "dbo"}],
        "tables": [
            {"object_id": 10, "schema_id": 1, "name": "Orders"},
            {"object_id": 20, "schema_id": 1, "name": "Log"},
            {"object_id": 30, "schema_id": 1, "name": "Bare"},
        ],
        "indexes": [
            {"object_id": 10, "index_id": 1},
            {"object_id": 10, "index_id": 2},
            {"object_id": 10, "index_id": 3},
            {"object_id": 20, "index_id": 0},
            {"object_id": 20, "index_id": 2},
            {"object_id": 30, "index_id": 1},
        ],
        "partitions": [
            _partition(10, 1, 100, 50),
            _partition(10, 1, 101, 30),
            # A nonclustered index's partitions hold the same rows: never a second count.
            _partition(10, 2, 102, 50),
            _partition(10, 2, 103, 30),
            _partition(10, 3, 104, 80),
            _partition(20, 0, 200, 1000),
            _partition(20, 2, 201, 1000),
            _partition(30, 1, 300, 7),
        ],
        "allocation_units": [
            _unit(100, 128),
            _unit(100, 128),  # LOB_DATA of partition 100
            _unit(101, 128),
            _unit(102, 64),
            _unit(103, 64),
            _unit(104, 128),
            _unit(200, 512),
            _unit(201, 32),
            _unit(300, 64),
        ],
        # Three of dbo.Orders' indexes and one of dbo.Log's have been used.
        "dm_db_index_usage_stats": [
            _usage(10, 1, 4, 10, 2),
            _usage(10, 2, 1, 30, 0),
            _usage(10, 3, 0, 5, 0),
            _usage(20, 0, 9, 0, 0),
        ],
    }
}
EXPECTED = {
    "dbo.Orders": {"row_count": 80, "data_size_mb": 3.0, "index_size_mb": 2.0},
    "dbo.Log": {"row_count": 1000, "data_size_mb": 4.0, "index_size_mb": 0.25},
    "dbo.Bare": {"row_count": 7, "data_size_mb": 0.5, "index_size_mb": 0.0},
}


def _code(text: str) -> str:
    return "\n".join(line.split("--", 1)[0] for line in text.splitlines())


def _for_sqlite(node: exp.Expression) -> exp.Expression:
    if isinstance(node, exp.Cast) and node.to.this == exp.DataType.Type.DECIMAL:
        return exp.func("ROUND", node.this, exp.Literal.number(int(node.to.expressions[1].name)))  # type: ignore[return-value]
    if isinstance(node, exp.Escape):
        return exp.true()
    if isinstance(node, exp.Anonymous) and node.name.upper() == "DB_ID":
        return exp.Literal.number(_DB_ID)
    return node


def _run(sql: str) -> dict[str, dict[str, Any]]:
    tree = sqlglot.parse_one(_code(sql), read="tsql").transform(_for_sqlite)
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("ATTACH DATABASE ':memory:' AS sys")
    for name, rows in CATALOG["sys"].items():
        columns = list(rows[0])
        # The table and column names are this module's own CATALOG constants.
        ddl = f"CREATE TABLE sys.{name} ({', '.join(columns)})"
        db.execute(ddl)  # nosemgrep: formatted-sql-query, sqlalchemy-execute-raw-query
        db.executemany(
            f"INSERT INTO sys.{name} VALUES ({', '.join('?' for _ in columns)})",
            [tuple(r[c] for c in columns) for r in rows],
        )
    result = [dict(r) for r in db.execute(tree.sql("sqlite"))]
    db.close()
    return {f"{r['schema_name']}.{r['table_name']}": r for r in result}


def _offline_rows() -> dict[str, dict[str, Any]]:
    match = re.search(r"SET @tables = \((.*?)FOR JSON PATH\s*\);", SCRIPT.read_text(), re.S)
    assert match is not None
    return _run(match.group(1))


def _live_rows() -> dict[str, dict[str, Any]]:
    seen: list[str] = []

    def query(sql: str) -> list[dict]:
        seen.append(sql)
        return []

    collector = SQLServerRemoteCollector.__new__(SQLServerRemoteCollector)
    collector._query = query  # type: ignore[method-assign]
    collector.collect_tables()
    (sql,) = seen
    return _run(sql)


_COLLECTORS = {"offline": _offline_rows, "live": _live_rows}


@pytest.mark.parametrize("collector", sorted(_COLLECTORS))
def test_index_size_sums_the_nonclustered_indexes(collector: str) -> None:
    rows = _COLLECTORS[collector]()
    assert {t: r["index_size_mb"] for t, r in rows.items()} == {
        t: e["index_size_mb"] for t, e in EXPECTED.items()
    }


@pytest.mark.passes_on_main
def test_offline_data_size_is_unchanged() -> None:
    """The heap or clustered index's pages, LOB units included, as before #1873."""
    rows = _offline_rows()
    assert {t: r["data_size_mb"] for t, r in rows.items()} == {
        t: e["data_size_mb"] for t, e in EXPECTED.items()
    }


def test_live_data_size_counts_each_page_once() -> None:
    """The live query joined the usage stats on the table alone, so a table whose
    indexes had N usage rows summed its pages N times; it now matches the offline
    script's figure."""
    rows = _live_rows()
    assert {t: r["data_size_mb"] for t, r in rows.items()} == {
        t: e["data_size_mb"] for t, e in EXPECTED.items()
    }


@pytest.mark.passes_on_main
@pytest.mark.parametrize("collector", sorted(_COLLECTORS))
def test_row_count_counts_the_heap_or_clustered_index_only(collector: str) -> None:
    rows = _COLLECTORS[collector]()
    assert {t: r["row_count"] for t, r in rows.items()} == {
        t: e["row_count"] for t, e in EXPECTED.items()
    }


@pytest.mark.passes_on_main
def test_live_usage_figures_are_unchanged() -> None:
    """The scans are still the largest over the table's indexes; no usage is null."""
    rows = _live_rows()
    assert {t: (r["full_table_scans"], r["index_scans"]) for t, r in rows.items()} == {
        "dbo.Orders": (4, 30),
        "dbo.Log": (9, 0),
        "dbo.Bare": (None, None),
    }


@pytest.mark.passes_on_main
def test_one_row_per_table() -> None:
    """Joining every index's partitions adds no row: still one per table."""
    for collector in _COLLECTORS.values():
        assert sorted(collector()) == sorted(EXPECTED)
