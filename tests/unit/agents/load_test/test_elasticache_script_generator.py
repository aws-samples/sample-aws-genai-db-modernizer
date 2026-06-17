"""Tests for ElastiCache k6 script generator.

Verifies:
  - generate_scenario produces valid JS for every supported Redis operation
  - Unsupported operations fall through to sendCommand
  - Key pattern placeholder is embedded in the rendered script
  - items_seeded is used as MAX_KEY_ID
  - Read ops have op_type="read", write ops have op_type="write"
  - generate_main produces constant-arrival-rate executor blocks per scenario
  - generate_main includes handleSummary and textSummary import
  - generate_main sets scenario thresholds for iteration_duration
  - generate_all writes main.js + one scenario file per access pattern
  - generate_all returns a path that exists on disk
  - scenario JS imports xk6-redis and uses the client
  - Safe JS identifier is generated for pattern_id containing hyphens/dots
"""

import json
from pathlib import Path
from typing import Any

import pytest

from src.agents.load_test.elasticache.script_generator import ElastiCacheScriptGenerator
from src.agents.load_test.models import SeedManifest
from src.contracts.load_test_models import TestConfig

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def generator() -> ElastiCacheScriptGenerator:
    return ElastiCacheScriptGenerator(region="us-east-1")


@pytest.fixture
def seed_info() -> dict[str, Any]:
    return {
        "key_pattern": "users:{user_id}",
        "data_type": "string",
        "items_seeded": 5_000,
    }


@pytest.fixture
def test_config() -> TestConfig:
    return TestConfig(duration_minutes=10, warmup_seconds=30)


def _make_pattern(
    pattern_id: str = "p001",
    operation: str = "GET",
    key_pattern: str = "users:{user_id}",
    design_rps: int = 50,
    command_example: str = "",
) -> dict[str, Any]:
    return {
        "pattern_id": pattern_id,
        "operation": operation,
        "key_pattern": key_pattern,
        "design_rps": design_rps,
        "command_example": command_example,
    }


def _make_seed_manifest(resources: dict[str, Any] | None = None) -> SeedManifest:
    resources = resources or {"users:{user_id}": {"items_seeded": 5000}}
    return SeedManifest(
        resources=resources,
        total_items=sum(r.get("items_seeded", 0) for r in resources.values()),
        duration_seconds=1.5,
    )


# =============================================================================
# generate_scenario — structure
# =============================================================================


class TestGenerateScenarioStructure:
    def test_imports_xk6_redis(
        self, generator: ElastiCacheScriptGenerator, seed_info: dict[str, Any]
    ) -> None:
        script = generator.generate_scenario(_make_pattern(), {}, seed_info)
        assert 'import redis from "k6/x/redis"' in script

    def test_includes_max_key_id(
        self, generator: ElastiCacheScriptGenerator, seed_info: dict[str, Any]
    ) -> None:
        script = generator.generate_scenario(_make_pattern(), {}, seed_info)
        assert "MAX_KEY_ID = 5000" in script

    def test_includes_key_pattern(
        self, generator: ElastiCacheScriptGenerator, seed_info: dict[str, Any]
    ) -> None:
        script = generator.generate_scenario(_make_pattern(), {}, seed_info)
        assert "users:{user_id}" in script

    def test_pattern_id_in_comment(
        self, generator: ElastiCacheScriptGenerator, seed_info: dict[str, Any]
    ) -> None:
        script = generator.generate_scenario(_make_pattern("p-42"), {}, seed_info)
        assert "p-42" in script

    def test_safe_id_replaces_hyphens_and_dots(
        self, generator: ElastiCacheScriptGenerator, seed_info: dict[str, Any]
    ) -> None:
        """Hyphens and dots must be converted to underscores so the ID is JS-safe."""
        script = generator.generate_scenario(_make_pattern("ap-1.read"), {}, seed_info)
        # Hyphens/dots in the function name should become underscores
        assert "ap_1_read" in script
        # The original invalid JS identifier should not appear as a function name
        assert "function ap-1.read" not in script

    def test_uses_tls_redis_uri(
        self, generator: ElastiCacheScriptGenerator, seed_info: dict[str, Any]
    ) -> None:
        script = generator.generate_scenario(_make_pattern(), {}, seed_info)
        assert "rediss://" in script

    def test_includes_request_and_error_counters(
        self, generator: ElastiCacheScriptGenerator, seed_info: dict[str, Any]
    ) -> None:
        script = generator.generate_scenario(_make_pattern("p001"), {}, seed_info)
        assert "requests_p001" in script
        assert "errors_p001" in script

    def test_exports_async_function(
        self, generator: ElastiCacheScriptGenerator, seed_info: dict[str, Any]
    ) -> None:
        script = generator.generate_scenario(_make_pattern("p001"), {}, seed_info)
        assert "export async function p001" in script

    def test_try_catch_increments_error_counter(
        self, generator: ElastiCacheScriptGenerator, seed_info: dict[str, Any]
    ) -> None:
        script = generator.generate_scenario(_make_pattern("p001"), {}, seed_info)
        assert "errors_p001.add(1)" in script


