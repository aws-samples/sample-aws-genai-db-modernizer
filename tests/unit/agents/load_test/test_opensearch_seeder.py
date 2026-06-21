"""Tests for OpenSearch seeder.

Mocks opensearchpy client and verifies:
  - Creates indices with correct mappings and settings
  - Seeds documents via bulk helper
  - Handles data streams (index template creation)
  - Generates typed field values
  - Refreshes indices after seeding
  - Raises when no endpoint provided
"""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.agents.load_test.opensearch.seeder import DEFAULT_DOCS_PER_INDEX, OpenSearchSeeder


@pytest.fixture
def schema_output_search() -> dict[str, Any]:
    """Schema output with a search workload index."""
    return {
        "_opensearch_endpoint": "search-test.us-east-1.es.amazonaws.com",
        "_opensearch_master_user": "loadtest_admin",
        "_opensearch_master_password": "TestPass123!",  # pragma: allowlist secret
        "index_designs": [
            {
                "index_name": "products",
                "source_tables": ["public.products"],
                "settings": {
                    "number_of_shards": 2,
                    "number_of_replicas": 0,
                    "refresh_interval": "1s",
                    "assumed_node_count": 2,
                    "custom_analyzers": [],
                },
                "field_mappings": [
                    {
                        "field_name": "title",
                        "field_type": "text",
                        "source_column": "title",
                        "analyzer": "standard",
                    },
                    {
                        "field_name": "category",
                        "field_type": "keyword",
                        "source_column": "category",
                    },
                    {
                        "field_name": "price",
                        "field_type": "float",
                        "source_column": "price",
                    },
                    {
                        "field_name": "in_stock",
                        "field_type": "boolean",
                        "source_column": "in_stock",
                    },
                ],
            }
        ],
        "data_stream_designs": [],
    }


@pytest.fixture
def schema_output_timeseries() -> dict[str, Any]:
    """Schema output with a data stream design."""
    return {
        "_opensearch_endpoint": "search-test.us-east-1.es.amazonaws.com",
        "_opensearch_master_user": "loadtest_admin",
        "_opensearch_master_password": "TestPass123!",  # pragma: allowlist secret
        "index_designs": [],
        "data_stream_designs": [
            {
                "data_stream_name": "application-logs",
                "source_tables": ["public.logs"],
                "timestamp_field": "created_at",
                "index_template": {
                    "template_name": "application-logs-template",
                    "index_patterns": ["application-logs-*"],
                    "settings": {
                        "number_of_shards": 1,
                        "number_of_replicas": 0,
                    },
                    "field_mappings": [
                        {
                            "field_name": "@timestamp",
                            "field_type": "date",
                            "source_column": "created_at",
                        },
                        {
                            "field_name": "level",
                            "field_type": "keyword",
                            "source_column": "log_level",
                        },
                        {
                            "field_name": "message",
                            "field_type": "text",
                            "source_column": "message",
                        },
                    ],
                },
                "ism_policy": {
                    "policy_name": "logs-lifecycle",
                    "hot_phase_days": 7,
                    "delete_after_days": 30,
                    "rollover_size_gb": 50,
                    "rollover_age_hours": 24,
                },
            }
        ],
    }


@pytest.fixture
def mock_os_client() -> MagicMock:
    """Mock opensearchpy client."""
    client = MagicMock()
    client.indices.exists.return_value = False
    client.indices.create.return_value = {"acknowledged": True}
    client.indices.put_index_template.return_value = {"acknowledged": True}
    client.indices.refresh.return_value = {}
    return client


class TestSeedSearchIndex:
    @patch("opensearchpy.helpers.bulk")
    @patch("opensearchpy.OpenSearch")
    def test_creates_index_and_seeds_documents(
        self,
        mock_os_class: MagicMock,
        mock_bulk: MagicMock,
        schema_output_search: dict[str, Any],
    ) -> None:
        mock_client = MagicMock()
        mock_client.indices.exists.return_value = False
        mock_os_class.return_value = mock_client
        mock_bulk.return_value = (1000, [])

        seeder = OpenSearchSeeder(region="us-east-1")
        manifest = seeder.seed(schema_output_search, max_items_per_table=10_000)

        mock_client.indices.create.assert_called_once()
        create_kwargs = mock_client.indices.create.call_args.kwargs
        assert create_kwargs["index"] == "products"

        mock_bulk.assert_called_once()
        assert manifest.total_items == DEFAULT_DOCS_PER_INDEX
        assert "products" in manifest.resources

    @patch("opensearchpy.helpers.bulk")
    @patch("opensearchpy.OpenSearch")
    def test_deletes_existing_index_before_create(
        self,
        mock_os_class: MagicMock,
        mock_bulk: MagicMock,
        schema_output_search: dict[str, Any],
    ) -> None:
        mock_client = MagicMock()
        mock_client.indices.exists.return_value = True
        mock_os_class.return_value = mock_client
        mock_bulk.return_value = (1000, [])

        seeder = OpenSearchSeeder(region="us-east-1")
        seeder.seed(schema_output_search)

        mock_client.indices.delete.assert_called_once_with(index="products")
        mock_client.indices.create.assert_called_once()

    @patch("opensearchpy.helpers.bulk")
    @patch("opensearchpy.OpenSearch")
    def test_refreshes_all_indices_after_seeding(
        self,
        mock_os_class: MagicMock,
        mock_bulk: MagicMock,
        schema_output_search: dict[str, Any],
    ) -> None:
        mock_client = MagicMock()
        mock_client.indices.exists.return_value = False
        mock_os_class.return_value = mock_client
        mock_bulk.return_value = (1000, [])

        seeder = OpenSearchSeeder(region="us-east-1")
        seeder.seed(schema_output_search)

        mock_client.indices.refresh.assert_called_once_with(index="_all")


