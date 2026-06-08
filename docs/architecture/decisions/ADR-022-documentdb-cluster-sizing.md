# ADR-022: DocumentDB Cluster Sizing for Load Tests

**Status:** Proposed
**Date:** 2026-06-08
**Deciders:** Database Modernizer Assessment Architecture Team

## Context

The load testing stage's primary value proposition is empirical latency comparison between source and target databases. For DynamoDB targets, sizing is a non-decision — DynamoDB is on-demand and zero-baseline, so a single `OnDemand` table provisioning is the only sizing call required.

DocumentDB is fundamentally different. It is a **provisioned-instance** database with significant baseline costs (`db.r6g.large` ≈ $0.49/hr minimum) and three independent sizing dimensions:

- **RAM** — DocumentDB is memory-bound; indexes must fit in RAM, working set should fit in RAM
- **vCPU** — query complexity (aggregations, `$lookup`, sorts) is CPU-heavy
- **Connections** — capped per instance; rarely binding but worth checking

Wrong sizing invalidates the load test result and breaks the comparison's value:

- **Undersized cluster** → latency artificially worse than source → "DocumentDB is slow!" (false conclusion, customer rejects DocumentDB based on bad data)
- **Oversized cluster** → latency artificially better than source → "DocumentDB is faster!" (also misleading; customer overpays in production)

ADR-020 (Load Testing Stage Architecture) lists DocumentDB as a future engine but defers the sizing approach. This ADR codifies sizing as testable Python heuristics with auditable rationale emitted alongside test results.

The sizing strategy must:

1. Be deterministic and reproducible — same inputs always produce the same instance class
2. Adapt to actual workload characteristics — memory-bound vs CPU-bound vs connection-bound
3. Fall back gracefully when source metrics are incomplete (Performance Insights gaps, Enhanced Monitoring disabled)
4. Cap costs to prevent runaway provisioning ($100/test would be unacceptable for a v1 feature)
5. Be auditable — customer receives the sizing rationale alongside the empirical result so they can defend the comparison internally

## Decision

### Hybrid Sizing Strategy: Derived (Primary) + Class-Mapped (Fallback)

When source metrics are complete, **derive** the cluster size from the workload profile (working set, index footprint, query complexity, connection demand). When metrics are incomplete, fall back to a **class-mapping** table from source instance class to recommended DocumentDB class.

**Why this hybrid over alternatives:**

| Alternative | Rejection reason |
| --- | --- |
| Always use derived sizing | Fails silently when source metrics are missing; no defensive fallback |
| Always use mapped sizing | Doesn't adapt to workload; over-provisions CPU-light workloads, under-provisions memory-heavy ones on the same instance class |
| Customer-supplied size only | Defeats automation; customer doesn't know DocumentDB sizing rules |
| Always provision largest viable | Costs $5-10/test minimum; wastes resources for small workloads |

**Why hybrid wins:**

- Derived sizing extracts maximum signal from collector output (`BufferCacheHitRatio`, observed CPU%, max connections)
- Mapped sizing provides a defensible default when metrics are incomplete (commonly the first deployment of the modernizer in a customer environment, where Performance Insights may not yet be enabled)
- The strategy used is recorded in the rationale artifact, so customers see whether their sizing was data-driven or rule-based

### Three Sizing Constraints

The chosen instance class must satisfy all three constraints simultaneously. The smallest class meeting all three is selected.

```python
chosen_class = pick_smallest_satisfying(ram_needed, vcpu_needed, conn_needed)
```

#### RAM (the dominant constraint)

DocumentDB is aggressively memory-dependent. Two hard rules:

1. **All indexes must fit in RAM.** If they spill, every query takes a disk hit and latency cliffs.
2. **Working set should fit in RAM.** Frequently accessed documents stay cached.

```python
ram_needed_gb = max(
    indexes_size_gb * INDEX_RAM_HEADROOM,    # = 1.5; index cache pressure
    working_set_gb * RAM_SAFETY_FACTOR,      # = 1.5; query buffers, sorts
    minimum_for_class                        # 16 GB at r6g.large floor
)
```

