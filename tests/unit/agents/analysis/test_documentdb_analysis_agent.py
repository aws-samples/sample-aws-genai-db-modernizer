"""Unit tests for DocumentDB analysis agent — per-pattern isolation + vertical."""

from __future__ import annotations

from src.agents.analysis.documentdb_analysis_agent import analyze_for_documentdb
from src.contracts.analysis_input import AnalysisInput, TargetDatabase
from src.contracts.analysis_output import AnalysisOutputContract
from tests.fixtures.documentdb_pattern_fixtures import (
    get_aggregation_pipeline_fixture,
    get_content_management_fixture,
    get_cross_collection_joins_fixture,
    get_extended_reference_fixture,
    get_flexible_schema_fixture,
    get_graph_hierarchy_fixture,
    get_nested_document_fixture,
    get_polymorphic_data_fixture,
    get_product_catalog_fixture,
    get_write_time_aggregation_fixture,
)


def _run(fixture: dict) -> tuple[set[str], set[str], AnalysisOutputContract, dict]:
    from unittest.mock import patch as _patch

    with _patch.dict("os.environ", {"ENABLE_LLM_ADVISOR": "false"}):
        inp = AnalysisInput(
            job_id=fixture["job_id"],
            collector_output=fixture,
            target_database=TargetDatabase.documentdb,
        )
        result, trace, _mermaid = analyze_for_documentdb(inp)
    pattern_types = {p.pattern_type for p in result.workload_analysis.patterns_detected}
    ap_types = {
        ap.anti_pattern_type for ap in (result.workload_analysis.anti_patterns_detected or [])
    }
    return pattern_types, ap_types, result, trace


class TestContentManagementPattern:
    def test_detects_content_management(self):
        patterns, _, _, _ = _run(get_content_management_fixture())
        assert "content-management" in patterns

    def test_confidence(self):
        _, _, result, _ = _run(get_content_management_fixture())
        assert result.table_recommendations[0].confidence_score >= 40


class TestProductCatalogPattern:
    def test_detects_product_catalog(self):
        patterns, _, _, _ = _run(get_product_catalog_fixture())
        assert "product-catalog" in patterns


class TestPolymorphicDataPattern:
    def test_detects_polymorphic(self):
        patterns, _, _, _ = _run(get_polymorphic_data_fixture())
        assert "polymorphic-data" in patterns


class TestNestedDocumentPattern:
    def test_detects_nested_document(self):
        patterns, _, _, _ = _run(get_nested_document_fixture())
        assert "nested-document" in patterns

    def test_fk_in_rationale(self):
        _, _, result, _ = _run(get_nested_document_fixture())
        all_rationales = [r.rationale for r in result.table_recommendations]
        assert any("FK" in r or "embedding" in r.lower() for r in all_rationales)


class TestAggregationPipelinePattern:
    def test_detects_aggregation(self):
        patterns, _, _, _ = _run(get_aggregation_pipeline_fixture())
        assert "aggregation-pipeline" in patterns


class TestFlexibleSchemaPattern:
    def test_detects_flexible_schema(self):
        patterns, _, _, _ = _run(get_flexible_schema_fixture())
        assert "flexible-schema" in patterns


class TestExtendedReferencePattern:
    def test_detects_extended_reference(self):
        patterns, _, _, _ = _run(get_extended_reference_fixture())
        assert "extended-reference" in patterns


class TestWriteTimeAggregationPattern:
    def test_detects_write_time_aggregation(self):
        patterns, _, _, _ = _run(get_write_time_aggregation_fixture())
        assert "write-time-aggregation" in patterns


class TestCrossCollectionJoinsAntiPattern:
    def test_detects_cross_joins(self):
        _, anti_patterns, _, _ = _run(get_cross_collection_joins_fixture())
        assert "heavy-cross-collection-joins" in anti_patterns


class TestGraphHierarchyAntiPattern:
    def test_detects_graph_hierarchy(self):
        _, anti_patterns, _, _ = _run(get_graph_hierarchy_fixture())
        assert "graph-traversal-hierarchy" in anti_patterns


class TestDecisionTrace:
    def test_trace_has_required_fields(self):
        _, _, _, trace = _run(get_content_management_fixture())
        assert trace["trace_version"] == "1.0"
        assert trace["agent"] == "documentdb-analysis-agent"
        assert "summary" in trace
        assert "query_matches" in trace
        assert "pattern_summaries" in trace
        assert "recommendation_derivations" in trace
        assert "embedding_candidates" in trace
        assert "polymorphic_tables" in trace
        assert "denormalization_strategies" in trace
        assert "llm_advisor" in trace
        assert trace["llm_advisor"]["status"] == "skipped"

    def test_trace_has_compatibility_section(self):
        _, _, _, trace = _run(get_graph_hierarchy_fixture())
        assert "documentdb_compatibility" in trace
        assert trace["documentdb_compatibility"]["target_version"] == "8.0"
        unsupported = trace["documentdb_compatibility"]["unsupported_features_detected"]
        assert any("graphLookup" in f for f in unsupported)

    def test_trace_summary_counts(self):
        _, _, _, trace = _run(get_content_management_fixture())
        summary = trace["summary"]
        assert summary["queries_analyzed"] == 1
        assert summary["queries_matched"] >= 1


class TestGracefulDegradation:
    def test_zero_queries(self):
        fixture = get_content_management_fixture()
        fixture["queries"]["query_patterns"] = []
        _, _, result, _ = _run(fixture)
        assert len(result.table_recommendations) == 1
        assert result.table_recommendations[0].confidence_score >= 0

    def test_zero_tables(self):
        fixture = get_content_management_fixture()
        fixture["database_schema"]["tables"] = []
        _, _, result, _ = _run(fixture)
        assert len(result.table_recommendations) == 0

    def test_missing_metrics(self):
        fixture = get_content_management_fixture()
        fixture.pop("metrics", None)
        _, _, result, _ = _run(fixture)
        assert result.contract_version == "2.1"


class TestCostEstimation:
    def test_produces_cost_estimate(self):
        _, _, result, _ = _run(get_content_management_fixture())
        assert result.cost_estimate.monthly_cost_usd > 0
        assert "instance_type" in result.cost_estimate.cost_components
        assert result.cost_estimate.cost_components["instance_type"] == "db.r6g.large"

    def test_includes_instance_cost(self):
        _, _, result, _ = _run(get_content_management_fixture())
        assert result.cost_estimate.cost_components["instance"] > 200  # db.r6g.large ~$254/mo


class TestEmbeddingCandidates:
    def test_nested_doc_has_embedding_candidates(self):
        _, _, _, trace = _run(get_nested_document_fixture())
        assert len(trace["embedding_candidates"]) >= 1
        cand = trace["embedding_candidates"][0]
        assert "parent_table" in cand
        assert "child_table" in cand
        assert "relationship_type" in cand

    def test_fallback_strategies_when_llm_disabled(self):
        _, _, _, trace = _run(get_nested_document_fixture())
        assert trace["llm_advisor"]["status"] == "skipped"
        assert len(trace["denormalization_strategies"]) >= 1


class TestMermaidDiagram:
    def test_produces_mermaid(self):
        from unittest.mock import patch as _patch

        with _patch.dict("os.environ", {"ENABLE_LLM_ADVISOR": "false"}):
            inp = AnalysisInput(
                job_id="test",
                collector_output=get_nested_document_fixture(),
                target_database=TargetDatabase.documentdb,
            )
            _, _, mermaid = analyze_for_documentdb(inp)
        assert mermaid.startswith("erDiagram")
        assert "embeds" in mermaid or "references" in mermaid
