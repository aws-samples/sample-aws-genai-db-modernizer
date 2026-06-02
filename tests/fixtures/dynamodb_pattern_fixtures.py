"""
Focused per-pattern fixtures for DynamoDB analysis testing.

Each function returns a minimal CollectorOutputContract-compliant dict
designed to trigger exactly ONE specific DynamoDB pattern (or anti-pattern).
"""

from typing import Any


def _base_output(
    job_id: str,
    tables: list[dict],
    query_patterns: list[dict],
) -> dict[str, Any]:
    """Minimal scaffold with only the fields the analysis agent reads."""
    return {
        "job_id": job_id,
        "database_schema": {"tables": tables},
        "queries": {"query_patterns": query_patterns},
    }


# ---------------------------------------------------------------------------
# Key-value lookup fixture
# ---------------------------------------------------------------------------


def get_key_value_fixture() -> dict[str, Any]:
    """1 table, 2 high-frequency PK lookups. Triggers key-value-lookup only."""
    table = {
        "table_id": "app.users",
        "table_name": "users",
        "row_count": 50000,
        "size_mb": 12.0,
        "columns": [
            {"column_name": "id", "ordinal_position": 1, "data_type": "int", "nullable": False},
            {
                "column_name": "email",
                "ordinal_position": 2,
                "data_type": "varchar",
                "nullable": False,
            },
            {
                "column_name": "name",
                "ordinal_position": 3,
                "data_type": "varchar",
                "nullable": False,
            },
        ],
        "indexes": [
            {
                "index_name": "PRIMARY",
                "columns": ["id"],
                "is_unique": True,
                "is_primary": True,
                "index_type": "btree",
            }
        ],
        "primary_key": ["id"],
    }
    queries = [
        {
            "query_id": "kv-001",
            "query_text": "SELECT * FROM users WHERE id = ?",
            "query_type": "SELECT",
            "frequency_per_hour": 5000,
            "calls_per_second": 1.4,
            "tables_accessed": ["app.users"],
            "rows_returned_avg": 1.0,
            "filter_columns": ["id"],
            "execution_time_ms_avg": 0.5,
        },
        {
            "query_id": "kv-002",
            "query_text": "SELECT email, name FROM users WHERE id = ?",
            "query_type": "SELECT",
            "frequency_per_hour": 3000,
            "calls_per_second": 0.8,
            "tables_accessed": ["app.users"],
            "rows_returned_avg": 1.0,
            "filter_columns": ["id"],
            "execution_time_ms_avg": 0.3,
        },
    ]
    return _base_output("ddb-kv-test", [table], queries)


# ---------------------------------------------------------------------------
# Range query fixture
# ---------------------------------------------------------------------------


def get_range_query_fixture() -> dict[str, Any]:
    """1 table, 2 range queries with ORDER BY. Triggers range-query only."""
    table = {
        "table_id": "app.orders",
        "table_name": "orders",
        "row_count": 200000,
        "size_mb": 80.0,
        "columns": [
            {
                "column_name": "customer_id",
                "ordinal_position": 1,
                "data_type": "int",
                "nullable": False,
            },
            {
                "column_name": "order_date",
                "ordinal_position": 2,
                "data_type": "datetime",
                "nullable": False,
            },
            {
                "column_name": "total",
                "ordinal_position": 3,
                "data_type": "decimal",
                "nullable": False,
            },
        ],
        "indexes": [
            {
                "index_name": "PRIMARY",
                "columns": ["customer_id", "order_date"],
                "is_unique": True,
                "is_primary": True,
                "index_type": "btree",
            },
        ],
        "primary_key": ["customer_id", "order_date"],
    }
    queries = [
        {
            "query_id": "rq-001",
            "query_text": "SELECT * FROM orders WHERE customer_id = ? AND order_date BETWEEN ? AND ? ORDER BY order_date DESC LIMIT 20",
            "query_type": "SELECT",
            "frequency_per_hour": 800,
            "calls_per_second": 0.2,
            "tables_accessed": ["app.orders"],
            "rows_returned_avg": 15.0,
            "filter_columns": ["customer_id", "order_date"],
            "execution_time_ms_avg": 2.0,
        },
        {
            "query_id": "rq-002",
            "query_text": "SELECT * FROM orders WHERE customer_id = ? AND total >= ? ORDER BY total DESC LIMIT 10",
            "query_type": "SELECT",
            "frequency_per_hour": 400,
            "calls_per_second": 0.1,
            "tables_accessed": ["app.orders"],
            "rows_returned_avg": 8.0,
            "filter_columns": ["customer_id", "total"],
            "execution_time_ms_avg": 3.0,
        },
    ]
    return _base_output("ddb-range-test", [table], queries)


