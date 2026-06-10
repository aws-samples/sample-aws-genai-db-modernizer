"""DocumentDB seeder for load testing.

Implements :class:`BaseSeeder` for DocumentDB. Generates synthetic documents
with deterministic primary keys and bulk-inserts them via pymongo. Connects
using IAM auth (MONGODB-AWS mechanism — bootstrapped by the provisioner).

The seeder keeps document generation deliberately minimal: each document has
a ``primary_id`` field with a padded sequential value (``doc-000001``,
``doc-000002``, ...). The script generator (commit 2C) uses the
``primary_key_pattern`` from the SeedManifest to generate random keys that
match seeded values at test time.

Required schema_output keys (set by coordinator post-provisioning):

  - ``_documentdb_endpoint`` (str): cluster endpoint hostname
  - ``_documentdb_replica_count`` (int, default 0): for read preference
  - ``_test_config`` (Any with ``.scale_factor``)
  - ``_collector_output`` (dict): for source row counts via ``schema.tables``

Document generation produces:

  - One document per ``min(source_rows * scale_factor, max_items_per_table)``
  - Each document has ``primary_id`` (deterministic, indexable)
  - Embedded entities (where ``strategy == "embed"``) get a stub array
    of synthetic children so script generator can exercise embedded reads
"""

import time
import urllib.parse
from typing import Any

import structlog

from src.agents.load_test.base import BaseSeeder
from src.agents.load_test.documentdb.provisioner import DOCDB_CA_BUNDLE_PATH
from src.agents.load_test.models import SeedManifest

logger = structlog.get_logger()

# Naming conventions
COLLECTION_PREFIX = "LoadTest_"
DEFAULT_DB_NAME = "loadtest"
PRIMARY_KEY_FIELD = "primary_id"
PRIMARY_KEY_WIDTH = 6  # zero-padded width for deterministic keys

# Embedding stub size (commit 2C may refine based on actual index analysis)
EMBED_STUB_CHILD_COUNT = 5

# Bulk insert chunk size — DocumentDB has a 16 MB max document and
# similar per-batch limits; 1000 docs per batch is a safe default
BULK_INSERT_CHUNK_SIZE = 1000

# Connection settings
CONNECTION_PORT = 27017
CONNECTION_TIMEOUT_MS = 30_000


