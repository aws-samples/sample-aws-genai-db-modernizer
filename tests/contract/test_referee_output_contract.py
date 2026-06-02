"""
Unit tests for RefereeOutputContract validation.

This module tests the RefereeOutputContract Pydantic model to ensure:
- Valid data creates contract instances successfully
- Contracts with all optional fields work correctly
- Contracts with minimal required fields work correctly
- Serialization and deserialization work correctly
"""

import json

import pytest
from pydantic import ValidationError

from src.contracts.referee_output import (
    ArchitectureType,
    RefereeOutputContract,
    RiskLevel,
    RiskType,
)


class TestRefereeOutputContractInstantiation:
    """Tests for creating RefereeOutputContract instances with various data."""

    def test_valid_contract_instantiation(self, valid_referee_output_data):
        """
        Test that valid data creates a contract instance successfully.

        This test validates that a complete, valid contract with all required
        fields and many optional fields can be instantiated without errors.
        """
        # Act
        contract = RefereeOutputContract(**valid_referee_output_data)

        # Assert
        assert contract is not None
        assert contract.contract_version == "2.0"
        assert contract.recommended_architecture is not None
        assert contract.table_mappings is not None
        assert contract.tco_analysis is not None
        assert contract.risk_assessment is not None

        # Verify nested structures
        assert (
            contract.recommended_architecture.architecture_type == ArchitectureType.MULTI_DATABASE
        )
        assert len(contract.recommended_architecture.databases) == 2
        assert len(contract.table_mappings) == 3
        assert contract.tco_analysis.current_monthly_cost == 5000.0
        assert contract.risk_assessment.overall_risk_level == RiskLevel.MEDIUM

    def test_contract_with_all_optional_fields(self, valid_referee_output_data):
        """
        Test that a contract with all optional fields populated works correctly.

        This test ensures that when all optional fields are provided with valid
        values, the contract instantiates successfully and retains all data.
        """
        # Act
        contract = RefereeOutputContract(**valid_referee_output_data)

        # Assert - Verify optional fields
        assert contract.assessment_report is not None
        assert contract.assessment_report.html_url is not None
        assert contract.assessment_report.pdf_url is not None
        assert contract.schema_design_requested is True
        assert contract.selected_databases is not None
        assert len(contract.selected_databases) == 2

        # Verify optional architecture fields
        assert contract.recommended_architecture.architecture_diagram_url is not None
        assert contract.recommended_architecture.rationale is not None

        # Verify optional TCO fields
        assert contract.tco_analysis.cost_breakdown is not None
        assert len(contract.tco_analysis.cost_breakdown) == 2
        assert contract.tco_analysis.three_year_tco is not None
        assert contract.tco_analysis.assumptions is not None

        # Verify optional risk assessment fields
        assert contract.risk_assessment.mitigation_strategies is not None
        assert len(contract.risk_assessment.risks) == 2

    def test_contract_with_minimal_required_fields(self, minimal_referee_output_data):
        """
        Test that a contract with only required fields works correctly.

        This test ensures that optional fields are truly optional and a valid
        contract can be created with just the minimum required data.
        """
        # Act
        contract = RefereeOutputContract(**minimal_referee_output_data)

        # Assert - Verify required fields are present
        assert contract.contract_version == "2.0"
        assert contract.recommended_architecture is not None
        assert contract.table_mappings is not None
        assert contract.tco_analysis is not None
        assert contract.risk_assessment is not None

        # Verify optional fields are None or have default values
        assert contract.assessment_report is None
        assert contract.schema_design_requested is False
        assert contract.selected_databases is None

        # Verify minimal architecture
        assert (
            contract.recommended_architecture.architecture_type == ArchitectureType.SINGLE_DATABASE
        )
        assert len(contract.recommended_architecture.databases) == 1
        assert contract.recommended_architecture.architecture_diagram_url is None
        assert contract.recommended_architecture.rationale is None

        # Verify minimal TCO
        assert contract.tco_analysis.cost_breakdown is None
        assert contract.tco_analysis.three_year_tco is None
        assert contract.tco_analysis.assumptions is None

        # Verify minimal risk assessment
        assert contract.risk_assessment.mitigation_strategies is None

    def test_contract_instantiation_preserves_data_types(self, valid_referee_output_data):
        """
        Test that contract instantiation preserves correct data types.

        This test ensures that Pydantic correctly converts and validates
        data types during instantiation.
        """
        # Act
        contract = RefereeOutputContract(**valid_referee_output_data)

        # Assert - Verify data types
        assert isinstance(contract.contract_version, str)
        assert isinstance(contract.recommended_architecture.architecture_type, ArchitectureType)
        assert isinstance(contract.tco_analysis.current_monthly_cost, float)
        assert isinstance(contract.tco_analysis.projected_monthly_cost, float)
        assert isinstance(contract.tco_analysis.savings_percent, float)

        # Verify list types
        assert isinstance(contract.recommended_architecture.databases, list)
        assert isinstance(contract.table_mappings, list)
        assert isinstance(contract.risk_assessment.risks, list)

        # Verify enum types
        assert isinstance(contract.risk_assessment.overall_risk_level, RiskLevel)
        assert isinstance(contract.risk_assessment.risks[0].risk_type, RiskType)

    def test_contract_with_default_contract_version(self, minimal_referee_output_data):
        """
        Test that contract_version defaults to "2.0" when not provided.

        This test verifies that the contract_version field has a proper
        default value and doesn't need to be explicitly provided.
        """
        # Arrange - Create data without contract_version
        data = minimal_referee_output_data.copy()
        data.pop("contract_version", None)

        # Act
        contract = RefereeOutputContract(**data)

        # Assert
        assert contract.contract_version == "2.0"


