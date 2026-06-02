"""Tests for common relational pattern detection accuracy."""


from src.tools.analysis.aurora_common_analysis_tools import analyze_aurora_common_use_cases


def _make_collector(tables, queries):
    return {
        "database_schema": {"tables": tables},
        "queries": {"query_patterns": queries},
    }


class TestSubqueryDetection:
    """aurora-common-06 correlated-subquery — regex-based."""

    def test_exists_subquery(self):
        collector = _make_collector(
            tables=[
                {
                    "table_id": "orders",
                    "columns": [],
                    "primary_key": {"columns": ["id"]},
                    "foreign_keys": [],
                    "row_count": 1000,
                    "size_mb": 1.0,
                }
            ],
            queries=[
                {
                    "query_id": "q1",
                    "query_type": "SELECT",
                    "query_text": "SELECT * FROM orders o WHERE EXISTS (SELECT 1 FROM items i WHERE i.order_id = o.id)",
                    "tables_accessed": ["orders"],
                    "calls_per_second": 1.0,
                    "has_joins": False,
                    "join_count": 0,
                    "rows_returned_avg": 10.0,
                }
            ],
        )
        patterns, _ = analyze_aurora_common_use_cases(collector)
        pattern_ids = [p.pattern_id for p in patterns]
        assert "aurora-common-06" in pattern_ids

    def test_not_exists_subquery(self):
        collector = _make_collector(
            tables=[
                {
                    "table_id": "customers",
                    "columns": [],
                    "primary_key": {"columns": ["id"]},
                    "foreign_keys": [],
                    "row_count": 5000,
                    "size_mb": 2.0,
                }
            ],
            queries=[
                {
                    "query_id": "q1",
                    "query_type": "SELECT",
                    "query_text": "SELECT * FROM customers c WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.id)",
                    "tables_accessed": ["customers"],
                    "calls_per_second": 1.0,
                    "has_joins": False,
                    "join_count": 0,
                    "rows_returned_avg": 10.0,
                }
            ],
        )
        patterns, _ = analyze_aurora_common_use_cases(collector)
        pattern_ids = [p.pattern_id for p in patterns]
        assert "aurora-common-06" in pattern_ids

    def test_in_select_subquery(self):
        collector = _make_collector(
            tables=[
                {
                    "table_id": "products",
                    "columns": [],
                    "primary_key": {"columns": ["id"]},
                    "foreign_keys": [],
                    "row_count": 10000,
                    "size_mb": 5.0,
                }
            ],
            queries=[
                {
                    "query_id": "q1",
                    "query_type": "SELECT",
                    "query_text": "SELECT * FROM products WHERE category_id IN (SELECT id FROM categories WHERE active = true)",
                    "tables_accessed": ["products"],
                    "calls_per_second": 2.0,
                    "has_joins": False,
                    "join_count": 0,
                    "rows_returned_avg": 50.0,
                }
            ],
        )
        patterns, _ = analyze_aurora_common_use_cases(collector)
        pattern_ids = [p.pattern_id for p in patterns]
        assert "aurora-common-06" in pattern_ids

    def test_column_named_exists_no_false_positive(self):
        """Column or value containing 'exists' should NOT trigger."""
        collector = _make_collector(
            tables=[
                {
                    "table_id": "checks",
                    "columns": [],
                    "primary_key": {"columns": ["id"]},
                    "foreign_keys": [],
                    "row_count": 100,
                    "size_mb": 0.1,
                }
            ],
            queries=[
                {
                    "query_id": "q1",
                    "query_type": "SELECT",
                    "query_text": "SELECT * FROM checks WHERE file_exists = true",
                    "tables_accessed": ["checks"],
                    "calls_per_second": 1.0,
                    "has_joins": False,
                    "join_count": 0,
                    "rows_returned_avg": 1.0,
                }
            ],
        )
        patterns, _ = analyze_aurora_common_use_cases(collector)
        pattern_ids = [p.pattern_id for p in patterns]
        assert "aurora-common-06" not in pattern_ids
