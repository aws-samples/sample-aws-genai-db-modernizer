"""Tests for DynamoDB script generator with per-operation templates."""

import pytest

from src.agents.load_test.dynamodb.script_generator import DynamoDBScriptGenerator


@pytest.fixture
def generator():
    return DynamoDBScriptGenerator(region="us-east-1")


@pytest.fixture
def seed_info_numeric_pk():
    return {
        "table_name": "LoadTest_WpPosts",
        "pk_attr": "ID",
        "pk_type": "N",
        "pk_count": 1000,
        "pk_pad_width": None,
        "sk_attr": None,
        "sk_type": None,
        "sk_count": None,
        "sk_pad_width": None,
        "items_seeded": 1000,
    }


@pytest.fixture
def seed_info_with_sk():
    return {
        "table_name": "LoadTest_WpPostMeta",
        "pk_attr": "post_id",
        "pk_type": "N",
        "pk_count": 100,
        "pk_pad_width": None,
        "sk_attr": "meta_key_id",
        "sk_type": "S",
        "sk_count": 10,
        "sk_pad_width": 4,
        "items_seeded": 1000,
    }


class TestDynamoDBScriptGenerator:
    def test_generate_getitem_numeric_pk(self, generator, seed_info_numeric_pk):
        access_pattern = {
            "pattern_id": "AP-1",
            "query_ids": ["q1"],
            "operation": "GetItem",
            "table_name": "WpPosts",
            "key_condition": "PK=ID",
            "design_rps": 100,
        }
        table_def = {
            "table_name": "WpPosts",
            "partition_key": {"attribute_name": "ID", "attribute_type": "N"},
            "sort_key": None,
        }
        script = generator.generate_scenario(access_pattern, table_def, seed_info_numeric_pk)

        assert "LoadTest_WpPosts" in script
        assert "GetItem" in script
        assert "ID" in script
        assert "createPatternMetrics('q1')" in script

    def test_generate_query_with_begins_with(self, generator, seed_info_with_sk):
        access_pattern = {
            "pattern_id": "AP-5",
            "query_ids": ["q5"],
            "operation": "Query",
            "table_name": "WpPostMeta",
            "key_condition": "PK=post_id AND SK begins_with 'meta_key#'",
            "design_rps": 200,
        }
        table_def = {
            "table_name": "WpPostMeta",
            "partition_key": {"attribute_name": "post_id", "attribute_type": "N"},
            "sort_key": {"attribute_name": "meta_key_id", "attribute_type": "S"},
        }
        script = generator.generate_scenario(access_pattern, table_def, seed_info_with_sk)

        assert "LoadTest_WpPostMeta" in script
        assert "Query" in script
        assert "begins_with" in script
        assert "post_id" in script
        assert "meta_key#" in script

    def test_generate_putitem(self, generator, seed_info_numeric_pk):
        access_pattern = {
            "pattern_id": "AP-10",
            "query_ids": ["q10"],
            "operation": "PutItem",
            "table_name": "WpPosts",
            "key_condition": "PK=ID",
            "design_rps": 50,
        }
        table_def = {
            "table_name": "WpPosts",
            "partition_key": {"attribute_name": "ID", "attribute_type": "N"},
            "sort_key": None,
        }
        script = generator.generate_scenario(access_pattern, table_def, seed_info_numeric_pk)

        assert "PutItem" in script
        assert "LoadTest_WpPosts" in script
        assert "createPatternMetrics('q10')" in script

    def test_generate_main_script(self, generator):
        scenarios = [
            {"query_id": "q1", "script_path": "scenarios/q1.js", "design_rps": 100},
            {"query_id": "q5", "script_path": "scenarios/q5.js", "design_rps": 200},
        ]
        main_js = generator.generate_main(scenarios, duration_minutes=5, warmup_seconds=30)

        assert "constant-arrival-rate" in main_js
        assert "q1" in main_js
        assert "q5" in main_js
        assert "handleSummary" in main_js

    def test_generate_main_sanitizes_negative_query_ids(self, generator):
        """Query IDs with hyphens (e.g. source ids like -7551067248247426933) must
        not leak into JS identifiers — a hyphen produces 'Unexpected token -'."""
        import re

        scenarios = [
            {
                "query_id": "-7551067248247426933",
                "script_path": "scenarios/-7551067248247426933.js",
                "design_rps": 50,
            },
        ]
        main_js = generator.generate_main(scenarios, duration_minutes=5, warmup_seconds=30)

        # Every generated JS identifier token must be a valid identifier (no hyphen).
        for token in re.findall(r"\b(?:scenario_|run_|s_)[A-Za-z0-9_]*-?[A-Za-z0-9_]*", main_js):
            assert "-" not in token, f"invalid JS identifier generated: {token!r}"

    def test_generate_updateitem(self, generator, seed_info_numeric_pk):
        access_pattern = {
            "pattern_id": "AP-11",
            "query_ids": ["q11"],
            "operation": "UpdateItem",
            "table_name": "WpPosts",
            "key_condition": "PK=ID",
            "design_rps": 20,
        }
        table_def = {
            "table_name": "WpPosts",
            "partition_key": {"attribute_name": "ID", "attribute_type": "N"},
            "sort_key": None,
        }
        script = generator.generate_scenario(access_pattern, table_def, seed_info_numeric_pk)

        assert "UpdateItem" in script
        assert "LoadTest_WpPosts" in script
        assert "createPatternMetrics('q11')" in script

    def test_generate_deleteitem(self, generator, seed_info_numeric_pk):
        access_pattern = {
            "pattern_id": "AP-12",
            "query_ids": ["q12"],
            "operation": "DeleteItem",
            "table_name": "WpPosts",
            "key_condition": "PK=ID",
            "design_rps": 5,
        }
        table_def = {
            "table_name": "WpPosts",
            "partition_key": {"attribute_name": "ID", "attribute_type": "N"},
            "sort_key": None,
        }
        script = generator.generate_scenario(access_pattern, table_def, seed_info_numeric_pk)

        assert "DeleteItem" in script
        assert "LoadTest_WpPosts" in script
        assert "createPatternMetrics('q12')" in script

    def test_generate_batchgetitem(self, generator, seed_info_numeric_pk):
        access_pattern = {
            "pattern_id": "AP-13",
            "query_ids": ["q13"],
            "operation": "BatchGetItem",
            "table_name": "WpPosts",
            "key_condition": "PK=ID",
            "design_rps": 10,
        }
        table_def = {
            "table_name": "WpPosts",
            "partition_key": {"attribute_name": "ID", "attribute_type": "N"},
            "sort_key": None,
        }
        script = generator.generate_scenario(access_pattern, table_def, seed_info_numeric_pk)

        assert "BatchGetItem" in script
        assert "LoadTest_WpPosts" in script
        assert "createPatternMetrics('q13')" in script
        assert "RequestItems" in script

    def test_generate_batchwriteitem(self, generator, seed_info_numeric_pk):
        access_pattern = {
            "pattern_id": "AP-14",
            "query_ids": ["q14"],
            "operation": "BatchWriteItem",
            "table_name": "WpPosts",
            "key_condition": "PK=ID",
            "design_rps": 5,
        }
        table_def = {
            "table_name": "WpPosts",
            "partition_key": {"attribute_name": "ID", "attribute_type": "N"},
            "sort_key": None,
        }
        script = generator.generate_scenario(access_pattern, table_def, seed_info_numeric_pk)

        assert "BatchWriteItem" in script
        assert "LoadTest_WpPosts" in script
        assert "createPatternMetrics('q14')" in script
        assert "PutRequest" in script

    def test_generate_query_pk_only(self, generator, seed_info_numeric_pk):
        """Query with no SK condition (PK-only query)."""
        access_pattern = {
            "pattern_id": "AP-20",
            "query_ids": ["q20"],
            "operation": "Query",
            "table_name": "WpPosts",
            "key_condition": "PK=ID",
            "design_rps": 50,
        }
        table_def = {
            "table_name": "WpPosts",
            "partition_key": {"attribute_name": "ID", "attribute_type": "N"},
            "sort_key": None,
        }
        script = generator.generate_scenario(access_pattern, table_def, seed_info_numeric_pk)

        assert "Query" in script
        assert "KeyConditionExpression" in script
        assert "createPatternMetrics('q20')" in script

    def test_generate_query_with_gsi(self, generator, seed_info_with_sk):
        """Query using a GSI."""
        access_pattern = {
            "pattern_id": "AP-21",
            "query_ids": ["q21"],
            "operation": "Query",
            "table_name": "WpPostMeta",
            "key_condition": "GSI1PK=author_id AND GSI1SK begins_with 'POST#'",
            "gsi_name": "GSI1",
            "design_rps": 30,
        }
        table_def = {
            "table_name": "WpPostMeta",
            "partition_key": {"attribute_name": "post_id", "attribute_type": "N"},
            "sort_key": {"attribute_name": "meta_key_id", "attribute_type": "S"},
        }
        script = generator.generate_scenario(access_pattern, table_def, seed_info_with_sk)

        assert "Query" in script
        assert "GSI1" in script
        assert "IndexName" in script

    def test_main_script_vus_calculation(self, generator):
        """pre_allocated_vus and max_vus are calculated from design_rps."""
        scenarios = [
            {"query_id": "q1", "script_path": "scenarios/q1.js", "design_rps": 100},
        ]
        main_js = generator.generate_main(scenarios, duration_minutes=5, warmup_seconds=30)

        # max_vus = ceil(100 * 2 * scale) = 200 (no cap for single scenario)
        # pre_allocated_vus = ceil(200 * 0.1) = 20
        assert "preAllocatedVUs: 20" in main_js
        assert "maxVUs: 200" in main_js

    def test_generate_all_produces_directory(self, generator):
        """generate_all writes all files to a temp dir and returns its path."""
        from pathlib import Path

        from src.agents.load_test.models import SeedManifest
        from src.contracts.load_test_models import TestConfig

        access_patterns = [
            {
                "pattern_id": "AP-1",
                "query_ids": ["q1"],
                "operation": "GetItem",
                "table_name": "WpPosts",
                "key_condition": "PK=ID",
                "design_rps": 10,
            }
        ]
        schema_output = {
            "table_definitions": [
                {
                    "table_name": "WpPosts",
                    "partition_key": {"attribute_name": "ID", "attribute_type": "N"},
                    "sort_key": None,
                }
            ]
        }
        seed_manifest = SeedManifest(
            resources={
                "WpPosts": {
                    "table_name": "LoadTest_WpPosts",
                    "pk_attr": "ID",
                    "pk_type": "N",
                    "pk_count": 1000,
                    "pk_pad_width": None,
                    "sk_attr": None,
                    "sk_type": None,
                    "sk_count": None,
                    "sk_pad_width": None,
                    "items_seeded": 1000,
                }
            },
            total_items=1000,
            duration_seconds=5.0,
        )
        test_config = TestConfig()

        scripts_dir = generator.generate_all(
            access_patterns, schema_output, seed_manifest, test_config
        )

        p = Path(scripts_dir)
        assert p.exists()
        assert (p / "main.js").exists()
        assert (p / "scenarios" / "q1.js").exists()
        assert (p / "helpers" / "aws-client.js").exists()
        assert (p / "helpers" / "metrics-collector.js").exists()

    def test_generate_all_deduplicates_query_ids(self, generator):
        """Same query_id in two access patterns is only generated once."""
        from pathlib import Path

        from src.agents.load_test.models import SeedManifest
        from src.contracts.load_test_models import TestConfig

        access_patterns = [
            {
                "query_ids": ["q1"],
                "operation": "GetItem",
                "table_name": "WpPosts",
                "key_condition": "PK=ID",
                "design_rps": 10,
            },
            {
                "query_ids": ["q1"],  # duplicate
                "operation": "GetItem",
                "table_name": "WpPosts",
                "key_condition": "PK=ID",
                "design_rps": 20,
            },
        ]
        schema_output = {
            "table_definitions": [
                {
                    "table_name": "WpPosts",
                    "partition_key": {"attribute_name": "ID", "attribute_type": "N"},
                    "sort_key": None,
                }
            ]
        }
        seed_manifest = SeedManifest(
            resources={
                "WpPosts": {
                    "table_name": "LoadTest_WpPosts",
                    "pk_attr": "ID",
                    "pk_type": "N",
                    "pk_count": 1000,
                    "pk_pad_width": None,
                    "sk_attr": None,
                    "sk_type": None,
                    "sk_count": None,
                    "sk_pad_width": None,
                    "items_seeded": 1000,
                }
            },
            total_items=1000,
            duration_seconds=5.0,
        )
        scripts_dir = generator.generate_all(
            access_patterns, schema_output, seed_manifest, TestConfig()
        )

        p = Path(scripts_dir)
        scenario_files = list((p / "scenarios").glob("q*.js"))
        assert len(scenario_files) == 1

    def test_generate_getitem_string_pk(self, generator):
        """String PK should use padStart in key generation."""
        seed_info = {
            "table_name": "LoadTest_WpOptions",
            "pk_attr": "option_name",
            "pk_type": "S",
            "pk_count": 500,
            "pk_pad_width": 6,
            "sk_attr": None,
            "sk_type": None,
            "sk_count": None,
            "sk_pad_width": None,
            "items_seeded": 500,
        }
        access_pattern = {
            "query_ids": ["q30"],
            "operation": "GetItem",
            "table_name": "WpOptions",
            "key_condition": "PK=option_name",
            "design_rps": 30,
        }
        table_def = {
            "table_name": "WpOptions",
            "partition_key": {"attribute_name": "option_name", "attribute_type": "S"},
            "sort_key": None,
        }
        script = generator.generate_scenario(access_pattern, table_def, seed_info)

        assert "padStart" in script
        assert "PK_PAD" in script
        assert "createPatternMetrics('q30')" in script
