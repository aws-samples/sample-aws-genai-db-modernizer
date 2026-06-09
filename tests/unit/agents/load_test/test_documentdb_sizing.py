"""Tests for DocumentDB cluster sizing module (ADR-022).

Covers:
  - Constants and catalog integrity
  - Helper functions (_safe_get, _field_size_bytes, _cpu_factor_for, _infer_field_type)
  - has_complete_metrics() input validation
  - estimate_working_set_gb() — all four regimes plus clamp edge case
  - estimate_denorm_multiplier() — no/all/mixed embedding plus cap
  - estimate_index_size_gb() — empty/single/multiple collections plus scale factor
  - estimate_target_data_size_gb() — schema-provided vs fallback heuristic
  - compute_ram_constraint(), compute_vcpu_constraint(), compute_conn_constraint()
  - pick_smallest_satisfying() — small/medium/large/exceeds-ceiling
  - apply_cost_ceiling(), apply_floor()
  - compute_replicas() — single/multi-az/replicas/both
  - derive_engine_version()
  - derive_cluster_config() integration — derived/mapped/fallback strategies
  - Property-based: chosen class always satisfies constraints (or hits ceiling)
  - Regression scenarios: realistic small/medium/large workloads
"""

from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src.agents.load_test.documentdb.sizing import (
    COST_CEILING_INSTANCE_CLASS,
    DOCUMENTDB_INSTANCE_CLASSES,
    FALLBACK_INSTANCE_CLASS,
    FLOOR_INSTANCE_CLASS,
    INSTANCE_CLASS_BY_NAME,
    INSTANCE_CLASS_ORDER,
    SOURCE_TO_DOCUMENTDB_BASELINE,
    ClusterConfig,
    SizingRationale,
    _cpu_factor_for,
    _field_size_bytes,
    _infer_field_type,
    _safe_get,
    apply_cost_ceiling,
    apply_floor,
    compute_conn_constraint,
    compute_ram_constraint,
    compute_replicas,
    compute_vcpu_constraint,
    derive_cluster_config,
    derive_engine_version,
    estimate_denorm_multiplier,
    estimate_index_size_gb,
    estimate_target_data_size_gb,
    estimate_working_set_gb,
    has_complete_metrics,
    pick_smallest_satisfying,
)

# =============================================================================
# Fixtures
# =============================================================================


class FakeTestConfig:
    """Lightweight stand-in for TestConfig (avoids contract dependency)."""

    def __init__(self, scale_factor: float = 1.0, max_concurrent_vus: int = 50) -> None:
        self.scale_factor = scale_factor
        self.max_concurrent_vus = max_concurrent_vus


@pytest.fixture
def fake_test_config() -> FakeTestConfig:
    return FakeTestConfig()


@pytest.fixture
def minimal_collector_output() -> dict[str, Any]:
    """Collector output with all metrics required by has_complete_metrics()."""
    return {
        "metrics": {
            "cache_hit_ratio_pct": 99.5,
            "cpu_utilization_avg": 30.0,
            "max_connections": 100,
        },
        "source_database": {"database_size_gb": 50.0},
        "rds_metadata": {
            "instance_class": "db.r6i.2xlarge",
            "instance_specs": {"memory_gb": 64.0, "vcpus": 8},
            "read_replica_count": 0,
            "multi_az": False,
        },
        "schema": {
            "tables": [{"table_name": "users", "row_count": 100_000}],
        },
    }


@pytest.fixture
def minimal_schema_output() -> dict[str, Any]:
    return {
        "collections": [
            {
                "source_tables": ["users"],
                "embedded_entities": [],
                "indexes": [
                    {"keys": {"_id": 1}},
                    {"keys": {"email": 1}},
                ],
                "fields": [{"name": "email", "type": "string"}],
                "estimated_avg_doc_size_kb": 0.5,
            }
        ],
        "access_patterns": [
            {"operation": "findOne", "design_rps": 10},
            {"operation": "find", "design_rps": 50},
        ],
        "target_engine_version_min": "5.0.0",
    }


# =============================================================================
# Constants & catalog integrity
# =============================================================================


