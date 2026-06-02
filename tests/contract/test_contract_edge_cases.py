"""
Edge case tests for CollectorOutputContract validation.

This module tests edge cases and error conditions for the CollectorOutputContract
Pydantic model to ensure proper validation and error handling for:
- Missing required fields
- Invalid data types
- Out-of-range values
- Invalid enum values
- Null values in non-nullable fields
- Extra fields

Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7
"""

import pytest
from pydantic import ValidationError

from src.contracts.collector_output import CollectorOutputContract


class TestMissingRequiredFields:
    """Tests for missing required fields validation."""

    def test_missing_contract_version_uses_default(self, minimal_collector_output_data):
        """
        Test that missing contract_version uses the default value.

        Since contract_version has a default value of "3.0", it should not
        raise a ValidationError when omitted.

        Requirements: 6.1
        """
        # Arrange - Remove contract_version
        data = minimal_collector_output_data.copy()
        data.pop("contract_version", None)

        # Act
        contract = CollectorOutputContract(**data)

        # Assert - Should use default value
        assert contract.contract_version == "3.0"

    def test_missing_job_id_raises_validation_error(self, minimal_collector_output_data):
        """
        Test that missing job_id raises ValidationError.

        The job_id field is required and has no default value, so omitting it
        should raise a ValidationError with a message mentioning the field.

        Requirements: 6.1, 6.8
        """
        # Arrange - Remove job_id
        data = minimal_collector_output_data.copy()
        data.pop("job_id")

        # Act & Assert
        with pytest.raises(ValidationError) as exc_info:
            CollectorOutputContract(**data)

        # Verify error message mentions the missing field
        error_str = str(exc_info.value)
        assert "job_id" in error_str.lower()
        assert "field required" in error_str.lower() or "missing" in error_str.lower()

    def test_missing_metadata_raises_validation_error(self, minimal_collector_output_data):
        """
        Test that missing metadata raises ValidationError.

        The metadata field is required, so omitting it should raise a
        ValidationError with a message mentioning the field.

        Requirements: 6.1, 6.8
        """
        # Arrange - Remove metadata
        data = minimal_collector_output_data.copy()
        data.pop("metadata")

        # Act & Assert
        with pytest.raises(ValidationError) as exc_info:
            CollectorOutputContract(**data)

        # Verify error message mentions the missing field
        error_str = str(exc_info.value)
        assert "metadata" in error_str.lower()
        assert "field required" in error_str.lower() or "missing" in error_str.lower()

    def test_missing_database_schema_raises_validation_error(self, minimal_collector_output_data):
        """
        Test that missing database_schema raises ValidationError.

        The database_schema field is required, so omitting it should raise a
        ValidationError with a message mentioning the field.

        Requirements: 6.1, 6.8
        """
        # Arrange - Remove database_schema
        data = minimal_collector_output_data.copy()
        data.pop("database_schema")

        # Act & Assert
        with pytest.raises(ValidationError) as exc_info:
            CollectorOutputContract(**data)

        # Verify error message mentions the missing field
        error_str = str(exc_info.value)
        assert "database_schema" in error_str.lower()
        assert "field required" in error_str.lower() or "missing" in error_str.lower()

    def test_missing_queries_raises_validation_error(self, minimal_collector_output_data):
        """
        Test that missing queries raises ValidationError.

        The queries field is required, so omitting it should raise a
        ValidationError with a message mentioning the field.

        Requirements: 6.1, 6.8
        """
        # Arrange - Remove queries
        data = minimal_collector_output_data.copy()
        data.pop("queries")

        # Act & Assert
        with pytest.raises(ValidationError) as exc_info:
            CollectorOutputContract(**data)

        # Verify error message mentions the missing field
        error_str = str(exc_info.value)
        assert "queries" in error_str.lower()
        assert "field required" in error_str.lower() or "missing" in error_str.lower()

    def test_missing_metrics_raises_validation_error(self, minimal_collector_output_data):
        """
        Test that missing metrics raises ValidationError.

        The metrics field is required, so omitting it should raise a
        ValidationError with a message mentioning the field.

        Requirements: 6.1, 6.8
        """
        # Arrange - Remove metrics
        data = minimal_collector_output_data.copy()
        data.pop("metrics")

        # Act & Assert
        with pytest.raises(ValidationError) as exc_info:
            CollectorOutputContract(**data)

        # Verify error message mentions the missing field
        error_str = str(exc_info.value)
        assert "metrics" in error_str.lower()
        assert "field required" in error_str.lower() or "missing" in error_str.lower()

    def test_missing_nested_required_field_raises_validation_error(
        self, minimal_collector_output_data
    ):
        """
        Test that missing required fields in nested models raise ValidationError.

        This test verifies that required fields in nested models (like
        source_database in metadata) are properly validated.

        Requirements: 6.1, 6.8
        """
        # Arrange - Remove source_database from metadata
        data = minimal_collector_output_data.copy()
        data["metadata"].pop("source_database")

        # Act & Assert
        with pytest.raises(ValidationError) as exc_info:
            CollectorOutputContract(**data)

        # Verify error message mentions the missing field
        error_str = str(exc_info.value)
        assert "source_database" in error_str.lower()
        assert "field required" in error_str.lower() or "missing" in error_str.lower()


