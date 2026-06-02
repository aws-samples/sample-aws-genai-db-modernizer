"""Tests for schema design input contract and projection function."""


import pytest
from pydantic import ValidationError

from src.contracts.schema_design_input import (
    AgentAnalysisInput,
    AgentCollectorInput,
    AgentContextInput,
    project_schema_design_input,
)

# Import fixtures
from tests.fixtures.schema_design_fixtures import *  # noqa: F401, F403


class TestAgentInputValidation:
    """Test that fixture data validates against the input contracts."""

    def test_sample_collector_validates(self, sample_collector_input):
        collector = AgentCollectorInput.model_validate(sample_collector_input)
        assert len(collector.tables) == 2
        assert len(collector.queries.query_patterns) == 3

    def test_sample_analysis_validates(self, sample_analysis_input):
        analysis = AgentAnalysisInput.model_validate(sample_analysis_input)
        assert len(analysis.patterns_detected) == 2
        assert len(analysis.table_recommendations) == 2

    def test_combined_input_validates(self, sample_collector_input, sample_analysis_input):
        AgentCollectorInput.model_validate(sample_collector_input)
        AgentAnalysisInput.model_validate(sample_analysis_input)
        AgentContextInput.model_validate({})

    def test_context_defaults(self):
        ctx = AgentContextInput()
        assert ctx.growth_multiplier == 10.0
        assert ctx.peak_to_avg_ratio == 3.0
        assert ctx.default_read_slo_ms == 10.0
        assert ctx.default_write_slo_ms == 20.0
        assert ctx.slo_overrides == {}

    def test_context_overrides(self):
        ctx = AgentContextInput(growth_multiplier=5.0, peak_to_avg_ratio=2.0)
        assert ctx.growth_multiplier == 5.0
        assert ctx.peak_to_avg_ratio == 2.0

    def test_collector_missing_job_id_fails(self):
        with pytest.raises(ValidationError):
            AgentCollectorInput(
                contract_version="3.0",
                # job_id missing
                source_database_name="db",
                source_database_engine="mysql",
                collection_timestamp="2024-01-01T00:00:00Z",
                tables=[],
                queries={"query_patterns": []},
            )


class TestProjectionFunction:
    """Test project_schema_design_input maps full contracts correctly."""

    def test_projection_from_full_contracts(self, valid_collector_output_data):
        """Projection from full CollectorOutputContract + AnalysisOutputContract."""
        from src.contracts.analysis_output import (
            AgentMetadata,
            AnalysisOutputContract,
            CostEstimate,
            Pattern,
            TableRecommendation,
            TargetDatabase,
            WorkloadAnalysis,
        )
        from src.contracts.collector_output import CollectorOutputContract

        collector = CollectorOutputContract.model_validate(valid_collector_output_data)

        analysis = AnalysisOutputContract(
            contract_version="2.1",
            agent_metadata=AgentMetadata(
                agent_name="dynamodb-analysis-agent",
                agent_version="1.0.0",
                target_database=TargetDatabase.DYNAMODB,
                analysis_timestamp="2024-01-15T12:00:00Z",
                analysis_duration_seconds=30.0,
            ),
            table_recommendations=[
                TableRecommendation(
                    table_id="public.users",
                    confidence_score=85,
                    score_breakdown={
                        "pattern_match_score": 90,
                        "complexity_score": 80,
                        "performance_score": 85,
                        "cost_score": 75,
                    },
                )
            ],
            workload_analysis=WorkloadAnalysis(
                patterns_detected=[
                    Pattern(
                        pattern_id="p1",
                        pattern_type="key-value-lookup",
                        confidence="HIGH",
                        query_ids=["q1-select-users"],
                        table_ids=["public.users"],
                    )
                ]
            ),
            cost_estimate=CostEstimate(
                monthly_cost_usd=50.0,
                cost_components={"read": 30, "write": 20},
            ),
        )

        agent_collector, agent_analysis, agent_context = project_schema_design_input(
            collector, analysis
        )

        # Collector projection
        assert agent_collector.job_id == "test-job-12345"
        assert agent_collector.source_database_name == "production_db"
        assert agent_collector.source_database_engine == "postgresql"
        assert len(agent_collector.tables) == 2
        assert len(agent_collector.queries.query_patterns) == 2

        # Verify engine-specific fields are dropped
        first_query = agent_collector.queries.query_patterns[0]
        assert first_query.query_id == "q1-select-users"
        assert first_query.frequency_per_hour == 1200.0
        assert not hasattr(first_query, "cache_hit_ratio_pct")
        assert not hasattr(first_query, "shared_blks_hit")

        # Analysis projection
        assert len(agent_analysis.patterns_detected) == 1
        assert agent_analysis.patterns_detected[0].pattern_id == "p1"
        assert len(agent_analysis.table_recommendations) == 1
        assert agent_analysis.table_recommendations[0].table_id == "public.users"

        # Context defaults
        assert agent_context.growth_multiplier == 10.0
        assert agent_context.peak_to_avg_ratio == 3.0

    def test_projection_with_context_overrides(self, valid_collector_output_data):
        from src.contracts.analysis_output import (
            AgentMetadata,
            AnalysisOutputContract,
            CostEstimate,
            TargetDatabase,
            WorkloadAnalysis,
        )
        from src.contracts.collector_output import CollectorOutputContract

        collector = CollectorOutputContract.model_validate(valid_collector_output_data)
        analysis = AnalysisOutputContract(
            contract_version="2.1",
            agent_metadata=AgentMetadata(
                agent_name="test",
                agent_version="1.0.0",
                target_database=TargetDatabase.DYNAMODB,
                analysis_timestamp="2024-01-15T12:00:00Z",
            ),
            table_recommendations=[],
            workload_analysis=WorkloadAnalysis(patterns_detected=[]),
            cost_estimate=CostEstimate(monthly_cost_usd=0, cost_components={}),
        )

        _, _, ctx = project_schema_design_input(
            collector, analysis, context={"growth_multiplier": 5.0}
        )
        assert ctx.growth_multiplier == 5.0
        assert ctx.peak_to_avg_ratio == 3.0  # default preserved