class DocumentDBSeeder(BaseSeeder):
    """Seeds DocumentDB collections with synthetic data for load testing."""

    def __init__(self, region: str = "us-east-1") -> None:
        self.region = region

    # =========================================================================
    # Public API
    # =========================================================================

    def seed(self, schema_output: Any, max_items_per_table: int = 10_000) -> SeedManifest:
        """Generate synthetic documents and bulk-insert into DocumentDB."""
        start = time.time()

        cluster_endpoint = self._get_required(schema_output, "_documentdb_endpoint")
        replica_count = int(schema_output.get("_documentdb_replica_count", 0) or 0)
        scale_factor = self._get_scale_factor(schema_output)
        collector_output = schema_output.get("_collector_output", {})

        # Lazy import: pymongo not always available in test environments
        from pymongo import MongoClient  # noqa: PLC0415

        uri = self._build_connection_uri(cluster_endpoint, replica_count, role="seeder")
        logger.info(
            "documentdb_seeder_connecting",
            endpoint=cluster_endpoint,
            replica_count=replica_count,
        )

        client: MongoClient[Any] = MongoClient(uri, serverSelectionTimeoutMS=CONNECTION_TIMEOUT_MS)
        try:
            db = client[DEFAULT_DB_NAME]

            resources: dict[str, Any] = {}
            total_items = 0

            for collection_def in schema_output.get("collections", []):
                seed_info = self._seed_collection(
                    db, collection_def, collector_output, scale_factor, max_items_per_table
                )
                if seed_info is None:
                    continue
                resources[seed_info["source_table"]] = seed_info["manifest_entry"]
                total_items += seed_info["doc_count"]

        finally:
            client.close()

        duration = round(time.time() - start, 2)
        logger.info(
            "documentdb_seeder_complete",
            total_items=total_items,
            collections=len(resources),
            duration_seconds=duration,
        )
        return SeedManifest(resources=resources, total_items=total_items, duration_seconds=duration)

    # =========================================================================
    # Internal helpers
    # =========================================================================

    def _get_required(self, schema_output: Any, key: str) -> str:
        value = schema_output.get(key)
        if not value:
            raise RuntimeError(
                f"DocumentDB seeder requires schema_output['{key}'] "
                f"(set by coordinator post-provisioning)"
            )
        return str(value)

    def _get_scale_factor(self, schema_output: Any) -> float:
        test_config = schema_output.get("_test_config")
        if test_config is None:
            return 1.0
        return float(getattr(test_config, "scale_factor", 1.0))

    def _build_connection_uri(self, endpoint: str, replica_count: int, role: str) -> str:
        """Build pymongo connection URI with MONGODB-AWS auth.

        ``role``: "seeder" prefers primary; "runner" prefers secondary if replicas exist.
        DocumentDB seeder writes go to primary regardless, so we just default to primary.
        """
        read_pref = "secondaryPreferred" if (replica_count > 0 and role == "runner") else "primary"

        params = {
            "tls": "true",
            "tlsCAFile": DOCDB_CA_BUNDLE_PATH,
            "replicaSet": "rs0",
            "authSource": "$external",
            "authMechanism": "MONGODB-AWS",
            "readPreference": read_pref,
            "retryWrites": "false",
        }
        encoded = urllib.parse.urlencode(params)
        return f"mongodb://{endpoint}:{CONNECTION_PORT}/{DEFAULT_DB_NAME}?{encoded}"

    def _seed_collection(
        self,
        db: Any,
        collection_def: dict[str, Any],
        collector_output: dict[str, Any],
        scale_factor: float,
        max_items_per_table: int,
    ) -> dict[str, Any] | None:
        """Seed one collection. Returns metadata for the SeedManifest, or None if skipped."""
        source_tables = collection_def.get("source_tables") or []
        if not source_tables:
            return None
        primary_table = source_tables[0]

        source_rows = self._get_source_rows(collector_output, primary_table)
        doc_count = min(int(source_rows * scale_factor), max_items_per_table)
        if doc_count <= 0:
            logger.info(
                "documentdb_seeder_skip_empty_collection",
                source_table=primary_table,
                source_rows=source_rows,
            )
            return None

        collection_name = f"{COLLECTION_PREFIX}{primary_table}"
        collection = db[collection_name]

        docs = self._generate_documents(collection_def, doc_count)
        self._bulk_insert_chunked(collection, docs)

        logger.info(
            "documentdb_seeder_collection_done",
            collection_name=collection_name,
            doc_count=doc_count,
        )

        return {
            "source_table": primary_table,
            "doc_count": doc_count,
            "manifest_entry": {
                "collection_name": collection_name,
                "document_count": doc_count,
                "primary_key_field": PRIMARY_KEY_FIELD,
                "primary_key_pattern": f"doc-{{index:0{PRIMARY_KEY_WIDTH}d}}",
                "primary_key_count": doc_count,
                "embedded_entity_paths": [
                    e.get("embed_path") or e.get("source_table") or "embedded"
                    for e in collection_def.get("embedded_entities") or []
                    if e.get("strategy") == "embed"
                ],
            },
        }

    def _get_source_rows(self, collector_output: dict[str, Any], table_name: str) -> int:
        """Look up source table row count from collector_output."""
        tables = collector_output.get("schema", {}).get("tables", [])
        for table in tables:
            if table.get("table_name") == table_name:
                return int(table.get("row_count", 0) or 0)
        return 0

    def _generate_documents(
        self, collection_def: dict[str, Any], doc_count: int
    ) -> list[dict[str, Any]]:
        """Generate ``doc_count`` synthetic documents for one collection.

        Each document has:
          - primary_id (deterministic, indexable for lookup queries)
          - One value field per index key (so indexed queries return docs)
          - Embedded child arrays for each embed-strategy entity

        Document size is intentionally minimal — load testing measures
        operation latency, not realistic data fidelity.
        """
        embedded_entities = [
            e
            for e in (collection_def.get("embedded_entities") or [])
            if e.get("strategy") == "embed"
        ]
        # Field names referenced in any index — we populate these so indexed
        # queries actually return documents
        index_fields: set[str] = set()
        for index in collection_def.get("indexes") or []:
            for key in index.get("keys") or {}:
                if key not in {"_id", PRIMARY_KEY_FIELD}:
                    index_fields.add(key)

        docs: list[dict[str, Any]] = []
        for i in range(doc_count):
            doc: dict[str, Any] = {
                PRIMARY_KEY_FIELD: f"doc-{i:0{PRIMARY_KEY_WIDTH}d}",
            }
            for field_name in index_fields:
                doc[field_name] = f"value-{i:0{PRIMARY_KEY_WIDTH}d}"

            for entity in embedded_entities:
                embed_path = entity.get("embed_path") or entity.get("source_table") or "embedded"
                array_max_length = entity.get("array_max_length")
                child_count = min(
                    EMBED_STUB_CHILD_COUNT, int(array_max_length or EMBED_STUB_CHILD_COUNT)
                )
                doc[embed_path] = [
                    {
                        "child_id": f"child-{i:0{PRIMARY_KEY_WIDTH}d}-{j:03d}",
                        "value": j,
                    }
                    for j in range(child_count)
                ]

            docs.append(doc)
        return docs

    def _bulk_insert_chunked(self, collection: Any, docs: list[dict[str, Any]]) -> None:
        """Insert documents in chunks to respect DocumentDB's per-batch size limits."""
        for chunk_start in range(0, len(docs), BULK_INSERT_CHUNK_SIZE):
            chunk = docs[chunk_start : chunk_start + BULK_INSERT_CHUNK_SIZE]
            collection.insert_many(chunk, ordered=False)
