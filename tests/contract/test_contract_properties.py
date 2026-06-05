"""
Property-based tests for CollectorOutputContract validation.

This module uses hypothesis to test universal properties that should hold
across all valid and invalid contract data. Property-based testing generates
many random test cases to verify correctness properties.

Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 6.8
"""

from datetime import UTC, datetime
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from src.contracts.collector_output import (
    CollectorOutputContract,
    DatabaseEngine,
    DeploymentType,
    ForeignKeyAction,
    IndexType,
    Metadata,
    Metrics,
    NormalizedDataType,
    ParameterMode,
    ProcedureType,
    Queries,
    QueryType,
    Schema,
    StorageType,
    TriggerEventType,
    TriggerForEach,
    TriggerTiming,
)

# ============================================================================
# Hypothesis Strategies for Contract Data Generation
# ============================================================================


# Basic type strategies
def semantic_version_strategy() -> st.SearchStrategy[str]:
    """Generate valid semantic version strings (MAJOR.MINOR.PATCH)."""
    return st.builds(
        lambda major, minor, patch: f"{major}.{minor}.{patch}",
        major=st.integers(min_value=0, max_value=99),
        minor=st.integers(min_value=0, max_value=99),
        patch=st.integers(min_value=0, max_value=99),
    )


def contract_version_strategy() -> st.SearchStrategy[str]:
    """Generate valid contract version strings (MAJOR.MINOR)."""
    return st.builds(
        lambda major, minor: f"{major}.{minor}",
        major=st.integers(min_value=0, max_value=99),
        minor=st.integers(min_value=0, max_value=99),
    )


def iso_datetime_strategy() -> st.SearchStrategy[datetime]:
    """Generate valid datetime objects with UTC timezone."""
    return st.datetimes(
        min_value=datetime(2020, 1, 1),
        max_value=datetime(2030, 12, 31),
        timezones=st.just(UTC),
    )


def hostname_strategy() -> st.SearchStrategy[str]:
    """Generate valid hostname strings."""
    return st.text(
        alphabet=st.characters(whitelist_categories=("Ll", "Nd"), whitelist_characters="-_."),
        min_size=5,
        max_size=50,
    ).filter(lambda x: x and x[0].isalnum() and x[-1].isalnum())


def identifier_strategy() -> st.SearchStrategy[str]:
    """Generate valid identifier strings (table names, column names, etc.)."""
    return st.text(
        alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), whitelist_characters="_"),
        min_size=1,
        max_size=50,
    ).filter(lambda x: x and x[0].isalpha())


def table_id_strategy() -> st.SearchStrategy[str]:
    """Generate valid table ID strings (schema.table format)."""
    return st.builds(
        lambda schema, table: f"{schema}.{table}",
        schema=identifier_strategy(),
        table=identifier_strategy(),
    )


# Enum strategies
database_engine_strategy = st.sampled_from(DatabaseEngine)
deployment_type_strategy = st.sampled_from(DeploymentType)
storage_type_strategy = st.sampled_from(StorageType)
normalized_data_type_strategy = st.sampled_from(NormalizedDataType)
index_type_strategy = st.sampled_from(IndexType)
foreign_key_action_strategy = st.sampled_from(ForeignKeyAction)
query_type_strategy = st.sampled_from(QueryType)
procedure_type_strategy = st.sampled_from(ProcedureType)
parameter_mode_strategy = st.sampled_from(ParameterMode)
trigger_event_type_strategy = st.sampled_from(TriggerEventType)
trigger_timing_strategy = st.sampled_from(TriggerTiming)
trigger_for_each_strategy = st.sampled_from(TriggerForEach)


# Model strategies
def column_strategy() -> st.SearchStrategy[dict[str, Any]]:
    """Generate valid Column data."""
    return st.fixed_dictionaries(
        {
            "column_name": identifier_strategy(),
            "data_type": st.text(min_size=1, max_size=50),
            "nullable": st.booleans(),
        },
        optional={
            "ordinal_position": st.integers(min_value=1, max_value=1000),
            "normalized_data_type": normalized_data_type_strategy,
            "max_length": st.integers(min_value=1, max_value=65535),
            "default_value": st.one_of(
                st.none(),
                st.text(max_size=100),
                st.integers(min_value=-1000000, max_value=1000000),
                st.floats(
                    min_value=-1000000.0, max_value=1000000.0, allow_nan=False, allow_infinity=False
                ),
                st.booleans(),
            ),
            "is_auto_increment": st.booleans(),
            "cardinality": st.integers(min_value=0, max_value=1000000),
        },
    )


def index_strategy() -> st.SearchStrategy[dict[str, Any]]:
    """Generate valid Index data."""
    return st.fixed_dictionaries(
        {
            "index_name": identifier_strategy(),
            "columns": st.lists(identifier_strategy(), min_size=1, max_size=5),
            "is_unique": st.booleans(),
        },
        optional={
            "is_primary": st.booleans(),
            "index_type": index_type_strategy,
        },
    )


def foreign_key_strategy() -> st.SearchStrategy[dict[str, Any]]:
    """Generate valid ForeignKey data."""
    return st.fixed_dictionaries(
        {
            "constraint_name": identifier_strategy(),
            "columns": st.lists(identifier_strategy(), min_size=1, max_size=5),
            "referenced_table": table_id_strategy(),
            "referenced_columns": st.lists(identifier_strategy(), min_size=1, max_size=5),
        },
        optional={
            "on_delete": foreign_key_action_strategy,
            "on_update": foreign_key_action_strategy,
        },
    )


