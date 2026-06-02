"""
Unit tests for CollectorOutputContract validation.

This module tests the CollectorOutputContract Pydantic model to ensure:
- Valid data creates contract instances successfully
- Contracts with all optional fields work correctly
- Contracts with minimal required fields work correctly
"""

import pytest

from src.contracts.collector_output import CollectorOutputContract


class TestCollectorOutputContractInstantiation:
    """Tests for creating CollectorOutputContract instances with various data."""

    def test_valid_contract_instantiation(self, valid_collector_output_data):
        """
        Test that valid data creates a contract instance successfully.

        This test validates that a complete, valid contract with all required
        fields and many optional fields can be instantiated without errors.

        Requirements: 2.2, 2.6
        """
        # Act
        contract = CollectorOutputContract(**valid_collector_output_data)

        # Assert
        assert contract is not None
        assert contract.contract_version == "3.0"
        assert contract.job_id == "test-job-12345"
        assert contract.metadata is not None
        assert contract.database_schema is not None
        assert contract.queries is not None
        assert contract.metrics is not None

        # Verify nested structures
        assert contract.metadata.collector_version == "1.2.3"
        assert contract.metadata.source_database.engine == "postgresql"
        assert len(contract.database_schema.tables) == 2
        assert len(contract.queries.query_patterns) == 2
        assert contract.metrics.performance_metrics is not None

    def test_contract_with_all_optional_fields(self, valid_collector_output_data):
        """
        Test that a contract with all optional fields populated works correctly.

        This test ensures that when all optional fields are provided with valid
        values, the contract instantiates successfully and retains all data.

        Requirements: 2.2, 2.6
        """
        # Act
        contract = CollectorOutputContract(**valid_collector_output_data)

        # Assert - Verify optional fields in metadata
        assert contract.metadata.collection_duration_seconds == 45.5
        assert contract.metadata.source_database.database_name == "production_db"
        assert contract.metadata.source_database.database_size_gb == 250.5
        assert contract.metadata.source_database.deployment_type == "rds_instance"
        assert contract.metadata.source_database.rds_instance_metadata is not None

        # Verify optional RDS metadata fields
        rds_metadata = contract.metadata.source_database.rds_instance_metadata
        assert rds_metadata.db_instance_identifier == "prod-db-instance"
        assert rds_metadata.instance_class == "db.r5.xlarge"
        assert rds_metadata.vcpu_count == 4
        assert rds_metadata.memory_gb == 32.0
        assert rds_metadata.storage_type == "gp3"
        assert rds_metadata.multi_az is True
        assert rds_metadata.performance_insights_enabled is True

        # Verify optional schema fields
        assert contract.database_schema.views is not None
        assert len(contract.database_schema.views) == 1
        assert contract.database_schema.procedures is not None
        assert len(contract.database_schema.procedures) == 1
        assert contract.database_schema.triggers is not None
        assert len(contract.database_schema.triggers) == 1

        # Verify optional table fields
        first_table = contract.database_schema.tables[0]
        assert first_table.size_mb == 50.5
        assert first_table.indexes is not None
        assert first_table.primary_key is not None

        # Verify optional query fields
        first_query = contract.queries.query_patterns[0]
        assert first_query.query_type == "SELECT"
        assert first_query.calls_per_second == 0.33
        assert first_query.rows_returned_avg == 1.0
        assert first_query.execution_time_ms_avg == 2.5
        assert first_query.has_joins is False

        # Verify optional metrics fields
        assert contract.metrics.rds_cloudwatch_metrics is not None
        assert contract.metrics.rds_cloudwatch_metrics.cpu_utilization is not None
        assert contract.metrics.performance_metrics.avg_query_time_ms == 5.5

    def test_contract_with_minimal_required_fields(self, minimal_collector_output_data):
        """
        Test that a contract with only required fields works correctly.

        This test ensures that optional fields are truly optional and a valid
        contract can be created with just the minimum required data.

        Requirements: 2.2, 2.6
        """
        # Act
        contract = CollectorOutputContract(**minimal_collector_output_data)

        # Assert - Verify required fields are present
        assert contract.contract_version == "3.0"
        assert contract.job_id == "minimal-job-001"
        assert contract.metadata is not None
        assert contract.database_schema is not None
        assert contract.queries is not None
        assert contract.metrics is not None

        # Verify minimal metadata
        assert contract.metadata.collection_timestamp is not None
        assert contract.metadata.collector_version == "1.0.0"
        assert contract.metadata.source_database.engine == "mysql"
        assert contract.metadata.source_database.version == "8.0.32"
        assert contract.metadata.source_database.hostname == "test-db.local"

        # Verify optional fields are None or empty
        assert contract.metadata.collection_duration_seconds is None
        assert contract.metadata.source_database.database_name is None
        assert contract.metadata.source_database.database_size_gb is None
        assert contract.metadata.source_database.deployment_type is None
        assert contract.metadata.source_database.rds_instance_metadata is None

        # Verify minimal schema
        assert len(contract.database_schema.tables) == 1
        assert contract.database_schema.views is None
        assert contract.database_schema.procedures is None
        assert contract.database_schema.triggers is None

        # Verify minimal table
        table = contract.database_schema.tables[0]
        assert table.table_id == "test.simple_table"
        assert table.table_name == "simple_table"
        assert table.row_count == 100
        assert len(table.columns) == 1
        assert table.size_mb is None
        assert table.indexes is None
        assert table.primary_key is None
        assert table.foreign_keys is None

        # Verify minimal queries
        assert len(contract.queries.query_patterns) == 1
        query = contract.queries.query_patterns[0]
        assert query.query_id == "q1"
        assert query.query_text == "SELECT * FROM simple_table"
        assert query.frequency_per_hour == 10.0
        assert query.query_type is None
        assert query.calls_per_second is None

        # Verify minimal metrics
        assert contract.metrics.performance_metrics is not None
        assert contract.metrics.rds_cloudwatch_metrics is None
        assert contract.metrics.performance_metrics.avg_query_time_ms is None

    def test_contract_instantiation_preserves_data_types(self, valid_collector_output_data):
        """
        Test that contract instantiation preserves correct data types.

        This test ensures that Pydantic correctly converts and validates
        data types during instantiation.

        Requirements: 2.2, 2.6
        """
        # Act
        contract = CollectorOutputContract(**valid_collector_output_data)

        # Assert - Verify data types
        assert isinstance(contract.contract_version, str)
        assert isinstance(contract.job_id, str)
        assert isinstance(contract.metadata.collection_duration_seconds, float)
        assert isinstance(contract.metadata.source_database.database_size_gb, float)

        # Verify integer types
        table = contract.database_schema.tables[0]
        assert isinstance(table.row_count, int)
        assert isinstance(table.size_mb, float)

        # Verify enum types
        assert isinstance(contract.metadata.source_database.engine.value, str)
        assert contract.metadata.source_database.engine.value == "postgresql"

        # Verify list types
        assert isinstance(contract.database_schema.tables, list)
        assert isinstance(contract.queries.query_patterns, list)

        # Verify nested model types
        from src.contracts.collector_output import Metadata, Metrics, Queries, Schema

        assert isinstance(contract.metadata, Metadata)
        assert isinstance(contract.database_schema, Schema)
        assert isinstance(contract.queries, Queries)
        assert isinstance(contract.metrics, Metrics)

    def test_contract_with_default_contract_version(self):
        """
        Test that contract_version defaults to "3.0" when not provided.

        This test verifies that the contract_version field has a proper
        default value and doesn't need to be explicitly provided.

        Requirements: 2.2, 2.6
        """
        # Arrange - Create data without contract_version
        data = {
            "job_id": "test-job",
            "metadata": {
                "collection_timestamp": "2024-01-15T10:00:00Z",
                "collector_version": "1.0.0",
                "source_database": {
                    "engine": "mysql",
                    "version": "8.0.32",
                    "hostname": "test-db.local",
                },
            },
            "database_schema": {
                "tables": [
                    {
                        "table_id": "test.table",
                        "table_name": "table",
                        "row_count": 100,
                        "columns": [
                            {
                                "column_name": "id",
                                "data_type": "int",
                                "nullable": False,
                            }
                        ],
                    }
                ]
            },
            "queries": {
                "query_patterns": [
                    {
                        "query_id": "q1",
                        "query_text": "SELECT * FROM table",
                        "frequency_per_hour": 10.0,
                        "tables_accessed": ["test.table"],
                    }
                ]
            },
            "metrics": {
                "performance_metrics": {},
            },
        }

        # Act
        contract = CollectorOutputContract(**data)

        # Assert
        assert contract.contract_version == "3.0"


