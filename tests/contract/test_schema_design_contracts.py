"""Tests for schema design contracts — input, output, and PE review."""

import pytest
from pydantic import ValidationError

from src.contracts.dynamodb_model_output import (
    AttributeDefinition,
    DynamoDBModelOutputContract,
    EntityDefinition,
    HotPartitionEntry,
    KeyDefinition,
    TableDefinition,
)
from src.contracts.dynamodb_pe_review import (
    ChangeCategory,
    ChangeRequest,
    PEReviewResult,
    ReviewVerdict,
    Severity,
)

# Import fixtures
from tests.fixtures.schema_design_fixtures import *  # noqa: F401, F403

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def minimal_access_pattern():
    return {
        "pattern_id": "DDB-AP-1",
        "pattern_group": "User reads",
        "query_ids": ["q1"],
        "source_tables": ["db.users"],
        "description": "Get user by ID",
        "operation": "GetItem",
        "table_name": "Users",
        "key_condition": "PK=user_id",
        "design_rps": 100.0,
        "item_size_bytes": 200,
    }


@pytest.fixture
def minimal_table_definition():
    return {
        "table_name": "Users",
        "aggregate_pattern": "separate",
        "source_tables": ["db.users"],
        "partition_key": {"attribute_name": "user_id", "attribute_type": "S"},
        "attributes": [
            {
                "name": "user_id",
                "type": "S",
                "source_table": "db.users",
                "source_column": "id",
            }
        ],
        "gsis": [],
        "item_count": 1000,
        "item_size_bytes": 200,
    }


@pytest.fixture
def minimal_hot_partition():
    return {
        "table_name": "Users",
        "operation": "read",
        "rcu_or_wcu_per_second": 500.0,
        "partition_limit": 3000.0,
        "utilization_pct": 16.7,
        "at_risk": False,
        "contributing_patterns": ["q1"],
    }


@pytest.fixture
def minimal_output(minimal_access_pattern, minimal_table_definition, minimal_hot_partition):
    return {
        "contract_version": "1.0",
        "job_id": "test-job-001",
        "source_database": "test_db",
        "target_engine": "dynamodb",
        "access_patterns": [minimal_access_pattern],
        "table_definitions": [minimal_table_definition],
        "hot_partition_analysis": [minimal_hot_partition],
        "trade_offs": [
            {
                "description": "Single table for simplicity",
                "impact": "All data in one table reduces operational overhead but limits independent scaling.",
                "source_tables": ["db.users"],
                "target_tables": ["Users"],
                "query_ids": ["q1"],
                "engine": "dynamodb",
            }
        ],
        "validation_passed": True,
    }


# ---------------------------------------------------------------------------
# DynamoDBModelOutputContract tests
# ---------------------------------------------------------------------------


class TestDynamoDBModelOutputContract:
    def test_valid_minimal_output(self, minimal_output):
        contract = DynamoDBModelOutputContract.model_validate(minimal_output)
        assert contract.job_id == "test-job-001"
        assert contract.target_engine == "dynamodb"
        assert len(contract.access_patterns) == 1
        assert len(contract.table_definitions) == 1
        assert contract.validation_passed is True

    def test_sample_fixture_validates(self, sample_dynamodb_output):
        """The sample fixture must validate against the contract."""
        contract = DynamoDBModelOutputContract.model_validate(sample_dynamodb_output)
        assert contract.validation_passed is True
        assert len(contract.table_definitions) >= 1

    def test_missing_access_patterns_fails(self, minimal_output):
        minimal_output["access_patterns"] = []
        with pytest.raises(ValidationError, match="access_patterns"):
            DynamoDBModelOutputContract.model_validate(minimal_output)

    def test_missing_table_definitions_fails(self, minimal_output):
        minimal_output["table_definitions"] = []
        with pytest.raises(ValidationError, match="table_definitions"):
            DynamoDBModelOutputContract.model_validate(minimal_output)

    def test_missing_trade_offs_fails(self, minimal_output):
        minimal_output["trade_offs"] = []
        with pytest.raises(ValidationError, match="trade_offs"):
            DynamoDBModelOutputContract.model_validate(minimal_output)

    def test_pattern_group_required(self, minimal_output):
        del minimal_output["access_patterns"][0]["pattern_group"]
        with pytest.raises(ValidationError, match="pattern_group"):
            DynamoDBModelOutputContract.model_validate(minimal_output)

    def test_serialization_roundtrip(self, minimal_output):
        contract = DynamoDBModelOutputContract.model_validate(minimal_output)
        json_str = contract.model_dump_json()
        roundtrip = DynamoDBModelOutputContract.model_validate_json(json_str)
        assert roundtrip.job_id == contract.job_id
        assert len(roundtrip.access_patterns) == len(contract.access_patterns)