# ---------------------------------------------------------------------------
# Write-heavy ingestion fixture
# ---------------------------------------------------------------------------


def get_write_heavy_fixture() -> dict[str, Any]:
    """1 table, high-frequency INSERTs. Triggers write-heavy-ingestion only."""
    table = {
        "table_id": "app.events",
        "table_name": "events",
        "row_count": 5000000,
        "size_mb": 800.0,
        "columns": [
            {
                "column_name": "event_id",
                "ordinal_position": 1,
                "data_type": "bigint",
                "nullable": False,
            },
            {
                "column_name": "event_type",
                "ordinal_position": 2,
                "data_type": "varchar",
                "nullable": False,
            },
            {
                "column_name": "payload",
                "ordinal_position": 3,
                "data_type": "json",
                "nullable": True,
            },
        ],
        "indexes": [
            {
                "index_name": "PRIMARY",
                "columns": ["event_id"],
                "is_unique": True,
                "is_primary": True,
                "index_type": "btree",
            }
        ],
        "primary_key": ["event_id"],
    }
    queries = [
        {
            "query_id": "wh-001",
            "query_text": "INSERT INTO events (event_type, payload) VALUES (?, ?)",
            "query_type": "INSERT",
            "frequency_per_hour": 36000,
            "calls_per_second": 10.0,
            "tables_accessed": ["app.events"],
            "rows_returned_avg": 0,
            "execution_time_ms_avg": 1.0,
        },
        {
            "query_id": "wh-002",
            "query_text": "UPDATE events SET payload = ? WHERE event_id = ?",
            "query_type": "UPDATE",
            "frequency_per_hour": 7200,
            "calls_per_second": 2.0,
            "tables_accessed": ["app.events"],
            "rows_returned_avg": 0,
            "filter_columns": ["event_id"],
            "execution_time_ms_avg": 0.8,
        },
    ]
    return _base_output("ddb-write-test", [table], queries)


# ---------------------------------------------------------------------------
# Time-series / event log fixture
# ---------------------------------------------------------------------------


def get_time_series_fixture() -> dict[str, Any]:
    """1 table, timestamp-based INSERT + range SELECT. Triggers time-series only."""
    table = {
        "table_id": "app.audit_log",
        "table_name": "audit_log",
        "row_count": 1000000,
        "size_mb": 200.0,
        "columns": [
            {"column_name": "id", "ordinal_position": 1, "data_type": "bigint", "nullable": False},
            {
                "column_name": "user_id",
                "ordinal_position": 2,
                "data_type": "int",
                "nullable": False,
            },
            {
                "column_name": "action",
                "ordinal_position": 3,
                "data_type": "varchar",
                "nullable": False,
            },
            {
                "column_name": "created_at",
                "ordinal_position": 4,
                "data_type": "datetime",
                "nullable": False,
            },
        ],
        "indexes": [
            {
                "index_name": "PRIMARY",
                "columns": ["id"],
                "is_unique": True,
                "is_primary": True,
                "index_type": "btree",
            }
        ],
        "primary_key": ["id"],
    }
    queries = [
        {
            "query_id": "ts-001",
            "query_text": "INSERT INTO audit_log (user_id, action, created_at) VALUES (?, ?, NOW())",
            "query_type": "INSERT",
            "frequency_per_hour": 18000,
            "calls_per_second": 5.0,
            "tables_accessed": ["app.audit_log"],
            "rows_returned_avg": 0,
            "execution_time_ms_avg": 0.5,
        },
        {
            "query_id": "ts-002",
            "query_text": "SELECT * FROM audit_log WHERE user_id = ? AND created_at BETWEEN ? AND ? ORDER BY created_at DESC LIMIT 50",
            "query_type": "SELECT",
            "frequency_per_hour": 500,
            "calls_per_second": 0.14,
            "tables_accessed": ["app.audit_log"],
            "rows_returned_avg": 30.0,
            "filter_columns": ["user_id", "created_at"],
            "execution_time_ms_avg": 5.0,
        },
    ]
    return _base_output("ddb-ts-test", [table], queries)


# ---------------------------------------------------------------------------
# Metadata / config store fixture
# ---------------------------------------------------------------------------


