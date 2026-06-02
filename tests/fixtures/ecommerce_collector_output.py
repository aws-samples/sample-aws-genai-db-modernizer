"""
Realistic e-commerce database collector output fixture.

Simulates a mid-size e-commerce platform running on MySQL 8.0 on RDS.
This fixture is fully CollectorOutputContract v3.0 compliant and contains
query patterns designed to trigger all implemented Redis analysis patterns:

  - Caching: High-frequency product and user lookups
  - Session stores: Session table queries by session_id/user_id
  - Leaderboards: Top-rated products, best sellers (ORDER BY + LIMIT)
  - Time series: Order analytics grouped by date
  - Anti-pattern (large result sets): Full catalog export query

Schema:
  - ecommerce.users          (50k rows, 12 MB)
  - ecommerce.products        (25k rows, 45 MB)
  - ecommerce.orders          (500k rows, 280 MB)
  - ecommerce.order_items     (1.2M rows, 180 MB)
  - ecommerce.sessions        (200k rows, 35 MB)
  - ecommerce.product_reviews (150k rows, 95 MB)
  - ecommerce.shopping_carts  (30k rows, 8 MB)
  - ecommerce.daily_analytics (5k rows, 2 MB)
"""

from typing import Any


def get_ecommerce_collector_output() -> dict[str, Any]:
    """Return a complete, contract-validated CollectorOutputContract for an e-commerce DB."""
    return {
        "contract_version": "3.0",
        "job_id": "job-ecommerce-001",
        "metadata": _metadata(),
        "database_schema": _schema(),
        "queries": _queries(),
        "metrics": _metrics(),
    }


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


def _metadata() -> dict[str, Any]:
    return {
        "collection_timestamp": "2026-02-15T08:30:00Z",
        "collector_version": "1.0.0",
        "collection_duration_seconds": 127.4,
        "source_database": {
            "engine": "mysql",
            "version": "8.0.35",
            "hostname": "ecommerce-prod.cxyz1234abcd.us-east-1.rds.amazonaws.com",
            "database_name": "ecommerce",
            "database_size_gb": 42.5,
            "deployment_type": "rds_instance",
            "rds_instance_metadata": {
                "db_instance_identifier": "ecommerce-prod",
                "instance_class": "db.r6g.xlarge",
                "vcpu_count": 4,
                "memory_gb": 32.0,
                "storage_type": "gp3",
                "storage_size_gb": 200,
                "storage_iops": 3000,
                "storage_throughput_mbps": 125,
                "multi_az": True,
                "region": "us-east-1",
                "availability_zone": "us-east-1a",
                "read_replica_count": 1,
                "backup_retention_days": 7,
                "performance_insights_enabled": True,
                "enhanced_monitoring_interval": 60,
            },
        },
    }


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def _schema() -> dict[str, Any]:
    return {
        "tables": [
            _users_table(),
            _products_table(),
            _orders_table(),
            _order_items_table(),
            _sessions_table(),
            _product_reviews_table(),
            _shopping_carts_table(),
            _daily_analytics_table(),
        ],
        "views": [
            {
                "view_id": "ecommerce.top_sellers_30d",
                "view_name": "top_sellers_30d",
                "schema_name": "ecommerce",
                "definition": (
                    "SELECT p.product_id, p.name, SUM(oi.quantity) AS total_sold "
                    "FROM products p JOIN order_items oi ON p.product_id = oi.product_id "
                    "JOIN orders o ON oi.order_id = o.order_id "
                    "WHERE o.created_at >= DATE_SUB(NOW(), INTERVAL 30 DAY) "
                    "GROUP BY p.product_id, p.name ORDER BY total_sold DESC"
                ),
                "is_updatable": False,
                "referenced_tables": [
                    "ecommerce.products",
                    "ecommerce.order_items",
                    "ecommerce.orders",
                ],
                "column_list": ["product_id", "name", "total_sold"],
            }
        ],
        "procedures": [
            {
                "procedure_id": "ecommerce.sp_recalculate_product_rating",
                "procedure_name": "sp_recalculate_product_rating",
                "schema_name": "ecommerce",
                "procedure_type": "PROCEDURE",
                "definition": (
                    "CREATE PROCEDURE sp_recalculate_product_rating(IN p_product_id INT) "
                    "BEGIN "
                    "  UPDATE products SET avg_rating = ("
                    "    SELECT AVG(rating) FROM product_reviews WHERE product_id = p_product_id"
                    "  ) WHERE product_id = p_product_id; "
                    "END"
                ),
                "language": "SQL",
                "parameters": [
                    {
                        "parameter_name": "p_product_id",
                        "data_type": "INT",
                        "parameter_mode": "IN",
                    }
                ],
                "referenced_tables": ["ecommerce.products", "ecommerce.product_reviews"],
            }
        ],
        "triggers": [
            {
                "trigger_id": "ecommerce.trg_review_after_insert",
                "trigger_name": "trg_review_after_insert",
                "schema_name": "ecommerce",
                "table_id": "ecommerce.product_reviews",
                "event_type": "INSERT",
                "timing": "AFTER",
                "for_each": "ROW",
                "definition": (
                    "CREATE TRIGGER trg_review_after_insert AFTER INSERT ON product_reviews "
                    "FOR EACH ROW CALL sp_recalculate_product_rating(NEW.product_id)"
                ),
                "is_enabled": True,
            }
        ],
    }


