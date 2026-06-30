"""Tests for ElastiCache (Valkey/Redis) seeder.

Mocks redis.Redis to verify:
  - Endpoint resolved from ELASTICACHE_ENDPOINT env var
  - Endpoint resolved from schema_output._cluster_endpoint as fallback
  - Raises ValueError when neither source provides an endpoint
  - TLS is enabled (ssl=True)
  - Each data type dispatches the correct Redis command via pipeline
  - Key interpolation replaces all {placeholder} occurrences with the index
  - TTL is set when key_design.ttl_seconds is present
  - Items are capped at max_items_per_table
  - SeedManifest carries key_pattern, data_type, items_seeded, and ttl_seconds
  - Multiple key designs are all seeded and totals are accumulated
  - Pipeline is flushed in chunks of 500
"""

import sys
from unittest.mock import MagicMock, patch

import pytest

from src.agents.load_test.elasticache.seeder import DEFAULT_ITEMS_PER_KEY_DESIGN, ElastiCacheSeeder

# =============================================================================
# Helpers
# =============================================================================


def _make_mock_redis_module() -> tuple[MagicMock, MagicMock, MagicMock]:
    """Build a fake redis module with a configurable Redis class."""
    mock_client = MagicMock()
    pipe = MagicMock()
    mock_client.pipeline.return_value = pipe
    pipe.execute.return_value = None

    redis_module = MagicMock()
    redis_module.Redis.return_value = mock_client
    return redis_module, mock_client, pipe


def _schema(
    key_designs: list | None = None,
    endpoint: str = "cache.example.com",
    port: int = 6379,
) -> dict:
    return {
        "_cluster_endpoint": endpoint,
        "_cluster_port": port,
        "key_designs": key_designs or [],
    }


def _key_design(
    key_pattern: str = "users:{user_id}",
    data_type: str = "string",
    estimated_key_count: int = 100,
    ttl_seconds: int | None = None,
    fields_mapped: list | None = None,
) -> dict:
    kd: dict = {
        "key_pattern": key_pattern,
        "data_type": data_type,
        "estimated_key_count": estimated_key_count,
    }
    if ttl_seconds is not None:
        kd["ttl_seconds"] = ttl_seconds
    if fields_mapped is not None:
        kd["fields_mapped"] = fields_mapped
    return kd


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def seeder() -> ElastiCacheSeeder:
    return ElastiCacheSeeder(region="us-east-1")


# =============================================================================
# Endpoint resolution
# =============================================================================