def get_metadata_store_fixture() -> dict[str, Any]:
    """1 small table, frequent reads, rare writes. Triggers metadata-config-store only."""
    table = {
        "table_id": "app.feature_flags",
        "table_name": "feature_flags",
        "row_count": 50,
        "size_mb": 0.01,
        "columns": [
            {
                "column_name": "flag_name",
                "ordinal_position": 1,
                "data_type": "varchar",
                "nullable": False,
            },
            {
                "column_name": "enabled",
                "ordinal_position": 2,
                "data_type": "tinyint",
                "nullable": False,
            },
            {
                "column_name": "updated_at",
                "ordinal_position": 3,
                "data_type": "datetime",
                "nullable": False,
            },
        ],
        "indexes": [
            {
                "index_name": "PRIMARY",
                "columns": ["flag_name"],
                "is_unique": True,
                "is_primary": True,
                "index_type": "btree",
            }
        ],
        "primary_key": ["flag_name"],
    }
    queries = [
        {
            "query_id": "md-001",
            "query_text": "SELECT enabled FROM feature_flags WHERE flag_name = ?",
            "query_type": "SELECT",
            "frequency_per_hour": 10000,
            "calls_per_second": 2.8,
            "tables_accessed": ["app.feature_flags"],
            "rows_returned_avg": 1.0,
            "filter_columns": ["flag_name"],
            "execution_time_ms_avg": 0.2,
        },
    ]
    return _base_output("ddb-meta-test", [table], queries)


# ---------------------------------------------------------------------------
# Session store fixture
# ---------------------------------------------------------------------------


def get_session_store_fixture() -> dict[str, Any]:
    """1 table with session keywords. Triggers session-store only."""
    table = {
        "table_id": "app.sessions",
        "table_name": "sessions",
        "row_count": 100000,
        "size_mb": 25.0,
        "columns": [
            {
                "column_name": "session_id",
                "ordinal_position": 1,
                "data_type": "varchar",
                "nullable": False,
            },
            {
                "column_name": "user_id",
                "ordinal_position": 2,
                "data_type": "int",
                "nullable": False,
            },
            {"column_name": "data", "ordinal_position": 3, "data_type": "json", "nullable": True},
            {
                "column_name": "expires_at",
                "ordinal_position": 4,
                "data_type": "datetime",
                "nullable": False,
            },
        ],
        "indexes": [
            {
                "index_name": "PRIMARY",
                "columns": ["session_id"],
                "is_unique": True,
                "is_primary": True,
                "index_type": "btree",
            }
        ],
        "primary_key": ["session_id"],
    }
    queries = [
        {
            "query_id": "ss-001",
            "query_text": "SELECT data FROM sessions WHERE session_id = ?",
            "query_type": "SELECT",
            "frequency_per_hour": 20000,
            "calls_per_second": 5.5,
            "tables_accessed": ["app.sessions"],
            "rows_returned_avg": 1.0,
            "filter_columns": ["session_id"],
            "execution_time_ms_avg": 0.3,
        },
        {
            "query_id": "ss-002",
            "query_text": "UPDATE sessions SET data = ?, expires_at = ? WHERE session_id = ?",
            "query_type": "UPDATE",
            "frequency_per_hour": 8000,
            "calls_per_second": 2.2,
            "tables_accessed": ["app.sessions"],
            "rows_returned_avg": 0,
            "filter_columns": ["session_id"],
            "execution_time_ms_avg": 0.5,
        },
    ]
    return _base_output("ddb-session-test", [table], queries)


# ---------------------------------------------------------------------------
# Denormalizable join fixture
# ---------------------------------------------------------------------------


def get_denormalizable_join_fixture() -> dict[str, Any]:
    """2 tables with FK, simple JOIN query. Triggers denormalizable-relationship only."""
    parent = {
        "table_id": "app.orders",
        "table_name": "orders",
        "row_count": 50000,
        "size_mb": 15.0,
        "columns": [
            {"column_name": "id", "ordinal_position": 1, "data_type": "int", "nullable": False},
            {
                "column_name": "customer_id",
                "ordinal_position": 2,
                "data_type": "int",
                "nullable": False,
            },
            {
                "column_name": "total",
                "ordinal_position": 3,
                "data_type": "decimal",
                "nullable": False,
            },
        ],
        "indexes": [
            {
                "index_name": "PRIMARY",
                "columns": ["id"],
                "is_unique": True,
                "is_primary": True,
                "index_type": "btree",
            }
        ],
        "primary_key": ["id"],
    }
    child = {
        "table_id": "app.order_items",
        "table_name": "order_items",
        "row_count": 200000,
        "size_mb": 40.0,
        "columns": [
            {"column_name": "id", "ordinal_position": 1, "data_type": "int", "nullable": False},
            {
                "column_name": "order_id",
                "ordinal_position": 2,
                "data_type": "int",
                "nullable": False,
            },
            {
                "column_name": "product_name",
                "ordinal_position": 3,
                "data_type": "varchar",
                "nullable": False,
            },
            {
                "column_name": "quantity",
                "ordinal_position": 4,
                "data_type": "int",
                "nullable": False,
            },
        ],
        "indexes": [
            {
                "index_name": "PRIMARY",
                "columns": ["id"],
                "is_unique": True,
                "is_primary": True,
                "index_type": "btree",
            }
        ],
        "primary_key": ["id"],
        "foreign_keys": [
            {
                "constraint_name": "fk_order",
                "columns": ["order_id"],
                "referenced_table": "orders",
                "referenced_columns": ["id"],
            }
        ],
    }
    queries = [
        {
            "query_id": "dj-001",
            "query_text": "SELECT o.*, oi.product_name, oi.quantity FROM orders o JOIN order_items oi ON o.id = oi.order_id WHERE o.id = ?",
            "query_type": "SELECT",
            "frequency_per_hour": 2000,
            "calls_per_second": 0.6,
            "tables_accessed": ["app.orders", "app.order_items"],
            "rows_returned_avg": 5.0,
            "filter_columns": ["id"],
            "has_joins": True,
            "join_count": 1,
            "execution_time_ms_avg": 3.0,
        },
    ]
    return _base_output("ddb-join-test", [parent, child], queries)


