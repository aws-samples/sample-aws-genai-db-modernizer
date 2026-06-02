"""
Per-pattern fixtures for DocumentDB analysis testing.

Each function returns a minimal CollectorOutputContract-compliant dict
designed to trigger exactly ONE specific DocumentDB pattern (or anti-pattern).
"""

from typing import Any


def _base(job_id: str, tables: list[dict], queries: list[dict]) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "database_schema": {"tables": tables},
        "queries": {"query_patterns": queries},
    }


def _table(
    tid: str,
    name: str,
    row_count: int = 10000,
    size_mb: float = 50.0,
    columns: list[dict] | None = None,
    pk: list[str] | None = None,
    fks: list[dict] | None = None,
) -> dict:
    return {
        "table_id": tid,
        "table_name": name,
        "row_count": row_count,
        "size_mb": size_mb,
        "columns": columns
        or [{"column_name": "id", "ordinal_position": 1, "data_type": "int", "nullable": False}],
        "indexes": [
            {
                "index_name": "PRIMARY",
                "columns": pk or ["id"],
                "is_unique": True,
                "is_primary": True,
                "index_type": "btree",
            }
        ],
        "primary_key": pk or ["id"],
        "foreign_keys": fks or [],
    }


# ---------------------------------------------------------------------------
# Pattern fixtures
# ---------------------------------------------------------------------------


def get_content_management_fixture() -> dict[str, Any]:
    """Polymorphic content table with sparse nullable columns + type discriminator."""
    cols = [
        {"column_name": "id", "ordinal_position": 1, "data_type": "int", "nullable": False},
        {"column_name": "type", "ordinal_position": 2, "data_type": "varchar", "nullable": False},
        {"column_name": "title", "ordinal_position": 3, "data_type": "varchar", "nullable": False},
        {"column_name": "body", "ordinal_position": 4, "data_type": "text", "nullable": True},
        {
            "column_name": "video_url",
            "ordinal_position": 5,
            "data_type": "varchar",
            "nullable": True,
        },
        {
            "column_name": "image_path",
            "ordinal_position": 6,
            "data_type": "varchar",
            "nullable": True,
        },
        {
            "column_name": "audio_url",
            "ordinal_position": 7,
            "data_type": "varchar",
            "nullable": True,
        },
        {
            "column_name": "duration_sec",
            "ordinal_position": 8,
            "data_type": "int",
            "nullable": True,
        },
        {"column_name": "author_id", "ordinal_position": 9, "data_type": "int", "nullable": True},
        {"column_name": "tags", "ordinal_position": 10, "data_type": "varchar", "nullable": True},
    ]
    t = _table("app.content", "content", 50000, 200.0, cols)
    q = {
        "query_id": "cm-001",
        "query_text": "SELECT * FROM content WHERE type = ? AND id = ?",
        "query_type": "SELECT",
        "frequency_per_hour": 2000,
        "calls_per_second": 0.6,
        "tables_accessed": ["app.content"],
        "rows_returned_avg": 1.0,
        "filter_columns": ["type", "id"],
    }
    return _base("docdb-cm-test", [t], [q])


def get_product_catalog_fixture() -> dict[str, Any]:
    """Wide table with many columns, some nullable, and JSON column."""
    cols = [
        {"column_name": "id", "ordinal_position": 1, "data_type": "int", "nullable": False},
        {"column_name": "name", "ordinal_position": 2, "data_type": "varchar", "nullable": False},
        {
            "column_name": "category",
            "ordinal_position": 3,
            "data_type": "varchar",
            "nullable": False,
        },
        {"column_name": "price", "ordinal_position": 4, "data_type": "decimal", "nullable": False},
        {
            "column_name": "description",
            "ordinal_position": 5,
            "data_type": "text",
            "nullable": True,
        },
        {"column_name": "weight", "ordinal_position": 6, "data_type": "decimal", "nullable": True},
        {"column_name": "color", "ordinal_position": 7, "data_type": "varchar", "nullable": True},
        {"column_name": "size", "ordinal_position": 8, "data_type": "varchar", "nullable": True},
        {"column_name": "brand", "ordinal_position": 9, "data_type": "varchar", "nullable": True},
        {
            "column_name": "material",
            "ordinal_position": 10,
            "data_type": "varchar",
            "nullable": True,
        },
        {
            "column_name": "warranty",
            "ordinal_position": 11,
            "data_type": "varchar",
            "nullable": True,
        },
        {
            "column_name": "dimensions",
            "ordinal_position": 12,
            "data_type": "varchar",
            "nullable": True,
        },
        {"column_name": "sku", "ordinal_position": 13, "data_type": "varchar", "nullable": False},
        {
            "column_name": "barcode",
            "ordinal_position": 14,
            "data_type": "varchar",
            "nullable": True,
        },
        {
            "column_name": "attributes",
            "ordinal_position": 15,
            "data_type": "json",
            "nullable": True,
        },
        {"column_name": "images", "ordinal_position": 16, "data_type": "json", "nullable": True},
    ]
    t = _table("app.products", "products", 25000, 45.0, cols)
    q = {
        "query_id": "pc-001",
        "query_text": "SELECT * FROM products WHERE category = ? AND price BETWEEN ? AND ?",
        "query_type": "SELECT",
        "frequency_per_hour": 5000,
        "calls_per_second": 1.4,
        "tables_accessed": ["app.products"],
        "rows_returned_avg": 50.0,
    }
    return _base("docdb-pc-test", [t], [q])