class TestCatalogIntegrity:
    def test_catalog_is_monotonically_increasing(self) -> None:
        """Each instance class must be larger than the previous on all dimensions."""
        for prev, curr in zip(
            DOCUMENTDB_INSTANCE_CLASSES, DOCUMENTDB_INSTANCE_CLASSES[1:], strict=False
        ):
            assert curr.vcpu >= prev.vcpu
            assert curr.ram_gb >= prev.ram_gb
            assert curr.max_connections >= prev.max_connections
            assert curr.hourly_cost_usd >= prev.hourly_cost_usd

    def test_floor_class_exists_in_catalog(self) -> None:
        assert FLOOR_INSTANCE_CLASS in INSTANCE_CLASS_BY_NAME

    def test_ceiling_class_exists_in_catalog(self) -> None:
        assert COST_CEILING_INSTANCE_CLASS in INSTANCE_CLASS_BY_NAME

    def test_fallback_class_exists_in_catalog(self) -> None:
        assert FALLBACK_INSTANCE_CLASS in INSTANCE_CLASS_BY_NAME

    def test_floor_is_smaller_than_ceiling(self) -> None:
        assert (
            INSTANCE_CLASS_ORDER[FLOOR_INSTANCE_CLASS]
            < INSTANCE_CLASS_ORDER[COST_CEILING_INSTANCE_CLASS]
        )

    def test_fallback_is_between_floor_and_ceiling(self) -> None:
        assert (
            INSTANCE_CLASS_ORDER[FLOOR_INSTANCE_CLASS]
            <= INSTANCE_CLASS_ORDER[FALLBACK_INSTANCE_CLASS]
            <= INSTANCE_CLASS_ORDER[COST_CEILING_INSTANCE_CLASS]
        )

    def test_mapping_table_values_exist_in_catalog(self) -> None:
        """Every mapping target must be a valid DocumentDB instance class."""
        for src_class, dst_class in SOURCE_TO_DOCUMENTDB_BASELINE.items():
            assert (
                dst_class in INSTANCE_CLASS_BY_NAME
            ), f"{src_class} maps to {dst_class} which is not in catalog"


# =============================================================================
# Internal helpers
# =============================================================================


class TestSafeGet:
    def test_returns_value_for_valid_path(self) -> None:
        assert _safe_get({"a": {"b": {"c": 42}}}, "a", "b", "c") == 42

    def test_returns_default_for_missing_intermediate_key(self) -> None:
        assert _safe_get({"a": {}}, "a", "b", "c", default=-1) == -1

    def test_returns_default_for_non_dict_intermediate(self) -> None:
        assert _safe_get({"a": "string"}, "a", "b", default=-1) == -1

    def test_returns_default_for_none_input(self) -> None:
        assert _safe_get(None, "a", default="x") == "x"

    def test_default_is_none_by_default(self) -> None:
        assert _safe_get({}, "missing") is None


class TestFieldSizeBytes:
    def test_known_type(self) -> None:
        assert _field_size_bytes("int") == 4
        assert _field_size_bytes("string") == 50

    def test_case_insensitive(self) -> None:
        assert _field_size_bytes("INT") == 4
        assert _field_size_bytes("ObjectId") == 12

    def test_unknown_type_uses_default(self) -> None:
        assert _field_size_bytes("custom_type") == 50

    def test_none_uses_default(self) -> None:
        assert _field_size_bytes(None) == 50


class TestCpuFactorFor:
    def test_known_operation(self) -> None:
        assert _cpu_factor_for("findOne") == 0.3
        assert _cpu_factor_for("aggregate_lookup") == 2.5

    def test_unknown_operation_uses_default(self) -> None:
        assert _cpu_factor_for("custom_op") == 0.5

    def test_none_uses_default(self) -> None:
        assert _cpu_factor_for(None) == 0.5


class TestInferFieldType:
    def test_id_field_returns_objectid(self) -> None:
        assert _infer_field_type("_id", {}) == "objectid"

    def test_named_field_lookup_in_metadata(self) -> None:
        collection = {"fields": [{"name": "email", "type": "string"}]}
        assert _infer_field_type("email", collection) == "string"

    def test_missing_field_returns_none(self) -> None:
        assert _infer_field_type("nonexistent", {"fields": []}) is None

    def test_no_fields_metadata_returns_none(self) -> None:
        assert _infer_field_type("anything", {}) is None


# =============================================================================
# has_complete_metrics
# =============================================================================


class TestHasCompleteMetrics:
    def test_complete_returns_true(self, minimal_collector_output: dict[str, Any]) -> None:
        assert has_complete_metrics(minimal_collector_output) is True

    def test_missing_cache_hit_ratio(self, minimal_collector_output: dict[str, Any]) -> None:
        del minimal_collector_output["metrics"]["cache_hit_ratio_pct"]
        assert has_complete_metrics(minimal_collector_output) is False

    def test_missing_cpu_utilization(self, minimal_collector_output: dict[str, Any]) -> None:
        del minimal_collector_output["metrics"]["cpu_utilization_avg"]
        assert has_complete_metrics(minimal_collector_output) is False

    def test_missing_database_size(self, minimal_collector_output: dict[str, Any]) -> None:
        del minimal_collector_output["source_database"]["database_size_gb"]
        assert has_complete_metrics(minimal_collector_output) is False

    def test_missing_memory_gb(self, minimal_collector_output: dict[str, Any]) -> None:
        del minimal_collector_output["rds_metadata"]["instance_specs"]["memory_gb"]
        assert has_complete_metrics(minimal_collector_output) is False

    def test_empty_dict_returns_false(self) -> None:
        assert has_complete_metrics({}) is False