# ---------------------------------------------------------------------------
# Anti-pattern: Frequent full scan
# ---------------------------------------------------------------------------


def get_frequent_scan_fixture() -> dict[str, Any]:
    """1 table, high-frequency SELECT with no WHERE. Triggers frequent-full-scan anti-pattern."""
    table = {
        "table_id": "app.products",
        "table_name": "products",
        "row_count": 100000,
        "size_mb": 50.0,
        "columns": [
            {"column_name": "id", "ordinal_position": 1, "data_type": "int", "nullable": False},
            {
                "column_name": "name",
                "ordinal_position": 2,
                "data_type": "varchar",
                "nullable": False,
            },
            {
                "column_name": "price",
                "ordinal_position": 3,
                "data_type": "decimal",
                "nullable": False,
            },
        ],
        "indexes": [
            {
                "index_name": "PRIMARY",
                "columns": ["id"],
                "is_unique": True,
                "is_primary": True,
                "index_type": "btree",
            }
        ],
        "primary_key": ["id"],
    }
    queries = [
        {
            "query_id": "scan-001",
            "query_text": "SELECT * FROM products",
            "query_type": "SELECT",
            "frequency_per_hour": 360,
            "calls_per_second": 0.1,
            "tables_accessed": ["app.products"],
            "rows_returned_avg": 100000,
            "execution_time_ms_avg": 500.0,
        },
    ]
    return _base_output("ddb-scan-test", [table], queries)


# ---------------------------------------------------------------------------
# Composite PK fixture (1, 2, 3, and 4-column PKs)
# ---------------------------------------------------------------------------


