"""Integration test: parse real WordPress schema, generate all scripts, validate."""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from src.agents.load_test.dynamodb.key_condition_parser import parse_key_condition
from src.agents.load_test.dynamodb.provisioner import DynamoDBProvisioner
from src.agents.load_test.dynamodb.script_generator import DynamoDBScriptGenerator
from src.agents.load_test.handler import _get_testable_patterns

SCHEMA_PATH = Path(
    "docs/examples/wordpress/load-test-results/schema-dynamodb/v2/schema_output.json"
)


@pytest.fixture
def wordpress_schema():
    if not SCHEMA_PATH.exists():
        pytest.skip("WordPress schema output not available")
    return json.loads(SCHEMA_PATH.read_text())


class TestWordPressIntegration:
    def test_all_in_scope_patterns_are_testable(self, wordpress_schema):
        """WordPress has 52 in-scope patterns with traffic."""
        patterns = _get_testable_patterns(wordpress_schema)
        assert len(patterns) >= 40

    def test_all_key_conditions_parse(self, wordpress_schema):
        """Every in-scope key_condition can be parsed without error."""
        patterns = _get_testable_patterns(wordpress_schema)
        for ap in patterns:
            kc = ap.get("key_condition", "")
            result = parse_key_condition(kc)
            # In-scope patterns should never have N/A key_condition
            assert (
                result is not None
            ), f"Pattern {ap.get('pattern_id')} has unparseable key_condition: {kc}"

    def test_provisioner_builds_params_for_all_tables(self, wordpress_schema):
        """Provisioner can build CreateTable params for every table with traffic."""
        with patch("boto3.client"):
            provisioner = DynamoDBProvisioner(region="us-east-1")
            table_defs = provisioner._filter_tables_with_traffic(wordpress_schema)
            assert len(table_defs) >= 15  # Most of 19 tables have traffic

            for td in table_defs:
                params = provisioner._build_create_table_params(td)
                assert params["TableName"].startswith("LoadTest_")
                assert params["BillingMode"] == "PAY_PER_REQUEST"
                assert len(params["KeySchema"]) >= 1

    def test_script_generator_produces_valid_scripts_for_all_patterns(self, wordpress_schema):
        """Script generator can produce a script for every in-scope pattern."""
        generator = DynamoDBScriptGenerator(region="us-east-1")
        patterns = _get_testable_patterns(wordpress_schema)
        table_defs = {td["table_name"]: td for td in wordpress_schema["table_definitions"]}

        generated = 0
        for ap in patterns:
            table_def = table_defs.get(ap["table_name"])
            if not table_def:
                continue

            # Build mock seed info from table definition
            pk = table_def["partition_key"]
            sk = table_def.get("sort_key")
            seed_info = {
                "table_name": f"LoadTest_{table_def['table_name']}",
                "pk_attr": pk["attribute_name"],
                "pk_type": pk["attribute_type"],
                "pk_count": 100,
                "pk_pad_width": 4 if pk["attribute_type"] == "S" else None,
                "sk_attr": sk["attribute_name"] if sk else None,
                "sk_type": sk["attribute_type"] if sk else None,
                "sk_count": 10 if sk else None,
                "sk_pad_width": 4 if sk and sk["attribute_type"] == "S" else None,
                "items_seeded": 1000,
            }

            try:
                script = generator.generate_scenario(ap, table_def, seed_info)
                assert len(script) > 100, f"Script too short for {ap['pattern_id']}"
                assert "dynamoRequest" in script, f"Missing dynamoRequest in {ap['pattern_id']}"
                generated += 1
            except Exception as e:
                pytest.fail(f"Failed to generate script for {ap['pattern_id']}: {e}")

        assert generated >= 10, f"Only generated {generated} scripts, expected >= 10"
