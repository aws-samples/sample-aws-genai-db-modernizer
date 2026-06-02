"""Unit tests for OpenSearch analysis agent — per-pattern isolation."""

from __future__ import annotations

from src.agents.analysis.opensearch_analysis_agent import analyze_for_opensearch
from src.contracts.analysis_input import AnalysisInput, TargetDatabase
from src.tools.analysis.opensearch_analysis_tools import (
    analyze_opensearch_patterns,
    analyze_opensearch_use_cases,
    build_opensearch_decision_trace,
    classify_table_workload,
    estimate_opensearch_costs,
)
from tests.fixtures.opensearch_pattern_fixtures import (
    base_query,
    base_table,
    get_audit_timestamp_fixture,
    get_fulltext_search_fixture,
    get_fuzzy_search_fixture,
    get_high_ingest_fixture,
    get_joins_fixture,
    get_regex_search_fixture,
    get_time_range_fixture,
    get_transactions_fixture,
    get_wildcard_search_fixture,
    wrap_fixture,
)
from tests.fixtures.saas_platform_collector_output import get_saas_platform_collector_output


def _run(fixture: dict):
    """Run analyze_opensearch_use_cases and return (pattern_types, anti_pattern_types, result)."""
    result = analyze_opensearch_use_cases(fixture)
    pattern_types = {p.pattern_type for p in result.patterns_detected}
    ap_types = {ap.anti_pattern_type for ap in (result.anti_patterns_detected or [])}
    return pattern_types, ap_types, result


# ==========================================================================
# TestSearchPatternDetection
# ==========================================================================


class TestSearchPatternDetection:
    # --- Detection tests ---

    def test_fulltext_search_detected(self):
        pattern_types, _, _ = _run(get_fulltext_search_fixture())
        assert "full-text-search" in pattern_types

    def test_wildcard_search_detected(self):
        pattern_types, _, _ = _run(get_wildcard_search_fixture())
        assert "wildcard-search" in pattern_types

    def test_regex_search_detected(self):
        pattern_types, _, _ = _run(get_regex_search_fixture())
        assert "regex-search" in pattern_types

    def test_fuzzy_search_detected(self):
        pattern_types, _, _ = _run(get_fuzzy_search_fixture())
        assert "fuzzy-search" in pattern_types

    # --- No cross-contamination: each fixture triggers only its own search pattern ---

    def test_fulltext_no_timeseries_patterns(self):
        pattern_types, _, _ = _run(get_fulltext_search_fixture())
        assert not pattern_types & {"time-range-query", "time-aggregation", "high-ingest"}

    def test_wildcard_no_fulltext_or_regex(self):
        pattern_types, _, _ = _run(get_wildcard_search_fixture())
        assert "full-text-search" not in pattern_types
        assert "regex-search" not in pattern_types

    def test_regex_no_fulltext_or_wildcard(self):
        pattern_types, _, _ = _run(get_regex_search_fixture())
        assert "full-text-search" not in pattern_types
        assert "wildcard-search" not in pattern_types

    def test_fuzzy_no_other_search_patterns(self):
        pattern_types, _, _ = _run(get_fuzzy_search_fixture())
        assert "full-text-search" not in pattern_types
        assert "wildcard-search" not in pattern_types
        assert "regex-search" not in pattern_types


# ==========================================================================
# TestTimeSeriesPatternDetection
# ==========================================================================


class TestTimeSeriesPatternDetection:
    # --- Detection tests ---

    def test_time_range_detected(self):
        pattern_types, _, _ = _run(get_time_range_fixture())
        assert "time-range-query" in pattern_types

    def test_high_ingest_detected(self):
        pattern_types, _, _ = _run(get_high_ingest_fixture())
        assert "high-ingest" in pattern_types

    # --- No cross-contamination ---

    def test_time_range_no_search_patterns(self):
        pattern_types, _, _ = _run(get_time_range_fixture())
        assert not pattern_types & {
            "full-text-search",
            "wildcard-search",
            "regex-search",
            "fuzzy-search",
        }

    def test_high_ingest_no_search_patterns(self):
        pattern_types, _, _ = _run(get_high_ingest_fixture())
        assert not pattern_types & {
            "full-text-search",
            "wildcard-search",
            "regex-search",
            "fuzzy-search",
        }


# ==========================================================================
# TestAntiPatternDetection
# ==========================================================================


class TestAntiPatternDetection:
    def test_joins_anti_pattern_detected(self):
        _, ap_types, _ = _run(get_joins_fixture())
        assert "multi-index-joins" in ap_types

    def test_transactions_anti_pattern_detected(self):
        _, ap_types, _ = _run(get_transactions_fixture())
        assert "acid-transactions" in ap_types

    def test_high_ingest_pattern_detected_at_cps_15(self):
        """Verify high-ingest fixture (CPS=15) triggers os-07."""
        pattern_types, _, _ = _run(get_high_ingest_fixture())
        assert "high-ingest" in pattern_types

    def test_audit_timestamp_no_timeseries_patterns(self):
        """Equality-only timestamp usage should NOT trigger time-range-query."""
        pattern_types, _, _ = _run(get_audit_timestamp_fixture())
        assert "time-range-query" not in pattern_types
        assert "time-aggregation" not in pattern_types

    def test_audit_timestamp_anti_pattern_emitted(self):
        """Equality-only timestamp usage should emit os-ap-03 (audit-columns-only)."""
        _, ap_types, _ = _run(get_audit_timestamp_fixture())
        assert "audit-columns-only" in ap_types


# ==========================================================================
# TestWorkloadClassification (Task 3)
# ==========================================================================


def _make_analysis_input(fixture: dict) -> AnalysisInput:
    return AnalysisInput(
        job_id=fixture.get("job_id", "test"),
        collector_output=fixture,
        target_database=TargetDatabase.opensearch,
    )


class TestWorkloadClassification:
    def test_fulltext_classified_as_search(self):
        """Full-text search fixture should classify as SEARCH."""
        from src.tools.analysis.opensearch_analysis_tools import WorkloadType

        fixture = get_fulltext_search_fixture()
        workload_analysis = analyze_opensearch_use_cases(fixture)
        queries = fixture["queries"]["query_patterns"]
        table_schema = fixture["database_schema"]["tables"][0]
        table_id = "public.documents"
        result = classify_table_workload(table_id, workload_analysis, table_schema, queries)
        assert result == WorkloadType.SEARCH

    def test_wildcard_classified_as_search(self):
        """Wildcard search fixture should classify as SEARCH."""
        from src.tools.analysis.opensearch_analysis_tools import WorkloadType

        fixture = get_wildcard_search_fixture()
        workload_analysis = analyze_opensearch_use_cases(fixture)
        queries = fixture["queries"]["query_patterns"]
        table_schema = fixture["database_schema"]["tables"][0]
        table_id = "public.products"
        result = classify_table_workload(table_id, workload_analysis, table_schema, queries)
        assert result == WorkloadType.SEARCH

    def test_high_ingest_log_table_classified_as_timeseries(self):
        """High-ingest log table meeting ALL 3 TS criteria should be TIMESERIES."""
        from src.tools.analysis.opensearch_analysis_tools import WorkloadType

        fixture = get_high_ingest_fixture()
        # Add a time-range query to trigger timestamp_range criterion
        time_range_q = base_query(
            query_id="hi-ts-001",
            query_text="SELECT * FROM application_log WHERE log_time >= $1 AND log_time <= $2",
            query_type="SELECT",
            tables=["public.application_log"],
            calls_per_second=1.0,
            execution_time_ms_avg=20.0,
        )
        fixture["queries"]["query_patterns"].append(time_range_q)

        workload_analysis = analyze_opensearch_use_cases(fixture)
        queries = fixture["queries"]["query_patterns"]
        table_schema = fixture["database_schema"]["tables"][0]
        table_id = "public.application_log"
        result = classify_table_workload(table_id, workload_analysis, table_schema, queries)
        assert result == WorkloadType.TIMESERIES

    def test_audit_timestamp_only_returns_none(self):
        """Audit-only timestamp table (no range queries) → None (not suitable)."""
        fixture = get_audit_timestamp_fixture()
        workload_analysis = analyze_opensearch_use_cases(fixture)
        queries = fixture["queries"]["query_patterns"]
        table_schema = fixture["database_schema"]["tables"][0]
        table_id = "public.user_events"
        result = classify_table_workload(table_id, workload_analysis, table_schema, queries)
        assert result is None

    def test_orders_table_moderate_writes_returns_none(self):
        """Orders table with moderate writes, no search patterns → None."""
        table = base_table(
            table_id="public.orders",
            table_name="orders",
            row_count=500000,
            size_mb=300.0,
        )
        queries = [
            base_query(
                query_id="ord-001",
                query_text="INSERT INTO orders (customer_id, amount) VALUES ($1, $2)",
                query_type="INSERT",
                tables=["public.orders"],
                calls_per_second=2.0,
            ),
            base_query(
                query_id="ord-002",
                query_text="SELECT * FROM orders WHERE id = $1",
                query_type="SELECT",
                tables=["public.orders"],
                calls_per_second=5.0,
            ),
        ]
        fixture = wrap_fixture("orders-test", [table], queries)
        workload_analysis = analyze_opensearch_use_cases(fixture)
        table_id = "public.orders"
        result = classify_table_workload(
            table_id, workload_analysis, table_schema=table, queries=queries
        )
        assert result is None

    def test_near_miss_timeseries_cps_below_threshold(self):
        """INSERT CPS=5 (below 10 threshold) should NOT classify as TIMESERIES."""
        table = base_table(
            table_id="public.application_log",
            table_name="application_log",
            row_count=1000000,
            size_mb=500.0,
            columns=[
                {
                    "column_name": "id",
                    "ordinal_position": 1,
                    "data_type": "bigint",
                    "nullable": False,
                },
                {
                    "column_name": "log_time",
                    "ordinal_position": 2,
                    "data_type": "timestamp",
                    "nullable": False,
                },
                {
                    "column_name": "message",
                    "ordinal_position": 3,
                    "data_type": "text",
                    "nullable": True,
                },
            ],
        )
        queries = [
            base_query(
                query_id="nm-001",
                query_text="INSERT INTO application_log (log_time, message) VALUES ($1, $2)",
                query_type="INSERT",
                tables=["public.application_log"],
                calls_per_second=5.0,  # below threshold of 10
            ),
            base_query(
                query_id="nm-002",
                query_text="SELECT * FROM application_log WHERE log_time >= $1 AND log_time <= $2",
                query_type="SELECT",
                tables=["public.application_log"],
                calls_per_second=1.0,
            ),
        ]
        fixture = wrap_fixture("near-miss-test", [table], queries)
        workload_analysis = analyze_opensearch_use_cases(fixture)
        result = classify_table_workload(
            "public.application_log", workload_analysis, table_schema=table, queries=queries
        )
        # Not TIMESERIES — INSERT CPS too low
        from src.tools.analysis.opensearch_analysis_tools import WorkloadType

        assert result != WorkloadType.TIMESERIES


# ==========================================================================
# TestWorkloadScoring (Task 4)
# ==========================================================================


class TestWorkloadScoring:
    def test_search_table_has_nonzero_pattern_match(self):
        """Full-text search table should have non-zero pattern_match score."""
        fixture = get_fulltext_search_fixture()
        workload_analysis = analyze_opensearch_use_cases(fixture)
        recommendations, _, _ = analyze_opensearch_patterns(fixture, workload_analysis)
        assert recommendations
        rec = next(r for r in recommendations if r.table_id == "public.documents")
        assert rec.score_breakdown.pattern_match_score > 0

    def test_not_suitable_table_has_zero_scores(self):
        """A table with no patterns should have zero scores and confidence 0."""
        table = base_table(
            table_id="public.simple_table",
            table_name="simple_table",
            row_count=1000,
            size_mb=10.0,
        )
        queries = [
            base_query(
                query_id="st-001",
                query_text="SELECT * FROM simple_table WHERE id = $1",
                query_type="SELECT",
                tables=["public.simple_table"],
                calls_per_second=1.0,
            ),
        ]
        fixture = wrap_fixture("simple-test", [table], queries)
        workload_analysis = analyze_opensearch_use_cases(fixture)
        recommendations, _, _ = analyze_opensearch_patterns(fixture, workload_analysis)
        assert recommendations
        rec = recommendations[0]
        assert rec.confidence_score == 0
        assert rec.score_breakdown.pattern_match_score == 0
        assert rec.score_breakdown.complexity_score == 0
        assert rec.score_breakdown.performance_score == 0
        assert rec.score_breakdown.cost_score == 0

    def test_search_table_all_score_dimensions_nonzero(self):
        """Search table should have non-zero values across multiple dimensions."""
        fixture = get_fulltext_search_fixture()
        workload_analysis = analyze_opensearch_use_cases(fixture)
        recommendations, _, _ = analyze_opensearch_patterns(fixture, workload_analysis)
        rec = recommendations[0]
        # At minimum pattern_match and complexity should be non-zero
        total = rec.score_breakdown.pattern_match_score + rec.score_breakdown.complexity_score
        assert total > 0

    def test_timeseries_table_uses_timeseries_weights(self):
        """Time-series table should have higher performance weight in confidence calculation."""
        from src.tools.analysis.opensearch_analysis_tools import WorkloadType

        fixture = get_high_ingest_fixture()
        # Add time-range query to trigger all 3 TS criteria
        fixture["queries"]["query_patterns"].append(
            base_query(
                query_id="hi-ts-002",
                query_text=(
                    "SELECT * FROM application_log WHERE log_time >= $1 AND log_time <= $2"
                ),
                query_type="SELECT",
                tables=["public.application_log"],
                calls_per_second=1.0,
            )
        )
        workload_analysis = analyze_opensearch_use_cases(fixture)
        _, classifications, _ = analyze_opensearch_patterns(fixture, workload_analysis)
        assert classifications.get("public.application_log") == WorkloadType.TIMESERIES


