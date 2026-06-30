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