class TestCollectorOutputContractSerialization:
    """Tests for JSON serialization of CollectorOutputContract."""

    def test_model_dump_produces_correct_dictionary(self, valid_collector_output_data):
        """
        Test that model_dump() produces a correct dictionary representation.

        This test verifies that the Pydantic model_dump() method correctly
        serializes a contract instance to a Python dictionary with all fields
        properly represented.

        Requirements: 2.4
        """
        # Arrange
        contract = CollectorOutputContract(**valid_collector_output_data)

        # Act
        dumped_dict = contract.model_dump()

        # Assert - Verify it's a dictionary
        assert isinstance(dumped_dict, dict)

        # Verify top-level fields are present
        assert "contract_version" in dumped_dict
        assert "job_id" in dumped_dict
        assert "metadata" in dumped_dict
        assert "database_schema" in dumped_dict
        assert "queries" in dumped_dict
        assert "metrics" in dumped_dict

        # Verify values match original data
        assert dumped_dict["contract_version"] == "3.0"
        assert dumped_dict["job_id"] == "test-job-12345"

        # Verify nested structures are dictionaries
        assert isinstance(dumped_dict["metadata"], dict)
        assert isinstance(dumped_dict["database_schema"], dict)
        assert isinstance(dumped_dict["queries"], dict)
        assert isinstance(dumped_dict["metrics"], dict)

        # Verify nested data is correct
        assert dumped_dict["metadata"]["collector_version"] == "1.2.3"
        assert dumped_dict["metadata"]["source_database"]["engine"] == "postgresql"
        assert len(dumped_dict["database_schema"]["tables"]) == 2
        assert len(dumped_dict["queries"]["query_patterns"]) == 2

    def test_model_dump_json_produces_valid_json_string(self, valid_collector_output_data):
        """
        Test that model_dump_json() produces a valid JSON string.

        This test verifies that the Pydantic model_dump_json() method correctly
        serializes a contract instance to a JSON string that is valid and
        properly formatted.

        Requirements: 2.4
        """
        # Arrange
        contract = CollectorOutputContract(**valid_collector_output_data)

        # Act
        json_string = contract.model_dump_json()

        # Assert - Verify it's a string
        assert isinstance(json_string, str)

        # Verify it's not empty
        assert len(json_string) > 0

        # Verify it contains expected keys (basic check)
        assert '"contract_version"' in json_string
        assert '"job_id"' in json_string
        assert '"metadata"' in json_string
        assert '"database_schema"' in json_string
        assert '"queries"' in json_string
        assert '"metrics"' in json_string

        # Verify it contains expected values
        assert '"3.0"' in json_string
        assert '"test-job-12345"' in json_string
        assert '"postgresql"' in json_string

    def test_serialized_json_can_be_parsed(self, valid_collector_output_data):
        """
        Test that serialized JSON can be parsed back into a Python object.

        This test verifies that the JSON string produced by model_dump_json()
        is valid JSON that can be parsed by the standard json module.

        Requirements: 2.4
        """
        import json

        # Arrange
        contract = CollectorOutputContract(**valid_collector_output_data)
        json_string = contract.model_dump_json()

        # Act
        parsed_data = json.loads(json_string)

        # Assert - Verify parsing succeeded and produced a dictionary
        assert isinstance(parsed_data, dict)

        # Verify all top-level keys are present
        assert "contract_version" in parsed_data
        assert "job_id" in parsed_data
        assert "metadata" in parsed_data
        assert "database_schema" in parsed_data
        assert "queries" in parsed_data
        assert "metrics" in parsed_data

        # Verify values are correct
        assert parsed_data["contract_version"] == "3.0"
        assert parsed_data["job_id"] == "test-job-12345"
        assert parsed_data["metadata"]["collector_version"] == "1.2.3"
        assert parsed_data["metadata"]["source_database"]["engine"] == "postgresql"

    def test_model_dump_with_minimal_data(self, minimal_collector_output_data):
        """
        Test that model_dump() works correctly with minimal required fields.

        This test verifies that serialization works even when only required
        fields are present, and optional fields are properly handled (None or omitted).

        Requirements: 2.4
        """
        # Arrange
        contract = CollectorOutputContract(**minimal_collector_output_data)

        # Act
        dumped_dict = contract.model_dump()

        # Assert - Verify required fields are present
        assert dumped_dict["contract_version"] == "3.0"
        assert dumped_dict["job_id"] == "minimal-job-001"
        assert dumped_dict["metadata"]["collector_version"] == "1.0.0"

        # Verify optional fields are None or not present in nested structures
        assert dumped_dict["metadata"]["collection_duration_seconds"] is None
        assert dumped_dict["metadata"]["source_database"]["database_name"] is None
        assert dumped_dict["database_schema"]["views"] is None
        assert dumped_dict["metrics"]["rds_cloudwatch_metrics"] is None

    def test_model_dump_json_with_minimal_data(self, minimal_collector_output_data):
        """
        Test that model_dump_json() works correctly with minimal required fields.

        This test verifies that JSON serialization works even when only required
        fields are present.

        Requirements: 2.4
        """
        import json

        # Arrange
        contract = CollectorOutputContract(**minimal_collector_output_data)

        # Act
        json_string = contract.model_dump_json()
        parsed_data = json.loads(json_string)

        # Assert - Verify required fields are present in JSON
        assert parsed_data["contract_version"] == "3.0"
        assert parsed_data["job_id"] == "minimal-job-001"
        assert parsed_data["metadata"]["collector_version"] == "1.0.0"

        # Verify optional fields are null in JSON
        assert parsed_data["metadata"]["collection_duration_seconds"] is None
        assert parsed_data["metadata"]["source_database"]["database_name"] is None

    def test_model_dump_preserves_data_types(self, valid_collector_output_data):
        """
        Test that model_dump() preserves correct Python data types.

        This test verifies that serialization to dictionary maintains proper
        Python types (int, float, str, bool, list, dict) rather than converting
        everything to strings.

        Requirements: 2.4
        """
        # Arrange
        contract = CollectorOutputContract(**valid_collector_output_data)

        # Act
        dumped_dict = contract.model_dump()

        # Assert - Verify string types
        assert isinstance(dumped_dict["contract_version"], str)
        assert isinstance(dumped_dict["job_id"], str)
        assert isinstance(dumped_dict["metadata"]["collector_version"], str)

        # Verify numeric types
        assert isinstance(dumped_dict["metadata"]["collection_duration_seconds"], float)
        assert isinstance(dumped_dict["metadata"]["source_database"]["database_size_gb"], float)

        # Verify integer types
        table = dumped_dict["database_schema"]["tables"][0]
        assert isinstance(table["row_count"], int)

        # Verify boolean types
        rds_metadata = dumped_dict["metadata"]["source_database"]["rds_instance_metadata"]
        assert isinstance(rds_metadata["multi_az"], bool)
        assert isinstance(rds_metadata["performance_insights_enabled"], bool)

        # Verify list types
        assert isinstance(dumped_dict["database_schema"]["tables"], list)
        assert isinstance(dumped_dict["queries"]["query_patterns"], list)

        # Verify enum values are serialized as strings
        assert isinstance(dumped_dict["metadata"]["source_database"]["engine"], str)
        assert dumped_dict["metadata"]["source_database"]["engine"] == "postgresql"

    def test_model_dump_json_datetime_serialization(self, valid_collector_output_data):
        """
        Test that datetime fields are properly serialized to ISO 8601 format in JSON.

        This test verifies that datetime objects are converted to ISO 8601 strings
        in the JSON output, as specified in the model configuration.

        Requirements: 2.4
        """
        import json

        # Arrange
        contract = CollectorOutputContract(**valid_collector_output_data)

        # Act
        json_string = contract.model_dump_json()
        parsed_data = json.loads(json_string)

        # Assert - Verify datetime fields are ISO 8601 strings
        collection_timestamp = parsed_data["metadata"]["collection_timestamp"]
        assert isinstance(collection_timestamp, str)
        assert "T" in collection_timestamp  # ISO 8601 format includes 'T'
        assert collection_timestamp.endswith("Z")  # UTC timezone indicator

        # Verify query datetime fields if present
        first_query = parsed_data["queries"]["query_patterns"][0]
        if first_query.get("first_seen"):
            assert isinstance(first_query["first_seen"], str)
            assert "T" in first_query["first_seen"]
        if first_query.get("last_seen"):
            assert isinstance(first_query["last_seen"], str)
            assert "T" in first_query["last_seen"]

    def test_model_dump_with_exclude_none(self, minimal_collector_output_data):
        """
        Test that model_dump() can exclude None values when requested.

        This test verifies that the exclude_none parameter works correctly,
        which is useful for producing cleaner output without null fields.

        Requirements: 2.4
        """
        # Arrange
        contract = CollectorOutputContract(**minimal_collector_output_data)

        # Act
        dumped_dict = contract.model_dump(exclude_none=True)

        # Assert - Verify None values are excluded at top level
        # Note: Pydantic's exclude_none works recursively
        metadata = dumped_dict["metadata"]

        # collection_duration_seconds should not be present (it's None)
        assert "collection_duration_seconds" not in metadata

        # Required fields should still be present
        assert "collection_timestamp" in metadata
        assert "collector_version" in metadata
        assert "source_database" in metadata


