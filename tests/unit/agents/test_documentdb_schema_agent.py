"""Tests for DocumentDB schema design agent and output contract."""

from __future__ import annotations

import json

import pytest

from src.contracts.documentdb_model_output import (
    AccessPattern,
    CollectionDefinition,
    DocumentDBModelOutputContract,
    MigrationNote,
    UnsupportedPattern,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _minimal_collection(**overrides: object) -> dict:
    base: dict = {
        "collection_name": "users",
        "source_tables": ["app.users"],
        "embedded_entities": [],
        "referenced_collections": [],
        "indexes": [],
        "document_examples": [{"_id": 1, "name": "test"}],
        "estimated_avg_doc_size_kb": 0.5,
        "estimated_max_doc_size_kb": 2.0,
    }
    base.update(overrides)
    return base


def _minimal_access_pattern(**overrides: object) -> dict:
    base: dict = {
        "pattern_id": "AP-1",
        "description": "Get user by ID",
        "operation": "findOne",
        "collection_name": "users",
        "query_filter": {"_id": 1},
        "index_used": "_id_",
        "source_query_ids": ["q1"],
        "source_tables": ["public.users"],
        "design_rps": 150.0,
    }
    base.update(overrides)
    return base


def _minimal_contract(**overrides: object) -> dict:
    base: dict = {
        "job_id": "test-job",
        "source_database": "testdb",
        "collections": [_minimal_collection()],
        "access_patterns": [_minimal_access_pattern()],
        "trade_offs": [
            {
                "description": "Embedded tags for read performance",
                "impact": "Tags are stored inside each document instead of a separate collection, so reads are faster but tag updates must propagate to all documents.",
                "source_tables": ["db.tags"],
                "target_tables": ["users"],
                "query_ids": ["q1"],
                "engine": "documentdb",
            }
        ],
        "validation_passed": True,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Contract validation tests
# ---------------------------------------------------------------------------


class TestDocumentDBModelOutputContract:
    def test_minimal_valid(self) -> None:
        contract = DocumentDBModelOutputContract.model_validate(_minimal_contract())
        assert contract.target_engine == "documentdb"
        assert contract.contract_version == "1.0"
        assert len(contract.collections) == 1
        assert len(contract.access_patterns) == 1

    def test_requires_collections(self) -> None:
        with pytest.raises(ValueError):
            DocumentDBModelOutputContract.model_validate(_minimal_contract(collections=[]))

    def test_requires_access_patterns(self) -> None:
        with pytest.raises(ValueError):
            DocumentDBModelOutputContract.model_validate(_minimal_contract(access_patterns=[]))

    def test_requires_trade_offs(self) -> None:
        with pytest.raises(ValueError):
            DocumentDBModelOutputContract.model_validate(_minimal_contract(trade_offs=[]))


class TestCollectionDefinition:
    def test_valid_collection(self) -> None:
        col = CollectionDefinition.model_validate(_minimal_collection())
        assert col.collection_name == "users"

    def test_rejects_oversized_doc(self) -> None:
        with pytest.raises(Exception, match="16MB"):
            CollectionDefinition.model_validate(
                _minimal_collection(estimated_max_doc_size_kb=17000)
            )

    def test_embedded_entity(self) -> None:
        col = CollectionDefinition.model_validate(
            _minimal_collection(
                embedded_entities=[
                    {
                        "source_table": "app.tags",
                        "embed_path": "tags",
                        "strategy": "embed",
                        "avg_array_length": 3.0,
                        "max_array_length_estimate": 20.0,
                        "rationale": "Low cardinality, always read with parent",
                    }
                ]
            )
        )
        assert len(col.embedded_entities) == 1
        assert col.embedded_entities[0].strategy == "embed"

    def test_index_definition(self) -> None:
        col = CollectionDefinition.model_validate(
            _minimal_collection(
                indexes=[
                    {
                        "index_name": "idx_users_email",
                        "keys": {"email": 1},
                        "index_type": "single",
                        "purpose": "User lookup by email",
                    }
                ]
            )
        )
        assert len(col.indexes) == 1
        assert col.indexes[0].keys == {"email": 1}


class TestAccessPattern:
    def test_find_pattern(self) -> None:
        ap = AccessPattern.model_validate(_minimal_access_pattern())
        assert ap.operation == "findOne"

    def test_aggregate_pattern(self) -> None:
        ap = AccessPattern.model_validate(
            _minimal_access_pattern(
                operation="aggregate",
                pipeline=[{"$group": {"_id": "$status", "count": {"$sum": 1}}}],
            )
        )
        assert ap.pipeline is not None
        assert len(ap.pipeline) == 1


class TestUnsupportedPattern:
    def test_valid(self) -> None:
        up = UnsupportedPattern.model_validate(
            {
                "source_query_ids": ["q5"],
                "reason": "$graphLookup not supported in DocumentDB",
                "workaround": "Use application-layer traversal or Neptune",
            }
        )
        assert up.workaround is not None


class TestMigrationNote:
    def test_valid(self) -> None:
        mn = MigrationNote.model_validate(
            {
                "object_name": "update_post_count",
                "object_type": "trigger",
                "source_table": "app.posts",
                "application_logic_required": "Implement post count increment in application layer",
            }
        )
        assert mn.object_type == "trigger"


# ---------------------------------------------------------------------------
# Schema agent trace tests
# ---------------------------------------------------------------------------


class TestSchemaDesignTrace:
    def test_trace_structure(self) -> None:
        from src.tools.schema.documentdb_schema_agent import SchemaDesignTrace

        trace = SchemaDesignTrace()
        contract = DocumentDBModelOutputContract.model_validate(_minimal_contract())
        trace.log_designer(0, 5.0, contract)
        result = trace.to_dict()
        assert result["total_iterations"] == 1
        assert result["iterations"][0]["designer"]["collections"] == 1

    def test_trace_pe_error(self) -> None:
        from src.tools.schema.documentdb_schema_agent import SchemaDesignTrace

        trace = SchemaDesignTrace()
        trace.log_pe_error(0, "timeout")
        result = trace.to_dict()
        assert result["iterations"][0]["pe_review"]["error"] == "timeout"


# ---------------------------------------------------------------------------
# JSON round-trip
# ---------------------------------------------------------------------------


class TestJsonRoundTrip:
    def test_serialize_deserialize(self) -> None:
        contract = DocumentDBModelOutputContract.model_validate(_minimal_contract())
        json_str = contract.model_dump_json(indent=2)
        parsed = json.loads(json_str)
        restored = DocumentDBModelOutputContract.model_validate(parsed)
        assert restored.job_id == contract.job_id
        assert len(restored.collections) == len(contract.collections)