class TestInvalidDataTypes:
    """Tests for invalid data type validation."""

    def test_string_instead_of_integer_raises_validation_error(self, minimal_collector_output_data):
        """
        Test that providing a string instead of an integer raises ValidationError.

        This test verifies that Pydantic properly validates integer fields and
        rejects string values that cannot be coerced to integers.

        Requirements: 6.2, 6.8
        """
        # Arrange - Set row_count to a non-numeric string
        data = minimal_collector_output_data.copy()
        data["database_schema"]["tables"][0]["row_count"] = "not_a_number"

        # Act & Assert
        with pytest.raises(ValidationError) as exc_info:
            CollectorOutputContract(**data)

        # Verify error message mentions the type mismatch
        error_str = str(exc_info.value)
        assert "row_count" in error_str.lower()
        assert any(
            keyword in error_str.lower()
            for keyword in ["int", "integer", "type", "invalid", "parse"]
        )

    def test_integer_instead_of_string_raises_validation_error(self, minimal_collector_output_data):
        """
        Test that providing an integer instead of a string raises ValidationError.

        This test verifies that Pydantic properly validates string fields and
        rejects integer values without coercion.

        Requirements: 6.2, 6.8
        """
        # Arrange - Set job_id to an integer
        data = minimal_collector_output_data.copy()
        data["job_id"] = 12345

        # Act & Assert - Pydantic v2 does not coerce integers to strings
        with pytest.raises(ValidationError) as exc_info:
            CollectorOutputContract(**data)

        # Verify error message mentions the type mismatch
        error_str = str(exc_info.value)
        assert "job_id" in error_str.lower()
        assert any(keyword in error_str.lower() for keyword in ["string", "str", "type", "invalid"])

    def test_string_instead_of_datetime_raises_validation_error(
        self, minimal_collector_output_data
    ):
        """
        Test that providing an invalid datetime string raises ValidationError.

        This test verifies that Pydantic properly validates datetime fields and
        rejects strings that cannot be parsed as ISO 8601 datetimes.

        Requirements: 6.2, 6.8
        """
        # Arrange - Set collection_timestamp to an invalid datetime string
        data = minimal_collector_output_data.copy()
        data["metadata"]["collection_timestamp"] = "not-a-valid-datetime"

        # Act & Assert
        with pytest.raises(ValidationError) as exc_info:
            CollectorOutputContract(**data)

        # Verify error message mentions the type mismatch
        error_str = str(exc_info.value)
        assert "collection_timestamp" in error_str.lower()
        assert any(
            keyword in error_str.lower()
            for keyword in ["datetime", "date", "time", "invalid", "parse"]
        )

    def test_list_instead_of_dict_raises_validation_error(self, minimal_collector_output_data):
        """
        Test that providing a list instead of a dict for nested models raises ValidationError.

        This test verifies that Pydantic properly validates nested model fields
        and rejects list values when a dict/object is expected.

        Requirements: 6.2, 6.8
        """
        # Arrange - Set metadata to a list instead of dict
        data = minimal_collector_output_data.copy()
        data["metadata"] = ["not", "a", "dict"]

        # Act & Assert
        with pytest.raises(ValidationError) as exc_info:
            CollectorOutputContract(**data)

        # Verify error message mentions the type mismatch
        error_str = str(exc_info.value)
        assert "metadata" in error_str.lower()
        assert any(keyword in error_str.lower() for keyword in ["dict", "object", "type", "input"])

    def test_dict_instead_of_list_raises_validation_error(self, minimal_collector_output_data):
        """
        Test that providing a dict instead of a list raises ValidationError.

        This test verifies that Pydantic properly validates list fields and
        rejects dict values when a list is expected.

        Requirements: 6.2, 6.8
        """
        # Arrange - Set tables to a dict instead of list
        data = minimal_collector_output_data.copy()
        data["database_schema"]["tables"] = {"not": "a list"}

        # Act & Assert
        with pytest.raises(ValidationError) as exc_info:
            CollectorOutputContract(**data)

        # Verify error message mentions the type mismatch
        error_str = str(exc_info.value)
        assert "tables" in error_str.lower()
        assert any(keyword in error_str.lower() for keyword in ["list", "array", "type", "input"])


