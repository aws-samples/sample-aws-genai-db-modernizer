"""Tests for synthesis output contract."""

from datetime import datetime

import pytest
from pydantic import ValidationError

from src.contracts.synthesis_output import (
    AssignmentSummary,
    CostBreakdown,
    EngineRanking,
    Risk,
    RiskAssessment,
    SynthesisOutputContract,
    TableMapping,
    TCOAnalysis,
)


class TestEngineRanking:
    """Test engine ranking model."""

    def test_valid_ranking(self):
        r = EngineRanking(target="dynamodb", confidence_score=85)
        assert r.target == "dynamodb"
        assert r.confidence_score == 85

    def test_full_ranking(self):
        r = EngineRanking(
            target="dynamodb",
            confidence_score=85,
            pattern_score=90,
            complexity_score=70,
            performance_score=95,
            cost_score=80,
            migration_complexity_avg="MEDIUM",
            assigned_queries=42,
            workload_percent=65.5,
        )
        assert r.assigned_queries == 42

    def test_confidence_bounds(self):
        with pytest.raises(ValidationError):
            EngineRanking(target="dynamodb", confidence_score=101)
        with pytest.raises(ValidationError):
            EngineRanking(target="dynamodb", confidence_score=-1)

    def test_extra_fields_allowed(self):
        r = EngineRanking(target="dynamodb", confidence_score=85, custom_field="value")
        assert r.model_extra["custom_field"] == "value"


class TestTableMapping:
    """Test table mapping model."""

    def test_valid_mapping(self):
        m = TableMapping(
            source_table="db.users",
            recommended_database="dynamodb",
            confidence_score=90,
        )
        assert m.source_table == "db.users"

    def test_with_alternatives(self):
        m = TableMapping(
            source_table="db.users",
            recommended_database="dynamodb",
            confidence_score=90,
            alternatives=[{"database": "documentdb", "score": 60}],
        )
        assert len(m.alternatives) == 1


class TestTCOAnalysis:
    """Test TCO analysis model."""

    def test_valid_tco(self):
        tco = TCOAnalysis(
            current_monthly_cost=1200.0,
            projected_monthly_cost=450.0,
            savings_percent=62.5,
        )
        assert tco.savings_percent == 62.5

    def test_with_breakdown(self):
        tco = TCOAnalysis(
            current_monthly_cost=1200.0,
            projected_monthly_cost=450.0,
            savings_percent=62.5,
            cost_breakdown=[
                CostBreakdown(database="dynamodb", monthly_cost_usd=300.0),
                CostBreakdown(database="opensearch", monthly_cost_usd=150.0),
            ],
        )
        assert len(tco.cost_breakdown) == 2

    def test_negative_savings_allowed(self):
        """Migration can cost more than current setup."""
        tco = TCOAnalysis(
            current_monthly_cost=500.0,
            projected_monthly_cost=800.0,
            savings_percent=-60.0,
        )
        assert tco.savings_percent == -60.0


class TestRiskAssessment:
    """Test risk assessment model."""

    def test_valid_assessment(self):
        ra = RiskAssessment(
            overall_risk_level="MEDIUM",
            risks=[
                Risk(severity="HIGH", description="Complex joins not fully supported"),
                Risk(severity="LOW", description="Minor schema changes needed"),
            ],
        )
        assert len(ra.risks) == 2
        assert ra.overall_risk_level == "MEDIUM"


class TestAssignmentSummary:
    """Test assignment summary model."""

    def test_valid_summary(self):
        s = AssignmentSummary(
            version=1,
            status="completed",
            query_count=100,
            in_scope_count=95,
            co_dependency_groups=3,
        )
        assert s.in_scope_count == 95

    def test_optional_fields(self):
        s = AssignmentSummary(query_count=50, in_scope_count=50)
        assert s.version is None
        assert s.status is None
        assert s.co_dependency_groups == 0