def _users_table() -> dict[str, Any]:
    return {
        "table_id": "ecommerce.users",
        "table_name": "users",
        "schema_name": "ecommerce",
        "row_count": 50000,
        "size_mb": 12.0,
        "columns": [
            {
                "column_name": "user_id",
                "ordinal_position": 1,
                "data_type": "int",
                "normalized_data_type": "integer",
                "nullable": False,
                "is_auto_increment": True,
                "cardinality": 50000,
            },
            {
                "column_name": "email",
                "ordinal_position": 2,
                "data_type": "varchar(255)",
                "normalized_data_type": "string",
                "max_length": 255,
                "nullable": False,
                "cardinality": 50000,
            },
            {
                "column_name": "username",
                "ordinal_position": 3,
                "data_type": "varchar(100)",
                "normalized_data_type": "string",
                "max_length": 100,
                "nullable": False,
                "cardinality": 49800,
            },
            {
                "column_name": "password_hash",
                "ordinal_position": 4,
                "data_type": "varchar(255)",
                "normalized_data_type": "string",
                "max_length": 255,
                "nullable": False,
            },
            {
                "column_name": "display_name",
                "ordinal_position": 5,
                "data_type": "varchar(150)",
                "normalized_data_type": "string",
                "max_length": 150,
                "nullable": True,
            },
            {
                "column_name": "is_active",
                "ordinal_position": 6,
                "data_type": "tinyint(1)",
                "normalized_data_type": "boolean",
                "nullable": False,
                "default_value": 1,
            },
            {
                "column_name": "created_at",
                "ordinal_position": 7,
                "data_type": "timestamp",
                "normalized_data_type": "timestamp",
                "nullable": False,
                "default_value": "CURRENT_TIMESTAMP",
            },
            {
                "column_name": "last_login_at",
                "ordinal_position": 8,
                "data_type": "timestamp",
                "normalized_data_type": "timestamp",
                "nullable": True,
            },
        ],
        "indexes": [
            {
                "index_name": "PRIMARY",
                "columns": ["user_id"],
                "is_unique": True,
                "is_primary": True,
                "index_type": "btree",
            },
            {
                "index_name": "idx_users_email",
                "columns": ["email"],
                "is_unique": True,
                "index_type": "btree",
            },
            {
                "index_name": "idx_users_username",
                "columns": ["username"],
                "is_unique": True,
                "index_type": "btree",
            },
        ],
        "primary_key": ["user_id"],
    }


def _products_table() -> dict[str, Any]:
    return {
        "table_id": "ecommerce.products",
        "table_name": "products",
        "schema_name": "ecommerce",
        "row_count": 25000,
        "size_mb": 45.0,
        "columns": [
            {
                "column_name": "product_id",
                "ordinal_position": 1,
                "data_type": "int",
                "normalized_data_type": "integer",
                "nullable": False,
                "is_auto_increment": True,
                "cardinality": 25000,
            },
            {
                "column_name": "name",
                "ordinal_position": 2,
                "data_type": "varchar(255)",
                "normalized_data_type": "string",
                "max_length": 255,
                "nullable": False,
                "cardinality": 24500,
            },
            {
                "column_name": "description",
                "ordinal_position": 3,
                "data_type": "text",
                "normalized_data_type": "text",
                "nullable": True,
            },
            {
                "column_name": "price",
                "ordinal_position": 4,
                "data_type": "decimal(10,2)",
                "normalized_data_type": "decimal",
                "nullable": False,
            },
            {
                "column_name": "category_id",
                "ordinal_position": 5,
                "data_type": "int",
                "normalized_data_type": "integer",
                "nullable": False,
                "cardinality": 120,
            },
            {
                "column_name": "stock_quantity",
                "ordinal_position": 6,
                "data_type": "int",
                "normalized_data_type": "integer",
                "nullable": False,
                "default_value": 0,
            },
            {
                "column_name": "avg_rating",
                "ordinal_position": 7,
                "data_type": "decimal(3,2)",
                "normalized_data_type": "decimal",
                "nullable": True,
            },
            {
                "column_name": "review_count",
                "ordinal_position": 8,
                "data_type": "int",
                "normalized_data_type": "integer",
                "nullable": False,
                "default_value": 0,
            },
            {
                "column_name": "is_active",
                "ordinal_position": 9,
                "data_type": "tinyint(1)",
                "normalized_data_type": "boolean",
                "nullable": False,
                "default_value": 1,
            },
            {
                "column_name": "created_at",
                "ordinal_position": 10,
                "data_type": "timestamp",
                "normalized_data_type": "timestamp",
                "nullable": False,
                "default_value": "CURRENT_TIMESTAMP",
            },
        ],
        "indexes": [
            {
                "index_name": "PRIMARY",
                "columns": ["product_id"],
                "is_unique": True,
                "is_primary": True,
                "index_type": "btree",
            },
            {
                "index_name": "idx_products_category",
                "columns": ["category_id"],
                "is_unique": False,
                "index_type": "btree",
            },
            {
                "index_name": "idx_products_rating",
                "columns": ["avg_rating"],
                "is_unique": False,
                "index_type": "btree",
            },
        ],
        "primary_key": ["product_id"],
    }