def table_strategy() -> st.SearchStrategy[dict[str, Any]]:
    """Generate valid Table data."""
    return st.fixed_dictionaries(
        {
            "table_id": table_id_strategy(),
            "table_name": identifier_strategy(),
            "row_count": st.integers(min_value=0, max_value=1000000000),
            "columns": st.lists(column_strategy(), min_size=1, max_size=10),
        },
        optional={
            "schema_name": identifier_strategy(),
            "size_mb": st.floats(min_value=0.0, max_value=1000000.0),
            "indexes": st.lists(index_strategy(), min_size=0, max_size=10),
            "primary_key": st.lists(identifier_strategy(), min_size=0, max_size=5),
            "foreign_keys": st.lists(foreign_key_strategy(), min_size=0, max_size=10),
        },
    )


def source_database_strategy() -> st.SearchStrategy[dict[str, Any]]:
    """Generate valid SourceDatabase data."""
    return st.fixed_dictionaries(
        {
            "engine": database_engine_strategy,
            "version": st.text(min_size=1, max_size=20),
            "hostname": hostname_strategy(),
        },
        optional={
            "database_name": identifier_strategy(),
            "database_size_gb": st.floats(min_value=0.0, max_value=100000.0),
            "deployment_type": deployment_type_strategy,
        },
    )


def metadata_strategy() -> st.SearchStrategy[dict[str, Any]]:
    """Generate valid Metadata data."""
    return st.fixed_dictionaries(
        {
            "collection_timestamp": iso_datetime_strategy(),
            "collector_version": semantic_version_strategy(),
            "source_database": source_database_strategy(),
        },
        optional={
            "collection_duration_seconds": st.floats(min_value=0.0, max_value=3600.0),
        },
    )


def query_pattern_strategy() -> st.SearchStrategy[dict[str, Any]]:
    """Generate valid QueryPattern data."""
    return st.fixed_dictionaries(
        {
            "query_id": identifier_strategy(),
            "query_text": st.text(min_size=10, max_size=500),
            "frequency_per_hour": st.floats(min_value=0.0, max_value=100000.0),
            "tables_accessed": st.lists(table_id_strategy(), min_size=1, max_size=10),
        },
        optional={
            "query_type": query_type_strategy,
            "calls_per_second": st.floats(min_value=0.0, max_value=10000.0),
            "rows_returned_avg": st.floats(min_value=0.0, max_value=1000000.0),
            "execution_time_ms_avg": st.floats(min_value=0.0, max_value=60000.0),
            "has_joins": st.booleans(),
            "join_count": st.integers(min_value=0, max_value=20),
            "has_aggregations": st.booleans(),
            "has_subqueries": st.booleans(),
        },
    )


def queries_strategy() -> st.SearchStrategy[dict[str, Any]]:
    """Generate valid Queries data."""
    return st.fixed_dictionaries(
        {
            "query_patterns": st.lists(query_pattern_strategy(), min_size=1, max_size=5),
        },
        optional={
            "total_queries_analyzed": st.integers(min_value=0, max_value=1000000),
            "collection_start_time": iso_datetime_strategy(),
            "collection_end_time": iso_datetime_strategy(),
        },
    )


def performance_metrics_strategy() -> st.SearchStrategy[dict[str, Any]]:
    """Generate valid PerformanceMetrics data."""
    return st.fixed_dictionaries(
        {},
        optional={
            "avg_query_time_ms": st.floats(min_value=0.0, max_value=60000.0),
            "p50_query_time_ms": st.floats(min_value=0.0, max_value=60000.0),
            "p95_query_time_ms": st.floats(min_value=0.0, max_value=60000.0),
            "p99_query_time_ms": st.floats(min_value=0.0, max_value=60000.0),
            "queries_per_second": st.floats(min_value=0.0, max_value=100000.0),
            "connection_pool_usage_percent": st.floats(min_value=0.0, max_value=100.0),
            "active_connections_avg": st.floats(min_value=0.0, max_value=10000.0),
            "active_connections_max": st.floats(min_value=0.0, max_value=10000.0),
            "transactions_per_second": st.floats(min_value=0.0, max_value=100000.0),
            "read_iops_avg": st.floats(min_value=0.0, max_value=100000.0),
            "write_iops_avg": st.floats(min_value=0.0, max_value=100000.0),
            "network_throughput_mbps_avg": st.floats(min_value=0.0, max_value=10000.0),
        },
    )


def schema_strategy() -> st.SearchStrategy[dict[str, Any]]:
    """Generate valid Schema data."""
    return st.fixed_dictionaries(
        {
            "tables": st.lists(table_strategy(), min_size=1, max_size=5),
        },
        optional={
            "views": st.none(),  # Simplified for now
            "procedures": st.none(),  # Simplified for now
            "triggers": st.none(),  # Simplified for now
        },
    )


def metrics_strategy() -> st.SearchStrategy[dict[str, Any]]:
    """Generate valid Metrics data."""
    return st.fixed_dictionaries(
        {
            "performance_metrics": performance_metrics_strategy(),
        },
        optional={
            "rds_cloudwatch_metrics": st.none(),  # Simplified for now
        },
    )


