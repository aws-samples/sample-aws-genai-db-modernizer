"""Unit tests for DocumentDB analysis agent LLM seam functions.

Tests verify the three clean seam functions introduced to support the Skill Sync
initiative, plus backward compatibility of the refactored analyze_for_documentdb.

Seam functions:
- analyze_for_documentdb_deterministic  — all deterministic logic, uses fallback embedding
- prepare_documentdb_llm_input          — formats LLM request payload
- apply_documentdb_llm_output           — merges LLM denorm strategies into trace
"""

from __future__ import annotations

import inspect
from unittest.mock import patch

from src.agents.analysis.documentdb_analysis_agent import (
    analyze_for_documentdb,
    analyze_for_documentdb_deterministic,
    apply_documentdb_llm_output,
    prepare_documentdb_llm_input,
)
from src.contracts.analysis_input import AnalysisInput, TargetDatabase
from src.contracts.analysis_output import AnalysisOutputContract
from src.tools.analysis.documentdb_analysis_tools import (
    DenormalizationStrategy,
    LlmDocumentDBOutput,
)
from tests.fixtures.documentdb_pattern_fixtures import get_nested_document_fixture

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_input(fixture: dict | None = None) -> AnalysisInput:
    """Build a minimal AnalysisInput from a fixture (defaults to nested-document)."""
    f = fixture or get_nested_document_fixture()
    return AnalysisInput(
        job_id=f["job_id"],
        collector_output=f,
        target_database=TargetDatabase.documentdb,
    )


def _make_llm_output() -> LlmDocumentDBOutput:
    """Create a valid LlmDocumentDBOutput for use in apply tests."""
    return LlmDocumentDBOutput(
        denormalization_strategies=[
            DenormalizationStrategy(
                parent_table="orders",
                child_table="order_items",
                strategy="embed",
                rationale="High co-access, bounded children",
                trade_offs="Embed avoids $lookup; update all items when parent changes",
                risk_level="low",
            )
        ],
        collection_design_notes="Embed order_items into orders for co-located reads.",
    )


# ---------------------------------------------------------------------------
# Test: analyze_for_documentdb_deterministic
# ---------------------------------------------------------------------------


class TestRunDeterministicUsesFallbackEmbedding:
    """analyze_for_documentdb_deterministic must use fallback strategies and never call LLM."""

    def test_returns_valid_output_contract(self):
        """Result[0] is an AnalysisOutputContract with contract_version 2.1."""
        inp = _make_input()
        result, trace, mermaid = analyze_for_documentdb_deterministic(inp)
        assert isinstance(result, AnalysisOutputContract)
        assert result.contract_version == "2.1"

    def test_returns_decision_trace_dict(self):
        """Result[1] is a dict with the expected trace keys."""
        inp = _make_input()
        _, trace, _ = analyze_for_documentdb_deterministic(inp)
        assert isinstance(trace, dict)
        assert "trace_version" in trace
        assert "agent" in trace

    def test_returns_mermaid_string(self):
        """Result[2] is a non-empty string (Mermaid ER diagram)."""
        inp = _make_input()
        _, _, mermaid = analyze_for_documentdb_deterministic(inp)
        assert isinstance(mermaid, str)

    def test_no_llm_called(self):
        """LlmDocumentDBAdvisor must never be called by the deterministic path."""
        inp = _make_input()
        with patch(
            "src.agents.analysis.documentdb_analysis_agent.LlmDocumentDBAdvisor"
        ) as mock_advisor_cls:
            analyze_for_documentdb_deterministic(inp)
        mock_advisor_cls.assert_not_called()

    def test_produces_table_recommendations(self):
        """Deterministic output includes at least one table recommendation."""
        inp = _make_input()
        result, _, _ = analyze_for_documentdb_deterministic(inp)
        assert len(result.table_recommendations) >= 1

    def test_fallback_strategies_applied(self):
        """When embedding candidates exist, fallback denorm strategies must be present in trace."""
        inp = _make_input()
        _, trace, _ = analyze_for_documentdb_deterministic(inp)
        # The nested-document fixture has FK relationships → embedding_candidates > 0
        if trace["embedding_candidates"]:
            assert len(trace["denormalization_strategies"]) >= 1
            strategy = trace["denormalization_strategies"][0]
            # Fallback strategies include the "Fallback rule:" rationale
            assert "Fallback rule" in strategy["rationale"]

    def test_llm_status_is_skipped_in_trace(self):
        """Decision trace should reflect that LLM was skipped."""
        inp = _make_input()
        _, trace, _ = analyze_for_documentdb_deterministic(inp)
        assert trace["llm_advisor"]["status"] == "skipped"

    def test_trace_has_embedding_candidates_key(self):
        """Trace must always include embedding_candidates list."""
        inp = _make_input()
        _, trace, _ = analyze_for_documentdb_deterministic(inp)
        assert "embedding_candidates" in trace
        assert isinstance(trace["embedding_candidates"], list)

    def test_trace_has_denormalization_strategies_key(self):
        """Trace must always include denormalization_strategies list."""
        inp = _make_input()
        _, trace, _ = analyze_for_documentdb_deterministic(inp)
        assert "denormalization_strategies" in trace
        assert isinstance(trace["denormalization_strategies"], list)


