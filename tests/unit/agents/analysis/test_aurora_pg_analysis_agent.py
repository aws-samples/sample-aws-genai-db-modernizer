"""Tests for Aurora PostgreSQL analysis agent."""

import pytest

from src.agents.analysis.aurora_pg_analysis_agent import (
    analyze_for_aurora_pg,
    analyze_for_aurora_pg_deterministic,
    apply_aurora_pg_llm_output,
    prepare_aurora_pg_llm_input,
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
        target_database=TargetDatabase.aurora_postgresql,
    )


@pytest.fixture
def ecommerce_fixture():
    """E-commerce workload with complex joins, aggregations, FK density."""
    tables = [
        {
            "table_id": "orders",
            "table_name": "orders",
            "columns": [
                {"column_name": "id", "data_type": "integer"},
                {"column_name": "customer_id", "data_type": "integer"},
                {"column_name": "product_id", "data_type": "integer"},
                {"column_name": "total", "data_type": "numeric"},
                {"column_name": "created_at", "data_type": "timestamp"},
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
                {"column_name": "id", "data_type": "integer"},
                {"column_name": "name", "data_type": "varchar"},
                {"column_name": "email", "data_type": "varchar"},
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
                "SELECT o.id, c.name, p.title, SUM(o.total) "
                "FROM orders o "
                "JOIN customers c ON o.customer_id = c.id "
                "JOIN products p ON o.product_id = p.id "
                "JOIN warehouses w ON o.warehouse_id = w.id "
                "GROUP BY c.name HAVING COUNT(*) > 5 "
                "ORDER BY SUM(o.total) DESC LIMIT 100"
            ),
            "tables_accessed": ["orders", "customers"],
            "calls_per_second": 5.0,
            "has_joins": True,
            "join_count": 4,
            "rows_returned_avg": 100.0,
            "execution_time_ms_avg": 50.0,
            "db_load_contribution_percent": 20.0,
        },
        {
            "query_id": "q2",
            "query_type": "SELECT",
            "query_text": (
                "SELECT ROW_NUMBER() OVER(PARTITION BY customer_id ORDER BY created_at DESC) "
                "FROM orders"
            ),
            "tables_accessed": ["orders"],
            "calls_per_second": 3.0,
            "has_joins": False,
            "join_count": 0,
            "rows_returned_avg": 50.0,
            "execution_time_ms_avg": 15.0,
            "db_load_contribution_percent": 5.0,
        },
        {
            "query_id": "q3",
            "query_type": "UPDATE",
            "query_text": "UPDATE orders SET status = 'shipped' WHERE id IN (SELECT id FROM batch_queue)",
            "tables_accessed": ["orders"],
            "calls_per_second": 2.0,
            "has_joins": False,
            "join_count": 0,
            "rows_returned_avg": 50.0,
            "execution_time_ms_avg": 10.0,
            "db_load_contribution_percent": 3.0,
        },
    ]
    return _make_input(tables, queries)


@pytest.fixture
def high_freq_pk_fixture():
    """High-frequency PK lookups — should trigger anti-pattern."""
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
            "row_count": 10000,
            "size_mb": 5.0,
        },
    ]
    queries = [
        {
            "query_id": "q1",
            "query_type": "SELECT",
            "query_text": "SELECT * FROM sessions WHERE id = $1",
            "tables_accessed": ["sessions"],
            "calls_per_second": 500.0,
            "has_joins": False,
            "join_count": 0,
            "rows_returned_avg": 1.0,
            "execution_time_ms_avg": 1.0,
            "db_load_contribution_percent": 10.0,
        },
    ]
    return _make_input(tables, queries)


@pytest.fixture
def pg_features_fixture():
    """Workload with PG-specific features: CTEs, JSONB, LATERAL."""
    tables = [
        {
            "table_id": "categories",
            "table_name": "categories",
            "columns": [
                {"column_name": "id", "data_type": "integer"},
                {"column_name": "parent_id", "data_type": "integer"},
                {"column_name": "name", "data_type": "varchar"},
                {"column_name": "metadata", "data_type": "jsonb"},
            ],
            "primary_key": {"columns": ["id"]},
            "foreign_keys": [{"referenced_table": "categories", "columns": ["parent_id"]}],
            "row_count": 1000,
            "size_mb": 1.0,
        },
    ]
    queries = [
        {
            "query_id": "q1",
            "query_type": "SELECT",
            "query_text": (
                "WITH RECURSIVE tree AS ("
                "  SELECT id, name, parent_id FROM categories WHERE parent_id IS NULL "
                "  UNION ALL "
                "  SELECT c.id, c.name, c.parent_id FROM categories c JOIN tree t ON c.parent_id = t.id"
                ") SELECT * FROM tree"
            ),
            "tables_accessed": ["categories"],
            "calls_per_second": 2.0,
            "has_joins": True,
            "join_count": 1,
            "rows_returned_avg": 50.0,
            "execution_time_ms_avg": 10.0,
            "db_load_contribution_percent": 5.0,
        },
        {
            "query_id": "q2",
            "query_type": "SELECT",
            "query_text": "SELECT id, metadata->>'color' FROM categories WHERE metadata @> '{\"active\": true}'",
            "tables_accessed": ["categories"],
            "calls_per_second": 10.0,
            "has_joins": False,
            "join_count": 0,
            "rows_returned_avg": 20.0,
            "execution_time_ms_avg": 5.0,
            "db_load_contribution_percent": 3.0,
        },
        {
            "query_id": "q3",
            "query_type": "INSERT",
            "query_text": "INSERT INTO categories (name, metadata) VALUES ($1, $2) ON CONFLICT (name) DO UPDATE SET metadata = $2 RETURNING id",
            "tables_accessed": ["categories"],
            "calls_per_second": 1.0,
            "has_joins": False,
            "join_count": 0,
            "rows_returned_avg": 1.0,
            "execution_time_ms_avg": 2.0,
            "db_load_contribution_percent": 1.0,
        },
    ]
    return _make_input(tables, queries)


