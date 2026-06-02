"""
Vertical fixture: SaaS platform with 4 tables representing the full spread of
OpenSearch workload classifications.

Tables:
  1. public.products         — 50k rows, 200MB, ILIKE query          → SEARCH
  2. public.knowledge_base   — 10k rows, 100MB, tsvector/tsquery     → SEARCH
  3. public.application_logs — 5M rows,  2GB, INSERT CPS=25, time    → TIMESERIES
  4. public.orders           — 500k rows, 300MB, PK lookup only       → NOT_SUITABLE

application_logs meets ALL 3 time-series criteria:
  - Timestamp range: WHERE log_time BETWEEN + DATE_TRUNC aggregation
  - High ingest: INSERT at CPS=25 (well above threshold of 10)
  - Staleness: "log" in table name, "log_time" column name
"""

from __future__ import annotations

from typing import Any


def get_saas_platform_collector_output() -> dict[str, Any]:
    """Return a CollectorOutputContract-compliant dict for a SaaS platform scenario."""

    # -----------------------------------------------------------------------
    # Tables
    # -----------------------------------------------------------------------

    table_products = {
        "table_id": "public.products",
        "table_name": "products",
        "row_count": 50_000,
        "size_mb": 200.0,
        "columns": [
            {
                "column_name": "id",
                "ordinal_position": 1,
                "data_type": "bigint",
                "nullable": False,
            },
            {
                "column_name": "name",
                "ordinal_position": 2,
                "data_type": "varchar",
                "nullable": False,
            },
            {
                "column_name": "description",
                "ordinal_position": 3,
                "data_type": "text",
                "nullable": True,
            },
            {
                "column_name": "price",
                "ordinal_position": 4,
                "data_type": "decimal",
                "nullable": False,
            },
            {
                "column_name": "category",
                "ordinal_position": 5,
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
            }
        ],
        "primary_key": ["id"],
        "foreign_keys": [],
    }

    table_knowledge_base = {
        "table_id": "public.knowledge_base",
        "table_name": "knowledge_base",
        "row_count": 10_000,
        "size_mb": 100.0,
        "columns": [
            {
                "column_name": "id",
                "ordinal_position": 1,
                "data_type": "bigint",
                "nullable": False,
            },
            {
                "column_name": "title",
                "ordinal_position": 2,
                "data_type": "text",
                "nullable": False,
            },
            {
                "column_name": "content",
                "ordinal_position": 3,
                "data_type": "text",
                "nullable": True,
            },
            {
                "column_name": "search_vector",
                "ordinal_position": 4,
                "data_type": "tsvector",
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
            }
        ],
        "primary_key": ["id"],
        "foreign_keys": [],
    }

    # application_logs: ALL 3 TS criteria
    #   - "log" in table name (staleness)
    #   - "log_time" column name (staleness)
    #   - INSERT CPS=25 (high ingest, threshold=10)
    #   - time-range query (log_time BETWEEN) + DATE_TRUNC aggregation
    table_application_logs = {
        "table_id": "public.application_logs",
        "table_name": "application_logs",
        "row_count": 5_000_000,
        "size_mb": 2048.0,
        "columns": [
            {
                "column_name": "id",
                "ordinal_position": 1,
                "data_type": "bigint",
                "nullable": False,
            },
            {
                "column_name": "log_time",
                "ordinal_position": 2,
                "data_type": "timestamp",
                "nullable": False,
            },
            {
                "column_name": "level",
                "ordinal_position": 3,
                "data_type": "varchar",
                "nullable": False,
            },
            {
                "column_name": "message",
                "ordinal_position": 4,
                "data_type": "text",
                "nullable": True,
            },
            {
                "column_name": "service_name",
                "ordinal_position": 5,
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
            {
                "index_name": "idx_app_logs_log_time",
                "columns": ["log_time"],
                "is_unique": False,
                "is_primary": False,
                "index_type": "btree",
            },
        ],
        "primary_key": ["id"],
        "foreign_keys": [],
    }

    table_orders = {
        "table_id": "public.orders",
        "table_name": "orders",
        "row_count": 500_000,
        "size_mb": 300.0,
        "columns": [
            {
                "column_name": "id",
                "ordinal_position": 1,
                "data_type": "bigint",
                "nullable": False,
            },
            {
                "column_name": "customer_id",
                "ordinal_position": 2,
                "data_type": "bigint",
                "nullable": False,
            },
            {
                "column_name": "total_amount",
                "ordinal_position": 3,
                "data_type": "decimal",
                "nullable": False,
            },
            {
                "column_name": "status",
                "ordinal_position": 4,
                "data_type": "varchar",
                "nullable": False,
            },
            {
                "column_name": "created_at",
                "ordinal_position": 5,
                "data_type": "timestamp",
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
                "column": "customer_id",
                "referenced_table": "customers",
                "referenced_column": "id",
            }
        ],
    }

    # -----------------------------------------------------------------------
    # Queries
    # -----------------------------------------------------------------------

    # products — ILIKE search → SEARCH
    q_products_ilike = {
        "query_id": "saas-prod-001",
        "query_text": "SELECT id, name, price FROM products WHERE name ILIKE $1 OR description ILIKE $1",
        "query_type": "SELECT",
        "frequency_per_hour": 7200,
        "calls_per_second": 2.0,
        "tables_accessed": ["public.products"],
        "rows_returned_avg": 15.0,
        "execution_time_ms_avg": 25.0,
        "has_joins": False,
        "join_count": 0,
        "filter_columns": ["name", "description"],
    }
    q_products_ilike2 = {
        "query_id": "saas-prod-002",
        "query_text": "SELECT * FROM products WHERE description ILIKE $1 ORDER BY name LIMIT 50",
        "query_type": "SELECT",
        "frequency_per_hour": 3600,
        "calls_per_second": 1.0,
        "tables_accessed": ["public.products"],
        "rows_returned_avg": 50.0,
        "execution_time_ms_avg": 35.0,
        "has_joins": False,
        "join_count": 0,
        "filter_columns": ["description"],
    }
    q_products_select = {
        "query_id": "saas-prod-003",
        "query_text": "SELECT * FROM products WHERE id = $1",
        "query_type": "SELECT",
        "frequency_per_hour": 18000,
        "calls_per_second": 5.0,
        "tables_accessed": ["public.products"],
        "rows_returned_avg": 1.0,
        "execution_time_ms_avg": 2.0,
        "has_joins": False,
        "join_count": 0,
        "filter_columns": ["id"],
    }

    # knowledge_base — tsvector/tsquery → SEARCH
    q_kb_fulltext = {
        "query_id": "saas-kb-001",
        "query_text": (
            "SELECT id, title FROM knowledge_base "
            "WHERE search_vector @@ to_tsvector('english', $1) "
            "ORDER BY ts_rank(search_vector, to_tsvector('english', $1)) DESC"
        ),
        "query_type": "SELECT",
        "frequency_per_hour": 5400,
        "calls_per_second": 1.5,
        "tables_accessed": ["public.knowledge_base"],
        "rows_returned_avg": 10.0,
        "execution_time_ms_avg": 18.0,
        "has_joins": False,
        "join_count": 0,
        "filter_columns": ["search_vector"],
    }
    q_kb_tsquery = {
        "query_id": "saas-kb-002",
        "query_text": (
            "SELECT * FROM knowledge_base " "WHERE search_vector @@ plainto_tsquery('english', $1)"
        ),
        "query_type": "SELECT",
        "frequency_per_hour": 1800,
        "calls_per_second": 0.5,
        "tables_accessed": ["public.knowledge_base"],
        "rows_returned_avg": 5.0,
        "execution_time_ms_avg": 12.0,
        "has_joins": False,
        "join_count": 0,
        "filter_columns": ["search_vector"],
    }

    # application_logs — INSERT CPS=25 + time-range BETWEEN + DATE_TRUNC → TIMESERIES
    q_logs_insert = {
        "query_id": "saas-logs-001",
        "query_text": (
            "INSERT INTO application_logs (log_time, level, message, service_name) "
            "VALUES ($1, $2, $3, $4)"
        ),
        "query_type": "INSERT",
        "frequency_per_hour": 90_000,
        "calls_per_second": 25.0,
        "tables_accessed": ["public.application_logs"],
        "rows_returned_avg": 0.0,
        "execution_time_ms_avg": 1.0,
        "has_joins": False,
        "join_count": 0,
        "filter_columns": [],
    }
    q_logs_time_range = {
        "query_id": "saas-logs-002",
        "query_text": (
            "SELECT * FROM application_logs "
            "WHERE service_name = $1 AND log_time BETWEEN $2 AND $3 "
            "ORDER BY log_time DESC LIMIT 1000"
        ),
        "query_type": "SELECT",
        "frequency_per_hour": 3600,
        "calls_per_second": 1.0,
        "tables_accessed": ["public.application_logs"],
        "rows_returned_avg": 100.0,
        "execution_time_ms_avg": 80.0,
        "has_joins": False,
        "join_count": 0,
        "filter_columns": ["service_name", "log_time"],
    }
    q_logs_aggregation = {
        "query_id": "saas-logs-003",
        "query_text": (
            "SELECT date_trunc('hour', log_time) AS hour, level, COUNT(*) AS cnt "
            "FROM application_logs "
            "WHERE log_time >= $1 AND log_time <= $2 "
            "GROUP BY 1, 2 ORDER BY 1 DESC"
        ),
        "query_type": "SELECT",
        "frequency_per_hour": 360,
        "calls_per_second": 0.1,
        "tables_accessed": ["public.application_logs"],
        "rows_returned_avg": 500.0,
        "execution_time_ms_avg": 250.0,
        "has_joins": False,
        "join_count": 0,
        "filter_columns": ["log_time"],
    }

    # orders — INSERT CPS=2 + PK lookup only → NOT_SUITABLE
    q_orders_insert = {
        "query_id": "saas-orders-001",
        "query_text": (
            "INSERT INTO orders (customer_id, total_amount, status, created_at) "
            "VALUES ($1, $2, $3, NOW())"
        ),
        "query_type": "INSERT",
        "frequency_per_hour": 7200,
        "calls_per_second": 2.0,
        "tables_accessed": ["public.orders"],
        "rows_returned_avg": 0.0,
        "execution_time_ms_avg": 3.0,
        "has_joins": False,
        "join_count": 0,
        "filter_columns": [],
    }
    q_orders_pk_lookup = {
        "query_id": "saas-orders-002",
        "query_text": "SELECT * FROM orders WHERE id = $1",
        "query_type": "SELECT",
        "frequency_per_hour": 18000,
        "calls_per_second": 5.0,
        "tables_accessed": ["public.orders"],
        "rows_returned_avg": 1.0,
        "execution_time_ms_avg": 2.0,
        "has_joins": False,
        "join_count": 0,
        "filter_columns": ["id"],
    }
    q_orders_by_customer = {
        "query_id": "saas-orders-003",
        "query_text": "SELECT * FROM orders WHERE customer_id = $1 ORDER BY created_at DESC",
        "query_type": "SELECT",
        "frequency_per_hour": 10800,
        "calls_per_second": 3.0,
        "tables_accessed": ["public.orders"],
        "rows_returned_avg": 20.0,
        "execution_time_ms_avg": 5.0,
        "has_joins": False,
        "join_count": 0,
        "filter_columns": ["customer_id"],
    }

    return {
        "job_id": "saas-platform-test",
        "database_schema": {
            "tables": [
                table_products,
                table_knowledge_base,
                table_application_logs,
                table_orders,
            ]
        },
        "queries": {
            "query_patterns": [
                q_products_ilike,
                q_products_ilike2,
                q_products_select,
                q_kb_fulltext,
                q_kb_tsquery,
                q_logs_insert,
                q_logs_time_range,
                q_logs_aggregation,
                q_orders_insert,
                q_orders_pk_lookup,
                q_orders_by_customer,
            ]
        },
    }