class TestOutOfRangeValues:
    """Tests for out-of-range value validation."""

    def test_negative_row_count_raises_validation_error(self, minimal_collector_output_data):
        """
        Test that negative row_count raises ValidationError.

        The row_count field has a constraint ge=0 (greater than or equal to 0),
        so negative values should raise a ValidationError.

        Requirements: 6.3, 6.8
        """
        # Arrange - Set row_count to negative value
        data = minimal_collector_output_data.copy()
        data["database_schema"]["tables"][0]["row_count"] = -100

        # Act & Assert
        with pytest.raises(ValidationError) as exc_info:
            CollectorOutputContract(**data)

        # Verify error message mentions the constraint violation
        error_str = str(exc_info.value)
        assert "row_count" in error_str.lower()
        assert any(
            keyword in error_str.lower()
            for keyword in ["greater", "equal", "0", "constraint", "validation"]
        )

    def test_backup_retention_days_exceeds_maximum_raises_validation_error(
        self, valid_collector_output_data
    ):
        """
        Test that backup_retention_days > 35 raises ValidationError.

        The backup_retention_days field has a constraint le=35 (less than or
        equal to 35), so values greater than 35 should raise a ValidationError.

        Requirements: 6.3, 6.8
        """
        # Arrange - Set backup_retention_days to value > 35
        data = valid_collector_output_data.copy()
        data["metadata"]["source_database"]["rds_instance_metadata"]["backup_retention_days"] = 40

        # Act & Assert
        with pytest.raises(ValidationError) as exc_info:
            CollectorOutputContract(**data)

        # Verify error message mentions the constraint violation
        error_str = str(exc_info.value)
        assert "backup_retention_days" in error_str.lower()
        assert any(
            keyword in error_str.lower()
            for keyword in ["less", "equal", "35", "constraint", "validation"]
        )

    def test_negative_size_mb_raises_validation_error(self, minimal_collector_output_data):
        """
        Test that negative size_mb raises ValidationError.

        The size_mb field has a constraint ge=0, so negative values should
        raise a ValidationError.

        Requirements: 6.3, 6.8
        """
        # Arrange - Add size_mb with negative value
        data = minimal_collector_output_data.copy()
        data["database_schema"]["tables"][0]["size_mb"] = -50.5

        # Act & Assert
        with pytest.raises(ValidationError) as exc_info:
            CollectorOutputContract(**data)

        # Verify error message mentions the constraint violation
        error_str = str(exc_info.value)
        assert "size_mb" in error_str.lower()
        assert any(
            keyword in error_str.lower()
            for keyword in ["greater", "equal", "0", "constraint", "validation"]
        )

    def test_negative_frequency_per_hour_raises_validation_error(
        self, minimal_collector_output_data
    ):
        """
        Test that negative frequency_per_hour raises ValidationError.

        The frequency_per_hour field has a constraint ge=0, so negative values
        should raise a ValidationError.

        Requirements: 6.3, 6.8
        """
        # Arrange - Set frequency_per_hour to negative value
        data = minimal_collector_output_data.copy()
        data["queries"]["query_patterns"][0]["frequency_per_hour"] = -10.0

        # Act & Assert
        with pytest.raises(ValidationError) as exc_info:
            CollectorOutputContract(**data)

        # Verify error message mentions the constraint violation
        error_str = str(exc_info.value)
        assert "frequency_per_hour" in error_str.lower()
        assert any(
            keyword in error_str.lower()
            for keyword in ["greater", "equal", "0", "constraint", "validation"]
        )

    def test_db_load_contribution_percent_exceeds_100_raises_validation_error(
        self, minimal_collector_output_data
    ):
        """
        Test that db_load_contribution_percent > 100 raises ValidationError.

        The db_load_contribution_percent field has constraints ge=0 and le=100,
        so values greater than 100 should raise a ValidationError.

        Requirements: 6.3, 6.8
        """
        # Arrange - Set db_load_contribution_percent to value > 100
        data = minimal_collector_output_data.copy()
        data["queries"]["query_patterns"][0]["db_load_contribution_percent"] = 150.0

        # Act & Assert
        with pytest.raises(ValidationError) as exc_info:
            CollectorOutputContract(**data)

        # Verify error message mentions the constraint violation
        error_str = str(exc_info.value)
        assert "db_load_contribution_percent" in error_str.lower()
        assert any(
            keyword in error_str.lower()
            for keyword in ["less", "equal", "100", "constraint", "validation"]
        )