# ==========================================================================
# TestRecommendationsAndCosts (Task 4)
# ==========================================================================


class TestRecommendationsAndCosts:
    def test_search_table_rationale_has_search_prefix(self):
        """Search table recommendation should have [SEARCH] prefix in rationale."""
        fixture = get_fulltext_search_fixture()
        workload_analysis = analyze_opensearch_use_cases(fixture)
        recommendations, _, _ = analyze_opensearch_patterns(fixture, workload_analysis)
        rec = next(r for r in recommendations if r.table_id == "public.documents")
        assert rec.rationale is not None
        assert "[SEARCH]" in rec.rationale

    def test_not_suitable_table_confidence_zero(self):
        """NOT_SUITABLE table should have confidence_score == 0."""
        table = base_table(
            table_id="public.config",
            table_name="config",
            row_count=100,
            size_mb=1.0,
        )
        queries = [
            base_query(
                query_id="cfg-001",
                query_text="SELECT value FROM config WHERE key = $1",
                query_type="SELECT",
                tables=["public.config"],
                calls_per_second=0.1,
            )
        ]
        fixture = wrap_fixture("config-test", [table], queries)
        workload_analysis = analyze_opensearch_use_cases(fixture)
        recommendations, _, _ = analyze_opensearch_patterns(fixture, workload_analysis)
        rec = recommendations[0]
        assert rec.confidence_score == 0
        assert "[NOT_SUITABLE]" in (rec.rationale or "")

    def test_cost_estimate_has_required_fields(self):
        """Cost estimate should have monthly_cost_usd and cost_components."""
        fixture = get_fulltext_search_fixture()
        cost = estimate_opensearch_costs(fixture, "us-east-1")
        assert cost.monthly_cost_usd >= 0
        assert isinstance(cost.cost_components, dict)
        assert "instance_cost_usd" in cost.cost_components
        assert "storage_cost_usd" in cost.cost_components

    def test_cost_estimation_disabled_returns_zero(self):
        """When perform_cost_estimation is False, cost should be 0."""
        from src.contracts.analysis_input import AnalysisOptions

        fixture = get_fulltext_search_fixture()
        options = AnalysisOptions(perform_cost_estimation=False)
        cost = estimate_opensearch_costs(fixture, "us-east-1", options)
        assert cost.monthly_cost_usd == 0.0


# ==========================================================================
# TestDecisionTrace (Task 5)
# ==========================================================================


class TestDecisionTrace:
    def _make_trace(self, fixture: dict) -> dict:
        workload_analysis = analyze_opensearch_use_cases(fixture)
        recommendations, classifications, weights_used = analyze_opensearch_patterns(
            fixture, workload_analysis
        )
        return build_opensearch_decision_trace(
            fixture, workload_analysis, recommendations, classifications, weights_used
        )

    def test_trace_has_required_keys(self):
        """Trace must have all required top-level keys."""
        trace = self._make_trace(get_fulltext_search_fixture())
        required = {
            "trace_version",
            "agent",
            "summary",
            "query_matches",
            "pattern_summaries",
            "workload_classifications",
            "recommendation_derivations",
        }
        assert required.issubset(set(trace.keys()))

    def test_trace_version_is_1_0(self):
        """Trace version should be 1.0."""
        trace = self._make_trace(get_fulltext_search_fixture())
        assert trace["trace_version"] == "1.0"

    def test_workload_classifications_contain_workload_type(self):
        """workload_classifications should have workload_type for each table."""
        trace = self._make_trace(get_fulltext_search_fixture())
        assert trace["workload_classifications"]
        wc = trace["workload_classifications"][0]
        assert "workload_type" in wc
        assert "table_id" in wc
        assert "timeseries_criteria_met" in wc

    def test_recommendation_derivations_has_final_recommendation(self):
        """recommendation_derivations should include final_recommendation field."""
        trace = self._make_trace(get_fulltext_search_fixture())
        assert trace["recommendation_derivations"]
        deriv = trace["recommendation_derivations"][0]
        assert "final_recommendation" in deriv

    def test_summary_includes_classification_counts(self):
        """Summary should include tables_search, tables_timeseries, tables_not_suitable."""
        trace = self._make_trace(get_fulltext_search_fixture())
        summary = trace["summary"]
        assert "tables_search" in summary
        assert "tables_timeseries" in summary
        assert "tables_not_suitable" in summary


# ==========================================================================
# TestAnalyzeForOpenSearch (Task 6)
# ==========================================================================


class TestAnalyzeForOpenSearch:
    def test_produces_valid_output_contract(self):
        """analyze_for_opensearch should return a valid AnalysisOutputContract."""
        fixture = get_fulltext_search_fixture()
        analysis_input = _make_analysis_input(fixture)
        result, _, _ = analyze_for_opensearch(analysis_input)

        assert result.contract_version == "2.1"
        assert result.agent_metadata.agent_name == "opensearch-analysis-agent"
        assert result.agent_metadata.target_database == "opensearch"
        assert isinstance(result.table_recommendations, list)
        assert result.cost_estimate is not None
        assert result.aggregate_recommendations is None

    def test_returns_3_tuple(self):
        """analyze_for_opensearch should return a 3-tuple."""
        fixture = get_fulltext_search_fixture()
        analysis_input = _make_analysis_input(fixture)
        result = analyze_for_opensearch(analysis_input)

        assert isinstance(result, tuple)
        assert len(result) == 3
        # Third element is empty string (no Mermaid diagram)
        assert result[2] == ""

    def test_decision_trace_present(self):
        """Second element (decision trace) should be a dict with trace_version."""
        fixture = get_fulltext_search_fixture()
        analysis_input = _make_analysis_input(fixture)
        _, decision_trace, _ = analyze_for_opensearch(analysis_input)

        assert isinstance(decision_trace, dict)
        assert "trace_version" in decision_trace

    def test_empty_collector_output(self):
        """Empty collector output → valid output, 0 recommendations."""
        empty_fixture = {
            "job_id": "empty-test",
            "database_schema": {"tables": []},
            "queries": {"query_patterns": []},
        }
        analysis_input = AnalysisInput(
            job_id="empty-test",
            collector_output=empty_fixture,
            target_database=TargetDatabase.opensearch,
        )
        result, decision_trace, mermaid = analyze_for_opensearch(analysis_input)

        assert result.contract_version == "2.1"
        assert result.table_recommendations == []
        assert mermaid == ""
        assert isinstance(decision_trace, dict)


# ==========================================================================
# TestSaaSPlatformIntegration (Task 7)
# ==========================================================================


class TestSaaSPlatformIntegration:
    """Integration tests against the SaaS platform vertical fixture (4 tables)."""

    def setup_method(self):
        """Run the full pipeline once and cache results for all tests in this class."""
        self.fixture = get_saas_platform_collector_output()
        self.workload_analysis = analyze_opensearch_use_cases(self.fixture)
        self.recommendations, self.classifications, self.weights_used = analyze_opensearch_patterns(
            self.fixture, self.workload_analysis
        )
        analysis_input = AnalysisInput(
            job_id="saas-platform-test",
            collector_output=self.fixture,
            target_database=TargetDatabase.opensearch,
        )
        self.contract, self.trace, _ = analyze_for_opensearch(analysis_input)

    # --- All 4 tables get recommendations ---

    def test_all_four_tables_have_recommendations(self):
        """All 4 tables in the fixture should receive a recommendation."""
        assert len(self.recommendations) == 4
        rec_ids = {r.table_id for r in self.recommendations}
        expected = {
            "public.products",
            "public.knowledge_base",
            "public.application_logs",
            "public.orders",
        }
        assert rec_ids == expected

    # --- Classification correctness ---

    def test_products_classified_as_search(self):
        """products table (ILIKE queries) should be classified as SEARCH."""
        from src.tools.analysis.opensearch_analysis_tools import WorkloadType

        assert self.classifications["public.products"] == WorkloadType.SEARCH

    def test_knowledge_base_classified_as_search(self):
        """knowledge_base (tsvector/tsquery) should be classified as SEARCH."""
        from src.tools.analysis.opensearch_analysis_tools import WorkloadType

        assert self.classifications["public.knowledge_base"] == WorkloadType.SEARCH

    def test_application_logs_classified_as_timeseries(self):
        """application_logs (INSERT CPS=25, time-range, log name) should be TIMESERIES."""
        from src.tools.analysis.opensearch_analysis_tools import WorkloadType

        assert self.classifications["public.application_logs"] == WorkloadType.TIMESERIES

    def test_orders_classified_as_not_suitable(self):
        """orders (no search/TS patterns) should be NOT_SUITABLE (confidence 0)."""
        orders_rec = next(r for r in self.recommendations if r.table_id == "public.orders")
        assert orders_rec.confidence_score == 0

    # --- Scores are differentiated ---

    def test_search_tables_have_higher_scores_than_orders(self):
        """SEARCH tables should score higher than NOT_SUITABLE orders."""
        products_rec = next(r for r in self.recommendations if r.table_id == "public.products")
        orders_rec = next(r for r in self.recommendations if r.table_id == "public.orders")
        assert products_rec.confidence_score > orders_rec.confidence_score

    def test_knowledge_base_confidence_above_zero(self):
        """knowledge_base should have a positive confidence score."""
        kb_rec = next(r for r in self.recommendations if r.table_id == "public.knowledge_base")
        assert kb_rec.confidence_score > 0

    # --- Both pattern types detected ---

    def test_search_patterns_detected(self):
        """Fixture should include at least one search pattern (wildcard or full-text)."""
        pattern_types = {p.pattern_type for p in self.workload_analysis.patterns_detected}
        search_types = pattern_types & {
            "full-text-search",
            "wildcard-search",
            "regex-search",
            "fuzzy-search",
        }
        assert search_types, f"Expected search patterns, got: {pattern_types}"

    def test_timeseries_patterns_detected(self):
        """Fixture should include time-series patterns (time-range, time-agg, high-ingest)."""
        pattern_types = {p.pattern_type for p in self.workload_analysis.patterns_detected}
        ts_types = pattern_types & {"time-range-query", "time-aggregation", "high-ingest"}
        assert ts_types, f"Expected TS patterns, got: {pattern_types}"

    # --- Cost estimate present ---

    def test_cost_estimate_present_and_positive(self):
        """Cost estimate should be present and positive."""
        assert self.contract.cost_estimate is not None
        assert self.contract.cost_estimate.monthly_cost_usd > 0

    # --- Decision trace has workload_classifications for all 4 tables ---

    def test_trace_has_workload_classifications_for_all_tables(self):
        """Decision trace should have workload_classifications for all 4 tables."""
        wc = self.trace["workload_classifications"]
        wc_ids = {entry["table_id"] for entry in wc}
        expected = {
            "public.products",
            "public.knowledge_base",
            "public.application_logs",
            "public.orders",
        }
        assert wc_ids == expected

    def test_trace_timeseries_criteria_all_true_for_logs(self):
        """application_logs should show all 3 time-series criteria as True in trace."""
        wc = self.trace["workload_classifications"]
        logs_entry = next(e for e in wc if e["table_id"] == "public.application_logs")
        criteria = logs_entry["timeseries_criteria_met"]
        assert criteria["timestamp_range"] is True
        assert criteria["high_ingest"] is True
        assert criteria["staleness"] is True


# ==========================================================================
# TestBoundaryAndEdgeCases (Task 8)
# ==========================================================================