class TestSynthesisOutputContract:
    """Test the full synthesis output contract."""

    @pytest.fixture
    def valid_synthesis_data(self):
        return {
            "job_id": "abc123",
            "database_name": "test_db",
            "agent_type": "referee-synthesis",
            "status": "completed",
            "timestamp": "2026-04-23T12:00:00Z",
            "needs_deeper_analysis": False,
            "ranking": [
                {"target": "dynamodb", "confidence_score": 85, "assigned_queries": 60},
                {"target": "documentdb", "confidence_score": 70, "assigned_queries": 40},
            ],
            "summary": "This database is a good candidate for DynamoDB + DocumentDB.",
            "summary_deterministic": "2 engines selected. DynamoDB: 60 queries. DocumentDB: 40 queries.",
            "recommended_architecture": {
                "architecture_type": "MULTI_DATABASE",
                "primary_database": "dynamodb",
                "databases": [
                    {"engine": "dynamodb", "role": "primary"},
                    {"engine": "documentdb", "role": "secondary"},
                ],
            },
            "table_mappings": [
                {
                    "source_table": "db.users",
                    "recommended_database": "dynamodb",
                    "confidence_score": 90,
                },
                {
                    "source_table": "db.posts",
                    "recommended_database": "documentdb",
                    "confidence_score": 75,
                },
            ],
            "query_groups": [
                {
                    "group_name": "user_lookups",
                    "engines": ["dynamodb"],
                    "access_patterns": [{"pattern": "GetItem by PK"}],
                },
            ],
            "tco_analysis": {
                "current_monthly_cost": 1200.0,
                "projected_monthly_cost": 450.0,
                "savings_percent": 62.5,
            },
            "risk_assessment": {
                "overall_risk_level": "MEDIUM",
                "risks": [
                    {"severity": "HIGH", "description": "Complex joins require refactoring"},
                ],
            },
            "schema_designs": {
                "dynamodb": {"status": "completed", "validation_passed": True},
            },
            "trade_offs": [
                {
                    "description": "GSI cost increase for flexible queries",
                    "impact": "Each GSI adds write amplification. For high-write tables this increases cost.",
                    "source_tables": ["db.orders"],
                    "target_tables": ["Orders"],
                    "query_ids": ["q1"],
                    "engine": "dynamodb",
                }
            ],
        }

    def test_valid_synthesis_validates(self, valid_synthesis_data):
        output = SynthesisOutputContract.model_validate(valid_synthesis_data)
        assert output.job_id == "abc123"
        assert len(output.ranking) == 2
        assert output.needs_deeper_analysis is False

    def test_contract_version_defaults(self, valid_synthesis_data):
        output = SynthesisOutputContract.model_validate(valid_synthesis_data)
        assert output.contract_version == "1.0"

    def test_missing_job_id_fails(self, valid_synthesis_data):
        del valid_synthesis_data["job_id"]
        with pytest.raises(ValidationError):
            SynthesisOutputContract.model_validate(valid_synthesis_data)

    def test_missing_ranking_fails(self, valid_synthesis_data):
        del valid_synthesis_data["ranking"]
        with pytest.raises(ValidationError):
            SynthesisOutputContract.model_validate(valid_synthesis_data)

    def test_needs_deeper_analysis_true(self, valid_synthesis_data):
        valid_synthesis_data["needs_deeper_analysis"] = True
        output = SynthesisOutputContract.model_validate(valid_synthesis_data)
        assert output.needs_deeper_analysis is True

    def test_with_assignment_summary(self, valid_synthesis_data):
        valid_synthesis_data["assignment_summary"] = {
            "version": 1,
            "status": "completed",
            "query_count": 100,
            "in_scope_count": 95,
            "co_dependency_groups": 3,
        }
        output = SynthesisOutputContract.model_validate(valid_synthesis_data)
        assert output.assignment_summary is not None
        assert output.assignment_summary.version == 1

    def test_without_assignment_summary(self, valid_synthesis_data):
        output = SynthesisOutputContract.model_validate(valid_synthesis_data)
        assert output.assignment_summary is None

    def test_empty_trade_offs(self, valid_synthesis_data):
        valid_synthesis_data["trade_offs"] = []
        output = SynthesisOutputContract.model_validate(valid_synthesis_data)
        assert output.trade_offs == []

    def test_schema_designs_defaults_to_empty(self, valid_synthesis_data):
        del valid_synthesis_data["schema_designs"]
        output = SynthesisOutputContract.model_validate(valid_synthesis_data)
        assert output.schema_designs == {}

    def test_roundtrip_serialization(self, valid_synthesis_data):
        """Validate that model_dump(mode='json') produces data that re-validates."""
        output = SynthesisOutputContract.model_validate(valid_synthesis_data)
        dumped = output.model_dump(mode="json")
        roundtrip = SynthesisOutputContract.model_validate(dumped)
        assert roundtrip.job_id == output.job_id
        assert len(roundtrip.ranking) == len(output.ranking)
        assert roundtrip.tco_analysis.savings_percent == output.tco_analysis.savings_percent

    def test_timestamp_parsing(self, valid_synthesis_data):
        output = SynthesisOutputContract.model_validate(valid_synthesis_data)
        assert isinstance(output.timestamp, datetime)

    def test_extra_fields_allowed_on_root(self, valid_synthesis_data):
        """Step Functions may add extra fields we don't know about."""
        valid_synthesis_data["synthesis_iteration"] = 2
        output = SynthesisOutputContract.model_validate(valid_synthesis_data)
        assert output.model_extra["synthesis_iteration"] == 2

    def test_empty_ranking_allowed(self, valid_synthesis_data):
        valid_synthesis_data["ranking"] = []
        output = SynthesisOutputContract.model_validate(valid_synthesis_data)
        assert output.ranking == []

    def test_status_defaults(self, valid_synthesis_data):
        del valid_synthesis_data["status"]
        output = SynthesisOutputContract.model_validate(valid_synthesis_data)
        assert output.status == "completed"
