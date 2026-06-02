"""Integration test: validates graduated scoring produces honest differentiation."""


from src.agents.analysis.aurora_pg_analysis_agent import analyze_for_aurora_pg
from src.contracts.analysis_input import AnalysisInput, TargetDatabase


def _make_input(tables, queries):
    return AnalysisInput(
        job_id="test-job",
        collector_output={
            "database_schema": {"tables": tables},
            "queries": {"query_patterns": queries},
        },
        target_database=TargetDatabase.aurora_postgresql,
    )


class TestGraduatedScoringDifferentiation:
    """Tables with different relational need should produce meaningfully different scores."""

    def test_isolated_kv_table_scores_below_relational_table(self):
        """A key-value table should score significantly lower than a join-heavy table."""
        tables = [
            {
                "table_id": "sessions",
                "table_name": "sessions",
                "columns": [
                    {"column_name": "id", "data_type": "uuid"},
                    {"column_name": "data", "data_type": "jsonb"},
                ],
                "primary_key": {"columns": ["id"]},
                "foreign_keys": [],
                "row_count": 100000,
                "size_mb": 50.0,
            },
            {
                "table_id": "orders",
                "table_name": "orders",
                "columns": [
                    {"column_name": "id", "data_type": "integer"},
                    {"column_name": "customer_id", "data_type": "integer"},
                    {"column_name": "product_id", "data_type": "integer"},
                ],
                "primary_key": {"columns": ["id"]},
                "foreign_keys": [
                    {"referenced_table": "customers", "columns": ["customer_id"]},
                    {"referenced_table": "products", "columns": ["product_id"]},
                    {"referenced_table": "warehouses", "columns": ["warehouse_id"]},
                ],
                "row_count": 500000,
                "size_mb": 200.0,
            },
            {
                "table_id": "customers",
                "table_name": "customers",
                "columns": [{"column_name": "id", "data_type": "integer"}],
                "primary_key": {"columns": ["id"]},
                "foreign_keys": [],
                "row_count": 50000,
                "size_mb": 10.0,
            },
        ]
        queries = [
            # sessions: pure KV access
            {
                "query_id": "q1",
                "query_type": "SELECT",
                "query_text": "SELECT data FROM sessions WHERE id = $1",
                "tables_accessed": ["sessions"],
                "calls_per_second": 20.0,
                "has_joins": False,
                "join_count": 0,
                "rows_returned_avg": 1.0,
                "execution_time_ms_avg": 1.0,
            },
            # orders: complex join
            {
                "query_id": "q2",
                "query_type": "SELECT",
                "query_text": (
                    "SELECT o.*, c.name FROM orders o "
                    "JOIN customers c ON o.customer_id = c.id "
                    "JOIN products p ON o.product_id = p.id "
                    "WHERE o.created_at > NOW() - INTERVAL '7 days'"
                ),
                "tables_accessed": ["orders", "customers"],
                "calls_per_second": 5.0,
                "has_joins": True,
                "join_count": 3,
                "rows_returned_avg": 50.0,
                "execution_time_ms_avg": 30.0,
            },
        ]

        result, _, _ = analyze_for_aurora_pg(_make_input(tables, queries))

        sessions_rec = next(r for r in result.table_recommendations if r.table_id == "sessions")
        orders_rec = next(r for r in result.table_recommendations if r.table_id == "orders")

        # The key assertion: orders should score meaningfully higher than sessions
        assert orders_rec.confidence_score > sessions_rec.confidence_score + 15, (
            f"Expected meaningful gap: orders={orders_rec.confidence_score}, "
            f"sessions={sessions_rec.confidence_score}"
        )

    def test_anti_patterns_reduce_confidence(self):
        """Tables with anti-patterns should have lower confidence."""
        inp = _make_input(
            tables=[
                {
                    "table_id": "cache_table",
                    "table_name": "cache_table",
                    "columns": [
                        {"column_name": "key", "data_type": "varchar"},
                        {"column_name": "value", "data_type": "text"},
                    ],
                    "primary_key": {"columns": ["key"]},
                    "foreign_keys": [],
                    "row_count": 1000,
                    "size_mb": 0.5,
                }
            ],
            queries=[
                {
                    "query_id": "q1",
                    "query_type": "SELECT",
                    "query_text": "SELECT value FROM cache_table WHERE key = $1",
                    "tables_accessed": ["cache_table"],
                    "calls_per_second": 35.0,  # triggers anti-01 (>30)
                    "has_joins": False,
                    "join_count": 0,
                    "rows_returned_avg": 1.0,
                    "execution_time_ms_avg": 0.5,
                }
            ],
        )
        result, _, _ = analyze_for_aurora_pg(inp)
        rec = result.table_recommendations[0]

        # Should have anti-patterns detected
        anti_ids = [
            ap.anti_pattern_id for ap in (result.workload_analysis.anti_patterns_detected or [])
        ]
        assert "aurora-anti-01" in anti_ids

        # Confidence should be moderate-to-low for a pure KV pattern with anti-patterns
        # (complexity/cost dimensions still contribute, but pattern_match is penalized)
        assert rec.confidence_score < 65, f"Expected <65, got {rec.confidence_score}"