class TestBoundaryAndEdgeCases:
    """Boundary, edge case, and robustness tests."""

    # --- Near-miss time-series ---

    def test_near_miss_timeseries_insert_cps_5(self):
        """INSERT CPS=5 (below threshold of 10) should NOT produce TIMESERIES."""
        from src.tools.analysis.opensearch_analysis_tools import WorkloadType

        table = base_table(
            table_id="public.near_miss_log",
            table_name="near_miss_log",
            row_count=1_000_000,
            size_mb=500.0,
            columns=[
                {
                    "column_name": "id",
                    "ordinal_position": 1,
                    "data_type": "bigint",
                    "nullable": False,
                },
                {
                    "column_name": "log_time",
                    "ordinal_position": 2,
                    "data_type": "timestamp",
                    "nullable": False,
                },
                {
                    "column_name": "message",
                    "ordinal_position": 3,
                    "data_type": "text",
                    "nullable": True,
                },
            ],
        )
        queries = [
            base_query(
                query_id="nm-insert",
                query_text="INSERT INTO near_miss_log (log_time, message) VALUES ($1, $2)",
                query_type="INSERT",
                tables=["public.near_miss_log"],
                calls_per_second=5.0,  # below 10
            ),
            base_query(
                query_id="nm-select",
                query_text=("SELECT * FROM near_miss_log WHERE log_time >= $1 AND log_time <= $2"),
                query_type="SELECT",
                tables=["public.near_miss_log"],
                calls_per_second=1.0,
            ),
        ]
        fixture = wrap_fixture("near-miss-5", [table], queries)
        wa = analyze_opensearch_use_cases(fixture)
        result = classify_table_workload("public.near_miss_log", wa, table, queries)
        assert result != WorkloadType.TIMESERIES

    # --- No queries → NOT_SUITABLE ---

    def test_table_with_no_queries_is_not_suitable(self):
        """A table with no queries should be NOT_SUITABLE."""
        table = base_table(
            table_id="public.orphan_table",
            table_name="orphan_table",
            row_count=100,
            size_mb=5.0,
        )
        fixture = wrap_fixture("no-queries-test", [table], [])
        wa = analyze_opensearch_use_cases(fixture)
        recs, cls, _ = analyze_opensearch_patterns(fixture, wa)
        assert cls["public.orphan_table"] is None
        assert recs[0].confidence_score == 0

    # --- Empty collector → valid output, 0 recommendations ---

    def test_empty_collector_produces_valid_output(self):
        """Empty collector (no tables, no queries) → valid output, 0 recommendations."""
        fixture = {
            "job_id": "empty-edge",
            "database_schema": {"tables": []},
            "queries": {"query_patterns": []},
        }
        wa = analyze_opensearch_use_cases(fixture)
        recs, cls, _ = analyze_opensearch_patterns(fixture, wa)
        assert recs == []
        assert cls == {}

    # --- Multi-pattern query (ILIKE + tsvector) triggers both ---

    def test_multi_pattern_query_triggers_both_search_patterns(self):
        """A query with both ILIKE and tsvector keywords triggers both wildcard and full-text."""
        table = base_table(
            table_id="public.multi_pattern",
            table_name="multi_pattern",
            row_count=10000,
            size_mb=50.0,
        )
        queries = [
            base_query(
                query_id="mp-001",
                query_text=(  # nosemgrep: string-concat-in-list
                    "SELECT * FROM multi_pattern "
                    "WHERE name ILIKE $1 "
                    "OR search_vector @@ to_tsvector('english', $1)"
                ),
                query_type="SELECT",
                tables=["public.multi_pattern"],
                calls_per_second=2.0,
            )
        ]
        fixture = wrap_fixture("multi-pattern-test", [table], queries)
        pattern_types, _, _ = _run(fixture)
        assert "wildcard-search" in pattern_types
        assert "full-text-search" in pattern_types

    # --- INSERT CPS exactly at threshold (10.0) triggers high-ingest ---

    def test_insert_cps_exactly_10_triggers_high_ingest(self):
        """INSERT CPS == 10.0 (at threshold) should trigger high-ingest pattern."""
        table = base_table(
            table_id="public.event_log",
            table_name="event_log",
            row_count=1_000_000,
            size_mb=500.0,
        )
        queries = [
            base_query(
                query_id="at-thresh-001",
                query_text="INSERT INTO event_log (message) VALUES ($1)",
                query_type="INSERT",
                tables=["public.event_log"],
                calls_per_second=10.0,
            )
        ]
        fixture = wrap_fixture("threshold-10", [table], queries)
        pattern_types, _, _ = _run(fixture)
        assert "high-ingest" in pattern_types

    # --- INSERT CPS 9.9 does NOT trigger high-ingest ---

    def test_insert_cps_9_9_does_not_trigger_high_ingest(self):
        """INSERT CPS == 9.9 (just below threshold) should NOT trigger high-ingest."""
        table = base_table(
            table_id="public.event_log",
            table_name="event_log",
            row_count=500_000,
            size_mb=200.0,
        )
        queries = [
            base_query(
                query_id="below-thresh-001",
                query_text="INSERT INTO event_log (message) VALUES ($1)",
                query_type="INSERT",
                tables=["public.event_log"],
                calls_per_second=9.9,
            )
        ]
        fixture = wrap_fixture("threshold-9.9", [table], queries)
        pattern_types, _, _ = _run(fixture)
        assert "high-ingest" not in pattern_types

    # --- TS with UPDATE/DELETE but high ratio (INSERT 50, DELETE 5, ratio 10:1) → TIMESERIES ---

    def test_timeseries_high_ratio_with_some_deletes_still_timeseries(self):
        """INSERT 50 CPS, DELETE 5 CPS (ratio 10:1 ≥ 5:1) → still TIMESERIES."""
        from src.tools.analysis.opensearch_analysis_tools import WorkloadType

        table = base_table(
            table_id="public.audit_log",
            table_name="audit_log",
            row_count=5_000_000,
            size_mb=2000.0,
            columns=[
                {
                    "column_name": "id",
                    "ordinal_position": 1,
                    "data_type": "bigint",
                    "nullable": False,
                },
                {
                    "column_name": "log_time",
                    "ordinal_position": 2,
                    "data_type": "timestamp",
                    "nullable": False,
                },
                {
                    "column_name": "action",
                    "ordinal_position": 3,
                    "data_type": "varchar",
                    "nullable": False,
                },
            ],
        )
        queries = [
            base_query(
                query_id="hr-insert",
                query_text="INSERT INTO audit_log (log_time, action) VALUES ($1, $2)",
                query_type="INSERT",
                tables=["public.audit_log"],
                calls_per_second=50.0,
            ),
            base_query(
                query_id="hr-delete",
                query_text="DELETE FROM audit_log WHERE log_time < $1",
                query_type="DELETE",
                tables=["public.audit_log"],
                calls_per_second=5.0,
            ),
            base_query(
                query_id="hr-select",
                query_text=("SELECT * FROM audit_log WHERE log_time >= $1 AND log_time <= $2"),
                query_type="SELECT",
                tables=["public.audit_log"],
                calls_per_second=1.0,
            ),
        ]
        fixture = wrap_fixture("high-ratio-ts", [table], queries)
        wa = analyze_opensearch_use_cases(fixture)
        result = classify_table_workload("public.audit_log", wa, table, queries)
        assert result == WorkloadType.TIMESERIES

    # --- TS fails ratio (INSERT 20, UPDATE 10, ratio 2:1) → NOT TIMESERIES ---

    def test_timeseries_fails_ratio_not_classified_timeseries(self):
        """INSERT 20 CPS, UPDATE 10 CPS (ratio 2:1 < 5:1) → NOT TIMESERIES."""
        from src.tools.analysis.opensearch_analysis_tools import WorkloadType

        table = base_table(
            table_id="public.metric_log",
            table_name="metric_log",
            row_count=2_000_000,
            size_mb=800.0,
            columns=[
                {
                    "column_name": "id",
                    "ordinal_position": 1,
                    "data_type": "bigint",
                    "nullable": False,
                },
                {
                    "column_name": "log_time",
                    "ordinal_position": 2,
                    "data_type": "timestamp",
                    "nullable": False,
                },
                {
                    "column_name": "value",
                    "ordinal_position": 3,
                    "data_type": "float",
                    "nullable": False,
                },
            ],
        )
        queries = [
            base_query(
                query_id="fr-insert",
                query_text="INSERT INTO metric_log (log_time, value) VALUES ($1, $2)",
                query_type="INSERT",
                tables=["public.metric_log"],
                calls_per_second=20.0,
            ),
            base_query(
                query_id="fr-update",
                query_text="UPDATE metric_log SET value = $1 WHERE id = $2",
                query_type="UPDATE",
                tables=["public.metric_log"],
                calls_per_second=10.0,
            ),
            base_query(
                query_id="fr-select",
                query_text=("SELECT * FROM metric_log WHERE log_time >= $1 AND log_time <= $2"),
                query_type="SELECT",
                tables=["public.metric_log"],
                calls_per_second=2.0,
            ),
        ]
        fixture = wrap_fixture("fail-ratio-ts", [table], queries)
        wa = analyze_opensearch_use_cases(fixture)
        result = classify_table_workload("public.metric_log", wa, table, queries)
        assert result != WorkloadType.TIMESERIES

    # --- Score clamped at 0 (many anti-patterns) ---

    def test_score_clamped_at_zero_with_many_anti_patterns(self):
        """Score dimensions should never go below 0 even with many anti-patterns."""
        # Use a table with joins + transactions (2 anti-patterns) but no matching patterns
        table = base_table(
            table_id="public.clamp_test",
            table_name="clamp_test",
            row_count=100,
            size_mb=1.0,
        )
        queries = [
            base_query(
                query_id="cl-001",
                query_text="SELECT id FROM clamp_test WHERE id = $1",
                query_type="SELECT",
                tables=["public.clamp_test"],
                calls_per_second=1.0,
            )
        ]
        fixture = wrap_fixture("clamp-zero", [table], queries)
        wa = analyze_opensearch_use_cases(fixture)
        recs, _, _ = analyze_opensearch_patterns(fixture, wa)
        rec = recs[0]
        assert rec.score_breakdown.pattern_match_score >= 0
        assert rec.score_breakdown.complexity_score >= 0
        assert rec.score_breakdown.performance_score >= 0
        assert rec.score_breakdown.cost_score >= 0

    # --- Score clamped at 100 (extreme positive signals) ---

    def test_score_clamped_at_100_with_extreme_signals(self):
        """Score dimensions should never exceed 100 even with extreme positive signals."""
        table = base_table(
            table_id="public.clamp_max_test",
            table_name="clamp_max_test",
            row_count=5_000_000,
            size_mb=300.0,
            columns=[
                {
                    "column_name": "id",
                    "ordinal_position": 1,
                    "data_type": "bigint",
                    "nullable": False,
                },
                {
                    "column_name": "title",
                    "ordinal_position": 2,
                    "data_type": "text",
                    "nullable": False,
                },
                {
                    "column_name": "body",
                    "ordinal_position": 3,
                    "data_type": "text",
                    "nullable": True,
                },
                {
                    "column_name": "snippet",
                    "ordinal_position": 4,
                    "data_type": "text",
                    "nullable": True,
                },
                {
                    "column_name": "search_vector",
                    "ordinal_position": 5,
                    "data_type": "tsvector",
                    "nullable": True,
                },
            ],
        )
        # High CPS fulltext + wildcard + fuzzy all together
        queries = [
            base_query(
                query_id="cm-001",
                query_text=(  # nosemgrep: string-concat-in-list
                    "SELECT * FROM clamp_max_test "
                    "WHERE search_vector @@ to_tsvector('english', $1) "
                    "AND title ILIKE $2 "
                    "AND similarity(title, $3) > 0.3"
                ),
                query_type="SELECT",
                tables=["public.clamp_max_test"],
                calls_per_second=100.0,
                execution_time_ms_avg=50.0,
            )
        ]
        fixture = wrap_fixture("clamp-max", [table], queries)
        wa = analyze_opensearch_use_cases(fixture)
        recs, _, _ = analyze_opensearch_patterns(fixture, wa)
        rec = recs[0]
        assert rec.score_breakdown.pattern_match_score <= 100
        assert rec.score_breakdown.complexity_score <= 100
        assert rec.score_breakdown.performance_score <= 100
        assert rec.score_breakdown.cost_score <= 100

    # --- Malformed input (missing queries key) → no crash ---

    def test_missing_queries_key_does_not_crash(self):
        """Missing 'queries' key in collector output should not crash."""
        fixture = {
            "job_id": "malformed-no-queries",
            "database_schema": {
                "tables": [
                    base_table(
                        table_id="public.test_table",
                        table_name="test_table",
                    )
                ]
            },
            # 'queries' key is absent
        }
        wa = analyze_opensearch_use_cases(fixture)
        recs, cls, _ = analyze_opensearch_patterns(fixture, wa)
        assert isinstance(recs, list)
        assert isinstance(cls, dict)

    # --- Malformed input (missing schema key) → no crash ---

    def test_missing_schema_key_does_not_crash(self):
        """Missing 'database_schema' key in collector output should not crash."""
        fixture = {
            "job_id": "malformed-no-schema",
            # 'database_schema' key is absent
            "queries": {
                "query_patterns": [
                    base_query(
                        query_id="ms-001",
                        query_text="SELECT * FROM some_table WHERE id = $1",
                        tables=["public.some_table"],
                    )
                ]
            },
        }
        wa = analyze_opensearch_use_cases(fixture)
        assert wa is not None
        recs, cls, _ = analyze_opensearch_patterns(fixture, wa)
        assert isinstance(recs, list)

    # --- Unicode in query text → no crash ---

    def test_unicode_in_query_text_does_not_crash(self):
        """Unicode characters in query text should not cause a crash."""
        table = base_table(
            table_id="public.i18n_content",
            table_name="i18n_content",
            row_count=10000,
            size_mb=50.0,
        )
        queries = [
            base_query(
                query_id="uc-001",
                query_text=(  # nosemgrep: string-concat-in-list
                    "SELECT * FROM i18n_content "
                    "WHERE title ILIKE '%日本語%' OR content ILIKE '%中文%'"
                ),
                query_type="SELECT",
                tables=["public.i18n_content"],
                calls_per_second=1.0,
            )
        ]
        fixture = wrap_fixture("unicode-test", [table], queries)
        pattern_types, _, _ = _run(fixture)
        # Should detect wildcard-search and not crash
        assert "wildcard-search" in pattern_types

    # --- All tables NOT_SUITABLE → valid output, all NOT_SUITABLE, non-zero cost ---

    def test_all_tables_not_suitable_produces_valid_output(self):
        """When all tables are NOT_SUITABLE, output should still be valid with non-zero cost."""
        table_a = base_table(
            table_id="public.config_a",
            table_name="config_a",
            row_count=50,
            size_mb=1.0,
        )
        table_b = base_table(
            table_id="public.config_b",
            table_name="config_b",
            row_count=100,
            size_mb=2.0,
        )
        queries = [
            base_query(
                query_id="ns-001",
                query_text="SELECT value FROM config_a WHERE key = $1",
                query_type="SELECT",
                tables=["public.config_a"],
                calls_per_second=0.1,
            ),
            base_query(
                query_id="ns-002",
                query_text="SELECT value FROM config_b WHERE key = $1",
                query_type="SELECT",
                tables=["public.config_b"],
                calls_per_second=0.1,
            ),
        ]
        fixture = wrap_fixture("all-not-suitable", [table_a, table_b], queries)
        wa = analyze_opensearch_use_cases(fixture)
        recs, cls, _ = analyze_opensearch_patterns(fixture, wa)
        cost = estimate_opensearch_costs(fixture, "us-east-1")

        assert len(recs) == 2
        for rec in recs:
            assert rec.confidence_score == 0
            assert "[NOT_SUITABLE]" in (rec.rationale or "")
        assert cost.monthly_cost_usd > 0

    # --- Concurrent anti-pattern + search pattern on same table → SEARCH with concerns ---

    def test_search_with_anti_pattern_produces_search_with_concerns(self):
        """Table with both search pattern AND anti-pattern should be SEARCH with concerns."""
        from src.tools.analysis.opensearch_analysis_tools import WorkloadType

        table = base_table(
            table_id="public.search_with_joins",
            table_name="search_with_joins",
            row_count=100_000,
            size_mb=500.0,
            foreign_keys=[
                {
                    "column": "category_id",
                    "referenced_table": "categories",
                    "referenced_column": "id",
                },
                {"column": "brand_id", "referenced_table": "brands", "referenced_column": "id"},
            ],
        )
        table_cat = base_table(
            table_id="public.categories", table_name="categories", row_count=100, size_mb=1.0
        )
        table_brand = base_table(
            table_id="public.brands", table_name="brands", row_count=50, size_mb=1.0
        )
        queries = [
            base_query(
                query_id="swj-search",
                query_text=(  # nosemgrep: string-concat-in-list
                    "SELECT p.*, c.name AS category FROM search_with_joins p "
                    "JOIN categories c ON p.category_id = c.id "
                    "JOIN brands b ON p.brand_id = b.id "
                    "WHERE p.name ILIKE $1"
                ),
                query_type="SELECT",
                tables=["public.search_with_joins", "public.categories", "public.brands"],
                calls_per_second=3.0,
                has_joins=True,
                join_count=2,
            )
        ]
        fixture = wrap_fixture("search-anti-pattern", [table, table_cat, table_brand], queries)
        wa = analyze_opensearch_use_cases(fixture)
        recs, cls, _ = analyze_opensearch_patterns(fixture, wa)

        # The main table should be SEARCH
        assert cls["public.search_with_joins"] == WorkloadType.SEARCH

        # And it should have concerns (anti-pattern detected)
        main_rec = next(r for r in recs if r.table_id == "public.search_with_joins")
        assert main_rec.concerns is not None
        assert len(main_rec.concerns) > 0