class TestCollectorOutputContractDeserialization:
    """Tests for JSON deserialization of CollectorOutputContract."""

    def test_model_validate_from_dictionary(self, valid_collector_output_data):
        """
        Test that model_validate() can deserialize from a dictionary.

        This test verifies that the Pydantic model_validate() method correctly
        deserializes a Python dictionary into a contract instance.

        Requirements: 2.5
        """
        # Act
        contract = CollectorOutputContract.model_validate(valid_collector_output_data)

        # Assert - Verify contract was created successfully
        assert contract is not None
        assert isinstance(contract, CollectorOutputContract)

        # Verify top-level fields
        assert contract.contract_version == "3.0"
        assert contract.job_id == "test-job-12345"

        # Verify nested structures were deserialized correctly
        assert contract.metadata is not None
        assert contract.metadata.collector_version == "1.2.3"
        assert contract.metadata.source_database.engine.value == "postgresql"
        assert len(contract.database_schema.tables) == 2
        assert len(contract.queries.query_patterns) == 2

    def test_model_validate_json_from_json_string(self, valid_collector_output_data):
        """
        Test that model_validate_json() can deserialize from a JSON string.

        This test verifies that the Pydantic model_validate_json() method correctly
        deserializes a JSON string into a contract instance.

        Requirements: 2.5
        """
        import json

        # Arrange - Create a JSON string from the test data
        json_string = json.dumps(valid_collector_output_data)

        # Act
        contract = CollectorOutputContract.model_validate_json(json_string)

        # Assert - Verify contract was created successfully
        assert contract is not None
        assert isinstance(contract, CollectorOutputContract)

        # Verify top-level fields
        assert contract.contract_version == "3.0"
        assert contract.job_id == "test-job-12345"

        # Verify nested structures were deserialized correctly
        assert contract.metadata is not None
        assert contract.metadata.collector_version == "1.2.3"
        assert contract.metadata.source_database.engine.value == "postgresql"
        assert len(contract.database_schema.tables) == 2
        assert len(contract.queries.query_patterns) == 2

    def test_deserialization_with_extra_fields(self, valid_collector_output_data):
        """
        Test that deserialization handles extra fields correctly.

        This test verifies that Pydantic's default behavior of ignoring extra
        fields works correctly during deserialization. Extra fields should be
        silently ignored without causing errors.

        Requirements: 2.5
        """
        # Arrange - Add extra fields to the data
        data_with_extra = valid_collector_output_data.copy()
        data_with_extra["extra_field_1"] = "should be ignored"
        data_with_extra["extra_field_2"] = 12345
        data_with_extra["metadata"]["extra_metadata_field"] = "also ignored"

        # Act
        contract = CollectorOutputContract.model_validate(data_with_extra)

        # Assert - Verify contract was created successfully
        assert contract is not None
        assert isinstance(contract, CollectorOutputContract)

        # Verify extra fields are not present in the contract
        assert not hasattr(contract, "extra_field_1")
        assert not hasattr(contract, "extra_field_2")
        assert not hasattr(contract.metadata, "extra_metadata_field")

        # Verify required fields are still present and correct
        assert contract.contract_version == "3.0"
        assert contract.job_id == "test-job-12345"
        assert contract.metadata.collector_version == "1.2.3"

    def test_model_validate_with_minimal_data(self, minimal_collector_output_data):
        """
        Test that model_validate() works with minimal required fields.

        This test verifies that deserialization works correctly when only
        required fields are present in the input data.

        Requirements: 2.5
        """
        # Act
        contract = CollectorOutputContract.model_validate(minimal_collector_output_data)

        # Assert - Verify contract was created successfully
        assert contract is not None
        assert isinstance(contract, CollectorOutputContract)

        # Verify required fields
        assert contract.contract_version == "3.0"
        assert contract.job_id == "minimal-job-001"
        assert contract.metadata.collector_version == "1.0.0"

        # Verify optional fields are None
        assert contract.metadata.collection_duration_seconds is None
        assert contract.metadata.source_database.database_name is None
        assert contract.database_schema.views is None

    def test_model_validate_json_with_minimal_data(self, minimal_collector_output_data):
        """
        Test that model_validate_json() works with minimal required fields.

        This test verifies that JSON deserialization works correctly when only
        required fields are present in the JSON string.

        Requirements: 2.5
        """
        import json

        # Arrange
        json_string = json.dumps(minimal_collector_output_data)

        # Act
        contract = CollectorOutputContract.model_validate_json(json_string)

        # Assert - Verify contract was created successfully
        assert contract is not None
        assert isinstance(contract, CollectorOutputContract)

        # Verify required fields
        assert contract.contract_version == "3.0"
        assert contract.job_id == "minimal-job-001"
        assert contract.metadata.collector_version == "1.0.0"

    def test_deserialization_preserves_data_types(self, valid_collector_output_data):
        """
        Test that deserialization preserves correct data types.

        This test verifies that Pydantic correctly converts data types during
        deserialization, ensuring integers remain integers, floats remain floats,
        strings remain strings, etc.

        Requirements: 2.5
        """
        # Act
        contract = CollectorOutputContract.model_validate(valid_collector_output_data)

        # Assert - Verify string types
        assert isinstance(contract.contract_version, str)
        assert isinstance(contract.job_id, str)
        assert isinstance(contract.metadata.collector_version, str)

        # Verify numeric types
        assert isinstance(contract.metadata.collection_duration_seconds, float)
        assert isinstance(contract.metadata.source_database.database_size_gb, float)

        # Verify integer types
        table = contract.database_schema.tables[0]
        assert isinstance(table.row_count, int)

        # Verify boolean types
        rds_metadata = contract.metadata.source_database.rds_instance_metadata
        assert isinstance(rds_metadata.multi_az, bool)
        assert isinstance(rds_metadata.performance_insights_enabled, bool)

        # Verify list types
        assert isinstance(contract.database_schema.tables, list)
        assert isinstance(contract.queries.query_patterns, list)

        # Verify enum types
        from src.contracts.collector_output import DatabaseEngine

        assert isinstance(contract.metadata.source_database.engine, DatabaseEngine)

    def test_deserialization_datetime_parsing(self, valid_collector_output_data):
        """
        Test that datetime strings are correctly parsed during deserialization.

        This test verifies that ISO 8601 datetime strings in the input data
        are correctly parsed into datetime objects by Pydantic.

        Requirements: 2.5
        """
        from datetime import datetime

        # Act
        contract = CollectorOutputContract.model_validate(valid_collector_output_data)

        # Assert - Verify datetime fields are parsed correctly
        assert isinstance(contract.metadata.collection_timestamp, datetime)

        # Verify the datetime value is correct
        expected_timestamp = datetime.fromisoformat("2024-01-15T10:30:00+00:00")
        assert contract.metadata.collection_timestamp == expected_timestamp

        # Verify query datetime fields if present
        first_query = contract.queries.query_patterns[0]
        if first_query.first_seen:
            assert isinstance(first_query.first_seen, datetime)
        if first_query.last_seen:
            assert isinstance(first_query.last_seen, datetime)

    def test_deserialization_with_extra_fields_in_nested_models(self, valid_collector_output_data):
        """
        Test that extra fields in nested models are ignored during deserialization.

        This test verifies that Pydantic's extra field handling works correctly
        for nested models, not just the top-level contract.

        Requirements: 2.5
        """
        # Arrange - Add extra fields to nested structures
        data_with_extra = valid_collector_output_data.copy()
        data_with_extra["metadata"]["extra_metadata_field"] = "ignored"
        data_with_extra["database_schema"]["extra_schema_field"] = "ignored"
        data_with_extra["database_schema"]["tables"][0]["extra_table_field"] = "ignored"
        data_with_extra["queries"]["query_patterns"][0]["extra_query_field"] = "ignored"

        # Act
        contract = CollectorOutputContract.model_validate(data_with_extra)

        # Assert - Verify contract was created successfully
        assert contract is not None

        # Verify extra fields are not present in nested models
        assert not hasattr(contract.metadata, "extra_metadata_field")
        assert not hasattr(contract.database_schema, "extra_schema_field")
        assert not hasattr(contract.database_schema.tables[0], "extra_table_field")
        assert not hasattr(contract.queries.query_patterns[0], "extra_query_field")

        # Verify required fields are still correct
        assert contract.metadata.collector_version == "1.2.3"
        assert contract.database_schema.tables[0].table_name == "users"
        assert contract.queries.query_patterns[0].query_id == "q1-select-users"

    def test_model_validate_json_with_extra_fields(self, valid_collector_output_data):
        """
        Test that model_validate_json() handles extra fields correctly.

        This test verifies that extra fields in a JSON string are ignored
        during deserialization, just like with model_validate().

        Requirements: 2.5
        """
        import json

        # Arrange - Add extra fields and convert to JSON
        data_with_extra = valid_collector_output_data.copy()
        data_with_extra["extra_field"] = "should be ignored"
        json_string = json.dumps(data_with_extra)

        # Act
        contract = CollectorOutputContract.model_validate_json(json_string)

        # Assert - Verify contract was created successfully
        assert contract is not None
        assert not hasattr(contract, "extra_field")
        assert contract.contract_version == "3.0"
        assert contract.job_id == "test-job-12345"

    def test_deserialization_round_trip_consistency(self, valid_collector_output_data):
        """
        Test that serialization followed by deserialization produces equivalent objects.

        This test verifies the round-trip consistency: creating a contract,
        serializing it to JSON, and deserializing it back should produce an
        equivalent contract with the same field values.

        Requirements: 2.5
        """
        # Arrange - Create original contract
        original_contract = CollectorOutputContract(**valid_collector_output_data)

        # Act - Serialize and deserialize
        json_string = original_contract.model_dump_json()
        deserialized_contract = CollectorOutputContract.model_validate_json(json_string)

        # Assert - Verify contracts are equivalent
        assert deserialized_contract.contract_version == original_contract.contract_version
        assert deserialized_contract.job_id == original_contract.job_id
        assert (
            deserialized_contract.metadata.collector_version
            == original_contract.metadata.collector_version
        )
        assert (
            deserialized_contract.metadata.source_database.engine
            == original_contract.metadata.source_database.engine
        )
        assert len(deserialized_contract.database_schema.tables) == len(
            original_contract.database_schema.tables
        )
        assert len(deserialized_contract.queries.query_patterns) == len(
            original_contract.queries.query_patterns
        )

        # Verify nested data is equivalent
        assert (
            deserialized_contract.database_schema.tables[0].table_name
            == original_contract.database_schema.tables[0].table_name
        )
        assert (
            deserialized_contract.queries.query_patterns[0].query_id
            == original_contract.queries.query_patterns[0].query_id
        )