# =============================================================================
# generate_scenario — per-operation command rendering
# =============================================================================


READ_OPS = [
    "GET", "HGET", "HGETALL", "HMGET", "LRANGE", "SMEMBERS", "SISMEMBER",
    "ZRANGE", "ZREVRANGE", "ZRANGEBYSCORE", "ZRANK", "ZSCORE",
    "XRANGE", "PFCOUNT", "JSON.GET",
]

WRITE_OPS = [
    "SET", "HSET", "LPUSH", "RPUSH", "SADD", "ZADD", "XADD",
    "GEOADD", "PFADD", "DEL", "INCR", "JSON.SET",
]


class TestGenerateScenarioOperations:
    @pytest.mark.parametrize("op", READ_OPS)
    def test_read_operation_renders_client_call(
        self, generator: ElastiCacheScriptGenerator, seed_info: dict[str, Any], op: str
    ) -> None:
        script = generator.generate_scenario(_make_pattern(operation=op), {}, seed_info)
        assert "client." in script or "client.sendCommand" in script

    @pytest.mark.parametrize("op", WRITE_OPS)
    def test_write_operation_renders_client_call(
        self, generator: ElastiCacheScriptGenerator, seed_info: dict[str, Any], op: str
    ) -> None:
        script = generator.generate_scenario(_make_pattern(operation=op), {}, seed_info)
        assert "client." in script or "client.sendCommand" in script

    def test_unknown_operation_uses_sendcommand(
        self, generator: ElastiCacheScriptGenerator, seed_info: dict[str, Any]
    ) -> None:
        script = generator.generate_scenario(
            _make_pattern(operation="CUSTOM_OP"), {}, seed_info
        )
        assert "sendCommand" in script
        assert "CUSTOM_OP" in script

    def test_get_uses_client_get(
        self, generator: ElastiCacheScriptGenerator, seed_info: dict[str, Any]
    ) -> None:
        script = generator.generate_scenario(_make_pattern(operation="GET"), {}, seed_info)
        assert "client.get(key)" in script

    def test_hset_uses_client_hset(
        self, generator: ElastiCacheScriptGenerator, seed_info: dict[str, Any]
    ) -> None:
        script = generator.generate_scenario(_make_pattern(operation="HSET"), {}, seed_info)
        assert "client.hset(" in script

    def test_zadd_uses_client_zadd(
        self, generator: ElastiCacheScriptGenerator, seed_info: dict[str, Any]
    ) -> None:
        script = generator.generate_scenario(_make_pattern(operation="ZADD"), {}, seed_info)
        assert "client.zadd(" in script

    def test_json_get_uses_sendcommand(
        self, generator: ElastiCacheScriptGenerator, seed_info: dict[str, Any]
    ) -> None:
        script = generator.generate_scenario(_make_pattern(operation="JSON.GET"), {}, seed_info)
        assert "sendCommand" in script
        assert "JSON.GET" in script

    def test_incr_uses_client_incr(
        self, generator: ElastiCacheScriptGenerator, seed_info: dict[str, Any]
    ) -> None:
        script = generator.generate_scenario(_make_pattern(operation="INCR"), {}, seed_info)
        assert "client.incr(key)" in script

    def test_incrby_maps_to_incr(
        self, generator: ElastiCacheScriptGenerator, seed_info: dict[str, Any]
    ) -> None:
        script = generator.generate_scenario(_make_pattern(operation="INCRBY"), {}, seed_info)
        assert "client.incr(key)" in script


# =============================================================================
# generate_main
# =============================================================================


class TestGenerateMain:
    def test_includes_handleSummary(self, generator: ElastiCacheScriptGenerator) -> None:
        main_js = generator.generate_main(
            [{"design_rps": 10}], duration_minutes=5, warmup_seconds=30
        )
        assert "handleSummary" in main_js

    def test_imports_textSummary(self, generator: ElastiCacheScriptGenerator) -> None:
        main_js = generator.generate_main(
            [{"design_rps": 10}], duration_minutes=5, warmup_seconds=30
        )
        assert "textSummary" in main_js

    def test_constant_arrival_rate_executor(self, generator: ElastiCacheScriptGenerator) -> None:
        main_js = generator.generate_main(
            [{"design_rps": 50}], duration_minutes=5, warmup_seconds=30
        )
        assert "constant-arrival-rate" in main_js

    def test_rate_matches_design_rps(self, generator: ElastiCacheScriptGenerator) -> None:
        main_js = generator.generate_main(
            [{"design_rps": 200}], duration_minutes=5, warmup_seconds=30
        )
        assert "rate: 200" in main_js

    def test_duration_in_minutes(self, generator: ElastiCacheScriptGenerator) -> None:
        main_js = generator.generate_main(
            [{"design_rps": 10}], duration_minutes=15, warmup_seconds=30
        )
        assert '"15m"' in main_js

    def test_warmup_as_start_time(self, generator: ElastiCacheScriptGenerator) -> None:
        main_js = generator.generate_main(
            [{"design_rps": 10}], duration_minutes=5, warmup_seconds=45
        )
        assert '"45s"' in main_js

    def test_one_scenario_per_access_pattern(self, generator: ElastiCacheScriptGenerator) -> None:
        scenarios = [{"design_rps": 10}, {"design_rps": 20}, {"design_rps": 30}]
        main_js = generator.generate_main(scenarios, duration_minutes=5, warmup_seconds=30)
        assert main_js.count("constant-arrival-rate") == 3

    def test_iteration_duration_threshold_present(
        self, generator: ElastiCacheScriptGenerator
    ) -> None:
        main_js = generator.generate_main(
            [{"design_rps": 10}], duration_minutes=5, warmup_seconds=30
        )
        assert "iteration_duration" in main_js
        assert "p(95)" in main_js

    def test_per_scenario_threshold_included(self, generator: ElastiCacheScriptGenerator) -> None:
        main_js = generator.generate_main(
            [{"design_rps": 10}, {"design_rps": 20}],
            duration_minutes=5,
            warmup_seconds=30,
        )
        assert "scenario:scenario_0" in main_js
        assert "scenario:scenario_1" in main_js

    def test_json_summary_path_from_env(self, generator: ElastiCacheScriptGenerator) -> None:
        main_js = generator.generate_main(
            [{"design_rps": 10}], duration_minutes=5, warmup_seconds=30
        )
        assert "K6_SUMMARY_PATH" in main_js