# ---------------------------------------------------------------------------
# Test: prepare_documentdb_llm_input
# ---------------------------------------------------------------------------


class TestPrepareLlmInputIncludesEmbeddingCandidates:
    """prepare_documentdb_llm_input must return a dict with all required keys."""

    def test_returns_dict(self):
        """Result is a plain dict."""
        inp = _make_input()
        contract, _, _ = analyze_for_documentdb_deterministic(inp)
        payload = prepare_documentdb_llm_input(contract, inp)
        assert isinstance(payload, dict)

    def test_has_deterministic_results_key(self):
        inp = _make_input()
        contract, _, _ = analyze_for_documentdb_deterministic(inp)
        payload = prepare_documentdb_llm_input(contract, inp)
        assert "deterministic_results" in payload

    def test_has_schema_key(self):
        inp = _make_input()
        contract, _, _ = analyze_for_documentdb_deterministic(inp)
        payload = prepare_documentdb_llm_input(contract, inp)
        assert "schema" in payload

    def test_has_queries_key(self):
        inp = _make_input()
        contract, _, _ = analyze_for_documentdb_deterministic(inp)
        payload = prepare_documentdb_llm_input(contract, inp)
        assert "queries" in payload

    def test_has_embedding_candidates_key(self):
        """embedding_candidates is present (this is the DocumentDB-specific key)."""
        inp = _make_input()
        contract, _, _ = analyze_for_documentdb_deterministic(inp)
        payload = prepare_documentdb_llm_input(contract, inp)
        assert "embedding_candidates" in payload

    def test_embedding_candidates_is_list(self):
        inp = _make_input()
        contract, _, _ = analyze_for_documentdb_deterministic(inp)
        payload = prepare_documentdb_llm_input(contract, inp)
        assert isinstance(payload["embedding_candidates"], list)

    def test_schema_matches_collector_output(self):
        """The schema key should reflect the database_schema from the collector output."""
        inp = _make_input()
        contract, _, _ = analyze_for_documentdb_deterministic(inp)
        payload = prepare_documentdb_llm_input(contract, inp)
        expected = inp.collector_output.get("database_schema", {})
        assert payload["schema"] == expected

    def test_queries_matches_collector_output(self):
        """The queries key should reflect the query_patterns from the collector output."""
        inp = _make_input()
        contract, _, _ = analyze_for_documentdb_deterministic(inp)
        payload = prepare_documentdb_llm_input(contract, inp)
        expected = inp.collector_output.get("queries", {}).get("query_patterns", [])
        assert payload["queries"] == expected

    def test_deterministic_results_has_patterns_and_anti_patterns(self):
        """deterministic_results must contain 'patterns' and 'anti_patterns' sub-keys."""
        inp = _make_input()
        contract, _, _ = analyze_for_documentdb_deterministic(inp)
        payload = prepare_documentdb_llm_input(contract, inp)
        dr = payload["deterministic_results"]
        assert "patterns" in dr
        assert "anti_patterns" in dr


# ---------------------------------------------------------------------------
# Test: apply_documentdb_llm_output
# ---------------------------------------------------------------------------


