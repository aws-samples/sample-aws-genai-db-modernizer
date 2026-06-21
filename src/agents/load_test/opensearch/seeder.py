"""OpenSearch seeder for load testing.

Seeds index mappings and documents from the schema output into the
provisioned OpenSearch domain using the opensearch-py client.
"""

import time

import structlog

from src.agents.load_test.base import BaseSeeder
from src.agents.load_test.models import SeedManifest

logger = structlog.get_logger()

DEFAULT_DOCS_PER_INDEX = 1000


class OpenSearchSeeder(BaseSeeder):
    """Seeds an OpenSearch domain with synthetic documents based on index designs."""

    def __init__(self, region: str = "us-east-1"):
        self.region = region

    def seed(self, schema_output: dict, max_items_per_table: int = 10_000) -> SeedManifest:
        """Create indices and seed documents matching the schema design."""
        from opensearchpy import OpenSearch  # type: ignore[import-untyped]

        endpoint = schema_output.get("_opensearch_endpoint", "")
        master_user = schema_output.get("_opensearch_master_user", "loadtest_admin")
        master_password = schema_output.get("_opensearch_master_password", "")

        if not endpoint:
            raise ValueError(
                "No _opensearch_endpoint in schema_output — "
                "ensure provisioner enriched schema_output"
            )

        client = OpenSearch(
            hosts=[{"host": endpoint, "port": 443}],
            http_auth=(master_user, master_password),
            use_ssl=True,
            verify_certs=True,
            ssl_show_warn=False,
            connection_class=None,
        )

        start = time.time()
        resources: dict = {}
        total_items = 0

        # Seed search-workload indices
        for idx_design in schema_output.get("index_designs", []):
            index_name = idx_design.get("index_name", "")
            if not index_name:
                continue

            self._create_index(client, idx_design)
            docs_to_seed = min(
                max_items_per_table,
                DEFAULT_DOCS_PER_INDEX,
            )
            seeded = self._seed_index(client, idx_design, docs_to_seed)
            resources[index_name] = {
                "index_name": index_name,
                "docs_seeded": seeded,
                "source_tables": idx_design.get("source_tables", []),
            }
            total_items += seeded
            logger.info("seeded_index", index_name=index_name, docs=seeded)

        # Seed time-series data streams
        for ds_design in schema_output.get("data_stream_designs", []):
            ds_name = ds_design.get("data_stream_name", "")
            if not ds_name:
                continue

            self._create_data_stream(client, ds_design)
            docs_to_seed = min(max_items_per_table, DEFAULT_DOCS_PER_INDEX)
            seeded = self._seed_data_stream(client, ds_design, docs_to_seed)
            resources[ds_name] = {
                "data_stream_name": ds_name,
                "docs_seeded": seeded,
                "source_tables": ds_design.get("source_tables", []),
            }
            total_items += seeded
            logger.info("seeded_data_stream", data_stream_name=ds_name, docs=seeded)

        # Refresh all indices for immediate searchability
        client.indices.refresh(index="_all")

        duration = time.time() - start
        return SeedManifest(
            resources=resources,
            total_items=total_items,
            duration_seconds=round(duration, 2),
        )

    def _create_index(self, client, idx_design: dict) -> None:
        """Create index with mappings and settings from schema design."""
        index_name = idx_design["index_name"]
        settings = idx_design.get("settings", {})
        field_mappings = idx_design.get("field_mappings", [])

        body: dict = {
            "settings": {
                "number_of_shards": settings.get("number_of_shards", 1),
                "number_of_replicas": settings.get("number_of_replicas", 0),
                "refresh_interval": settings.get("refresh_interval", "1s"),
            },
            "mappings": {"properties": self._build_mapping_properties(field_mappings)},
        }

        # Add custom analyzers if defined
        custom_analyzers = settings.get("custom_analyzers", [])
        if custom_analyzers:
            body["settings"]["analysis"] = {"analyzer": {}}
            for analyzer in custom_analyzers:
                body["settings"]["analysis"]["analyzer"][analyzer.get("name", "custom")] = {
                    "tokenizer": analyzer.get("tokenizer", "standard"),
                    "filter": analyzer.get("filter", []),
                    "char_filter": analyzer.get("char_filter", []),
                }

        if client.indices.exists(index=index_name):
            client.indices.delete(index=index_name)

        client.indices.create(index=index_name, body=body)
        logger.info("created_index", index_name=index_name)

    def _create_data_stream(self, client, ds_design: dict) -> None:
        """Create index template and data stream."""
        ds_name = ds_design["data_stream_name"]
        template = ds_design.get("index_template", {})
        template_name = template.get("template_name", f"{ds_name}-template")
        index_patterns = template.get("index_patterns", [f"{ds_name}-*"])
        settings = template.get("settings", {})
        field_mappings = template.get("field_mappings", [])

        body = {
            "index_patterns": index_patterns,
            "data_stream": {},
            "template": {
                "settings": {
                    "number_of_shards": settings.get("number_of_shards", 1),
                    "number_of_replicas": settings.get("number_of_replicas", 0),
                },
                "mappings": {
                    "properties": self._build_mapping_properties(field_mappings),
                },
            },
        }

        client.indices.put_index_template(name=template_name, body=body)
        # Data stream is auto-created on first index request
        logger.info("created_data_stream_template", template_name=template_name)

    def _build_mapping_properties(self, field_mappings: list[dict]) -> dict:
        """Convert field mapping list to OpenSearch properties dict."""
        properties: dict = {}
        for fm in field_mappings:
            field_name = fm.get("field_name", "")
            if not field_name:
                continue

            prop: dict = {"type": fm.get("field_type", "keyword")}
            if fm.get("analyzer"):
                prop["analyzer"] = fm["analyzer"]
            if fm.get("search_analyzer"):
                prop["search_analyzer"] = fm["search_analyzer"]
            if fm.get("multi_field"):
                prop["fields"] = {"keyword": {"type": "keyword", "ignore_above": 256}}

            properties[field_name] = prop

        return properties

    def _seed_index(self, client, idx_design: dict, count: int) -> int:
        """Bulk-index synthetic documents into an index."""
        from opensearchpy import helpers  # type: ignore[import-untyped]

        index_name = idx_design["index_name"]
        field_mappings = idx_design.get("field_mappings", [])

        actions = []
        for i in range(1, count + 1):
            doc = self._generate_document(field_mappings, i)
            actions.append({"_index": index_name, "_id": str(i), "_source": doc})

        if actions:
            helpers.bulk(client, actions, chunk_size=500, raise_on_error=False)

        return count

    def _seed_data_stream(self, client, ds_design: dict, count: int) -> int:
        """Bulk-index synthetic time-series documents into a data stream."""
        from datetime import UTC, datetime, timedelta

        from opensearchpy import helpers  # type: ignore[import-untyped]

        ds_name = ds_design["data_stream_name"]
        template = ds_design.get("index_template", {})
        field_mappings = template.get("field_mappings", [])
        timestamp_field = ds_design.get("timestamp_field", "@timestamp")

        base_time = datetime.now(UTC) - timedelta(hours=1)
        actions = []
        for i in range(1, count + 1):
            doc = self._generate_document(field_mappings, i)
            doc[timestamp_field] = (base_time + timedelta(seconds=i)).isoformat()
            actions.append({"_index": ds_name, "_source": doc})

        if actions:
            helpers.bulk(client, actions, chunk_size=500, raise_on_error=False)

        return count

    def _generate_document(self, field_mappings: list[dict], index: int) -> dict:
        """Generate a synthetic document from field mappings."""
        doc: dict = {}
        for fm in field_mappings:
            field_name = fm.get("field_name", "")
            field_type = fm.get("field_type", "keyword")
            if not field_name or field_name == "@timestamp":
                continue

            doc[field_name] = self._generate_field_value(field_type, field_name, index)

        return doc

    def _generate_field_value(self, field_type: str, field_name: str, index: int):
        """Generate a typed field value for seeding."""
        match field_type:
            case "text":
                return f"Sample text content for {field_name} document {index}"
            case "keyword":
                return f"{field_name}_{index % 100}"
            case "integer" | "short" | "byte":
                return index % 10000
            case "long":
                return index * 1000
            case "float" | "double" | "half_float":
                return float(index) * 1.5
            case "boolean":
                return index % 2 == 0
            case "date":
                from datetime import UTC, datetime, timedelta

                base = datetime(2024, 1, 1, tzinfo=UTC)
                return (base + timedelta(hours=index)).isoformat()
            case "ip":
                return f"192.168.{(index // 256) % 256}.{index % 256}"
            case "geo_point":
                return {"lat": 40.0 + (index % 100) * 0.01, "lon": -74.0 + (index % 100) * 0.01}
            case "nested" | "object":
                return {"id": index, "value": f"nested_{index}"}
            case _:
                return f"{field_name}_{index}"