**Working set estimation** uses the source's `BufferCacheHitRatio` to infer how much of the data is hot. Three regimes:

```python
# Hit ratio >= 95% → working set fits comfortably in source buffer pool
#                    Buffer pool default is ~75% of instance RAM
working_set_gb = min(source_ram_gb * 0.75, total_data_gb)

# 80% <= Hit ratio < 95% → some thrashing, working set slightly exceeds RAM
miss_rate = (100 - hit_ratio) / 100
working_set_gb = min(
    source_ram_gb * (1 + miss_rate * 3.0),
    total_data_gb * 0.5
)

# Hit ratio < 80% → severe thrashing; source is undersized
#                   Cannot infer working set from source RAM (it's wrong)
working_set_gb = total_data_gb * 0.5

# Metric unavailable → fixed-fraction heuristic
working_set_gb = total_data_gb * 0.20
```

**Index size estimation uses schema_output, not source cardinality.** Schema design changes cardinality fundamentally — embedding reduces collection count, denormalization duplicates data, and index field selection differs from source. Computing from source row counts produces wrong sizing.

```python
for collection in schema_output.collections:
    parent_table = collection.source_tables[0]
    doc_count = source_table_rows(parent_table) * scale_factor

    for index in collection.indexes:
        # MongoDB B-tree: ~40 byte overhead + sum of indexed field sizes
        entry_size = 40 + sum(field_size_bytes(f) for f in index.keys)
        total_index_bytes += entry_size * doc_count
```

Field sizes use a conservative defaults table (ObjectId=12, int=4, string≈50, etc.) keyed by inferred type from collection metadata.

#### vCPU (workload-dependent)

DocumentDB's vCPU need depends on operation mix. Different operations consume different amounts of CPU per request:

| Operation | vCPU per 1,000 RPS |
| --- | --- |
| `findOne` by `_id` | 0.3 |
| `find` simple filter | 0.5 |
| `find` with sort | 1.0 |
| `aggregate` simple pipeline | 1.5 |
| `aggregate` with `$lookup` | 2.5 |
| Write operations | 0.4-0.6 |

```python
vcpu_needed = max(
    sum(p.design_rps * cpu_factor[p.operation] for p in patterns) / 1000,
    source_vcpu * source_cpu_utilization * VCPU_HEADROOM,    # = 1.3
    minimum_for_class
)
```

The second term ensures DocumentDB has at least source's observed CPU plus 30% headroom — apples-to-apples capacity for the same workload.

#### Connections

Each DocumentDB instance class has a connection limit:

| Instance | Max Connections |
| --- | --- |
| `db.r6g.large` | 2,000 |
| `db.r6g.xlarge` | 4,000 |
| `db.r6g.2xlarge` | 8,000 |
| `db.r6g.4xlarge` | 16,000 |
| `db.r6g.8xlarge` | 32,000 |
| `db.r6g.12xlarge`+ | 64,000 |

```python
conn_needed = max(
    source_max_connections * 1.5,           # source observed × headroom
    sum(scenario.maxVUs for scenario in scenarios)  # k6 peak concurrency
)
```

In practice this is rarely the binding constraint. RAM dominates first.

### Strategy Selection

```python
def derive_cluster_config(schema_output, collector_output, test_config):
    if has_complete_metrics(collector_output):
        # Compute all three constraints from observed data
        ram = compute_ram_constraint(schema_output, collector_output, test_config)
        vcpu = compute_vcpu_constraint(schema_output, collector_output, test_config)
        conn = compute_conn_constraint(collector_output, test_config)
        instance_class = pick_smallest_satisfying(ram, vcpu, conn)
        strategy = "derived"
    elif source_class in SOURCE_TO_DOCUMENTDB_BASELINE:
        # Use mapping table
        instance_class = SOURCE_TO_DOCUMENTDB_BASELINE[source_class]
        strategy = "mapped"
    else:
        # Last resort
        instance_class = "db.r6g.xlarge"
        strategy = "fallback"

    instance_class = apply_cost_ceiling(instance_class)
    instance_class = apply_floor(instance_class)

    return ClusterConfig(
        instance_class=instance_class,
        replica_count=compute_replicas(collector_output),
        engine_version=schema_output.target_engine_version_min,
        sizing_strategy=strategy,
        sizing_rationale=build_rationale(...),
    )
```