class TestNestedModels:
    """Tests for nested Pydantic models within CollectorOutputContract."""

    def test_metadata_model_instantiation(self):
        """
        Test that Metadata model can be instantiated independently.

        This test verifies that the Metadata nested model works correctly
        when instantiated on its own, with all required and optional fields.

        Requirements: 2.7
        """
        from src.contracts.collector_output import Metadata, SourceDatabase

        # Arrange - Create metadata with all fields
        metadata_data = {
            "collection_timestamp": "2024-01-15T10:30:00Z",
            "collector_version": "1.2.3",
            "collection_duration_seconds": 45.5,
            "source_database": {
                "engine": "postgresql",
                "version": "14.7",
                "hostname": "test-db.example.com",
                "database_name": "test_db",
                "database_size_gb": 100.5,
                "deployment_type": "rds_instance",
            },
        }

        # Act
        metadata = Metadata(**metadata_data)

        # Assert - Verify all fields are set correctly
        assert metadata is not None
        assert metadata.collector_version == "1.2.3"
        assert metadata.collection_duration_seconds == 45.5
        assert isinstance(metadata.source_database, SourceDatabase)
        assert metadata.source_database.engine.value == "postgresql"
        assert metadata.source_database.version == "14.7"
        assert metadata.source_database.hostname == "test-db.example.com"
        assert metadata.source_database.database_name == "test_db"
        assert metadata.source_database.database_size_gb == 100.5

    def test_source_database_model_instantiation(self):
        """
        Test that SourceDatabase model can be instantiated independently.

        This test verifies that the SourceDatabase nested model works correctly
        with required fields and optional RDS metadata.

        Requirements: 2.7
        """
        from src.contracts.collector_output import RDSInstanceMetadata, SourceDatabase

        # Arrange - Create source database with RDS metadata
        source_db_data = {
            "engine": "mysql",
            "version": "8.0.32",
            "hostname": "prod-mysql.example.com",
            "database_name": "production",
            "database_size_gb": 500.0,
            "deployment_type": "rds_instance",
            "rds_instance_metadata": {
                "db_instance_identifier": "prod-mysql-01",
                "instance_class": "db.r5.2xlarge",
                "vcpu_count": 8,
                "memory_gb": 64.0,
                "storage_type": "gp3",
                "storage_size_gb": 1000,
                "multi_az": True,
                "region": "us-west-2",
                "performance_insights_enabled": True,
                "enhanced_monitoring_interval": 60,
            },
        }

        # Act
        source_db = SourceDatabase(**source_db_data)

        # Assert - Verify all fields are set correctly
        assert source_db is not None
        assert source_db.engine.value == "mysql"
        assert source_db.version == "8.0.32"
        assert source_db.hostname == "prod-mysql.example.com"
        assert source_db.database_name == "production"
        assert source_db.database_size_gb == 500.0
        assert source_db.deployment_type.value == "rds_instance"

        # Verify RDS metadata
        assert isinstance(source_db.rds_instance_metadata, RDSInstanceMetadata)
        assert source_db.rds_instance_metadata.db_instance_identifier == "prod-mysql-01"
        assert source_db.rds_instance_metadata.instance_class == "db.r5.2xlarge"
        assert source_db.rds_instance_metadata.vcpu_count == 8
        assert source_db.rds_instance_metadata.memory_gb == 64.0
        assert source_db.rds_instance_metadata.storage_type.value == "gp3"
        assert source_db.rds_instance_metadata.multi_az is True
        assert source_db.rds_instance_metadata.performance_insights_enabled is True

    def test_schema_model_with_tables(self):
        """
        Test that Schema model works correctly with tables.

        This test verifies that the Schema model can be instantiated with
        a list of tables and that table data is preserved correctly.

        Requirements: 2.7
        """
        from src.contracts.collector_output import Schema

        # Arrange - Create schema with multiple tables
        schema_data = {
            "tables": [
                {
                    "table_id": "public.users",
                    "table_name": "users",
                    "schema_name": "public",
                    "row_count": 1000,
                    "size_mb": 10.5,
                    "columns": [
                        {
                            "column_name": "id",
                            "data_type": "integer",
                            "nullable": False,
                        },
                        {
                            "column_name": "name",
                            "data_type": "varchar",
                            "nullable": True,
                        },
                    ],
                },
                {
                    "table_id": "public.orders",
                    "table_name": "orders",
                    "schema_name": "public",
                    "row_count": 5000,
                    "columns": [
                        {
                            "column_name": "order_id",
                            "data_type": "integer",
                            "nullable": False,
                        }
                    ],
                },
            ]
        }

        # Act
        schema = Schema(**schema_data)

        # Assert - Verify schema and tables
        assert schema is not None
        assert len(schema.tables) == 2
        assert schema.tables[0].table_name == "users"
        assert schema.tables[0].row_count == 1000
        assert schema.tables[0].size_mb == 10.5
        assert len(schema.tables[0].columns) == 2
        assert schema.tables[1].table_name == "orders"
        assert schema.tables[1].row_count == 5000

    def test_schema_model_with_views(self):
        """
        Test that Schema model works correctly with views.

        This test verifies that the Schema model can include view definitions
        and that view data is preserved correctly.

        Requirements: 2.7
        """
        from src.contracts.collector_output import Schema

        # Arrange - Create schema with views
        schema_data = {
            "tables": [
                {
                    "table_id": "public.users",
                    "table_name": "users",
                    "row_count": 100,
                    "columns": [
                        {
                            "column_name": "id",
                            "data_type": "integer",
                            "nullable": False,
                        }
                    ],
                }
            ],
            "views": [
                {
                    "view_id": "public.active_users",
                    "view_name": "active_users",
                    "schema_name": "public",
                    "definition": "SELECT * FROM users WHERE active = true",
                    "is_updatable": False,
                    "referenced_tables": ["public.users"],
                    "column_list": ["id", "name", "email"],
                }
            ],
        }

        # Act
        schema = Schema(**schema_data)

        # Assert - Verify views
        assert schema is not None
        assert schema.views is not None
        assert len(schema.views) == 1
        assert schema.views[0].view_name == "active_users"
        assert schema.views[0].schema_name == "public"
        assert schema.views[0].is_updatable is False  # nosemgrep: is-function-without-parentheses
        assert schema.views[0].referenced_tables == ["public.users"]
        assert len(schema.views[0].column_list) == 3

    def test_schema_model_with_procedures(self):
        """
        Test that Schema model works correctly with procedures.

        This test verifies that the Schema model can include stored procedures
        and functions with their definitions and parameters.

        Requirements: 2.7
        """
        from src.contracts.collector_output import Schema

        # Arrange - Create schema with procedures
        schema_data = {
            "tables": [
                {
                    "table_id": "public.users",
                    "table_name": "users",
                    "row_count": 100,
                    "columns": [
                        {
                            "column_name": "id",
                            "data_type": "integer",
                            "nullable": False,
                        }
                    ],
                }
            ],
            "procedures": [
                {
                    "procedure_id": "public.get_user",
                    "procedure_name": "get_user",
                    "schema_name": "public",
                    "procedure_type": "FUNCTION",
                    "definition": "CREATE FUNCTION get_user(user_id INT) RETURNS TABLE(...)",
                    "language": "plpgsql",
                    "return_type": "TABLE",
                    "parameters": [
                        {
                            "parameter_name": "user_id",
                            "data_type": "integer",
                            "parameter_mode": "IN",
                        }
                    ],
                    "referenced_tables": ["public.users"],
                }
            ],
        }

        # Act
        schema = Schema(**schema_data)

        # Assert - Verify procedures
        assert schema is not None
        assert schema.procedures is not None
        assert len(schema.procedures) == 1
        assert schema.procedures[0].procedure_name == "get_user"
        assert schema.procedures[0].procedure_type.value == "FUNCTION"
        assert schema.procedures[0].language == "plpgsql"
        assert schema.procedures[0].return_type == "TABLE"
        assert len(schema.procedures[0].parameters) == 1
        assert schema.procedures[0].parameters[0].parameter_name == "user_id"
        assert schema.procedures[0].parameters[0].parameter_mode.value == "IN"

    def test_schema_model_with_triggers(self):
        """
        Test that Schema model works correctly with triggers.

        This test verifies that the Schema model can include trigger definitions
        with their event types, timing, and execution details.

        Requirements: 2.7
        """
        from src.contracts.collector_output import Schema

        # Arrange - Create schema with triggers
        schema_data = {
            "tables": [
                {
                    "table_id": "public.users",
                    "table_name": "users",
                    "row_count": 100,
                    "columns": [
                        {
                            "column_name": "id",
                            "data_type": "integer",
                            "nullable": False,
                        }
                    ],
                }
            ],
            "triggers": [
                {
                    "trigger_id": "public.audit_user_changes",
                    "trigger_name": "audit_user_changes",
                    "schema_name": "public",
                    "table_id": "public.users",
                    "event_type": "UPDATE",
                    "timing": "AFTER",
                    "for_each": "ROW",
                    "definition": "CREATE TRIGGER audit_user_changes AFTER UPDATE ON users...",
                    "is_enabled": True,
                }
            ],
        }

        # Act
        schema = Schema(**schema_data)

        # Assert - Verify triggers
        assert schema is not None
        assert schema.triggers is not None
        assert len(schema.triggers) == 1
        assert schema.triggers[0].trigger_name == "audit_user_changes"
        assert schema.triggers[0].table_id == "public.users"
        assert schema.triggers[0].event_type.value == "UPDATE"
        assert schema.triggers[0].timing.value == "AFTER"
        assert schema.triggers[0].for_each.value == "ROW"
        assert schema.triggers[0].is_enabled is True  # nosemgrep: is-function-without-parentheses

    def test_table_model_with_columns(self):
        """
        Test that Table model works correctly with columns.

        This test verifies that the Table model can include column definitions
        with various data types and constraints.

        Requirements: 2.7
        """
        from src.contracts.collector_output import Table

        # Arrange - Create table with multiple columns
        table_data = {
            "table_id": "public.products",
            "table_name": "products",
            "schema_name": "public",
            "row_count": 10000,
            "size_mb": 50.0,
            "columns": [
                {
                    "column_name": "id",
                    "ordinal_position": 1,
                    "data_type": "integer",
                    "normalized_data_type": "integer",
                    "nullable": False,
                    "is_auto_increment": True,
                    "cardinality": 10000,
                },
                {
                    "column_name": "name",
                    "ordinal_position": 2,
                    "data_type": "varchar",
                    "normalized_data_type": "string",
                    "max_length": 255,
                    "nullable": False,
                    "cardinality": 9500,
                },
                {
                    "column_name": "price",
                    "ordinal_position": 3,
                    "data_type": "numeric",
                    "normalized_data_type": "decimal",
                    "nullable": False,
                    "default_value": 0.0,
                },
                {
                    "column_name": "created_at",
                    "ordinal_position": 4,
                    "data_type": "timestamp",
                    "normalized_data_type": "timestamp",
                    "nullable": False,
                    "default_value": "CURRENT_TIMESTAMP",
                },
            ],
        }

        # Act
        table = Table(**table_data)

        # Assert - Verify table and columns
        assert table is not None
        assert table.table_name == "products"
        assert table.row_count == 10000
        assert len(table.columns) == 4

        # Verify first column (id)
        assert table.columns[0].column_name == "id"
        assert table.columns[0].ordinal_position == 1
        assert table.columns[0].data_type == "integer"
        assert table.columns[0].normalized_data_type.value == "integer"
        assert table.columns[0].nullable is False
        # nosemgrep: is-function-without-parentheses
        assert table.columns[0].is_auto_increment is True

        # Verify second column (name)
        assert table.columns[1].column_name == "name"
        assert table.columns[1].max_length == 255
        assert table.columns[1].normalized_data_type.value == "string"

        # Verify third column (price)
        assert table.columns[2].column_name == "price"
        assert table.columns[2].default_value == 0.0
        assert table.columns[2].normalized_data_type.value == "decimal"

    def test_table_model_with_indexes(self):
        """
        Test that Table model works correctly with indexes.

        This test verifies that the Table model can include index definitions
        with various types and uniqueness constraints.

        Requirements: 2.7
        """
        from src.contracts.collector_output import Table

        # Arrange - Create table with indexes
        table_data = {
            "table_id": "public.users",
            "table_name": "users",
            "row_count": 1000,
            "columns": [
                {
                    "column_name": "id",
                    "data_type": "integer",
                    "nullable": False,
                }
            ],
            "indexes": [
                {
                    "index_name": "users_pkey",
                    "columns": ["id"],
                    "is_unique": True,
                    "is_primary": True,
                    "index_type": "btree",
                },
                {
                    "index_name": "idx_users_email",
                    "columns": ["email"],
                    "is_unique": True,
                    "index_type": "btree",
                },
                {
                    "index_name": "idx_users_name_email",
                    "columns": ["name", "email"],
                    "is_unique": False,
                    "index_type": "btree",
                },
            ],
            "primary_key": ["id"],
        }

        # Act
        table = Table(**table_data)

        # Assert - Verify indexes
        assert table is not None
        assert table.indexes is not None
        assert len(table.indexes) == 3

        # Verify primary key index
        assert table.indexes[0].index_name == "users_pkey"
        assert table.indexes[0].columns == ["id"]
        assert table.indexes[0].is_unique is True  # nosemgrep: is-function-without-parentheses
        assert table.indexes[0].is_primary is True  # nosemgrep: is-function-without-parentheses
        assert table.indexes[0].index_type.value == "btree"

        # Verify unique index
        assert table.indexes[1].index_name == "idx_users_email"
        assert table.indexes[1].is_unique is True  # nosemgrep: is-function-without-parentheses

        # Verify composite index
        assert table.indexes[2].index_name == "idx_users_name_email"
        assert len(table.indexes[2].columns) == 2
        assert table.indexes[2].is_unique is False  # nosemgrep: is-function-without-parentheses

    def test_table_model_with_foreign_keys(self):
        """
        Test that Table model works correctly with foreign keys.

        This test verifies that the Table model can include foreign key
        constraints with referential actions.

        Requirements: 2.7
        """
        from src.contracts.collector_output import Table

        # Arrange - Create table with foreign keys
        table_data = {
            "table_id": "public.orders",
            "table_name": "orders",
            "row_count": 5000,
            "columns": [
                {
                    "column_name": "order_id",
                    "data_type": "integer",
                    "nullable": False,
                },
                {
                    "column_name": "user_id",
                    "data_type": "integer",
                    "nullable": False,
                },
                {
                    "column_name": "product_id",
                    "data_type": "integer",
                    "nullable": False,
                },
            ],
            "foreign_keys": [
                {
                    "constraint_name": "fk_orders_user",
                    "columns": ["user_id"],
                    "referenced_table": "public.users",
                    "referenced_columns": ["id"],
                    "on_delete": "CASCADE",
                    "on_update": "CASCADE",
                },
                {
                    "constraint_name": "fk_orders_product",
                    "columns": ["product_id"],
                    "referenced_table": "public.products",
                    "referenced_columns": ["id"],
                    "on_delete": "RESTRICT",
                    "on_update": "NO ACTION",
                },
            ],
        }

        # Act
        table = Table(**table_data)

        # Assert - Verify foreign keys
        assert table is not None
        assert table.foreign_keys is not None
        assert len(table.foreign_keys) == 2

        # Verify first foreign key
        assert table.foreign_keys[0].constraint_name == "fk_orders_user"
        assert table.foreign_keys[0].columns == ["user_id"]
        assert table.foreign_keys[0].referenced_table == "public.users"
        assert table.foreign_keys[0].referenced_columns == ["id"]
        assert table.foreign_keys[0].on_delete.value == "CASCADE"
        assert table.foreign_keys[0].on_update.value == "CASCADE"

        # Verify second foreign key
        assert table.foreign_keys[1].constraint_name == "fk_orders_product"
        assert table.foreign_keys[1].columns == ["product_id"]
        assert table.foreign_keys[1].referenced_table == "public.products"
        assert table.foreign_keys[1].on_delete.value == "RESTRICT"
        assert table.foreign_keys[1].on_update.value == "NO ACTION"


