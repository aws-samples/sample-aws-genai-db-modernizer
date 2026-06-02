"""Fixtures for schema design contract tests."""

import pytest


@pytest.fixture
def sample_collector_input():
    """Minimal valid AgentCollectorInput data."""
    return {
        "contract_version": "3.0",
        "job_id": "test-job-001",
        "source_database_name": "forum_db",
        "source_database_engine": "mysql",
        "collection_timestamp": "2024-01-15T10:00:00Z",
        "tables": [
            {
                "table_id": "forum_db.users",
                "table_name": "users",
                "row_count": 5000,
                "size_mb": 2.5,
                "columns": [
                    {
                        "column_name": "id",
                        "ordinal_position": 1,
                        "normalized_data_type": "integer",
                        "nullable": False,
                        "is_auto_increment": True,
                    },
                    {
                        "column_name": "username",
                        "ordinal_position": 2,
                        "normalized_data_type": "string",
                        "max_length": 50,
                        "nullable": False,
                    },
                    {
                        "column_name": "email",
                        "ordinal_position": 3,
                        "normalized_data_type": "string",
                        "max_length": 255,
                        "nullable": False,
                    },
                ],
                "indexes": [
                    {
                        "index_name": "PRIMARY",
                        "columns": ["id"],
                        "is_unique": True,
                        "is_primary": True,
                    },
                    {
                        "index_name": "idx_email",
                        "columns": ["email"],
                        "is_unique": True,
                        "is_primary": False,
                    },
                ],
                "primary_key": ["id"],
            },
            {
                "table_id": "forum_db.posts",
                "table_name": "posts",
                "row_count": 50000,
                "size_mb": 25.0,
                "columns": [
                    {
                        "column_name": "id",
                        "ordinal_position": 1,
                        "normalized_data_type": "integer",
                        "nullable": False,
                    },
                    {
                        "column_name": "discussion_id",
                        "ordinal_position": 2,
                        "normalized_data_type": "integer",
                        "nullable": False,
                    },
                    {
                        "column_name": "user_id",
                        "ordinal_position": 3,
                        "normalized_data_type": "integer",
                        "nullable": False,
                    },
                    {
                        "column_name": "body",
                        "ordinal_position": 4,
                        "normalized_data_type": "text",
                        "nullable": False,
                    },
                ],
                "primary_key": ["id"],
                "foreign_keys": [
                    {
                        "constraint_name": "fk_posts_user",
                        "columns": ["user_id"],
                        "referenced_table": "forum_db.users",
                        "referenced_columns": ["id"],
                        "on_delete": "CASCADE",
                    },
                ],
            },
        ],
        "queries": {
            "query_patterns": [
                {
                    "query_id": "q1",
                    "query_text": "SELECT * FROM users WHERE id = ?",
                    "query_type": "SELECT",
                    "frequency_per_hour": 3600.0,
                    "calls_per_second": 1.0,
                    "tables_accessed": ["forum_db.users"],
                    "rows_returned_avg": 1.0,
                    "execution_time_ms_avg": 2.0,
                },
                {
                    "query_id": "q2",
                    "query_text": "SELECT p.* FROM posts p WHERE p.discussion_id = ? ORDER BY p.id",
                    "query_type": "SELECT",
                    "frequency_per_hour": 1800.0,
                    "calls_per_second": 0.5,
                    "tables_accessed": ["forum_db.posts"],
                    "rows_returned_avg": 10.0,
                    "execution_time_ms_avg": 5.0,
                },
                {
                    "query_id": "q3",
                    "query_text": "INSERT INTO posts (discussion_id, user_id, body) VALUES (?, ?, ?)",
                    "query_type": "INSERT",
                    "frequency_per_hour": 360.0,
                    "calls_per_second": 0.1,
                    "tables_accessed": ["forum_db.posts"],
                    "rows_affected_avg": 1.0,
                },
            ],
            "total_queries_analyzed": 100,
            "query_log_source": "performance_schema",
        },
    }