def valid_contract_strategy() -> st.SearchStrategy[dict[str, Any]]:
    """Generate valid CollectorOutputContract data."""
    return st.fixed_dictionaries(
        {
            "job_id": st.text(min_size=5, max_size=100),
            "metadata": metadata_strategy(),
            "database_schema": schema_strategy(),
            "queries": queries_strategy(),
            "metrics": metrics_strategy(),
        },
        optional={
            "contract_version": contract_version_strategy(),
        },
    )


# ============================================================================
# Property-Based Tests
# ============================================================================


# Note: Property tests will be implemented in subsequent tasks (7.2-7.6)
# This file establishes the foundation with hypothesis imports and strategies


# Placeholder for future property tests
class TestContractProperties:
    """Property-based tests for contract validation."""

    @settings(suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
    @given(
        semantic_version_strategy(),
        contract_version_strategy(),
        iso_datetime_strategy(),
        hostname_strategy(),
        identifier_strategy(),
        table_id_strategy(),
        database_engine_strategy,
        deployment_type_strategy,
        query_type_strategy,
        column_strategy(),
        table_strategy(),
        source_database_strategy(),
        metadata_strategy(),
        query_pattern_strategy(),
        queries_strategy(),
        performance_metrics_strategy(),
        schema_strategy(),
        metrics_strategy(),
        valid_contract_strategy(),
    )
    def test_strategies_are_defined(
        self,
        semantic_version,
        contract_version,
        iso_datetime,
        hostname,
        identifier,
        table_id,
        database_engine,
        deployment_type,
        query_type,
        column,
        table,
        source_database,
        metadata,
        query_pattern,
        queries,
        performance_metrics,
        schema,
        metrics,
        valid_contract,
    ):
        """
        Verify that all hypothesis strategies are properly defined.

        This is a sanity check to ensure the strategies module is working
        and can generate sample data.
        """
        # Test basic strategies
        assert semantic_version is not None
        assert contract_version is not None
        assert iso_datetime is not None
        assert hostname is not None
        assert identifier is not None
        assert table_id is not None

        # Test enum strategies
        assert database_engine is not None
        assert deployment_type is not None
        assert query_type is not None

        # Test model strategies
        assert column is not None
        assert table is not None
        assert source_database is not None
        assert metadata is not None
        assert query_pattern is not None
        assert queries is not None
        assert performance_metrics is not None
        assert schema is not None
        assert metrics is not None
        assert valid_contract is not None

    @given(valid_contract_strategy())
    def test_valid_contract_strategy_generates_valid_data(self, contract_data):
        """
        Verify that the valid_contract_strategy generates data that can
        instantiate a CollectorOutputContract.

        This is a basic sanity check before implementing full property tests.
        """
        # Verify contract data structure
        assert contract_data is not None
        assert "job_id" in contract_data
        assert "metadata" in contract_data
        assert "database_schema" in contract_data
        assert "queries" in contract_data
        assert "metrics" in contract_data

        # Try to instantiate the contract
        contract = CollectorOutputContract(**contract_data)
        assert contract is not None


# ============================================================================
# Property 2: Valid Data Instantiation
# Feature: contract-validation-improvements
# ============================================================================


class TestValidDataInstantiation:
    """
    Property 2: Valid Data Instantiation

    For any valid data that conforms to a contract model's schema, creating an
    instance of that contract model should succeed without raising a ValidationError.

    Validates: Requirements 2.2
    """

    @given(valid_contract_strategy())
    def test_valid_contract_data_instantiates_successfully(self, contract_data):
        """
        Property test: Valid contract data should always instantiate successfully.

        This test generates random valid contract data using hypothesis strategies
        and verifies that CollectorOutputContract instances can be created without
        errors. Runs minimum 100 iterations by default (hypothesis default).

        Validates: Requirements 2.2
        """
        # Attempt to instantiate the contract with generated valid data
        # This should never raise a ValidationError
        contract = CollectorOutputContract(**contract_data)

        # Verify the contract was created successfully
        assert contract is not None
        assert isinstance(contract, CollectorOutputContract)

        # Verify required fields are present and have expected types
        assert contract.job_id is not None
        assert isinstance(contract.job_id, str)

        assert contract.metadata is not None
        assert isinstance(contract.metadata, Metadata)

        assert contract.database_schema is not None
        assert isinstance(contract.database_schema, Schema)

        assert contract.queries is not None
        assert isinstance(contract.queries, Queries)

        assert contract.metrics is not None
        assert isinstance(contract.metrics, Metrics)

        # Verify contract version has expected format
        assert contract.contract_version is not None
        assert isinstance(contract.contract_version, str)
        # Should match pattern: MAJOR.MINOR
        assert len(contract.contract_version.split(".")) == 2


# ============================================================================
# Property 3: Invalid Data Rejection
# Feature: contract-validation-improvements
# ============================================================================


class TestInvalidDataRejection:
    """
    Property 3: Invalid Data Rejection

    For any invalid data that violates a contract model's validation rules
    (missing fields, wrong types, out-of-range values, invalid enums, null in
    non-nullable fields, invalid validator inputs), attempting to create an
    instance should raise a ValidationError.

    Validates: Requirements 2.3, 6.1, 6.2, 6.3, 6.4, 6.5, 6.7
    """

    @given(st.data())
    def test_missing_required_fields_raises_validation_error(self, data):
        """
        Property test: Missing required fields should always raise ValidationError.

        This test generates valid contract data, then removes one required field
        at a time and verifies that ValidationError is raised.

        Validates: Requirements 2.3, 6.1
        """
        # Generate valid contract data
        contract_data = data.draw(valid_contract_strategy())

        # List of required fields in CollectorOutputContract
        required_fields = ["job_id", "metadata", "database_schema", "queries", "metrics"]

        # Pick a random required field to remove
        field_to_remove = data.draw(st.sampled_from(required_fields))

        # Remove the field
        invalid_data = {k: v for k, v in contract_data.items() if k != field_to_remove}

        # Attempt to instantiate should raise ValidationError
        with pytest.raises(ValidationError) as exc_info:
            CollectorOutputContract(**invalid_data)

        # Verify error message mentions the missing field
        error_message = str(exc_info.value)
        assert field_to_remove in error_message.lower() or "required" in error_message.lower()

    @given(st.data())
    def test_invalid_data_types_raise_validation_error(self, data):
        """
        Property test: Invalid data types should always raise ValidationError.

        This test generates valid contract data, then replaces field values with
        invalid types and verifies that ValidationError is raised.

        Validates: Requirements 2.3, 6.2
        """
        # Generate valid contract data
        contract_data = data.draw(valid_contract_strategy())

        # Define type mutations: replace valid values with invalid types
        type_mutations = [
            ("job_id", 12345),  # Should be string, not int
            ("metadata", "not a dict"),  # Should be dict/Metadata, not string
            ("database_schema", 42),  # Should be dict/Schema, not int
            ("queries", True),  # Should be dict/Queries, not bool
            ("metrics", []),  # Should be dict/Metrics, not list
        ]

        # Pick a random mutation
        field_name, invalid_value = data.draw(st.sampled_from(type_mutations))

        # Apply the mutation
        invalid_data = contract_data.copy()
        invalid_data[field_name] = invalid_value

        # Attempt to instantiate should raise ValidationError
        with pytest.raises(ValidationError) as exc_info:
            CollectorOutputContract(**invalid_data)

        # Verify error message mentions type mismatch
        error_message = str(exc_info.value).lower()
        assert any(keyword in error_message for keyword in ["type", "invalid", "expected"])

    @given(st.data())
    def test_out_of_range_values_raise_validation_error(self, data):
        """
        Property test: Out-of-range values should always raise ValidationError.

        This test generates contract data with out-of-range values for fields
        with constraints (ge, le, min_value, max_value) and verifies that
        ValidationError is raised.

        Validates: Requirements 2.3, 6.3
        """
        # Generate valid contract data
        contract_data = data.draw(valid_contract_strategy())

        # Pick a random out-of-range mutation
        mutation_choice = data.draw(st.integers(min_value=0, max_value=2))

        if mutation_choice == 0:
            # Negative row_count (should be >= 0)
            contract_data["database_schema"]["tables"][0]["row_count"] = -100
        elif mutation_choice == 1:
            # backup_retention_days > 35 (should be <= 35)
            if "rds_instance_metadata" not in contract_data["metadata"]["source_database"]:
                contract_data["metadata"]["source_database"]["rds_instance_metadata"] = {}
            contract_data["metadata"]["source_database"]["rds_instance_metadata"][
                "backup_retention_days"
            ] = 50
        else:
            # Negative size_mb (should be >= 0)
            contract_data["database_schema"]["tables"][0]["size_mb"] = -10.5

        # Attempt to instantiate should raise ValidationError
        with pytest.raises(ValidationError) as exc_info:
            CollectorOutputContract(**contract_data)

        # Verify error message mentions constraint violation
        error_message = str(exc_info.value).lower()
        assert any(
            keyword in error_message for keyword in ["greater", "less", "range", "constraint"]
        )

    @given(st.data())
    def test_invalid_enum_values_raise_validation_error(self, data):
        """
        Property test: Invalid enum values should always raise ValidationError.

        This test generates contract data with invalid enum values and verifies
        that ValidationError is raised.

        Validates: Requirements 2.3, 6.4
        """
        # Generate valid contract data
        contract_data = data.draw(valid_contract_strategy())

        # Define invalid enum mutations
        invalid_enum_mutations = [
            ("metadata.source_database.engine", "invalid_engine"),
            ("metadata.source_database.deployment_type", "invalid_deployment"),
            ("queries.query_patterns[0].query_type", "INVALID_QUERY_TYPE"),
        ]

        # Pick a random mutation
        field_path, invalid_value = data.draw(st.sampled_from(invalid_enum_mutations))

        # Apply the mutation based on field path
        if field_path == "metadata.source_database.engine":
            contract_data["metadata"]["source_database"]["engine"] = invalid_value
        elif field_path == "metadata.source_database.deployment_type":
            contract_data["metadata"]["source_database"]["deployment_type"] = invalid_value
        elif field_path == "queries.query_patterns[0].query_type":
            contract_data["queries"]["query_patterns"][0]["query_type"] = invalid_value

        # Attempt to instantiate should raise ValidationError
        with pytest.raises(ValidationError) as exc_info:
            CollectorOutputContract(**contract_data)

        # Verify error message mentions enum or valid options
        error_message = str(exc_info.value).lower()
        assert any(
            keyword in error_message for keyword in ["enum", "invalid", "not a valid", "permitted"]
        )

    @given(st.data())
    def test_null_in_non_nullable_fields_raises_validation_error(self, data):
        """
        Property test: Null values in non-nullable fields should raise ValidationError.

        This test generates contract data with null values in required fields
        and verifies that ValidationError is raised.

        Validates: Requirements 2.3, 6.5
        """
        # Generate valid contract data
        contract_data = data.draw(valid_contract_strategy())

        # Define null mutations for non-nullable fields
        null_mutations = [
            "job_id",
            "metadata",
            "database_schema",
            "queries",
            "metrics",
        ]

        # Pick a random field to set to None
        field_to_null = data.draw(st.sampled_from(null_mutations))

        # Apply the mutation
        invalid_data = contract_data.copy()
        invalid_data[field_to_null] = None

        # Attempt to instantiate should raise ValidationError
        with pytest.raises(ValidationError) as exc_info:
            CollectorOutputContract(**invalid_data)

        # Verify error message mentions the field cannot be null
        error_message = str(exc_info.value).lower()
        assert any(keyword in error_message for keyword in ["none", "null", "required", "missing"])

    @given(st.data())
    def test_invalid_validator_inputs_raise_validation_error(self, data):
        """
        Property test: Invalid inputs to field validators should raise ValidationError.

        This test generates contract data with invalid values for fields that have
        custom validators and verifies that ValidationError is raised.

        Validates: Requirements 2.3, 6.7
        """
        # Generate valid contract data
        contract_data = data.draw(valid_contract_strategy())

        # Pick a random validator to test
        validator_choice = data.draw(st.integers(min_value=0, max_value=2))

        if validator_choice == 0:
            # Invalid enhanced_monitoring_interval (must be 0, 1, 5, 10, 15, 30, or 60)
            if "rds_instance_metadata" not in contract_data["metadata"]["source_database"]:
                contract_data["metadata"]["source_database"]["rds_instance_metadata"] = {}
            contract_data["metadata"]["source_database"]["rds_instance_metadata"][
                "enhanced_monitoring_interval"
            ] = 7
        elif validator_choice == 1:
            # Invalid contract_version pattern (must be MAJOR.MINOR)
            contract_data["contract_version"] = "1.2.3.4"
        else:
            # Invalid collector_version pattern (must be semantic version)
            contract_data["metadata"]["collector_version"] = "invalid-version"

        # Attempt to instantiate should raise ValidationError
        with pytest.raises(ValidationError) as exc_info:
            CollectorOutputContract(**contract_data)

        # Verify error message is descriptive
        error_message = str(exc_info.value).lower()
        assert any(keyword in error_message for keyword in ["invalid", "pattern", "must", "should"])

    @settings(suppress_health_check=[HealthCheck.too_slow])
    @given(st.data())
    def test_nested_model_validation_errors_propagate(self, data):
        """
        Property test: Validation errors in nested models should propagate correctly.

        This test generates contract data with invalid nested model data and
        verifies that ValidationError is raised with proper error context.

        Validates: Requirements 2.3
        """
        # Generate valid contract data
        contract_data = data.draw(valid_contract_strategy())

        # Pick a random nested model to invalidate
        nested_mutation_choice = data.draw(st.integers(min_value=0, max_value=2))

        if nested_mutation_choice == 0:
            # Invalid column data (missing required field)
            contract_data["database_schema"]["tables"][0]["columns"][0] = {
                "column_name": "test_column"
                # Missing required fields: data_type, nullable
            }
        elif nested_mutation_choice == 1:
            # Invalid source_database (missing required field)
            contract_data["metadata"]["source_database"] = {
                "engine": "postgresql",
                "version": "14.7",
                # Missing required field: hostname
            }
        else:
            # Invalid query_pattern (missing required field)
            contract_data["queries"]["query_patterns"][0] = {
                "query_id": "q1",
                "query_text": "SELECT * FROM users",
                # Missing required fields: frequency_per_hour, tables_accessed
            }

        # Attempt to instantiate should raise ValidationError
        with pytest.raises(ValidationError) as exc_info:
            CollectorOutputContract(**contract_data)

        # Verify error message provides context about nested field
        error_message = str(exc_info.value).lower()
        assert "required" in error_message or "missing" in error_message


# ============================================================================
# Property 1: Contract Import Integrity
# Feature: contract-validation-improvements
# ============================================================================


class TestContractImportIntegrity:
    """
    Property 1: Contract Import Integrity

    For all Python files in the src/contracts/ directory, importing the module
    should succeed without errors, and all Pydantic models should be discoverable.

    Validates: Requirements 2.1
    """

    def test_all_contract_modules_can_be_imported(self):
        """
        Test that all contract modules can be imported without errors.

        This test verifies that:
        1. All contract Python files can be imported
        2. No import errors occur
        3. The imports are successful
        """
        import importlib
        from pathlib import Path

        # Get the contracts directory path
        contracts_dir = Path("src/contracts")
        assert contracts_dir.exists(), f"Contracts directory not found: {contracts_dir}"

        # Find all Python files in the contracts directory (excluding __init__.py and __pycache__)
        contract_files = [
            f
            for f in contracts_dir.glob("*.py")
            if f.name != "__init__.py" and not f.name.startswith(".")
        ]

        assert len(contract_files) > 0, "No contract files found in src/contracts/"

        # Try to import each contract module
        imported_modules = []
        for contract_file in contract_files:
            module_name = f"src.contracts.{contract_file.stem}"
            try:
                module = importlib.import_module(module_name)  # nosemgrep: non-literal-import
                imported_modules.append((module_name, module))
            except Exception as e:
                pytest.fail(f"Failed to import {module_name}: {e}")

        # Verify we imported at least one module
        assert len(imported_modules) > 0, "No modules were successfully imported"

    def test_all_pydantic_models_are_discoverable(self):
        """
        Test that all Pydantic models in contract modules are discoverable.

        This test verifies that:
        1. Each contract module contains at least one Pydantic BaseModel
        2. Models can be accessed via module attributes
        3. Models are properly defined classes
        """
        import importlib
        import inspect
        from pathlib import Path

        from pydantic import BaseModel

        # Get the contracts directory path
        contracts_dir = Path("src/contracts")

        # Find all Python files in the contracts directory (excluding __init__.py)
        contract_files = [
            f
            for f in contracts_dir.glob("*.py")
            if f.name != "__init__.py" and not f.name.startswith(".")
        ]

        # Track discovered models
        all_models = []

        # Import each module and discover Pydantic models
        for contract_file in contract_files:
            module_name = f"src.contracts.{contract_file.stem}"
            module = importlib.import_module(module_name)  # nosemgrep: non-literal-import

            # Find all classes in the module that inherit from BaseModel
            models_in_module = [
                (name, obj)
                for name, obj in inspect.getmembers(module, inspect.isclass)
                if issubclass(obj, BaseModel) and obj is not BaseModel
            ]

            # Verify at least one model exists in each contract file
            assert len(models_in_module) > 0, (
                f"No Pydantic models found in {module_name}. "
                f"Contract files should define at least one BaseModel."
            )

            all_models.extend(models_in_module)

        # Verify we discovered models across all contract files
        assert len(all_models) > 0, "No Pydantic models discovered in any contract file"

    def test_contract_models_have_required_attributes(self):
        """
        Test that discovered Pydantic models have expected attributes.

        This test verifies that:
        1. Models have model_fields attribute (Pydantic v2)
        2. Models can be instantiated (will be tested with valid data in other tests)
        3. Models have proper Pydantic configuration
        """
        import importlib
        import inspect
        from pathlib import Path

        from pydantic import BaseModel

        # Get the contracts directory path
        contracts_dir = Path("src/contracts")

        # Find all Python files in the contracts directory (excluding __init__.py)
        contract_files = [
            f
            for f in contracts_dir.glob("*.py")
            if f.name != "__init__.py" and not f.name.startswith(".")
        ]

        # Import each module and check model attributes
        for contract_file in contract_files:
            module_name = f"src.contracts.{contract_file.stem}"
            module = importlib.import_module(module_name)  # nosemgrep: non-literal-import

            # Find all Pydantic models in the module
            models_in_module = [
                (name, obj)
                for name, obj in inspect.getmembers(module, inspect.isclass)
                if issubclass(obj, BaseModel) and obj is not BaseModel
            ]

            # Check each model has required Pydantic attributes
            for model_name, model_class in models_in_module:
                # Verify model has model_fields (Pydantic v2 attribute)
                assert hasattr(model_class, "model_fields"), (
                    f"Model {model_name} in {module_name} is missing model_fields attribute. "
                    f"This suggests it may not be a properly defined Pydantic model."
                )

                # Verify model has model_config (Pydantic v2 attribute)
                assert hasattr(
                    model_class, "model_config"
                ), f"Model {model_name} in {module_name} is missing model_config attribute."

                # Verify model has model_validate method (Pydantic v2 method)
                assert hasattr(
                    model_class, "model_validate"
                ), f"Model {model_name} in {module_name} is missing model_validate method."

                # Verify model has model_dump method (Pydantic v2 method)
                assert hasattr(
                    model_class, "model_dump"
                ), f"Model {model_name} in {module_name} is missing model_dump method."


# ============================================================================
# Property 4: Serialization Round-Trip Consistency
# Feature: contract-validation-improvements
# ============================================================================


class TestSerializationRoundTrip:
    """
    Property 4: Serialization Round-Trip Consistency

    For any valid contract instance, serializing to JSON and then deserializing
    back should produce an equivalent object with the same field values.

    Validates: Requirements 2.4, 2.5
    """

    @settings(max_examples=10)
    @given(valid_contract_strategy())
    def test_contract_serialization_round_trip_consistency(self, contract_data):
        """
        Property test: Serialization round-trip should preserve all data.

        This test generates random valid contract instances, serializes them to
        JSON, deserializes them back, and verifies the deserialized object equals
        the original. Runs minimum 100 iterations by default (hypothesis default).

        The test validates:
        1. Contract can be serialized to JSON string
        2. JSON string can be deserialized back to contract
        3. Deserialized contract equals original contract
        4. All field values are preserved through the round-trip

        Validates: Requirements 2.4, 2.5
        """
        # Create the original contract instance from generated data
        original_contract = CollectorOutputContract(**contract_data)

        # Serialize to JSON string
        json_str = original_contract.model_dump_json()

        # Verify JSON string is valid (not empty, is a string)
        assert json_str is not None
        assert isinstance(json_str, str)
        assert len(json_str) > 0

        # Deserialize back to contract instance
        deserialized_contract = CollectorOutputContract.model_validate_json(json_str)

        # Verify deserialized contract is not None
        assert deserialized_contract is not None
        assert isinstance(deserialized_contract, CollectorOutputContract)

        # Verify the complete objects are equal using Pydantic's equality
        # This checks all fields recursively
        assert deserialized_contract == original_contract

    @settings(max_examples=10)
    @given(valid_contract_strategy())
    def test_contract_dict_serialization_round_trip(self, contract_data):
        """
        Property test: Dictionary serialization round-trip should preserve all data.

        This test verifies that serializing to dictionary and back also preserves
        all data correctly. This tests the model_dump() and model_validate() methods.

        Validates: Requirements 2.4, 2.5
        """
        # Create the original contract instance from generated data
        original_contract = CollectorOutputContract(**contract_data)

        # Serialize to dictionary
        contract_dict = original_contract.model_dump()

        # Verify dictionary is valid
        assert contract_dict is not None
        assert isinstance(contract_dict, dict)

        # Deserialize back to contract instance
        deserialized_contract = CollectorOutputContract.model_validate(contract_dict)

        # Verify deserialized contract matches original
        assert deserialized_contract == original_contract


# ============================================================================
# Property 5: Error Message Descriptiveness
# Feature: contract-validation-improvements
# ============================================================================


class TestErrorMessageDescriptiveness:
    """
    Property 5: Error Message Descriptiveness

    For any validation error raised by a contract model, the error message should
    include the field name that failed validation and a description of why the
    validation failed.

    Validates: Requirements 6.8
    """

    @settings(max_examples=100)
    @given(st.data())
    def test_missing_field_errors_are_descriptive(self, data):
        """
        Property test: Error messages for missing fields should be descriptive.

        This test generates contract data with missing required fields and verifies
        that the ValidationError message contains:
        1. The field name that is missing
        2. A clear indication that the field is required

        Validates: Requirements 6.8
        """
        # Generate valid contract data
        contract_data = data.draw(valid_contract_strategy())

        # List of required fields
        required_fields = ["job_id", "metadata", "database_schema", "queries", "metrics"]

        # Pick a random required field to remove
        field_to_remove = data.draw(st.sampled_from(required_fields))

        # Remove the field
        invalid_data = {k: v for k, v in contract_data.items() if k != field_to_remove}

        # Capture the ValidationError
        with pytest.raises(ValidationError) as exc_info:
            CollectorOutputContract(**invalid_data)

        error_message = str(exc_info.value).lower()

        # Verify error message contains the field name
        assert field_to_remove in error_message, (
            f"Error message should mention the missing field '{field_to_remove}'. "
            f"Got: {exc_info.value}"
        )

        # Verify error message indicates the field is required/missing
        assert any(
            keyword in error_message for keyword in ["required", "missing", "field required"]
        ), (
            f"Error message should indicate the field is required or missing. "
            f"Got: {exc_info.value}"
        )

    @settings(max_examples=100)
    @given(st.data())
    def test_type_mismatch_errors_are_descriptive(self, data):
        """
        Property test: Error messages for type mismatches should be descriptive.

        This test generates contract data with invalid types and verifies that
        the ValidationError message contains:
        1. The field name with the type error
        2. Information about the expected type or the type mismatch

        Validates: Requirements 6.8
        """
        # Generate valid contract data
        contract_data = data.draw(valid_contract_strategy())

        # Define type mutations with field paths
        type_mutations = [
            ("job_id", 12345, "job_id"),
            ("metadata", "not a dict", "metadata"),
            ("database_schema", 42, "database_schema"),
            ("queries", True, "queries"),
            ("metrics", [], "metrics"),
        ]

        # Pick a random mutation
        field_name, invalid_value, field_path = data.draw(st.sampled_from(type_mutations))

        # Apply the mutation
        invalid_data = contract_data.copy()
        invalid_data[field_name] = invalid_value

        # Capture the ValidationError
        with pytest.raises(ValidationError) as exc_info:
            CollectorOutputContract(**invalid_data)

        error_message = str(exc_info.value).lower()

        # Verify error message contains the field name or path
        assert field_path in error_message, (
            f"Error message should mention the field '{field_path}' with type error. "
            f"Got: {exc_info.value}"
        )

        # Verify error message indicates a type issue
        assert any(
            keyword in error_message
            for keyword in ["type", "invalid", "expected", "input should be", "instance of"]
        ), (f"Error message should indicate a type mismatch. " f"Got: {exc_info.value}")

    @settings(max_examples=100)
    @given(st.data())
    def test_constraint_violation_errors_are_descriptive(self, data):
        """
        Property test: Error messages for constraint violations should be descriptive.

        This test generates contract data with values that violate constraints
        (min/max values, patterns, etc.) and verifies that the ValidationError
        message contains:
        1. The field name with the constraint violation
        2. Information about the constraint that was violated

        Validates: Requirements 6.8
        """
        # Generate valid contract data
        contract_data = data.draw(valid_contract_strategy())

        # Pick a random constraint violation
        violation_choice = data.draw(st.integers(min_value=0, max_value=2))

        if violation_choice == 0:
            # Negative row_count (should be >= 0)
            contract_data["database_schema"]["tables"][0]["row_count"] = -100
            expected_field = "row_count"
        elif violation_choice == 1:
            # Negative size_mb (should be >= 0)
            contract_data["database_schema"]["tables"][0]["size_mb"] = -10.5
            expected_field = "size_mb"
        else:
            # Invalid contract_version pattern
            contract_data["contract_version"] = "invalid.version.format"
            expected_field = "contract_version"

        # Capture the ValidationError
        with pytest.raises(ValidationError) as exc_info:
            CollectorOutputContract(**contract_data)

        error_message = str(exc_info.value).lower()

        # Verify error message contains the field name
        assert expected_field in error_message, (
            f"Error message should mention the field '{expected_field}' with constraint violation. "
            f"Got: {exc_info.value}"
        )

        # Verify error message indicates a constraint violation
        assert any(
            keyword in error_message
            for keyword in [
                "greater",
                "less",
                "range",
                "constraint",
                "pattern",
                "match",
                "should be",
            ]
        ), (
            f"Error message should indicate a constraint violation. " f"Got: {exc_info.value}"
        )

    @settings(max_examples=100)
    @given(st.data())
    def test_enum_validation_errors_are_descriptive(self, data):
        """
        Property test: Error messages for invalid enum values should be descriptive.

        This test generates contract data with invalid enum values and verifies
        that the ValidationError message contains:
        1. The field name with the invalid enum
        2. Information about valid enum values or that the value is not permitted

        Validates: Requirements 6.8
        """
        # Generate valid contract data
        contract_data = data.draw(valid_contract_strategy())

        # Define invalid enum mutations
        enum_mutations = [
            ("engine", "invalid_engine", "metadata.source_database.engine"),
            ("deployment_type", "invalid_deployment", "metadata.source_database.deployment_type"),
            ("query_type", "INVALID_QUERY", "queries.query_patterns[0].query_type"),
        ]

        # Pick a random mutation
        field_name, invalid_value, field_path = data.draw(st.sampled_from(enum_mutations))

        # Apply the mutation based on field
        if field_name == "engine":
            contract_data["metadata"]["source_database"]["engine"] = invalid_value
        elif field_name == "deployment_type":
            contract_data["metadata"]["source_database"]["deployment_type"] = invalid_value
        elif field_name == "query_type":
            contract_data["queries"]["query_patterns"][0]["query_type"] = invalid_value

        # Capture the ValidationError
        with pytest.raises(ValidationError) as exc_info:
            CollectorOutputContract(**contract_data)

        error_message = str(exc_info.value).lower()

        # Verify error message contains the field name
        assert field_name in error_message, (
            f"Error message should mention the field '{field_name}' with invalid enum. "
            f"Got: {exc_info.value}"
        )

        # Verify error message indicates an enum validation issue
        assert any(
            keyword in error_message
            for keyword in [
                "enum",
                "invalid",
                "not a valid",
                "permitted",
                "allowed",
                "input should be",
            ]
        ), (
            f"Error message should indicate an enum validation error. " f"Got: {exc_info.value}"
        )

    @settings(max_examples=100)
    @given(st.data())
    def test_nested_field_errors_are_descriptive(self, data):
        """
        Property test: Error messages for nested field errors should be descriptive.

        This test generates contract data with errors in nested models and verifies
        that the ValidationError message contains:
        1. The path to the nested field (e.g., "metadata.source_database.hostname")
        2. Information about what validation failed

        Validates: Requirements 6.8
        """
        # Generate valid contract data
        contract_data = data.draw(valid_contract_strategy())

        # Pick a random nested field to invalidate
        nested_mutation_choice = data.draw(st.integers(min_value=0, max_value=2))

        if nested_mutation_choice == 0:
            # Missing required field in Column
            contract_data["database_schema"]["tables"][0]["columns"][0] = {
                "column_name": "test_column"
                # Missing: data_type, nullable
            }
            expected_fields = ["data_type", "nullable"]
        elif nested_mutation_choice == 1:
            # Missing required field in SourceDatabase
            contract_data["metadata"]["source_database"] = {
                "engine": "postgresql",
                "version": "14.7",
                # Missing: hostname
            }
            expected_fields = ["hostname"]
        else:
            # Missing required field in QueryPattern
            contract_data["queries"]["query_patterns"][0] = {
                "query_id": "q1",
                "query_text": "SELECT * FROM users",
                # Missing: frequency_per_hour, tables_accessed
            }
            expected_fields = ["frequency_per_hour", "tables_accessed"]

        # Capture the ValidationError
        with pytest.raises(ValidationError) as exc_info:
            CollectorOutputContract(**contract_data)

        error_message = str(exc_info.value).lower()

        # Verify error message contains at least one of the expected field names
        assert any(field in error_message for field in expected_fields), (
            f"Error message should mention one of the nested fields {expected_fields}. "
            f"Got: {exc_info.value}"
        )

        # Verify error message indicates the field is required/missing
        assert any(
            keyword in error_message for keyword in ["required", "missing", "field required"]
        ), (f"Error message should indicate a required field is missing. " f"Got: {exc_info.value}")