# ---------------------------------------------------------------------------
# TableDefinition validator tests
# ---------------------------------------------------------------------------


class TestTableDefinitionValidators:
    def test_single_entity_requires_attributes(self):
        """separate/identifying_relationship tables must have attributes, not entities."""
        with pytest.raises(ValidationError):
            TableDefinition(
                table_name="T",
                aggregate_pattern="separate",
                source_tables=["db.t"],
                partition_key=KeyDefinition(attribute_name="pk", attribute_type="S"),
                attributes=[],  # empty — should fail
                entities=None,
                gsis=[],
                item_count=1,
                item_size_bytes=1,
            )

    def test_item_collection_requires_entities(self):
        """item_collection tables must have entities, not attributes."""
        with pytest.raises(ValidationError):
            TableDefinition(
                table_name="T",
                aggregate_pattern="item_collection",
                source_tables=["db.t"],
                partition_key=KeyDefinition(attribute_name="pk", attribute_type="S"),
                attributes=None,
                entities=[],  # empty — should fail
                gsis=[],
                item_count=1,
                item_size_bytes=1,
            )

    def test_entities_only_valid_for_item_collection(self):
        """entities field is only valid when aggregate_pattern=item_collection."""
        with pytest.raises(ValidationError, match="item_collection"):
            TableDefinition(
                table_name="T",
                aggregate_pattern="separate",
                source_tables=["db.t"],
                partition_key=KeyDefinition(attribute_name="pk", attribute_type="S"),
                entities=[
                    EntityDefinition(
                        entity_type="USER",
                        source_table="db.t",
                        pk_template="USER#{id}",
                        sk_template="PROFILE",
                        attributes=[
                            AttributeDefinition(
                                name="id", type="S", source_table="db.t", source_column="id"
                            )
                        ],
                    )
                ],
                gsis=[],
                item_count=1,
                item_size_bytes=1,
            )


# ---------------------------------------------------------------------------
# HotPartitionEntry validator tests
# ---------------------------------------------------------------------------


class TestHotPartitionEntry:
    def test_at_risk_requires_mitigation(self):
        with pytest.raises(ValidationError, match="mitigation"):
            HotPartitionEntry(
                table_name="T",
                operation="read",
                rcu_or_wcu_per_second=2500,
                partition_limit=3000,
                utilization_pct=83.3,
                at_risk=True,
                contributing_patterns=["q1"],
                mitigation=None,  # required when at_risk=True
            )

    def test_at_risk_with_mitigation_passes(self):
        entry = HotPartitionEntry(
            table_name="T",
            operation="read",
            rcu_or_wcu_per_second=2500,
            partition_limit=3000,
            utilization_pct=83.3,
            at_risk=True,
            contributing_patterns=["q1"],
            mitigation="Add shard suffix",
        )
        assert entry.at_risk is True

    def test_not_at_risk_no_mitigation_passes(self):
        entry = HotPartitionEntry(
            table_name="T",
            operation="write",
            rcu_or_wcu_per_second=100,
            partition_limit=1000,
            utilization_pct=10.0,
            at_risk=False,
            contributing_patterns=["q1"],
        )
        assert entry.mitigation is None


# ---------------------------------------------------------------------------
# AttributeDefinition validator tests
# ---------------------------------------------------------------------------


class TestAttributeDefinition:
    def test_denormalized_requires_justification(self):
        with pytest.raises(ValidationError, match="justification"):
            AttributeDefinition(
                name="user_email",
                type="S",
                source_table="db.users",
                source_column="email",
                denormalized=True,
                justification=None,
            )

    def test_denormalized_with_justification_passes(self):
        attr = AttributeDefinition(
            name="user_email",
            type="S",
            source_table="db.users",
            source_column="email",
            denormalized=True,
            justification="AP-6 needs email inline to avoid GetItem round-trip",
        )
        assert attr.denormalized is True


# ---------------------------------------------------------------------------
# PE Review contract tests
# ---------------------------------------------------------------------------


