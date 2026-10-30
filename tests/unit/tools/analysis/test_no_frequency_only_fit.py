# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""No fit pattern keys on call frequency alone (#920).

A fit pattern says the engine serves an access pattern well. How often a query runs is
not an access pattern: DynamoDB's ``low-frequency-read`` (any SELECT under 0.1 calls/s,
joins, aggregates and full scans included) and ``low-frequency-write`` (any write under
0.5 calls/s) fired on 1207 and 292 discourse queries, and ``write-heavy-ingestion`` fired
on every write at 0.5 calls/s or more, whatever it wrote. That on-demand capacity suits
sporadic traffic is a cost fact (ADR-031 Decision 5, #494), not a fit.

The guard: run every owning engine's detectors on probes of different shapes (key
lookups, key-scoped reads, aggregates, full scans, unanchored joins; keyed and unkeyed
writes) at a range of call rates. A fit pattern that fires on every probe of a kind at
some call rate is keyed on the rate, since no shape is common to all of them. ElastiCache
is left out: it owns no query (Decision 2 step 1), and a cache's value is its hit rate,
so its overlay decision is meant to read the call rate.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from src.contracts.analysis_output import WorkloadAnalysis
from src.tools.analysis.aurora_mysql_analysis_tools import analyze_aurora_mysql_use_cases
from src.tools.analysis.aurora_pg_analysis_tools import analyze_aurora_pg_use_cases
from src.tools.analysis.documentdb_analysis_tools import analyze_documentdb_use_cases
from src.tools.analysis.dynamodb_analysis_tools import analyze_dynamodb_use_cases
from src.tools.analysis.opensearch_analysis_tools import analyze_opensearch_use_cases

ANALYSES: dict[str, Callable[[dict], WorkloadAnalysis]] = {
    "dynamodb": analyze_dynamodb_use_cases,
    "documentdb": analyze_documentdb_use_cases,
    "opensearch": analyze_opensearch_use_cases,
    "aurora_postgresql": analyze_aurora_pg_use_cases,
    "aurora_mysql": analyze_aurora_mysql_use_cases,
}

CALL_RATES = (0.001, 0.05, 0.3, 5.0, 300.0)


def _col(name: str, data_type: str, nullable: bool = False) -> dict[str, Any]:
    return {"column_name": name, "data_type": data_type, "nullable": nullable}


def _table(name: str, columns: list[dict], fks: list[dict] | None = None) -> dict[str, Any]:
    return {
        "table_id": f"public.{name}",
        "table_name": name,
        "schema_name": "public",
        "row_count": 2_000_000,
        "size_mb": 1500.0,
        "columns": columns,
        "primary_key": ["id"],
        "foreign_keys": fks or [],
        "indexes": [
            {"index_name": f"{name}_pkey", "columns": ["id"], "is_unique": True, "is_primary": True}
        ],
    }


ORDERS = _table(
    "orders",
    [_col("id", "bigint"), _col("status", "varchar"), _col("created_at", "timestamp")],
)
ORDER_ITEMS = _table(
    "order_items",
    [_col("id", "bigint"), _col("order_id", "bigint"), _col("sku", "varchar")],
    fks=[{"columns": ["order_id"], "referenced_table": "orders", "referenced_columns": ["id"]}],
)

READS = (
    ("key lookup", "SELECT * FROM orders WHERE id = $1", ["orders"], 1),
    ("key-scoped read", "SELECT * FROM order_items WHERE order_id = $1", ["order_items"], 5),
    ("aggregate", "SELECT status, COUNT(*) FROM orders GROUP BY status", ["orders"], 6),
    ("full scan", "SELECT * FROM orders ORDER BY created_at DESC", ["orders"], 5000),
    (
        "unanchored join",
        "SELECT o.id, i.sku FROM orders o JOIN order_items i ON i.order_id = o.id "
        "WHERE o.status = $1",
        ["orders", "order_items"],
        40,
    ),
)
WRITES = (
    ("keyed insert", "INSERT INTO orders (status, created_at) VALUES ($1, $2)", ["orders"], 0),
    ("keyed update", "UPDATE orders SET status = $1 WHERE id = $2", ["orders"], 0),
    ("range delete", "DELETE FROM orders WHERE created_at < $1", ["orders"], 0),
    ("unkeyed update", "UPDATE orders SET status = $1 WHERE status = $2", ["orders"], 0),
    (
        "joined update",
        "UPDATE orders o SET status = $1 FROM order_items i "
        "WHERE i.order_id = o.id AND i.sku = $2",
        ["orders", "order_items"],
        0,
    ),
)


def _query(sql: str, tables: list[str], rows: float, cps: float) -> dict[str, Any]:
    verb = sql.split()[0].upper()
    joins = len(tables) - 1
    return {
        "query_id": "probe",
        "query_text": sql,
        "query_type": verb,
        "calls_per_second": cps,
        "frequency_per_hour": cps * 3600,
        "tables_accessed": [f"public.{t}" for t in tables],
        "rows_returned_avg": rows,
        "execution_time_ms_avg": 4.0,
        "has_joins": joins > 0,
        "join_count": joins,
    }


def _fits(engine: str, sql: str, tables: list[str], rows: float, cps: float) -> set[str]:
    collector = {
        "metadata": {"source_database": {"engine": "postgresql"}},
        "database_schema": {"tables": [ORDERS, ORDER_ITEMS]},
        "queries": {"query_patterns": [_query(sql, tables, rows, cps)]},
    }
    analysis = ANALYSES[engine](collector)
    return {p.pattern_type for p in analysis.patterns_detected if "probe" in (p.query_ids or [])}


@pytest.mark.parametrize("engine", sorted(ANALYSES))
@pytest.mark.parametrize(("kind", "probes"), [("read", READS), ("write", WRITES)])
def test_no_fit_pattern_fires_on_every_shape_at_some_call_rate(engine, kind, probes):
    keyed_on_rate: dict[str, float] = {}
    for cps in CALL_RATES:
        per_probe = [_fits(engine, sql, tables, rows, cps) for _name, sql, tables, rows in probes]
        for pattern_type in set.intersection(*per_probe):
            keyed_on_rate.setdefault(pattern_type, cps)
    assert not keyed_on_rate, (
        f"{engine}: these fit patterns fire on every {kind} shape at the given call rate, "
        f"so they key on the rate, not on an access pattern: {keyed_on_rate}"
    )


def test_the_low_frequency_catalog_entries_are_not_fits():
    from src.shared.catalog.patterns import pattern

    for pattern_id in ("low-frequency-read", "low-frequency-write"):
        entry = pattern(pattern_id)
        assert entry.detectors == ()
        assert all(s.kind != "fit" for s in entry.engines.values()), pattern_id
        assert "#494" in entry.engines["dynamodb"].why
