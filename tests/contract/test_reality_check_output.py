"""Tests for reality check output contract."""

import pytest
from pydantic import ValidationError

from src.contracts.reality_check_output import (
    ArchitecturalPattern,
    Consolidation,
    RealityCheckOutputContract,
    UniqueValueAssessment,
)


class TestUniqueValueAssessment:
    """Test per-engine unique value model."""

    def test_valid_assessment(self):
        uva = UniqueValueAssessment(
            total_queries=20,
            unique_queries=["q1", "q2", "q3"],
            redundant_queries=["q4", "q5"],
            unique_ratio=0.6,
            avg_delta=18.5,
            is_primary=True,
            is_mandatory=False,
        )
        assert uva.total_queries == 20
        assert len(uva.unique_queries) == 3
        assert uva.consolidation_blocked is None

    def test_unique_ratio_bounds(self):
        with pytest.raises(ValidationError):
            UniqueValueAssessment(
                total_queries=10,
                unique_queries=[],
                redundant_queries=[],
                unique_ratio=1.5,
                avg_delta=0,
                is_primary=False,
                is_mandatory=False,
            )

    def test_with_consolidation_blocked(self):
        uva = UniqueValueAssessment(
            total_queries=5,
            unique_queries=["q1"],
            redundant_queries=["q2", "q3"],
            unique_ratio=0.2,
            avg_delta=8.0,
            is_primary=False,
            is_mandatory=False,
            consolidation_blocked="Some queries could not be placed",
        )
        assert uva.consolidation_blocked is not None


class TestConsolidation:
    """Test consolidation record model."""

    def test_valid_consolidation(self):
        c = Consolidation(
            from_engine="opensearch",
            to_engine="dynamodb",
            query_count=15,
            reason="opensearch provides no unique capabilities",
            saved_cost_estimate=450.0,
        )
        assert c.from_engine == "opensearch"
        assert c.saved_cost_estimate == 450.0

    def test_query_count_must_be_positive(self):
        with pytest.raises(ValidationError):
            Consolidation(
                from_engine="opensearch",
                to_engine="dynamodb",
                query_count=0,
                reason="test",
                saved_cost_estimate=0,
            )

    def test_saved_cost_cannot_be_negative(self):
        with pytest.raises(ValidationError):
            Consolidation(
                from_engine="opensearch",
                to_engine="dynamodb",
                query_count=5,
                reason="test",
                saved_cost_estimate=-100.0,
            )


class TestArchitecturalPattern:
    """Test architectural pattern model."""

    def test_valid_cqrs_pattern(self):
        p = ArchitecturalPattern(
            name="Command Query Responsibility Segregation (CQRS)",
            description="Separate writes from reads across databases",
            when="One engine handles writes, another handles reads/search",
            example="DynamoDB for CRUD, OpenSearch for full-text search",
            applies_to={
                "write_engine": "dynamodb",
                "read_engines": ["opensearch"],
            },
        )
        assert "CQRS" in p.name
        assert p.applies_to["write_engine"] == "dynamodb"


class TestRealityCheckOutputContract:
    """Test the full reality check output contract."""

    @pytest.fixture
    def valid_reality_check_data(self):
        return {
            "source_assignment_version": 1,
            "unique_value_assessment": {
                "dynamodb": {
                    "total_queries": 85,
                    "unique_queries": ["q1", "q2", "q3"],
                    "redundant_queries": ["q4"],
                    "unique_ratio": 0.75,
                    "avg_delta": 22.3,
                    "is_primary": True,
                    "is_mandatory": False,
                },
                "opensearch": {
                    "total_queries": 20,
                    "unique_queries": ["q10", "q11"],
                    "redundant_queries": ["q12", "q13", "q14"],
                    "unique_ratio": 0.4,
                    "avg_delta": 12.0,
                    "is_primary": False,
                    "is_mandatory": True,
                },
            },
            "consolidations": [],
            "architectural_patterns": [
                {
                    "name": "CQRS",
                    "description": "Separate writes from reads",
                    "when": "Write/read split detected",
                    "example": "DynamoDB writes, OpenSearch reads",
                    "applies_to": {
                        "write_engine": "dynamodb",
                        "read_engines": ["opensearch"],
                    },
                }
            ],
            "recommendations": [
                "No consolidation opportunities found — current assignment is optimal."
            ],
            "before_distribution": {"dynamodb": 85, "opensearch": 20},
            "after_distribution": {"dynamodb": 85, "opensearch": 20},
        }

    def test_valid_data_validates(self, valid_reality_check_data):
        output = RealityCheckOutputContract.model_validate(valid_reality_check_data)
        assert output.source_assignment_version == 1
        assert len(output.unique_value_assessment) == 2
        assert len(output.architectural_patterns) == 1

    def test_contract_version_defaults(self, valid_reality_check_data):
        output = RealityCheckOutputContract.model_validate(valid_reality_check_data)
        assert output.contract_version == "1.1"

    def test_missing_assignment_version_fails(self, valid_reality_check_data):
        del valid_reality_check_data["source_assignment_version"]
        with pytest.raises(ValidationError):
            RealityCheckOutputContract.model_validate(valid_reality_check_data)

    def test_with_consolidation(self, valid_reality_check_data):
        valid_reality_check_data["consolidations"] = [
            {
                "from_engine": "documentdb",
                "to_engine": "dynamodb",
                "query_count": 30,
                "reason": "documentdb provides no unique capabilities",
                "saved_cost_estimate": 500.0,
            }
        ]
        valid_reality_check_data["after_distribution"] = {
            "dynamodb": 115,
            "opensearch": 20,
        }
        output = RealityCheckOutputContract.model_validate(valid_reality_check_data)
        assert len(output.consolidations) == 1
        assert output.consolidations[0].saved_cost_estimate == 500.0

    def test_empty_consolidations_allowed(self, valid_reality_check_data):
        output = RealityCheckOutputContract.model_validate(valid_reality_check_data)
        assert output.consolidations == []

    def test_empty_unique_value_assessment_allowed(self, valid_reality_check_data):
        valid_reality_check_data["unique_value_assessment"] = {}
        output = RealityCheckOutputContract.model_validate(valid_reality_check_data)
        assert output.unique_value_assessment == {}

    def test_roundtrip_serialization(self, valid_reality_check_data):
        output = RealityCheckOutputContract.model_validate(valid_reality_check_data)
        dumped = output.model_dump(mode="json")
        roundtrip = RealityCheckOutputContract.model_validate(dumped)
        assert roundtrip.source_assignment_version == output.source_assignment_version
        assert len(roundtrip.consolidations) == len(output.consolidations)

    def test_distribution_changes_after_consolidation(self, valid_reality_check_data):
        valid_reality_check_data["before_distribution"] = {
            "dynamodb": 85,
            "documentdb": 30,
            "opensearch": 20,
        }
        valid_reality_check_data["after_distribution"] = {
            "dynamodb": 115,
            "opensearch": 20,
        }
        output = RealityCheckOutputContract.model_validate(valid_reality_check_data)
        assert "documentdb" in output.before_distribution
        assert "documentdb" not in output.after_distribution
        assert output.after_distribution["dynamodb"] == 115