class TestEnumValidation:
    """Tests for enum validation in CollectorOutputContract."""

    def test_valid_database_engine_values(self):
        """
        Test that all valid DatabaseEngine enum values are accepted.

        This test verifies that each valid database engine value can be used
        in the contract without raising a ValidationError.

        Requirements: 2.8
        """

        # Arrange - List of all valid database engine values
        valid_engines = [
            "mysql",
            "postgresql",
            "mariadb",
            "sqlserver",
            "oracle",
            "db2",
        ]

        # Act & Assert - Test each valid engine value
        for engine_value in valid_engines:
            data = {
                "job_id": "test-job",
                "metadata": {
                    "collection_timestamp": "2024-01-15T10:00:00Z",
                    "collector_version": "1.0.0",
                    "source_database": {
                        "engine": engine_value,
                        "version": "1.0",
                        "hostname": "test-db.local",
                    },
                },
                "database_schema": {
                    "tables": [
                        {
                            "table_id": "test.table",
                            "table_name": "table",
                            "row_count": 100,
                            "columns": [
                                {
                                    "column_name": "id",
                                    "data_type": "int",
                                    "nullable": False,
                                }
                            ],
                        }
                    ]
                },
                "queries": {
                    "query_patterns": [
                        {
                            "query_id": "q1",
                            "query_text": "SELECT * FROM table",
                            "frequency_per_hour": 10.0,
                            "tables_accessed": ["test.table"],
                        }
                    ]
                },
                "metrics": {
                    "performance_metrics": {},
                },
            }

            # Should not raise ValidationError
            contract = CollectorOutputContract(**data)
            assert contract.metadata.source_database.engine.value == engine_value

    def test_valid_deployment_type_values(self):
        """
        Test that all valid DeploymentType enum values are accepted.

        This test verifies that each valid deployment type value can be used
        in the contract without raising a ValidationError.

        Requirements: 2.8
        """

        # Arrange - List of all valid deployment type values
        valid_deployment_types = [
            "rds_instance",
            "on_premises",
            "ec2_self_managed",
        ]

        # Act & Assert - Test each valid deployment type
        for deployment_type_value in valid_deployment_types:
            data = {
                "job_id": "test-job",
                "metadata": {
                    "collection_timestamp": "2024-01-15T10:00:00Z",
                    "collector_version": "1.0.0",
                    "source_database": {
                        "engine": "mysql",
                        "version": "8.0.32",
                        "hostname": "test-db.local",
                        "deployment_type": deployment_type_value,
                    },
                },
                "database_schema": {
                    "tables": [
                        {
                            "table_id": "test.table",
                            "table_name": "table",
                            "row_count": 100,
                            "columns": [
                                {
                                    "column_name": "id",
                                    "data_type": "int",
                                    "nullable": False,
                                }
                            ],
                        }
                    ]
                },
                "queries": {
                    "query_patterns": [
                        {
                            "query_id": "q1",
                            "query_text": "SELECT * FROM table",
                            "frequency_per_hour": 10.0,
                            "tables_accessed": ["test.table"],
                        }
                    ]
                },
                "metrics": {
                    "performance_metrics": {},
                },
            }

            # Should not raise ValidationError
            contract = CollectorOutputContract(**data)
            assert contract.metadata.source_database.deployment_type.value == deployment_type_value

    def test_valid_query_type_values(self):
        """
        Test that all valid QueryType enum values are accepted.

        This test verifies that each valid query type value can be used
        in the contract without raising a ValidationError.

        Requirements: 2.8
        """

        # Arrange - List of all valid query type values
        valid_query_types = [
            "SELECT",
            "INSERT",
            "UPDATE",
            "DELETE",
            "MERGE",
            "OTHER",
        ]

        # Act & Assert - Test each valid query type
        for query_type_value in valid_query_types:
            data = {
                "job_id": "test-job",
                "metadata": {
                    "collection_timestamp": "2024-01-15T10:00:00Z",
                    "collector_version": "1.0.0",
                    "source_database": {
                        "engine": "mysql",
                        "version": "8.0.32",
                        "hostname": "test-db.local",
                    },
                },
                "database_schema": {
                    "tables": [
                        {
                            "table_id": "test.table",
                            "table_name": "table",
                            "row_count": 100,
                            "columns": [
                                {
                                    "column_name": "id",
                                    "data_type": "int",
                                    "nullable": False,
                                }
                            ],
                        }
                    ]
                },
                "queries": {
                    "query_patterns": [
                        {
                            "query_id": "q1",
                            "query_text": "SELECT * FROM table",
                            "frequency_per_hour": 10.0,
                            "tables_accessed": ["test.table"],
                            "query_type": query_type_value,
                        }
                    ]
                },
                "metrics": {
                    "performance_metrics": {},
                },
            }

            # Should not raise ValidationError
            contract = CollectorOutputContract(**data)
            assert contract.queries.query_patterns[0].query_type.value == query_type_value

    def test_invalid_database_engine_raises_validation_error(self):
        """
        Test that invalid DatabaseEngine values raise ValidationError.

        This test verifies that attempting to use an invalid database engine
        value raises a ValidationError with an appropriate error message.

        Requirements: 2.8
        """
        import pytest
        from pydantic import ValidationError

        # Arrange - Invalid database engine values
        invalid_engines = [
            "invalid_engine",
            "mongodb",
            "redis",
            "cassandra",
            "MYSQL",  # Case sensitive
            "",
            "postgres",  # Should be "postgresql"
        ]

        # Act & Assert - Test each invalid engine value
        for invalid_engine in invalid_engines:
            data = {
                "job_id": "test-job",
                "metadata": {
                    "collection_timestamp": "2024-01-15T10:00:00Z",
                    "collector_version": "1.0.0",
                    "source_database": {
                        "engine": invalid_engine,
                        "version": "1.0",
                        "hostname": "test-db.local",
                    },
                },
                "database_schema": {
                    "tables": [
                        {
                            "table_id": "test.table",
                            "table_name": "table",
                            "row_count": 100,
                            "columns": [
                                {
                                    "column_name": "id",
                                    "data_type": "int",
                                    "nullable": False,
                                }
                            ],
                        }
                    ]
                },
                "queries": {
                    "query_patterns": [
                        {
                            "query_id": "q1",
                            "query_text": "SELECT * FROM table",
                            "frequency_per_hour": 10.0,
                            "tables_accessed": ["test.table"],
                        }
                    ]
                },
                "metrics": {
                    "performance_metrics": {},
                },
            }

            # Should raise ValidationError
            with pytest.raises(ValidationError) as exc_info:
                CollectorOutputContract(**data)

            # Verify error message mentions the field and provides valid options
            error_message = str(exc_info.value)
            assert "engine" in error_message.lower()

    def test_invalid_deployment_type_raises_validation_error(self):
        """
        Test that invalid DeploymentType values raise ValidationError.

        This test verifies that attempting to use an invalid deployment type
        value raises a ValidationError with an appropriate error message.

        Requirements: 2.8
        """
        import pytest
        from pydantic import ValidationError

        # Arrange - Invalid deployment type values
        invalid_deployment_types = [
            "invalid_deployment",
            "cloud",
            "aws",
            "RDS_INSTANCE",  # Case sensitive
            "",
            "rds",
        ]

        # Act & Assert - Test each invalid deployment type
        for invalid_type in invalid_deployment_types:
            data = {
                "job_id": "test-job",
                "metadata": {
                    "collection_timestamp": "2024-01-15T10:00:00Z",
                    "collector_version": "1.0.0",
                    "source_database": {
                        "engine": "mysql",
                        "version": "8.0.32",
                        "hostname": "test-db.local",
                        "deployment_type": invalid_type,
                    },
                },
                "database_schema": {
                    "tables": [
                        {
                            "table_id": "test.table",
                            "table_name": "table",
                            "row_count": 100,
                            "columns": [
                                {
                                    "column_name": "id",
                                    "data_type": "int",
                                    "nullable": False,
                                }
                            ],
                        }
                    ]
                },
                "queries": {
                    "query_patterns": [
                        {
                            "query_id": "q1",
                            "query_text": "SELECT * FROM table",
                            "frequency_per_hour": 10.0,
                            "tables_accessed": ["test.table"],
                        }
                    ]
                },
                "metrics": {
                    "performance_metrics": {},
                },
            }

            # Should raise ValidationError
            with pytest.raises(ValidationError) as exc_info:
                CollectorOutputContract(**data)

            # Verify error message mentions the field
            error_message = str(exc_info.value)
            assert "deployment_type" in error_message.lower()

    def test_invalid_query_type_raises_validation_error(self):
        """
        Test that invalid QueryType values raise ValidationError.

        This test verifies that attempting to use an invalid query type
        value raises a ValidationError with an appropriate error message.

        Requirements: 2.8
        """
        import pytest
        from pydantic import ValidationError

        # Arrange - Invalid query type values
        invalid_query_types = [
            "invalid_query",
            "TRUNCATE",
            "DROP",
            "CREATE",
            "select",  # Case sensitive
            "",
            "QUERY",
        ]

        # Act & Assert - Test each invalid query type
        for invalid_type in invalid_query_types:
            data = {
                "job_id": "test-job",
                "metadata": {
                    "collection_timestamp": "2024-01-15T10:00:00Z",
                    "collector_version": "1.0.0",
                    "source_database": {
                        "engine": "mysql",
                        "version": "8.0.32",
                        "hostname": "test-db.local",
                    },
                },
                "database_schema": {
                    "tables": [
                        {
                            "table_id": "test.table",
                            "table_name": "table",
                            "row_count": 100,
                            "columns": [
                                {
                                    "column_name": "id",
                                    "data_type": "int",
                                    "nullable": False,
                                }
                            ],
                        }
                    ]
                },
                "queries": {
                    "query_patterns": [
                        {
                            "query_id": "q1",
                            "query_text": "SELECT * FROM table",
                            "frequency_per_hour": 10.0,
                            "tables_accessed": ["test.table"],
                            "query_type": invalid_type,
                        }
                    ]
                },
                "metrics": {
                    "performance_metrics": {},
                },
            }

            # Should raise ValidationError
            with pytest.raises(ValidationError) as exc_info:
                CollectorOutputContract(**data)

            # Verify error message mentions the field
            error_message = str(exc_info.value)
            assert "query_type" in error_message.lower()

    def test_enum_values_are_case_sensitive(self):
        """
        Test that enum values are case-sensitive.

        This test verifies that enum validation is case-sensitive and that
        using incorrect casing raises a ValidationError.

        Requirements: 2.8
        """
        import pytest
        from pydantic import ValidationError

        # Test DatabaseEngine case sensitivity
        data = {
            "job_id": "test-job",
            "metadata": {
                "collection_timestamp": "2024-01-15T10:00:00Z",
                "collector_version": "1.0.0",
                "source_database": {
                    "engine": "MySQL",  # Should be "mysql"
                    "version": "8.0.32",
                    "hostname": "test-db.local",
                },
            },
            "database_schema": {
                "tables": [
                    {
                        "table_id": "test.table",
                        "table_name": "table",
                        "row_count": 100,
                        "columns": [
                            {
                                "column_name": "id",
                                "data_type": "int",
                                "nullable": False,
                            }
                        ],
                    }
                ]
            },
            "queries": {
                "query_patterns": [
                    {
                        "query_id": "q1",
                        "query_text": "SELECT * FROM table",
                        "frequency_per_hour": 10.0,
                        "tables_accessed": ["test.table"],
                    }
                ]
            },
            "metrics": {
                "performance_metrics": {},
            },
        }

        # Should raise ValidationError due to incorrect casing
        with pytest.raises(ValidationError):
            CollectorOutputContract(**data)

    def test_enum_serialization_produces_string_values(self):
        """
        Test that enum values are serialized as strings.

        This test verifies that when a contract with enum values is serialized,
        the enum values are converted to their string representations.

        Requirements: 2.8
        """
        # Arrange
        data = {
            "job_id": "test-job",
            "metadata": {
                "collection_timestamp": "2024-01-15T10:00:00Z",
                "collector_version": "1.0.0",
                "source_database": {
                    "engine": "postgresql",
                    "version": "14.7",
                    "hostname": "test-db.local",
                    "deployment_type": "rds_instance",
                },
            },
            "database_schema": {
                "tables": [
                    {
                        "table_id": "test.table",
                        "table_name": "table",
                        "row_count": 100,
                        "columns": [
                            {
                                "column_name": "id",
                                "data_type": "int",
                                "nullable": False,
                            }
                        ],
                    }
                ]
            },
            "queries": {
                "query_patterns": [
                    {
                        "query_id": "q1",
                        "query_text": "SELECT * FROM table",
                        "frequency_per_hour": 10.0,
                        "tables_accessed": ["test.table"],
                        "query_type": "SELECT",
                    }
                ]
            },
            "metrics": {
                "performance_metrics": {},
            },
        }

        # Act
        contract = CollectorOutputContract(**data)
        dumped_dict = contract.model_dump()

        # Assert - Verify enum values are serialized as strings
        assert isinstance(dumped_dict["metadata"]["source_database"]["engine"], str)
        assert dumped_dict["metadata"]["source_database"]["engine"] == "postgresql"
        assert isinstance(dumped_dict["metadata"]["source_database"]["deployment_type"], str)
        assert dumped_dict["metadata"]["source_database"]["deployment_type"] == "rds_instance"
        assert isinstance(dumped_dict["queries"]["query_patterns"][0]["query_type"], str)
        assert dumped_dict["queries"]["query_patterns"][0]["query_type"] == "SELECT"