def get_polymorphic_data_fixture() -> dict[str, Any]:
    """Table with >30% nullable columns (sparse)."""
    cols = [
        {"column_name": "id", "ordinal_position": 1, "data_type": "int", "nullable": False},
        {
            "column_name": "entity_type",
            "ordinal_position": 2,
            "data_type": "varchar",
            "nullable": False,
        },
        {"column_name": "name", "ordinal_position": 3, "data_type": "varchar", "nullable": False},
        {"column_name": "attr1", "ordinal_position": 4, "data_type": "varchar", "nullable": True},
        {"column_name": "attr2", "ordinal_position": 5, "data_type": "varchar", "nullable": True},
        {"column_name": "attr3", "ordinal_position": 6, "data_type": "int", "nullable": True},
        {"column_name": "attr4", "ordinal_position": 7, "data_type": "decimal", "nullable": True},
    ]
    t = _table("app.entities", "entities", 100000, 80.0, cols)
    q = {
        "query_id": "poly-001",
        "query_text": "SELECT * FROM entities WHERE entity_type = ? AND attr1 = ?",
        "query_type": "SELECT",
        "frequency_per_hour": 3000,
        "calls_per_second": 0.8,
        "tables_accessed": ["app.entities"],
        "rows_returned_avg": 10.0,
    }
    return _base("docdb-poly-test", [t], [q])