# =============================================================================
# estimate_working_set_gb
# =============================================================================


class TestEstimateWorkingSet:
    def test_high_hit_ratio_uses_buffer_pool_fraction(self) -> None:
        co = {
            "metrics": {"cache_hit_ratio_pct": 99.5},
            "rds_metadata": {"instance_specs": {"memory_gb": 64}},
            "source_database": {"database_size_gb": 100},
        }
        ws, method, regime = estimate_working_set_gb(co)
        assert ws == 64 * 0.75  # min(48, 100)
        assert method == "buffer_cache_ratio"
        assert regime == "high_hit_ratio"

    def test_high_hit_ratio_clamps_to_total_data(self) -> None:
        """Working set never exceeds total data (over-provisioned source)."""
        co = {
            "metrics": {"cache_hit_ratio_pct": 99.99},
            "rds_metadata": {"instance_specs": {"memory_gb": 512}},  # huge
            "source_database": {"database_size_gb": 10},  # tiny
        }
        ws, _, _ = estimate_working_set_gb(co)
        assert ws == 10  # clamped to data size, not 384

    def test_moderate_hit_ratio_uses_miss_extrapolation(self) -> None:
        co = {
            "metrics": {"cache_hit_ratio_pct": 85.0},  # 15% miss
            "rds_metadata": {"instance_specs": {"memory_gb": 64}},
            "source_database": {"database_size_gb": 200},
        }
        ws, method, regime = estimate_working_set_gb(co)
        # miss_rate=0.15, factor = 1 + 0.15*3 = 1.45 → 64 * 1.45 = 92.8
        # cap at 200 * 0.5 = 100
        assert ws == pytest.approx(64 * 1.45)
        assert regime == "moderate_hit_ratio"
        assert method == "buffer_cache_ratio"

    def test_thrashing_uses_data_fraction(self) -> None:
        co = {
            "metrics": {"cache_hit_ratio_pct": 60.0},
            "rds_metadata": {"instance_specs": {"memory_gb": 8}},
            "source_database": {"database_size_gb": 100},
        }
        ws, method, regime = estimate_working_set_gb(co)
        assert ws == 50  # 100 * 0.5
        assert method == "data_fraction"
        assert regime == "thrashing"

    def test_no_metric_uses_default_fraction(self) -> None:
        co = {
            "metrics": {},
            "rds_metadata": {"instance_specs": {"memory_gb": 64}},
            "source_database": {"database_size_gb": 100},
        }
        ws, method, regime = estimate_working_set_gb(co)
        assert ws == 20  # 100 * 0.20
        assert method == "data_fraction"
        assert regime == "no_metric"


# =============================================================================
# estimate_denorm_multiplier
# =============================================================================


class TestEstimateDenormMultiplier:
    def test_no_collections_returns_1(self) -> None:
        assert estimate_denorm_multiplier({}) == 1.0

    def test_no_embedded_entities_returns_1(self) -> None:
        schema: dict[str, Any] = {"collections": [{"embedded_entities": []}]}
        assert estimate_denorm_multiplier(schema) == 1.0

    def test_all_embedded_returns_max_per_embed(self) -> None:
        schema = {
            "collections": [
                {
                    "embedded_entities": [
                        {"strategy": "embed"},
                        {"strategy": "embed"},
                    ]
                }
            ]
        }
        # ratio = 1.0, multiplier = 1.0 + 1.0 * 0.2 = 1.2
        assert estimate_denorm_multiplier(schema) == pytest.approx(1.2)

    def test_all_referenced_returns_1(self) -> None:
        schema = {
            "collections": [
                {"embedded_entities": [{"strategy": "reference"}, {"strategy": "reference"}]}
            ]
        }
        # ratio = 0, multiplier = 1.0
        assert estimate_denorm_multiplier(schema) == 1.0

    def test_mixed_returns_proportional(self) -> None:
        schema = {
            "collections": [
                {
                    "embedded_entities": [
                        {"strategy": "embed"},
                        {"strategy": "reference"},
                    ]
                }
            ]
        }
        # ratio = 0.5, multiplier = 1.0 + 0.5 * 0.2 = 1.1
        assert estimate_denorm_multiplier(schema) == pytest.approx(1.1)

    def test_caps_at_max_multiplier(self) -> None:
        """Even with extreme embedding, multiplier caps at DENORM_MAX_MULTIPLIER."""
        # Per-embed factor is 0.2, max ratio is 1.0 → max multiplier from formula = 1.2.
        # The cap at 1.3 is defensive; this confirms it's not violated.
        schema = {"collections": [{"embedded_entities": [{"strategy": "embed"}] * 100}]}
        assert estimate_denorm_multiplier(schema) <= 1.3