**`has_complete_metrics()`** requires:

- `cache_hit_ratio_pct` (CW for MySQL, `pg_stat_database` for PostgreSQL)
- `cpu_utilization_avg` (CW)
- `max_connections` (CW)
- `database_size_gb` (collector)
- `instance_specs.memory_gb` and `vcpus` (RDS API)

If any of these is missing, fall back to mapped strategy.

### Source Class Mapping Table

When derived sizing is unavailable, map source instance class to a DocumentDB baseline. Memory-optimized sources map to equivalent r6g classes; general-purpose and burstable sources promote to memory-optimized one tier down.

```python
SOURCE_TO_DOCUMENTDB_BASELINE = {
    # Memory-optimized (r-series) — match RAM 1:1
    "db.r6i.large":    "db.r6g.large",
    "db.r6i.xlarge":   "db.r6g.xlarge",
    "db.r6i.2xlarge":  "db.r6g.2xlarge",
    "db.r6i.4xlarge":  "db.r6g.4xlarge",
    "db.r6i.8xlarge":  "db.r6g.8xlarge",
    # ... r5, r6g, r7g equivalents

    # General-purpose (m-series) — promote to memory-optimized
    "db.m6i.large":    "db.r6g.large",
    "db.m6i.xlarge":   "db.r6g.xlarge",
    "db.m6i.2xlarge":  "db.r6g.xlarge",      # downsize: m has more vCPU than RAM
    "db.m6i.4xlarge":  "db.r6g.2xlarge",
    # ... m5, m6g, m7g equivalents

    # Burstable (t-series) — minimum production tier
    "db.t3.small":     "db.r6g.large",
    "db.t3.medium":    "db.r6g.large",
    "db.t3.large":     "db.r6g.large",
    "db.t3.xlarge":    "db.r6g.xlarge",
    "db.t3.2xlarge":   "db.r6g.xlarge",
}
```

Full mapping covers ~30 source instance classes. Unknown classes fall through to the `fallback` strategy with `db.r6g.xlarge` (32 GB / 4 vCPU) — chosen over `db.r6g.large` because it provides realistic latency on most "small but real" workloads with only marginal cost increase.

### Cost Ceiling and Floor

| Class | vCPU | RAM | Hourly | 30-min single-instance cost |
| --- | --- | --- | --- | --- |
| `db.r6g.large` (floor) | 2 | 16 GB | $0.245 | $0.12 |
| `db.r6g.xlarge` (fallback default) | 4 | 32 GB | $0.490 | $0.25 |
| `db.r6g.2xlarge` | 8 | 64 GB | $0.980 | $0.49 |
| `db.r6g.4xlarge` | 16 | 128 GB | $1.960 | $0.98 |
| **`db.r6g.8xlarge` (ceiling)** | **32** | **256 GB** | **$3.920** | **$1.96** |
| `db.r6g.12xlarge` | 48 | 384 GB | $5.880 | $2.94 — requires explicit override (deferred to v2) |
| `db.r6g.16xlarge` | 64 | 512 GB | $7.840 | $3.92 — requires explicit override (deferred to v2) |

**Floor at `db.r6g.large`** prevents undersized provisioning that would invalidate latency comparison.

**Ceiling at `db.r6g.8xlarge`** caps a single-instance test at ~$2 (or ~$4 with one replica). Workloads with working sets larger than ~170 GB exceed this ceiling. v1 does not support overrides — those workloads will receive sized-at-ceiling clusters with a warning in the rationale ("source workload exceeds default ceiling; consider scale_factor < 1.0 for full-data accuracy").

