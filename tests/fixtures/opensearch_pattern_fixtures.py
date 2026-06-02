"""
Focused per-pattern fixtures for OpenSearch analysis testing.

Each function returns a minimal CollectorOutputContract-compliant dict
designed to trigger exactly ONE specific OpenSearch pattern (or anti-pattern).

Public helpers (base_metadata, base_table, base_query, wrap_fixture) allow
inline construction of ad-hoc fixtures in test files.
"""

from typing import Any

# ==========================================================================
# Public helpers for ad-hoc fixture construction
# ==========================================================================


def base_metadata(job_id: str = "os-test") -> dict[str, Any]:
    """Minimal top-level scaffold fields."""
    return {"job_id": job_id}


def base_table(
    table_id: str,
    table_name: str,
    row_count: int = 10000,
    size_mb: float = 50.0,
    columns: list[dict] | None = None,
    primary_key: list[str] | None = None,
    foreign_keys: list[dict] | None = None,
) -> dict[str, Any]:
    """Minimal table dict with sensible defaults."""
    if columns is None:
        columns = [
            {"column_name": "id", "ordinal_position": 1, "data_type": "bigint", "nullable": False},
            {
                "column_name": "name",
                "ordinal_position": 2,
                "data_type": "varchar",
                "nullable": True,
            },
        ]
    if primary_key is None:
        primary_key = ["id"]
    return {
        "table_id": table_id,
        "table_name": table_name,
        "row_count": row_count,
        "size_mb": size_mb,
        "columns": columns,
        "indexes": [
            {
                "index_name": "PRIMARY",
                "columns": primary_key,
                "is_unique": True,
                "is_primary": True,
                "index_type": "btree",
            }
        ],
        "primary_key": primary_key,
        "foreign_keys": foreign_keys or [],
    }


def base_query(
    query_id: str,
    query_text: str,
    query_type: str = "SELECT",
    tables: list[str] | None = None,
    calls_per_second: float = 1.0,
    execution_time_ms_avg: float = 5.0,
    rows_returned_avg: float = 10.0,
    has_joins: bool = False,
    join_count: int = 0,
    filter_columns: list[str] | None = None,
) -> dict[str, Any]:
    """Minimal query dict. tables must be provided explicitly — no silent override."""
    return {
        "query_id": query_id,
        "query_text": query_text,
        "query_type": query_type,
        "frequency_per_hour": int(calls_per_second * 3600),
        "calls_per_second": calls_per_second,
        "tables_accessed": tables if tables is not None else [],
        "rows_returned_avg": rows_returned_avg,
        "execution_time_ms_avg": execution_time_ms_avg,
        "has_joins": has_joins,
        "join_count": join_count,
        "filter_columns": filter_columns or [],
    }


def wrap_fixture(
    job_id: str,
    tables: list[dict],
    queries: list[dict],
) -> dict[str, Any]:
    """Assemble a CollectorOutputContract-compliant dict."""
    return {
        "job_id": job_id,
        "database_schema": {"tables": tables},
        "queries": {"query_patterns": queries},
    }


# ==========================================================================
# Search pattern fixtures
# ==========================================================================


