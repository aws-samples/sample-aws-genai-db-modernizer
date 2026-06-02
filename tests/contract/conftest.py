"""
Test fixtures and utilities for contract validation tests.

This module provides shared fixtures and utility functions for testing
Pydantic contract models, particularly the CollectorOutputContract.
"""

from typing import Any

import pytest


@pytest.fixture
def valid_collector_output_data() -> dict[str, Any]:
    """
    Fixture providing a complete valid CollectorOutputContract data dictionary.

    This fixture includes all required fields and many optional fields to provide
    a comprehensive example of valid contract data.

    Returns:
        dict: A dictionary that can be used to instantiate CollectorOutputContract
    """
    return {
        "contract_version": "3.0",
        "job_id": "test-job-12345",
        "metadata": {
            "collection_timestamp": "2024-01-15T10:30:00Z",
            "collector_version": "1.2.3",
            "collection_duration_seconds": 45.5,
            "source_database": {
                "engine": "postgresql",
                "version": "14.7",
                "hostname": "prod-db.example.com",
                "database_name": "production_db",
                "database_size_gb": 250.5,
                "deployment_type": "rds_instance",
                "rds_instance_metadata": {
                    "db_instance_identifier": "prod-db-instance",
                    "instance_class": "db.r5.xlarge",
                    "vcpu_count": 4,
                    "memory_gb": 32.0,
                    "storage_type": "gp3",
                    "storage_size_gb": 500,
                    "storage_iops": 3000,
                    "storage_throughput_mbps": 125,
                    "multi_az": True,
                    "region": "us-east-1",
                    "availability_zone": "us-east-1a",
                    "read_replica_count": 2,
                    "backup_retention_days": 7,
                    "performance_insights_enabled": True,
                    "enhanced_monitoring_interval": 60,
                },
            },
        },
        "database_schema": {
            "tables": [
                {
                    "table_id": "public.users",
                    "table_name": "users",
                    "schema_name": "public",
                    "row_count": 10000,
                    "size_mb": 50.5,
                    "columns": [
                        {
                            "column_name": "id",
                            "ordinal_position": 1,
                            "data_type": "integer",
                            "normalized_data_type": "integer",
                            "nullable": False,
                            "is_auto_increment": True,
                            "cardinality": 10000,
                        },
                        {
                            "column_name": "username",
                            "ordinal_position": 2,
                            "data_type": "varchar",
                            "normalized_data_type": "string",
                            "max_length": 255,
                            "nullable": False,
                            "cardinality": 9950,
                        },
                        {
                            "column_name": "email",
                            "ordinal_position": 3,
                            "data_type": "varchar",
                            "normalized_data_type": "string",
                            "max_length": 255,
                            "nullable": False,
                            "cardinality": 10000,
                        },
                        {
                            "column_name": "created_at",
                            "ordinal_position": 4,
                            "data_type": "timestamp",
                            "normalized_data_type": "timestamp",
                            "nullable": False,
                            "default_value": "CURRENT_TIMESTAMP",
                        },
                    ],
                    "indexes": [
                        {
                            "index_name": "users_pkey",
                            "columns": ["id"],
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
                    ],
                    "primary_key": ["id"],
                },
                {
                    "table_id": "public.orders",
                    "table_name": "orders",
                    "schema_name": "public",
                    "row_count": 50000,
                    "size_mb": 150.0,
                    "columns": [
                        {
                            "column_name": "order_id",
                            "ordinal_position": 1,
                            "data_type": "integer",
                            "normalized_data_type": "integer",
                            "nullable": False,
                            "is_auto_increment": True,
                        },
                        {
                            "column_name": "user_id",
                            "ordinal_position": 2,
                            "data_type": "integer",
                            "normalized_data_type": "integer",
                            "nullable": False,
                        },
                        {
                            "column_name": "total_amount",
                            "ordinal_position": 3,
                            "data_type": "numeric",
                            "normalized_data_type": "decimal",
                            "nullable": False,
                        },
                    ],
                    "indexes": [
                        {
                            "index_name": "orders_pkey",
                            "columns": ["order_id"],
                            "is_unique": True,
                            "is_primary": True,
                            "index_type": "btree",
                        }
                    ],
                    "primary_key": ["order_id"],
                    "foreign_keys": [
                        {
                            "constraint_name": "fk_orders_user",
                            "columns": ["user_id"],
                            "referenced_table": "public.users",
                            "referenced_columns": ["id"],
                            "on_delete": "CASCADE",
                            "on_update": "CASCADE",
                        }
                    ],
                },
            ],
            "views": [
                {
                    "view_id": "public.user_order_summary",
                    "view_name": "user_order_summary",
                    "schema_name": "public",
                    "definition": "SELECT u.id, u.username, COUNT(o.order_id) as order_count FROM users u LEFT JOIN orders o ON u.id = o.user_id GROUP BY u.id, u.username",
                    "is_updatable": False,
                    "referenced_tables": ["public.users", "public.orders"],
                    "column_list": ["id", "username", "order_count"],
                }
            ],
            "procedures": [
                {
                    "procedure_id": "public.get_user_orders",
                    "procedure_name": "get_user_orders",
                    "schema_name": "public",
                    "procedure_type": "FUNCTION",
                    "definition": "CREATE FUNCTION get_user_orders(user_id_param INTEGER) RETURNS TABLE(order_id INTEGER, total_amount NUMERIC) AS $$ BEGIN RETURN QUERY SELECT order_id, total_amount FROM orders WHERE user_id = user_id_param; END; $$ LANGUAGE plpgsql;",
                    "language": "plpgsql",
                    "return_type": "TABLE",
                    "referenced_tables": ["public.orders"],
                }
            ],
            "triggers": [
                {
                    "trigger_id": "public.update_user_timestamp",
                    "trigger_name": "update_user_timestamp",
                    "schema_name": "public",
                    "table_id": "public.users",
                    "event_type": "UPDATE",
                    "timing": "BEFORE",
                    "for_each": "ROW",
                    "definition": "CREATE TRIGGER update_user_timestamp BEFORE UPDATE ON users FOR EACH ROW EXECUTE FUNCTION update_timestamp();",
                    "is_enabled": True,
                }
            ],
        },
        "queries": {
            "query_patterns": [
                {
                    "query_id": "q1-select-users",
                    "query_text": "SELECT * FROM users WHERE id = ?",
                    "query_type": "SELECT",
                    "frequency_per_hour": 1200.0,
                    "calls_per_second": 0.33,
                    "tables_accessed": ["public.users"],
                    "rows_returned_avg": 1.0,
                    "rows_returned_p95": 1.0,
                    "execution_time_ms_avg": 2.5,
                    "execution_time_ms_min": 1.0,
                    "execution_time_ms_max": 10.0,
                    "execution_time_ms_p50": 2.0,
                    "execution_time_ms_p95": 5.0,
                    "execution_time_ms_p99": 8.0,
                    "total_time_ms": 3000.0,
                    "db_load_contribution_percent": 15.5,
                    "has_joins": False,
                    "join_count": 0,
                    "has_aggregations": False,
                    "has_subqueries": False,
                    "filter_columns": ["id"],
                    "first_seen": "2024-01-15T00:00:00Z",
                    "last_seen": "2024-01-15T10:30:00Z",
                },
                {
                    "query_id": "q2-join-orders",
                    "query_text": "SELECT u.username, o.order_id, o.total_amount FROM users u JOIN orders o ON u.id = o.user_id WHERE u.id = ?",
                    "query_type": "SELECT",
                    "frequency_per_hour": 500.0,
                    "calls_per_second": 0.14,
                    "tables_accessed": ["public.users", "public.orders"],
                    "rows_returned_avg": 5.0,
                    "rows_returned_p95": 15.0,
                    "execution_time_ms_avg": 8.5,
                    "execution_time_ms_min": 3.0,
                    "execution_time_ms_max": 50.0,
                    "execution_time_ms_p50": 7.0,
                    "execution_time_ms_p95": 20.0,
                    "execution_time_ms_p99": 35.0,
                    "total_time_ms": 4250.0,
                    "db_load_contribution_percent": 22.0,
                    "has_joins": True,
                    "join_count": 1,
                    "has_aggregations": False,
                    "has_subqueries": False,
                    "filter_columns": ["id"],
                    "first_seen": "2024-01-15T00:00:00Z",
                    "last_seen": "2024-01-15T10:30:00Z",
                },
            ],
            "total_queries_analyzed": 50000,
            "query_log_source": "performance_insights",
            "collection_start_time": "2024-01-15T00:00:00Z",
            "collection_end_time": "2024-01-15T10:30:00Z",
        },
        "metrics": {
            "performance_metrics": {
                "avg_query_time_ms": 5.5,
                "p50_query_time_ms": 3.0,
                "p95_query_time_ms": 15.0,
                "p99_query_time_ms": 30.0,
                "queries_per_second": 150.0,
                "connection_pool_usage_percent": 65.0,
                "active_connections_avg": 25.0,
                "active_connections_max": 50.0,
                "transactions_per_second": 120.0,
                "read_iops_avg": 1000.0,
                "write_iops_avg": 200.0,
                "network_throughput_mbps_avg": 50.0,
            },
            "rds_cloudwatch_metrics": {
                "cpu_utilization": {"avg": 45.0, "max": 75.0, "min": 20.0, "p95": 65.0},
                "freeable_memory_gb": {"avg": 20.0, "max": 28.0, "min": 15.0, "p95": 18.0},
                "database_connections": {"avg": 25.0, "max": 50.0, "min": 10.0, "p95": 45.0},
                "read_iops": {"avg": 1000.0, "max": 2000.0, "min": 500.0, "p95": 1800.0},
                "write_iops": {"avg": 200.0, "max": 500.0, "min": 50.0, "p95": 400.0},
                "read_latency_ms": {"avg": 2.5, "max": 10.0, "min": 1.0, "p95": 8.0},
                "write_latency_ms": {"avg": 5.0, "max": 20.0, "min": 2.0, "p95": 15.0},
                "network_receive_throughput_mbps": {
                    "avg": 30.0,
                    "max": 80.0,
                    "min": 10.0,
                    "p95": 70.0,
                },
                "network_transmit_throughput_mbps": {
                    "avg": 20.0,
                    "max": 60.0,
                    "min": 5.0,
                    "p95": 50.0,
                },
                "free_storage_space_gb": 250.0,
            },
        },
    }


@pytest.fixture
def minimal_collector_output_data() -> dict[str, Any]:
    """
    Fixture providing minimal valid CollectorOutputContract data.

    This fixture includes only the required fields to create a valid contract,
    useful for testing that optional fields are truly optional.

    Returns:
        dict: A minimal dictionary that can be used to instantiate CollectorOutputContract
    """
    return {
        "contract_version": "3.0",
        "job_id": "minimal-job-001",
        "metadata": {
            "collection_timestamp": "2024-01-15T10:00:00Z",
            "collector_version": "1.0.0",
            "source_database": {
                "engine": "mysql",
                "version": "8.0.32",
                "hostname": "test-db.local",
            },
        },
        "database_schema": {
            "tables": [
                {
                    "table_id": "test.simple_table",
                    "table_name": "simple_table",
                    "row_count": 100,
                    "columns": [
                        {
                            "column_name": "id",
                            "data_type": "int",
                            "nullable": False,
                        }
                    ],
                }
            ]
        },
        "queries": {
            "query_patterns": [
                {
                    "query_id": "q1",
                    "query_text": "SELECT * FROM simple_table",
                    "frequency_per_hour": 10.0,
                    "tables_accessed": ["test.simple_table"],
                }
            ]
        },
        "metrics": {
            "performance_metrics": {},
        },
    }


# Utility functions for creating test data


def create_column(
    name: str = "test_column",
    data_type: str = "varchar",
    nullable: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """
    Utility function to create a column dictionary for testing.

    Args:
        name: Column name
        data_type: Column data type
        nullable: Whether column allows NULL
        **kwargs: Additional column properties

    Returns:
        dict: Column data dictionary
    """
    column_data = {
        "column_name": name,
        "data_type": data_type,
        "nullable": nullable,
    }
    column_data.update(kwargs)
    return column_data


def create_table(
    table_id: str = "test.table",
    table_name: str = "table",
    row_count: int = 1000,
    columns: list[dict[str, Any]] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """
    Utility function to create a table dictionary for testing.

    Args:
        table_id: Unique table identifier
        table_name: Table name
        row_count: Number of rows
        columns: List of column dictionaries (creates default if None)
        **kwargs: Additional table properties

    Returns:
        dict: Table data dictionary
    """
    if columns is None:
        columns = [create_column("id", "integer", False)]

    table_data = {
        "table_id": table_id,
        "table_name": table_name,
        "row_count": row_count,
        "columns": columns,
    }
    table_data.update(kwargs)
    return table_data


def create_query_pattern(
    query_id: str = "test-query",
    query_text: str = "SELECT * FROM test",
    frequency: float = 100.0,
    tables: list[str] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """
    Utility function to create a query pattern dictionary for testing.

    Args:
        query_id: Unique query identifier
        query_text: SQL query text
        frequency: Queries per hour
        tables: List of table IDs accessed
        **kwargs: Additional query pattern properties

    Returns:
        dict: Query pattern data dictionary
    """
    if tables is None:
        tables = ["test.table"]

    query_data = {
        "query_id": query_id,
        "query_text": query_text,
        "frequency_per_hour": frequency,
        "tables_accessed": tables,
    }
    query_data.update(kwargs)
    return query_data


def create_source_database(
    engine: str = "postgresql",
    version: str = "14.7",
    hostname: str = "test-db.local",
    **kwargs: Any,
) -> dict[str, Any]:
    """
    Utility function to create a source database dictionary for testing.

    Args:
        engine: Database engine type
        version: Database version
        hostname: Database hostname
        **kwargs: Additional source database properties

    Returns:
        dict: Source database data dictionary
    """
    db_data = {
        "engine": engine,
        "version": version,
        "hostname": hostname,
    }
    db_data.update(kwargs)
    return db_data


def create_metadata(
    timestamp: str = "2024-01-15T10:00:00Z",
    collector_version: str = "1.0.0",
    source_db: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """
    Utility function to create metadata dictionary for testing.

    Args:
        timestamp: Collection timestamp
        collector_version: Collector version
        source_db: Source database dictionary (creates default if None)
        **kwargs: Additional metadata properties

    Returns:
        dict: Metadata data dictionary
    """
    if source_db is None:
        source_db = create_source_database()

    metadata = {
        "collection_timestamp": timestamp,
        "collector_version": collector_version,
        "source_database": source_db,
    }
    metadata.update(kwargs)
    return metadata


def create_minimal_contract_data(
    job_id: str = "test-job",
    **kwargs: Any,
) -> dict[str, Any]:
    """
    Utility function to create minimal contract data for testing.

    This is useful for quickly creating test data with custom overrides.

    Args:
        job_id: Job identifier
        **kwargs: Additional contract properties to override

    Returns:
        dict: Minimal contract data dictionary
    """
    contract_data = {
        "contract_version": "3.0",
        "job_id": job_id,
        "metadata": create_metadata(),
        "database_schema": {
            "tables": [create_table()],
        },
        "queries": {
            "query_patterns": [create_query_pattern()],
        },
        "metrics": {
            "performance_metrics": {},
        },
    }
    contract_data.update(kwargs)
    return contract_data


@pytest.fixture
def valid_referee_output_data() -> dict[str, Any]:
    """
    Fixture providing a complete valid RefereeOutputContract data dictionary.

    This fixture includes all required fields and many optional fields to provide
    a comprehensive example of valid contract data.

    Returns:
        dict: A dictionary that can be used to instantiate RefereeOutputContract
    """
    return {
        "contract_version": "2.0",
        "recommended_architecture": {
            "databases": [
                {
                    "service": "DynamoDB",
                    "table_count": 5,
                    "rationale": "High-velocity key-value access patterns",
                    "tables": ["users", "sessions", "products", "orders", "inventory"],
                    "confidence_score": 85,
                },
                {
                    "service": "DocumentDB",
                    "table_count": 3,
                    "rationale": "Complex document structures with nested data",
                    "tables": ["product_catalog", "user_profiles", "reviews"],
                    "confidence_score": 78,
                },
            ],
            "architecture_type": "MULTI_DATABASE",
            "architecture_diagram_url": "https://s3.amazonaws.com/diagrams/arch-123.png",
            "rationale": "Workload analysis indicates distinct access patterns requiring specialized databases",
        },
        "table_mappings": [
            {
                "source_table": "public.users",
                "recommended_database": "DynamoDB",
                "confidence_score": 90,
                "alternatives": [
                    {"database": "DocumentDB", "confidence_score": 65},
                ],
                "caching_layer": {
                    "recommended": True,
                    "type": "ElastiCache Redis",
                    "rationale": "High read frequency detected",
                },
            },
            {
                "source_table": "public.products",
                "recommended_database": "DynamoDB",
                "confidence_score": 85,
                "alternatives": [
                    {"database": "Aurora PostgreSQL", "confidence_score": 70},
                ],
            },
            {
                "source_table": "public.product_catalog",
                "recommended_database": "DocumentDB",
                "confidence_score": 88,
                "alternatives": [
                    {"database": "DynamoDB", "confidence_score": 60},
                ],
            },
        ],
        "tco_analysis": {
            "current_monthly_cost": 5000.0,
            "projected_monthly_cost": 3500.0,
            "savings_percent": 30.0,
            "cost_breakdown": [
                {"database": "DynamoDB", "monthly_cost_usd": 2000.0},
                {"database": "DocumentDB", "monthly_cost_usd": 1500.0},
            ],
            "three_year_tco": {
                "current": 180000.0,
                "projected": 126000.0,
                "savings": 54000.0,
            },
            "assumptions": [
                "Based on current workload patterns",
                "Assumes reserved capacity pricing",
                "Includes data transfer costs",
            ],
        },
        "risk_assessment": {
            "overall_risk_level": "MEDIUM",
            "risks": [
                {
                    "risk_id": "RISK-001",
                    "risk_type": "MIGRATION_COMPLEXITY",
                    "severity": "MEDIUM",
                    "description": "Complex join patterns require application refactoring",
                    "affected_tables": ["public.users", "public.orders"],
                    "mitigation": "Implement denormalization strategy and update application logic",
                },
                {
                    "risk_id": "RISK-002",
                    "risk_type": "PERFORMANCE_DEGRADATION",
                    "severity": "LOW",
                    "description": "Potential latency increase for complex queries",
                    "affected_tables": ["public.product_catalog"],
                    "mitigation": "Add caching layer and optimize query patterns",
                },
            ],
            "mitigation_strategies": [
                "Implement comprehensive testing strategy",
                "Use blue-green deployment for zero-downtime migration",
                "Monitor performance metrics closely during migration",
            ],
        },
        "assessment_report": {
            "contract_version": "2.0",
            "html_url": "https://s3.amazonaws.com/reports/assessment-123.html",
            "pdf_url": "https://s3.amazonaws.com/reports/assessment-123.pdf",
        },
        "schema_design_requested": True,
        "selected_databases": ["DynamoDB", "DocumentDB"],
    }


@pytest.fixture
def minimal_referee_output_data() -> dict[str, Any]:
    """
    Fixture providing minimal valid RefereeOutputContract data.

    This fixture includes only the required fields to create a valid contract,
    useful for testing that optional fields are truly optional.

    Returns:
        dict: A minimal dictionary that can be used to instantiate RefereeOutputContract
    """
    return {
        "contract_version": "2.0",
        "recommended_architecture": {
            "databases": [
                {
                    "service": "DynamoDB",
                    "table_count": 1,
                    "rationale": "Simple key-value access pattern",
                }
            ],
            "architecture_type": "SINGLE_DATABASE",
        },
        "table_mappings": [
            {
                "source_table": "public.users",
                "recommended_database": "DynamoDB",
                "confidence_score": 75,
            }
        ],
        "tco_analysis": {
            "current_monthly_cost": 1000.0,
            "projected_monthly_cost": 800.0,
            "savings_percent": 20.0,
        },
        "risk_assessment": {
            "overall_risk_level": "LOW",
            "risks": [
                {
                    "risk_id": "RISK-001",
                    "risk_type": "MIGRATION_COMPLEXITY",
                    "severity": "LOW",
                    "description": "Minimal migration complexity",
                }
            ],
        },
    }
