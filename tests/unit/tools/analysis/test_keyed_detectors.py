# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""DynamoDB's key patterns require their key, read from the SQL facts (#920, PR B).

The catalog review found DynamoDB detectors that do not test what their ``why``
says. ``key-value-lookup`` (100) read the collector's ``filter_columns``, which is
empty on every sample query, so it never fired. ``range-query`` (90) was any range
plus any ``ORDER BY``, with no partition value. ``metadata-config-store`` (90) was any
small read of a small table. ``bounded-parent-child``, ``co-accessed-tables`` and
``many-to-many-junction`` counted joins. ``time-series-event-log`` was any INSERT that
names a timestamp column. Aurora's ``high-frequency-pk-lookup`` checked "one table, at
most one row" and never a key. Each now reads the access-pattern facts
(:mod:`src.tools.analysis.key_access`): the key a query pins, the range it bounds
on the same table, whether every table of a join is reached by key.

A key lookup is also Aurora's native access path, a B-tree point read; Aurora's
``key-value-lookup`` fit (80, sourced) replaces the 60 it borrowed from
``standard-crud``, so turning DynamoDB's 100 on does not push every table with a key
lookup to DynamoDB by a borrowed gap.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.shared.catalog.patterns import pattern
from src.tools.analysis.aurora_common_analysis_tools import analyze_aurora_common_use_cases
from src.tools.analysis.dynamodb_analysis_tools import analyze_dynamodb_use_cases


def _col(name: str, data_type: str = "bigint") -> dict[str, Any]:
    normalized = {"bigint": "integer", "varchar": "string", "timestamp": "timestamp"}
    return {
        "column_name": name,
        "data_type": data_type,
        "normalized_data_type": normalized.get(data_type),
        "nullable": False,
    }


def _table(
    name: str,
    columns: list[dict],
    *,
    rows: int = 2_000_000,
    pk: tuple[str, ...] = ("id",),
    fks: tuple[tuple[str, str], ...] = (),
    indexes: tuple[tuple[str, ...], ...] = (),
) -> dict[str, Any]:
    return {
        "table_id": f"public.{name}",
        "table_name": name,
        "schema_name": "public",
        "row_count": rows,
        "size_mb": 50.0,
        "columns": columns,
        "primary_key": list(pk),
        "foreign_keys": [
            {"columns": [c], "referenced_table": t, "referenced_columns": ["id"]} for c, t in fks
        ],
        "indexes": [
            {
                "index_name": f"{name}_pkey",
                "columns": list(pk),
                "is_unique": True,
                "is_primary": True,
            }
        ]
        + [
            {"index_name": f"{name}_{i}", "columns": list(c), "is_unique": False}
            for i, c in enumerate(indexes)
        ],
    }


TABLES = [
    _table("orders", [_col("id"), _col("status", "varchar"), _col("created_at", "timestamp")]),
    _table(
        "order_items",
        [_col("id"), _col("order_id"), _col("sku", "varchar")],
        fks=(("order_id", "orders"),),
    ),
    _table(
        "events",
        [_col("id"), _col("account_id"), _col("kind", "varchar"), _col("created_at", "timestamp")],
        indexes=(("account_id",),),
    ),
    _table(
        "settings",
        [_col("id"), _col("name", "varchar"), _col("value", "varchar")],
        rows=400,
    ),
    _table("posts", [_col("id"), _col("title", "varchar")]),
    _table("tags", [_col("id"), _col("label", "varchar")]),
    _table(
        "post_tags",
        [_col("post_id"), _col("tag_id")],
        pk=("post_id", "tag_id"),
        fks=(("post_id", "posts"), ("tag_id", "tags")),
    ),
    _table(
        "promotions",
        [_col("id"), _col("starts_on", "timestamp"), _col("ends_on", "timestamp")],
    ),
    _table(
        "user_auth_tokens",
        [
            _col("id"),
            _col("user_id"),
            _col("auth_token", "varchar"),
            _col("prev_auth_token", "varchar"),
        ],
        rows=900,
        indexes=(("auth_token",), ("prev_auth_token",)),
    ),
]


