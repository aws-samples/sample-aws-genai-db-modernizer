"""Tests for DocumentDB script generator.

Renders real Jinja2 templates (no mocking of templates) and verifies:
  - Each operation type renders correctly with substituted context
  - Unsupported operation types raise ValueError
  - Helpers are copied verbatim to scripts_dir/helpers/
  - main.js has scenarios for each access pattern with correct rate/duration
  - VU scaling caps total at MAX_TOTAL_VUS
  - Patterns missing source_table / collection_def / seed_info are skipped
  - Duplicate query_ids are deduplicated
  - safe_id is JS-identifier-safe and unique even with same query_id prefix
  - Generated JS imports the expected helpers + uses the dispatcher API
"""

from pathlib import Path
from typing import Any

import pytest

from src.agents.load_test.documentdb.script_generator import (
    HELPER_FILES,
    SUPPORTED_OPERATIONS,
    DocumentDBScriptGenerator,
)
from src.agents.load_test.models import SeedManifest
from src.contracts.load_test_models import TestConfig

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def generator() -> DocumentDBScriptGenerator:
    return DocumentDBScriptGenerator(region="us-east-1")


@pytest.fixture
def collection_def() -> dict[str, Any]:
    return {
        "source_tables": ["users"],
        "embedded_entities": [],
        "indexes": [{"keys": {"primary_id": 1}}],
    }


@pytest.fixture
def seed_info() -> dict[str, Any]:
    return {
        "collection_name": "LoadTest_users",
        "document_count": 5_000,
        "primary_key_field": "primary_id",
        "primary_key_pattern": "doc-{index:06d}",
        "primary_key_count": 5_000,
        "embedded_entity_paths": [],
    }


@pytest.fixture
def schema_output(collection_def: dict[str, Any]) -> dict[str, Any]:
    return {"collections": [collection_def]}


@pytest.fixture
def seed_manifest(seed_info: dict[str, Any]) -> SeedManifest:
    return SeedManifest(
        resources={"users": seed_info},
        total_items=seed_info["document_count"],
        duration_seconds=1.5,
    )


@pytest.fixture
def test_config() -> TestConfig:
    return TestConfig(duration_minutes=15, warmup_seconds=30)


def _make_pattern(
    pattern_id: str,
    operation: str,
    source_table: str = "users",
    design_rps: int = 5,
    description: str = "",
) -> dict[str, Any]:
    return {
        "pattern_id": pattern_id,
        "operation": operation,
        "source_table": source_table,
        "design_rps": design_rps,
        "description": description,
    }


# =============================================================================
# Single scenario rendering
# =============================================================================


class TestGenerateScenario:
    def test_findOne_renders_with_collection_and_key_count(
        self,
        generator: DocumentDBScriptGenerator,
        collection_def: dict[str, Any],
        seed_info: dict[str, Any],
    ) -> None:
        ap = _make_pattern("p001", "findOne", description="Get user by id")
        rendered = generator.generate_scenario(ap, collection_def, seed_info)

        assert "findOne" in rendered
        assert "LoadTest_users" in rendered
        assert "p001" in rendered
        assert "5000" in rendered  # primary_key_count
        assert "Get user by id" in rendered
        # Imports the helpers
        assert "../helpers/client.js" in rendered
        assert "../helpers/key-generator.js" in rendered
        assert "../helpers/metrics.js" in rendered

    def test_aggregate_uses_only_supported_operators(
        self,
        generator: DocumentDBScriptGenerator,
        collection_def: dict[str, Any],
        seed_info: dict[str, Any],
    ) -> None:
        ap = _make_pattern("p001", "aggregate")
        rendered = generator.generate_scenario(ap, collection_def, seed_info)

        # Must use supported operators
        assert "$match" in rendered
        assert "$group" in rendered
        # Must NOT use unsupported operators (per claude.md §45)
        assert "$graphLookup" not in rendered
        assert "$facet" not in rendered
        assert "$setWindowFields" not in rendered

    def test_each_supported_operation_has_template(
        self,
        generator: DocumentDBScriptGenerator,
        collection_def: dict[str, Any],
        seed_info: dict[str, Any],
    ) -> None:
        """Every operation in SUPPORTED_OPERATIONS must have a template."""
        for op in SUPPORTED_OPERATIONS:
            ap = _make_pattern(f"p_{op}", op)
            rendered = generator.generate_scenario(ap, collection_def, seed_info)
            assert "LoadTest_users" in rendered, f"template for {op} missing collection"
            assert "createPatternMetrics" in rendered, f"template for {op} missing metrics"

    def test_unsupported_operation_raises(
        self,
        generator: DocumentDBScriptGenerator,
        collection_def: dict[str, Any],
        seed_info: dict[str, Any],
    ) -> None:
        ap = _make_pattern("p001", "doSomethingWeird")
        with pytest.raises(ValueError, match="doSomethingWeird"):
            generator.generate_scenario(ap, collection_def, seed_info)


