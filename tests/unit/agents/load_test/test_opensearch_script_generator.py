"""Tests for OpenSearch k6 script generator.

Verifies:
  - Scenario scripts are generated for each access pattern
  - Main.js orchestrates all scenarios with constant-arrival-rate
  - generate_all creates temp directory with correct files
  - HTTP requests use correct paths for each operation type
  - DSL is properly escaped in generated JavaScript
"""

from typing import Any

import pytest

from src.agents.load_test.models import SeedManifest
from src.agents.load_test.opensearch.script_generator import OpenSearchScriptGenerator
from src.contracts.load_test_models import TestConfig


@pytest.fixture
def access_patterns() -> list[dict[str, Any]]:
    return [
        {
            "pattern_id": "OS-AP-1",
            "operation": "search",
            "index_or_stream": "products",
            "opensearch_dsl": '{"query": {"match": {"title": "laptop"}}}',
            "design_rps": 100,
            "query_ids": ["q1"],
        },
        {
            "pattern_id": "OS-AP-2",
            "operation": "aggregate",
            "index_or_stream": "products",
            "opensearch_dsl": '{"aggs": {"avg_price": {"avg": {"field": "price"}}}}',
            "design_rps": 50,
            "query_ids": ["q2"],
        },
        {
            "pattern_id": "OS-AP-3",
            "operation": "get_by_id",
            "index_or_stream": "products",
            "opensearch_dsl": "{}",
            "design_rps": 200,
            "query_ids": ["q3"],
        },
    ]


@pytest.fixture
def schema_output() -> dict[str, Any]:
    return {
        "index_designs": [
            {
                "index_name": "products",
                "settings": {"number_of_shards": 2},
            }
        ],
    }


@pytest.fixture
def seed_manifest() -> SeedManifest:
    return SeedManifest(
        resources={"products": {"docs_seeded": 5000}},
        total_items=5000,
        duration_seconds=2.5,
    )


@pytest.fixture
def test_config() -> TestConfig:
    return TestConfig(duration_minutes=5, warmup_seconds=10)


class TestGenerateScenario:
    def test_generates_search_scenario(self, access_patterns: list[dict]) -> None:
        gen = OpenSearchScriptGenerator(region="us-east-1")
        js = gen.generate_scenario(access_patterns[0], {}, {"docs_seeded": 5000})

        assert "scenario" in js or "OS_AP_1" in js
        assert "http.post" in js
        assert "_search" in js
        assert "products" in js

    def test_generates_aggregate_scenario(self, access_patterns: list[dict]) -> None:
        gen = OpenSearchScriptGenerator(region="us-east-1")
        js = gen.generate_scenario(access_patterns[1], {}, {"docs_seeded": 5000})

        assert "http.post" in js
        assert "_search?size=0" in js

    def test_generates_get_by_id_scenario(self, access_patterns: list[dict]) -> None:
        gen = OpenSearchScriptGenerator(region="us-east-1")
        js = gen.generate_scenario(access_patterns[2], {}, {"docs_seeded": 5000})

        assert "http.get" in js
        assert "_doc/" in js


class TestGenerateMain:
    def test_main_imports_all_scenarios(self, access_patterns: list[dict]) -> None:
        gen = OpenSearchScriptGenerator(region="us-east-1")
        main = gen.generate_main(access_patterns, duration_minutes=5, warmup_seconds=10)

        assert "import { scenario_0 }" in main
        assert "import { scenario_1 }" in main
        assert "import { scenario_2 }" in main

    def test_main_has_constant_arrival_rate(self, access_patterns: list[dict]) -> None:
        gen = OpenSearchScriptGenerator(region="us-east-1")
        main = gen.generate_main(access_patterns, duration_minutes=5, warmup_seconds=10)

        assert "constant-arrival-rate" in main
        assert '"5m"' in main
        assert '"10s"' in main

    def test_main_has_thresholds(self, access_patterns: list[dict]) -> None:
        gen = OpenSearchScriptGenerator(region="us-east-1")
        main = gen.generate_main(access_patterns, duration_minutes=5, warmup_seconds=10)

        assert "http_req_duration" in main
        assert "handleSummary" in main

    def test_main_uses_rps_from_patterns(self, access_patterns: list[dict]) -> None:
        gen = OpenSearchScriptGenerator(region="us-east-1")
        main = gen.generate_main(access_patterns, duration_minutes=5, warmup_seconds=10)

        assert "rate: 100" in main
        assert "rate: 50" in main
        assert "rate: 200" in main