class TestInvalidEnumValues:
    """Tests for invalid enum value validation."""

    def test_invalid_database_engine_raises_validation_error(self, minimal_collector_output_data):
        """
        Test that invalid DatabaseEngine value raises ValidationError.

        The engine field must be one of the valid DatabaseEngine enum values.
        Invalid values should raise a ValidationError with a message listing
        valid options.

        Requirements: 6.4, 6.8
        """
        # Arrange - Set engine to invalid value
        data = minimal_collector_output_data.copy()
        data["metadata"]["source_database"]["engine"] = "invalid_engine"

        # Act & Assert
        with pytest.raises(ValidationError) as exc_info:
            CollectorOutputContract(**data)

        # Verify error message mentions valid enum options
        error_str = str(exc_info.value)
        assert "engine" in error_str.lower()
        # Check for at least some valid enum values in error message
        assert any(
            engine in error_str.lower() for engine in ["mysql", "postgresql", "oracle", "sqlserver"]
        )

    def test_invalid_query_type_raises_validation_error(self, minimal_collector_output_data):
        """
        Test that invalid QueryType value raises ValidationError.

        The query_type field must be one of the valid QueryType enum values.
        Invalid values should raise a ValidationError with a message listing
        valid options.

        Requirements: 6.4, 6.8
        """
        # Arrange - Set query_type to invalid value
        data = minimal_collector_output_data.copy()
        data["queries"]["query_patterns"][0]["query_type"] = "INVALID_TYPE"

        # Act & Assert
        with pytest.raises(ValidationError) as exc_info:
            CollectorOutputContract(**data)

        # Verify error message mentions valid enum options
        error_str = str(exc_info.value)
        assert "query_type" in error_str.lower()
        # Check for at least some valid enum values in error message
        assert any(qtype in error_str for qtype in ["SELECT", "INSERT", "UPDATE", "DELETE"])

    def test_invalid_deployment_type_raises_validation_error(self, minimal_collector_output_data):
        """
        Test that invalid DeploymentType value raises ValidationError.

        The deployment_type field must be one of the valid DeploymentType enum
        values. Invalid values should raise a ValidationError.

        Requirements: 6.4, 6.8
        """
        # Arrange - Set deployment_type to invalid value
        data = minimal_collector_output_data.copy()
        data["metadata"]["source_database"]["deployment_type"] = "invalid_deployment"

        # Act & Assert
        with pytest.raises(ValidationError) as exc_info:
            CollectorOutputContract(**data)

        # Verify error message mentions valid enum options
        error_str = str(exc_info.value)
        assert "deployment_type" in error_str.lower()
        # Check for at least some valid enum values in error message
        assert any(
            dtype in error_str.lower()
            for dtype in ["rds_instance", "on_premises", "ec2_self_managed"]
        )

    def test_invalid_storage_type_raises_validation_error(self, valid_collector_output_data):
        """
        Test that invalid StorageType value raises ValidationError.

        The storage_type field must be one of the valid StorageType enum values.
        Invalid values should raise a ValidationError.

        Requirements: 6.4, 6.8
        """
        # Arrange - Set storage_type to invalid value
        data = valid_collector_output_data.copy()
        data["metadata"]["source_database"]["rds_instance_metadata"][
            "storage_type"
        ] = "invalid_storage"

        # Act & Assert
        with pytest.raises(ValidationError) as exc_info:
            CollectorOutputContract(**data)

        # Verify error message mentions valid enum options
        error_str = str(exc_info.value)
        assert "storage_type" in error_str.lower()
        # Check for at least some valid enum values in error message
        assert any(stype in error_str.lower() for stype in ["gp2", "gp3", "io1", "io2"])