# =============================================================================
# main.js generation
# =============================================================================


class TestGenerateMain:
    def test_main_imports_each_scenario(self, generator: DocumentDBScriptGenerator) -> None:
        scenarios = [
            {"query_id": "p001", "design_rps": 5},
            {"query_id": "p002", "design_rps": 10},
        ]
        rendered = generator.generate_main(scenarios, duration_minutes=15, warmup_seconds=30)

        assert "scenarios/p001.js" in rendered
        assert "scenarios/p002.js" in rendered
        assert "executor: 'constant-arrival-rate'" in rendered
        assert "duration: '15m'" in rendered
        assert "startTime: '30s'" in rendered

    def test_main_includes_handle_summary(self, generator: DocumentDBScriptGenerator) -> None:
        rendered = generator.generate_main(
            [{"query_id": "p001", "design_rps": 1}], duration_minutes=15, warmup_seconds=30
        )
        assert "handleSummary" in rendered
        assert "K6_SUMMARY_PATH" in rendered

    def test_safe_ids_are_unique_with_same_prefix(
        self, generator: DocumentDBScriptGenerator
    ) -> None:
        # query_ids that share an 8-char prefix would collide without the index suffix
        scenarios = [
            {"query_id": "abcdefghi_1", "design_rps": 1},
            {"query_id": "abcdefghi_2", "design_rps": 1},
            {"query_id": "abcdefghi_3", "design_rps": 1},
        ]
        rendered = generator.generate_main(scenarios, duration_minutes=15, warmup_seconds=30)
        # Each scenario should have a distinct safe_id thanks to the index suffix
        assert "qabcdefgh_0" in rendered
        assert "qabcdefgh_1" in rendered
        assert "qabcdefgh_2" in rendered

    def test_vu_cap_scales_down_when_total_exceeds_max(
        self, generator: DocumentDBScriptGenerator
    ) -> None:
        # 100 scenarios at 1000 rps each = 100k raw VUs; should scale to <=10k
        scenarios = [{"query_id": f"p{i:03d}", "design_rps": 1000} for i in range(100)]
        rendered = generator.generate_main(scenarios, duration_minutes=15, warmup_seconds=30)

        # Extract maxVUs values and sum
        import re

        max_vus_values = [int(m) for m in re.findall(r"maxVUs:\s*(\d+)", rendered)]
        assert sum(max_vus_values) <= 10_000


# =============================================================================
# generate_all integration — full output verification
# =============================================================================