def get_composite_pk_fixture() -> dict[str, Any]:
    """4 tables with 1, 2, 3, and 4-column PKs for PK classification testing."""
    single_pk = {
        "table_id": "app.users",
        "table_name": "users",
        "row_count": 10000,
        "size_mb": 5.0,
        "columns": [
            {"column_name": "id", "ordinal_position": 1, "data_type": "int", "nullable": False},
            {
                "column_name": "name",
                "ordinal_position": 2,
                "data_type": "varchar",
                "nullable": False,
            },
        ],
        "indexes": [
            {
                "index_name": "PRIMARY",
                "columns": ["id"],
                "is_unique": True,
                "is_primary": True,
                "index_type": "btree",
            },
        ],
        "primary_key": ["id"],
    }
    two_col_pk = {
        "table_id": "app.orders",
        "table_name": "orders",
        "row_count": 50000,
        "size_mb": 20.0,
        "columns": [
            {
                "column_name": "customer_id",
                "ordinal_position": 1,
                "data_type": "int",
                "nullable": False,
            },
            {
                "column_name": "order_date",
                "ordinal_position": 2,
                "data_type": "datetime",
                "nullable": False,
            },
            {
                "column_name": "total",
                "ordinal_position": 3,
                "data_type": "decimal",
                "nullable": False,
            },
        ],
        "indexes": [
            {
                "index_name": "PRIMARY",
                "columns": ["customer_id", "order_date"],
                "is_unique": True,
                "is_primary": True,
                "index_type": "btree",
            },
        ],
        "primary_key": ["customer_id", "order_date"],
    }
    three_col_pk = {
        "table_id": "app.inventory",
        "table_name": "inventory",
        "row_count": 200000,
        "size_mb": 30.0,
        "columns": [
            {
                "column_name": "warehouse_id",
                "ordinal_position": 1,
                "data_type": "int",
                "nullable": False,
            },
            {
                "column_name": "product_id",
                "ordinal_position": 2,
                "data_type": "int",
                "nullable": False,
            },
            {
                "column_name": "batch_id",
                "ordinal_position": 3,
                "data_type": "int",
                "nullable": False,
            },
            {
                "column_name": "quantity",
                "ordinal_position": 4,
                "data_type": "int",
                "nullable": False,
            },
        ],
        "indexes": [
            {
                "index_name": "PRIMARY",
                "columns": ["warehouse_id", "product_id", "batch_id"],
                "is_unique": True,
                "is_primary": True,
                "index_type": "btree",
            },
        ],
        "primary_key": ["warehouse_id", "product_id", "batch_id"],
    }
    four_col_pk = {
        "table_id": "app.audit_entries",
        "table_name": "audit_entries",
        "row_count": 1000000,
        "size_mb": 100.0,
        "columns": [
            {
                "column_name": "region",
                "ordinal_position": 1,
                "data_type": "varchar",
                "nullable": False,
            },
            {
                "column_name": "service",
                "ordinal_position": 2,
                "data_type": "varchar",
                "nullable": False,
            },
            {"column_name": "date", "ordinal_position": 3, "data_type": "date", "nullable": False},
            {"column_name": "seq", "ordinal_position": 4, "data_type": "int", "nullable": False},
            {
                "column_name": "payload",
                "ordinal_position": 5,
                "data_type": "json",
                "nullable": True,
            },
        ],
        "indexes": [
            {
                "index_name": "PRIMARY",
                "columns": ["region", "service", "date", "seq"],
                "is_unique": True,
                "is_primary": True,
                "index_type": "btree",
            },
        ],
        "primary_key": ["region", "service", "date", "seq"],
    }
    queries = [
        {
            "query_id": "cpk-001",
            "query_text": "SELECT * FROM users WHERE id = ?",
            "query_type": "SELECT",
            "frequency_per_hour": 1000,
            "calls_per_second": 0.3,
            "tables_accessed": ["app.users"],
            "rows_returned_avg": 1.0,
            "filter_columns": ["id"],
            "execution_time_ms_avg": 0.5,
        },
    ]
    return _base_output(
        "ddb-composite-pk-test",
        [single_pk, two_col_pk, three_col_pk, four_col_pk],
        queries,
    )


# ---------------------------------------------------------------------------
# Aggregate fixture (3 tables with FK relationships and co-access queries)
# ---------------------------------------------------------------------------


