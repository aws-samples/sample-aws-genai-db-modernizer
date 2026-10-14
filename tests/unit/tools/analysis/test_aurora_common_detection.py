"""Tests for common relational pattern detection accuracy."""

import pytest

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


# ==========================================================================
# The scan skips only admin statements, never an application query typed OTHER (#734)
#
# The collector types OTHER any statement whose first word is not a DML
# keyword: a WITH-led CTE query, REFRESH, EXEC, or one that opens with a
# comment or "(". Those are application queries, often the most relational
# ones in the workload, so the pattern scan must look at them. Only real
# admin or utility statements are skipped, by the same rule the anti-05 loop
# uses (#733): SHOW/DESCRIBE/EXPLAIN/SET, or ``is_utility_statement``.
# ==========================================================================

_ONE_TABLE = [{"table_id": "t1", "columns": [], "primary_key": ["id"], "foreign_keys": []}]


def _query(qid: str, sql: str, query_type: str, **extra) -> dict:
    return {
        "query_id": qid,
        "query_type": query_type,
        "query_text": sql,
        "tables_accessed": ["t1"],
        "calls_per_second": 1.0,
        "has_joins": False,
        "join_count": 0,
        "rows_returned_avg": 1.0,
        **extra,
    }


def _pattern_query_ids(queries: list[dict]) -> dict[str, list[str]]:
    patterns, _ = analyze_aurora_common_use_cases(_make_collector(_ONE_TABLE, queries))
    return {p.pattern_id: list(p.query_ids or []) for p in patterns}


class TestScanSkipsOnlyAdminStatements:
    def test_a_with_query_typed_other_is_scanned_for_aggregation(self):
        """The discourse shape: WITH ... GROUP BY ... HAVING."""
        sql = (
            "WITH ips AS (SELECT ip_address FROM t1 WHERE action_type = $1) "
            "SELECT ip_address FROM ips GROUP BY ip_address HAVING COUNT(*) >= $2"
        )
        found = _pattern_query_ids([_query("cte", sql, "OTHER")])
        assert found.get("aurora-common-02") == ["cte"]

    def test_a_with_query_typed_other_is_scanned_for_reporting(self):
        sql = "WITH s AS (SELECT a, SUM(v) AS total FROM t1 GROUP BY a) SELECT * FROM s"
        found = _pattern_query_ids([_query("report", sql, "OTHER", rows_returned_avg=500.0)])
        assert found.get("aurora-common-05") == ["report"]

    def test_a_query_opening_with_a_comment_is_scanned_for_pagination(self):
        sql = "-- list page\nSELECT * FROM t1 WHERE a = $1 ORDER BY b LIMIT $2 OFFSET $3"
        found = _pattern_query_ids([_query("page", sql, "OTHER")])
        assert found.get("aurora-common-08") == ["page"]

    def test_a_parenthesised_union_typed_other_is_scanned_for_subqueries(self):
        sql = (
            "(SELECT id FROM t1 WHERE EXISTS (SELECT 1 FROM t2 WHERE t2.ref = t1.id)) "
            "UNION (SELECT id FROM t1 WHERE a = $1)"
        )
        found = _pattern_query_ids([_query("union", sql, "OTHER")])
        assert found.get("aurora-common-06") == ["union"]

    def test_an_other_typed_join_is_scanned_for_complex_joins(self):
        sql = (
            "WITH r AS (SELECT * FROM t1 JOIN t2 ON t2.a = t1.id JOIN t3 ON t3.b = t2.id "
            "JOIN t4 ON t4.c = t3.id) SELECT * FROM r"
        )
        query = _query("join", sql, "OTHER", has_joins=True, join_count=3)
        query["tables_accessed"] = ["t1", "t2", "t3", "t4"]
        found = _pattern_query_ids([query])
        assert found.get("aurora-common-01") == ["join"]

    @pytest.mark.parametrize(
        "sql, query_type",
        [
            ("SHOW TABLE STATUS WHERE Rows > 0 GROUP BY Engine", "SHOW"),
            ("SET @n = (SELECT COUNT(*) FROM t1)", "SET"),
            ("EXPLAIN SELECT a, COUNT(*) FROM t1 GROUP BY a", "EXPLAIN"),
            ("DESCRIBE t1", "DESCRIBE"),
            ("CREATE MATERIALIZED VIEW mv AS SELECT a, COUNT(*) FROM t1 GROUP BY a", "OTHER"),
            ("SET statement_timeout = 0", "OTHER"),
            ("BEGIN", "OTHER"),
        ],
    )
    def test_admin_and_utility_statements_are_still_skipped(self, sql, query_type):
        found = _pattern_query_ids([_query("admin", sql, query_type)])
        assert all("admin" not in ids for ids in found.values()), found

    def test_a_catalog_read_typed_select_is_a_utility_statement_and_skipped(self):
        """``is_utility_statement``: a query whose tables are all catalog tables."""
        query = _query("catalog", "SELECT COUNT(*) FROM information_schema.tables", "SELECT")
        query["tables_accessed"] = ["information_schema.tables"]
        found = _pattern_query_ids([query])
        assert all("catalog" not in ids for ids in found.values()), found
