"""DocumentDB cluster sizing for load tests.

Implements the hybrid derived+mapped sizing strategy from ADR-022. The module
is intentionally pure-functional — no AWS calls, no I/O, no database connections.
All inputs are passed in as plain dicts so the module is fully unit-testable.

Public entry point: :func:`derive_cluster_config`.

See ``docs/architecture/decisions/ADR-022-documentdb-cluster-sizing.md`` for the
design discussion behind the heuristics in this module.
"""

from dataclasses import asdict, dataclass
from typing import Any, Literal

# =============================================================================
# Constants — see ADR-022 for the rationale behind each value
# =============================================================================

# Floor — never provision smaller (latency baseline guarantee)
FLOOR_INSTANCE_CLASS = "db.r6g.large"

# Cost ceiling — explicit override to exceed is deferred to v2 (YAGNI)
COST_CEILING_INSTANCE_CLASS = "db.r6g.8xlarge"

# Last-resort fallback when no metrics + no class mapping
FALLBACK_INSTANCE_CLASS = "db.r6g.xlarge"

# Working set heuristics (see ADR-022 § Three Sizing Constraints)
WORKING_SET_HIT_RATIO_HIGH = 95.0
WORKING_SET_HIT_RATIO_MODERATE = 80.0
INNODB_BUFFER_POOL_FRACTION = 0.75  # mysqld default buffer pool fraction
WORKING_SET_FRACTION_DEFAULT = 0.20  # when hit ratio metric is unavailable
WORKING_SET_FRACTION_THRASHING = 0.50  # severe thrashing, conservative

# Safety factors
RAM_SAFETY_FACTOR = 1.5
INDEX_RAM_HEADROOM = 1.5
VCPU_HEADROOM = 1.3
CONNECTION_HEADROOM = 1.5

# Denormalization multiplier (see ADR-022 § Edge Cases)
DENORM_MAX_MULTIPLIER = 1.3
DENORM_PER_EMBED_FACTOR = 0.2

# vCPU per 1,000 RPS by operation type (see ADR-022 § vCPU)
CPU_FACTORS: dict[str, float] = {
    "findOne": 0.3,
    "find": 0.5,
    "find_with_sort": 1.0,
    "aggregate": 1.5,
    "aggregate_lookup": 2.5,
    "insertOne": 0.4,
    "insertMany": 0.6,
    "updateOne": 0.4,
    "updateMany": 0.5,
    "deleteOne": 0.3,
    "deleteMany": 0.4,
    "bulkWrite": 0.6,
}
DEFAULT_CPU_FACTOR = 0.5

# Field-size estimates (bytes) for index size calculation
FIELD_SIZE_BYTES: dict[str, int] = {
    "_id": 12,
    "objectid": 12,
    "int": 4,
    "long": 8,
    "double": 8,
    "decimal": 16,
    "string": 50,
    "boolean": 1,
    "date": 8,
    "uuid": 16,
    "binary": 100,
}
DEFAULT_FIELD_SIZE_BYTES = 50

# B-tree entry overhead in MongoDB indexes
INDEX_ENTRY_OVERHEAD_BYTES = 40


# =============================================================================
# Instance class catalog
# =============================================================================


@dataclass(frozen=True)
class InstanceClass:
    """A DocumentDB instance class with its capacity and cost."""

    name: str
    vcpu: int
    ram_gb: int
    max_connections: int
    hourly_cost_usd: float


DOCUMENTDB_INSTANCE_CLASSES: tuple[InstanceClass, ...] = (
    InstanceClass("db.r6g.large", 2, 16, 2_000, 0.245),
    InstanceClass("db.r6g.xlarge", 4, 32, 4_000, 0.490),
    InstanceClass("db.r6g.2xlarge", 8, 64, 8_000, 0.980),
    InstanceClass("db.r6g.4xlarge", 16, 128, 16_000, 1.960),
    InstanceClass("db.r6g.8xlarge", 32, 256, 32_000, 3.920),
    InstanceClass("db.r6g.12xlarge", 48, 384, 64_000, 5.880),
    InstanceClass("db.r6g.16xlarge", 64, 512, 64_000, 7.840),
)