class TestPEReviewResult:
    def test_approved_with_no_changes(self):
        review = PEReviewResult(
            verdict=ReviewVerdict.APPROVED,
            summary="Design looks good.",
            strengths=["Clean key design"],
        )
        assert review.verdict == ReviewVerdict.APPROVED
        assert len(review.change_requests) == 0

    def test_approved_with_blocker_fails(self):
        with pytest.raises(ValidationError, match="blocker"):
            PEReviewResult(
                verdict=ReviewVerdict.APPROVED,
                summary="Approved but has blockers?",
                change_requests=[
                    ChangeRequest(
                        category=ChangeCategory.TABLE_BOUNDARY,
                        severity=Severity.BLOCKER,
                        target="Users",
                        current_state="3 tables",
                        requested_change="Merge to 1",
                        rationale="Over-engineered",
                    )
                ],
            )

    def test_changes_needed_requires_change_requests(self):
        with pytest.raises(ValidationError, match="change_requests"):
            PEReviewResult(
                verdict=ReviewVerdict.CHANGES_REQUESTED,
                summary="Needs work.",
                change_requests=[],
            )

    def test_changes_needed_with_requests(self):
        review = PEReviewResult(
            verdict=ReviewVerdict.CHANGES_REQUESTED,
            summary="Needs work.",
            change_requests=[
                ChangeRequest(
                    category=ChangeCategory.OVER_ENGINEERING,
                    severity=Severity.MAJOR,
                    target="UserEmailLookup",
                    current_state="Dedicated lookup table",
                    requested_change="Use GSI on Users table",
                    rationale="Simpler, cheaper",
                )
            ],
        )
        assert len(review.change_requests) == 1

    def test_approved_with_minor_changes_ok(self):
        """Approved is fine with non-blocker change requests."""
        review = PEReviewResult(
            verdict=ReviewVerdict.APPROVED,
            summary="Good with minor notes.",
            change_requests=[
                ChangeRequest(
                    category=ChangeCategory.MISSING_TRADE_OFF,
                    severity=Severity.SUGGESTION,
                    target="trade_offs",
                    current_state="Missing PII note",
                    requested_change="Add PII spread note",
                    rationale="Compliance awareness",
                )
            ],
        )
        assert review.verdict == ReviewVerdict.APPROVED

    def test_scope_challenge_category(self):
        """PE can issue scope_challenge to make designer retry unsupported patterns."""
        review = PEReviewResult(
            verdict=ReviewVerdict.CHANGES_REQUESTED,
            summary="Some patterns marked unsupported can be served with pre-computed counters.",
            change_requests=[
                ChangeRequest(
                    category=ChangeCategory.SCOPE_CHALLENGE,
                    severity=Severity.MAJOR,
                    target="q-42",
                    current_state="Marked as unsupported (aggregation)",
                    requested_change="Redesign as pre-computed counter using DynamoDB Streams",
                    rationale="COUNT with GROUP BY on 5 distinct values is feasible via materialized counters",
                )
            ],
        )
        assert review.change_requests[0].category == ChangeCategory.SCOPE_CHALLENGE


# ---------------------------------------------------------------------------
# OpenSearch UnsupportedPattern query_ids field
# ---------------------------------------------------------------------------


class TestOpenSearchUnsupportedPatternQueryIds:
    def test_unsupported_pattern_without_query_ids_backward_compat(self):
        """Old-format UnsupportedPattern without query_ids still validates."""
        from src.contracts.opensearch_model_output import UnsupportedPattern

        pattern = UnsupportedPattern(
            source_query="INSERT INTO t1 SELECT ...",
            reason="transactional write",
            recommendation="Use DynamoDB",
        )
        assert pattern.query_ids == []

    def test_unsupported_pattern_with_query_ids(self):
        """New-format UnsupportedPattern with query_ids."""
        from src.contracts.opensearch_model_output import UnsupportedPattern

        pattern = UnsupportedPattern(
            query_ids=["q-10", "q-11"],
            source_query="INSERT INTO t1 SELECT ...",
            reason="transactional write",
            recommendation="Use DynamoDB",
        )
        assert pattern.query_ids == ["q-10", "q-11"]


# ---------------------------------------------------------------------------
# Post-Schema Router Output Contract
# ---------------------------------------------------------------------------


class TestRouterOutputContract:
    def test_empty_router_output(self):
        from src.contracts.post_schema_router_output import RouterOutput

        output = RouterOutput(job_id="test-123")
        assert output.routings == []
        assert output.terminal_queries == []
        assert output.cascade_depth == 0

    def test_router_output_with_routings(self):
        from src.contracts.post_schema_router_output import QueryRouting, RouterOutput

        output = RouterOutput(
            job_id="test-123",
            routings=[
                QueryRouting(
                    query_id="q-1",
                    from_engine="dynamodb",
                    to_engine="opensearch",
                    reason="full-text search",
                    cascade_depth=0,
                )
            ],
            terminal_queries=["q-99"],
            cascade_depth=0,
        )
        assert len(output.routings) == 1
        assert output.routings[0].to_engine == "opensearch"
        assert "q-99" in output.terminal_queries

    def test_query_routing_with_none_target(self):
        """to_engine=None means application-layer handling."""
        from src.contracts.post_schema_router_output import QueryRouting

        routing = QueryRouting(
            query_id="q-1",
            from_engine="dynamodb",
            to_engine=None,
            reason="complex OLAP",
        )
        assert routing.to_engine is None