class TestRefereeOutputContractSerialization:
    """Tests for JSON serialization of RefereeOutputContract."""

    def test_model_dump_produces_correct_dictionary(self, valid_referee_output_data):
        """
        Test that model_dump() produces a correct dictionary representation.

        This test verifies that the Pydantic model_dump() method correctly
        serializes a contract instance to a Python dictionary with all fields
        properly represented.
        """
        # Arrange
        contract = RefereeOutputContract(**valid_referee_output_data)

        # Act
        dumped_dict = contract.model_dump()

        # Assert - Verify it's a dictionary
        assert isinstance(dumped_dict, dict)

        # Verify top-level fields are present
        assert "contract_version" in dumped_dict
        assert "recommended_architecture" in dumped_dict
        assert "table_mappings" in dumped_dict
        assert "tco_analysis" in dumped_dict
        assert "risk_assessment" in dumped_dict

        # Verify values match original data
        assert dumped_dict["contract_version"] == "2.0"

        # Verify nested structures are dictionaries
        assert isinstance(dumped_dict["recommended_architecture"], dict)
        assert isinstance(dumped_dict["table_mappings"], list)
        assert isinstance(dumped_dict["tco_analysis"], dict)
        assert isinstance(dumped_dict["risk_assessment"], dict)

    def test_model_dump_json_produces_valid_json_string(self, valid_referee_output_data):
        """
        Test that model_dump_json() produces a valid JSON string.

        This test verifies that the Pydantic model_dump_json() method correctly
        serializes a contract instance to a JSON string that is valid and
        properly formatted.
        """
        # Arrange
        contract = RefereeOutputContract(**valid_referee_output_data)

        # Act
        json_string = contract.model_dump_json()

        # Assert - Verify it's a string
        assert isinstance(json_string, str)

        # Verify it's not empty
        assert len(json_string) > 0

        # Verify it contains expected keys
        assert '"contract_version"' in json_string
        assert '"recommended_architecture"' in json_string
        assert '"table_mappings"' in json_string
        assert '"tco_analysis"' in json_string
        assert '"risk_assessment"' in json_string

    def test_serialized_json_can_be_parsed(self, valid_referee_output_data):
        """
        Test that serialized JSON can be parsed back into a Python object.

        This test verifies that the JSON string produced by model_dump_json()
        is valid JSON that can be parsed by the standard json module.
        """
        # Arrange
        contract = RefereeOutputContract(**valid_referee_output_data)
        json_string = contract.model_dump_json()

        # Act
        parsed_data = json.loads(json_string)

        # Assert - Verify parsing succeeded and produced a dictionary
        assert isinstance(parsed_data, dict)

        # Verify all top-level keys are present
        assert "contract_version" in parsed_data
        assert "recommended_architecture" in parsed_data
        assert "table_mappings" in parsed_data
        assert "tco_analysis" in parsed_data
        assert "risk_assessment" in parsed_data

        # Verify values are correct
        assert parsed_data["contract_version"] == "2.0"

    def test_model_dump_with_minimal_data(self, minimal_referee_output_data):
        """
        Test that model_dump() works correctly with minimal required fields.

        This test verifies that serialization works even when only required
        fields are present, and optional fields are properly handled.
        """
        # Arrange
        contract = RefereeOutputContract(**minimal_referee_output_data)

        # Act
        dumped_dict = contract.model_dump()

        # Assert - Verify required fields are present
        assert dumped_dict["contract_version"] == "2.0"
        assert dumped_dict["recommended_architecture"] is not None
        assert dumped_dict["table_mappings"] is not None
        assert dumped_dict["tco_analysis"] is not None
        assert dumped_dict["risk_assessment"] is not None

        # Verify optional fields are None or default values
        assert dumped_dict["assessment_report"] is None
        assert dumped_dict["schema_design_requested"] is False
        assert dumped_dict["selected_databases"] is None

    def test_model_dump_preserves_data_types(self, valid_referee_output_data):
        """
        Test that model_dump() preserves correct Python data types.

        This test verifies that serialization to dictionary maintains proper
        Python types rather than converting everything to strings.
        """
        # Arrange
        contract = RefereeOutputContract(**valid_referee_output_data)

        # Act
        dumped_dict = contract.model_dump()

        # Assert - Verify string types
        assert isinstance(dumped_dict["contract_version"], str)

        # Verify numeric types
        assert isinstance(dumped_dict["tco_analysis"]["current_monthly_cost"], float)
        assert isinstance(dumped_dict["tco_analysis"]["projected_monthly_cost"], float)
        assert isinstance(dumped_dict["tco_analysis"]["savings_percent"], float)

        # Verify boolean types
        assert isinstance(dumped_dict["schema_design_requested"], bool)

        # Verify list types
        assert isinstance(dumped_dict["recommended_architecture"]["databases"], list)
        assert isinstance(dumped_dict["table_mappings"], list)
        assert isinstance(dumped_dict["risk_assessment"]["risks"], list)

        # Verify enum values are serialized as strings
        assert isinstance(dumped_dict["recommended_architecture"]["architecture_type"], str)
        assert isinstance(dumped_dict["risk_assessment"]["overall_risk_level"], str)