class TestApplyLlmOutputUpdatesTrace:
    """apply_documentdb_llm_output must merge denorm strategies into the trace dict."""

    def test_returns_two_tuple(self):
        """Result is a 2-tuple of (AnalysisOutputContract, dict)."""
        inp = _make_input()
        contract, trace, _ = analyze_for_documentdb_deterministic(inp)
        llm_out = _make_llm_output()
        result = apply_documentdb_llm_output(contract, llm_out, trace)
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_first_element_is_output_contract(self):
        inp = _make_input()
        contract, trace, _ = analyze_for_documentdb_deterministic(inp)
        llm_out = _make_llm_output()
        updated_contract, _ = apply_documentdb_llm_output(contract, llm_out, trace)
        assert isinstance(updated_contract, AnalysisOutputContract)

    def test_second_element_is_trace_dict(self):
        inp = _make_input()
        contract, trace, _ = analyze_for_documentdb_deterministic(inp)
        llm_out = _make_llm_output()
        _, updated_trace = apply_documentdb_llm_output(contract, llm_out, trace)
        assert isinstance(updated_trace, dict)

    def test_denorm_strategies_merged_into_trace(self):
        """LLM denorm strategies must appear in trace['denormalization_strategies']."""
        inp = _make_input()
        contract, trace, _ = analyze_for_documentdb_deterministic(inp)
        llm_out = _make_llm_output()
        _, updated_trace = apply_documentdb_llm_output(contract, llm_out, trace)
        strategies = updated_trace["denormalization_strategies"]
        assert len(strategies) >= 1
        # The LLM output strategy should be present
        parent_tables = [s["parent_table"] for s in strategies]
        assert "orders" in parent_tables

    def test_denorm_strategy_fields_present(self):
        """Each merged strategy must include parent_table, child_table, strategy fields."""
        inp = _make_input()
        contract, trace, _ = analyze_for_documentdb_deterministic(inp)
        llm_out = _make_llm_output()
        _, updated_trace = apply_documentdb_llm_output(contract, llm_out, trace)
        for s in updated_trace["denormalization_strategies"]:
            assert "parent_table" in s
            assert "child_table" in s
            assert "strategy" in s

    def test_none_llm_output_leaves_trace_unchanged(self):
        """Passing None as llm_output must not crash and must return the trace as-is."""
        inp = _make_input()
        contract, trace, _ = analyze_for_documentdb_deterministic(inp)
        original_strategies = list(trace["denormalization_strategies"])
        updated_contract, updated_trace = apply_documentdb_llm_output(contract, None, trace)
        assert isinstance(updated_contract, AnalysisOutputContract)
        assert updated_trace["denormalization_strategies"] == original_strategies

    def test_contract_fields_preserved_after_apply(self):
        """Table recommendations and workload analysis must survive the merge."""
        inp = _make_input()
        contract, trace, _ = analyze_for_documentdb_deterministic(inp)
        llm_out = _make_llm_output()
        updated_contract, _ = apply_documentdb_llm_output(contract, llm_out, trace)
        assert updated_contract.table_recommendations == contract.table_recommendations
        assert updated_contract.workload_analysis == contract.workload_analysis


# ---------------------------------------------------------------------------
# Test: analyze_for_documentdb backward compatibility
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    """analyze_for_documentdb called with no llm_mode arg must behave identically to before."""

    def test_returns_three_tuple(self):
        """Return value is a 3-tuple regardless of llm_mode."""
        with patch.dict("os.environ", {"ENABLE_LLM_ADVISOR": "false"}):
            inp = _make_input()
            result = analyze_for_documentdb(inp)
        assert isinstance(result, tuple)
        assert len(result) == 3

    def test_first_element_is_output_contract(self):
        with patch.dict("os.environ", {"ENABLE_LLM_ADVISOR": "false"}):
            inp = _make_input()
            contract, _, _ = analyze_for_documentdb(inp)
        assert isinstance(contract, AnalysisOutputContract)
        assert contract.contract_version == "2.1"

    def test_second_element_is_decision_trace(self):
        with patch.dict("os.environ", {"ENABLE_LLM_ADVISOR": "false"}):
            inp = _make_input()
            _, trace, _ = analyze_for_documentdb(inp)
        assert isinstance(trace, dict)
        assert "trace_version" in trace

    def test_third_element_is_mermaid_string(self):
        with patch.dict("os.environ", {"ENABLE_LLM_ADVISOR": "false"}):
            inp = _make_input()
            _, _, mermaid = analyze_for_documentdb(inp)
        assert isinstance(mermaid, str)

    def test_default_llm_mode_is_bedrock(self):
        """Calling without llm_mode defaults to 'bedrock' behavior."""
        sig = inspect.signature(analyze_for_documentdb)
        assert "llm_mode" in sig.parameters
        assert sig.parameters["llm_mode"].default == "bedrock"

    def test_llm_mode_none_skips_llm(self):
        """llm_mode='none' must not call LlmDocumentDBAdvisor at all."""
        inp = _make_input()
        with patch(
            "src.agents.analysis.documentdb_analysis_agent.LlmDocumentDBAdvisor"
        ) as mock_advisor_cls:
            contract, trace, mermaid = analyze_for_documentdb(inp, llm_mode="none")
        mock_advisor_cls.assert_not_called()
        assert isinstance(contract, AnalysisOutputContract)

    def test_llm_mode_none_trace_status_skipped(self):
        """With llm_mode='none', decision trace shows LLM was skipped."""
        inp = _make_input()
        _, trace, _ = analyze_for_documentdb(inp, llm_mode="none")
        assert trace["llm_advisor"]["status"] == "skipped"

    def test_llm_mode_external_returns_valid_contract(self):
        """llm_mode='external' must return a valid contract without calling Bedrock."""
        inp = _make_input()
        with patch(
            "src.agents.analysis.documentdb_analysis_agent.LlmDocumentDBAdvisor"
        ) as mock_advisor_cls:
            contract, trace, mermaid = analyze_for_documentdb(inp, llm_mode="external")
        mock_advisor_cls.assert_not_called()
        assert isinstance(contract, AnalysisOutputContract)

    def test_llm_mode_external_sets_awaiting_external_status(self):
        """llm_mode='external' must mark the trace as awaiting_external."""
        inp = _make_input()
        _, trace, _ = analyze_for_documentdb(inp, llm_mode="external")
        assert trace["llm_advisor"]["status"] == "awaiting_external"