# ==========================================================================
# Tests
# ==========================================================================


class TestAuroraPgBasic:
    """Basic agent behavior tests."""

    def test_returns_valid_contract(self, ecommerce_fixture):
        result, trace, mermaid = analyze_for_aurora_pg(ecommerce_fixture)
        assert result.contract_version == "2.1"
        assert result.agent_metadata.agent_name == "aurora-pg-analysis-agent"
        assert result.agent_metadata.agent_version == "1.0.0"
        assert result.agent_metadata.target_database == OutputTargetDB.AURORA_POSTGRESQL

    def test_returns_empty_mermaid(self, ecommerce_fixture):
        _, _, mermaid = analyze_for_aurora_pg(ecommerce_fixture)
        assert mermaid == ""

    def test_empty_collector_output(self):
        inp = _make_input([], [])
        result, trace, _ = analyze_for_aurora_pg(inp)
        assert result.table_recommendations == []
        assert result.workload_analysis.patterns_detected == []

    def test_decision_trace_structure(self, ecommerce_fixture):
        _, trace, _ = analyze_for_aurora_pg(ecommerce_fixture)
        assert trace["trace_version"] == "1.0"
        assert trace["agent"] == "aurora-pg-analysis-agent"
        assert "summary" in trace
        assert "query_matches" in trace
        assert "pattern_summaries" in trace
        assert "recommendation_derivations" in trace
        assert trace["llm_advisor"]["status"] == "skipped"


class TestAuroraPgPatternDetection:
    """Pattern detection tests."""

    def test_detects_complex_joins(self, ecommerce_fixture):
        result, _, _ = analyze_for_aurora_pg(ecommerce_fixture)
        pattern_ids = [p.pattern_id for p in result.workload_analysis.patterns_detected]
        assert "aurora-common-01" in pattern_ids  # complex-join

    def test_detects_aggregation(self, ecommerce_fixture):
        result, _, _ = analyze_for_aurora_pg(ecommerce_fixture)
        pattern_ids = [p.pattern_id for p in result.workload_analysis.patterns_detected]
        assert "aurora-common-02" in pattern_ids  # aggregation-analytics

    def test_detects_referential_integrity(self, ecommerce_fixture):
        result, _, _ = analyze_for_aurora_pg(ecommerce_fixture)
        pattern_ids = [p.pattern_id for p in result.workload_analysis.patterns_detected]
        assert "aurora-common-04" in pattern_ids  # referential-integrity (>2 FKs)

    def test_detects_window_functions(self, ecommerce_fixture):
        result, _, _ = analyze_for_aurora_pg(ecommerce_fixture)
        pattern_ids = [p.pattern_id for p in result.workload_analysis.patterns_detected]
        assert "aurora-pg-02" in pattern_ids  # window-function

    def test_detects_cte_recursive(self, pg_features_fixture):
        result, _, _ = analyze_for_aurora_pg(pg_features_fixture)
        pattern_ids = [p.pattern_id for p in result.workload_analysis.patterns_detected]
        assert "aurora-pg-01" in pattern_ids  # cte-recursive

    def test_detects_jsonb_operations(self, pg_features_fixture):
        result, _, _ = analyze_for_aurora_pg(pg_features_fixture)
        pattern_ids = [p.pattern_id for p in result.workload_analysis.patterns_detected]
        assert "aurora-pg-03" in pattern_ids  # jsonb-operations

    def test_detects_upsert_conflict(self, pg_features_fixture):
        result, _, _ = analyze_for_aurora_pg(pg_features_fixture)
        pattern_ids = [p.pattern_id for p in result.workload_analysis.patterns_detected]
        assert "aurora-pg-07" in pattern_ids  # upsert-conflict

    def test_detects_pagination(self, ecommerce_fixture):
        result, _, _ = analyze_for_aurora_pg(ecommerce_fixture)
        pattern_ids = [p.pattern_id for p in result.workload_analysis.patterns_detected]
        assert "aurora-common-08" in pattern_ids  # pagination (LIMIT in query)