class TestRefereeOutputContractDeserialization:
    """Tests for JSON deserialization of RefereeOutputContract."""

    def test_model_validate_from_dictionary(self, valid_referee_output_data):
        """
        Test that model_validate() can deserialize from a dictionary.

        This test verifies that the Pydantic model_validate() method correctly
        deserializes a Python dictionary into a contract instance.
        """
        # Act
        contract = RefereeOutputContract.model_validate(valid_referee_output_data)

        # Assert - Verify contract was created successfully
        assert contract is not None
        assert isinstance(contract, RefereeOutputContract)

        # Verify top-level fields
        assert contract.contract_version == "2.0"
        assert contract.recommended_architecture is not None
        assert contract.table_mappings is not None
        assert contract.tco_analysis is not None
        assert contract.risk_assessment is not None

    def test_model_validate_json_from_json_string(self, valid_referee_output_data):
        """
        Test that model_validate_json() can deserialize from a JSON string.

        This test verifies that the Pydantic model_validate_json() method correctly
        deserializes a JSON string into a contract instance.
        """
        # Arrange - Create a JSON string from the test data
        json_string = json.dumps(valid_referee_output_data)

        # Act
        contract = RefereeOutputContract.model_validate_json(json_string)

        # Assert - Verify contract was created successfully
        assert contract is not None
        assert isinstance(contract, RefereeOutputContract)

        # Verify top-level fields
        assert contract.contract_version == "2.0"
        assert contract.recommended_architecture is not None

    def test_deserialization_with_extra_fields(self, valid_referee_output_data):
        """
        Test that deserialization handles extra fields correctly.

        This test verifies that Pydantic's extra="ignore" configuration works
        correctly during deserialization. Extra fields should be silently
        ignored without causing errors.
        """
        # Arrange - Add extra fields to the data
        data_with_extra = valid_referee_output_data.copy()
        data_with_extra["extra_field_1"] = "should be ignored"
        data_with_extra["extra_field_2"] = 12345

        # Act
        contract = RefereeOutputContract.model_validate(data_with_extra)

        # Assert - Verify contract was created successfully
        assert contract is not None
        assert isinstance(contract, RefereeOutputContract)

        # Verify extra fields are not present in the contract
        assert not hasattr(contract, "extra_field_1")
        assert not hasattr(contract, "extra_field_2")

        # Verify required fields are still present and correct
        assert contract.contract_version == "2.0"

    def test_model_validate_with_minimal_data(self, minimal_referee_output_data):
        """
        Test that model_validate() works with minimal required fields.

        This test verifies that deserialization works correctly when only
        required fields are present in the input data.
        """
        # Act
        contract = RefereeOutputContract.model_validate(minimal_referee_output_data)

        # Assert - Verify contract was created successfully
        assert contract is not None
        assert isinstance(contract, RefereeOutputContract)

        # Verify required fields
        assert contract.contract_version == "2.0"
        assert contract.recommended_architecture is not None

        # Verify optional fields are None or defaults
        assert contract.assessment_report is None
        assert contract.schema_design_requested is False