class TestNullValuesInNonNullableFields:
    """Tests for null values in non-nullable fields."""

    def test_null_in_required_string_field_raises_validation_error(
        self, minimal_collector_output_data
    ):
        """
        Test that null in required string field raises ValidationError.

        Required string fields like job_id should not accept None values.

        Requirements: 6.5, 6.8
        """
        # Arrange - Set job_id to None
        data = minimal_collector_output_data.copy()
        data["job_id"] = None

        # Act & Assert
        with pytest.raises(ValidationError) as exc_info:
            CollectorOutputContract(**data)

        # Verify error message mentions the field cannot be null
        error_str = str(exc_info.value)
        assert "job_id" in error_str.lower()
        assert any(keyword in error_str.lower() for keyword in ["none", "null", "required"])

    def test_null_in_required_nested_model_raises_validation_error(
        self, minimal_collector_output_data
    ):
        """
        Test that null in required nested model raises ValidationError.

        Required nested models like metadata should not accept None values.

        Requirements: 6.5, 6.8
        """
        # Arrange - Set metadata to None
        data = minimal_collector_output_data.copy()
        data["metadata"] = None

        # Act & Assert
        with pytest.raises(ValidationError) as exc_info:
            CollectorOutputContract(**data)

        # Verify error message mentions the field cannot be null
        error_str = str(exc_info.value)
        assert "metadata" in error_str.lower()
        assert any(keyword in error_str.lower() for keyword in ["none", "null", "required"])

    def test_null_in_required_list_field_raises_validation_error(
        self, minimal_collector_output_data
    ):
        """
        Test that null in required list field raises ValidationError.

        Required list fields like tables should not accept None values.

        Requirements: 6.5, 6.8
        """
        # Arrange - Set tables to None
        data = minimal_collector_output_data.copy()
        data["database_schema"]["tables"] = None

        # Act & Assert
        with pytest.raises(ValidationError) as exc_info:
            CollectorOutputContract(**data)

        # Verify error message mentions the field cannot be null
        error_str = str(exc_info.value)
        assert "tables" in error_str.lower()
        assert any(keyword in error_str.lower() for keyword in ["none", "null", "required"])

    def test_null_in_non_nullable_column_field_raises_validation_error(
        self, minimal_collector_output_data
    ):
        """
        Test that null in non-nullable column field raises ValidationError.

        Non-nullable fields in nested models like column_name should not accept
        None values.

        Requirements: 6.5, 6.8
        """
        # Arrange - Set column_name to None
        data = minimal_collector_output_data.copy()
        data["database_schema"]["tables"][0]["columns"][0]["column_name"] = None

        # Act & Assert
        with pytest.raises(ValidationError) as exc_info:
            CollectorOutputContract(**data)

        # Verify error message mentions the field cannot be null
        error_str = str(exc_info.value)
        assert "column_name" in error_str.lower()
        assert any(keyword in error_str.lower() for keyword in ["none", "null", "required"])

    def test_null_in_optional_field_is_allowed(self, minimal_collector_output_data):
        """
        Test that null values are allowed in optional fields.

        Optional fields should accept None values without raising errors.

        Requirements: 6.5
        """
        # Arrange - Set optional field to None (it already is in minimal data)
        data = minimal_collector_output_data.copy()
        data["metadata"]["collection_duration_seconds"] = None

        # Act
        contract = CollectorOutputContract(**data)

        # Assert - Should succeed
        assert contract.metadata.collection_duration_seconds is None