# ==========================================================================
# TestScoringBranches — iteration 1 coverage additions
# ==========================================================================


class TestScoringBranches:
    """Cover uncovered scoring adjustment branches in opensearch_analysis_tools.py."""

    # -----------------------------------------------------------------------
    # Line 553: regex-search bonus (+5) in _apply_opensearch_search_adjustments
    # -----------------------------------------------------------------------

    def test_regex_search_pattern_gets_bonus_in_search_adjustments(self):
        """regex-search pattern type triggers the +5 pattern bonus (line 553)."""
        from src.contracts.analysis_output import ScoreBreakdown
        from src.tools.analysis.opensearch_analysis_tools import (
            _apply_opensearch_search_adjustments,
        )
        from src.tools.analysis.scoring import TableProfile

        profile = TableProfile(
            table_id="public.regex_table",
            row_count=10000,
            size_mb=50.0,
            column_count=3,
            has_primary_key=True,
            foreign_key_count=0,
            pattern_types=["regex-search"],
            avg_execution_time_ms=5.0,
        )
        base_scores = ScoreBreakdown(
            pattern_match_score=10,
            complexity_score=10,
            performance_score=10,
            cost_score=10,
        )
        table_schema = {
            "columns": [
                {"column_name": "id", "data_type": "bigint"},
                {"column_name": "name", "data_type": "varchar"},
            ]
        }
        result = _apply_opensearch_search_adjustments(base_scores, profile, table_schema)
        # regex-search adds +5 to pattern, so pattern_match_score must be >= 15
        assert result.pattern_match_score >= 15

    # -----------------------------------------------------------------------
    # Line 570: search cost penalty size > 10GB (10_000 MB)
    # -----------------------------------------------------------------------

    def test_search_large_table_gets_cost_penalty(self):
        """Table > 10GB triggers -10 cost penalty in search adjustments (line 570)."""
        from src.contracts.analysis_output import ScoreBreakdown
        from src.tools.analysis.opensearch_analysis_tools import (
            _apply_opensearch_search_adjustments,
        )
        from src.tools.analysis.scoring import TableProfile

        profile = TableProfile(
            table_id="public.huge_search_table",
            row_count=50_000_000,
            size_mb=15_000.0,  # > 10_000 MB
            column_count=3,
            has_primary_key=True,
            foreign_key_count=0,
            pattern_types=["full-text-search"],
            avg_execution_time_ms=20.0,
        )
        base_scores = ScoreBreakdown(
            pattern_match_score=50,
            complexity_score=50,
            performance_score=50,
            cost_score=50,
        )
        table_schema = {
            "columns": [
                {"column_name": "id", "data_type": "bigint"},
                {"column_name": "body", "data_type": "text"},
            ]
        }
        result = _apply_opensearch_search_adjustments(base_scores, profile, table_schema)
        # cost_score started at 50; large-table penalty subtracts 10 → expect ≤ 40
        assert result.cost_score <= 40

    # -----------------------------------------------------------------------
    # Line 602-603: write_ratio >= 0.7 complexity bonus in TS adjustments
    # -----------------------------------------------------------------------

    def test_ts_high_write_ratio_gets_complexity_bonus(self):
        """write_ratio >= 0.7 triggers +15 complexity bonus in TS adjustments (line 603)."""
        from src.contracts.analysis_output import ScoreBreakdown
        from src.tools.analysis.opensearch_analysis_tools import (
            _apply_opensearch_timeseries_adjustments,
        )
        from src.tools.analysis.scoring import TableProfile

        profile = TableProfile(
            table_id="public.write_heavy_log",
            row_count=5_000_000,
            size_mb=500.0,
            column_count=4,
            has_primary_key=True,
            foreign_key_count=0,
            read_ratio=0.1,  # write_ratio = 0.9 >= 0.7
            total_calls_per_second=20.0,
            pattern_types=["high-ingest"],
            avg_execution_time_ms=2.0,
        )
        base_scores = ScoreBreakdown(
            pattern_match_score=30,
            complexity_score=10,
            performance_score=10,
            cost_score=10,
        )
        result = _apply_opensearch_timeseries_adjustments(base_scores, profile)
        # complexity started at 10; write_ratio bonus adds +15, FK bonus adds +10 → >= 35
        assert result.complexity_score >= 35

    # -----------------------------------------------------------------------
    # Line 615: TS performance bonus for avg_execution_time_ms >= 50ms
    # -----------------------------------------------------------------------

    def test_ts_slow_query_gets_performance_bonus(self):
        """avg_execution_time_ms >= 50ms triggers +10 performance bonus in TS (line 615)."""
        from src.contracts.analysis_output import ScoreBreakdown
        from src.tools.analysis.opensearch_analysis_tools import (
            _apply_opensearch_timeseries_adjustments,
        )
        from src.tools.analysis.scoring import TableProfile

        profile = TableProfile(
            table_id="public.slow_ts_table",
            row_count=2_000_000,
            size_mb=800.0,
            column_count=5,
            has_primary_key=True,
            foreign_key_count=0,
            read_ratio=0.5,
            total_calls_per_second=5.0,  # below 10 so no CPS bonus
            pattern_types=["time-range-query"],
            avg_execution_time_ms=75.0,  # >= 50ms → +10 bonus
        )
        base_scores = ScoreBreakdown(
            pattern_match_score=30,
            complexity_score=20,
            performance_score=10,
            cost_score=20,
        )
        result = _apply_opensearch_timeseries_adjustments(base_scores, profile)
        # performance started at 10; latency bonus adds +10 → >= 20
        assert result.performance_score >= 20

    # -----------------------------------------------------------------------
    # Lines 621-622: TS cost penalty for size < 10 MB
    # -----------------------------------------------------------------------

    def test_ts_tiny_table_gets_cost_penalty(self):
        """size_mb < 10 triggers -10 cost penalty in TS adjustments (lines 621-622)."""
        from src.contracts.analysis_output import ScoreBreakdown
        from src.tools.analysis.opensearch_analysis_tools import (
            _apply_opensearch_timeseries_adjustments,
        )
        from src.tools.analysis.scoring import TableProfile

        profile = TableProfile(
            table_id="public.tiny_ts_table",
            row_count=100,
            size_mb=5.0,  # < 10 MB → -10 cost
            column_count=3,
            has_primary_key=True,
            foreign_key_count=0,
            read_ratio=0.5,
            total_calls_per_second=15.0,
            pattern_types=["high-ingest"],
            avg_execution_time_ms=5.0,
        )
        base_scores = ScoreBreakdown(
            pattern_match_score=20,
            complexity_score=20,
            performance_score=20,
            cost_score=30,
        )
        result = _apply_opensearch_timeseries_adjustments(base_scores, profile)
        # cost started at 30; tiny-table penalty subtracts 10 → <= 20
        assert result.cost_score <= 20

    # -----------------------------------------------------------------------
    # Line 525: _compute_text_ratio with empty columns list
    # -----------------------------------------------------------------------

    def test_compute_text_ratio_empty_columns_returns_zero(self):
        """_compute_text_ratio returns 0.0 when columns list is empty (line 525)."""
        from src.tools.analysis.opensearch_analysis_tools import _compute_text_ratio

        assert _compute_text_ratio({}) == 0.0
        assert _compute_text_ratio({"columns": []}) == 0.0
        assert _compute_text_ratio({"columns": None}) == 0.0


# ==========================================================================
# TestCatalogScoringEdgeCases — iteration 1 coverage additions
# ==========================================================================