class TestEndpointResolution:
    def test_uses_env_var_endpoint(self, seeder: ElastiCacheSeeder, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ELASTICACHE_ENDPOINT", "env-cache.example.com")
        monkeypatch.setenv("ELASTICACHE_PORT", "6380")

        redis_mod, mock_client, _ = _make_mock_redis_module()
        with patch.dict(sys.modules, {"redis": redis_mod}):
            seeder.seed(_schema(endpoint="", key_designs=[]), max_items_per_table=10)

        redis_mod.Redis.assert_called_once()
        call_kwargs = redis_mod.Redis.call_args.kwargs
        assert call_kwargs["host"] == "env-cache.example.com"
        assert call_kwargs["port"] == 6380

    def test_falls_back_to_schema_output_endpoint(
        self, seeder: ElastiCacheSeeder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ELASTICACHE_ENDPOINT", raising=False)
        monkeypatch.delenv("ELASTICACHE_PORT", raising=False)

        schema = _schema(endpoint="schema-cache.example.com", port=6382)
        redis_mod, _, _ = _make_mock_redis_module()
        with patch.dict(sys.modules, {"redis": redis_mod}):
            seeder.seed(schema, max_items_per_table=10)

        call_kwargs = redis_mod.Redis.call_args.kwargs
        assert call_kwargs["host"] == "schema-cache.example.com"
        assert call_kwargs["port"] == 6382

    def test_raises_when_no_endpoint_provided(
        self, seeder: ElastiCacheSeeder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ELASTICACHE_ENDPOINT", raising=False)
        schema = {"_cluster_endpoint": "", "key_designs": []}

        redis_mod, _, _ = _make_mock_redis_module()
        with patch.dict(sys.modules, {"redis": redis_mod}):
            with pytest.raises(ValueError, match="ELASTICACHE_ENDPOINT"):
                seeder.seed(schema, max_items_per_table=10)

    def test_tls_is_always_enabled(
        self, seeder: ElastiCacheSeeder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ELASTICACHE_ENDPOINT", raising=False)
        redis_mod, _, _ = _make_mock_redis_module()
        with patch.dict(sys.modules, {"redis": redis_mod}):
            seeder.seed(_schema(key_designs=[]), max_items_per_table=10)

        call_kwargs = redis_mod.Redis.call_args.kwargs
        assert call_kwargs["ssl"] is True


# =============================================================================
# Manifest structure
# =============================================================================


class TestSeedManifest:
    def test_manifest_contains_resource_for_each_key_design(
        self, seeder: ElastiCacheSeeder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ELASTICACHE_ENDPOINT", raising=False)
        schema = _schema(
            key_designs=[
                _key_design("users:{id}", "string", 50),
                _key_design("posts:{id}", "hash", 30),
            ]
        )
        redis_mod, _, _ = _make_mock_redis_module()
        with patch.dict(sys.modules, {"redis": redis_mod}):
            manifest = seeder.seed(schema, max_items_per_table=100)

        assert "users:{id}" in manifest.resources
        assert "posts:{id}" in manifest.resources

    def test_manifest_total_items_sums_all_designs(
        self, seeder: ElastiCacheSeeder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ELASTICACHE_ENDPOINT", raising=False)
        schema = _schema(
            key_designs=[
                _key_design("k1:{id}", "string", 10),
                _key_design("k2:{id}", "string", 20),
            ]
        )
        redis_mod, _, _ = _make_mock_redis_module()
        with patch.dict(sys.modules, {"redis": redis_mod}):
            manifest = seeder.seed(schema, max_items_per_table=100)

        assert manifest.total_items == 30

    def test_manifest_carries_ttl(
        self, seeder: ElastiCacheSeeder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ELASTICACHE_ENDPOINT", raising=False)
        schema = _schema(key_designs=[_key_design("session:{id}", "string", 5, ttl_seconds=3600)])
        redis_mod, _, _ = _make_mock_redis_module()
        with patch.dict(sys.modules, {"redis": redis_mod}):
            manifest = seeder.seed(schema, max_items_per_table=100)

        assert manifest.resources["session:{id}"]["ttl_seconds"] == 3600

    def test_manifest_resource_carries_data_type(
        self, seeder: ElastiCacheSeeder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ELASTICACHE_ENDPOINT", raising=False)
        schema = _schema(key_designs=[_key_design("h:{id}", "hash", 5)])
        redis_mod, _, _ = _make_mock_redis_module()
        with patch.dict(sys.modules, {"redis": redis_mod}):
            manifest = seeder.seed(schema, max_items_per_table=100)

        assert manifest.resources["h:{id}"]["data_type"] == "hash"

    def test_empty_key_designs_returns_empty_manifest(
        self, seeder: ElastiCacheSeeder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ELASTICACHE_ENDPOINT", raising=False)
        redis_mod, _, _ = _make_mock_redis_module()
        with patch.dict(sys.modules, {"redis": redis_mod}):
            manifest = seeder.seed(_schema(key_designs=[]), max_items_per_table=100)

        assert manifest.total_items == 0
        assert manifest.resources == {}


# =============================================================================
# Cap at max_items_per_table
# =============================================================================


class TestMaxItemsCap:
    def test_caps_items_at_max_items_per_table(
        self, seeder: ElastiCacheSeeder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ELASTICACHE_ENDPOINT", raising=False)
        schema = _schema(key_designs=[_key_design("big:{id}", "string", 100_000)])
        redis_mod, _, _ = _make_mock_redis_module()
        with patch.dict(sys.modules, {"redis": redis_mod}):
            manifest = seeder.seed(schema, max_items_per_table=500)

        assert manifest.resources["big:{id}"]["items_seeded"] <= 500

    def test_uses_default_when_estimated_key_count_missing(
        self, seeder: ElastiCacheSeeder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ELASTICACHE_ENDPOINT", raising=False)
        schema = _schema(key_designs=[{"key_pattern": "k:{id}", "data_type": "string"}])
        redis_mod, _, _ = _make_mock_redis_module()
        with patch.dict(sys.modules, {"redis": redis_mod}):
            manifest = seeder.seed(schema, max_items_per_table=10_000)

        assert manifest.resources["k:{id}"]["items_seeded"] == DEFAULT_ITEMS_PER_KEY_DESIGN


# =============================================================================
# Key interpolation
# =============================================================================


class TestInterpolateKey:
    def test_single_placeholder_replaced(self, seeder: ElastiCacheSeeder) -> None:
        assert seeder._interpolate_key("users:{user_id}", 42) == "users:42"

    def test_multiple_placeholders_all_replaced(self, seeder: ElastiCacheSeeder) -> None:
        assert seeder._interpolate_key("a:{x}:b:{y}", 7) == "a:7:b:7"

    def test_no_placeholder_unchanged(self, seeder: ElastiCacheSeeder) -> None:
        assert seeder._interpolate_key("static-key", 1) == "static-key"


# =============================================================================
# Redis command dispatching per data type
# =============================================================================


class TestSeedKeyDesignCommands:
    """Verify that each data type calls the correct pipeline method at least once."""

    def _seed_type(
        self,
        seeder: ElastiCacheSeeder,
        data_type: str,
        monkeypatch: pytest.MonkeyPatch,
        fields: list | None = None,
        ttl: int | None = None,
    ) -> MagicMock:
        monkeypatch.delenv("ELASTICACHE_ENDPOINT", raising=False)
        redis_mod, mock_client, pipe = _make_mock_redis_module()
        kd = _key_design("key:{id}", data_type, 3, ttl_seconds=ttl, fields_mapped=fields)
        schema = _schema(key_designs=[kd])
        with patch.dict(sys.modules, {"redis": redis_mod}):
            seeder.seed(schema, max_items_per_table=3)
        pipe_mock: MagicMock = pipe
        return pipe_mock

    def test_string_uses_set(self, seeder: ElastiCacheSeeder, monkeypatch: pytest.MonkeyPatch) -> None:
        pipe = self._seed_type(seeder, "string", monkeypatch)
        pipe.set.assert_called()

    def test_hash_uses_hset(self, seeder: ElastiCacheSeeder, monkeypatch: pytest.MonkeyPatch) -> None:
        pipe = self._seed_type(seeder, "hash", monkeypatch, fields=["f1", "f2"])
        pipe.hset.assert_called()

    def test_list_uses_rpush(self, seeder: ElastiCacheSeeder, monkeypatch: pytest.MonkeyPatch) -> None:
        pipe = self._seed_type(seeder, "list", monkeypatch)
        pipe.rpush.assert_called()

    def test_set_uses_sadd(self, seeder: ElastiCacheSeeder, monkeypatch: pytest.MonkeyPatch) -> None:
        pipe = self._seed_type(seeder, "set", monkeypatch)
        pipe.sadd.assert_called()

    def test_sorted_set_uses_zadd(self, seeder: ElastiCacheSeeder, monkeypatch: pytest.MonkeyPatch) -> None:
        pipe = self._seed_type(seeder, "sorted_set", monkeypatch)
        pipe.zadd.assert_called()

    def test_stream_uses_xadd(self, seeder: ElastiCacheSeeder, monkeypatch: pytest.MonkeyPatch) -> None:
        pipe = self._seed_type(seeder, "stream", monkeypatch)
        pipe.xadd.assert_called()

    def test_hyperloglog_uses_pfadd(
        self, seeder: ElastiCacheSeeder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pipe = self._seed_type(seeder, "hyperloglog", monkeypatch)
        pipe.pfadd.assert_called()

    def test_unknown_type_falls_back_to_set(
        self, seeder: ElastiCacheSeeder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pipe = self._seed_type(seeder, "unknown_type_xyz", monkeypatch)
        pipe.set.assert_called()

    def test_ttl_calls_expire_on_pipeline(
        self, seeder: ElastiCacheSeeder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pipe = self._seed_type(seeder, "string", monkeypatch, ttl=300)
        pipe.expire.assert_called()
        # TTL value is forwarded
        expire_call_ttls = [c.args[1] for c in pipe.expire.call_args_list]
        assert all(t == 300 for t in expire_call_ttls)

    def test_no_expire_when_ttl_not_set(
        self, seeder: ElastiCacheSeeder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pipe = self._seed_type(seeder, "string", monkeypatch, ttl=None)
        pipe.expire.assert_not_called()


# =============================================================================
# Pipeline batching
# =============================================================================


class TestPipelineBatching:
    def test_pipeline_flushed_every_500_items(
        self, seeder: ElastiCacheSeeder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ELASTICACHE_ENDPOINT", raising=False)
        schema = _schema(key_designs=[_key_design("k:{id}", "string", 1_200)])
        redis_mod, mock_client, pipe = _make_mock_redis_module()
        # pipeline() returns the same pipe object each time
        mock_client.pipeline.return_value = pipe

        with patch.dict(sys.modules, {"redis": redis_mod}):
            seeder.seed(schema, max_items_per_table=1_200)

        # 1200 items: flush at 500, 1000, and final remainder → 3 execute() calls
        assert pipe.execute.call_count >= 3
