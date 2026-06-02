"""Unit tests for DynamoDB analysis agent LLM seam functions.

Tests verify the three clean seam functions introduced to support the Skill Sync
initiative, plus backward compatibility of the refactored analyze_for_dynamodb.

Seam functions:
- analyze_for_dynamodb_deterministic  — all deterministic logic, no LLM
- prepare_dynamodb_llm_input          — formats LLM request payload
- apply_dynamodb_llm_output           — merges LLM output into contract
"""

from __future__ import annotations

from unittest.mock import patch

from src.agents.analysis.dynamodb_analysis_agent import (
    analyze_for_dynamodb,
    analyze_for_dynamodb_deterministic,
    apply_dynamodb_llm_output,
    prepare_dynamodb_llm_input,
)
from src.contracts.analysis_input import AnalysisInput, TargetDatabase
from src.contracts.analysis_output import AnalysisOutputContract
from src.tools.analysis.dynamodb_analysis_tools import (
    AggregateKeyDesign,
    DenormStrategy,
    LlmAdvisorOutput,
)
from tests.fixtures.dynamodb_pattern_fixtures import get_key_value_fixture

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_input(fixture: dict | None = None) -> AnalysisInput:
    """Build a minimal AnalysisInput from a fixture (defaults to key-value)."""
    f = fixture or get_key_value_fixture()
    return AnalysisInput(
        job_id=f["job_id"],
        collector_output=f,
        target_database=TargetDatabase.dynamodb,
    )


def _make_llm_output() -> LlmAdvisorOutput:
    """Create a valid LlmAdvisorOutput for use in apply tests."""
    return LlmAdvisorOutput(
        aggregate_recommendations=[
            AggregateKeyDesign(
                aggregate_id="agg-users",
                partition_key="user_id",
                sort_key="created_at",
                rationale="User-scoped data ordered by creation",
            )
        ],
        denormalization_strategies=[
            DenormStrategy(
                opportunity_id="denorm-1",
                strategy="embed child items",
                rationale="Low cardinality bounded parent-child",
            )
        ],
    )


# ---------------------------------------------------------------------------
# Test: analyze_for_dynamodb_deterministic
# ---------------------------------------------------------------------------


class TestRunDeterministicReturnsResultsWithoutLlm:
    """analyze_for_dynamodb_deterministic must return valid results and never call LlmAdvisor."""

    def test_returns_valid_output_contract(self):
        """Result[0] is an AnalysisOutputContract with contract_version 2.1."""
        inp = _make_input()
        result, trace, mermaid = analyze_for_dynamodb_deterministic(inp)
        assert isinstance(result, AnalysisOutputContract)
        assert result.contract_version == "2.1"

    def test_returns_decision_trace_dict(self):
        """Result[1] is a dict with the expected trace keys."""
        inp = _make_input()
        _, trace, _ = analyze_for_dynamodb_deterministic(inp)
        assert isinstance(trace, dict)
        assert "trace_version" in trace
        assert "agent" in trace

    def test_returns_mermaid_string(self):
        """Result[2] is a string (Mermaid ER diagram)."""
        inp = _make_input()
        _, _, mermaid = analyze_for_dynamodb_deterministic(inp)
        assert isinstance(mermaid, str)

    def test_no_llm_called(self):
        """LlmAdvisor.advise must never be called by the deterministic path."""
        inp = _make_input()
        with patch("src.agents.analysis.dynamodb_analysis_agent.LlmAdvisor") as mock_advisor_cls:
            analyze_for_dynamodb_deterministic(inp)
        mock_advisor_cls.assert_not_called()

    def test_produces_table_recommendations(self):
        """Deterministic output includes at least one table recommendation."""
        inp = _make_input()
        result, _, _ = analyze_for_dynamodb_deterministic(inp)
        assert len(result.table_recommendations) >= 1

    def test_llm_status_is_skipped_in_trace(self):
        """Decision trace should reflect that LLM was skipped."""
        inp = _make_input()
        _, trace, _ = analyze_for_dynamodb_deterministic(inp)
        assert trace["llm_advisor"]["status"] == "skipped"


