"""Unit tests for DynamoDB analysis agent — per-pattern isolation + vertical."""

from __future__ import annotations

from src.agents.analysis.dynamodb_analysis_agent import analyze_for_dynamodb
from src.contracts.analysis_input import AnalysisInput, TargetDatabase
from src.contracts.analysis_output import AnalysisOutputContract
from tests.fixtures.dynamodb_pattern_fixtures import (
    get_denormalizable_join_fixture,
    get_frequent_scan_fixture,
    get_key_value_fixture,
    get_metadata_store_fixture,
    get_range_query_fixture,
    get_session_store_fixture,
    get_time_series_fixture,
    get_write_heavy_fixture,
)


def _run(fixture: dict) -> tuple[set[str], set[str], AnalysisOutputContract, dict]:
    """Run the DynamoDB agent and return (pattern_types, anti_pattern_types, result, trace)."""
    from unittest.mock import patch as _patch

    with _patch.dict("os.environ", {"ENABLE_LLM_ADVISOR": "false"}):
        inp = AnalysisInput(
            job_id=fixture["job_id"],
            collector_output=fixture,
            target_database=TargetDatabase.dynamodb,
        )
        result, trace, _mermaid = analyze_for_dynamodb(inp)
    pattern_types = {p.pattern_type for p in result.workload_analysis.patterns_detected}
    ap_types = {
        ap.anti_pattern_type for ap in (result.workload_analysis.anti_patterns_detected or [])
    }
    return pattern_types, ap_types, result, trace


class TestKeyValuePattern:
    def test_detects_key_value_only(self):
        patterns, anti_patterns, result, _ = _run(get_key_value_fixture())
        assert "key-value-lookup" in patterns
        assert not anti_patterns

    def test_high_confidence(self):
        _, _, result, _ = _run(get_key_value_fixture())
        rec = result.table_recommendations[0]
        assert rec.confidence_score >= 60


class TestRangeQueryPattern:
    def test_detects_range_query(self):
        patterns, anti_patterns, _, _ = _run(get_range_query_fixture())
        assert "range-query" in patterns


class TestWriteHeavyPattern:
    def test_detects_write_heavy(self):
        patterns, _, _, _ = _run(get_write_heavy_fixture())
        assert "write-heavy-ingestion" in patterns


class TestTimeSeriesPattern:
    def test_detects_time_series(self):
        patterns, _, _, _ = _run(get_time_series_fixture())
        assert "time-series-event-log" in patterns


class TestMetadataStorePattern:
    def test_detects_metadata_store(self):
        patterns, _, _, _ = _run(get_metadata_store_fixture())
        assert "metadata-config-store" in patterns

    def test_small_table_high_confidence(self):
        _, _, result, _ = _run(get_metadata_store_fixture())
        rec = result.table_recommendations[0]
        assert rec.confidence_score >= 60


class TestSessionStorePattern:
    def test_detects_session_store(self):
        patterns, _, _, _ = _run(get_session_store_fixture())
        assert "session-store" in patterns


class TestDenormalizableJoinPattern:
    def test_detects_denormalizable_join(self):
        patterns, _, _, _ = _run(get_denormalizable_join_fixture())
        # After catalog split, join queries match the first denorm sub-pattern (bounded-parent-child)
        assert "bounded-parent-child" in patterns

    def test_fk_cluster_in_concerns(self):
        _, _, result, _ = _run(get_denormalizable_join_fixture())
        # At least one table should mention FK cluster
        all_concerns = []
        for rec in result.table_recommendations:
            all_concerns.extend(rec.concerns or [])
        assert any("FK cluster" in c or "foreign key" in c for c in all_concerns)


class TestFrequentScanAntiPattern:
    def test_detects_frequent_scan(self):
        _, anti_patterns, _, _ = _run(get_frequent_scan_fixture())
        assert "frequent-full-scan" in anti_patterns


class TestDecisionTrace:
    def test_trace_has_required_fields(self):
        _, _, _, trace = _run(get_key_value_fixture())
        assert trace["trace_version"] == "1.1"
        assert trace["agent"] == "dynamodb-analysis-agent"
        assert "summary" in trace
        assert "query_matches" in trace
        assert "pattern_summaries" in trace
        assert "recommendation_derivations" in trace
        # v1.1 sections
        assert "pk_classifications" in trace
        assert "aggregates" in trace
        assert "gsi_candidates" in trace
        assert "denormalization_opportunities" in trace
        assert "secondary_index_dominance" in trace
        assert "llm_advisor" in trace
        assert trace["llm_advisor"]["status"] == "skipped"

    def test_trace_summary_counts(self):
        _, _, _, trace = _run(get_key_value_fixture())
        summary = trace["summary"]
        assert summary["queries_analyzed"] == 2
        assert summary["queries_matched"] >= 1

    def test_trace_includes_table_groups(self):
        _, _, _, trace = _run(get_denormalizable_join_fixture())
        assert "table_groups" in trace
        # Should have one group with 2 tables
        groups = trace["table_groups"]
        assert len(groups) >= 1
        assert len(groups[0]["tables"]) == 2


class TestGracefulDegradation:
    def test_zero_queries(self):
        """Agent should produce recommendations based on structure only."""
        fixture = get_key_value_fixture()
        fixture["queries"]["query_patterns"] = []
        _, _, result, _ = _run(fixture)
        assert len(result.table_recommendations) == 1
        assert result.table_recommendations[0].confidence_score >= 0

    def test_zero_tables(self):
        """Agent should produce empty recommendations."""
        fixture = get_key_value_fixture()
        fixture["database_schema"]["tables"] = []
        _, _, result, _ = _run(fixture)
        assert len(result.table_recommendations) == 0

    def test_missing_metrics(self):
        """Agent should not crash on missing metrics."""
        fixture = get_key_value_fixture()
        fixture.pop("metrics", None)
        _, _, result, _ = _run(fixture)
        assert result.contract_version == "2.1"


class TestCostEstimation:
    def test_produces_cost_estimate(self):
        _, _, result, _ = _run(get_key_value_fixture())
        assert result.cost_estimate.monthly_cost_usd >= 0
        assert "pricing_mode" in result.cost_estimate.cost_components
        assert result.cost_estimate.cost_components["pricing_mode"] == "on-demand"

    def test_write_heavy_higher_cost(self):
        """Write-heavy workload should cost more than metadata store."""
        _, _, write_result, _ = _run(get_write_heavy_fixture())
        _, _, meta_result, _ = _run(get_metadata_store_fixture())
        assert (
            write_result.cost_estimate.monthly_cost_usd > meta_result.cost_estimate.monthly_cost_usd
        )