def get_fulltext_search_fixture() -> dict[str, Any]:
    """Triggers os-01 (full-text-search) only. Uses tsvector/tsquery operators."""
    table = base_table(
        table_id="public.documents",
        table_name="documents",
        row_count=100000,
        size_mb=500.0,
        columns=[
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
                "column_name": "body",
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
    )
    queries = [
        base_query(
            query_id="ft-001",
            query_text=(  # nosemgrep: string-concat-in-list
                "SELECT id, title FROM documents "
                "WHERE search_vector @@ to_tsvector('english', $1) "
                "ORDER BY ts_rank(search_vector, to_tsvector('english', $1)) DESC"
            ),
            query_type="SELECT",
            tables=["public.documents"],
            calls_per_second=3.0,
            execution_time_ms_avg=12.0,
        ),
        base_query(
            query_id="ft-002",
            query_text=(  # nosemgrep: string-concat-in-list
                "SELECT * FROM documents " "WHERE search_vector @@ plainto_tsquery('english', $1)"
            ),
            query_type="SELECT",
            tables=["public.documents"],
            calls_per_second=1.5,
            execution_time_ms_avg=8.0,
        ),
    ]
    return wrap_fixture("os-fulltext-test", [table], queries)


def get_wildcard_search_fixture() -> dict[str, Any]:
    """Triggers os-02 (wildcard-search) only. Uses ILIKE pattern."""
    table = base_table(
        table_id="public.products",
        table_name="products",
        row_count=50000,
        size_mb=200.0,
        columns=[
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
        ],
    )
    queries = [
        base_query(
            query_id="wc-001",
            query_text="SELECT id, name FROM products WHERE name ILIKE $1 OR description ILIKE $1",
            query_type="SELECT",
            tables=["public.products"],
            calls_per_second=2.0,
            execution_time_ms_avg=25.0,
        ),
        base_query(
            query_id="wc-002",
            query_text="SELECT * FROM products WHERE description ILIKE '%wireless%'",
            query_type="SELECT",
            tables=["public.products"],
            calls_per_second=0.5,
            execution_time_ms_avg=40.0,
        ),
    ]
    return wrap_fixture("os-wildcard-test", [table], queries)


def get_regex_search_fixture() -> dict[str, Any]:
    """Triggers os-03 (regex-search) only. Uses PostgreSQL ~ operator."""
    table = base_table(
        table_id="public.user_profiles",
        table_name="user_profiles",
        row_count=200000,
        size_mb=150.0,
        columns=[
            {
                "column_name": "id",
                "ordinal_position": 1,
                "data_type": "bigint",
                "nullable": False,
            },
            {
                "column_name": "username",
                "ordinal_position": 2,
                "data_type": "varchar",
                "nullable": False,
            },
            {
                "column_name": "bio",
                "ordinal_position": 3,
                "data_type": "text",
                "nullable": True,
            },
        ],
    )
    queries = [
        base_query(
            query_id="rx-001",
            query_text="SELECT * FROM user_profiles WHERE username ~ $1",
            query_type="SELECT",
            tables=["public.user_profiles"],
            calls_per_second=1.0,
            execution_time_ms_avg=15.0,
        ),
        base_query(
            query_id="rx-002",
            query_text="SELECT id, username FROM user_profiles WHERE bio ~* $1",
            query_type="SELECT",
            tables=["public.user_profiles"],
            calls_per_second=0.3,
            execution_time_ms_avg=30.0,
        ),
    ]
    return wrap_fixture("os-regex-test", [table], queries)


def get_fuzzy_search_fixture() -> dict[str, Any]:
    """Triggers os-04 (fuzzy-search) only. Uses pg_trgm similarity()."""
    table = base_table(
        table_id="public.knowledge_base",
        table_name="knowledge_base",
        row_count=10000,
        size_mb=100.0,
        columns=[
            {
                "column_name": "id",
                "ordinal_position": 1,
                "data_type": "bigint",
                "nullable": False,
            },
            {
                "column_name": "title",
                "ordinal_position": 2,
                "data_type": "varchar",
                "nullable": False,
            },
            {
                "column_name": "content",
                "ordinal_position": 3,
                "data_type": "text",
                "nullable": True,
            },
        ],
    )
    queries = [
        base_query(
            query_id="fz-001",
            query_text=(  # nosemgrep: string-concat-in-list
                "SELECT id, title, similarity(title, $1) AS score "
                "FROM knowledge_base "
                "WHERE similarity(title, $1) > 0.3 "
                "ORDER BY score DESC LIMIT 10"
            ),
            query_type="SELECT",
            tables=["public.knowledge_base"],
            calls_per_second=1.5,
            execution_time_ms_avg=20.0,
        ),
    ]
    return wrap_fixture("os-fuzzy-test", [table], queries)


# ==========================================================================
# Time-series pattern fixtures
# ==========================================================================


def get_time_range_fixture() -> dict[str, Any]:
    """Triggers os-05 (time-range-query) only. Timestamp range with BETWEEN."""
    table = base_table(
        table_id="public.sensor_readings",
        table_name="sensor_readings",
        row_count=5000000,
        size_mb=1200.0,
        columns=[
            {
                "column_name": "id",
                "ordinal_position": 1,
                "data_type": "bigint",
                "nullable": False,
            },
            {
                "column_name": "sensor_id",
                "ordinal_position": 2,
                "data_type": "int",
                "nullable": False,
            },
            {
                "column_name": "value",
                "ordinal_position": 3,
                "data_type": "float",
                "nullable": False,
            },
            {
                "column_name": "event_time",
                "ordinal_position": 4,
                "data_type": "timestamp",
                "nullable": False,
            },
        ],
    )
    queries = [
        base_query(
            query_id="tr-001",
            query_text=(  # nosemgrep: string-concat-in-list
                "SELECT * FROM sensor_readings "
                "WHERE sensor_id = $1 AND event_time BETWEEN $2 AND $3 "
                "ORDER BY event_time DESC"
            ),
            query_type="SELECT",
            tables=["public.sensor_readings"],
            calls_per_second=2.0,
            execution_time_ms_avg=30.0,
            filter_columns=["sensor_id", "event_time"],
        ),
        base_query(
            query_id="tr-002",
            query_text=(  # nosemgrep: string-concat-in-list
                "SELECT * FROM sensor_readings " "WHERE event_time >= $1 AND event_time <= $2"
            ),
            query_type="SELECT",
            tables=["public.sensor_readings"],
            calls_per_second=1.0,
            execution_time_ms_avg=50.0,
            filter_columns=["event_time"],
        ),
    ]
    return wrap_fixture("os-time-range-test", [table], queries)


def get_time_aggregation_fixture() -> dict[str, Any]:
    """Triggers os-06 (time-aggregation) only. Uses DATE_TRUNC aggregation."""
    table = base_table(
        table_id="public.application_metrics",
        table_name="application_metrics",
        row_count=2000000,
        size_mb=800.0,
        columns=[
            {
                "column_name": "id",
                "ordinal_position": 1,
                "data_type": "bigint",
                "nullable": False,
            },
            {
                "column_name": "service",
                "ordinal_position": 2,
                "data_type": "varchar",
                "nullable": False,
            },
            {
                "column_name": "metric_value",
                "ordinal_position": 3,
                "data_type": "float",
                "nullable": False,
            },
            {
                "column_name": "recorded_at",
                "ordinal_position": 4,
                "data_type": "timestamp",
                "nullable": False,
            },
        ],
    )
    queries = [
        base_query(
            query_id="ta-001",
            query_text=(  # nosemgrep: string-concat-in-list
                "SELECT date_trunc('hour', recorded_at) AS hour, "
                "service, AVG(metric_value) AS avg_val "
                "FROM application_metrics "
                "GROUP BY 1, 2 ORDER BY 1 DESC"
            ),
            query_type="SELECT",
            tables=["public.application_metrics"],
            calls_per_second=0.5,
            execution_time_ms_avg=150.0,
        ),
    ]
    return wrap_fixture("os-time-agg-test", [table], queries)


def get_high_ingest_fixture() -> dict[str, Any]:
    """Triggers os-07 (high-ingest) only. INSERT at CPS=15 with log-like table name."""
    table = base_table(
        table_id="public.application_log",
        table_name="application_log",
        row_count=10000000,
        size_mb=3000.0,
        columns=[
            {
                "column_name": "id",
                "ordinal_position": 1,
                "data_type": "bigint",
                "nullable": False,
            },
            {
                "column_name": "level",
                "ordinal_position": 2,
                "data_type": "varchar",
                "nullable": False,
            },
            {
                "column_name": "message",
                "ordinal_position": 3,
                "data_type": "text",
                "nullable": False,
            },
            {
                "column_name": "log_time",
                "ordinal_position": 4,
                "data_type": "timestamp",
                "nullable": False,
            },
        ],
    )
    queries = [
        base_query(
            query_id="hi-001",
            query_text=(
                "INSERT INTO application_log (level, message, log_time) VALUES ($1, $2, NOW())"
            ),
            query_type="INSERT",
            tables=["public.application_log"],
            calls_per_second=15.0,
            execution_time_ms_avg=1.0,
            rows_returned_avg=0,
        ),
    ]
    return wrap_fixture("os-high-ingest-test", [table], queries)


# ==========================================================================
# Anti-pattern fixtures
# ==========================================================================


def get_joins_fixture() -> dict[str, Any]:
    """Triggers os-ap-01 (multi-index-joins). Query with join_count >= 2."""
    table_a = base_table(
        table_id="public.orders",
        table_name="orders",
        row_count=500000,
        size_mb=300.0,
        columns=[
            {
                "column_name": "id",
                "ordinal_position": 1,
                "data_type": "bigint",
                "nullable": False,
            },
            {
                "column_name": "customer_id",
                "ordinal_position": 2,
                "data_type": "int",
                "nullable": False,
            },
            {
                "column_name": "product_id",
                "ordinal_position": 3,
                "data_type": "int",
                "nullable": False,
            },
            {
                "column_name": "amount",
                "ordinal_position": 4,
                "data_type": "decimal",
                "nullable": False,
            },
        ],
        foreign_keys=[
            {"column": "customer_id", "referenced_table": "customers", "referenced_column": "id"},
            {"column": "product_id", "referenced_table": "products", "referenced_column": "id"},
        ],
    )
    table_b = base_table(
        table_id="public.customers",
        table_name="customers",
        row_count=100000,
        size_mb=50.0,
    )
    table_c = base_table(
        table_id="public.products",
        table_name="products",
        row_count=10000,
        size_mb=20.0,
    )
    queries = [
        base_query(
            query_id="jn-001",
            query_text=(  # nosemgrep: string-concat-in-list
                "SELECT o.id, c.name, p.name "
                "FROM orders o "
                "JOIN customers c ON o.customer_id = c.id "
                "JOIN products p ON o.product_id = p.id "
                "WHERE o.id = $1"
            ),
            query_type="SELECT",
            tables=["public.orders", "public.customers", "public.products"],
            calls_per_second=2.0,
            execution_time_ms_avg=10.0,
            has_joins=True,
            join_count=2,
        ),
    ]
    return wrap_fixture("os-joins-test", [table_a, table_b, table_c], queries)


def get_transactions_fixture() -> dict[str, Any]:
    """Triggers os-ap-02 (acid-transactions). UPDATE/DELETE > 1 CPS."""
    table = base_table(
        table_id="public.inventory",
        table_name="inventory",
        row_count=50000,
        size_mb=100.0,
        columns=[
            {
                "column_name": "id",
                "ordinal_position": 1,
                "data_type": "bigint",
                "nullable": False,
            },
            {
                "column_name": "product_id",
                "ordinal_position": 2,
                "data_type": "int",
                "nullable": False,
            },
            {
                "column_name": "quantity",
                "ordinal_position": 3,
                "data_type": "int",
                "nullable": False,
            },
            {
                "column_name": "updated_at",
                "ordinal_position": 4,
                "data_type": "timestamp",
                "nullable": False,
            },
        ],
    )
    queries = [
        base_query(
            query_id="tx-001",
            query_text="UPDATE inventory SET quantity = quantity - $1 WHERE product_id = $2",
            query_type="UPDATE",
            tables=["public.inventory"],
            calls_per_second=5.0,
            execution_time_ms_avg=2.0,
            filter_columns=["product_id"],
        ),
        base_query(
            query_id="tx-002",
            query_text="DELETE FROM inventory WHERE product_id = $1 AND quantity = 0",
            query_type="DELETE",
            tables=["public.inventory"],
            calls_per_second=2.0,
            execution_time_ms_avg=3.0,
            filter_columns=["product_id"],
        ),
    ]
    return wrap_fixture("os-transactions-test", [table], queries)


def get_audit_timestamp_fixture() -> dict[str, Any]:
    """Triggers os-ap-03 (audit-columns-only). Timestamp used only in equality check.

    Query text includes a timestamp keyword AND an equality operator (= $2), but
    NO range operators (between, >=, <=). This lets the detector identify equality-only
    timestamp usage vs range usage per table.
    """
    table = base_table(
        table_id="public.user_events",
        table_name="user_events",
        row_count=1000000,
        size_mb=400.0,
        columns=[
            {
                "column_name": "id",
                "ordinal_position": 1,
                "data_type": "bigint",
                "nullable": False,
            },
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
                "data_type": "timestamp",
                "nullable": False,
            },
        ],
    )
    queries = [
        base_query(
            query_id="at-001",
            # Timestamp keyword present in query, used in equality only (no between/>=/<= range)
            query_text="SELECT * FROM user_events WHERE id = $1 AND created_at = $2",
            query_type="SELECT",
            tables=["public.user_events"],
            calls_per_second=1.0,
            execution_time_ms_avg=5.0,
            filter_columns=["id", "created_at"],
        ),
        base_query(
            query_id="at-002",
            query_text="SELECT action FROM user_events WHERE user_id = $1 AND created_at = $2",
            query_type="SELECT",
            tables=["public.user_events"],
            calls_per_second=0.5,
            execution_time_ms_avg=3.0,
            filter_columns=["user_id", "created_at"],
        ),
    ]
    return wrap_fixture("os-audit-timestamp-test", [table], queries)