# ---------------------------------------------------------------------------
# Test: prepare_dynamodb_llm_input
# ---------------------------------------------------------------------------


class TestPrepareLlmInputReturnsStructuredDict:
    """prepare_dynamodb_llm_input must return a dict with all required keys."""

    def test_returns_dict(self):
        """Result is a plain dict."""
        inp = _make_input()
        contract, _, _ = analyze_for_dynamodb_deterministic(inp)
        payload = prepare_dynamodb_llm_input(contract, inp)
        assert isinstance(payload, dict)

    def test_has_deterministic_results_key(self):
        inp = _make_input()
        contract, _, _ = analyze_for_dynamodb_deterministic(inp)
        payload = prepare_dynamodb_llm_input(contract, inp)
        assert "deterministic_results" in payload

    def test_has_schema_key(self):
        inp = _make_input()
        contract, _, _ = analyze_for_dynamodb_deterministic(inp)
        payload = prepare_dynamodb_llm_input(contract, inp)
        assert "schema" in payload

    def test_has_queries_key(self):
        inp = _make_input()
        contract, _, _ = analyze_for_dynamodb_deterministic(inp)
        payload = prepare_dynamodb_llm_input(contract, inp)
        assert "queries" in payload

    def test_has_aggregates_key(self):
        inp = _make_input()
        contract, _, _ = analyze_for_dynamodb_deterministic(inp)
        payload = prepare_dynamodb_llm_input(contract, inp)
        assert "aggregates" in payload

    def test_has_denorm_opportunities_key(self):
        inp = _make_input()
        contract, _, _ = analyze_for_dynamodb_deterministic(inp)
        payload = prepare_dynamodb_llm_input(contract, inp)
        assert "denorm_opportunities" in payload

    def test_schema_matches_collector_output(self):
        """The schema key should reflect the database_schema from the collector output."""
        inp = _make_input()
        contract, _, _ = analyze_for_dynamodb_deterministic(inp)
        payload = prepare_dynamodb_llm_input(contract, inp)
        expected = inp.collector_output.get("database_schema", {})
        assert payload["schema"] == expected

    def test_queries_matches_collector_output(self):
        """The queries key should reflect the query_patterns from the collector output."""
        inp = _make_input()
        contract, _, _ = analyze_for_dynamodb_deterministic(inp)
        payload = prepare_dynamodb_llm_input(contract, inp)
        expected = inp.collector_output.get("queries", {}).get("query_patterns", [])
        assert payload["queries"] == expected


# ---------------------------------------------------------------------------
# Test: apply_dynamodb_llm_output
# ---------------------------------------------------------------------------


class TestApplyLlmOutputMergesRecommendations:
    """apply_dynamodb_llm_output must merge LLM aggregate_recommendations into the contract."""

    def test_returns_analysis_output_contract(self):
        """Result is an AnalysisOutputContract."""
        inp = _make_input()
        contract, _, _ = analyze_for_dynamodb_deterministic(inp)
        llm_out = _make_llm_output()
        updated = apply_dynamodb_llm_output(contract, llm_out)
        assert isinstance(updated, AnalysisOutputContract)

    def test_aggregate_recommendations_populated(self):
        """aggregate_recommendations is non-empty after merge."""
        inp = _make_input()
        contract, _, _ = analyze_for_dynamodb_deterministic(inp)
        llm_out = _make_llm_output()
        updated = apply_dynamodb_llm_output(contract, llm_out)
        assert updated.aggregate_recommendations is not None
        assert len(updated.aggregate_recommendations) >= 1

    def test_aggregate_id_matches_llm_output(self):
        """The aggregate_id from the LLM output appears in the merged contract."""
        inp = _make_input()
        contract, _, _ = analyze_for_dynamodb_deterministic(inp)
        llm_out = _make_llm_output()
        updated = apply_dynamodb_llm_output(contract, llm_out)
        ids = [r.aggregate_id for r in updated.aggregate_recommendations]
        assert "agg-users" in ids

    def test_none_llm_output_leaves_contract_unchanged(self):
        """Passing None as llm_output must not crash and must return the contract as-is."""
        inp = _make_input()
        contract, _, _ = analyze_for_dynamodb_deterministic(inp)
        original_recs = contract.aggregate_recommendations
        updated = apply_dynamodb_llm_output(contract, None)
        assert isinstance(updated, AnalysisOutputContract)
        assert updated.aggregate_recommendations == original_recs

    def test_existing_contract_fields_preserved(self):
        """Table recommendations and workload analysis must survive the merge."""
        inp = _make_input()
        contract, _, _ = analyze_for_dynamodb_deterministic(inp)
        llm_out = _make_llm_output()
        updated = apply_dynamodb_llm_output(contract, llm_out)
        assert updated.table_recommendations == contract.table_recommendations
        assert updated.workload_analysis == contract.workload_analysis