INSTANCE_CLASS_BY_NAME: dict[str, InstanceClass] = {
    ic.name: ic for ic in DOCUMENTDB_INSTANCE_CLASSES
}

# Smallest-to-largest ordinal for ceiling/floor comparisons
INSTANCE_CLASS_ORDER: dict[str, int] = {
    ic.name: i for i, ic in enumerate(DOCUMENTDB_INSTANCE_CLASSES)
}


# =============================================================================
# Source class → DocumentDB baseline mapping
# =============================================================================

SOURCE_TO_DOCUMENTDB_BASELINE: dict[str, str] = {
    # Memory-optimized r-series — match RAM 1:1
    "db.r6i.large": "db.r6g.large",
    "db.r6i.xlarge": "db.r6g.xlarge",
    "db.r6i.2xlarge": "db.r6g.2xlarge",
    "db.r6i.4xlarge": "db.r6g.4xlarge",
    "db.r6i.8xlarge": "db.r6g.8xlarge",
    "db.r5.large": "db.r6g.large",
    "db.r5.xlarge": "db.r6g.xlarge",
    "db.r5.2xlarge": "db.r6g.2xlarge",
    "db.r5.4xlarge": "db.r6g.4xlarge",
    "db.r5.8xlarge": "db.r6g.8xlarge",
    "db.r6g.large": "db.r6g.large",
    "db.r6g.xlarge": "db.r6g.xlarge",
    "db.r6g.2xlarge": "db.r6g.2xlarge",
    "db.r6g.4xlarge": "db.r6g.4xlarge",
    "db.r6g.8xlarge": "db.r6g.8xlarge",
    "db.r7g.large": "db.r6g.large",
    "db.r7g.xlarge": "db.r6g.xlarge",
    "db.r7g.2xlarge": "db.r6g.2xlarge",
    "db.r7g.4xlarge": "db.r6g.4xlarge",
    "db.r7g.8xlarge": "db.r6g.8xlarge",
    # General-purpose m-series — promote to memory-optimized one tier down
    "db.m6i.large": "db.r6g.large",
    "db.m6i.xlarge": "db.r6g.xlarge",
    "db.m6i.2xlarge": "db.r6g.xlarge",
    "db.m6i.4xlarge": "db.r6g.2xlarge",
    "db.m5.large": "db.r6g.large",
    "db.m5.xlarge": "db.r6g.xlarge",
    "db.m5.2xlarge": "db.r6g.xlarge",
    "db.m5.4xlarge": "db.r6g.2xlarge",
    "db.m6g.large": "db.r6g.large",
    "db.m6g.xlarge": "db.r6g.xlarge",
    "db.m6g.2xlarge": "db.r6g.xlarge",
    "db.m6g.4xlarge": "db.r6g.2xlarge",
    # Burstable t-series — minimum production tier
    "db.t3.small": "db.r6g.large",
    "db.t3.medium": "db.r6g.large",
    "db.t3.large": "db.r6g.large",
    "db.t3.xlarge": "db.r6g.xlarge",
    "db.t3.2xlarge": "db.r6g.xlarge",
    "db.t4g.small": "db.r6g.large",
    "db.t4g.medium": "db.r6g.large",
    "db.t4g.large": "db.r6g.large",
    "db.t4g.xlarge": "db.r6g.xlarge",
    "db.t4g.2xlarge": "db.r6g.xlarge",
}


# =============================================================================
# Output dataclasses
# =============================================================================