class TestFieldValidators:
    """Tests for custom field validators in CollectorOutputContract."""

    def test_enhanced_monitoring_interval_valid_values(self):
        """
        Test that enhanced_monitoring_interval validator accepts valid values.

        This test verifies that the enhanced_monitoring_interval field accepts
        all valid monitoring interval values: 0, 1, 5, 10, 15, 30, 60.

        Requirements: 2.9
        """

        from src.contracts.collector_output import RDSInstanceMetadata

        # Arrange - List of all valid monitoring interval values
        valid_intervals = [0, 1, 5, 10, 15, 30, 60]

        # Act & Assert - Test each valid interval value
        for interval in valid_intervals:
            rds_metadata_data = {
                "db_instance_identifier": "test-instance",
                "instance_class": "db.t3.micro",
                "enhanced_monitoring_interval": interval,
            }

            # Should not raise ValidationError
            rds_metadata = RDSInstanceMetadata(**rds_metadata_data)
            assert rds_metadata.enhanced_monitoring_interval == interval

    def test_enhanced_monitoring_interval_invalid_values(self):
        """
        Test that enhanced_monitoring_interval validator rejects invalid values.

        This test verifies that the enhanced_monitoring_interval field raises
        a ValidationError when given values that are not in the allowed set.

        Requirements: 2.9
        """
        from pydantic import ValidationError

        from src.contracts.collector_output import RDSInstanceMetadata

        # Arrange - List of invalid monitoring interval values
        invalid_intervals = [2, 3, 7, 20, 45, 90, 120, -1, 100]

        # Act & Assert - Test each invalid interval value
        for interval in invalid_intervals:
            rds_metadata_data = {
                "db_instance_identifier": "test-instance",
                "instance_class": "db.t3.micro",
                "enhanced_monitoring_interval": interval,
            }

            # Should raise ValidationError
            try:
                RDSInstanceMetadata(**rds_metadata_data)
                # If we get here, the test failed
                pytest.fail(
                    f"Expected ValidationError for interval {interval}, but none was raised"
                )
            except ValidationError as e:
                # Verify error message mentions the valid values
                error_message = str(e)
                assert "Enhanced monitoring interval must be one of" in error_message
                assert "0, 1, 5, 10, 15, 30, 60" in error_message

    def test_enhanced_monitoring_interval_none_is_valid(self):
        """
        Test that enhanced_monitoring_interval accepts None (optional field).

        This test verifies that the enhanced_monitoring_interval field can be
        None since it's an optional field.

        Requirements: 2.9
        """
        from src.contracts.collector_output import RDSInstanceMetadata

        # Arrange - Create RDS metadata without enhanced_monitoring_interval
        rds_metadata_data = {
            "db_instance_identifier": "test-instance",
            "instance_class": "db.t3.micro",
        }

        # Act
        rds_metadata = RDSInstanceMetadata(**rds_metadata_data)

        # Assert - Should be None by default
        assert rds_metadata.enhanced_monitoring_interval is None

    def test_contract_version_pattern_validator_valid_values(self):
        """
        Test that contract_version pattern validator accepts valid MAJOR.MINOR format.

        This test verifies that the contract_version field accepts strings in
        the MAJOR.MINOR format (e.g., "1.0", "2.5", "10.99").

        Requirements: 2.9
        """

        # Arrange - List of valid contract version values
        valid_versions = ["1.0", "2.0", "3.0", "10.5", "99.99", "0.1"]

        # Act & Assert - Test each valid version
        for version in valid_versions:
            data = {
                "contract_version": version,
                "job_id": "test-job",
                "metadata": {
                    "collection_timestamp": "2024-01-15T10:00:00Z",
                    "collector_version": "1.0.0",
                    "source_database": {
                        "engine": "mysql",
                        "version": "8.0.32",
                        "hostname": "test-db.local",
                    },
                },
                "database_schema": {
                    "tables": [
                        {
                            "table_id": "test.table",
                            "table_name": "table",
                            "row_count": 100,
                            "columns": [
                                {
                                    "column_name": "id",
                                    "data_type": "int",
                                    "nullable": False,
                                }
                            ],
                        }
                    ]
                },
                "queries": {
                    "query_patterns": [
                        {
                            "query_id": "q1",
                            "query_text": "SELECT * FROM table",
                            "frequency_per_hour": 10.0,
                            "tables_accessed": ["test.table"],
                        }
                    ]
                },
                "metrics": {
                    "performance_metrics": {},
                },
            }

            # Should not raise ValidationError
            contract = CollectorOutputContract(**data)
            assert contract.contract_version == version

    def test_contract_version_pattern_validator_invalid_values(self):
        """
        Test that contract_version pattern validator rejects invalid formats.

        This test verifies that the contract_version field raises a ValidationError
        when given strings that don't match the MAJOR.MINOR format.

        Requirements: 2.9
        """
        from pydantic import ValidationError

        # Arrange - List of invalid contract version values
        invalid_versions = [
            "1",  # Missing minor version
            "1.0.0",  # Too many parts (semantic version)
            "v1.0",  # Prefix not allowed
            "1.0-beta",  # Suffix not allowed
            "abc",  # Not a number
            "1.x",  # Non-numeric minor version
            "",  # Empty string
            "1.",  # Missing minor number
            ".0",  # Missing major number
        ]

        # Act & Assert - Test each invalid version
        for version in invalid_versions:
            data = {
                "contract_version": version,
                "job_id": "test-job",
                "metadata": {
                    "collection_timestamp": "2024-01-15T10:00:00Z",
                    "collector_version": "1.0.0",
                    "source_database": {
                        "engine": "mysql",
                        "version": "8.0.32",
                        "hostname": "test-db.local",
                    },
                },
                "database_schema": {
                    "tables": [
                        {
                            "table_id": "test.table",
                            "table_name": "table",
                            "row_count": 100,
                            "columns": [
                                {
                                    "column_name": "id",
                                    "data_type": "int",
                                    "nullable": False,
                                }
                            ],
                        }
                    ]
                },
                "queries": {
                    "query_patterns": [
                        {
                            "query_id": "q1",
                            "query_text": "SELECT * FROM table",
                            "frequency_per_hour": 10.0,
                            "tables_accessed": ["test.table"],
                        }
                    ]
                },
                "metrics": {
                    "performance_metrics": {},
                },
            }

            # Should raise ValidationError
            try:
                CollectorOutputContract(**data)
                # If we get here, the test failed
                pytest.fail(
                    f"Expected ValidationError for version '{version}', but none was raised"
                )
            except ValidationError as e:
                # Verify error message mentions the pattern or validation issue
                error_message = str(e)
                assert "contract_version" in error_message.lower()

    def test_collector_version_pattern_validator_valid_values(self):
        """
        Test that collector_version pattern validator accepts valid semantic versions.

        This test verifies that the collector_version field accepts strings in
        the semantic version format MAJOR.MINOR.PATCH (e.g., "1.0.0", "2.5.10").

        Requirements: 2.9
        """

        # Arrange - List of valid collector version values
        valid_versions = ["1.0.0", "2.5.10", "10.99.88", "0.0.1", "100.200.300"]

        # Act & Assert - Test each valid version
        for version in valid_versions:
            data = {
                "job_id": "test-job",
                "metadata": {
                    "collection_timestamp": "2024-01-15T10:00:00Z",
                    "collector_version": version,
                    "source_database": {
                        "engine": "mysql",
                        "version": "8.0.32",
                        "hostname": "test-db.local",
                    },
                },
                "database_schema": {
                    "tables": [
                        {
                            "table_id": "test.table",
                            "table_name": "table",
                            "row_count": 100,
                            "columns": [
                                {
                                    "column_name": "id",
                                    "data_type": "int",
                                    "nullable": False,
                                }
                            ],
                        }
                    ]
                },
                "queries": {
                    "query_patterns": [
                        {
                            "query_id": "q1",
                            "query_text": "SELECT * FROM table",
                            "frequency_per_hour": 10.0,
                            "tables_accessed": ["test.table"],
                        }
                    ]
                },
                "metrics": {
                    "performance_metrics": {},
                },
            }

            # Should not raise ValidationError
            contract = CollectorOutputContract(**data)
            assert contract.metadata.collector_version == version

    def test_collector_version_pattern_validator_invalid_values(self):
        """
        Test that collector_version pattern validator rejects invalid formats.

        This test verifies that the collector_version field raises a ValidationError
        when given strings that don't match the semantic version format.

        Requirements: 2.9
        """
        from pydantic import ValidationError

        # Arrange - List of invalid collector version values
        invalid_versions = [
            "1.0",  # Missing patch version
            "1",  # Only major version
            "1.0.0.0",  # Too many parts
            "v1.0.0",  # Prefix not allowed
            "1.0.0-beta",  # Pre-release suffix not allowed
            "1.0.0+build",  # Build metadata not allowed
            "abc",  # Not a number
            "1.x.0",  # Non-numeric minor version
            "1.0.x",  # Non-numeric patch version
            "",  # Empty string
            "1.0.",  # Missing patch number
            "1..0",  # Missing minor number
        ]

        # Act & Assert - Test each invalid version
        for version in invalid_versions:
            data = {
                "job_id": "test-job",
                "metadata": {
                    "collection_timestamp": "2024-01-15T10:00:00Z",
                    "collector_version": version,
                    "source_database": {
                        "engine": "mysql",
                        "version": "8.0.32",
                        "hostname": "test-db.local",
                    },
                },
                "database_schema": {
                    "tables": [
                        {
                            "table_id": "test.table",
                            "table_name": "table",
                            "row_count": 100,
                            "columns": [
                                {
                                    "column_name": "id",
                                    "data_type": "int",
                                    "nullable": False,
                                }
                            ],
                        }
                    ]
                },
                "queries": {
                    "query_patterns": [
                        {
                            "query_id": "q1",
                            "query_text": "SELECT * FROM table",
                            "frequency_per_hour": 10.0,
                            "tables_accessed": ["test.table"],
                        }
                    ]
                },
                "metrics": {
                    "performance_metrics": {},
                },
            }

            # Should raise ValidationError
            try:
                CollectorOutputContract(**data)
                # If we get here, the test failed
                pytest.fail(
                    f"Expected ValidationError for version '{version}', but none was raised"
                )
            except ValidationError as e:
                # Verify error message mentions the pattern or validation issue
                error_message = str(e)
                assert "collector_version" in error_message.lower()

    def test_enhanced_monitoring_interval_validator_in_full_contract(self):
        """
        Test enhanced_monitoring_interval validator within a full contract.

        This test verifies that the validator works correctly when the
        RDSInstanceMetadata is nested within a full CollectorOutputContract.

        Requirements: 2.9
        """
        from pydantic import ValidationError

        # Arrange - Create contract with valid monitoring interval
        data = {
            "job_id": "test-job",
            "metadata": {
                "collection_timestamp": "2024-01-15T10:00:00Z",
                "collector_version": "1.0.0",
                "source_database": {
                    "engine": "mysql",
                    "version": "8.0.32",
                    "hostname": "test-db.local",
                    "deployment_type": "rds_instance",
                    "rds_instance_metadata": {
                        "db_instance_identifier": "test-instance",
                        "instance_class": "db.t3.micro",
                        "enhanced_monitoring_interval": 60,  # Valid value
                    },
                },
            },
            "database_schema": {
                "tables": [
                    {
                        "table_id": "test.table",
                        "table_name": "table",
                        "row_count": 100,
                        "columns": [
                            {
                                "column_name": "id",
                                "data_type": "int",
                                "nullable": False,
                            }
                        ],
                    }
                ]
            },
            "queries": {
                "query_patterns": [
                    {
                        "query_id": "q1",
                        "query_text": "SELECT * FROM table",
                        "frequency_per_hour": 10.0,
                        "tables_accessed": ["test.table"],
                    }
                ]
            },
            "metrics": {
                "performance_metrics": {},
            },
        }

        # Act - Should not raise ValidationError
        contract = CollectorOutputContract(**data)

        # Assert
        assert (
            contract.metadata.source_database.rds_instance_metadata.enhanced_monitoring_interval
            == 60
        )

        # Now test with invalid value
        data["metadata"]["source_database"]["rds_instance_metadata"][
            "enhanced_monitoring_interval"
        ] = 99  # Invalid value

        # Should raise ValidationError
        try:
            CollectorOutputContract(**data)
            pytest.fail("Expected ValidationError for invalid monitoring interval")
        except ValidationError as e:
            error_message = str(e)
            assert "Enhanced monitoring interval must be one of" in error_message