def get_aggregate_fixture() -> dict[str, Any]:
    """3 tables: orders → order_items, orders → shipments, with co-access queries."""
    orders = {
        "table_id": "app.orders",
        "table_name": "orders",
        "row_count": 50000,
        "size_mb": 15.0,
        "columns": [
            {"column_name": "id", "ordinal_position": 1, "data_type": "int", "nullable": False},
            {
                "column_name": "customer_id",
                "ordinal_position": 2,
                "data_type": "int",
                "nullable": False,
            },
            {
                "column_name": "total",
                "ordinal_position": 3,
                "data_type": "decimal",
                "nullable": False,
            },
        ],
        "indexes": [
            {
                "index_name": "PRIMARY",
                "columns": ["id"],
                "is_unique": True,
                "is_primary": True,
                "index_type": "btree",
            },
        ],
        "primary_key": ["id"],
    }
    order_items = {
        "table_id": "app.order_items",
        "table_name": "order_items",
        "row_count": 200000,
        "size_mb": 40.0,
        "columns": [
            {"column_name": "id", "ordinal_position": 1, "data_type": "int", "nullable": False},
            {
                "column_name": "order_id",
                "ordinal_position": 2,
                "data_type": "int",
                "nullable": False,
            },
            {
                "column_name": "product_name",
                "ordinal_position": 3,
                "data_type": "varchar",
                "nullable": False,
            },
            {
                "column_name": "quantity",
                "ordinal_position": 4,
                "data_type": "int",
                "nullable": False,
            },
        ],
        "indexes": [
            {
                "index_name": "PRIMARY",
                "columns": ["id"],
                "is_unique": True,
                "is_primary": True,
                "index_type": "btree",
            },
        ],
        "primary_key": ["id"],
        "foreign_keys": [
            {
                "constraint_name": "fk_order",
                "columns": ["order_id"],
                "referenced_table": "orders",
                "referenced_columns": ["id"],
            },
        ],
    }
    shipments = {
        "table_id": "app.shipments",
        "table_name": "shipments",
        "row_count": 60000,
        "size_mb": 10.0,
        "columns": [
            {"column_name": "id", "ordinal_position": 1, "data_type": "int", "nullable": False},
            {
                "column_name": "order_id",
                "ordinal_position": 2,
                "data_type": "int",
                "nullable": False,
            },
            {
                "column_name": "status",
                "ordinal_position": 3,
                "data_type": "varchar",
                "nullable": False,
            },
        ],
        "indexes": [
            {
                "index_name": "PRIMARY",
                "columns": ["id"],
                "is_unique": True,
                "is_primary": True,
                "index_type": "btree",
            },
        ],
        "primary_key": ["id"],
        "foreign_keys": [
            {
                "constraint_name": "fk_ship_order",
                "columns": ["order_id"],
                "referenced_table": "orders",
                "referenced_columns": ["id"],
            },
        ],
    }
    queries = [
        {
            "query_id": "agg-001",
            "query_text": "SELECT o.*, oi.product_name FROM orders o JOIN order_items oi ON o.id = oi.order_id WHERE o.id = ?",
            "query_type": "SELECT",
            "frequency_per_hour": 2000,
            "calls_per_second": 0.6,
            "tables_accessed": ["app.orders", "app.order_items"],
            "rows_returned_avg": 5.0,
            "filter_columns": ["id"],
            "has_joins": True,
            "join_count": 1,
            "execution_time_ms_avg": 3.0,
        },
        {
            "query_id": "agg-002",
            "query_text": "SELECT o.id, s.status FROM orders o JOIN shipments s ON o.id = s.order_id WHERE o.id = ?",
            "query_type": "SELECT",
            "frequency_per_hour": 800,
            "calls_per_second": 0.2,
            "tables_accessed": ["app.orders", "app.shipments"],
            "rows_returned_avg": 1.0,
            "filter_columns": ["id"],
            "has_joins": True,
            "join_count": 1,
            "execution_time_ms_avg": 2.0,
        },
    ]
    return _base_output("ddb-aggregate-test", [orders, order_items, shipments], queries)


# ---------------------------------------------------------------------------
# GSI candidate fixture (high-frequency non-PK filter columns)
# ---------------------------------------------------------------------------


def get_gsi_candidate_fixture() -> dict[str, Any]:
    """1 table with high-frequency queries on non-PK columns (status, region)."""
    table = {
        "table_id": "app.orders",
        "table_name": "orders",
        "row_count": 500000,
        "size_mb": 100.0,
        "columns": [
            {"column_name": "id", "ordinal_position": 1, "data_type": "int", "nullable": False},
            {
                "column_name": "status",
                "ordinal_position": 2,
                "data_type": "varchar",
                "nullable": False,
            },
            {
                "column_name": "region",
                "ordinal_position": 3,
                "data_type": "varchar",
                "nullable": False,
            },
            {
                "column_name": "order_date",
                "ordinal_position": 4,
                "data_type": "datetime",
                "nullable": False,
            },
            {
                "column_name": "total",
                "ordinal_position": 5,
                "data_type": "decimal",
                "nullable": False,
            },
        ],
        "indexes": [
            {
                "index_name": "PRIMARY",
                "columns": ["id"],
                "is_unique": True,
                "is_primary": True,
                "index_type": "btree",
            },
            {
                "index_name": "idx_status",
                "columns": ["status"],
                "is_unique": False,
                "is_primary": False,
                "index_type": "btree",
            },
        ],
        "primary_key": ["id"],
    }
    queries = [
        {
            "query_id": "gsi-001",
            "query_text": "SELECT * FROM orders WHERE status = ? ORDER BY order_date DESC",
            "query_type": "SELECT",
            "frequency_per_hour": 300,
            "calls_per_second": 0.08,
            "tables_accessed": ["app.orders"],
            "rows_returned_avg": 50.0,
            "filter_columns": ["status"],
            "sort_columns": ["order_date"],
            "execution_time_ms_avg": 10.0,
        },
        {
            "query_id": "gsi-002",
            "query_text": "SELECT * FROM orders WHERE region = ? AND status = ?",
            "query_type": "SELECT",
            "frequency_per_hour": 200,
            "calls_per_second": 0.06,
            "tables_accessed": ["app.orders"],
            "rows_returned_avg": 30.0,
            "filter_columns": ["region", "status"],
            "sort_columns": [],
            "execution_time_ms_avg": 8.0,
        },
    ]
    return _base_output("ddb-gsi-test", [table], queries)


# ---------------------------------------------------------------------------
# Sparse index fixture (nullable low-population column)
# ---------------------------------------------------------------------------