class TestRefereeOutputContractValidation:
    """Tests for validation rules in RefereeOutputContract."""

    def test_missing_required_field_raises_validation_error(self, valid_referee_output_data):
        """
        Test that missing required fields raise ValidationError.
        """
        # Arrange - Remove a required field
        invalid_data = valid_referee_output_data.copy()
        del invalid_data["recommended_architecture"]

        # Act & Assert
        with pytest.raises(ValidationError) as exc_info:
            RefereeOutputContract(**invalid_data)

        # Verify error message mentions the missing field
        error_message = str(exc_info.value).lower()
        assert "required" in error_message or "missing" in error_message

    def test_invalid_enum_value_raises_validation_error(self, valid_referee_output_data):
        """
        Test that invalid enum values raise ValidationError.
        """
        # Arrange - Set invalid enum value
        invalid_data = valid_referee_output_data.copy()
        invalid_data["recommended_architecture"]["architecture_type"] = "INVALID_TYPE"

        # Act & Assert
        with pytest.raises(ValidationError) as exc_info:
            RefereeOutputContract(**invalid_data)

        # Verify error message mentions enum validation
        error_message = str(exc_info.value).lower()
        assert any(keyword in error_message for keyword in ["invalid", "enum", "not a valid"])

    def test_negative_cost_raises_validation_error(self, valid_referee_output_data):
        """
        Test that negative cost values raise ValidationError.
        """
        # Arrange - Set negative cost
        invalid_data = valid_referee_output_data.copy()
        invalid_data["tco_analysis"]["current_monthly_cost"] = -1000.0

        # Act & Assert
        with pytest.raises(ValidationError) as exc_info:
            RefereeOutputContract(**invalid_data)

        # Verify error message mentions constraint violation
        error_message = str(exc_info.value).lower()
        assert any(keyword in error_message for keyword in ["greater", "constraint"])

    def test_invalid_confidence_score_raises_validation_error(self, valid_referee_output_data):
        """
        Test that confidence scores outside 0-100 range raise ValidationError.
        """
        # Arrange - Set invalid confidence score
        invalid_data = valid_referee_output_data.copy()
        invalid_data["table_mappings"][0]["confidence_score"] = 150

        # Act & Assert
        with pytest.raises(ValidationError) as exc_info:
            RefereeOutputContract(**invalid_data)

        # Verify error message mentions constraint violation
        error_message = str(exc_info.value).lower()
        assert any(keyword in error_message for keyword in ["less", "constraint"])

    def test_invalid_contract_version_pattern_raises_validation_error(
        self, valid_referee_output_data
    ):
        """
        Test that invalid contract version pattern raises ValidationError.
        """
        # Arrange - Set invalid contract version
        invalid_data = valid_referee_output_data.copy()
        invalid_data["contract_version"] = "1.2.3"  # Should be MAJOR.MINOR only

        # Act & Assert
        with pytest.raises(ValidationError) as exc_info:
            RefereeOutputContract(**invalid_data)

        # Verify error message mentions pattern validation
        error_message = str(exc_info.value).lower()
        assert any(keyword in error_message for keyword in ["pattern", "match"])