# =============================================================================
# estimate_index_size_gb
# =============================================================================


class TestEstimateIndexSize:
    def test_empty_schema_returns_zero(self) -> None:
        size, count = estimate_index_size_gb({}, {}, 1.0)
        assert size == 0
        assert count == 0

    def test_single_collection_single_index(self) -> None:
        schema = {
            "collections": [
                {
                    "source_tables": ["users"],
                    "indexes": [{"keys": {"_id": 1}}],
                }
            ]
        }
        co = {"schema": {"tables": [{"table_name": "users", "row_count": 1_000_000}]}}
        size, count = estimate_index_size_gb(schema, co, 1.0)
        # entry = 40 + 12 (objectid) = 52 bytes; total = 52M bytes ≈ 0.0484 GB
        assert size == pytest.approx(52 * 1_000_000 / (1024**3), rel=0.01)
        assert count == 1

    def test_multiple_collections(self) -> None:
        schema = {
            "collections": [
                {
                    "source_tables": ["users"],
                    "indexes": [{"keys": {"_id": 1}}, {"keys": {"email": 1}}],
                    "fields": [{"name": "email", "type": "string"}],
                },
                {
                    "source_tables": ["orders"],
                    "indexes": [{"keys": {"_id": 1}}],
                },
            ]
        }
        co = {
            "schema": {
                "tables": [
                    {"table_name": "users", "row_count": 1_000},
                    {"table_name": "orders", "row_count": 5_000},
                ]
            }
        }
        _, count = estimate_index_size_gb(schema, co, 1.0)
        assert count == 3

    def test_scale_factor_increases_size(self) -> None:
        schema = {
            "collections": [
                {
                    "source_tables": ["users"],
                    "indexes": [{"keys": {"_id": 1}}],
                }
            ]
        }
        co = {"schema": {"tables": [{"table_name": "users", "row_count": 1_000_000}]}}
        size_1x, _ = estimate_index_size_gb(schema, co, 1.0)
        size_2x, _ = estimate_index_size_gb(schema, co, 2.0)
        assert size_2x == pytest.approx(size_1x * 2, rel=0.01)

    def test_missing_source_table_skipped_silently(self) -> None:
        schema = {
            "collections": [
                {
                    "source_tables": ["nonexistent"],
                    "indexes": [{"keys": {"_id": 1}}],
                }
            ]
        }
        co: dict[str, Any] = {"schema": {"tables": []}}
        size, count = estimate_index_size_gb(schema, co, 1.0)
        assert size == 0
        assert count == 0

    def test_compound_index_sums_field_sizes(self) -> None:
        schema = {
            "collections": [
                {
                    "source_tables": ["users"],
                    "indexes": [{"keys": {"first_name": 1, "last_name": 1}}],
                    "fields": [
                        {"name": "first_name", "type": "string"},
                        {"name": "last_name", "type": "string"},
                    ],
                }
            ]
        }
        co = {"schema": {"tables": [{"table_name": "users", "row_count": 1000}]}}
        size, _ = estimate_index_size_gb(schema, co, 1.0)
        # 40 + 50 + 50 = 140 bytes per entry × 1000 docs
        assert size == pytest.approx(140 * 1000 / (1024**3), rel=0.01)


# =============================================================================
# estimate_target_data_size_gb
# =============================================================================


class TestEstimateTargetDataSize:
    def test_uses_schema_provided_estimates(self) -> None:
        schema = {
            "collections": [
                {
                    "source_tables": ["users"],
                    "estimated_avg_doc_size_kb": 2.0,
                }
            ]
        }
        co = {"schema": {"tables": [{"table_name": "users", "row_count": 1_000_000}]}}
        size = estimate_target_data_size_gb(schema, co, 1.0)
        # 1M docs × 2 KB × 1024 bytes/KB / (1024^3 bytes/GiB) ≈ 1.907 GiB
        expected = (1_000_000 * 2.0 * 1024) / (1024**3)
        assert size == pytest.approx(expected, rel=0.01)

    def test_falls_back_to_source_data_with_multiplier(self) -> None:
        schema: dict[str, Any] = {"collections": []}  # no estimates
        co = {"source_database": {"database_size_gb": 100.0}}
        size = estimate_target_data_size_gb(schema, co, 1.0)
        # multiplier = 1.0 (no embedding), so size = 100
        assert size == 100.0

    def test_scale_factor_applied(self) -> None:
        schema: dict[str, Any] = {"collections": []}
        co = {"source_database": {"database_size_gb": 100.0}}
        size = estimate_target_data_size_gb(schema, co, 0.5)
        assert size == 50.0

    def test_denorm_multiplier_applied_in_fallback(self) -> None:
        schema = {
            "collections": [
                {"embedded_entities": [{"strategy": "embed"}]},
            ]
        }
        co = {"source_database": {"database_size_gb": 100.0}}
        size = estimate_target_data_size_gb(schema, co, 1.0)
        # multiplier = 1.0 + 1.0 * 0.2 = 1.2
        assert size == pytest.approx(120.0, rel=0.01)