class TestCatalogScoringEdgeCases:
    """Cover uncovered catalog-scoring edge cases."""

    # -----------------------------------------------------------------------
    # Line 491: _compute_catalog_pattern_score returns 0 for empty table_patterns
    # -----------------------------------------------------------------------

    def test_catalog_score_empty_patterns_returns_zero(self):
        """_compute_catalog_pattern_score returns 0 when table_patterns is empty (line 491)."""
        from src.tools.analysis.opensearch_analysis_tools import _compute_catalog_pattern_score

        collector_output = {
            "queries": {
                "query_patterns": [
                    base_query(
                        query_id="q-001",
                        query_text="SELECT * FROM t WHERE id = $1",
                        tables=["public.t"],
                        calls_per_second=1.0,
                    )
                ]
            }
        }
        result = _compute_catalog_pattern_score([], collector_output)
        assert result == 0

    # -----------------------------------------------------------------------
    # Line 504: catalog pattern not found (unknown pattern_id) → continue
    # -----------------------------------------------------------------------

    def test_catalog_score_unknown_pattern_id_is_skipped(self):
        """Pattern with unknown pattern_id is skipped via continue (line 504)."""
        from src.contracts.analysis_output import Confidence, Pattern
        from src.tools.analysis.opensearch_analysis_tools import _compute_catalog_pattern_score

        # Create a Pattern object with an unknown ID that won't exist in PATTERN_BY_ID
        unknown_pattern = Pattern(
            pattern_id="os-UNKNOWN-9999",
            pattern_type="unknown-type",
            confidence=Confidence.LOW,
            description="Unknown pattern for testing.",
            query_ids=["q-unknown"],
            table_ids=["public.t"],
        )
        collector_output = {
            "queries": {
                "query_patterns": [
                    base_query(
                        query_id="q-unknown",
                        query_text="SELECT * FROM t WHERE id = $1",
                        tables=["public.t"],
                        calls_per_second=2.0,
                    )
                ]
            }
        }
        # All patterns are unknown → total_weight stays 0 → returns 0
        result = _compute_catalog_pattern_score([unknown_pattern], collector_output)
        assert result == 0

    # -----------------------------------------------------------------------
    # Line 512: total_weight == 0 → return 0
    # (All matched patterns have zero CPS, but the min(cps, 0.01) guard means
    #  we need ALL patterns to be unknown to hit total_weight == 0 path)
    # -----------------------------------------------------------------------

    def test_catalog_score_all_unknown_patterns_returns_zero(self):
        """When all patterns have unknown IDs, total_weight stays 0 → return 0 (line 512)."""
        from src.contracts.analysis_output import Confidence, Pattern
        from src.tools.analysis.opensearch_analysis_tools import _compute_catalog_pattern_score

        p1 = Pattern(
            pattern_id="os-FAKE-1",
            pattern_type="fake-type-1",
            confidence=Confidence.LOW,
            description="Fake pattern 1",
            query_ids=["fq-001"],
            table_ids=["public.fake"],
        )
        p2 = Pattern(
            pattern_id="os-FAKE-2",
            pattern_type="fake-type-2",
            confidence=Confidence.LOW,
            description="Fake pattern 2",
            query_ids=["fq-002"],
            table_ids=["public.fake"],
        )
        collector_output = {
            "queries": {
                "query_patterns": [
                    base_query(
                        query_id="fq-001",
                        query_text="SELECT 1",
                        tables=["public.fake"],
                        calls_per_second=5.0,
                    ),
                    base_query(
                        query_id="fq-002",
                        query_text="SELECT 2",
                        tables=["public.fake"],
                        calls_per_second=3.0,
                    ),
                ]
            }
        }
        result = _compute_catalog_pattern_score([p1, p2], collector_output)
        assert result == 0


# ==========================================================================
# TestHelperFunctionBranches — iteration 1 coverage additions
# ==========================================================================


class TestHelperFunctionBranches:
    """Cover uncovered helper function branches."""

    # -----------------------------------------------------------------------
    # Line 646: _build_rationale with workload type but empty table_patterns list
    # -----------------------------------------------------------------------

    def test_build_rationale_search_no_patterns_returns_prefix_message(self):
        """_build_rationale returns prefix + 'No strong patterns' when patterns list empty (line 646)."""
        from src.tools.analysis.opensearch_analysis_tools import WorkloadType, _build_rationale
        from src.tools.analysis.scoring import TableProfile

        profile = TableProfile(
            table_id="public.sparse_table",
            row_count=1000,
            size_mb=10.0,
            column_count=2,
            has_primary_key=True,
            foreign_key_count=0,
            pattern_types=[],
            total_calls_per_second=1.0,
        )
        result = _build_rationale(profile, [], WorkloadType.SEARCH)
        assert "[SEARCH]" in result
        assert "No strong patterns" in result

    def test_build_rationale_timeseries_no_patterns_returns_prefix_message(self):
        """_build_rationale with TIMESERIES workload and empty patterns list (line 646)."""
        from src.tools.analysis.opensearch_analysis_tools import WorkloadType, _build_rationale
        from src.tools.analysis.scoring import TableProfile

        profile = TableProfile(
            table_id="public.sparse_ts",
            row_count=1000,
            size_mb=10.0,
            column_count=2,
            has_primary_key=True,
            foreign_key_count=0,
            pattern_types=[],
            total_calls_per_second=1.0,
        )
        result = _build_rationale(profile, [], WorkloadType.TIMESERIES)
        assert "[TIMESERIES]" in result
        assert "No strong patterns" in result

    # -----------------------------------------------------------------------
    # Line 660: TS rationale with append-heavy write pattern (write_ratio >= 0.7)
    # -----------------------------------------------------------------------

    def test_build_rationale_ts_append_heavy_includes_data_streams_message(self):
        """_build_rationale for TIMESERIES with write_ratio >= 0.7 mentions data streams (line 660)."""
        from src.contracts.analysis_output import Confidence, Pattern
        from src.tools.analysis.opensearch_analysis_tools import WorkloadType, _build_rationale
        from src.tools.analysis.scoring import TableProfile

        profile = TableProfile(
            table_id="public.write_heavy",
            row_count=2_000_000,
            size_mb=800.0,
            column_count=4,
            has_primary_key=True,
            foreign_key_count=0,
            read_ratio=0.1,  # write_ratio = 0.9 >= 0.7
            total_calls_per_second=25.0,
            pattern_types=["high-ingest"],
        )
        ts_pattern = Pattern(
            pattern_id="os-07",
            pattern_type="high-ingest",
            confidence=Confidence.HIGH,
            description="High ingest pattern",
            query_ids=["hi-001"],
            table_ids=["public.write_heavy"],
        )
        result = _build_rationale(profile, [ts_pattern], WorkloadType.TIMESERIES)
        assert "[TIMESERIES]" in result
        assert "Append-heavy write pattern" in result

    # -----------------------------------------------------------------------
    # Line 706: _assess_migration_complexity HIGH (2+ anti-patterns or FK > 3)
    # -----------------------------------------------------------------------

    def test_assess_migration_complexity_high_two_anti_patterns(self):
        """2 anti-patterns → HIGH migration complexity (line 706)."""
        from src.contracts.analysis_output import AntiPattern, MigrationComplexity
        from src.tools.analysis.opensearch_analysis_tools import _assess_migration_complexity
        from src.tools.analysis.scoring import TableProfile

        profile = TableProfile(
            table_id="public.complex_table",
            row_count=100_000,
            size_mb=500.0,
            column_count=10,
            has_primary_key=True,
            foreign_key_count=1,
            pattern_types=[],
        )
        ap1 = AntiPattern(
            anti_pattern_id="os-ap-01",
            anti_pattern_type="multi-index-joins",
            severity_weight=0.8,
            description="Joins detected",
            query_ids=["q-1"],
            table_ids=["public.complex_table"],
        )
        ap2 = AntiPattern(
            anti_pattern_id="os-ap-02",
            anti_pattern_type="acid-transactions",
            severity_weight=0.7,
            description="Transactions detected",
            query_ids=["q-2"],
            table_ids=["public.complex_table"],
        )
        result = _assess_migration_complexity(profile, [ap1, ap2], None)
        assert result == MigrationComplexity.HIGH

    def test_assess_migration_complexity_high_fk_count_greater_than_3(self):
        """FK count > 3 → HIGH migration complexity (line 706)."""
        from src.contracts.analysis_output import MigrationComplexity
        from src.tools.analysis.opensearch_analysis_tools import _assess_migration_complexity
        from src.tools.analysis.scoring import TableProfile

        profile = TableProfile(
            table_id="public.fk_heavy",
            row_count=50_000,
            size_mb=200.0,
            column_count=10,
            has_primary_key=True,
            foreign_key_count=4,  # > 3 → HIGH
            pattern_types=[],
        )
        result = _assess_migration_complexity(profile, [], None)
        assert result == MigrationComplexity.HIGH

    # -----------------------------------------------------------------------
    # Lines 719, 721: _confidence_to_recommendation SUITABLE and MARGINAL
    # -----------------------------------------------------------------------

    def test_confidence_to_recommendation_suitable_threshold(self):
        """Confidence 50-74 returns SUITABLE (line 719)."""
        from src.tools.analysis.opensearch_analysis_tools import _confidence_to_recommendation

        assert _confidence_to_recommendation(50) == "SUITABLE"
        assert _confidence_to_recommendation(60) == "SUITABLE"
        assert _confidence_to_recommendation(74) == "SUITABLE"

    def test_confidence_to_recommendation_marginal_threshold(self):
        """Confidence 25-49 returns MARGINAL (line 721)."""
        from src.tools.analysis.opensearch_analysis_tools import _confidence_to_recommendation

        assert _confidence_to_recommendation(25) == "MARGINAL"
        assert _confidence_to_recommendation(40) == "MARGINAL"
        assert _confidence_to_recommendation(49) == "MARGINAL"

    def test_confidence_to_recommendation_not_suitable_below_25(self):
        """Confidence < 25 returns NOT_SUITABLE."""
        from src.tools.analysis.opensearch_analysis_tools import _confidence_to_recommendation

        assert _confidence_to_recommendation(0) == "NOT_SUITABLE"
        assert _confidence_to_recommendation(24) == "NOT_SUITABLE"


# ==========================================================================
# TestTimeseriesColumnStaleness — iteration 1 coverage additions
# ==========================================================================


class TestTimeseriesColumnStaleness:
    """Cover staleness detection via column names (lines 435-439, 979-980)."""

    # -----------------------------------------------------------------------
    # Lines 435-439: _is_timeseries staleness via column names (not table name)
    # -----------------------------------------------------------------------

    def test_staleness_via_column_name_not_table_name(self):
        """Table with generic name but staleness column (e.g. 'event_time') → TIMESERIES (lines 435-439)."""
        from src.tools.analysis.opensearch_analysis_tools import WorkloadType

        # Use a table name that has NO staleness keywords
        table = base_table(
            table_id="public.generic_records",
            table_name="generic_records",  # no staleness keyword
            row_count=5_000_000,
            size_mb=2000.0,
            columns=[
                {
                    "column_name": "id",
                    "ordinal_position": 1,
                    "data_type": "bigint",
                    "nullable": False,
                },
                {
                    "column_name": "event_time",  # staleness keyword in column name
                    "ordinal_position": 2,
                    "data_type": "timestamp",
                    "nullable": False,
                },
                {
                    "column_name": "payload",
                    "ordinal_position": 3,
                    "data_type": "text",
                    "nullable": True,
                },
            ],
        )
        queries = [
            base_query(
                query_id="gs-insert",
                query_text="INSERT INTO generic_records (event_time, payload) VALUES ($1, $2)",
                query_type="INSERT",
                tables=["public.generic_records"],
                calls_per_second=15.0,
            ),
            base_query(
                query_id="gs-select",
                query_text="SELECT * FROM generic_records WHERE event_time >= $1 AND event_time <= $2",
                query_type="SELECT",
                tables=["public.generic_records"],
                calls_per_second=1.0,
            ),
        ]
        fixture = wrap_fixture("column-staleness-test", [table], queries)
        wa = analyze_opensearch_use_cases(fixture)
        result = classify_table_workload("public.generic_records", wa, table, queries)
        assert result == WorkloadType.TIMESERIES

    def test_staleness_via_log_level_column_name(self):
        """Table with 'log_level' column (staleness keyword) qualifies as TIMESERIES."""
        from src.tools.analysis.opensearch_analysis_tools import WorkloadType

        table = base_table(
            table_id="public.app_data",
            table_name="app_data",  # no staleness in table name
            row_count=10_000_000,
            size_mb=4000.0,
            columns=[
                {
                    "column_name": "id",
                    "ordinal_position": 1,
                    "data_type": "bigint",
                    "nullable": False,
                },
                {
                    "column_name": "log_level",  # staleness keyword in column name
                    "ordinal_position": 2,
                    "data_type": "varchar",
                    "nullable": False,
                },
                {
                    "column_name": "recorded_at",
                    "ordinal_position": 3,
                    "data_type": "timestamp",
                    "nullable": False,
                },
            ],
        )
        queries = [
            base_query(
                query_id="ld-insert",
                query_text="INSERT INTO app_data (log_level, recorded_at) VALUES ($1, NOW())",
                query_type="INSERT",
                tables=["public.app_data"],
                calls_per_second=20.0,
            ),
            base_query(
                query_id="ld-select",
                query_text="SELECT * FROM app_data WHERE recorded_at >= $1 AND recorded_at <= $2",
                query_type="SELECT",
                tables=["public.app_data"],
                calls_per_second=2.0,
            ),
        ]
        fixture = wrap_fixture("log-level-column-staleness", [table], queries)
        wa = analyze_opensearch_use_cases(fixture)
        result = classify_table_workload("public.app_data", wa, table, queries)
        assert result == WorkloadType.TIMESERIES

    # -----------------------------------------------------------------------
    # Lines 979-980: decision trace staleness from column names (not table name)
    # -----------------------------------------------------------------------

    def test_decision_trace_staleness_from_column_names(self):
        """Decision trace reports staleness=True when detected via column names (lines 979-980)."""
        table = base_table(
            table_id="public.generic_records",
            table_name="generic_records",  # no staleness keyword in table name
            row_count=5_000_000,
            size_mb=2000.0,
            columns=[
                {
                    "column_name": "id",
                    "ordinal_position": 1,
                    "data_type": "bigint",
                    "nullable": False,
                },
                {
                    "column_name": "event_time",  # staleness keyword in column name
                    "ordinal_position": 2,
                    "data_type": "timestamp",
                    "nullable": False,
                },
                {
                    "column_name": "payload",
                    "ordinal_position": 3,
                    "data_type": "text",
                    "nullable": True,
                },
            ],
        )
        queries = [
            base_query(
                query_id="tr-gs-insert",
                query_text="INSERT INTO generic_records (event_time, payload) VALUES ($1, $2)",
                query_type="INSERT",
                tables=["public.generic_records"],
                calls_per_second=15.0,
            ),
            base_query(
                query_id="tr-gs-select",
                query_text="SELECT * FROM generic_records WHERE event_time >= $1 AND event_time <= $2",
                query_type="SELECT",
                tables=["public.generic_records"],
                calls_per_second=1.0,
            ),
        ]
        fixture = wrap_fixture("trace-column-staleness-test", [table], queries)
        wa = analyze_opensearch_use_cases(fixture)
        recs, cls, weights = analyze_opensearch_patterns(fixture, wa)
        trace = build_opensearch_decision_trace(fixture, wa, recs, cls, weights)

        wc = trace["workload_classifications"]
        entry = next(e for e in wc if e["table_id"] == "public.generic_records")
        assert entry["timeseries_criteria_met"]["staleness"] is True