def _orders_table() -> dict[str, Any]:
    return {
        "table_id": "ecommerce.orders",
        "table_name": "orders",
        "schema_name": "ecommerce",
        "row_count": 500000,
        "size_mb": 280.0,
        "columns": [
            {
                "column_name": "order_id",
                "ordinal_position": 1,
                "data_type": "int",
                "normalized_data_type": "integer",
                "nullable": False,
                "is_auto_increment": True,
                "cardinality": 500000,
            },
            {
                "column_name": "user_id",
                "ordinal_position": 2,
                "data_type": "int",
                "normalized_data_type": "integer",
                "nullable": False,
                "cardinality": 42000,
            },
            {
                "column_name": "status",
                "ordinal_position": 3,
                "data_type": "enum('pending','processing','shipped','delivered','cancelled')",
                "normalized_data_type": "string",
                "nullable": False,
                "default_value": "pending",
                "cardinality": 5,
            },
            {
                "column_name": "total_amount",
                "ordinal_position": 4,
                "data_type": "decimal(12,2)",
                "normalized_data_type": "decimal",
                "nullable": False,
            },
            {
                "column_name": "shipping_address_json",
                "ordinal_position": 5,
                "data_type": "json",
                "normalized_data_type": "json",
                "nullable": True,
            },
            {
                "column_name": "created_at",
                "ordinal_position": 6,
                "data_type": "timestamp",
                "normalized_data_type": "timestamp",
                "nullable": False,
                "default_value": "CURRENT_TIMESTAMP",
            },
            {
                "column_name": "updated_at",
                "ordinal_position": 7,
                "data_type": "timestamp",
                "normalized_data_type": "timestamp",
                "nullable": False,
                "default_value": "CURRENT_TIMESTAMP",
            },
        ],
        "indexes": [
            {
                "index_name": "PRIMARY",
                "columns": ["order_id"],
                "is_unique": True,
                "is_primary": True,
                "index_type": "btree",
            },
            {
                "index_name": "idx_orders_user_id",
                "columns": ["user_id"],
                "is_unique": False,
                "index_type": "btree",
            },
            {
                "index_name": "idx_orders_status",
                "columns": ["status"],
                "is_unique": False,
                "index_type": "btree",
            },
            {
                "index_name": "idx_orders_created_at",
                "columns": ["created_at"],
                "is_unique": False,
                "index_type": "btree",
            },
        ],
        "primary_key": ["order_id"],
        "foreign_keys": [
            {
                "constraint_name": "fk_orders_user_id",
                "columns": ["user_id"],
                "referenced_table": "ecommerce.users",
                "referenced_columns": ["user_id"],
                "on_delete": "RESTRICT",
                "on_update": "CASCADE",
            }
        ],
    }


def _order_items_table() -> dict[str, Any]:
    return {
        "table_id": "ecommerce.order_items",
        "table_name": "order_items",
        "schema_name": "ecommerce",
        "row_count": 1200000,
        "size_mb": 180.0,
        "columns": [
            {
                "column_name": "item_id",
                "ordinal_position": 1,
                "data_type": "int",
                "normalized_data_type": "integer",
                "nullable": False,
                "is_auto_increment": True,
                "cardinality": 1200000,
            },
            {
                "column_name": "order_id",
                "ordinal_position": 2,
                "data_type": "int",
                "normalized_data_type": "integer",
                "nullable": False,
                "cardinality": 500000,
            },
            {
                "column_name": "product_id",
                "ordinal_position": 3,
                "data_type": "int",
                "normalized_data_type": "integer",
                "nullable": False,
                "cardinality": 22000,
            },
            {
                "column_name": "quantity",
                "ordinal_position": 4,
                "data_type": "int",
                "normalized_data_type": "integer",
                "nullable": False,
                "default_value": 1,
            },
            {
                "column_name": "unit_price",
                "ordinal_position": 5,
                "data_type": "decimal(10,2)",
                "normalized_data_type": "decimal",
                "nullable": False,
            },
        ],
        "indexes": [
            {
                "index_name": "PRIMARY",
                "columns": ["item_id"],
                "is_unique": True,
                "is_primary": True,
                "index_type": "btree",
            },
            {
                "index_name": "idx_order_items_order",
                "columns": ["order_id"],
                "is_unique": False,
                "index_type": "btree",
            },
            {
                "index_name": "idx_order_items_product",
                "columns": ["product_id"],
                "is_unique": False,
                "index_type": "btree",
            },
        ],
        "primary_key": ["item_id"],
        "foreign_keys": [
            {
                "constraint_name": "fk_order_items_order",
                "columns": ["order_id"],
                "referenced_table": "ecommerce.orders",
                "referenced_columns": ["order_id"],
                "on_delete": "CASCADE",
                "on_update": "CASCADE",
            },
            {
                "constraint_name": "fk_order_items_product",
                "columns": ["product_id"],
                "referenced_table": "ecommerce.products",
                "referenced_columns": ["product_id"],
                "on_delete": "RESTRICT",
                "on_update": "CASCADE",
            },
        ],
    }


def _sessions_table() -> dict[str, Any]:
    return {
        "table_id": "ecommerce.sessions",
        "table_name": "sessions",
        "schema_name": "ecommerce",
        "row_count": 200000,
        "size_mb": 35.0,
        "columns": [
            {
                "column_name": "session_id",
                "ordinal_position": 1,
                "data_type": "varchar(128)",
                "normalized_data_type": "string",
                "max_length": 128,
                "nullable": False,
                "cardinality": 200000,
            },
            {
                "column_name": "user_id",
                "ordinal_position": 2,
                "data_type": "int",
                "normalized_data_type": "integer",
                "nullable": True,
                "cardinality": 45000,
            },
            {
                "column_name": "session_data",
                "ordinal_position": 3,
                "data_type": "mediumblob",
                "normalized_data_type": "blob",
                "nullable": True,
            },
            {
                "column_name": "ip_address",
                "ordinal_position": 4,
                "data_type": "varchar(45)",
                "normalized_data_type": "string",
                "max_length": 45,
                "nullable": True,
            },
            {
                "column_name": "user_agent",
                "ordinal_position": 5,
                "data_type": "varchar(512)",
                "normalized_data_type": "string",
                "max_length": 512,
                "nullable": True,
            },
            {
                "column_name": "created_at",
                "ordinal_position": 6,
                "data_type": "timestamp",
                "normalized_data_type": "timestamp",
                "nullable": False,
                "default_value": "CURRENT_TIMESTAMP",
            },
            {
                "column_name": "expires_at",
                "ordinal_position": 7,
                "data_type": "timestamp",
                "normalized_data_type": "timestamp",
                "nullable": False,
            },
            {
                "column_name": "last_activity_at",
                "ordinal_position": 8,
                "data_type": "timestamp",
                "normalized_data_type": "timestamp",
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
            },
            {
                "index_name": "idx_sessions_user_id",
                "columns": ["user_id"],
                "is_unique": False,
                "index_type": "btree",
            },
            {
                "index_name": "idx_sessions_expires",
                "columns": ["expires_at"],
                "is_unique": False,
                "index_type": "btree",
            },
        ],
        "primary_key": ["session_id"],
        "foreign_keys": [
            {
                "constraint_name": "fk_sessions_user",
                "columns": ["user_id"],
                "referenced_table": "ecommerce.users",
                "referenced_columns": ["user_id"],
                "on_delete": "SET NULL",
                "on_update": "CASCADE",
            }
        ],
    }


