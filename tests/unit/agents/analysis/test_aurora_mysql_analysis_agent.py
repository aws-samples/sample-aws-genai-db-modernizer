"""Tests for Aurora MySQL analysis agent."""

import pytest

from src.agents.analysis.aurora_mysql_analysis_agent import (
    analyze_for_aurora_mysql,
    analyze_for_aurora_mysql_deterministic,
    apply_aurora_mysql_llm_output,
    prepare_aurora_mysql_llm_input,
)
from src.contracts.analysis_input import AnalysisInput, TargetDatabase
from src.contracts.analysis_output import TargetDatabase as OutputTargetDB

# ==========================================================================
# Fixtures
# ==========================================================================


def _make_input(tables, queries):
    """Build AnalysisInput from raw tables and queries."""
    return AnalysisInput(
        job_id="test-job",
        collector_output={
            "database_schema": {"tables": tables},
            "queries": {"query_patterns": queries},
        },
        target_database=TargetDatabase.aurora_mysql,
    )


@pytest.fixture
def ecommerce_fixture():
    """E-commerce workload with complex joins, aggregations, FK density."""
    tables = [
        {
            "table_id": "orders",
            "table_name": "orders",
            "columns": [
                {"column_name": "id", "data_type": "int"},
                {"column_name": "customer_id", "data_type": "int"},
                {"column_name": "product_id", "data_type": "int"},
                {"column_name": "total", "data_type": "decimal"},
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
            "columns": [
                {"column_name": "id", "data_type": "int"},
                {"column_name": "name", "data_type": "varchar"},
            ],
            "primary_key": {"columns": ["id"]},
            "foreign_keys": [],
            "row_count": 50000,
            "size_mb": 10.0,
        },
    ]
    queries = [
        {
            "query_id": "q1",
            "query_type": "SELECT",
            "query_text": (
                "SELECT o.id, c.name, SUM(o.total) "
                "FROM orders o "
                "JOIN customers c ON o.customer_id = c.id "
                "JOIN products p ON o.product_id = p.id "
                "JOIN warehouses w ON o.warehouse_id = w.id "
                "GROUP BY c.name HAVING COUNT(*) > 5"
            ),
            "tables_accessed": ["orders", "customers"],
            "calls_per_second": 5.0,
            "has_joins": True,
            "join_count": 4,
            "rows_returned_avg": 100.0,
            "execution_time_ms_avg": 50.0,
            "db_load_contribution_percent": 20.0,
        },
    ]
    return _make_input(tables, queries)


@pytest.fixture
def mysql_features_fixture():
    """Workload with MySQL-specific features: JSON, FULLTEXT, ON DUPLICATE KEY."""
    tables = [
        {
            "table_id": "products",
            "table_name": "products",
            "columns": [
                {"column_name": "id", "data_type": "int"},
                {"column_name": "name", "data_type": "varchar"},
                {"column_name": "description", "data_type": "text"},
                {"column_name": "attributes", "data_type": "json"},
                {"column_name": "tags", "data_type": "varchar"},
            ],
            "primary_key": {"columns": ["id"]},
            "foreign_keys": [],
            "row_count": 10000,
            "size_mb": 20.0,
        },
    ]
    queries = [
        {
            "query_id": "q1",
            "query_type": "SELECT",
            "query_text": "SELECT id, JSON_EXTRACT(attributes, '$.color') FROM products WHERE JSON_CONTAINS(attributes, '\"red\"', '$.tags')",
            "tables_accessed": ["products"],
            "calls_per_second": 10.0,
            "has_joins": False,
            "join_count": 0,
            "rows_returned_avg": 20.0,
            "execution_time_ms_avg": 5.0,
            "db_load_contribution_percent": 5.0,
        },
        {
            "query_id": "q2",
            "query_type": "SELECT",
            "query_text": "SELECT id, name FROM products WHERE MATCH(name, description) AGAINST('wireless headphones' IN NATURAL LANGUAGE MODE)",
            "tables_accessed": ["products"],
            "calls_per_second": 8.0,
            "has_joins": False,
            "join_count": 0,
            "rows_returned_avg": 15.0,
            "execution_time_ms_avg": 3.0,
            "db_load_contribution_percent": 3.0,
        },
        {
            "query_id": "q3",
            "query_type": "INSERT",
            "query_text": "INSERT INTO products (id, name, attributes) VALUES (?, ?, ?) ON DUPLICATE KEY UPDATE attributes = VALUES(attributes)",
            "tables_accessed": ["products"],
            "calls_per_second": 5.0,
            "has_joins": False,
            "join_count": 0,
            "rows_returned_avg": 0.0,
            "execution_time_ms_avg": 2.0,
            "db_load_contribution_percent": 2.0,
        },
        {
            "query_id": "q4",
            "query_type": "SELECT",
            "query_text": "SELECT category, GROUP_CONCAT(name ORDER BY name SEPARATOR ', ') FROM products GROUP BY category",
            "tables_accessed": ["products"],
            "calls_per_second": 2.0,
            "has_joins": False,
            "join_count": 0,
            "rows_returned_avg": 30.0,
            "execution_time_ms_avg": 10.0,
            "db_load_contribution_percent": 2.0,
        },
    ]
    return _make_input(tables, queries)


@pytest.fixture
def multi_table_update_fixture():
    """Workload with MySQL multi-table UPDATE syntax."""
    tables = [
        {
            "table_id": "inventory",
            "table_name": "inventory",
            "columns": [
                {"column_name": "id", "data_type": "int"},
                {"column_name": "product_id", "data_type": "int"},
                {"column_name": "quantity", "data_type": "int"},
            ],
            "primary_key": {"columns": ["id"]},
            "foreign_keys": [{"referenced_table": "products", "columns": ["product_id"]}],
            "row_count": 10000,
            "size_mb": 5.0,
        },
    ]
    queries = [
        {
            "query_id": "q1",
            "query_type": "UPDATE",
            "query_text": "UPDATE inventory i JOIN products p ON i.product_id = p.id SET i.quantity = i.quantity - 1 WHERE p.status = 'active'",
            "tables_accessed": ["inventory", "products"],
            "calls_per_second": 20.0,
            "has_joins": True,
            "join_count": 1,
            "rows_returned_avg": 10.0,
            "execution_time_ms_avg": 5.0,
            "db_load_contribution_percent": 10.0,
        },
    ]
    return _make_input(tables, queries)


# ==========================================================================
# Tests
# ==========================================================================


class TestAuroraMysqlBasic:
    """Basic agent behavior tests."""

    def test_returns_valid_contract(self, ecommerce_fixture):
        result, trace, mermaid = analyze_for_aurora_mysql(ecommerce_fixture)
        assert result.contract_version == "2.1"
        assert result.agent_metadata.agent_name == "aurora-mysql-analysis-agent"
        assert result.agent_metadata.agent_version == "1.0.0"
        assert result.agent_metadata.target_database == OutputTargetDB.AURORA_MYSQL

    def test_returns_empty_mermaid(self, ecommerce_fixture):
        _, _, mermaid = analyze_for_aurora_mysql(ecommerce_fixture)
        assert mermaid == ""

    def test_empty_collector_output(self):
        inp = _make_input([], [])
        result, trace, _ = analyze_for_aurora_mysql(inp)
        assert result.table_recommendations == []
        assert result.workload_analysis.patterns_detected == []

    def test_decision_trace_structure(self, ecommerce_fixture):
        _, trace, _ = analyze_for_aurora_mysql(ecommerce_fixture)
        assert trace["trace_version"] == "1.0"
        assert trace["agent"] == "aurora-mysql-analysis-agent"
        assert "summary" in trace
        assert "query_matches" in trace
        assert "pattern_summaries" in trace
        assert "recommendation_derivations" in trace
        assert trace["llm_advisor"]["status"] == "skipped"


class TestAuroraMysqlPatternDetection:
    """Pattern detection tests."""

    def test_detects_complex_joins(self, ecommerce_fixture):
        result, _, _ = analyze_for_aurora_mysql(ecommerce_fixture)
        pattern_ids = [p.pattern_id for p in result.workload_analysis.patterns_detected]
        assert "aurora-common-01" in pattern_ids

    def test_detects_aggregation(self, ecommerce_fixture):
        result, _, _ = analyze_for_aurora_mysql(ecommerce_fixture)
        pattern_ids = [p.pattern_id for p in result.workload_analysis.patterns_detected]
        assert "aurora-common-02" in pattern_ids

    def test_detects_json_document_query(self, mysql_features_fixture):
        result, _, _ = analyze_for_aurora_mysql(mysql_features_fixture)
        pattern_ids = [p.pattern_id for p in result.workload_analysis.patterns_detected]
        assert "aurora-mysql-01" in pattern_ids

    def test_detects_fulltext_match(self, mysql_features_fixture):
        result, _, _ = analyze_for_aurora_mysql(mysql_features_fixture)
        pattern_ids = [p.pattern_id for p in result.workload_analysis.patterns_detected]
        assert "aurora-mysql-02" in pattern_ids

    def test_detects_on_duplicate_key(self, mysql_features_fixture):
        result, _, _ = analyze_for_aurora_mysql(mysql_features_fixture)
        pattern_ids = [p.pattern_id for p in result.workload_analysis.patterns_detected]
        assert "aurora-mysql-03" in pattern_ids

    def test_detects_group_concat(self, mysql_features_fixture):
        result, _, _ = analyze_for_aurora_mysql(mysql_features_fixture)
        pattern_ids = [p.pattern_id for p in result.workload_analysis.patterns_detected]
        assert "aurora-mysql-04" in pattern_ids

    def test_detects_multi_table_update(self, multi_table_update_fixture):
        result, _, _ = analyze_for_aurora_mysql(multi_table_update_fixture)
        pattern_ids = [p.pattern_id for p in result.workload_analysis.patterns_detected]
        assert "aurora-mysql-06" in pattern_ids


class TestAuroraMysqlScoring:
    """Scoring and confidence tests."""

    def test_complex_relational_scores_high(self, ecommerce_fixture):
        result, _, _ = analyze_for_aurora_mysql(ecommerce_fixture)
        orders_rec = next(r for r in result.table_recommendations if r.table_id == "orders")
        assert orders_rec.confidence_score >= 70

    def test_mysql_features_boost_score(self, mysql_features_fixture):
        result, _, _ = analyze_for_aurora_mysql(mysql_features_fixture)
        products_rec = next(r for r in result.table_recommendations if r.table_id == "products")
        # JSON + FULLTEXT + ON DUPLICATE KEY + GROUP_CONCAT = good MySQL fit
        assert products_rec.confidence_score >= 60


class TestAuroraMysqlLlmSeam:
    """LLM Seam Pattern tests."""

    def test_deterministic_matches_none_mode(self, ecommerce_fixture):
        det_result, _, _ = analyze_for_aurora_mysql_deterministic(ecommerce_fixture)
        none_result, _, _ = analyze_for_aurora_mysql(ecommerce_fixture, llm_mode="none")
        # Compare structure (timestamps will differ between calls)
        assert len(det_result.table_recommendations) == len(none_result.table_recommendations)
        assert len(det_result.workload_analysis.patterns_detected) == len(
            none_result.workload_analysis.patterns_detected
        )
        assert det_result.agent_metadata.agent_name == none_result.agent_metadata.agent_name

    def test_external_mode_sets_awaiting(self, ecommerce_fixture):
        _, trace, _ = analyze_for_aurora_mysql(ecommerce_fixture, llm_mode="external")
        assert trace["llm_advisor"]["status"] == "awaiting_external"

    def test_prepare_llm_input_returns_dict(self, ecommerce_fixture):
        result, _, _ = analyze_for_aurora_mysql_deterministic(ecommerce_fixture)
        payload = prepare_aurora_mysql_llm_input(result, ecommerce_fixture)
        assert "deterministic_results" in payload
        assert "schema" in payload
        assert "queries" in payload

    def test_apply_llm_output_none_returns_unchanged(self, ecommerce_fixture):
        result, _, _ = analyze_for_aurora_mysql_deterministic(ecommerce_fixture)
        updated = apply_aurora_mysql_llm_output(result, None)
        assert updated.model_dump() == result.model_dump()


class TestAuroraMysqlCostEstimation:
    """Cost estimation tests."""

    def test_cost_has_both_options(self, ecommerce_fixture):
        result, _, _ = analyze_for_aurora_mysql(ecommerce_fixture)
        cost = result.cost_estimate
        assert cost.monthly_cost_usd > 0
        assert "serverless_v2" in cost.cost_components
        assert "provisioned" in cost.cost_components

    def test_mysql_pricing_differs_from_pg(self, ecommerce_fixture):
        # MySQL is slightly cheaper per-instance than PG
        result_mysql, _, _ = analyze_for_aurora_mysql(ecommerce_fixture)
        mysql_provisioned = result_mysql.cost_estimate.cost_components["provisioned"][
            "compute_monthly_usd"
        ]
        # db.r6g.large MySQL = $0.22/hr vs PG = $0.24/hr
        expected_mysql = 0.22 * 24 * 30
        assert abs(mysql_provisioned - expected_mysql) < 1.0