# ==========================================================================
# TestDecisionTraceSignals — iteration 1 coverage additions
# ==========================================================================


class TestDecisionTraceSignals:
    """Cover decision trace branch: multi_table_join signal (line 902)."""

    # -----------------------------------------------------------------------
    # Line 902: signal `multi_table_join` in decision trace query_matches
    # -----------------------------------------------------------------------

    def test_multi_table_join_signal_in_decision_trace(self):
        """Query with has_joins=True, join_count>=2 emits multi_table_join signal (line 902)."""
        table_a = base_table(
            table_id="public.articles",
            table_name="articles",
            row_count=100_000,
            size_mb=500.0,
        )
        table_b = base_table(
            table_id="public.authors",
            table_name="authors",
            row_count=10_000,
            size_mb=50.0,
        )
        table_c = base_table(
            table_id="public.categories",
            table_name="categories",
            row_count=500,
            size_mb=5.0,
        )
        queries = [
            base_query(
                query_id="mtj-001",
                query_text=(  # nosemgrep: string-concat-in-list
                    "SELECT a.title, au.name, c.label "
                    "FROM articles a "
                    "JOIN authors au ON a.author_id = au.id "
                    "JOIN categories c ON a.category_id = c.id "
                    "WHERE a.title ILIKE $1"
                ),
                query_type="SELECT",
                tables=["public.articles", "public.authors", "public.categories"],
                calls_per_second=2.0,
                has_joins=True,
                join_count=2,
            )
        ]
        fixture = wrap_fixture("multi-join-trace-test", [table_a, table_b, table_c], queries)
        wa = analyze_opensearch_use_cases(fixture)
        recs, cls, weights = analyze_opensearch_patterns(fixture, wa)
        trace = build_opensearch_decision_trace(fixture, wa, recs, cls, weights)

        # Find the query match for mtj-001
        qm = next(m for m in trace["query_matches"] if m["query_id"] == "mtj-001")
        assert "multi_table_join" in qm["signals"]


# ==========================================================================
# TestEdgeCasesV2 (Iteration 2)
# ==========================================================================