def get_nested_document_fixture() -> dict[str, Any]:
    """Parent-child with FK, simple JOIN query."""
    parent = _table(
        "app.orders",
        "orders",
        200000,
        80.0,
        [
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
    )
    child = _table(
        "app.order_items",
        "order_items",
        800000,
        120.0,
        [
            {"column_name": "id", "ordinal_position": 1, "data_type": "int", "nullable": False},
            {
                "column_name": "order_id",
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
                "column_name": "quantity",
                "ordinal_position": 4,
                "data_type": "int",
                "nullable": False,
            },
        ],
        fks=[{"column": "order_id", "referenced_table": "orders", "referenced_column": "id"}],
    )
    q = {
        "query_id": "nd-001",
        "query_text": "SELECT o.*, oi.* FROM orders o JOIN order_items oi ON o.id = oi.order_id WHERE o.id = ?",
        "query_type": "SELECT",
        "frequency_per_hour": 4000,
        "calls_per_second": 1.1,
        "tables_accessed": ["app.orders", "app.order_items"],
        "rows_returned_avg": 4.0,
        "has_joins": True,
        "join_count": 1,
    }
    return _base("docdb-nd-test", [parent, child], [q])


def get_aggregation_pipeline_fixture() -> dict[str, Any]:
    """GROUP BY + SUM/COUNT without JOINs."""
    t = _table(
        "app.sales",
        "sales",
        500000,
        200.0,
        [
            {"column_name": "id", "ordinal_position": 1, "data_type": "int", "nullable": False},
            {
                "column_name": "product_id",
                "ordinal_position": 2,
                "data_type": "int",
                "nullable": False,
            },
            {
                "column_name": "amount",
                "ordinal_position": 3,
                "data_type": "decimal",
                "nullable": False,
            },
            {
                "column_name": "sale_date",
                "ordinal_position": 4,
                "data_type": "date",
                "nullable": False,
            },
        ],
    )
    q = {
        "query_id": "agg-001",
        "query_text": "SELECT product_id, SUM(amount), COUNT(*) FROM sales GROUP BY product_id",
        "query_type": "SELECT",
        "frequency_per_hour": 500,
        "calls_per_second": 0.14,
        "tables_accessed": ["app.sales"],
        "rows_returned_avg": 100.0,
    }
    return _base("docdb-agg-test", [t], [q])


def get_flexible_schema_fixture() -> dict[str, Any]:
    """Table with JSON column and JSON_EXTRACT in query."""
    cols = [
        {"column_name": "id", "ordinal_position": 1, "data_type": "int", "nullable": False},
        {"column_name": "data", "ordinal_position": 2, "data_type": "json", "nullable": True},
        {
            "column_name": "created_at",
            "ordinal_position": 3,
            "data_type": "datetime",
            "nullable": False,
        },
    ]
    t = _table("app.events", "events", 1000000, 500.0, cols)
    q = {
        "query_id": "flex-001",
        "query_text": "SELECT id, JSON_EXTRACT(data, '$.type') FROM events WHERE JSON_EXTRACT(data, '$.user_id') = ?",
        "query_type": "SELECT",
        "frequency_per_hour": 6000,
        "calls_per_second": 1.7,
        "tables_accessed": ["app.events"],
        "rows_returned_avg": 5.0,
    }
    return _base("docdb-flex-test", [t], [q])


def get_extended_reference_fixture() -> dict[str, Any]:
    """Large table joined with small lookup table."""
    big = _table("app.transactions", "transactions", 500000, 300.0)
    small = _table(
        "app.countries",
        "countries",
        200,
        0.1,
        [
            {"column_name": "id", "ordinal_position": 1, "data_type": "int", "nullable": False},
            {
                "column_name": "name",
                "ordinal_position": 2,
                "data_type": "varchar",
                "nullable": False,
            },
        ],
    )
    q = {
        "query_id": "er-001",
        "query_text": "SELECT t.*, c.name FROM transactions t JOIN countries c ON t.country_id = c.id WHERE t.id = ?",
        "query_type": "SELECT",
        "frequency_per_hour": 8000,
        "calls_per_second": 2.2,
        "tables_accessed": ["app.transactions", "app.countries"],
        "rows_returned_avg": 1.0,
        "has_joins": True,
        "join_count": 1,
    }
    return _base("docdb-er-test", [big, small], [q])


def get_write_time_aggregation_fixture() -> dict[str, Any]:
    """Multiple SUM/COUNT + JOIN — pre-compute at write time."""
    t1 = _table("app.payments", "payments", 1000000, 500.0)
    t2 = _table("app.settlements", "settlements", 50000, 20.0)
    q = {
        "query_id": "wta-001",
        "query_text": "SELECT s.id, SUM(p.amount), COUNT(p.id), MIN(p.date), MAX(p.date) FROM settlements s JOIN payments p ON s.id = p.settlement_id WHERE s.id = ? GROUP BY s.id",
        "query_type": "SELECT",
        "frequency_per_hour": 3000,
        "calls_per_second": 0.8,
        "tables_accessed": ["app.payments", "app.settlements"],
        "rows_returned_avg": 1.0,
        "has_joins": True,
        "join_count": 1,
    }
    return _base("docdb-wta-test", [t1, t2], [q])


# ---------------------------------------------------------------------------
# Anti-pattern fixtures
# ---------------------------------------------------------------------------


def get_cross_collection_joins_fixture() -> dict[str, Any]:
    """4+ table JOIN query."""
    tables = [_table(f"app.t{i}", f"t{i}") for i in range(4)]
    q = {
        "query_id": "ccj-001",
        "query_text": "SELECT * FROM t0 JOIN t1 ON t0.id = t1.t0_id JOIN t2 ON t1.id = t2.t1_id JOIN t3 ON t2.id = t3.t2_id",
        "query_type": "SELECT",
        "frequency_per_hour": 500,
        "calls_per_second": 0.14,
        "tables_accessed": ["app.t0", "app.t1", "app.t2", "app.t3"],
        "rows_returned_avg": 50.0,
        "has_joins": True,
        "join_count": 3,
    }
    return _base("docdb-ccj-test", tables, [q])


def get_graph_hierarchy_fixture() -> dict[str, Any]:
    """Self-referential FK table."""
    cols = [
        {"column_name": "id", "ordinal_position": 1, "data_type": "int", "nullable": False},
        {"column_name": "parent_id", "ordinal_position": 2, "data_type": "int", "nullable": True},
        {"column_name": "name", "ordinal_position": 3, "data_type": "varchar", "nullable": False},
    ]
    t = _table(
        "app.categories",
        "categories",
        5000,
        2.0,
        cols,
        fks=[{"column": "parent_id", "referenced_table": "categories", "referenced_column": "id"}],
    )
    q = {
        "query_id": "gh-001",
        "query_text": "WITH RECURSIVE tree AS (SELECT * FROM categories WHERE id = ? UNION ALL SELECT c.* FROM categories c JOIN tree t ON c.parent_id = t.id) SELECT * FROM tree",
        "query_type": "SELECT",
        "frequency_per_hour": 200,
        "calls_per_second": 0.06,
        "tables_accessed": ["app.categories"],
        "rows_returned_avg": 20.0,
        "has_joins": True,
        "join_count": 1,
    }
    return _base("docdb-gh-test", [t], [q])
