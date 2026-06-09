"""Tests for DocumentDB seeder.

Mocks pymongo MongoClient + collection insert_many to verify:
  - Connection URI uses MONGODB-AWS auth (no password)
  - Read preference matches replica count + role
  - Documents have deterministic primary_id (doc-000001, doc-000002, ...)
  - Indexed fields are populated so queries return matches
  - Embedded entities produce stub child arrays
  - Document count respects scale_factor and max_items_per_table cap
  - Empty source table → collection skipped
  - Multiple collections → multiple bulk inserts
  - SeedManifest carries primary_key_pattern + count for script generator
  - Required schema_output keys → clear error if missing
  - Bulk insert chunks large doc lists
"""

from typing import Any
from unittest.mock import MagicMock, call

import pytest

from src.agents.load_test.documentdb.seeder import (
    BULK_INSERT_CHUNK_SIZE,
    COLLECTION_PREFIX,
    DEFAULT_DB_NAME,
    PRIMARY_KEY_FIELD,
    PRIMARY_KEY_WIDTH,
    DocumentDBSeeder,
)
from src.agents.load_test.models import SeedManifest

# =============================================================================
# Fixtures
# =============================================================================


class _FakeTestConfig:
    def __init__(self, scale_factor: float = 1.0) -> None:
        self.scale_factor = scale_factor


@pytest.fixture
def schema_output() -> dict[str, Any]:
    """Minimal schema_output with all keys the seeder requires."""
    return {
        "_documentdb_endpoint": "loadtest-abc.cluster-xyz.us-east-1.docdb.amazonaws.com",
        "_documentdb_replica_count": 0,
        "_test_config": _FakeTestConfig(),
        "_collector_output": {
            "schema": {"tables": [{"table_name": "users", "row_count": 5_000}]},
        },
        "collections": [
            {
                "source_tables": ["users"],
                "embedded_entities": [],
                "indexes": [
                    {"keys": {"_id": 1}},
                    {"keys": {"email": 1}},
                ],
            }
        ],
    }