class TestSeedDataStream:
    @patch("opensearchpy.helpers.bulk")
    @patch("opensearchpy.OpenSearch")
    def test_creates_index_template_and_seeds_data_stream(
        self,
        mock_os_class: MagicMock,
        mock_bulk: MagicMock,
        schema_output_timeseries: dict[str, Any],
    ) -> None:
        mock_client = MagicMock()
        mock_os_class.return_value = mock_client
        mock_bulk.return_value = (1000, [])

        seeder = OpenSearchSeeder(region="us-east-1")
        manifest = seeder.seed(schema_output_timeseries)

        mock_client.indices.put_index_template.assert_called_once()
        template_kwargs = mock_client.indices.put_index_template.call_args.kwargs
        assert template_kwargs["name"] == "application-logs-template"

        mock_bulk.assert_called_once()
        assert "application-logs" in manifest.resources


class TestNoEndpoint:
    @patch("opensearchpy.OpenSearch")
    def test_raises_when_no_endpoint(self, mock_os_class: MagicMock) -> None:
        schema: dict[str, Any] = {"index_designs": [], "data_stream_designs": []}
        seeder = OpenSearchSeeder(region="us-east-1")
        with pytest.raises(ValueError, match="_opensearch_endpoint"):
            seeder.seed(schema)


class TestGenerateFieldValue:
    def test_text_field(self) -> None:
        seeder = OpenSearchSeeder()
        value = seeder._generate_field_value("text", "description", 5)
        assert isinstance(value, str)
        assert "description" in value

    def test_keyword_field(self) -> None:
        seeder = OpenSearchSeeder()
        value = seeder._generate_field_value("keyword", "status", 5)
        assert isinstance(value, str)

    def test_integer_field(self) -> None:
        seeder = OpenSearchSeeder()
        value = seeder._generate_field_value("integer", "count", 5)
        assert isinstance(value, int)

    def test_float_field(self) -> None:
        seeder = OpenSearchSeeder()
        value = seeder._generate_field_value("float", "price", 5)
        assert isinstance(value, float)

    def test_boolean_field(self) -> None:
        seeder = OpenSearchSeeder()
        value = seeder._generate_field_value("boolean", "active", 5)
        assert isinstance(value, bool)

    def test_geo_point_field(self) -> None:
        seeder = OpenSearchSeeder()
        value = seeder._generate_field_value("geo_point", "location", 5)
        assert "lat" in value
        assert "lon" in value

    def test_nested_field(self) -> None:
        seeder = OpenSearchSeeder()
        value = seeder._generate_field_value("nested", "tags", 5)
        assert isinstance(value, dict)
        assert "id" in value


class TestBuildMappingProperties:
    def test_builds_properties_from_field_mappings(self) -> None:
        seeder = OpenSearchSeeder()
        field_mappings: list[dict[str, Any]] = [
            {"field_name": "title", "field_type": "text", "analyzer": "standard"},
            {"field_name": "status", "field_type": "keyword"},
            {
                "field_name": "description",
                "field_type": "text",
                "multi_field": True,
            },
        ]
        props = seeder._build_mapping_properties(field_mappings)

        assert props["title"]["type"] == "text"
        assert props["title"]["analyzer"] == "standard"
        assert props["status"]["type"] == "keyword"
        assert "fields" in props["description"]
        assert props["description"]["fields"]["keyword"]["type"] == "keyword"

    def test_skips_empty_field_names(self) -> None:
        seeder = OpenSearchSeeder()
        field_mappings = [
            {"field_name": "", "field_type": "text"},
            {"field_name": "title", "field_type": "text"},
        ]
        props = seeder._build_mapping_properties(field_mappings)
        assert "" not in props
        assert "title" in props