The override field (`cluster_size_override` on `TestConfig`) is intentionally not added in v1 — it would be premature flexibility per YAGNI. The contract grows in response to a concrete customer ask, not in anticipation.

### Replica Count

Replicas are sized to match source HA topology and read fan-out:

```python
replicas_needed = max(
    source_replica_count,                        # match source HA topology
    1 if source_multi_az else 0,                 # minimum HA realism
    ceil(read_rps / per_instance_capacity) - 1   # read scale-out
)
```

For load test latency comparison, **read routing must match source**:

- Source had read replicas → DocumentDB reads use `read_preference = SECONDARY_PREFERRED`
- Source was single-instance → DocumentDB reads use `read_preference = PRIMARY`

This ensures apples-to-apples comparison. Replica reads on DocumentDB have eventual consistency and slightly different latency profile than primary reads.

### Engine Version Selection

DocumentDB has multiple compatibility versions (4.0, 5.0, latest). The engine version is determined by **schema design**, not by the load test stage:

```python
engine_version = schema_output.target_engine_version_min
```

The DocumentDB analysis and schema design agents emit a `documentdb_compatibility` section that tracks which features the customer's workload uses (text indexes, partial indexes, views, etc.) and computes the minimum required version. The load test provisions exactly that version.

This matters for two reasons:

1. **Realism** — customer may run an older version in production for compliance or compatibility reasons. Testing against the latest version produces irrelevant latency numbers.
2. **Feature availability** — DocumentDB 4.0 lacks operators present in 5.0+ (window functions, etc.). Schema design has already filtered out unsupported operations; the load test should not retry them.

### Edge Cases

**Working set clamp:** the formula `source_ram_gb * 0.75` could over-provision when source is over-provisioned (e.g., 512 GB RAM with 10 GB data and 99.99% hit ratio). The `min(source_ram_gb * 0.75, total_data_gb)` clamp prevents this — working set can never exceed total data.

**Denormalization multiplier:** schema design's embedding strategy inflates target data size compared to source. A 1 GB MySQL with 5 normalized tables may become 1.4 GB DocumentDB after embedding (40% growth from duplication). When schema design output provides per-collection `estimated_avg_doc_size_kb`, sizing uses that directly. When unavailable, a heuristic `1.0 + (embed_ratio * 0.2)` applies, capped at 1.3x.

**Empty schema or zero patterns:** if schema_output has no collections or test has no access patterns, return `db.r6g.large` (floor) with `strategy = "fallback"` and `binding_constraint = "minimum"` in the rationale.

**Severely undersized source:** if hit ratio < 80%, the formula falls back to `total_data_gb * 0.5` rather than trusting source RAM. The rationale explicitly notes "source appears undersized" for customer awareness.

### Customer Transparency: Sizing Rationale Artifact

Every load test run emits `load-test/v{N}/sizing_rationale.json` alongside `infrastructure.json`. This is plain JSON — no Pydantic model, no contract surface — designed for direct customer consumption.

```json
{
  "source_instance_class": "db.r6i.4xlarge",
  "source_ram_gb": 128,
  "source_vcpu": 16,
  "source_metrics_complete": true,
  "strategy": "derived",
  "working_set": {
    "estimated_gb": 48.2,
    "method": "buffer_cache_ratio",
    "hit_ratio_pct": 99.86,
    "regime": "high_hit_ratio"
  },
  "indexes": {
    "estimated_size_gb": 4.1,
    "collections": 12,
    "total_indexes": 38
  },
  "data": {
    "source_size_gb": 42.0,
    "target_size_gb": 47.6,
    "denorm_multiplier": 1.13
  },
  "constraints": {
    "ram_needed_gb": 78.4,
    "vcpu_needed": 6.2,
    "conn_needed": 750
  },
  "binding_constraint": "ram",
  "chosen": {
    "instance_class": "db.r6g.2xlarge",
    "vcpu": 8,
    "ram_gb": 64,
    "max_connections": 8000,
    "rationale": "RAM 64 GB satisfies indexes (4.1 GB × 1.5 = 6.2 GB) + working set (48.2 GB × 1.5 = 72.3 GB)"
  },
  "replicas": {
    "count": 1,
    "reason": "matches source multi-az topology"
  },
  "cost": {
    "hourly_per_instance_usd": 0.98,
    "test_duration_minutes": 30,
    "estimated_cluster_cost_usd": 0.98
  }
}
```