@pytest.fixture
def sample_analysis_input():
    """Minimal valid AgentAnalysisInput data."""
    return {
        "contract_version": "2.1",
        "patterns_detected": [
            {
                "pattern_id": "p1",
                "pattern_type": "key-value-lookup",
                "confidence": "HIGH",
                "description": "Single-item lookups by primary key",
                "query_ids": ["q1"],
                "table_ids": ["forum_db.users"],
            },
            {
                "pattern_id": "p2",
                "pattern_type": "range-query",
                "confidence": "HIGH",
                "description": "Range queries on posts by discussion_id",
                "query_ids": ["q2"],
                "table_ids": ["forum_db.posts"],
            },
        ],
        "anti_patterns_detected": [],
        "table_recommendations": [
            {"table_id": "forum_db.users", "confidence_score": 90, "concerns": []},
            {
                "table_id": "forum_db.posts",
                "confidence_score": 85,
                "concerns": ["High write volume"],
            },
        ],
        "aggregate_recommendations": [
            {
                "aggregate_id": "agg-forum",
                "root_table": "forum_db.users",
                "member_tables": ["forum_db.users", "forum_db.posts"],
                "co_access_confidence": 75,
                "combined_migration_complexity": "MEDIUM",
            }
        ],
    }


@pytest.fixture
def sample_dynamodb_output():
    """Minimal valid DynamoDBModelOutputContract data."""
    return {
        "contract_version": "1.0",
        "job_id": "test-job-001",
        "source_database": "forum_db",
        "target_engine": "dynamodb",
        "access_patterns": [
            {
                "pattern_id": "DDB-AP-1",
                "pattern_group": "User reads",
                "query_ids": ["q1"],
                "source_tables": ["forum_db.users"],
                "description": "Get user by ID",
                "operation": "GetItem",
                "table_name": "Users",
                "key_condition": "PK=user_id",
                "design_rps": 30.0,
                "item_size_bytes": 200,
            },
            {
                "pattern_id": "DDB-AP-2",
                "pattern_group": "Post reads",
                "query_ids": ["q2"],
                "source_tables": ["forum_db.posts"],
                "description": "Get posts for discussion",
                "operation": "Query",
                "table_name": "Forum",
                "key_condition": "PK=discussion_id AND SK begins_with 'POST#'",
                "design_rps": 15.0,
                "avg_items_returned": 10.0,
                "item_size_bytes": 500,
            },
        ],
        "table_definitions": [
            {
                "table_name": "Forum",
                "aggregate_pattern": "item_collection",
                "source_tables": ["forum_db.users", "forum_db.posts"],
                "partition_key": {"attribute_name": "pk", "attribute_type": "S"},
                "sort_key": {"attribute_name": "sk", "attribute_type": "S"},
                "entities": [
                    {
                        "entity_type": "USER",
                        "source_table": "forum_db.users",
                        "pk_template": "USER#{id}",
                        "sk_template": "PROFILE",
                        "attributes": [
                            {
                                "name": "user_id",
                                "type": "S",
                                "source_table": "forum_db.users",
                                "source_column": "id",
                            },
                            {
                                "name": "username",
                                "type": "S",
                                "source_table": "forum_db.users",
                                "source_column": "username",
                            },
                        ],
                    },
                ],
                "gsis": [],
                "item_count": 55000,
                "item_size_bytes": 350,
            },
        ],
        "hot_partition_analysis": [
            {
                "table_name": "Forum",
                "operation": "read",
                "rcu_or_wcu_per_second": 450.0,
                "partition_limit": 3000.0,
                "utilization_pct": 15.0,
                "at_risk": False,
                "contributing_patterns": ["q1", "q2"],
            },
        ],
        "trade_offs": [
            {
                "description": "Item collection trades write simplicity for read efficiency",
                "impact": "Writes update a single item collection instead of separate tables, but reads of individual entities need a sort key filter.",
                "source_tables": ["db.users"],
                "target_tables": ["Users"],
                "query_ids": ["q1", "q2"],
                "engine": "dynamodb",
            }
        ],
        "validation_passed": True,
        "validation_failures": [],
    }