# =============================================================================
# Constraint computations
# =============================================================================


class TestComputeRamConstraint:
    def test_floor_when_workload_is_tiny(
        self, minimal_schema_output: dict[str, Any], fake_test_config: FakeTestConfig
    ) -> None:
        co = {
            "metrics": {"cache_hit_ratio_pct": 99.9},
            "rds_metadata": {"instance_specs": {"memory_gb": 4}},  # tiny source
            "source_database": {"database_size_gb": 1},
            "schema": {"tables": [{"table_name": "users", "row_count": 100}]},
        }
        ram, _ = compute_ram_constraint(minimal_schema_output, co, fake_test_config)
        floor = INSTANCE_CLASS_BY_NAME[FLOOR_INSTANCE_CLASS]
        assert ram >= floor.ram_gb

    def test_breakdown_includes_indexes_and_working_set(
        self,
        minimal_schema_output: dict[str, Any],
        minimal_collector_output: dict[str, Any],
        fake_test_config: FakeTestConfig,
    ) -> None:
        _, breakdown = compute_ram_constraint(
            minimal_schema_output, minimal_collector_output, fake_test_config
        )
        assert "indexes_gb" in breakdown
        assert "working_set_gb" in breakdown
        assert "needed_gb" in breakdown


class TestComputeVcpuConstraint:
    def test_floor_when_workload_zero(self, fake_test_config: FakeTestConfig) -> None:
        schema: dict[str, Any] = {"access_patterns": []}
        co = {
            "rds_metadata": {"instance_specs": {"vcpus": 0}},
            "metrics": {"cpu_utilization_avg": 0.0},
        }
        vcpu, _ = compute_vcpu_constraint(schema, co, fake_test_config)
        floor = INSTANCE_CLASS_BY_NAME[FLOOR_INSTANCE_CLASS]
        assert vcpu >= floor.vcpu

    def test_workload_dominant(self, fake_test_config: FakeTestConfig) -> None:
        schema = {
            "access_patterns": [
                {"operation": "aggregate_lookup", "design_rps": 5000},  # heavy
            ]
        }
        co = {
            "rds_metadata": {"instance_specs": {"vcpus": 2}},
            "metrics": {"cpu_utilization_avg": 10.0},  # source idle
        }
        vcpu, breakdown = compute_vcpu_constraint(schema, co, fake_test_config)
        # workload: 5000 * 2.5 / 1000 = 12.5
        assert breakdown["workload_vcpu"] == pytest.approx(12.5)
        assert vcpu >= 12.5


class TestComputeConnConstraint:
    def test_source_dominant(self, fake_test_config: FakeTestConfig) -> None:
        co = {"metrics": {"max_connections": 1000}}
        conns, _ = compute_conn_constraint(co, fake_test_config)
        assert conns >= 1500  # 1000 * 1.5

    def test_test_concurrency_dominant(self) -> None:
        cfg = FakeTestConfig(max_concurrent_vus=5000)
        co = {"metrics": {"max_connections": 100}}
        conns, _ = compute_conn_constraint(co, cfg)
        assert conns >= 5000


# =============================================================================
# pick_smallest_satisfying
# =============================================================================


class TestPickSmallestSatisfying:
    def test_picks_floor_for_tiny_load(self) -> None:
        ic, binding = pick_smallest_satisfying(ram_gb=1.0, vcpu=1.0, conn=100)
        assert ic.name == "db.r6g.large"
        # binding = whichever is highest pressure relative to capacity
        assert binding in {"ram", "vcpu", "conn"}

    def test_ram_binds_for_memory_heavy(self) -> None:
        # 60 GB RAM needed, 2 vCPU, low conn → r6g.2xlarge (64 GB / 8 vCPU)
        ic, binding = pick_smallest_satisfying(ram_gb=60.0, vcpu=2.0, conn=100)
        assert ic.name == "db.r6g.2xlarge"
        assert binding == "ram"

    def test_vcpu_binds_for_cpu_heavy(self) -> None:
        # Low RAM, 30 vCPU needed → r6g.8xlarge (32 vCPU / 256 GB)
        ic, binding = pick_smallest_satisfying(ram_gb=10.0, vcpu=30.0, conn=100)
        assert ic.name == "db.r6g.8xlarge"
        assert binding == "vcpu"

    def test_returns_largest_when_constraints_exceed_max(self) -> None:
        # Beyond largest class → returns largest with binding="exceeds_ceiling"
        ic, binding = pick_smallest_satisfying(ram_gb=10000.0, vcpu=200, conn=100_000)
        assert ic.name == DOCUMENTDB_INSTANCE_CLASSES[-1].name
        assert binding == "exceeds_ceiling"