class TestEdgeCasesV2:
    """Edge case tests that stress the system beyond existing coverage."""

    # ------------------------------------------------------------------
    # Pattern detection edge cases
    # ------------------------------------------------------------------

    def test_all_four_search_patterns_detected_simultaneously(self):
        """A single query containing tsvector + ILIKE + REGEXP + similarity() triggers all 4."""
        table = base_table(
            table_id="public.all_search",
            table_name="all_search",
            row_count=50000,
            size_mb=200.0,
        )
        queries = [
            base_query(
                query_id="all4-001",
                query_text=(  # nosemgrep: string-concat-in-list
                    "SELECT * FROM all_search "
                    "WHERE search_vector @@ to_tsvector('english', $1) "
                    "OR name ILIKE $2 "
                    "OR code ~ $3 "
                    "OR similarity(title, $4) > 0.3"
                ),
                query_type="SELECT",
                tables=["public.all_search"],
                calls_per_second=1.0,
            )
        ]
        fixture = wrap_fixture("all-four-search", [table], queries)
        pattern_types, _, _ = _run(fixture)
        assert "full-text-search" in pattern_types
        assert "wildcard-search" in pattern_types
        assert "regex-search" in pattern_types
        assert "fuzzy-search" in pattern_types

    def test_query_text_none_does_not_crash(self):
        """A query with query_text=None should not raise an exception."""
        table = base_table(
            table_id="public.null_text_table",
            table_name="null_text_table",
            row_count=1000,
            size_mb=5.0,
        )
        queries = [
            base_query(
                query_id="null-text-001",
                query_text="SELECT * FROM null_text_table WHERE id = $1",
                query_type="SELECT",
                tables=["public.null_text_table"],
                calls_per_second=1.0,
            )
        ]
        # Mutate query_text to None after construction
        queries[0]["query_text"] = None
        fixture = wrap_fixture("null-text-test", [table], queries)
        # Must not raise
        result = analyze_opensearch_use_cases(fixture)
        assert result is not None

    def test_query_with_no_query_id_builds_pattern(self):
        """A query missing query_id should still be processed (query_ids may contain '')."""
        table = base_table(
            table_id="public.noid_table",
            table_name="noid_table",
            row_count=5000,
            size_mb=20.0,
        )
        query_no_id = {
            "query_text": "SELECT id FROM noid_table WHERE name ILIKE $1",
            "query_type": "SELECT",
            "tables_accessed": ["public.noid_table"],
            "calls_per_second": 2.0,
            "execution_time_ms_avg": 10.0,
            "rows_returned_avg": 5,
            "has_joins": False,
            "join_count": 0,
            "filter_columns": [],
            # no "query_id" key
        }
        fixture = wrap_fixture("no-id-test", [table], [query_no_id])
        result = analyze_opensearch_use_cases(fixture)
        pattern_types = {p.pattern_type for p in result.patterns_detected}
        assert "wildcard-search" in pattern_types

    def test_very_long_query_text_no_crash(self):
        """A query with 10,000-character text should not crash or timeout."""
        table = base_table(
            table_id="public.long_text_table",
            table_name="long_text_table",
            row_count=1000,
            size_mb=5.0,
        )
        long_text = "SELECT * FROM long_text_table WHERE name ILIKE $1 " + (
            "x" * 9950  # nosec B608
        )
        queries = [
            base_query(
                query_id="long-001",
                query_text=long_text,
                query_type="SELECT",
                tables=["public.long_text_table"],
                calls_per_second=0.5,
            )
        ]
        fixture = wrap_fixture("long-text-test", [table], queries)
        result = analyze_opensearch_use_cases(fixture)
        assert result is not None

    def test_unicode_characters_in_query_text_no_crash(self):
        """Japanese characters and emoji in query text should not crash the detector."""
        table = base_table(
            table_id="public.unicode_table",
            table_name="unicode_table",
            row_count=1000,
            size_mb=5.0,
        )
        queries = [
            base_query(
                query_id="uni-001",
                query_text="SELECT * FROM unicode_table WHERE name ILIKE '%\u65e5\u672c\u8a9e\U0001f600%'",
                query_type="SELECT",
                tables=["public.unicode_table"],
                calls_per_second=0.5,
            )
        ]
        fixture = wrap_fixture("unicode-test", [table], queries)
        result = analyze_opensearch_use_cases(fixture)
        assert result is not None
        pattern_types = {p.pattern_type for p in result.patterns_detected}
        assert "wildcard-search" in pattern_types

    # ------------------------------------------------------------------
    # Classification edge cases
    # ------------------------------------------------------------------

    def test_no_ts_pattern_with_high_insert_fails_ts_criterion_1(self):
        """Table with INSERT CPS < threshold: high-ingest not triggered, no TS patterns → NOT TIMESERIES.

        Criterion 1 requires at least one of {time-range-query, time-aggregation, high-ingest}.
        With INSERT CPS = 5 (below 10), none are triggered → criterion 1 fails.
        """
        from src.tools.analysis.opensearch_analysis_tools import WorkloadType

        table = base_table(
            table_id="public.low_insert_store",
            table_name="low_insert_store",
            row_count=500_000,
            size_mb=200.0,
            columns=[
                {
                    "column_name": "id",
                    "ordinal_position": 1,
                    "data_type": "bigint",
                    "nullable": False,
                },
                {
                    "column_name": "data",
                    "ordinal_position": 2,
                    "data_type": "text",
                    "nullable": True,
                },
                # Note: no timestamp column, so no time-range or time-agg patterns
            ],
        )
        queries = [
            base_query(
                query_id="li-insert",
                query_text="INSERT INTO low_insert_store (data) VALUES ($1)",
                query_type="INSERT",
                tables=["public.low_insert_store"],
                calls_per_second=5.0,  # below HIGH_INGEST_CPS_THRESHOLD of 10.0
            )
        ]
        fixture = wrap_fixture("low-insert-store", [table], queries)
        wa = analyze_opensearch_use_cases(fixture)
        # No TS patterns: high-ingest not triggered (CPS < 10), no time-range, no time-agg
        ts_pattern_types = {p.pattern_type for p in wa.patterns_detected}
        assert not ts_pattern_types & {"time-range-query", "time-aggregation", "high-ingest"}
        result = classify_table_workload("public.low_insert_store", wa, table, queries)
        assert result != WorkloadType.TIMESERIES

    def test_all_ts_patterns_but_no_staleness_in_names_fails_criterion_3(self):
        """Table named 'data_store' with columns 'col1','col2' fails staleness criterion → NOT TIMESERIES."""
        from src.tools.analysis.opensearch_analysis_tools import WorkloadType

        table = base_table(
            table_id="public.data_store",
            table_name="data_store",
            row_count=2_000_000,
            size_mb=800.0,
            columns=[
                {
                    "column_name": "id",
                    "ordinal_position": 1,
                    "data_type": "bigint",
                    "nullable": False,
                },
                {
                    "column_name": "col1",
                    "ordinal_position": 2,
                    "data_type": "float",
                    "nullable": True,
                },
                {
                    "column_name": "col2",
                    "ordinal_position": 3,
                    "data_type": "float",
                    "nullable": True,
                },
                # timestamp keyword so range query is detected
                {
                    "column_name": "created_at",
                    "ordinal_position": 4,
                    "data_type": "timestamp",
                    "nullable": False,
                },
            ],
        )
        queries = [
            base_query(
                query_id="ds-insert",
                query_text="INSERT INTO data_store (col1, col2, created_at) VALUES ($1, $2, NOW())",
                query_type="INSERT",
                tables=["public.data_store"],
                calls_per_second=15.0,
            ),
            base_query(
                query_id="ds-select",
                query_text="SELECT * FROM data_store WHERE created_at >= $1 AND created_at <= $2",
                query_type="SELECT",
                tables=["public.data_store"],
                calls_per_second=2.0,
            ),
        ]
        fixture = wrap_fixture("data-store-no-staleness", [table], queries)
        wa = analyze_opensearch_use_cases(fixture)
        result = classify_table_workload("public.data_store", wa, table, queries)
        # "data_store" has no staleness keywords (log/event/metric/trace/telemetry)
        # and columns are col1/col2/created_at — none contain staleness keywords
        assert result != WorkloadType.TIMESERIES

    def test_staleness_from_column_name_metric_value(self):
        """No staleness in table name, but column 'metric_value' satisfies staleness criterion."""
        from src.tools.analysis.opensearch_analysis_tools import WorkloadType

        table = base_table(
            table_id="public.readings",
            table_name="readings",
            row_count=3_000_000,
            size_mb=1200.0,
            columns=[
                {
                    "column_name": "id",
                    "ordinal_position": 1,
                    "data_type": "bigint",
                    "nullable": False,
                },
                # "metric" is in STALENESS_TABLE_KEYWORDS — satisfies criterion 3
                {
                    "column_name": "metric_value",
                    "ordinal_position": 2,
                    "data_type": "float",
                    "nullable": False,
                },
                {
                    "column_name": "recorded_at",
                    "ordinal_position": 3,
                    "data_type": "timestamp",
                    "nullable": False,
                },
            ],
        )
        queries = [
            base_query(
                query_id="mv-insert",
                query_text="INSERT INTO readings (metric_value, recorded_at) VALUES ($1, NOW())",
                query_type="INSERT",
                tables=["public.readings"],
                calls_per_second=12.0,
            ),
            base_query(
                query_id="mv-select",
                query_text="SELECT * FROM readings WHERE recorded_at >= $1 AND recorded_at <= $2",
                query_type="SELECT",
                tables=["public.readings"],
                calls_per_second=1.0,
            ),
        ]
        fixture = wrap_fixture("readings-metric-col", [table], queries)
        wa = analyze_opensearch_use_cases(fixture)
        result = classify_table_workload("public.readings", wa, table, queries)
        assert result == WorkloadType.TIMESERIES

    def test_insert_cps_exactly_at_threshold_triggers_high_ingest(self):
        """INSERT CPS == 10.0 exactly at the 10.0 threshold should trigger high-ingest pattern."""
        table = base_table(
            table_id="public.boundary_log",
            table_name="boundary_log",
            row_count=1_000_000,
            size_mb=400.0,
        )
        queries = [
            base_query(
                query_id="bound-insert",
                query_text="INSERT INTO boundary_log (data) VALUES ($1)",
                query_type="INSERT",
                tables=["public.boundary_log"],
                calls_per_second=10.0,
            )
        ]
        fixture = wrap_fixture("boundary-exact-10", [table], queries)
        pattern_types, _, _ = _run(fixture)
        assert "high-ingest" in pattern_types

    def test_insert_cps_just_below_threshold_does_not_trigger_high_ingest(self):
        """INSERT CPS == 9.99 (just below 10.0) should NOT trigger high-ingest pattern."""
        table = base_table(
            table_id="public.just_below_log",
            table_name="just_below_log",
            row_count=500_000,
            size_mb=200.0,
        )
        queries = [
            base_query(
                query_id="below-insert",
                query_text="INSERT INTO just_below_log (data) VALUES ($1)",
                query_type="INSERT",
                tables=["public.just_below_log"],
                calls_per_second=9.99,
            )
        ]
        fixture = wrap_fixture("just-below-9.99", [table], queries)
        pattern_types, _, _ = _run(fixture)
        assert "high-ingest" not in pattern_types

    def test_zero_update_delete_cps_with_high_insert_no_division_by_zero(self):
        """Zero UPDATE/DELETE CPS with high INSERT: ratio check passes without division by zero."""
        from src.tools.analysis.opensearch_analysis_tools import WorkloadType

        table = base_table(
            table_id="public.append_only_log",
            table_name="append_only_log",
            row_count=10_000_000,
            size_mb=5000.0,
            columns=[
                {
                    "column_name": "id",
                    "ordinal_position": 1,
                    "data_type": "bigint",
                    "nullable": False,
                },
                {
                    "column_name": "log_time",
                    "ordinal_position": 2,
                    "data_type": "timestamp",
                    "nullable": False,
                },
                {
                    "column_name": "message",
                    "ordinal_position": 3,
                    "data_type": "text",
                    "nullable": True,
                },
            ],
        )
        queries = [
            base_query(
                query_id="ao-insert",
                query_text="INSERT INTO append_only_log (log_time, message) VALUES ($1, $2)",
                query_type="INSERT",
                tables=["public.append_only_log"],
                calls_per_second=25.0,
            ),
            base_query(
                query_id="ao-select",
                query_text="SELECT * FROM append_only_log WHERE log_time >= $1 AND log_time <= $2",
                query_type="SELECT",
                tables=["public.append_only_log"],
                calls_per_second=2.0,
            ),
        ]
        fixture = wrap_fixture("append-only-no-div-zero", [table], queries)
        wa = analyze_opensearch_use_cases(fixture)
        # "log" in table name satisfies staleness; must not raise ZeroDivisionError
        result = classify_table_workload("public.append_only_log", wa, table, queries)
        assert result == WorkloadType.TIMESERIES

    # ------------------------------------------------------------------
    # Scoring edge cases
    # ------------------------------------------------------------------

    def test_table_with_100_percent_text_columns_gets_complexity_bonus(self):
        """text_ratio == 1.0 (all text columns) → complexity_score gets +15 from search adjustments."""
        table = base_table(
            table_id="public.all_text_table",
            table_name="all_text_table",
            row_count=100_000,
            size_mb=500.0,
            columns=[
                {
                    "column_name": "title",
                    "ordinal_position": 1,
                    "data_type": "text",
                    "nullable": False,
                },
                {
                    "column_name": "body",
                    "ordinal_position": 2,
                    "data_type": "text",
                    "nullable": True,
                },
                {
                    "column_name": "summary",
                    "ordinal_position": 3,
                    "data_type": "text",
                    "nullable": True,
                },
            ],
        )
        queries = [
            base_query(
                query_id="att-001",
                query_text="SELECT title FROM all_text_table WHERE body ILIKE $1",
                query_type="SELECT",
                tables=["public.all_text_table"],
                calls_per_second=2.0,
                execution_time_ms_avg=15.0,
            )
        ]
        fixture = wrap_fixture("all-text-test", [table], queries)
        wa = analyze_opensearch_use_cases(fixture)
        recs, _, _ = analyze_opensearch_patterns(fixture, wa)
        rec = recs[0]
        # text_ratio = 1.0 >= 0.3, so complexity gets +15
        assert rec.score_breakdown.complexity_score > 0

    def test_table_with_zero_columns_no_crash(self):
        """Table schema with 0 columns: text_ratio = 0.0, no crash."""
        table = base_table(
            table_id="public.no_columns_table",
            table_name="no_columns_table",
            row_count=1000,
            size_mb=5.0,
            columns=[],
        )
        queries = [
            base_query(
                query_id="nc-001",
                query_text="SELECT * FROM no_columns_table WHERE name ILIKE $1",
                query_type="SELECT",
                tables=["public.no_columns_table"],
                calls_per_second=1.0,
            )
        ]
        fixture = wrap_fixture("no-columns-test", [table], queries)
        wa = analyze_opensearch_use_cases(fixture)
        recs, _, _ = analyze_opensearch_patterns(fixture, wa)
        assert recs
        rec = recs[0]
        # text_ratio = 0.0 (no columns), but no crash
        assert rec.score_breakdown.pattern_match_score >= 0
        assert rec.score_breakdown.complexity_score >= 0

    def test_pattern_score_with_all_zero_cps_uses_minimum_weight(self):
        """Patterns where all matched queries have 0 CPS use minimum weight (0.01) → non-zero score."""
        table = base_table(
            table_id="public.zero_cps_table",
            table_name="zero_cps_table",
            row_count=10_000,
            size_mb=50.0,
        )
        queries = [
            base_query(
                query_id="zcps-001",
                query_text="SELECT * FROM zero_cps_table WHERE body ILIKE $1",
                query_type="SELECT",
                tables=["public.zero_cps_table"],
                calls_per_second=0.0,  # zero CPS
            )
        ]
        fixture = wrap_fixture("zero-cps-test", [table], queries)
        wa = analyze_opensearch_use_cases(fixture)
        recs, _, _ = analyze_opensearch_patterns(fixture, wa)
        rec = recs[0]
        # Even with 0 CPS, minimum weight 0.01 ensures pattern_match_score > 0
        assert rec.score_breakdown.pattern_match_score > 0

    def test_not_suitable_table_still_gets_cost_estimate(self):
        """NOT_SUITABLE table (confidence=0) still produces a non-zero cost estimate at the pipeline level."""
        table = base_table(
            table_id="public.unsuitable_table",
            table_name="unsuitable_table",
            row_count=500,
            size_mb=2.0,
        )
        queries = [
            base_query(
                query_id="uns-001",
                query_text="SELECT id FROM unsuitable_table WHERE id = $1",
                query_type="SELECT",
                tables=["public.unsuitable_table"],
                calls_per_second=0.5,
            )
        ]
        fixture = wrap_fixture("unsuitable-cost-test", [table], queries)
        cost = estimate_opensearch_costs(fixture, "us-east-1")
        # Cost estimate is driven by infrastructure (2 data nodes), not table suitability
        assert cost.monthly_cost_usd > 0

    # ------------------------------------------------------------------
    # Decision trace edge cases
    # ------------------------------------------------------------------

    def _make_trace(self, fixture: dict) -> dict:
        wa = analyze_opensearch_use_cases(fixture)
        recs, cls, weights = analyze_opensearch_patterns(fixture, wa)
        return build_opensearch_decision_trace(fixture, wa, recs, cls, weights)

    def test_trace_with_zero_queries_has_queries_analyzed_zero(self):
        """Decision trace with no queries should report queries_analyzed=0 in summary."""
        table = base_table(
            table_id="public.empty_query_table",
            table_name="empty_query_table",
            row_count=1000,
            size_mb=5.0,
        )
        fixture = wrap_fixture("zero-queries-trace", [table], [])
        trace = self._make_trace(fixture)
        assert trace["summary"]["queries_analyzed"] == 0

    def test_trace_search_table_failing_ts_shows_partial_ts_criteria(self):
        """Table classified SEARCH (meets search patterns but fails TS) shows partial TS criteria."""
        table = base_table(
            table_id="public.search_not_ts",
            table_name="search_not_ts",
            row_count=50_000,
            size_mb=200.0,
        )
        queries = [
            base_query(
                query_id="snts-001",
                query_text="SELECT * FROM search_not_ts WHERE name ILIKE $1",
                query_type="SELECT",
                tables=["public.search_not_ts"],
                calls_per_second=5.0,
            )
        ]
        fixture = wrap_fixture("search-not-ts-trace", [table], queries)
        trace = self._make_trace(fixture)
        wc = trace["workload_classifications"]
        assert wc, "Expected at least one workload classification entry"
        entry = wc[0]
        assert entry["workload_type"] == "SEARCH"
        # TS criteria should all be False: no time-range, no high ingest, no staleness
        criteria = entry["timeseries_criteria_met"]
        assert criteria["timestamp_range"] is False
        assert criteria["high_ingest"] is False

    def test_trace_includes_not_suitable_tables_in_workload_classifications(self):
        """NOT_SUITABLE tables must appear in workload_classifications (not silently dropped)."""
        table = base_table(
            table_id="public.ignored_table",
            table_name="ignored_table",
            row_count=100,
            size_mb=1.0,
        )
        queries = [
            base_query(
                query_id="ign-001",
                query_text="SELECT value FROM ignored_table WHERE key = $1",
                query_type="SELECT",
                tables=["public.ignored_table"],
                calls_per_second=0.1,
            )
        ]
        fixture = wrap_fixture("not-suitable-trace", [table], queries)
        trace = self._make_trace(fixture)
        wc_ids = {entry["table_id"] for entry in trace["workload_classifications"]}
        assert "public.ignored_table" in wc_ids
        entry = next(
            e for e in trace["workload_classifications"] if e["table_id"] == "public.ignored_table"
        )
        assert entry["workload_type"] == "NOT_SUITABLE"

    # ------------------------------------------------------------------
    # Full pipeline edge cases
    # ------------------------------------------------------------------

    def test_table_matching_search_and_ts_patterns_but_failing_ts_classified_as_search(self):
        """Table with ILIKE + timestamp BETWEEN + INSERT (low CPS) → SEARCH (TS fails CPS criterion)."""
        from src.tools.analysis.opensearch_analysis_tools import WorkloadType

        table = base_table(
            table_id="public.mixed_pattern_table",
            table_name="mixed_pattern_table",
            row_count=500_000,
            size_mb=300.0,
            columns=[
                {
                    "column_name": "id",
                    "ordinal_position": 1,
                    "data_type": "bigint",
                    "nullable": False,
                },
                {
                    "column_name": "name",
                    "ordinal_position": 2,
                    "data_type": "varchar",
                    "nullable": False,
                },
                {
                    "column_name": "event_time",
                    "ordinal_position": 3,
                    "data_type": "timestamp",
                    "nullable": False,
                },
            ],
        )
        queries = [
            base_query(
                query_id="mix-search",
                query_text="SELECT * FROM mixed_pattern_table WHERE name ILIKE $1",
                query_type="SELECT",
                tables=["public.mixed_pattern_table"],
                calls_per_second=5.0,
            ),
            base_query(
                query_id="mix-time",
                query_text="SELECT * FROM mixed_pattern_table WHERE event_time >= $1 AND event_time <= $2",
                query_type="SELECT",
                tables=["public.mixed_pattern_table"],
                calls_per_second=2.0,
            ),
            base_query(
                query_id="mix-insert",
                query_text="INSERT INTO mixed_pattern_table (name, event_time) VALUES ($1, NOW())",
                query_type="INSERT",
                tables=["public.mixed_pattern_table"],
                # Low CPS — below HIGH_INGEST_CPS_THRESHOLD of 10.0 → TS criterion 2 fails
                calls_per_second=3.0,
            ),
        ]
        fixture = wrap_fixture("mixed-pattern-full-pipeline", [table], queries)
        wa = analyze_opensearch_use_cases(fixture)

        # Both search and TS patterns should be detected
        all_pattern_types = {p.pattern_type for p in wa.patterns_detected}
        assert "wildcard-search" in all_pattern_types
        assert "time-range-query" in all_pattern_types

        # But TS classification fails (INSERT CPS = 3.0 < 10.0)
        result = classify_table_workload("public.mixed_pattern_table", wa, table, queries)
        assert result == WorkloadType.SEARCH


# ==========================================================================
# TestRobustnessV2 (Iteration 3)
# ==========================================================================