def _q(
    qid: str,
    sql: str,
    tables: list[str],
    *,
    cps: float = 2.0,
    rows: float = 1.0,
) -> dict[str, Any]:
    joins = len(set(tables)) - 1
    return {
        "query_id": qid,
        "query_text": sql,
        "query_type": sql.split()[0].upper(),
        "calls_per_second": cps,
        "frequency_per_hour": cps * 3600,
        "tables_accessed": [f"public.{t}" for t in tables],
        "rows_returned_avg": rows,
        "execution_time_ms_avg": 3.0,
        "has_joins": joins > 0,
        "join_count": joins,
        "filter_columns": [],  # empty, as on every bundled sample query
    }


def _collector(*queries: dict) -> dict[str, Any]:
    return {
        "metadata": {"source_database": {"engine": "postgresql"}},
        "database_schema": {"tables": TABLES},
        "queries": {"query_patterns": list(queries)},
    }


def _ddb(*queries: dict) -> dict[str, set[str]]:
    analysis = analyze_dynamodb_use_cases(_collector(*queries))
    return {p.pattern_type: set(p.query_ids or []) for p in analysis.patterns_detected}


def _fires(pattern_type: str, query: dict, *context: dict) -> bool:
    return query["query_id"] in _ddb(query, *context).get(pattern_type, set())


KEY_READ = _q("kv", "SELECT * FROM orders WHERE id = $1", ["orders"])
ANCHORED_JOIN = (
    "SELECT o.id, i.sku FROM orders o JOIN order_items i ON i.order_id = o.id WHERE o.id = $1"
)
UNANCHORED_JOIN = (
    "SELECT o.id, i.sku FROM orders o JOIN order_items i ON i.order_id = o.id "
    "WHERE o.status = $1"
)
SETTINGS_READ = _q("cfg", "SELECT value FROM settings WHERE id = $1", ["settings"], cps=5)

CASES = [
    # (pattern, positive, negative, context queries for both)
    (
        "key-value-lookup",
        KEY_READ,
        _q("by-status", "SELECT * FROM orders WHERE status = $1", ["orders"], rows=1),
        (),
    ),
    (
        "range-query",
        _q(
            "entity-range",
            "SELECT * FROM events WHERE account_id = $1 AND created_at > $2 ORDER BY created_at",
            ["events"],
            rows=20,
        ),
        # An interval containment on two columns with no partition value (trailgear's
        # Promotion query): a Scan with a filter on DynamoDB.
        _q(
            "interval",
            "SELECT * FROM promotions WHERE $1 BETWEEN starts_on AND ends_on ORDER BY ends_on",
            ["promotions"],
            rows=3,
        ),
        (),
    ),
    (
        "metadata-config-store",
        SETTINGS_READ,
        # The same small table, but written about as often as it is read.
        _q("cfg-hot", "SELECT value FROM settings WHERE id = $1", ["settings"], cps=5),
        (_q("cfg-write", "UPDATE settings SET value = $1 WHERE id = $2", ["settings"], cps=4),),
    ),
    (
        "bounded-parent-child",
        _q("parent-child", ANCHORED_JOIN, ["orders", "order_items"], rows=4),
        _q("unanchored", UNANCHORED_JOIN, ["orders", "order_items"], rows=400),
        (),
    ),
    (
        "co-accessed-tables",
        _q("co-access", ANCHORED_JOIN, ["orders", "order_items"], rows=4),
        _q("co-access-rare", ANCHORED_JOIN, ["orders", "order_items"], cps=0.005, rows=4),
        (),
    ),
    (
        "many-to-many-junction",
        _q(
            "junction",
            "SELECT t.label FROM posts p JOIN post_tags pt ON pt.post_id = p.id "
            "JOIN tags t ON t.id = pt.tag_id WHERE p.id = $1",
            ["posts", "post_tags", "tags"],
            rows=5,
        ),
        _q(
            "no-junction",
            "SELECT o.id FROM orders o JOIN order_items i ON i.order_id = o.id "
            "JOIN events e ON e.account_id = o.id WHERE o.status = $1",
            ["orders", "order_items", "events"],
            rows=50,
        ),
        (),
    ),
    (
        "session-store",
        _q(
            "token-read",
            "SELECT user_id FROM user_auth_tokens WHERE auth_token = $1",
            ["user_auth_tokens"],
        ),
        # Either of two token columns (an OR): no single key addresses the item.
        _q(
            "token-or",
            "SELECT * FROM user_auth_tokens WHERE (auth_token = $1 OR prev_auth_token = $2)",
            ["user_auth_tokens"],
        ),
        (),
    ),
    (
        "time-series-event-log",
        _q(
            "append",
            "INSERT INTO events (account_id, kind, created_at) VALUES ($1, $2, $3)",
            ["events"],
        ),
        # A timestamp column on a table the workload also updates: not an event log.
        _q(
            "mutable",
            "INSERT INTO orders (status, created_at) VALUES ($1, $2)",
            ["orders"],
        ),
        (_q("upd", "UPDATE orders SET status = $1 WHERE id = $2", ["orders"]),),
    ),
]