Customers receive this alongside their latency report. When they read "DocumentDB P95 8 ms vs MySQL P95 4 ms", they have the evidence to defend the comparison: the cluster was sized to match their workload's working set with explicit headroom.

### Failure Modes

| Failure | Detection | Behavior |
| --- | --- | --- |
| Schema output missing `collections` | Validation in `derive_cluster_config()` | Return floor class, rationale notes "no collections in schema_output" |
| All sizing constraints zero | Validation | Return floor class, rationale notes "workload signals all zero" |
| Source class unknown to mapping AND no metrics | Strategy selection | Apply `fallback` to `db.r6g.xlarge`, rationale notes "no metrics, unknown source class" |
| Constraints exceed ceiling | After `pick_smallest_satisfying` | Cap at ceiling, rationale notes "workload exceeds ceiling; latency results may show resource pressure" |

In all cases the load test continues. Sizing degrades gracefully — it does not abort the run.

## Consequences

### Positive

- Right-sized DocumentDB clusters → meaningful latency comparison vs source
- Cost capped at `db.r6g.8xlarge` → no runaway provisioning surprises
- Sizing rationale is auditable customer artifact — defensible comparison evidence
- Graceful degradation when source metrics are incomplete
- Pure-function sizing module is fully unit-testable independent of AWS
- Pattern transfers to other provisioned-instance engines (Aurora, Neptune, Keyspaces) when their load test engines are added

### Negative

- Heuristic-based; sizing is approximate, not optimal
- Adds significant complexity vs DynamoDB's "just provision a table"
- Edge cases (over-provisioned source, severe denormalization, all-aggregate workloads) may produce surprising sizing that requires human judgment
- Source class mapping table requires maintenance as AWS ships new instance types

### Risks

- **Mapping table staleness** — new RDS instance classes (r7g, r8g) ship periodically. Mitigation: derived strategy is preferred path; mapping is fallback. Mapping additions are low-risk PRs.
- **Source metrics inaccuracy** — Performance Insights gaps or Enhanced Monitoring outages can produce wrong sizing. Mitigation: rationale exposes confidence level; "incomplete metrics" cases use mapping fallback.
- **Workload exceeds ceiling** — large customer workloads may legitimately need clusters above `db.r6g.8xlarge`. Mitigation: v1 documents the limit; v2 introduces `cluster_size_override` field if customers ask.
- **Customer disagreement** — customer may believe their production cluster is sized differently and reject our recommendation. Mitigation: rationale shows the math; customer can dispute specific assumptions (working set fraction, headroom factors). v2 adds tunables if patterns emerge.

## References

- [ADR-020: Load Testing Stage Architecture](ADR-020-load-testing-stage.md) — k6 on ECS coordinator, engine extensibility model
- [ADR-019: Query Journey Materialization](ADR-019-query-journey-materialization.md) — sizing rationale follows the progressive enrichment pattern
- DocumentDB pricing: <https://aws.amazon.com/documentdb/pricing/>
- DocumentDB instance classes: <https://docs.aws.amazon.com/documentdb/latest/developerguide/db-instance-classes.html>
- DocumentDB best practices (memory and indexing): <https://docs.aws.amazon.com/documentdb/latest/developerguide/best_practices.html>
- Reference implementation: `src/agents/load_test/documentdb/sizing.py`
- Implementation guide: `docs/guides/load-testing-new-engine.md`