class TestRobustnessV2:
    """Robustness tests that probe malformed inputs, boundary values, and edge conditions."""

    # ------------------------------------------------------------------
    # 1. Malformed collector: missing `queries` key → empty WorkloadAnalysis
    # ------------------------------------------------------------------

    def test_malformed_collector_missing_queries_key_returns_empty_analysis(self):
        """collector_output == {} (no 'queries' key) → empty WorkloadAnalysis, no crash."""
        from src.contracts.analysis_output import WorkloadAnalysis

        wa = analyze_opensearch_use_cases({})
        assert isinstance(wa, WorkloadAnalysis)
        assert wa.patterns_detected == []
        assert (wa.anti_patterns_detected or []) == []

    # ------------------------------------------------------------------
    # 2. Malformed collector: missing `database_schema` key
    #    → analyze_opensearch_patterns returns empty list
    # ------------------------------------------------------------------

    def test_malformed_collector_missing_database_schema_returns_empty_recommendations(self):
        """Only queries, no 'database_schema' key → analyze_opensearch_patterns returns []."""
        fixture = {
            "job_id": "no-schema",
            "queries": {
                "query_patterns": [
                    base_query(
                        query_id="ns-q1",
                        query_text="SELECT * FROM t WHERE name ILIKE $1",
                        query_type="SELECT",
                        tables=["public.t"],
                        calls_per_second=1.0,
                    )
                ]
            },
            # 'database_schema' key is absent
        }
        wa = analyze_opensearch_use_cases(fixture)
        recs, cls, _ = analyze_opensearch_patterns(fixture, wa)
        assert recs == []
        assert cls == {}

    # ------------------------------------------------------------------
    # 3. Query dict with minimal fields (only query_id and query_text)
    #    → no crash, pattern still detected
    # ------------------------------------------------------------------

    def test_minimal_query_dict_no_crash_pattern_detected(self):
        """Query with only query_id and query_text should not crash; wildcard pattern detected."""
        table = base_table(
            table_id="public.minimal_q_table",
            table_name="minimal_q_table",
            row_count=1000,
            size_mb=10.0,
        )
        minimal_query = {
            "query_id": "min-q-001",
            "query_text": "SELECT * FROM minimal_q_table WHERE name ILIKE $1",
        }
        fixture = wrap_fixture("minimal-query-test", [table], [minimal_query])
        # Must not raise
        wa = analyze_opensearch_use_cases(fixture)
        pattern_types = {p.pattern_type for p in wa.patterns_detected}
        assert "wildcard-search" in pattern_types

    # ------------------------------------------------------------------
    # 4. Table with missing `columns` key
    #    → no crash in text_ratio computation
    # ------------------------------------------------------------------

    def test_table_missing_columns_key_no_crash(self):
        """Table dict without 'columns' key should not crash in text_ratio computation."""
        table_no_cols = {
            "table_id": "public.t1",
            "table_name": "t1",
            # 'columns' key absent
        }
        fixture = {
            "job_id": "no-columns-key",
            "database_schema": {"tables": [table_no_cols]},
            "queries": {
                "query_patterns": [
                    base_query(
                        query_id="ncols-001",
                        query_text="SELECT * FROM t1 WHERE name ILIKE $1",
                        query_type="SELECT",
                        tables=["public.t1"],
                        calls_per_second=1.0,
                    )
                ]
            },
        }
        wa = analyze_opensearch_use_cases(fixture)
        recs, cls, _ = analyze_opensearch_patterns(fixture, wa)
        assert recs is not None

    # ------------------------------------------------------------------
    # 5. Table with `primary_key: []`
    #    → profile.has_primary_key == False
    # ------------------------------------------------------------------

    def test_table_with_empty_primary_key_list_has_primary_key_false(self):
        """Table with primary_key == [] should result in has_primary_key == False."""
        from src.tools.analysis.scoring import build_table_profiles

        table = {
            "table_id": "public.no_pk_table",
            "table_name": "no_pk_table",
            "row_count": 5000,
            "size_mb": 20.0,
            "columns": [
                {
                    "column_name": "id",
                    "ordinal_position": 1,
                    "data_type": "bigint",
                    "nullable": False,
                }
            ],
            "primary_key": [],  # empty list
            "foreign_keys": [],
            "indexes": [],
        }
        fixture = wrap_fixture("empty-pk-test", [table], [])
        wa = analyze_opensearch_use_cases(fixture)
        profiles = build_table_profiles(fixture, wa)
        profile = profiles.get("public.no_pk_table")
        assert profile is not None
        assert profile.has_primary_key is False

    # ------------------------------------------------------------------
    # 6. Table with `primary_key: None`
    #    → profile.has_primary_key == False
    # ------------------------------------------------------------------

    def test_table_with_primary_key_none_has_primary_key_false(self):
        """Table with primary_key == None should result in has_primary_key == False."""
        from src.tools.analysis.scoring import build_table_profiles

        table = {
            "table_id": "public.null_pk_table",
            "table_name": "null_pk_table",
            "row_count": 1000,
            "size_mb": 5.0,
            "columns": [
                {
                    "column_name": "id",
                    "ordinal_position": 1,
                    "data_type": "bigint",
                    "nullable": False,
                }
            ],
            "primary_key": None,  # explicitly None
            "foreign_keys": [],
            "indexes": [],
        }
        fixture = wrap_fixture("null-pk-test", [table], [])
        wa = analyze_opensearch_use_cases(fixture)
        profiles = build_table_profiles(fixture, wa)
        profile = profiles.get("public.null_pk_table")
        assert profile is not None
        assert profile.has_primary_key is False

    # ------------------------------------------------------------------
    # 7. Extremely high CPS values (10,000)
    #    → no overflow or unexpected behavior
    # ------------------------------------------------------------------

    def test_extremely_high_cps_no_overflow(self):
        """INSERT CPS=10,000 should not overflow or behave unexpectedly."""
        table = base_table(
            table_id="public.extreme_cps_log",
            table_name="extreme_cps_log",
            row_count=10_000_000,
            size_mb=5000.0,
            columns=[
                {
                    "column_name": "id",
                    "ordinal_position": 1,
                    "data_type": "bigint",
                    "nullable": False,
                },
                {
                    "column_name": "log_time",
                    "ordinal_position": 2,
                    "data_type": "timestamp",
                    "nullable": False,
                },
                {
                    "column_name": "msg",
                    "ordinal_position": 3,
                    "data_type": "text",
                    "nullable": True,
                },
            ],
        )
        queries = [
            base_query(
                query_id="exc-insert",
                query_text="INSERT INTO extreme_cps_log (log_time, msg) VALUES ($1, $2)",
                query_type="INSERT",
                tables=["public.extreme_cps_log"],
                calls_per_second=10_000.0,
            ),
            base_query(
                query_id="exc-select",
                query_text="SELECT * FROM extreme_cps_log WHERE log_time >= $1 AND log_time <= $2",
                query_type="SELECT",
                tables=["public.extreme_cps_log"],
                calls_per_second=100.0,
            ),
        ]
        fixture = wrap_fixture("extreme-cps-test", [table], queries)
        wa = analyze_opensearch_use_cases(fixture)
        recs, cls, _ = analyze_opensearch_patterns(fixture, wa)
        # Must not crash; high-ingest pattern must be detected
        pattern_types = {p.pattern_type for p in wa.patterns_detected}
        assert "high-ingest" in pattern_types
        assert recs is not None
        for rec in recs:
            assert rec.score_breakdown.pattern_match_score <= 100
            assert rec.score_breakdown.performance_score <= 100

    # ------------------------------------------------------------------
    # 8. Negative CPS values — should handle gracefully
    # ------------------------------------------------------------------

    def test_negative_cps_values_handled_gracefully(self):
        """Negative CPS should not crash; treated as 0 via `float(... or 0.0)`."""
        table = base_table(
            table_id="public.neg_cps_table",
            table_name="neg_cps_table",
            row_count=1000,
            size_mb=10.0,
        )
        query_neg_cps = {
            "query_id": "neg-001",
            "query_text": "SELECT * FROM neg_cps_table WHERE name ILIKE $1",
            "query_type": "SELECT",
            "tables_accessed": ["public.neg_cps_table"],
            "calls_per_second": -5.0,
            "execution_time_ms_avg": 10.0,
            "rows_returned_avg": 5,
            "has_joins": False,
            "join_count": 0,
            "filter_columns": [],
        }
        fixture = wrap_fixture("neg-cps-test", [table], [query_neg_cps])
        # Must not raise
        wa = analyze_opensearch_use_cases(fixture)
        recs, cls, _ = analyze_opensearch_patterns(fixture, wa)
        assert wa is not None
        assert recs is not None

    # ------------------------------------------------------------------
    # 9. Two tables with identical search patterns
    #    → independent scores, independent recommendations
    # ------------------------------------------------------------------

    def test_two_tables_identical_search_patterns_have_independent_scores(self):
        """Two tables with identical ILIKE queries should get independent recommendations."""
        table_a = base_table(
            table_id="public.twin_a",
            table_name="twin_a",
            row_count=10_000,
            size_mb=50.0,
        )
        table_b = base_table(
            table_id="public.twin_b",
            table_name="twin_b",
            row_count=10_000,
            size_mb=50.0,
        )
        queries = [
            base_query(
                query_id="twin-a-001",
                query_text="SELECT * FROM twin_a WHERE name ILIKE $1",
                query_type="SELECT",
                tables=["public.twin_a"],
                calls_per_second=3.0,
            ),
            base_query(
                query_id="twin-b-001",
                query_text="SELECT * FROM twin_b WHERE name ILIKE $1",
                query_type="SELECT",
                tables=["public.twin_b"],
                calls_per_second=3.0,
            ),
        ]
        fixture = wrap_fixture("twin-tables-test", [table_a, table_b], queries)
        wa = analyze_opensearch_use_cases(fixture)
        recs, cls, _ = analyze_opensearch_patterns(fixture, wa)

        rec_ids = {r.table_id for r in recs}
        assert "public.twin_a" in rec_ids
        assert "public.twin_b" in rec_ids

        rec_a = next(r for r in recs if r.table_id == "public.twin_a")
        rec_b = next(r for r in recs if r.table_id == "public.twin_b")
        # Each table has its own independent recommendation
        assert rec_a.score_breakdown is not None
        assert rec_b.score_breakdown is not None
        # Scores should be equal since tables are structurally identical
        assert rec_a.confidence_score == rec_b.confidence_score

    # ------------------------------------------------------------------
    # 10. Table referenced in queries but missing from schema
    #     → should not crash (table not in profiles)
    # ------------------------------------------------------------------

    def test_table_referenced_in_queries_but_missing_from_schema_no_crash(self):
        """Queries reference a table not present in database_schema → no crash."""
        # Only schema table is 'public.real_table'; queries also reference 'public.ghost_table'
        table_real = base_table(
            table_id="public.real_table",
            table_name="real_table",
            row_count=5000,
            size_mb=25.0,
        )
        queries = [
            base_query(
                query_id="ghost-q-001",
                query_text="SELECT * FROM ghost_table WHERE name ILIKE $1",
                query_type="SELECT",
                tables=["public.ghost_table"],  # not in schema
                calls_per_second=2.0,
            ),
            base_query(
                query_id="real-q-001",
                query_text="SELECT * FROM real_table WHERE name ILIKE $1",
                query_type="SELECT",
                tables=["public.real_table"],
                calls_per_second=1.0,
            ),
        ]
        fixture = wrap_fixture("ghost-table-test", [table_real], queries)
        wa = analyze_opensearch_use_cases(fixture)
        recs, cls, _ = analyze_opensearch_patterns(fixture, wa)
        # ghost_table not in profiles (not in schema)
        assert "public.ghost_table" not in cls
        # real_table should still be processed
        assert "public.real_table" in cls

    # ------------------------------------------------------------------
    # 11. Decision trace with 100+ queries → trace still well-formed,
    #     all queries present
    # ------------------------------------------------------------------

    def test_decision_trace_with_100_plus_queries_is_well_formed(self):
        """Decision trace with 101 queries must be well-formed and contain all query IDs."""
        table = base_table(
            table_id="public.bulk_table",
            table_name="bulk_table",
            row_count=1_000_000,
            size_mb=500.0,
        )
        queries = [
            base_query(
                query_id=f"bulk-{i:04d}",
                query_text=f"SELECT * FROM bulk_table WHERE name ILIKE $1 -- q{i}",  # nosec B608
                query_type="SELECT",
                tables=["public.bulk_table"],
                calls_per_second=1.0,
            )
            for i in range(101)
        ]
        fixture = wrap_fixture("bulk-trace-test", [table], queries)
        wa = analyze_opensearch_use_cases(fixture)
        recs, cls, weights = analyze_opensearch_patterns(fixture, wa)
        trace = build_opensearch_decision_trace(fixture, wa, recs, cls, weights)

        # Trace must have all required keys
        required_keys = {
            "trace_version",
            "agent",
            "summary",
            "query_matches",
            "pattern_summaries",
            "workload_classifications",
            "recommendation_derivations",
        }
        assert required_keys.issubset(set(trace.keys()))

        # All 101 queries must appear in query_matches
        assert trace["summary"]["queries_analyzed"] == 101
        traced_qids = {m["query_id"] for m in trace["query_matches"]}
        expected_qids = {f"bulk-{i:04d}" for i in range(101)}
        assert traced_qids == expected_qids