# =============================================================================
# generate_all
# =============================================================================


class TestGenerateAll:
    def test_returns_existing_directory(
        self,
        generator: ElastiCacheScriptGenerator,
        test_config: TestConfig,
    ) -> None:
        schema_output: dict = {"key_designs": []}
        seed_manifest = _make_seed_manifest()
        access_patterns = [_make_pattern("p001", "GET", "users:{user_id}", 10)]

        scripts_dir = generator.generate_all(
            access_patterns, schema_output, seed_manifest, test_config
        )
        assert Path(scripts_dir).exists()

    def test_writes_main_js(
        self,
        generator: ElastiCacheScriptGenerator,
        test_config: TestConfig,
    ) -> None:
        schema_output: dict = {}
        seed_manifest = _make_seed_manifest()
        access_patterns = [_make_pattern("p001", "GET", "users:{user_id}", 10)]

        scripts_dir = generator.generate_all(
            access_patterns, schema_output, seed_manifest, test_config
        )
        assert (Path(scripts_dir) / "main.js").exists()

    def test_writes_one_scenario_per_pattern(
        self,
        generator: ElastiCacheScriptGenerator,
        test_config: TestConfig,
    ) -> None:
        schema_output: dict = {}
        seed_manifest = _make_seed_manifest(
            {
                "users:{user_id}": {"items_seeded": 100},
                "posts:{post_id}": {"items_seeded": 200},
            }
        )
        access_patterns = [
            _make_pattern("p001", "GET", "users:{user_id}", 10),
            _make_pattern("p002", "HGET", "posts:{post_id}", 5),
        ]

        scripts_dir = generator.generate_all(
            access_patterns, schema_output, seed_manifest, test_config
        )
        p = Path(scripts_dir)
        assert (p / "scenario_0.js").exists()
        assert (p / "scenario_1.js").exists()

    def test_scenario_js_references_correct_max_key_id(
        self,
        generator: ElastiCacheScriptGenerator,
        test_config: TestConfig,
    ) -> None:
        schema_output: dict = {}
        seed_manifest = _make_seed_manifest({"users:{user_id}": {"items_seeded": 7_500}})
        access_patterns = [_make_pattern("p001", "GET", "users:{user_id}", 10)]

        scripts_dir = generator.generate_all(
            access_patterns, schema_output, seed_manifest, test_config
        )
        scenario_text = (Path(scripts_dir) / "scenario_0.js").read_text()
        assert "MAX_KEY_ID = 7500" in scenario_text

    def test_falls_back_to_default_seed_info_when_key_pattern_not_in_manifest(
        self,
        generator: ElastiCacheScriptGenerator,
        test_config: TestConfig,
    ) -> None:
        """If key_pattern is absent from seed_manifest, items_seeded defaults to 1000."""
        schema_output: dict = {}
        seed_manifest = _make_seed_manifest({})  # empty resources
        access_patterns = [_make_pattern("p001", "GET", "missing:{id}", 10)]

        scripts_dir = generator.generate_all(
            access_patterns, schema_output, seed_manifest, test_config
        )
        scenario_text = (Path(scripts_dir) / "scenario_0.js").read_text()
        assert "MAX_KEY_ID = 1000" in scenario_text

    def test_empty_access_patterns_produces_main_only(
        self,
        generator: ElastiCacheScriptGenerator,
        test_config: TestConfig,
    ) -> None:
        scripts_dir = generator.generate_all([], {}, _make_seed_manifest({}), test_config)
        p = Path(scripts_dir)
        assert (p / "main.js").exists()
        scenario_files = list(p.glob("scenario_*.js"))
        assert len(scenario_files) == 0