class TestExtraFields:
    """Tests for handling extra fields not in the model."""

    def test_extra_fields_are_ignored(self, minimal_collector_output_data):
        """
        Test that extra fields are ignored by default (Pydantic behavior).

        Pydantic's default behavior is to ignore extra fields that are not
        defined in the model schema.

        Requirements: 6.6
        """
        # Arrange - Add extra fields at various levels
        data = minimal_collector_output_data.copy()
        data["extra_top_level_field"] = "should be ignored"
        data["another_extra_field"] = 12345
        data["metadata"]["extra_metadata_field"] = "also ignored"
        data["database_schema"]["extra_schema_field"] = {"nested": "data"}

        # Act
        contract = CollectorOutputContract(**data)

        # Assert - Contract should be created successfully
        assert contract is not None
        assert isinstance(contract, CollectorOutputContract)

        # Verify extra fields are not present in the contract
        assert not hasattr(contract, "extra_top_level_field")
        assert not hasattr(contract, "another_extra_field")
        assert not hasattr(contract.metadata, "extra_metadata_field")
        assert not hasattr(contract.database_schema, "extra_schema_field")

    def test_contract_can_be_instantiated_with_extra_fields(self, minimal_collector_output_data):
        """
        Test that contract can be instantiated with extra fields present.

        This test verifies that the presence of extra fields does not prevent
        successful contract instantiation.

        Requirements: 6.6
        """
        # Arrange - Add many extra fields
        data = minimal_collector_output_data.copy()
        data["unknown_field_1"] = "value1"
        data["unknown_field_2"] = "value2"
        data["unknown_field_3"] = "value3"
        data["metadata"]["unknown_metadata"] = "metadata_value"
        data["queries"]["unknown_queries"] = "queries_value"

        # Act
        contract = CollectorOutputContract(**data)

        # Assert - Should succeed and have correct required fields
        assert contract is not None
        assert contract.job_id == "minimal-job-001"
        assert contract.metadata.collector_version == "1.0.0"

    def test_extra_fields_dont_appear_in_serialized_output(self, minimal_collector_output_data):
        """
        Test that extra fields don't appear in serialized output.

        When a contract is serialized using model_dump(), extra fields that
        were present in the input should not appear in the output.

        Requirements: 6.6
        """
        # Arrange - Add extra fields
        data = minimal_collector_output_data.copy()
        data["extra_field"] = "should not appear in output"
        data["metadata"]["extra_metadata"] = "also should not appear"

        # Act
        contract = CollectorOutputContract(**data)
        dumped_dict = contract.model_dump()

        # Assert - Extra fields should not be in serialized output
        assert "extra_field" not in dumped_dict
        assert "extra_metadata" not in dumped_dict["metadata"]

        # Verify required fields are still present
        assert "job_id" in dumped_dict
        assert "metadata" in dumped_dict
        assert "collector_version" in dumped_dict["metadata"]

    def test_extra_fields_in_nested_models_are_ignored(self, minimal_collector_output_data):
        """
        Test that extra fields in nested models are ignored.

        This test verifies that extra fields at any nesting level are properly
        ignored without causing errors.

        Requirements: 6.6
        """
        # Arrange - Add extra fields in deeply nested structures
        data = minimal_collector_output_data.copy()
        data["database_schema"]["tables"][0]["extra_table_field"] = "ignored"
        data["database_schema"]["tables"][0]["columns"][0]["extra_column_field"] = "also ignored"
        data["queries"]["query_patterns"][0]["extra_query_field"] = "ignored too"

        # Act
        contract = CollectorOutputContract(**data)

        # Assert - Contract should be created successfully
        assert contract is not None
        table = contract.database_schema.tables[0]
        column = table.columns[0]
        query = contract.queries.query_patterns[0]

        # Verify extra fields are not present
        assert not hasattr(table, "extra_table_field")
        assert not hasattr(column, "extra_column_field")
        assert not hasattr(query, "extra_query_field")
