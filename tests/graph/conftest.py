"""Shared fixtures for graph tests."""

import pytest

from src.graph.schema import initialize_schema
from src.graph.store import GraphStore


@pytest.fixture
def graph_store(tmp_path):
    """Fresh graph with schema initialized."""
    store = GraphStore(str(tmp_path / "test.lbug"))
    initialize_schema(store)
    yield store
    store.close()


@pytest.fixture
def sample_collector_output():
    """Minimal collector output with 3 queries and 2 tables."""
    return {
        "queries": {
            "query_patterns": [
                {
                    "query_id": "q1",
                    "query_text": "SELECT * FROM orders WHERE customer_id = ?",
                    "query_type": "SELECT",
                    "tables_accessed": ["orders"],
                    "calls_per_second": 423.0,
                },
                {
                    "query_id": "q2",
                    "query_text": "SELECT o.*, c.name FROM orders o JOIN customers c ON o.customer_id = c.id",
                    "query_type": "SELECT",
                    "tables_accessed": ["orders", "customers"],
                    "calls_per_second": 89.0,
                },
                {
                    "query_id": "q3",
                    "query_text": "INSERT INTO orders (customer_id, total) VALUES (?, ?)",
                    "query_type": "INSERT",
                    "tables_accessed": ["orders"],
                    "calls_per_second": 15.0,
                },
            ]
        }
    }


@pytest.fixture
def sample_triage_output():
    """Minimal triage output with 2 signals."""
    return {
        "signals": [
            {
                "signal": "key_value_lookups",
                "targets": ["dynamodb", "elasticache"],
                "evidence": "key-value lookup queries (PK reads)",
                "query_ids": ["q1"],
                "table_ids": ["orders"],
                "query_count": 1,
            },
            {
                "signal": "complex_joins",
                "targets": ["documentdb", "aurora"],
                "evidence": "complex join queries (3+ tables)",
                "query_ids": ["q2"],
                "table_ids": ["orders", "customers"],
                "query_count": 1,
            },
        ]
    }


@pytest.fixture
def sample_assignment():
    """Minimal assignment with 3 queries across 2 engines."""
    return {
        "query_assignments": [
            {
                "query_id": "q1",
                "assigned_engine": "dynamodb",
                "confidence": 0.92,
                "source_tables": ["orders"],
                "assignment_reason": "Key-value lookup pattern with high traffic",
                "in_scope": True,
            },
            {
                "query_id": "q2",
                "assigned_engine": "documentdb",
                "confidence": 0.78,
                "source_tables": ["orders", "customers"],
                "assignment_reason": "Join pattern suited for document model",
                "in_scope": True,
            },
            {
                "query_id": "q3",
                "assigned_engine": "dynamodb",
                "confidence": 0.85,
                "source_tables": ["orders"],
                "assignment_reason": "Write pattern for DynamoDB",
                "in_scope": True,
            },
        ],
        "table_assignments": [
            {
                "table_id": "orders",
                "primary_engine": "dynamodb",
                "engines": ["dynamodb", "documentdb"],
                "query_count": 3,
            },
            {
                "table_id": "customers",
                "primary_engine": "documentdb",
                "engines": ["documentdb"],
                "query_count": 1,
            },
        ],
        "co_dependency_groups": [
            {
                "group_id": "grp-1",
                "query_ids": ["q1", "q3"],
                "reason": "Both access orders with same key pattern",
            },
        ],
    }


@pytest.fixture
def sample_analysis_output():
    """Minimal analysis output with 1 anti-pattern."""
    return {
        "workload_analysis": {
            "anti_patterns_detected": [
                {
                    "anti_pattern_id": "ap-1",
                    "anti_pattern_type": "hot-partition",
                    "severity_weight": 0.7,
                    "description": "customer_id=42 receives 30% of traffic",
                    "query_ids": ["q1"],
                    "table_ids": ["orders"],
                    "recommendation": "Add write sharding or spread traffic across partition keys",
                },
            ]
        }
    }


@pytest.fixture
def sample_reality_check_output():
    """Minimal reality check with 1 consolidation."""
    return {
        "consolidations": [
            {
                "from_engine": "opensearch",
                "to_engine": "dynamodb",
                "query_count": 5,
                "reason": "OpenSearch had no unique value; all queries can be served by DynamoDB",
                "saved_cost_estimate": 45.0,
                "action": "full",
                "queries_retained": [],
            }
        ]
    }


@pytest.fixture
def sample_schema_design_output():
    """Minimal schema design with 1 trade-off."""
    return {
        "trade_offs": [
            {
                "description": "Denormalized customer data into orders collection",
                "impact": "Faster reads, stale data risk on customer updates",
                "source_tables": ["orders", "customers"],
                "target_tables": ["orders-collection"],
                "query_ids": ["q2"],
                "engine": "documentdb",
            }
        ],
        "access_patterns": [
            {
                "pattern_id": "DDB-AP-1",
                "pattern_group": "Order reads",
                "query_ids": ["q1", "q3"],
                "description": "Fetch orders by customer",
                "operation": "GetItem / Query",
                "table_name": "orders-by-customer",
                "design_rps": 1200.0,
                "in_scope": True,
            }
        ],
    }


@pytest.fixture
def sample_router_output():
    """Minimal post-schema router output with 1 reroute."""
    return {
        "routings": [
            {
                "query_id": "q2",
                "from_engine": "documentdb",
                "to_engine": "aurora_mysql",
                "reason": "Complex aggregation not supported in DocumentDB schema",
                "cascade_depth": 0,
            }
        ],
        "terminal_queries": [],
    }


@pytest.fixture
def sample_load_test_output():
    """Minimal load test output with 1 pattern result."""
    return {
        "target_engine": "dynamodb",
        "version": 1,
        "pattern_results": [
            {
                "query_id": "q1",
                "operation_type": "GetItem",
                "source_latency_ms": {
                    "p50": 45.0,
                    "p90": 66.0,
                    "p95": 88.0,
                    "p99": 132.0,
                    "p999": 220.0,
                    "min": 13.0,
                    "max": 440.0,
                },
                "target_latency_ms": {
                    "p50": 3.0,
                    "p90": 4.5,
                    "p95": 6.0,
                    "p99": 9.0,
                    "p999": 15.0,
                    "min": 1.0,
                    "max": 30.0,
                },
                "improvement_factor": 15.0,
                "throughput_rps": 12000.0,
                "error_rate_pct": 0.01,
                "cost_per_operation_usd": 0.0000012,
            }
        ],
        "total_cost_usd": 0.05,
    }


@pytest.fixture
def sample_synthesis_output():
    """Minimal synthesis output with 1 risk."""
    return {
        "risk_assessment": {
            "overall_risk_level": "MEDIUM",
            "risks": [
                {
                    "risk_id": "risk-1",
                    "risk_type": "DATA_CONSISTENCY",
                    "severity": "MEDIUM",
                    "description": "Cross-table transactions on orders+customers cannot be atomic in DynamoDB",
                    "affected_tables": ["orders", "customers"],
                    "mitigation": "Use DynamoDB transactions or implement saga pattern",
                }
            ],
        }
    }