class TestGenerateAll:
    def test_helpers_are_copied_verbatim(
        self,
        generator: DocumentDBScriptGenerator,
        schema_output: dict[str, Any],
        seed_manifest: SeedManifest,
        test_config: TestConfig,
    ) -> None:
        access_patterns = [_make_pattern("p001", "findOne")]
        scripts_dir = Path(
            generator.generate_all(access_patterns, schema_output, seed_manifest, test_config)
        )

        helpers_dir = scripts_dir / "helpers"
        for helper in HELPER_FILES:
            assert (helpers_dir / helper).exists(), f"missing helper: {helper}"
            content = (helpers_dir / helper).read_text()
            assert len(content) > 0

    def test_scenario_files_created_per_pattern(
        self,
        generator: DocumentDBScriptGenerator,
        schema_output: dict[str, Any],
        seed_manifest: SeedManifest,
        test_config: TestConfig,
    ) -> None:
        access_patterns = [
            _make_pattern("p_find_one", "findOne"),
            _make_pattern("p_find_many", "find"),
            _make_pattern("p_insert", "insertOne"),
        ]
        scripts_dir = Path(
            generator.generate_all(access_patterns, schema_output, seed_manifest, test_config)
        )

        scenarios_dir = scripts_dir / "scenarios"
        assert (scenarios_dir / "p_find_one.js").exists()
        assert (scenarios_dir / "p_find_many.js").exists()
        assert (scenarios_dir / "p_insert.js").exists()

    def test_main_js_at_root(
        self,
        generator: DocumentDBScriptGenerator,
        schema_output: dict[str, Any],
        seed_manifest: SeedManifest,
        test_config: TestConfig,
    ) -> None:
        access_patterns = [_make_pattern("p001", "findOne")]
        scripts_dir = Path(
            generator.generate_all(access_patterns, schema_output, seed_manifest, test_config)
        )
        assert (scripts_dir / "main.js").exists()

    def test_pattern_with_no_source_table_skipped(
        self,
        generator: DocumentDBScriptGenerator,
        schema_output: dict[str, Any],
        seed_manifest: SeedManifest,
        test_config: TestConfig,
    ) -> None:
        access_patterns = [
            _make_pattern("p_valid", "findOne", source_table="users"),
            {"pattern_id": "p_no_source", "operation": "findOne", "design_rps": 1},
        ]
        scripts_dir = Path(
            generator.generate_all(access_patterns, schema_output, seed_manifest, test_config)
        )
        scenarios_dir = scripts_dir / "scenarios"
        assert (scenarios_dir / "p_valid.js").exists()
        assert not (scenarios_dir / "p_no_source.js").exists()

    def test_pattern_with_unknown_collection_skipped(
        self,
        generator: DocumentDBScriptGenerator,
        schema_output: dict[str, Any],
        seed_manifest: SeedManifest,
        test_config: TestConfig,
    ) -> None:
        access_patterns = [
            _make_pattern("p_valid", "findOne", source_table="users"),
            _make_pattern("p_unknown", "findOne", source_table="phantom_table"),
        ]
        scripts_dir = Path(
            generator.generate_all(access_patterns, schema_output, seed_manifest, test_config)
        )
        scenarios_dir = scripts_dir / "scenarios"
        assert (scenarios_dir / "p_valid.js").exists()
        assert not (scenarios_dir / "p_unknown.js").exists()

    def test_pattern_with_no_seed_info_skipped(
        self,
        generator: DocumentDBScriptGenerator,
        schema_output: dict[str, Any],
        test_config: TestConfig,
    ) -> None:
        # SeedManifest with no resources — every pattern should be skipped
        empty_manifest = SeedManifest(resources={}, total_items=0, duration_seconds=0.1)
        access_patterns = [_make_pattern("p001", "findOne")]
        scripts_dir = Path(
            generator.generate_all(access_patterns, schema_output, empty_manifest, test_config)
        )
        scenarios_dir = scripts_dir / "scenarios"
        # No scenario files should exist (directory may not exist either)
        if scenarios_dir.exists():
            assert not any(scenarios_dir.iterdir())

    def test_duplicate_query_ids_deduplicated(
        self,
        generator: DocumentDBScriptGenerator,
        schema_output: dict[str, Any],
        seed_manifest: SeedManifest,
        test_config: TestConfig,
    ) -> None:
        # Two access patterns with the same pattern_id — only one scenario file
        access_patterns = [
            _make_pattern("p_dup", "findOne"),
            _make_pattern("p_dup", "find"),  # different op, same id
        ]
        scripts_dir = Path(
            generator.generate_all(access_patterns, schema_output, seed_manifest, test_config)
        )
        scenarios = list((scripts_dir / "scenarios").iterdir())
        # Only one file because the second was deduped
        assert len([f for f in scenarios if f.suffix == ".js"]) == 1

    def test_unsupported_operation_logs_and_continues(
        self,
        generator: DocumentDBScriptGenerator,
        schema_output: dict[str, Any],
        seed_manifest: SeedManifest,
        test_config: TestConfig,
    ) -> None:
        access_patterns = [
            _make_pattern("p_good", "findOne"),
            _make_pattern("p_bad", "weirdOp"),
        ]
        scripts_dir = Path(
            generator.generate_all(access_patterns, schema_output, seed_manifest, test_config)
        )
        scenarios_dir = scripts_dir / "scenarios"
        assert (scenarios_dir / "p_good.js").exists()
        assert not (scenarios_dir / "p_bad.js").exists()


# =============================================================================
# Generated content quality
# =============================================================================


class TestGeneratedContent:
    def test_no_credentials_in_any_generated_file(
        self,
        generator: DocumentDBScriptGenerator,
        schema_output: dict[str, Any],
        seed_manifest: SeedManifest,
        test_config: TestConfig,
    ) -> None:
        """Generated scripts must not contain any credentials inline."""
        access_patterns = [_make_pattern("p001", "findOne")]
        scripts_dir = Path(
            generator.generate_all(access_patterns, schema_output, seed_manifest, test_config)
        )
        for js_file in scripts_dir.rglob("*.js"):
            content = js_file.read_text()
            # No raw access keys, secrets, or passwords
            assert "AKIA" not in content, f"AWS key prefix found in {js_file}"
            assert "password=" not in content.lower(), f"hardcoded password in {js_file}"
            # MONGODB-AWS auth means no user:pass@ in URI
            for line in content.splitlines():
                if "mongodb://" in line:
                    # URI before query string should not contain credentials
                    uri_root = line.split("mongodb://")[1].split("?")[0]
                    assert "@" not in uri_root, f"creds in URI: {js_file}"

    def test_scenario_uses_dispatcher_api(
        self,
        generator: DocumentDBScriptGenerator,
        schema_output: dict[str, Any],
        seed_manifest: SeedManifest,
        test_config: TestConfig,
    ) -> None:
        """Scenarios go through runOperation() — keeps xk6-mongo coupling in one file."""
        access_patterns = [_make_pattern("p001", "find")]
        scripts_dir = Path(
            generator.generate_all(access_patterns, schema_output, seed_manifest, test_config)
        )
        content = (scripts_dir / "scenarios" / "p001.js").read_text()
        assert "runOperation" in content
        assert "metrics.latency" in content
        assert "metrics.requests" in content
        assert "metrics.errors" in content