class TestAuroraPgAntiPatterns:
    """Anti-pattern detection tests."""

    def test_detects_high_freq_pk_lookup(self, high_freq_pk_fixture):
        result, _, _ = analyze_for_aurora_pg(high_freq_pk_fixture)
        anti_ids = [
            ap.anti_pattern_id for ap in (result.workload_analysis.anti_patterns_detected or [])
        ]
        assert "aurora-anti-01" in anti_ids

    def test_no_anti_pattern_for_complex_queries(self, ecommerce_fixture):
        result, _, _ = analyze_for_aurora_pg(ecommerce_fixture)
        anti_ids = [
            ap.anti_pattern_id for ap in (result.workload_analysis.anti_patterns_detected or [])
        ]
        # Complex join queries should NOT trigger high-freq PK lookup
        assert "aurora-anti-01" not in anti_ids


class TestAuroraPgScoring:
    """Scoring and confidence tests."""

    def test_complex_relational_scores_high(self, ecommerce_fixture):
        result, _, _ = analyze_for_aurora_pg(ecommerce_fixture)
        orders_rec = next(r for r in result.table_recommendations if r.table_id == "orders")
        # Complex joins + aggregation + FK density = high confidence
        assert orders_rec.confidence_score >= 70

    def test_pk_lookup_only_scores_lower(self, high_freq_pk_fixture):
        result, _, _ = analyze_for_aurora_pg(high_freq_pk_fixture)
        sessions_rec = next(r for r in result.table_recommendations if r.table_id == "sessions")
        # High-freq PK lookups are an anti-pattern for Aurora
        assert sessions_rec.confidence_score < 70

    def test_pg_features_boost_score(self, pg_features_fixture):
        result, _, _ = analyze_for_aurora_pg(pg_features_fixture)
        cat_rec = next(r for r in result.table_recommendations if r.table_id == "categories")
        # CTE + JSONB + upsert = good PG fit
        assert cat_rec.confidence_score >= 60


class TestAuroraPgLlmSeam:
    """LLM Seam Pattern tests."""

    def test_deterministic_matches_none_mode(self, ecommerce_fixture):
        det_result, _, _ = analyze_for_aurora_pg_deterministic(ecommerce_fixture)
        none_result, _, _ = analyze_for_aurora_pg(ecommerce_fixture, llm_mode="none")
        # Compare structure (timestamps will differ between calls)
        assert len(det_result.table_recommendations) == len(none_result.table_recommendations)
        assert len(det_result.workload_analysis.patterns_detected) == len(
            none_result.workload_analysis.patterns_detected
        )
        assert det_result.agent_metadata.agent_name == none_result.agent_metadata.agent_name

    def test_external_mode_sets_awaiting(self, ecommerce_fixture):
        _, trace, _ = analyze_for_aurora_pg(ecommerce_fixture, llm_mode="external")
        assert trace["llm_advisor"]["status"] == "awaiting_external"

    def test_prepare_llm_input_returns_dict(self, ecommerce_fixture):
        result, _, _ = analyze_for_aurora_pg_deterministic(ecommerce_fixture)
        payload = prepare_aurora_pg_llm_input(result, ecommerce_fixture)
        assert "deterministic_results" in payload
        assert "schema" in payload
        assert "queries" in payload

    def test_apply_llm_output_none_returns_unchanged(self, ecommerce_fixture):
        result, _, _ = analyze_for_aurora_pg_deterministic(ecommerce_fixture)
        updated = apply_aurora_pg_llm_output(result, None)
        assert updated.model_dump() == result.model_dump()


class TestAuroraPgCostEstimation:
    """Cost estimation tests."""

    def test_cost_has_both_options(self, ecommerce_fixture):
        result, _, _ = analyze_for_aurora_pg(ecommerce_fixture)
        cost = result.cost_estimate
        assert cost.monthly_cost_usd > 0
        assert "serverless_v2" in cost.cost_components
        assert "provisioned" in cost.cost_components
        assert "recommended" in cost.cost_components

    def test_cost_disabled(self):
        inp = AnalysisInput(
            job_id="test",
            collector_output={
                "database_schema": {
                    "tables": [
                        {
                            "table_id": "t1",
                            "columns": [],
                            "row_count": 10,
                            "size_mb": 1.0,
                            "primary_key": {"columns": ["id"]},
                            "foreign_keys": [],
                        }
                    ]
                },
                "queries": {"query_patterns": []},
            },
            target_database=TargetDatabase.aurora_postgresql,
        )
        inp.analysis_options.perform_cost_estimation = False
        result, _, _ = analyze_for_aurora_pg(inp)
        assert result.cost_estimate.monthly_cost_usd == 0.0