def get_sparse_index_fixture() -> dict[str, Any]:
    """1 table with a nullable column that has low population (< 30%)."""
    table = {
        "table_id": "app.tasks",
        "table_name": "tasks",
        "row_count": 100000,
        "size_mb": 20.0,
        "columns": [
            {"column_name": "id", "ordinal_position": 1, "data_type": "int", "nullable": False},
            {
                "column_name": "title",
                "ordinal_position": 2,
                "data_type": "varchar",
                "nullable": False,
            },
            {
                "column_name": "assigned_to",
                "ordinal_position": 3,
                "data_type": "varchar",
                "nullable": True,
            },
        ],
        "indexes": [
            {
                "index_name": "PRIMARY",
                "columns": ["id"],
                "is_unique": True,
                "is_primary": True,
                "index_type": "btree",
            },
        ],
        "primary_key": ["id"],
        "column_stats": {
            "assigned_to": {"null_fraction": 0.80, "distinct_count": 50},
        },
    }
    queries = [
        {
            "query_id": "sparse-001",
            "query_text": "SELECT * FROM tasks WHERE assigned_to = ?",
            "query_type": "SELECT",
            "frequency_per_hour": 500,
            "calls_per_second": 0.14,
            "tables_accessed": ["app.tasks"],
            "rows_returned_avg": 20.0,
            "filter_columns": ["assigned_to"],
            "execution_time_ms_avg": 5.0,
        },
    ]
    return _base_output("ddb-sparse-test", [table], queries)


# ---------------------------------------------------------------------------
# Junction table fixture (classic M:N)
# ---------------------------------------------------------------------------


def get_junction_table_fixture() -> dict[str, Any]:
    """3 tables: students, courses, and a student_courses junction table."""
    students = {
        "table_id": "app.students",
        "table_name": "students",
        "row_count": 5000,
        "size_mb": 2.0,
        "columns": [
            {"column_name": "id", "ordinal_position": 1, "data_type": "int", "nullable": False},
            {
                "column_name": "name",
                "ordinal_position": 2,
                "data_type": "varchar",
                "nullable": False,
            },
        ],
        "indexes": [
            {
                "index_name": "PRIMARY",
                "columns": ["id"],
                "is_unique": True,
                "is_primary": True,
                "index_type": "btree",
            },
        ],
        "primary_key": ["id"],
    }
    courses = {
        "table_id": "app.courses",
        "table_name": "courses",
        "row_count": 200,
        "size_mb": 0.5,
        "columns": [
            {"column_name": "id", "ordinal_position": 1, "data_type": "int", "nullable": False},
            {
                "column_name": "title",
                "ordinal_position": 2,
                "data_type": "varchar",
                "nullable": False,
            },
        ],
        "indexes": [
            {
                "index_name": "PRIMARY",
                "columns": ["id"],
                "is_unique": True,
                "is_primary": True,
                "index_type": "btree",
            },
        ],
        "primary_key": ["id"],
    }
    student_courses = {
        "table_id": "app.student_courses",
        "table_name": "student_courses",
        "row_count": 25000,
        "size_mb": 3.0,
        "columns": [
            {
                "column_name": "student_id",
                "ordinal_position": 1,
                "data_type": "int",
                "nullable": False,
            },
            {
                "column_name": "course_id",
                "ordinal_position": 2,
                "data_type": "int",
                "nullable": False,
            },
        ],
        "indexes": [
            {
                "index_name": "PRIMARY",
                "columns": ["student_id", "course_id"],
                "is_unique": True,
                "is_primary": True,
                "index_type": "btree",
            },
        ],
        "primary_key": ["student_id", "course_id"],
        "foreign_keys": [
            {
                "constraint_name": "fk_student",
                "columns": ["student_id"],
                "referenced_table": "students",
                "referenced_columns": ["id"],
            },
            {
                "constraint_name": "fk_course",
                "columns": ["course_id"],
                "referenced_table": "courses",
                "referenced_columns": ["id"],
            },
        ],
    }
    queries = [
        {
            "query_id": "jt-001",
            "query_text": "SELECT c.title FROM student_courses sc JOIN courses c ON sc.course_id = c.id WHERE sc.student_id = ?",
            "query_type": "SELECT",
            "frequency_per_hour": 1500,
            "calls_per_second": 0.4,
            "tables_accessed": ["app.student_courses", "app.courses"],
            "rows_returned_avg": 5.0,
            "filter_columns": ["student_id"],
            "has_joins": True,
            "join_count": 1,
            "execution_time_ms_avg": 2.0,
        },
    ]
    return _base_output("ddb-junction-test", [students, courses, student_courses], queries)