def _product_reviews_table() -> dict[str, Any]:
    return {
        "table_id": "ecommerce.product_reviews",
        "table_name": "product_reviews",
        "schema_name": "ecommerce",
        "row_count": 150000,
        "size_mb": 95.0,
        "columns": [
            {
                "column_name": "review_id",
                "ordinal_position": 1,
                "data_type": "int",
                "normalized_data_type": "integer",
                "nullable": False,
                "is_auto_increment": True,
                "cardinality": 150000,
            },
            {
                "column_name": "product_id",
                "ordinal_position": 2,
                "data_type": "int",
                "normalized_data_type": "integer",
                "nullable": False,
                "cardinality": 18000,
            },
            {
                "column_name": "user_id",
                "ordinal_position": 3,
                "data_type": "int",
                "normalized_data_type": "integer",
                "nullable": False,
                "cardinality": 35000,
            },
            {
                "column_name": "rating",
                "ordinal_position": 4,
                "data_type": "tinyint",
                "normalized_data_type": "integer",
                "nullable": False,
                "cardinality": 5,
            },
            {
                "column_name": "title",
                "ordinal_position": 5,
                "data_type": "varchar(255)",
                "normalized_data_type": "string",
                "max_length": 255,
                "nullable": True,
            },
            {
                "column_name": "body",
                "ordinal_position": 6,
                "data_type": "text",
                "normalized_data_type": "text",
                "nullable": True,
            },
            {
                "column_name": "created_at",
                "ordinal_position": 7,
                "data_type": "timestamp",
                "normalized_data_type": "timestamp",
                "nullable": False,
                "default_value": "CURRENT_TIMESTAMP",
            },
        ],
        "indexes": [
            {
                "index_name": "PRIMARY",
                "columns": ["review_id"],
                "is_unique": True,
                "is_primary": True,
                "index_type": "btree",
            },
            {
                "index_name": "idx_reviews_product",
                "columns": ["product_id"],
                "is_unique": False,
                "index_type": "btree",
            },
            {
                "index_name": "idx_reviews_user_product",
                "columns": ["user_id", "product_id"],
                "is_unique": True,
                "index_type": "btree",
            },
        ],
        "primary_key": ["review_id"],
        "foreign_keys": [
            {
                "constraint_name": "fk_reviews_product",
                "columns": ["product_id"],
                "referenced_table": "ecommerce.products",
                "referenced_columns": ["product_id"],
                "on_delete": "CASCADE",
                "on_update": "CASCADE",
            },
            {
                "constraint_name": "fk_reviews_user",
                "columns": ["user_id"],
                "referenced_table": "ecommerce.users",
                "referenced_columns": ["user_id"],
                "on_delete": "CASCADE",
                "on_update": "CASCADE",
            },
        ],
    }


def _shopping_carts_table() -> dict[str, Any]:
    return {
        "table_id": "ecommerce.shopping_carts",
        "table_name": "shopping_carts",
        "schema_name": "ecommerce",
        "row_count": 30000,
        "size_mb": 8.0,
        "columns": [
            {
                "column_name": "cart_id",
                "ordinal_position": 1,
                "data_type": "int",
                "normalized_data_type": "integer",
                "nullable": False,
                "is_auto_increment": True,
                "cardinality": 30000,
            },
            {
                "column_name": "user_id",
                "ordinal_position": 2,
                "data_type": "int",
                "normalized_data_type": "integer",
                "nullable": False,
                "cardinality": 28000,
            },
            {
                "column_name": "product_id",
                "ordinal_position": 3,
                "data_type": "int",
                "normalized_data_type": "integer",
                "nullable": False,
                "cardinality": 12000,
            },
            {
                "column_name": "quantity",
                "ordinal_position": 4,
                "data_type": "int",
                "normalized_data_type": "integer",
                "nullable": False,
                "default_value": 1,
            },
            {
                "column_name": "added_at",
                "ordinal_position": 5,
                "data_type": "timestamp",
                "normalized_data_type": "timestamp",
                "nullable": False,
                "default_value": "CURRENT_TIMESTAMP",
            },
        ],
        "indexes": [
            {
                "index_name": "PRIMARY",
                "columns": ["cart_id"],
                "is_unique": True,
                "is_primary": True,
                "index_type": "btree",
            },
            {
                "index_name": "idx_carts_user",
                "columns": ["user_id"],
                "is_unique": False,
                "index_type": "btree",
            },
        ],
        "primary_key": ["cart_id"],
        "foreign_keys": [
            {
                "constraint_name": "fk_carts_user",
                "columns": ["user_id"],
                "referenced_table": "ecommerce.users",
                "referenced_columns": ["user_id"],
                "on_delete": "CASCADE",
                "on_update": "CASCADE",
            },
            {
                "constraint_name": "fk_carts_product",
                "columns": ["product_id"],
                "referenced_table": "ecommerce.products",
                "referenced_columns": ["product_id"],
                "on_delete": "CASCADE",
                "on_update": "CASCADE",
            },
        ],
    }