# ---------------------------------------------------------------------------
# Test: analyze_for_dynamodb backward compatibility
# ---------------------------------------------------------------------------


class TestAnalyzeForDynamodbBackwardCompatible:
    """analyze_for_dynamodb called with no llm_mode arg must behave identically to before."""

    def test_returns_three_tuple(self):
        """Return value is a 3-tuple regardless of llm_mode."""
        with patch.dict("os.environ", {"ENABLE_LLM_ADVISOR": "false"}):
            inp = _make_input()
            result = analyze_for_dynamodb(inp)
        assert isinstance(result, tuple)
        assert len(result) == 3

    def test_first_element_is_output_contract(self):
        with patch.dict("os.environ", {"ENABLE_LLM_ADVISOR": "false"}):
            inp = _make_input()
            contract, _, _ = analyze_for_dynamodb(inp)
        assert isinstance(contract, AnalysisOutputContract)
        assert contract.contract_version == "2.1"

    def test_second_element_is_decision_trace(self):
        with patch.dict("os.environ", {"ENABLE_LLM_ADVISOR": "false"}):
            inp = _make_input()
            _, trace, _ = analyze_for_dynamodb(inp)
        assert isinstance(trace, dict)
        assert "trace_version" in trace

    def test_third_element_is_mermaid_string(self):
        with patch.dict("os.environ", {"ENABLE_LLM_ADVISOR": "false"}):
            inp = _make_input()
            _, _, mermaid = analyze_for_dynamodb(inp)
        assert isinstance(mermaid, str)

    def test_default_llm_mode_is_bedrock(self):
        """Calling without llm_mode defaults to 'bedrock' behavior (LlmAdvisor consulted)."""
        import inspect

        sig = inspect.signature(analyze_for_dynamodb)
        assert "llm_mode" in sig.parameters
        assert sig.parameters["llm_mode"].default == "bedrock"

    def test_llm_mode_none_skips_llm(self):
        """llm_mode='none' must not call LlmAdvisor at all."""
        inp = _make_input()
        with patch("src.agents.analysis.dynamodb_analysis_agent.LlmAdvisor") as mock_advisor_cls:
            contract, trace, mermaid = analyze_for_dynamodb(inp, llm_mode="none")
        mock_advisor_cls.assert_not_called()
        assert isinstance(contract, AnalysisOutputContract)

    def test_llm_mode_none_trace_status_skipped(self):
        """With llm_mode='none', decision trace shows LLM was skipped."""
        inp = _make_input()
        _, trace, _ = analyze_for_dynamodb(inp, llm_mode="none")
        assert trace["llm_advisor"]["status"] == "skipped"

    def test_llm_mode_external_returns_valid_contract(self):
        """llm_mode='external' must return a valid contract without calling Bedrock."""
        inp = _make_input()
        with patch("src.agents.analysis.dynamodb_analysis_agent.LlmAdvisor") as mock_advisor_cls:
            contract, trace, mermaid = analyze_for_dynamodb(inp, llm_mode="external")
        mock_advisor_cls.assert_not_called()
        assert isinstance(contract, AnalysisOutputContract)