# =============================================================================
# apply_cost_ceiling / apply_floor
# =============================================================================


class TestApplyCostCeiling:
    def test_below_ceiling_unchanged(self) -> None:
        small = INSTANCE_CLASS_BY_NAME["db.r6g.large"]
        assert apply_cost_ceiling(small).name == "db.r6g.large"

    def test_at_ceiling_unchanged(self) -> None:
        ceiling = INSTANCE_CLASS_BY_NAME[COST_CEILING_INSTANCE_CLASS]
        assert apply_cost_ceiling(ceiling).name == COST_CEILING_INSTANCE_CLASS

    def test_above_ceiling_capped(self) -> None:
        big = INSTANCE_CLASS_BY_NAME["db.r6g.16xlarge"]
        assert apply_cost_ceiling(big).name == COST_CEILING_INSTANCE_CLASS


class TestApplyFloor:
    def test_at_floor_unchanged(self) -> None:
        floor = INSTANCE_CLASS_BY_NAME[FLOOR_INSTANCE_CLASS]
        assert apply_floor(floor).name == FLOOR_INSTANCE_CLASS

    def test_above_floor_unchanged(self) -> None:
        bigger = INSTANCE_CLASS_BY_NAME["db.r6g.4xlarge"]
        assert apply_floor(bigger).name == "db.r6g.4xlarge"

    # Floor is the smallest class, so "below floor" isn't naturally reachable
    # via the catalog. apply_floor() exists for defensive use should the
    # catalog ever expand to include sub-r6g.large classes.


# =============================================================================
# compute_replicas
# =============================================================================


class TestComputeReplicas:
    def test_single_instance_source(self) -> None:
        co = {"rds_metadata": {"read_replica_count": 0, "multi_az": False}}
        count, reason = compute_replicas(co)
        assert count == 0
        assert "single-instance" in reason

    def test_multi_az_no_replicas(self) -> None:
        co = {"rds_metadata": {"read_replica_count": 0, "multi_az": True}}
        count, reason = compute_replicas(co)
        assert count == 1
        assert "multi-az" in reason

    def test_with_replicas(self) -> None:
        co = {"rds_metadata": {"read_replica_count": 3, "multi_az": False}}
        count, reason = compute_replicas(co)
        assert count == 3
        assert "replica count" in reason

    def test_replicas_take_precedence_over_multi_az(self) -> None:
        co = {"rds_metadata": {"read_replica_count": 2, "multi_az": True}}
        count, _ = compute_replicas(co)
        assert count == 2  # explicit replicas, not just HA minimum


# =============================================================================
# derive_engine_version
# =============================================================================


class TestDeriveEngineVersion:
    def test_uses_schema_provided_version(self) -> None:
        schema = {"target_engine_version_min": "8.0.0"}
        assert derive_engine_version(schema) == "8.0.0"

    def test_defaults_to_5_0_0(self) -> None:
        assert derive_engine_version({}) == "5.0.0"


# =============================================================================
# derive_cluster_config — integration
# =============================================================================