@pytest.fixture
def mock_mongo_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace pymongo.MongoClient with a MagicMock — captures URI + writes."""
    mock_client = MagicMock()

    # client[db_name] returns a database mock; database[collection_name] returns
    # a collection mock with insert_many capturing calls
    db_mock = MagicMock()
    collection_mock = MagicMock()
    db_mock.__getitem__.return_value = collection_mock
    mock_client.return_value.__getitem__.return_value = db_mock

    fake_pymongo = MagicMock(MongoClient=mock_client)
    monkeypatch.setitem(__import__("sys").modules, "pymongo", fake_pymongo)
    return mock_client


@pytest.fixture
def seeder() -> DocumentDBSeeder:
    return DocumentDBSeeder(region="us-east-1")


# =============================================================================
# Connection URI
# =============================================================================


class TestConnectionUri:
    def test_seeder_uses_mongodb_aws_auth(
        self,
        seeder: DocumentDBSeeder,
        schema_output: dict[str, Any],
        mock_mongo_client: MagicMock,
    ) -> None:
        seeder.seed(schema_output, max_items_per_table=100)
        uri = mock_mongo_client.call_args.args[0]
        assert "authMechanism=MONGODB-AWS" in uri
        assert "authSource=%24external" in uri
        assert "tls=true" in uri
        assert "replicaSet=rs0" in uri

    def test_no_password_in_uri(
        self,
        seeder: DocumentDBSeeder,
        schema_output: dict[str, Any],
        mock_mongo_client: MagicMock,
    ) -> None:
        seeder.seed(schema_output, max_items_per_table=100)
        uri = mock_mongo_client.call_args.args[0]
        # IAM auth — no user:password@ in URI
        assert "@" not in uri.split("?")[0]  # the path part has no creds

    def test_seeder_role_uses_primary_read_preference(
        self,
        seeder: DocumentDBSeeder,
        schema_output: dict[str, Any],
        mock_mongo_client: MagicMock,
    ) -> None:
        # Even with replicas, seeder writes go to primary
        schema_output["_documentdb_replica_count"] = 2
        seeder.seed(schema_output, max_items_per_table=100)
        uri = mock_mongo_client.call_args.args[0]
        assert "readPreference=primary" in uri


# =============================================================================
# Document generation
# =============================================================================


class TestDocumentGeneration:
    def test_documents_have_deterministic_primary_id(
        self,
        seeder: DocumentDBSeeder,
        schema_output: dict[str, Any],
        mock_mongo_client: MagicMock,
    ) -> None:
        seeder.seed(schema_output, max_items_per_table=10)
        # Inspect the first call to insert_many
        db_mock = mock_mongo_client.return_value.__getitem__.return_value
        collection_mock = db_mock.__getitem__.return_value
        docs = collection_mock.insert_many.call_args.args[0]

        assert len(docs) == 10
        assert docs[0][PRIMARY_KEY_FIELD] == f"doc-{0:0{PRIMARY_KEY_WIDTH}d}"
        assert docs[5][PRIMARY_KEY_FIELD] == f"doc-{5:0{PRIMARY_KEY_WIDTH}d}"
        assert docs[9][PRIMARY_KEY_FIELD] == f"doc-{9:0{PRIMARY_KEY_WIDTH}d}"

    def test_indexed_fields_are_populated(
        self,
        seeder: DocumentDBSeeder,
        schema_output: dict[str, Any],
        mock_mongo_client: MagicMock,
    ) -> None:
        """Indexed fields must have values so indexed queries return docs."""
        seeder.seed(schema_output, max_items_per_table=5)
        db_mock = mock_mongo_client.return_value.__getitem__.return_value
        collection_mock = db_mock.__getitem__.return_value
        docs = collection_mock.insert_many.call_args.args[0]

        # Index "email" is in schema_output → must be in docs
        assert all("email" in doc for doc in docs)
        # _id is auto-generated by MongoDB; we don't populate it
        assert all("_id" not in doc for doc in docs)

    def test_embedded_entity_creates_stub_array(
        self,
        seeder: DocumentDBSeeder,
        schema_output: dict[str, Any],
        mock_mongo_client: MagicMock,
    ) -> None:
        schema_output["collections"][0]["embedded_entities"] = [
            {
                "source_table": "addresses",
                "embed_path": "addresses",
                "strategy": "embed",
                "array_max_length": 3,
            }
        ]
        seeder.seed(schema_output, max_items_per_table=5)
        db_mock = mock_mongo_client.return_value.__getitem__.return_value
        collection_mock = db_mock.__getitem__.return_value
        docs = collection_mock.insert_many.call_args.args[0]

        for doc in docs:
            assert "addresses" in doc
            assert isinstance(doc["addresses"], list)
            assert len(doc["addresses"]) == 3  # respects array_max_length
            assert all("child_id" in child for child in doc["addresses"])

    def test_referenced_entity_does_not_create_array(
        self,
        seeder: DocumentDBSeeder,
        schema_output: dict[str, Any],
        mock_mongo_client: MagicMock,
    ) -> None:
        schema_output["collections"][0]["embedded_entities"] = [
            {
                "source_table": "orders",
                "strategy": "reference",
            }
        ]
        seeder.seed(schema_output, max_items_per_table=5)
        db_mock = mock_mongo_client.return_value.__getitem__.return_value
        collection_mock = db_mock.__getitem__.return_value
        docs = collection_mock.insert_many.call_args.args[0]

        # No "orders" field in docs — only embed strategy creates fields
        assert all("orders" not in doc for doc in docs)


# =============================================================================
# Document count and scaling
# =============================================================================


class TestDocumentCount:
    def test_capped_at_max_items_per_table(
        self,
        seeder: DocumentDBSeeder,
        schema_output: dict[str, Any],
        mock_mongo_client: MagicMock,
    ) -> None:
        # Source has 5000 rows; we cap at 100
        seeder.seed(schema_output, max_items_per_table=100)
        db_mock = mock_mongo_client.return_value.__getitem__.return_value
        collection_mock = db_mock.__getitem__.return_value
        docs = collection_mock.insert_many.call_args.args[0]
        assert len(docs) == 100

    def test_scale_factor_reduces_doc_count(
        self,
        seeder: DocumentDBSeeder,
        schema_output: dict[str, Any],
        mock_mongo_client: MagicMock,
    ) -> None:
        schema_output["_test_config"] = _FakeTestConfig(scale_factor=0.1)
        # 5000 source rows × 0.1 = 500, max=10000 → 500 docs
        seeder.seed(schema_output, max_items_per_table=10_000)
        db_mock = mock_mongo_client.return_value.__getitem__.return_value
        collection_mock = db_mock.__getitem__.return_value
        docs = collection_mock.insert_many.call_args.args[0]
        assert len(docs) == 500

    def test_zero_source_rows_skips_collection(
        self,
        seeder: DocumentDBSeeder,
        schema_output: dict[str, Any],
        mock_mongo_client: MagicMock,
    ) -> None:
        schema_output["_collector_output"]["schema"]["tables"][0]["row_count"] = 0
        manifest = seeder.seed(schema_output, max_items_per_table=100)

        db_mock = mock_mongo_client.return_value.__getitem__.return_value
        collection_mock = db_mock.__getitem__.return_value
        # No insert_many should be called
        assert not collection_mock.insert_many.called
        assert manifest.total_items == 0
        assert manifest.resources == {}

    def test_collection_with_no_source_tables_skipped(
        self,
        seeder: DocumentDBSeeder,
        schema_output: dict[str, Any],
        mock_mongo_client: MagicMock,
    ) -> None:
        schema_output["collections"][0]["source_tables"] = []
        manifest = seeder.seed(schema_output, max_items_per_table=100)
        assert manifest.total_items == 0


# =============================================================================
# Multiple collections
# =============================================================================


class TestMultipleCollections:
    def test_multiple_collections_each_get_bulk_insert(
        self,
        seeder: DocumentDBSeeder,
        schema_output: dict[str, Any],
        mock_mongo_client: MagicMock,
    ) -> None:
        schema_output["collections"] = [
            {
                "source_tables": ["users"],
                "embedded_entities": [],
                "indexes": [{"keys": {"_id": 1}}],
            },
            {
                "source_tables": ["orders"],
                "embedded_entities": [],
                "indexes": [{"keys": {"_id": 1}}],
            },
        ]
        schema_output["_collector_output"]["schema"]["tables"] = [
            {"table_name": "users", "row_count": 50},
            {"table_name": "orders", "row_count": 100},
        ]
        manifest = seeder.seed(schema_output, max_items_per_table=1000)

        assert "users" in manifest.resources
        assert "orders" in manifest.resources
        assert manifest.resources["users"]["document_count"] == 50
        assert manifest.resources["orders"]["document_count"] == 100
        assert manifest.total_items == 150

    def test_collection_names_have_loadtest_prefix(
        self,
        seeder: DocumentDBSeeder,
        schema_output: dict[str, Any],
        mock_mongo_client: MagicMock,
    ) -> None:
        manifest = seeder.seed(schema_output, max_items_per_table=10)
        assert manifest.resources["users"]["collection_name"] == f"{COLLECTION_PREFIX}users"


# =============================================================================
# SeedManifest contents (drives script generator)
# =============================================================================


class TestSeedManifestContents:
    def test_manifest_includes_primary_key_pattern(
        self,
        seeder: DocumentDBSeeder,
        schema_output: dict[str, Any],
        mock_mongo_client: MagicMock,
    ) -> None:
        manifest = seeder.seed(schema_output, max_items_per_table=10)
        entry = manifest.resources["users"]
        assert entry["primary_key_field"] == PRIMARY_KEY_FIELD
        assert entry["primary_key_pattern"] == f"doc-{{index:0{PRIMARY_KEY_WIDTH}d}}"
        assert entry["primary_key_count"] == 10

    def test_manifest_lists_embedded_paths(
        self,
        seeder: DocumentDBSeeder,
        schema_output: dict[str, Any],
        mock_mongo_client: MagicMock,
    ) -> None:
        schema_output["collections"][0]["embedded_entities"] = [
            {"source_table": "addresses", "strategy": "embed", "embed_path": "addresses"},
            {"source_table": "orders", "strategy": "reference"},  # not embedded
        ]
        manifest = seeder.seed(schema_output, max_items_per_table=10)
        entry = manifest.resources["users"]
        assert entry["embedded_entity_paths"] == ["addresses"]

    def test_manifest_returns_seed_manifest_type(
        self,
        seeder: DocumentDBSeeder,
        schema_output: dict[str, Any],
        mock_mongo_client: MagicMock,
    ) -> None:
        manifest = seeder.seed(schema_output, max_items_per_table=10)
        assert isinstance(manifest, SeedManifest)
        assert manifest.duration_seconds >= 0


# =============================================================================
# Validation
# =============================================================================


class TestValidation:
    def test_missing_endpoint_raises(
        self, seeder: DocumentDBSeeder, schema_output: dict[str, Any]
    ) -> None:
        del schema_output["_documentdb_endpoint"]
        with pytest.raises(RuntimeError, match="_documentdb_endpoint"):
            seeder.seed(schema_output, max_items_per_table=100)


# =============================================================================
# Bulk insert chunking
# =============================================================================


class TestBulkInsertChunking:
    def test_large_doc_set_split_into_chunks(
        self,
        seeder: DocumentDBSeeder,
        schema_output: dict[str, Any],
        mock_mongo_client: MagicMock,
    ) -> None:
        # 2,500 docs → 3 chunks (1000 + 1000 + 500) at default chunk size
        schema_output["_collector_output"]["schema"]["tables"][0]["row_count"] = 2_500
        seeder.seed(schema_output, max_items_per_table=2_500)

        db_mock = mock_mongo_client.return_value.__getitem__.return_value
        collection_mock = db_mock.__getitem__.return_value

        # Should have 3 calls to insert_many (one per chunk)
        assert collection_mock.insert_many.call_count == 3

        chunk_sizes = [len(c.args[0]) for c in collection_mock.insert_many.call_args_list]
        assert chunk_sizes == [BULK_INSERT_CHUNK_SIZE, BULK_INSERT_CHUNK_SIZE, 500]

    def test_insert_many_uses_unordered_writes(
        self,
        seeder: DocumentDBSeeder,
        schema_output: dict[str, Any],
        mock_mongo_client: MagicMock,
    ) -> None:
        seeder.seed(schema_output, max_items_per_table=10)
        db_mock = mock_mongo_client.return_value.__getitem__.return_value
        collection_mock = db_mock.__getitem__.return_value
        # ordered=False allows partial-failure tolerance + better throughput
        assert collection_mock.insert_many.call_args.kwargs["ordered"] is False


# =============================================================================
# Database name + collection routing
# =============================================================================


class TestDatabaseRouting:
    def test_uses_default_database_name(
        self,
        seeder: DocumentDBSeeder,
        schema_output: dict[str, Any],
        mock_mongo_client: MagicMock,
    ) -> None:
        seeder.seed(schema_output, max_items_per_table=10)
        # client[DEFAULT_DB_NAME] was called
        client_instance = mock_mongo_client.return_value
        assert call(DEFAULT_DB_NAME) in client_instance.__getitem__.call_args_list

    def test_collection_indexed_with_loadtest_prefix(
        self,
        seeder: DocumentDBSeeder,
        schema_output: dict[str, Any],
        mock_mongo_client: MagicMock,
    ) -> None:
        seeder.seed(schema_output, max_items_per_table=10)
        db_mock = mock_mongo_client.return_value.__getitem__.return_value
        # db[f"{COLLECTION_PREFIX}users"] was called
        assert call(f"{COLLECTION_PREFIX}users") in db_mock.__getitem__.call_args_list


# =============================================================================
# Connection lifecycle
# =============================================================================


class TestConnectionLifecycle:
    def test_client_closed_after_seeding(
        self,
        seeder: DocumentDBSeeder,
        schema_output: dict[str, Any],
        mock_mongo_client: MagicMock,
    ) -> None:
        seeder.seed(schema_output, max_items_per_table=10)
        assert mock_mongo_client.return_value.close.called

    def test_client_closed_even_on_failure(
        self,
        seeder: DocumentDBSeeder,
        schema_output: dict[str, Any],
        mock_mongo_client: MagicMock,
    ) -> None:
        # Force insert_many to raise
        db_mock = mock_mongo_client.return_value.__getitem__.return_value
        collection_mock = db_mock.__getitem__.return_value
        collection_mock.insert_many.side_effect = RuntimeError("network down")

        with pytest.raises(RuntimeError, match="network down"):
            seeder.seed(schema_output, max_items_per_table=10)

        # Client must still be closed
        assert mock_mongo_client.return_value.close.called