def _daily_analytics_table() -> dict[str, Any]:
    return {
        "table_id": "ecommerce.daily_analytics",
        "table_name": "daily_analytics",
        "schema_name": "ecommerce",
        "row_count": 5000,
        "size_mb": 2.0,
        "columns": [
            {
                "column_name": "analytics_id",
                "ordinal_position": 1,
                "data_type": "int",
                "normalized_data_type": "integer",
                "nullable": False,
                "is_auto_increment": True,
                "cardinality": 5000,
            },
            {
                "column_name": "metric_date",
                "ordinal_position": 2,
                "data_type": "date",
                "normalized_data_type": "date",
                "nullable": False,
                "cardinality": 365,
            },
            {
                "column_name": "category_id",
                "ordinal_position": 3,
                "data_type": "int",
                "normalized_data_type": "integer",
                "nullable": False,
                "cardinality": 120,
            },
            {
                "column_name": "total_orders",
                "ordinal_position": 4,
                "data_type": "int",
                "normalized_data_type": "integer",
                "nullable": False,
                "default_value": 0,
            },
            {
                "column_name": "total_revenue",
                "ordinal_position": 5,
                "data_type": "decimal(14,2)",
                "normalized_data_type": "decimal",
                "nullable": False,
                "default_value": 0,
            },
            {
                "column_name": "unique_customers",
                "ordinal_position": 6,
                "data_type": "int",
                "normalized_data_type": "integer",
                "nullable": False,
                "default_value": 0,
            },
        ],
        "indexes": [
            {
                "index_name": "PRIMARY",
                "columns": ["analytics_id"],
                "is_unique": True,
                "is_primary": True,
                "index_type": "btree",
            },
            {
                "index_name": "idx_analytics_date_category",
                "columns": ["metric_date", "category_id"],
                "is_unique": True,
                "index_type": "btree",
            },
        ],
        "primary_key": ["analytics_id"],
    }


# ---------------------------------------------------------------------------
# Query Patterns
# ---------------------------------------------------------------------------


