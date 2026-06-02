"""
Property 11: Output contract backward compatibility

For any valid AnalysisOutputContract produced by the pre-enhancement agent at
version "2.0", that data shall still validate successfully against the
post-enhancement AnalysisOutputContract schema at version "2.1", because the
only change is the addition of one optional field (aggregate_recommendations)
which defaults to None. Conversely, any new v2.1 output with
aggregate_recommendations present shall also validate.

Feature: enhanced-dynamodb-analysis, Property 11: Output contract backward compatibility
Validates: Requirements 7.5
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from src.contracts.analysis_output import (
    AggregateRecommendation,
    AnalysisOutputContract,
    MigrationComplexity,
)

# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

_score = st.integers(min_value=0, max_value=100)
_table_id = st.from_regex(r"[a-z]{2,8}\.[a-z_]{2,12}", fullmatch=True)


def _score_breakdown_strategy() -> st.SearchStrategy[dict]:
    return st.fixed_dictionaries(
        {
            "pattern_match_score": _score,
            "complexity_score": _score,
            "performance_score": _score,
            "cost_score": _score,
        }
    )


def _table_recommendation_strategy() -> st.SearchStrategy[dict]:
    return st.fixed_dictionaries(
        {
            "table_id": _table_id,
            "confidence_score": _score,
            "score_breakdown": _score_breakdown_strategy(),
        }
    )


def _agent_metadata_strategy() -> st.SearchStrategy[dict]:
    return st.fixed_dictionaries(
        {
            "agent_name": st.just("dynamodb-analysis-agent"),
            "agent_version": st.just("1.0.0"),
            "target_database": st.just("dynamodb"),
            "analysis_timestamp": st.just("2026-01-15T10:00:00Z"),
        }
    )


def _cost_estimate_strategy() -> st.SearchStrategy[dict]:
    return st.fixed_dictionaries(
        {
            "monthly_cost_usd": st.floats(min_value=0.0, max_value=10000.0),
            "cost_components": st.just({"pricing_mode": "on-demand"}),
        }
    )


def _pattern_strategy() -> st.SearchStrategy[dict]:
    return st.fixed_dictionaries(
        {
            "pattern_id": st.from_regex(r"dynamodb-[a-z\-]+-\d{3}", fullmatch=True),
            "pattern_type": st.sampled_from(
                ["key-value-lookup", "range-query", "write-heavy-ingestion"]
            ),
            "confidence": st.sampled_from(["HIGH", "MEDIUM", "LOW"]),
        }
    )


def _workload_analysis_strategy() -> st.SearchStrategy[dict]:
    return st.fixed_dictionaries(
        {
            "patterns_detected": st.lists(_pattern_strategy(), min_size=0, max_size=3),
        }
    )


def _v20_output_strategy() -> st.SearchStrategy[dict]:
    """Generate valid v2.0 output dicts (no aggregate_recommendations field)."""
    return st.fixed_dictionaries(
        {
            "contract_version": st.just("2.0"),
            "agent_metadata": _agent_metadata_strategy(),
            "table_recommendations": st.lists(
                _table_recommendation_strategy(), min_size=1, max_size=5
            ),
            "workload_analysis": _workload_analysis_strategy(),
            "cost_estimate": _cost_estimate_strategy(),
        }
    )


def _aggregate_recommendation_strategy() -> st.SearchStrategy[dict]:
    return st.fixed_dictionaries(
        {
            "aggregate_id": st.from_regex(r"agg-[a-z]{3,8}", fullmatch=True),
            "root_table": _table_id,
            "member_tables": st.lists(_table_id, min_size=1, max_size=4),
            "co_access_confidence": _score,
            "combined_migration_complexity": st.sampled_from(["LOW", "MEDIUM", "HIGH"]),
        }
    )


def _v21_output_with_aggregates_strategy() -> st.SearchStrategy[dict]:
    """Generate valid v2.1 output dicts WITH aggregate_recommendations."""
    return st.fixed_dictionaries(
        {
            "contract_version": st.just("2.1"),
            "agent_metadata": _agent_metadata_strategy(),
            "table_recommendations": st.lists(
                _table_recommendation_strategy(), min_size=1, max_size=5
            ),
            "workload_analysis": _workload_analysis_strategy(),
            "cost_estimate": _cost_estimate_strategy(),
            "aggregate_recommendations": st.lists(
                _aggregate_recommendation_strategy(), min_size=1, max_size=4
            ),
        }
    )


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------


class TestOutputContractBackwardCompatibility:
    """Property 11: Output contract backward compatibility."""

    @given(data=_v20_output_strategy())
    def test_v20_output_validates_against_v21_schema(self, data: dict):
        """A pre-enhancement v2.0 output (without aggregate_recommendations)
        shall validate against the current AnalysisOutputContract schema.
        The aggregate_recommendations field defaults to None."""
        contract = AnalysisOutputContract.model_validate(data)

        assert contract is not None
        assert contract.contract_version == "2.0"
        assert contract.aggregate_recommendations is None
        assert len(contract.table_recommendations) >= 1

    @given(data=_v21_output_with_aggregates_strategy())
    def test_v21_output_with_aggregates_validates(self, data: dict):
        """A v2.1 output with aggregate_recommendations present shall validate."""
        contract = AnalysisOutputContract.model_validate(data)

        assert contract is not None
        assert contract.contract_version == "2.1"
        assert contract.aggregate_recommendations is not None
        assert len(contract.aggregate_recommendations) >= 1
        for agg in contract.aggregate_recommendations:
            assert isinstance(agg, AggregateRecommendation)
            assert 0 <= agg.co_access_confidence <= 100
            assert agg.combined_migration_complexity in {e.value for e in MigrationComplexity}

    @given(data=_v20_output_strategy())
    def test_v20_roundtrip_preserves_none_aggregates(self, data: dict):
        """Serializing and deserializing a v2.0 output preserves
        aggregate_recommendations as None (not omitted)."""
        contract = AnalysisOutputContract.model_validate(data)
        serialized = contract.model_dump()
        restored = AnalysisOutputContract.model_validate(serialized)

        assert restored.aggregate_recommendations is None
        assert restored.contract_version == contract.contract_version
        assert len(restored.table_recommendations) == len(contract.table_recommendations)

    @given(data=_v21_output_with_aggregates_strategy())
    def test_v21_roundtrip_preserves_aggregates(self, data: dict):
        """Serializing and deserializing a v2.1 output preserves
        aggregate_recommendations content."""
        contract = AnalysisOutputContract.model_validate(data)
        serialized = contract.model_dump()
        restored = AnalysisOutputContract.model_validate(serialized)

        assert restored.aggregate_recommendations is not None
        assert len(restored.aggregate_recommendations) == len(contract.aggregate_recommendations)
        for orig, rest in zip(
            contract.aggregate_recommendations, restored.aggregate_recommendations, strict=False
        ):
            assert orig.aggregate_id == rest.aggregate_id
            assert orig.root_table == rest.root_table
            assert orig.co_access_confidence == rest.co_access_confidence

    def test_v20_dict_without_aggregate_field_validates(self):
        """A v2.0 dict that completely omits aggregate_recommendations
        (as a real pre-enhancement agent would produce) validates fine."""
        data = {
            "contract_version": "2.0",
            "agent_metadata": {
                "agent_name": "dynamodb-analysis-agent",
                "agent_version": "1.0.0",
                "target_database": "dynamodb",
                "analysis_timestamp": "2026-01-15T10:00:00Z",
            },
            "table_recommendations": [
                {
                    "table_id": "app.users",
                    "confidence_score": 85,
                    "score_breakdown": {
                        "pattern_match_score": 90,
                        "complexity_score": 80,
                        "performance_score": 85,
                        "cost_score": 75,
                    },
                }
            ],
            "workload_analysis": {"patterns_detected": []},
            "cost_estimate": {
                "monthly_cost_usd": 25.0,
                "cost_components": {"pricing_mode": "on-demand"},
            },
        }
        contract = AnalysisOutputContract.model_validate(data)
        assert contract.aggregate_recommendations is None
        assert contract.contract_version == "2.0"