# ---------------------------------------------------------------------------
# Adjacency list fixture (self-referential FK: employee → manager)
# ---------------------------------------------------------------------------


def get_adjacency_list_fixture() -> dict[str, Any]:
    """1 table with a self-referential FK (employee.manager_id → employee.id)."""
    employees = {
        "table_id": "app.employees",
        "table_name": "employees",
        "row_count": 10000,
        "size_mb": 5.0,
        "columns": [
            {"column_name": "id", "ordinal_position": 1, "data_type": "int", "nullable": False},
            {
                "column_name": "name",
                "ordinal_position": 2,
                "data_type": "varchar",
                "nullable": False,
            },
            {
                "column_name": "manager_id",
                "ordinal_position": 3,
                "data_type": "int",
                "nullable": True,
            },
        ],
        "indexes": [
            {
                "index_name": "PRIMARY",
                "columns": ["id"],
                "is_unique": True,
                "is_primary": True,
                "index_type": "btree",
            },
        ],
        "primary_key": ["id"],
        "foreign_keys": [
            {
                "constraint_name": "fk_manager",
                "columns": ["manager_id"],
                "referenced_table": "employees",
                "referenced_columns": ["id"],
            },
        ],
    }
    queries = [
        {
            "query_id": "adj-001",
            "query_text": "SELECT e.name, m.name AS manager FROM employees e LEFT JOIN employees m ON e.manager_id = m.id WHERE e.id = ?",
            "query_type": "SELECT",
            "frequency_per_hour": 500,
            "calls_per_second": 0.14,
            "tables_accessed": ["app.employees"],
            "rows_returned_avg": 1.0,
            "filter_columns": ["id"],
            "has_joins": True,
            "join_count": 1,
            "execution_time_ms_avg": 1.5,
        },
    ]
    return _base_output("ddb-adjacency-test", [employees], queries)


# ---------------------------------------------------------------------------
# Secondary index dominant fixture
# ---------------------------------------------------------------------------


def get_secondary_index_dominant_fixture() -> dict[str, Any]:
    """1 table where most queries use a secondary index (idx_category) instead of PK."""
    products = {
        "table_id": "app.products",
        "table_name": "products",
        "row_count": 100000,
        "size_mb": 50.0,
        "columns": [
            {"column_name": "id", "ordinal_position": 1, "data_type": "int", "nullable": False},
            {
                "column_name": "category_id",
                "ordinal_position": 2,
                "data_type": "int",
                "nullable": False,
            },
            {
                "column_name": "name",
                "ordinal_position": 3,
                "data_type": "varchar",
                "nullable": False,
            },
            {
                "column_name": "price",
                "ordinal_position": 4,
                "data_type": "decimal",
                "nullable": False,
            },
        ],
        "indexes": [
            {
                "index_name": "PRIMARY",
                "columns": ["id"],
                "is_unique": True,
                "is_primary": True,
                "index_type": "btree",
            },
            {
                "index_name": "idx_category",
                "columns": ["category_id"],
                "is_unique": False,
                "is_primary": False,
                "index_type": "btree",
            },
        ],
        "primary_key": ["id"],
        "foreign_keys": [],
    }
    queries = [
        {
            "query_id": "sid-001",
            "query_text": "SELECT * FROM products WHERE category_id = ?",
            "query_type": "SELECT",
            "frequency_per_hour": 5000,
            "calls_per_second": 1.4,
            "tables_accessed": ["app.products"],
            "rows_returned_avg": 50.0,
            "filter_columns": ["category_id"],
            "sort_columns": [],
            "execution_time_ms_avg": 3.0,
        },
        {
            "query_id": "sid-002",
            "query_text": "SELECT * FROM products WHERE category_id = ? ORDER BY price",
            "query_type": "SELECT",
            "frequency_per_hour": 3000,
            "calls_per_second": 0.8,
            "tables_accessed": ["app.products"],
            "rows_returned_avg": 50.0,
            "filter_columns": ["category_id"],
            "sort_columns": ["price"],
            "execution_time_ms_avg": 4.0,
        },
        {
            "query_id": "sid-003",
            "query_text": "SELECT * FROM products WHERE id = ?",
            "query_type": "SELECT",
            "frequency_per_hour": 1000,
            "calls_per_second": 0.3,
            "tables_accessed": ["app.products"],
            "rows_returned_avg": 1.0,
            "filter_columns": ["id"],
            "sort_columns": [],
            "execution_time_ms_avg": 0.5,
        },
    ]
    return _base_output("ddb-sec-idx-dominant-test", [products], queries)