class TestDeriveClusterConfigIntegration:
    def test_derived_strategy_when_metrics_complete(
        self,
        minimal_schema_output: dict[str, Any],
        minimal_collector_output: dict[str, Any],
        fake_test_config: FakeTestConfig,
    ) -> None:
        cfg = derive_cluster_config(
            minimal_schema_output, minimal_collector_output, fake_test_config
        )
        assert cfg.sizing_strategy == "derived"
        assert cfg.instance_class in INSTANCE_CLASS_BY_NAME
        assert cfg.sizing_rationale.source_metrics_complete is True

    def test_mapped_strategy_when_metrics_missing(
        self,
        minimal_schema_output: dict[str, Any],
        fake_test_config: FakeTestConfig,
    ) -> None:
        # Missing metrics but known source class
        co = {
            "rds_metadata": {
                "instance_class": "db.r6i.4xlarge",
                "instance_specs": {"memory_gb": 128, "vcpus": 16},
                "read_replica_count": 0,
                "multi_az": False,
            },
            "schema": {"tables": [{"table_name": "users", "row_count": 100}]},
            "source_database": {"database_size_gb": 50},
            # metrics missing
        }
        cfg = derive_cluster_config(minimal_schema_output, co, fake_test_config)
        assert cfg.sizing_strategy == "mapped"
        # r6i.4xlarge maps to r6g.4xlarge
        assert cfg.instance_class == "db.r6g.4xlarge"

    def test_fallback_when_unknown_source_and_no_metrics(
        self,
        minimal_schema_output: dict[str, Any],
        fake_test_config: FakeTestConfig,
    ) -> None:
        co = {
            "rds_metadata": {
                "instance_class": "db.unknown.instance",  # not in mapping
                "read_replica_count": 0,
                "multi_az": False,
            },
            "schema": {"tables": []},
        }
        cfg = derive_cluster_config(minimal_schema_output, co, fake_test_config)
        assert cfg.sizing_strategy == "fallback"
        assert cfg.instance_class == FALLBACK_INSTANCE_CLASS

    def test_replica_count_propagates(
        self,
        minimal_schema_output: dict[str, Any],
        minimal_collector_output: dict[str, Any],
        fake_test_config: FakeTestConfig,
    ) -> None:
        minimal_collector_output["rds_metadata"]["multi_az"] = True
        cfg = derive_cluster_config(
            minimal_schema_output, minimal_collector_output, fake_test_config
        )
        assert cfg.replica_count == 1
        assert cfg.instance_count == 2  # 1 writer + 1 replica

    def test_engine_version_from_schema(
        self,
        minimal_schema_output: dict[str, Any],
        minimal_collector_output: dict[str, Any],
        fake_test_config: FakeTestConfig,
    ) -> None:
        minimal_schema_output["target_engine_version_min"] = "8.0.0"
        cfg = derive_cluster_config(
            minimal_schema_output, minimal_collector_output, fake_test_config
        )
        assert cfg.engine_version == "8.0.0"

    def test_rationale_serializes_to_dict(
        self,
        minimal_schema_output: dict[str, Any],
        minimal_collector_output: dict[str, Any],
        fake_test_config: FakeTestConfig,
    ) -> None:
        cfg = derive_cluster_config(
            minimal_schema_output, minimal_collector_output, fake_test_config
        )
        d = cfg.sizing_rationale.to_dict()
        assert "chosen_class" in d
        assert "binding_constraint" in d
        assert "working_set_gb" in d


# =============================================================================
# Property-based tests — Hypothesis
# =============================================================================


class TestPropertyBased:
    @given(
        ram=st.floats(min_value=0.1, max_value=10000.0),
        vcpu=st.floats(min_value=0.1, max_value=200.0),
        conn=st.integers(min_value=1, max_value=100_000),
    )
    @settings(max_examples=200)
    def test_chosen_satisfies_or_exceeds_ceiling(self, ram: float, vcpu: float, conn: int) -> None:
        """For any plausible inputs, chosen class either satisfies all
        constraints or is the largest class with binding=exceeds_ceiling."""
        ic, binding = pick_smallest_satisfying(ram, vcpu, conn)
        if binding == "exceeds_ceiling":
            assert ic.name == DOCUMENTDB_INSTANCE_CLASSES[-1].name
        else:
            assert ic.ram_gb >= ram
            assert ic.vcpu >= vcpu
            assert ic.max_connections >= conn

    @given(st.sampled_from(list(INSTANCE_CLASS_BY_NAME.values())))
    def test_apply_floor_never_returns_smaller(self, ic: Any) -> None:
        """apply_floor never returns a class smaller than the floor."""
        result = apply_floor(ic)
        assert INSTANCE_CLASS_ORDER[result.name] >= INSTANCE_CLASS_ORDER[FLOOR_INSTANCE_CLASS]

    @given(st.sampled_from(list(INSTANCE_CLASS_BY_NAME.values())))
    def test_apply_ceiling_never_returns_larger(self, ic: Any) -> None:
        """apply_cost_ceiling never returns a class larger than the ceiling."""
        result = apply_cost_ceiling(ic)
        assert (
            INSTANCE_CLASS_ORDER[result.name] <= INSTANCE_CLASS_ORDER[COST_CEILING_INSTANCE_CLASS]
        )

    @given(
        hit_ratio=st.floats(min_value=0.0, max_value=100.0),
        ram_gb=st.floats(min_value=1.0, max_value=2048.0),
        data_gb=st.floats(min_value=0.1, max_value=10_000.0),
    )
    @settings(max_examples=200)
    def test_working_set_never_exceeds_total_data(
        self, hit_ratio: float, ram_gb: float, data_gb: float
    ) -> None:
        """Working set estimate is always ≤ total data (clamp invariant)."""
        co = {
            "metrics": {"cache_hit_ratio_pct": hit_ratio},
            "rds_metadata": {"instance_specs": {"memory_gb": ram_gb}},
            "source_database": {"database_size_gb": data_gb},
        }
        ws, _, _ = estimate_working_set_gb(co)
        assert ws <= data_gb


# =============================================================================
# Regression scenarios — realistic customer workloads
# =============================================================================