@pytest.mark.parametrize(
    ("pattern_type", "positive", "negative", "context"), CASES, ids=[c[0] for c in CASES]
)
def test_the_pattern_fires_on_its_keyed_shape_only(pattern_type, positive, negative, context):
    if pattern_type == "metadata-config-store":
        # Read-mostly: the positive's table has no writes in its workload.
        assert _fires(pattern_type, positive)
    else:
        assert _fires(pattern_type, positive, *context)
    assert not _fires(pattern_type, negative, *context)


def test_an_unanchored_ordered_read_of_a_small_table_fires_no_key_pattern():
    # discourse's `SELECT "user_auth_tokens".* ... ORDER BY "id" ASC LIMIT $1` went to
    # DynamoDB at 89 after catalog PR A: a Scan returns no order.
    query = _q(
        "ordered",
        'SELECT "user_auth_tokens".* FROM "user_auth_tokens" '
        'ORDER BY "user_auth_tokens"."id" ASC LIMIT $1',
        ["user_auth_tokens"],
        rows=1,
        cps=3,
    )
    fired = {p for p, ids in _ddb(query).items() if "ordered" in ids}
    keyed = {
        "key-value-lookup",
        "range-query",
        "metadata-config-store",
        "bounded-parent-child",
        "co-accessed-tables",
        "many-to-many-junction",
        "time-series-event-log",
        "session-store",
    }
    assert not fired & keyed, fired


def test_aurora_scores_a_key_lookup_from_its_own_documented_access_path():
    analysis = analyze_aurora_common_use_cases(_collector(KEY_READ))
    patterns, _anti = analysis
    assert any(p.pattern_type == "key-value-lookup" and "kv" in p.query_ids for p in patterns)
    entry = pattern("key-value-lookup")
    aurora = [entry.engines[e] for e in ("aurora_postgresql", "aurora_mysql")]
    assert {(a.kind, a.score) for a in aurora} == {("fit", 80)}
    assert all("PostgreSQL documentation" in a.why or "Reference Manual" in a.why for a in aurora)
    assert entry.engines["dynamodb"].score == 100


def test_aurora_does_not_score_a_non_key_read_as_a_key_lookup():
    analysis = analyze_aurora_common_use_cases(
        _collector(_q("by-status", "SELECT * FROM orders WHERE status = $1", ["orders"]))
    )
    patterns, _anti = analysis
    assert not any(p.pattern_type == "key-value-lookup" for p in patterns)


def test_high_frequency_pk_lookup_needs_a_key():
    count = _q(
        "count", "SELECT COUNT(*) FROM orders WHERE status = $1", ["orders"], cps=500, rows=1
    )
    point = _q("point", "SELECT * FROM orders WHERE id = $1", ["orders"], cps=500, rows=1)
    _patterns, anti = analyze_aurora_common_use_cases(_collector(count, point))
    hits = {
        qid
        for ap in anti
        if ap.anti_pattern_type == "high-frequency-pk-lookup"
        for qid in ap.query_ids or []
    }
    assert hits == {"point"}