@dataclass(frozen=True)
class SizingRationale:
    """Auditable explanation of how the cluster size was chosen.

    Serialized to ``sizing_rationale.json`` for customer transparency.
    """

    source_instance_class: str | None
    source_ram_gb: float | None
    source_vcpu: int | None
    source_metrics_complete: bool
    strategy: Literal["derived", "mapped", "fallback"]
    working_set_gb: float
    working_set_method: str
    working_set_regime: str
    indexes_size_gb: float
    target_data_size_gb: float
    denorm_multiplier: float
    constraint_ram_gb: float
    constraint_vcpu: float
    constraint_conn: int
    binding_constraint: str
    chosen_class: str
    chosen_class_vcpu: int
    chosen_class_ram_gb: int
    chosen_class_reason: str
    replicas_count: int
    replicas_reason: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize to plain dict for JSON output."""
        return asdict(self)


@dataclass(frozen=True)
class ClusterConfig:
    """Final cluster configuration to be provisioned."""

    instance_class: str
    instance_count: int  # writer + replicas
    replica_count: int
    engine_version: str
    sizing_strategy: Literal["derived", "mapped", "fallback"]
    sizing_rationale: SizingRationale


# =============================================================================
# Internal helpers
# =============================================================================


def _safe_get(d: Any, *keys: str, default: Any = None) -> Any:
    """Safely traverse a nested dict; return default if any key missing."""
    cur = d
    for key in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def _field_size_bytes(field_type: str | None) -> int:
    """Look up byte size for an indexed field type."""
    if field_type is None:
        return DEFAULT_FIELD_SIZE_BYTES
    return FIELD_SIZE_BYTES.get(field_type.lower(), DEFAULT_FIELD_SIZE_BYTES)


def _cpu_factor_for(operation: str | None) -> float:
    """Look up CPU factor for an operation type."""
    if operation is None:
        return DEFAULT_CPU_FACTOR
    return CPU_FACTORS.get(operation, DEFAULT_CPU_FACTOR)


def _infer_field_type(field_name: str, collection: dict[str, Any]) -> str | None:
    """Infer field type from collection metadata; None falls back to default."""
    if field_name == "_id":
        return "objectid"
    fields = _safe_get(collection, "fields", default=[])
    for field_meta in fields:
        if _safe_get(field_meta, "name") == field_name:
            field_type = _safe_get(field_meta, "type")
            return str(field_type) if field_type is not None else None
    return None


# =============================================================================
# Input validation
# =============================================================================


def has_complete_metrics(collector_output: dict[str, Any]) -> bool:
    """Return True when all metrics needed for derived sizing are present."""
    required_paths = (
        ("metrics", "cache_hit_ratio_pct"),
        ("metrics", "cpu_utilization_avg"),
        ("metrics", "max_connections"),
        ("source_database", "database_size_gb"),
        ("rds_metadata", "instance_specs", "memory_gb"),
        ("rds_metadata", "instance_specs", "vcpus"),
    )
    return all(_safe_get(collector_output, *path) is not None for path in required_paths)


# =============================================================================
# Working set estimation
# =============================================================================


def estimate_working_set_gb(
    collector_output: dict[str, Any],
) -> tuple[float, str, str]:
    """Estimate hot working set size using BufferCacheHitRatio.

    Returns (working_set_gb, method, regime).

    method values:
      - "buffer_cache_ratio" — derived from observed hit ratio
      - "data_fraction"      — fixed-fraction fallback

    regime values:
      - "high_hit_ratio"     — hit ratio >= 95%, working set fits in source RAM
      - "moderate_hit_ratio" — 80% <= hit ratio < 95%, slight thrashing
      - "thrashing"          — hit ratio < 80%, source undersized
      - "no_metric"          — hit ratio unavailable
    """
    hit_ratio = _safe_get(collector_output, "metrics", "cache_hit_ratio_pct")
    source_ram_gb = _safe_get(
        collector_output, "rds_metadata", "instance_specs", "memory_gb", default=0.0
    )
    total_data_gb = _safe_get(collector_output, "source_database", "database_size_gb", default=0.0)

    if hit_ratio is None:
        return total_data_gb * WORKING_SET_FRACTION_DEFAULT, "data_fraction", "no_metric"

    if hit_ratio >= WORKING_SET_HIT_RATIO_HIGH:
        ws = min(source_ram_gb * INNODB_BUFFER_POOL_FRACTION, total_data_gb)
        return ws, "buffer_cache_ratio", "high_hit_ratio"

    if hit_ratio >= WORKING_SET_HIT_RATIO_MODERATE:
        miss_rate = (100 - hit_ratio) / 100
        ws = min(source_ram_gb * (1.0 + miss_rate * 3.0), total_data_gb * 0.5)
        return ws, "buffer_cache_ratio", "moderate_hit_ratio"

    return total_data_gb * WORKING_SET_FRACTION_THRASHING, "data_fraction", "thrashing"


# =============================================================================
# Denormalization multiplier
# =============================================================================


def estimate_denorm_multiplier(schema_output: dict[str, Any]) -> float:
    """Estimate target data size growth from embedding.

    Returns a multiplier in [1.0, DENORM_MAX_MULTIPLIER]. Each embedded
    relationship adds approximately DENORM_PER_EMBED_FACTOR (20%) to parent
    document size, scaled by the embed:reference ratio.
    """
    collections = _safe_get(schema_output, "collections", default=[])
    embedded_count = 0
    referenced_count = 0
    for collection in collections:
        for entity in _safe_get(collection, "embedded_entities", default=[]):
            strategy = _safe_get(entity, "strategy")
            if strategy == "embed":
                embedded_count += 1
            elif strategy == "reference":
                referenced_count += 1

    total = embedded_count + referenced_count
    if total == 0:
        return 1.0

    embed_ratio = embedded_count / total
    multiplier = 1.0 + (embed_ratio * DENORM_PER_EMBED_FACTOR)
    return min(multiplier, DENORM_MAX_MULTIPLIER)


# =============================================================================
# Index size estimation
# =============================================================================


def estimate_index_size_gb(
    schema_output: dict[str, Any],
    collector_output: dict[str, Any],
    scale_factor: float,
) -> tuple[float, int]:
    """Estimate total index size across all collections.

    Returns (total_size_gb, total_indexes_count).

    Document count uses the parent table row count from collector_output
    (children are inside parent docs, not separate index entries — see ADR-022).
    """
    total_bytes = 0
    total_indexes = 0

    collections = _safe_get(schema_output, "collections", default=[])
    source_tables = _safe_get(collector_output, "schema", "tables", default=[])
    table_rows = {t.get("table_name"): t.get("row_count", 0) for t in source_tables}

    for collection in collections:
        source_tables_list = _safe_get(collection, "source_tables", default=[])
        if not source_tables_list:
            continue
        parent_table = source_tables_list[0]
        doc_count = int(table_rows.get(parent_table, 0) * scale_factor)

        if doc_count == 0:
            continue

        for index in _safe_get(collection, "indexes", default=[]):
            keys = _safe_get(index, "keys", default={})
            entry_size = INDEX_ENTRY_OVERHEAD_BYTES
            for field_name in keys:
                entry_size += _field_size_bytes(_infer_field_type(field_name, collection))
            total_bytes += entry_size * doc_count
            total_indexes += 1

    return total_bytes / (1024**3), total_indexes


# =============================================================================
# Target data size
# =============================================================================


def estimate_target_data_size_gb(
    schema_output: dict[str, Any],
    collector_output: dict[str, Any],
    scale_factor: float,
) -> float:
    """Estimate post-denormalization data size in GB.

    Prefers schema-provided ``estimated_avg_doc_size_kb`` when every collection
    has it. Otherwise falls back to ``source_data * scale * denorm_multiplier``.
    """
    collections = _safe_get(schema_output, "collections", default=[])
    has_estimates = bool(collections) and all(
        _safe_get(c, "estimated_avg_doc_size_kb") is not None for c in collections
    )

    if has_estimates:
        source_tables = _safe_get(collector_output, "schema", "tables", default=[])
        table_rows = {t.get("table_name"): t.get("row_count", 0) for t in source_tables}
        total_bytes = 0.0
        for collection in collections:
            source_list = _safe_get(collection, "source_tables", default=[])
            if not source_list:
                continue
            parent_rows = table_rows.get(source_list[0], 0) * scale_factor
            avg_doc_size_kb = _safe_get(collection, "estimated_avg_doc_size_kb", default=0.0)
            total_bytes += parent_rows * avg_doc_size_kb * 1024
        return total_bytes / (1024**3)

    source_data_gb = float(
        _safe_get(collector_output, "source_database", "database_size_gb", default=0.0) or 0.0
    )
    multiplier = estimate_denorm_multiplier(schema_output)
    return source_data_gb * scale_factor * multiplier


# =============================================================================
# Constraint computation
# =============================================================================


def compute_ram_constraint(
    schema_output: dict[str, Any],
    collector_output: dict[str, Any],
    test_config: Any,
) -> tuple[float, dict[str, float]]:
    """Compute RAM needed in GB. Returns (ram_gb, breakdown)."""
    scale_factor = float(getattr(test_config, "scale_factor", 1.0))
    indexes_gb, _ = estimate_index_size_gb(schema_output, collector_output, scale_factor)
    working_set_gb, _, _ = estimate_working_set_gb(collector_output)
    floor_class = INSTANCE_CLASS_BY_NAME[FLOOR_INSTANCE_CLASS]

    ram_gb = max(
        indexes_gb * INDEX_RAM_HEADROOM,
        working_set_gb * RAM_SAFETY_FACTOR,
        float(floor_class.ram_gb),
    )
    breakdown = {
        "indexes_gb": indexes_gb,
        "working_set_gb": working_set_gb,
        "needed_gb": ram_gb,
    }
    return ram_gb, breakdown


def compute_vcpu_constraint(
    schema_output: dict[str, Any],
    collector_output: dict[str, Any],
    test_config: Any,
) -> tuple[float, dict[str, float]]:
    """Compute vCPU needed. Returns (vcpu, breakdown)."""
    access_patterns = _safe_get(schema_output, "access_patterns", default=[])
    workload_vcpu = (
        sum(
            _safe_get(p, "design_rps", default=0.0) * _cpu_factor_for(_safe_get(p, "operation"))
            for p in access_patterns
        )
        / 1000.0
    )

    source_vcpu = _safe_get(collector_output, "rds_metadata", "instance_specs", "vcpus", default=0)
    source_cpu_pct = _safe_get(collector_output, "metrics", "cpu_utilization_avg", default=0.0)
    source_observed_vcpu = source_vcpu * (source_cpu_pct / 100.0) * VCPU_HEADROOM

    floor_class = INSTANCE_CLASS_BY_NAME[FLOOR_INSTANCE_CLASS]

    vcpu = max(workload_vcpu, source_observed_vcpu, float(floor_class.vcpu))
    breakdown = {
        "workload_vcpu": workload_vcpu,
        "source_observed_vcpu": source_observed_vcpu,
        "needed_vcpu": vcpu,
    }
    return vcpu, breakdown


def compute_conn_constraint(
    collector_output: dict[str, Any],
    test_config: Any,
) -> tuple[int, dict[str, int]]:
    """Compute connections needed. Returns (conns, breakdown)."""
    source_max = _safe_get(collector_output, "metrics", "max_connections", default=0) or 0
    test_concurrency = int(getattr(test_config, "max_concurrent_vus", 0) or 0)

    conns = int(max(source_max * CONNECTION_HEADROOM, test_concurrency))
    breakdown = {
        "source_max_connections": int(source_max),
        "test_concurrency": test_concurrency,
        "needed_connections": conns,
    }
    return conns, breakdown


# =============================================================================
# Class selection
# =============================================================================


def pick_smallest_satisfying(
    ram_gb: float,
    vcpu: float,
    conn: int,
) -> tuple[InstanceClass, str]:
    """Pick smallest DocumentDB class satisfying all three constraints.

    Returns (instance_class, binding_constraint_name) where the binding
    constraint is the one with the highest pressure (closest to capacity).
    """
    for ic in DOCUMENTDB_INSTANCE_CLASSES:
        if ic.ram_gb >= ram_gb and ic.vcpu >= vcpu and ic.max_connections >= conn:
            ram_pressure = ram_gb / ic.ram_gb if ic.ram_gb else 0.0
            vcpu_pressure = vcpu / ic.vcpu if ic.vcpu else 0.0
            conn_pressure = conn / ic.max_connections if ic.max_connections else 0.0
            pressures = {
                "ram": ram_pressure,
                "vcpu": vcpu_pressure,
                "conn": conn_pressure,
            }
            binding = max(pressures, key=lambda k: pressures[k])
            return ic, binding

    # No class satisfies — return largest; ceiling cap will kick in next
    largest = DOCUMENTDB_INSTANCE_CLASSES[-1]
    return largest, "exceeds_ceiling"


def apply_cost_ceiling(instance: InstanceClass) -> InstanceClass:
    """Cap instance class at COST_CEILING_INSTANCE_CLASS."""
    ceiling_idx = INSTANCE_CLASS_ORDER[COST_CEILING_INSTANCE_CLASS]
    instance_idx = INSTANCE_CLASS_ORDER[instance.name]
    if instance_idx > ceiling_idx:
        return INSTANCE_CLASS_BY_NAME[COST_CEILING_INSTANCE_CLASS]
    return instance


def apply_floor(instance: InstanceClass) -> InstanceClass:
    """Promote instance class to FLOOR_INSTANCE_CLASS if smaller."""
    floor_idx = INSTANCE_CLASS_ORDER[FLOOR_INSTANCE_CLASS]
    instance_idx = INSTANCE_CLASS_ORDER[instance.name]
    if instance_idx < floor_idx:
        return INSTANCE_CLASS_BY_NAME[FLOOR_INSTANCE_CLASS]
    return instance


# =============================================================================
# Replica computation
# =============================================================================


def compute_replicas(
    collector_output: dict[str, Any],
) -> tuple[int, str]:
    """Compute replica count to match source HA topology.

    Returns (replica_count, reason).
    """
    source_replicas = (
        _safe_get(collector_output, "rds_metadata", "read_replica_count", default=0) or 0
    )
    source_multi_az = _safe_get(collector_output, "rds_metadata", "multi_az", default=False)

    if source_replicas > 0:
        return source_replicas, f"matches source replica count ({source_replicas})"
    if source_multi_az:
        return 1, "matches source multi-az topology"
    return 0, "source is single-instance"


# =============================================================================
# Engine version
# =============================================================================


def derive_engine_version(schema_output: dict[str, Any]) -> str:
    """Use the minimum compatible version from schema design output.

    Defaults to ``5.0.0`` if not specified.
    """
    return str(_safe_get(schema_output, "target_engine_version_min", default="5.0.0"))


# =============================================================================
# Main entry point
# =============================================================================


def derive_cluster_config(
    schema_output: dict[str, Any],
    collector_output: dict[str, Any],
    test_config: Any,
) -> ClusterConfig:
    """Derive a DocumentDB cluster config from schema design + source metrics.

    Implements the hybrid strategy from ADR-022:

    1. If complete source metrics available → derive from workload signals
    2. Else if source class is in the mapping table → use mapping
    3. Else → fallback to ``db.r6g.xlarge``

    All paths apply the cost ceiling and floor.
    """
    source_class = _safe_get(collector_output, "rds_metadata", "instance_class")
    source_ram_gb = _safe_get(collector_output, "rds_metadata", "instance_specs", "memory_gb")
    source_vcpu = _safe_get(collector_output, "rds_metadata", "instance_specs", "vcpus")

    metrics_complete = has_complete_metrics(collector_output)

    chosen: InstanceClass
    strategy: Literal["derived", "mapped", "fallback"]
    binding: str
    ram_gb: float
    vcpu: float
    conn: int

    if metrics_complete:
        ram_gb, _ = compute_ram_constraint(schema_output, collector_output, test_config)
        vcpu, _ = compute_vcpu_constraint(schema_output, collector_output, test_config)
        conn, _ = compute_conn_constraint(collector_output, test_config)
        chosen, binding = pick_smallest_satisfying(ram_gb, vcpu, conn)
        strategy = "derived"
    elif source_class and source_class in SOURCE_TO_DOCUMENTDB_BASELINE:
        chosen = INSTANCE_CLASS_BY_NAME[SOURCE_TO_DOCUMENTDB_BASELINE[source_class]]
        binding = "mapped"
        strategy = "mapped"
        ram_gb = float(chosen.ram_gb)
        vcpu = float(chosen.vcpu)
        conn = chosen.max_connections
    else:
        chosen = INSTANCE_CLASS_BY_NAME[FALLBACK_INSTANCE_CLASS]
        binding = "fallback"
        strategy = "fallback"
        ram_gb = float(chosen.ram_gb)
        vcpu = float(chosen.vcpu)
        conn = chosen.max_connections

    chosen = apply_cost_ceiling(chosen)
    chosen = apply_floor(chosen)

    replica_count, replica_reason = compute_replicas(collector_output)

    scale_factor = float(getattr(test_config, "scale_factor", 1.0))
    indexes_gb, _ = estimate_index_size_gb(schema_output, collector_output, scale_factor)
    working_set_gb, ws_method, ws_regime = estimate_working_set_gb(collector_output)
    target_data_gb = estimate_target_data_size_gb(schema_output, collector_output, scale_factor)
    denorm = estimate_denorm_multiplier(schema_output)

    chosen_reason = _build_chosen_reason(strategy, chosen, binding, ram_gb, vcpu, source_class)

    rationale = SizingRationale(
        source_instance_class=source_class,
        source_ram_gb=float(source_ram_gb) if source_ram_gb is not None else None,
        source_vcpu=int(source_vcpu) if source_vcpu is not None else None,
        source_metrics_complete=metrics_complete,
        strategy=strategy,
        working_set_gb=working_set_gb,
        working_set_method=ws_method,
        working_set_regime=ws_regime,
        indexes_size_gb=indexes_gb,
        target_data_size_gb=target_data_gb,
        denorm_multiplier=denorm,
        constraint_ram_gb=ram_gb,
        constraint_vcpu=vcpu,
        constraint_conn=conn,
        binding_constraint=binding,
        chosen_class=chosen.name,
        chosen_class_vcpu=chosen.vcpu,
        chosen_class_ram_gb=chosen.ram_gb,
        chosen_class_reason=chosen_reason,
        replicas_count=replica_count,
        replicas_reason=replica_reason,
    )

    return ClusterConfig(
        instance_class=chosen.name,
        instance_count=1 + replica_count,
        replica_count=replica_count,
        engine_version=derive_engine_version(schema_output),
        sizing_strategy=strategy,
        sizing_rationale=rationale,
    )


def _build_chosen_reason(
    strategy: str,
    chosen: InstanceClass,
    binding: str,
    ram_gb: float,
    vcpu: float,
    source_class: str | None,
) -> str:
    """Human-readable explanation of why this class was chosen."""
    if strategy == "derived":
        if binding == "ram":
            return f"RAM {chosen.ram_gb} GB satisfies workload requirement {ram_gb:.1f} GB"
        if binding == "vcpu":
            return f"vCPU {chosen.vcpu} satisfies workload requirement {vcpu:.1f}"
        if binding == "conn":
            return f"connections {chosen.max_connections:,} satisfies workload concurrency"
        if binding == "exceeds_ceiling":
            return (
                f"workload exceeds ceiling — capped at {chosen.name}; "
                "results may show resource pressure"
            )
        return "smallest class satisfying all constraints"
    if strategy == "mapped":
        return f"source class {source_class} maps to {chosen.name} via baseline table"
    return f"no metrics or known mapping; conservative fallback to {chosen.name}"