class TestRegressionScenarios:
    def test_mysql_loadtest_scenario_from_claude_md(self, fake_test_config: FakeTestConfig) -> None:
        """Reproduces the mysql-loadtest scenario from claude.md §28.

        Source: db.r6g.2xlarge (8 vCPU, 64 GB RAM), 42 GB data, 99.86% hit ratio.
        Expected: derived strategy picks something at or near r6g.2xlarge.
        """
        co = {
            "metrics": {
                "cache_hit_ratio_pct": 99.86,
                "cpu_utilization_avg": 1.3,
                "max_connections": 50,
            },
            "source_database": {"database_size_gb": 42.0},
            "rds_metadata": {
                "instance_class": "db.r6g.2xlarge",
                "instance_specs": {"memory_gb": 64, "vcpus": 8},
                "read_replica_count": 0,
                "multi_az": False,
            },
            "schema": {"tables": [{"table_name": "users", "row_count": 391_000_000}]},
        }
        schema = {
            "collections": [
                {
                    "source_tables": ["users"],
                    "embedded_entities": [],
                    "indexes": [{"keys": {"_id": 1}}],
                    "estimated_avg_doc_size_kb": 0.1,
                }
            ],
            "access_patterns": [],
            "target_engine_version_min": "5.0.0",
        }
        cfg = derive_cluster_config(schema, co, fake_test_config)
        assert cfg.sizing_strategy == "derived"
        # Working set: 64 GB * 0.75 = 48 GB; ram_needed = 48 * 1.5 = 72 GB
        # → smallest class with ≥72 GB RAM = r6g.4xlarge (128 GB)
        # Alternatively if indexes dominate, could be different.
        # Either way, must be at or above r6g.2xlarge.
        assert INSTANCE_CLASS_ORDER[cfg.instance_class] >= INSTANCE_CLASS_ORDER["db.r6g.2xlarge"]

    def test_small_workload_picks_floor(self, fake_test_config: FakeTestConfig) -> None:
        """A tiny dev workload (1 GB, no traffic) should pick floor class."""
        co = {
            "metrics": {
                "cache_hit_ratio_pct": 100.0,
                "cpu_utilization_avg": 0.5,
                "max_connections": 5,
            },
            "source_database": {"database_size_gb": 1.0},
            "rds_metadata": {
                "instance_class": "db.t3.micro",
                "instance_specs": {"memory_gb": 1.0, "vcpus": 2},
                "read_replica_count": 0,
                "multi_az": False,
            },
            "schema": {"tables": [{"table_name": "users", "row_count": 1000}]},
        }
        schema = {
            "collections": [{"source_tables": ["users"], "indexes": []}],
            "access_patterns": [],
            "target_engine_version_min": "5.0.0",
        }
        cfg = derive_cluster_config(schema, co, fake_test_config)
        assert cfg.instance_class == FLOOR_INSTANCE_CLASS

    def test_huge_workload_caps_at_ceiling(self, fake_test_config: FakeTestConfig) -> None:
        """A workload exceeding all classes should cap at the ceiling."""
        co = {
            "metrics": {
                "cache_hit_ratio_pct": 99.9,
                "cpu_utilization_avg": 95.0,  # very high
                "max_connections": 80_000,  # exceeds even r6g.16xlarge
            },
            "source_database": {"database_size_gb": 5_000.0},
            "rds_metadata": {
                "instance_class": "db.r6i.32xlarge",  # fictional huge source
                "instance_specs": {"memory_gb": 1024, "vcpus": 128},
                "read_replica_count": 0,
                "multi_az": False,
            },
            "schema": {"tables": [{"table_name": "events", "row_count": 10_000_000_000}]},
        }
        schema = {
            "collections": [
                {
                    "source_tables": ["events"],
                    "indexes": [{"keys": {"_id": 1}}, {"keys": {"timestamp": 1}}],
                    "fields": [{"name": "timestamp", "type": "date"}],
                }
            ],
            "access_patterns": [],
            "target_engine_version_min": "5.0.0",
        }
        cfg = derive_cluster_config(schema, co, fake_test_config)
        # Must cap at ceiling (or below)
        assert (
            INSTANCE_CLASS_ORDER[cfg.instance_class]
            <= INSTANCE_CLASS_ORDER[COST_CEILING_INSTANCE_CLASS]
        )

    def test_dataclass_immutability(
        self,
        minimal_schema_output: dict[str, Any],
        minimal_collector_output: dict[str, Any],
        fake_test_config: FakeTestConfig,
    ) -> None:
        """ClusterConfig and SizingRationale are frozen — mutation raises."""
        from dataclasses import FrozenInstanceError

        cfg = derive_cluster_config(
            minimal_schema_output, minimal_collector_output, fake_test_config
        )
        assert isinstance(cfg, ClusterConfig)
        assert isinstance(cfg.sizing_rationale, SizingRationale)
        with pytest.raises(FrozenInstanceError):
            cfg.instance_class = "db.r6g.large"  # type: ignore[misc]