class TestGenerateAll:
    def test_creates_files_in_temp_directory(
        self,
        access_patterns: list[dict],
        schema_output: dict,
        seed_manifest: SeedManifest,
        test_config: TestConfig,
    ) -> None:
        from pathlib import Path

        gen = OpenSearchScriptGenerator(region="us-east-1")
        scripts_dir = gen.generate_all(access_patterns, schema_output, seed_manifest, test_config)

        scripts_path = Path(scripts_dir)
        assert scripts_path.exists()
        assert (scripts_path / "main.js").exists()
        assert (scripts_path / "scenario_0.js").exists()
        assert (scripts_path / "scenario_1.js").exists()
        assert (scripts_path / "scenario_2.js").exists()

    def test_scenario_files_contain_correct_operations(
        self,
        access_patterns: list[dict],
        schema_output: dict,
        seed_manifest: SeedManifest,
        test_config: TestConfig,
    ) -> None:
        from pathlib import Path

        gen = OpenSearchScriptGenerator(region="us-east-1")
        scripts_dir = gen.generate_all(access_patterns, schema_output, seed_manifest, test_config)

        scripts_path = Path(scripts_dir)
        s0 = (scripts_path / "scenario_0.js").read_text()
        assert "_search" in s0
        assert "products" in s0

        s2 = (scripts_path / "scenario_2.js").read_text()
        assert "_doc/" in s2

    def test_scenario_metrics_keyed_by_query_id(
        self,
        access_patterns: list[dict],
        schema_output: dict,
        seed_manifest: SeedManifest,
        test_config: TestConfig,
    ) -> None:
        """Custom metrics must be named by query_id so the engine-agnostic
        handler (which looks up latency_/requests_/errors_{query_id}) can find
        per-pattern results. The exported function stays ordinal-based because
        main.js imports it as scenario_{i}."""
        from pathlib import Path

        gen = OpenSearchScriptGenerator(region="us-east-1")
        scripts_dir = gen.generate_all(access_patterns, schema_output, seed_manifest, test_config)

        s0 = (Path(scripts_dir) / "scenario_0.js").read_text()
        assert 'new Trend("latency_q1"' in s0
        assert 'new Counter("requests_q1"' in s0
        assert 'new Counter("errors_q1"' in s0
        # Function/export name remains ordinal so main.js imports resolve.
        assert "export function scenario_0()" in s0


class TestPrepareDslForK6:
    def test_valid_json_is_passed_through(self) -> None:
        gen = OpenSearchScriptGenerator()
        result = gen._prepare_dsl_for_k6('{"query": {"match_all": {}}}', 1000)
        assert '"query"' in result
        assert '"match_all"' in result

    def test_invalid_json_wrapped_in_json_parse(self) -> None:
        gen = OpenSearchScriptGenerator()
        result = gen._prepare_dsl_for_k6("not json {", 1000)
        assert "JSON.parse" in result

    def test_empty_string_returns_match_all(self) -> None:
        gen = OpenSearchScriptGenerator()
        result = gen._prepare_dsl_for_k6("", 1000)
        assert "match_all" in result


class TestGenerateRequestCode:
    def test_search_uses_post_to_search_endpoint(self) -> None:
        gen = OpenSearchScriptGenerator()
        code = gen._generate_request_code("search", "products", '{"query":{}}')
        assert "/_search" in code
        assert "http.post" in code

    def test_aggregate_uses_size_zero(self) -> None:
        gen = OpenSearchScriptGenerator()
        code = gen._generate_request_code("aggregate", "products", '{"aggs":{}}')
        assert "_search?size=0" in code

    def test_get_by_id_uses_get(self) -> None:
        gen = OpenSearchScriptGenerator()
        code = gen._generate_request_code("get_by_id", "products", "{}")
        assert "http.get" in code
        assert "_doc/" in code

    def test_bulk_index_uses_post_to_doc(self) -> None:
        gen = OpenSearchScriptGenerator()
        code = gen._generate_request_code("bulk_index", "products", '{"title":"test"}')
        assert "/_doc" in code
        assert "http.post" in code

    def test_unknown_operation_defaults_to_search(self) -> None:
        gen = OpenSearchScriptGenerator()
        code = gen._generate_request_code("unknown_op", "products", '{"query":{}}')
        assert "/_search" in code
