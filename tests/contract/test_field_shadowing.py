"""
Tests for field shadowing resolution in CollectorOutputContract.

This test module verifies that:
1. Importing CollectorOutputContract produces no warnings
2. The database_schema field works correctly
3. The old schema field name is not present

Requirements: 3.1, 3.2, 3.3
"""

import warnings

from src.contracts.collector_output import CollectorOutputContract


class TestFieldShadowingResolution:
    """Test suite for field shadowing resolution."""

    def test_import_produces_no_warnings(self):
        """
        Test that importing CollectorOutputContract produces no warnings.

        This verifies that the field rename from 'schema' to 'database_schema'
        successfully resolved the field shadowing warning.

        Requirements: 3.1, 3.2
        """
        # Capture warnings during import
        with warnings.catch_warnings(record=True) as warning_list:
            warnings.simplefilter("always")

            # Re-import the module to trigger any warnings
            import importlib

            import src.contracts.collector_output

            importlib.reload(src.contracts.collector_output)

            # Check for field shadowing warnings
            shadowing_warnings = [
                w
                for w in warning_list
                if "shadows an attribute" in str(w.message).lower()
                or "field name" in str(w.message).lower()
            ]

            assert len(shadowing_warnings) == 0, (
                f"Expected no field shadowing warnings, but found {len(shadowing_warnings)}: "
                f"{[str(w.message) for w in shadowing_warnings]}"
            )

    def test_database_schema_field_exists(self):
        """
        Test that the database_schema field exists and works correctly.

        Requirements: 3.3, 3.4
        """
        # Create minimal valid contract data
        contract_data = {
            "contract_version": "3.0",
            "job_id": "test-job-123",
            "metadata": {
                "collection_timestamp": "2024-01-01T00:00:00Z",
                "collector_version": "1.0.0",
                "source_database": {
                    "engine": "postgresql",
                    "version": "14.7",
                    "hostname": "test-db.example.com",
                },
            },
            "database_schema": {
                "tables": [
                    {
                        "table_id": "public.users",
                        "table_name": "users",
                        "row_count": 100,
                        "columns": [
                            {"column_name": "id", "data_type": "integer", "nullable": False}
                        ],
                    }
                ]
            },
            "queries": {
                "query_patterns": [
                    {
                        "query_id": "q1",
                        "query_text": "SELECT * FROM users",
                        "frequency_per_hour": 10.0,
                        "tables_accessed": ["public.users"],
                    }
                ]
            },
            "metrics": {"performance_metrics": {"avg_query_time_ms": 5.0}},
        }

        # Create contract instance
        contract = CollectorOutputContract(**contract_data)

        # Verify database_schema field exists and is accessible
        assert hasattr(contract, "database_schema"), "database_schema field should exist"
        assert contract.database_schema is not None, "database_schema should not be None"

        # Verify database_schema contains expected data
        assert len(contract.database_schema.tables) == 1
        assert contract.database_schema.tables[0].table_name == "users"

    def test_database_schema_field_serialization(self):
        """
        Test that database_schema field serializes correctly to JSON.

        Requirements: 3.3, 3.4
        """
        contract_data = {
            "contract_version": "3.0",
            "job_id": "test-job-456",
            "metadata": {
                "collection_timestamp": "2024-01-01T00:00:00Z",
                "collector_version": "1.0.0",
                "source_database": {
                    "engine": "mysql",
                    "version": "8.0.32",
                    "hostname": "test-mysql.example.com",
                },
            },
            "database_schema": {
                "tables": [
                    {
                        "table_id": "mydb.products",
                        "table_name": "products",
                        "row_count": 500,
                        "columns": [
                            {"column_name": "product_id", "data_type": "bigint", "nullable": False}
                        ],
                    }
                ]
            },
            "queries": {
                "query_patterns": [
                    {
                        "query_id": "q2",
                        "query_text": "SELECT * FROM products",
                        "frequency_per_hour": 20.0,
                        "tables_accessed": ["mydb.products"],
                    }
                ]
            },
            "metrics": {"performance_metrics": {"avg_query_time_ms": 8.5}},
        }

        contract = CollectorOutputContract(**contract_data)

        # Serialize to dictionary
        serialized = contract.model_dump()

        # Verify database_schema is in serialized output
        assert "database_schema" in serialized, "database_schema should be in serialized output"
        assert "tables" in serialized["database_schema"]
        assert len(serialized["database_schema"]["tables"]) == 1

        # Serialize to JSON string
        json_str = contract.model_dump_json()
        assert "database_schema" in json_str, "database_schema should be in JSON string"

    def test_old_schema_field_not_present(self):
        """
        Test that the old 'schema' field name is not present in the model.

        This verifies that the field was properly renamed and the old name
        is no longer accessible.

        Requirements: 3.3
        """
        contract_data = {
            "contract_version": "3.0",
            "job_id": "test-job-789",
            "metadata": {
                "collection_timestamp": "2024-01-01T00:00:00Z",
                "collector_version": "1.0.0",
                "source_database": {
                    "engine": "postgresql",
                    "version": "14.7",
                    "hostname": "test-db.example.com",
                },
            },
            "database_schema": {
                "tables": [
                    {
                        "table_id": "public.orders",
                        "table_name": "orders",
                        "row_count": 1000,
                        "columns": [
                            {"column_name": "order_id", "data_type": "uuid", "nullable": False}
                        ],
                    }
                ]
            },
            "queries": {
                "query_patterns": [
                    {
                        "query_id": "q3",
                        "query_text": "SELECT * FROM orders",
                        "frequency_per_hour": 50.0,
                        "tables_accessed": ["public.orders"],
                    }
                ]
            },
            "metrics": {"performance_metrics": {"avg_query_time_ms": 12.0}},
        }

        # Instantiate to verify the contract data is valid
        CollectorOutputContract(**contract_data)

        # Verify 'schema' field does not exist as a direct attribute
        # Note: We check the model fields, not the BaseModel.schema() method
        model_fields = CollectorOutputContract.model_fields.keys()
        assert (
            "schema" not in model_fields
        ), "Old 'schema' field name should not be present in model fields"

        # Verify database_schema is the correct field name
        assert (
            "database_schema" in model_fields
        ), "New 'database_schema' field name should be present in model fields"

    def test_contract_version_reflects_breaking_change(self):
        """
        Test that the contract version has been updated to reflect the breaking change.

        The field rename from 'schema' to 'database_schema' is a breaking change,
        so the contract version should be bumped to 3.0.

        Requirements: 3.4
        """
        contract_data = {
            "contract_version": "3.0",
            "job_id": "test-job-version",
            "metadata": {
                "collection_timestamp": "2024-01-01T00:00:00Z",
                "collector_version": "1.0.0",
                "source_database": {
                    "engine": "postgresql",
                    "version": "14.7",
                    "hostname": "test-db.example.com",
                },
            },
            "database_schema": {
                "tables": [
                    {
                        "table_id": "public.test",
                        "table_name": "test",
                        "row_count": 1,
                        "columns": [
                            {"column_name": "id", "data_type": "integer", "nullable": False}
                        ],
                    }
                ]
            },
            "queries": {
                "query_patterns": [
                    {
                        "query_id": "q4",
                        "query_text": "SELECT 1",
                        "frequency_per_hour": 1.0,
                        "tables_accessed": ["public.test"],
                    }
                ]
            },
            "metrics": {"performance_metrics": {"avg_query_time_ms": 1.0}},
        }

        contract = CollectorOutputContract(**contract_data)

        # Verify contract version is 3.0 or higher
        assert (
            contract.contract_version == "3.0"
        ), "Contract version should be 3.0 to reflect the breaking change"

        # Verify the default contract version in the model is also 3.0
        default_version = CollectorOutputContract.model_fields["contract_version"].default
        assert default_version == "3.0", "Default contract version should be 3.0"