def _queries() -> dict[str, Any]:
    return {
        "query_patterns": [
            # ── Caching pattern: high-frequency product lookups ──
            {
                "query_id": "q01-product-by-id",
                "query_text": "SELECT product_id, name, description, price, stock_quantity, avg_rating, review_count FROM products WHERE product_id = ?",
                "query_type": "SELECT",
                "frequency_per_hour": 36000.0,
                "calls_per_second": 10.0,
                "tables_accessed": ["ecommerce.products"],
                "rows_returned_avg": 1.0,
                "rows_returned_p95": 1.0,
                "execution_time_ms_avg": 0.8,
                "execution_time_ms_min": 0.3,
                "execution_time_ms_max": 12.0,
                "execution_time_ms_p50": 0.6,
                "execution_time_ms_p95": 2.1,
                "execution_time_ms_p99": 5.5,
                "total_time_ms": 28800.0,
                "db_load_contribution_percent": 18.5,
                "has_joins": False,
                "join_count": 0,
                "has_aggregations": False,
                "has_subqueries": False,
                "filter_columns": ["product_id"],
                "first_seen": "2026-02-14T00:00:00Z",
                "last_seen": "2026-02-15T08:30:00Z",
            },
            # ── Caching pattern: high-frequency user profile reads ──
            {
                "query_id": "q02-user-by-id",
                "query_text": "SELECT user_id, username, display_name, email FROM users WHERE user_id = ?",
                "query_type": "SELECT",
                "frequency_per_hour": 18000.0,
                "calls_per_second": 5.0,
                "tables_accessed": ["ecommerce.users"],
                "rows_returned_avg": 1.0,
                "rows_returned_p95": 1.0,
                "execution_time_ms_avg": 0.5,
                "execution_time_ms_min": 0.2,
                "execution_time_ms_max": 8.0,
                "execution_time_ms_p50": 0.4,
                "execution_time_ms_p95": 1.5,
                "execution_time_ms_p99": 3.8,
                "total_time_ms": 9000.0,
                "db_load_contribution_percent": 5.8,
                "has_joins": False,
                "join_count": 0,
                "has_aggregations": False,
                "has_subqueries": False,
                "filter_columns": ["user_id"],
                "first_seen": "2026-02-14T00:00:00Z",
                "last_seen": "2026-02-15T08:30:00Z",
            },
            # ── Caching pattern: product listing by category ──
            {
                "query_id": "q03-products-by-category",
                "query_text": "SELECT product_id, name, price, avg_rating FROM products WHERE category_id = ? AND is_active = 1 ORDER BY avg_rating DESC LIMIT 20",
                "query_type": "SELECT",
                "frequency_per_hour": 7200.0,
                "calls_per_second": 2.0,
                "tables_accessed": ["ecommerce.products"],
                "rows_returned_avg": 18.5,
                "rows_returned_p95": 20.0,
                "execution_time_ms_avg": 3.2,
                "execution_time_ms_min": 1.0,
                "execution_time_ms_max": 25.0,
                "execution_time_ms_p50": 2.5,
                "execution_time_ms_p95": 8.0,
                "execution_time_ms_p99": 15.0,
                "total_time_ms": 23040.0,
                "db_load_contribution_percent": 14.8,
                "has_joins": False,
                "join_count": 0,
                "has_aggregations": False,
                "has_subqueries": False,
                "filter_columns": ["category_id", "is_active"],
                "sort_columns": ["avg_rating"],
                "first_seen": "2026-02-14T00:00:00Z",
                "last_seen": "2026-02-15T08:30:00Z",
            },
            # ── Session store pattern: session lookups ──
            {
                "query_id": "q04-session-lookup",
                "query_text": "SELECT session_id, user_id, session_data, expires_at FROM sessions WHERE session_id = ? AND expires_at > NOW()",
                "query_type": "SELECT",
                "frequency_per_hour": 54000.0,
                "calls_per_second": 15.0,
                "tables_accessed": ["ecommerce.sessions"],
                "rows_returned_avg": 1.0,
                "rows_returned_p95": 1.0,
                "execution_time_ms_avg": 0.4,
                "execution_time_ms_min": 0.1,
                "execution_time_ms_max": 6.0,
                "execution_time_ms_p50": 0.3,
                "execution_time_ms_p95": 1.2,
                "execution_time_ms_p99": 3.0,
                "total_time_ms": 21600.0,
                "db_load_contribution_percent": 13.9,
                "has_joins": False,
                "join_count": 0,
                "has_aggregations": False,
                "has_subqueries": False,
                "filter_columns": ["session_id", "expires_at"],
                "first_seen": "2026-02-14T00:00:00Z",
                "last_seen": "2026-02-15T08:30:00Z",
            },
            # ── Session store pattern: session upsert ──
            {
                "query_id": "q05-session-upsert",
                "query_text": "INSERT INTO sessions (session_id, user_id, session_data, expires_at, last_activity_at) VALUES (?, ?, ?, ?, NOW()) ON DUPLICATE KEY UPDATE session_data = VALUES(session_data), last_activity_at = NOW(), expires_at = VALUES(expires_at)",
                "query_type": "INSERT",
                "frequency_per_hour": 36000.0,
                "calls_per_second": 10.0,
                "tables_accessed": ["ecommerce.sessions"],
                "rows_affected_avg": 1.0,
                "execution_time_ms_avg": 1.2,
                "execution_time_ms_min": 0.5,
                "execution_time_ms_max": 18.0,
                "execution_time_ms_p50": 0.9,
                "execution_time_ms_p95": 3.5,
                "execution_time_ms_p99": 8.0,
                "total_time_ms": 43200.0,
                "db_load_contribution_percent": 8.2,
                "has_joins": False,
                "join_count": 0,
                "has_aggregations": False,
                "has_subqueries": False,
                "lock_time_ms": 5400.0,
                "lock_time_pct": 12.5,
                "first_seen": "2026-02-14T00:00:00Z",
                "last_seen": "2026-02-15T08:30:00Z",
            },
            # ── Session store pattern: user's active sessions ──
            {
                "query_id": "q06-user-sessions",
                "query_text": "SELECT session_id, ip_address, user_agent, last_activity_at FROM sessions WHERE user_id = ? AND expires_at > NOW() ORDER BY last_activity_at DESC",
                "query_type": "SELECT",
                "frequency_per_hour": 3600.0,
                "calls_per_second": 1.0,
                "tables_accessed": ["ecommerce.sessions"],
                "rows_returned_avg": 2.3,
                "rows_returned_p95": 5.0,
                "execution_time_ms_avg": 1.1,
                "execution_time_ms_min": 0.4,
                "execution_time_ms_max": 10.0,
                "execution_time_ms_p50": 0.8,
                "execution_time_ms_p95": 3.0,
                "execution_time_ms_p99": 6.0,
                "total_time_ms": 3960.0,
                "db_load_contribution_percent": 2.5,
                "has_joins": False,
                "join_count": 0,
                "has_aggregations": False,
                "has_subqueries": False,
                "filter_columns": ["user_id", "expires_at"],
                "sort_columns": ["last_activity_at"],
                "first_seen": "2026-02-14T00:00:00Z",
                "last_seen": "2026-02-15T08:30:00Z",
            },
            # ── Leaderboard pattern: top-rated products ──
            {
                "query_id": "q07-top-rated-products",
                "query_text": "SELECT product_id, name, avg_rating, review_count FROM products WHERE is_active = 1 AND review_count >= 10 ORDER BY avg_rating DESC LIMIT 50",
                "query_type": "SELECT",
                "frequency_per_hour": 5400.0,
                "calls_per_second": 1.5,
                "tables_accessed": ["ecommerce.products"],
                "rows_returned_avg": 50.0,
                "rows_returned_p95": 50.0,
                "execution_time_ms_avg": 4.5,
                "execution_time_ms_min": 2.0,
                "execution_time_ms_max": 30.0,
                "execution_time_ms_p50": 3.5,
                "execution_time_ms_p95": 12.0,
                "execution_time_ms_p99": 22.0,
                "total_time_ms": 24300.0,
                "db_load_contribution_percent": 7.8,
                "has_joins": False,
                "join_count": 0,
                "has_aggregations": False,
                "has_subqueries": False,
                "filter_columns": ["is_active", "review_count"],
                "sort_columns": ["avg_rating"],
                "first_seen": "2026-02-14T00:00:00Z",
                "last_seen": "2026-02-15T08:30:00Z",
            },
            # ── Leaderboard pattern: best sellers ──
            {
                "query_id": "q08-best-sellers",
                "query_text": "SELECT p.product_id, p.name, SUM(oi.quantity) AS total_sold FROM products p JOIN order_items oi ON p.product_id = oi.product_id JOIN orders o ON oi.order_id = o.order_id WHERE o.created_at >= DATE_SUB(NOW(), INTERVAL 7 DAY) GROUP BY p.product_id, p.name ORDER BY total_sold DESC LIMIT 25",
                "query_type": "SELECT",
                "frequency_per_hour": 1800.0,
                "calls_per_second": 0.5,
                "tables_accessed": [
                    "ecommerce.products",
                    "ecommerce.order_items",
                    "ecommerce.orders",
                ],
                "rows_returned_avg": 25.0,
                "rows_returned_p95": 25.0,
                "rows_examined_avg": 85000.0,
                "execution_time_ms_avg": 45.0,
                "execution_time_ms_min": 20.0,
                "execution_time_ms_max": 350.0,
                "execution_time_ms_p50": 35.0,
                "execution_time_ms_p95": 120.0,
                "execution_time_ms_p99": 250.0,
                "total_time_ms": 81000.0,
                "db_load_contribution_percent": 12.0,
                "has_joins": True,
                "join_count": 2,
                "has_aggregations": True,
                "has_subqueries": False,
                "filter_columns": ["created_at"],
                "sort_columns": ["total_sold"],
                "first_seen": "2026-02-14T00:00:00Z",
                "last_seen": "2026-02-15T08:30:00Z",
            },
            # ── Time series pattern: daily revenue analytics ──
            {
                "query_id": "q09-daily-revenue",
                "query_text": "SELECT DATE(created_at) AS order_date, COUNT(*) AS order_count, SUM(total_amount) AS revenue FROM orders WHERE created_at >= DATE_SUB(NOW(), INTERVAL 30 DAY) GROUP BY DATE(created_at) ORDER BY order_date",
                "query_type": "SELECT",
                "frequency_per_hour": 720.0,
                "calls_per_second": 0.2,
                "tables_accessed": ["ecommerce.orders"],
                "rows_returned_avg": 30.0,
                "rows_returned_p95": 30.0,
                "rows_examined_avg": 120000.0,
                "execution_time_ms_avg": 85.0,
                "execution_time_ms_min": 40.0,
                "execution_time_ms_max": 500.0,
                "execution_time_ms_p50": 70.0,
                "execution_time_ms_p95": 200.0,
                "execution_time_ms_p99": 400.0,
                "total_time_ms": 61200.0,
                "db_load_contribution_percent": 5.5,
                "has_joins": False,
                "join_count": 0,
                "has_aggregations": True,
                "has_subqueries": False,
                "filter_columns": ["created_at"],
                "sort_columns": ["order_date"],
                "first_seen": "2026-02-14T00:00:00Z",
                "last_seen": "2026-02-15T08:30:00Z",
            },
            # ── Time series pattern: hourly order counts ──
            {
                "query_id": "q10-hourly-orders",
                "query_text": "SELECT DATE_FORMAT(created_at, '%Y-%m-%d %H:00:00') AS hour_bucket, COUNT(*) AS orders, SUM(total_amount) AS revenue FROM orders WHERE created_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR) GROUP BY hour_bucket ORDER BY hour_bucket",
                "query_type": "SELECT",
                "frequency_per_hour": 360.0,
                "calls_per_second": 0.1,
                "tables_accessed": ["ecommerce.orders"],
                "rows_returned_avg": 24.0,
                "rows_returned_p95": 24.0,
                "rows_examined_avg": 15000.0,
                "execution_time_ms_avg": 25.0,
                "execution_time_ms_min": 10.0,
                "execution_time_ms_max": 150.0,
                "execution_time_ms_p50": 20.0,
                "execution_time_ms_p95": 60.0,
                "execution_time_ms_p99": 120.0,
                "total_time_ms": 9000.0,
                "db_load_contribution_percent": 1.2,
                "has_joins": False,
                "join_count": 0,
                "has_aggregations": True,
                "has_subqueries": False,
                "filter_columns": ["created_at"],
                "sort_columns": ["hour_bucket"],
                "first_seen": "2026-02-14T00:00:00Z",
                "last_seen": "2026-02-15T08:30:00Z",
            },
            # ── Shopping cart reads (user_id pattern, medium frequency) ──
            {
                "query_id": "q11-cart-items",
                "query_text": "SELECT c.cart_id, c.product_id, c.quantity, p.name, p.price FROM shopping_carts c JOIN products p ON c.product_id = p.product_id WHERE c.user_id = ?",
                "query_type": "SELECT",
                "frequency_per_hour": 10800.0,
                "calls_per_second": 3.0,
                "tables_accessed": ["ecommerce.shopping_carts", "ecommerce.products"],
                "rows_returned_avg": 3.2,
                "rows_returned_p95": 8.0,
                "execution_time_ms_avg": 2.0,
                "execution_time_ms_min": 0.5,
                "execution_time_ms_max": 15.0,
                "execution_time_ms_p50": 1.5,
                "execution_time_ms_p95": 5.0,
                "execution_time_ms_p99": 10.0,
                "total_time_ms": 21600.0,
                "db_load_contribution_percent": 4.5,
                "has_joins": True,
                "join_count": 1,
                "has_aggregations": False,
                "has_subqueries": False,
                "filter_columns": ["user_id"],
                "first_seen": "2026-02-14T00:00:00Z",
                "last_seen": "2026-02-15T08:30:00Z",
            },
            # ── Order creation (write, moderate frequency) ──
            {
                "query_id": "q12-create-order",
                "query_text": "INSERT INTO orders (user_id, status, total_amount, shipping_address_json, created_at, updated_at) VALUES (?, 'pending', ?, ?, NOW(), NOW())",
                "query_type": "INSERT",
                "frequency_per_hour": 1800.0,
                "calls_per_second": 0.5,
                "tables_accessed": ["ecommerce.orders"],
                "rows_affected_avg": 1.0,
                "execution_time_ms_avg": 3.5,
                "execution_time_ms_min": 1.0,
                "execution_time_ms_max": 45.0,
                "execution_time_ms_p50": 2.5,
                "execution_time_ms_p95": 10.0,
                "execution_time_ms_p99": 25.0,
                "total_time_ms": 6300.0,
                "db_load_contribution_percent": 1.8,
                "has_joins": False,
                "join_count": 0,
                "has_aggregations": False,
                "has_subqueries": False,
                "lock_time_ms": 900.0,
                "lock_time_pct": 14.3,
                "first_seen": "2026-02-14T00:00:00Z",
                "last_seen": "2026-02-15T08:30:00Z",
            },
            # ── Anti-pattern: large result set (full catalog export) ──
            {
                "query_id": "q13-full-catalog-export",
                "query_text": "SELECT p.product_id, p.name, p.description, p.price, p.stock_quantity, p.avg_rating, p.review_count, p.category_id FROM products p WHERE p.is_active = 1",
                "query_type": "SELECT",
                "frequency_per_hour": 36.0,
                "calls_per_second": 0.01,
                "tables_accessed": ["ecommerce.products"],
                "rows_returned_avg": 22000.0,
                "rows_returned_p95": 24500.0,
                "rows_examined_avg": 25000.0,
                "execution_time_ms_avg": 180.0,
                "execution_time_ms_min": 80.0,
                "execution_time_ms_max": 800.0,
                "execution_time_ms_p50": 150.0,
                "execution_time_ms_p95": 400.0,
                "execution_time_ms_p99": 650.0,
                "total_time_ms": 6480.0,
                "db_load_contribution_percent": 1.5,
                "has_joins": False,
                "join_count": 0,
                "has_aggregations": False,
                "has_subqueries": False,
                "filter_columns": ["is_active"],
                "full_table_scans": 36,
                "first_seen": "2026-02-14T00:00:00Z",
                "last_seen": "2026-02-15T08:30:00Z",
            },
            # ── Product reviews (moderate frequency read) ──
            {
                "query_id": "q14-product-reviews",
                "query_text": "SELECT r.review_id, r.user_id, u.username, r.rating, r.title, r.body, r.created_at FROM product_reviews r JOIN users u ON r.user_id = u.user_id WHERE r.product_id = ? ORDER BY r.created_at DESC LIMIT 20",
                "query_type": "SELECT",
                "frequency_per_hour": 5400.0,
                "calls_per_second": 1.5,
                "tables_accessed": ["ecommerce.product_reviews", "ecommerce.users"],
                "rows_returned_avg": 15.0,
                "rows_returned_p95": 20.0,
                "execution_time_ms_avg": 5.0,
                "execution_time_ms_min": 1.5,
                "execution_time_ms_max": 40.0,
                "execution_time_ms_p50": 3.8,
                "execution_time_ms_p95": 12.0,
                "execution_time_ms_p99": 25.0,
                "total_time_ms": 27000.0,
                "db_load_contribution_percent": 3.0,
                "has_joins": True,
                "join_count": 1,
                "has_aggregations": False,
                "has_subqueries": False,
                "filter_columns": ["product_id"],
                "sort_columns": ["created_at"],
                "first_seen": "2026-02-14T00:00:00Z",
                "last_seen": "2026-02-15T08:30:00Z",
            },
        ],
        "total_queries_analyzed": 982450,
        "query_log_source": "performance_insights",
        "collection_start_time": "2026-02-14T00:00:00Z",
        "collection_end_time": "2026-02-15T08:30:00Z",
    }


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _metrics() -> dict[str, Any]:
    return {
        "performance_metrics": {
            "avg_query_time_ms": 8.2,
            "p50_query_time_ms": 1.5,
            "p95_query_time_ms": 35.0,
            "p99_query_time_ms": 120.0,
            "queries_per_second": 50.8,
            "connection_pool_usage_percent": 58.0,
            "active_connections_avg": 42.0,
            "active_connections_max": 85.0,
            "transactions_per_second": 38.5,
            "read_iops_avg": 2800.0,
            "write_iops_avg": 450.0,
            "network_throughput_mbps_avg": 35.0,
        },
        "rds_cloudwatch_metrics": {
            "cpu_utilization": {"avg": 52.0, "max": 88.0, "min": 15.0, "p95": 78.0},
            "freeable_memory_gb": {"avg": 18.5, "max": 26.0, "min": 8.0, "p95": 12.0},
            "database_connections": {"avg": 42.0, "max": 85.0, "min": 12.0, "p95": 72.0},
            "read_iops": {"avg": 2800.0, "max": 5500.0, "min": 800.0, "p95": 4800.0},
            "write_iops": {"avg": 450.0, "max": 1200.0, "min": 100.0, "p95": 900.0},
            "read_latency_ms": {"avg": 1.8, "max": 15.0, "min": 0.3, "p95": 8.0},
            "write_latency_ms": {"avg": 3.5, "max": 25.0, "min": 1.0, "p95": 12.0},
            "network_receive_throughput_mbps": {
                "avg": 22.0,
                "max": 65.0,
                "min": 5.0,
                "p95": 55.0,
            },
            "network_transmit_throughput_mbps": {
                "avg": 35.0,
                "max": 90.0,
                "min": 8.0,
                "p95": 75.0,
            },
            "free_storage_space_gb": 158.0,
        },
    }
