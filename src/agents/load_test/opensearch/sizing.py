"""OpenSearch cluster sizing for load testing.

Derives cluster configuration from schema output and collector metrics.
Three workload types have distinct sizing strategies:

- Search: 25GB target shard size, r8g instances
- Log Analytics (data streams): 50GB target shard size, or2 instances
- Vector Search: 25GB target shard size, r8g instances (same as search)

Node count is always a multiple of 3 for even AZ distribution.
"""

import math

import structlog

logger = structlog.get_logger()

# Instance types by workload
SEARCH_INSTANCE_TYPE = "r8g.large.search"
LOG_ANALYTICS_INSTANCE_TYPE = "or2.large.search"

# Shard sizing targets (GB)
SEARCH_SHARD_SIZE_GB = 25
LOG_ANALYTICS_SHARD_SIZE_GB = 50

# Minimum cluster sizes
MIN_NODES = 3
MAX_NODES_LOAD_TEST = 12

# Default data volume assumptions when collector doesn't provide sizing
DEFAULT_SEARCH_DATA_GB = 10
DEFAULT_DAILY_INGEST_GB = 50


def derive_cluster_config(
    schema_output: dict,
    collector_output: dict,
    override_node_count: int | None = None,
    override_instance_type: str | None = None,
    override_data_volume_gb: float | None = None,
    override_daily_ingest_gb: float | None = None,
) -> dict:
    """Derive OpenSearch cluster configuration from schema and collector data.

    Returns a dict with: instance_type, instance_count, shard_count, ebs_volume_gb,
    workload_type, sizing_rationale.
    """
    workload_type = _classify_workload(schema_output)
    data_volume_gb = _estimate_data_volume(
        schema_output, collector_output, override_data_volume_gb, override_daily_ingest_gb
    )

    if workload_type == "log_analytics":
        shard_count = _compute_shard_count(data_volume_gb, LOG_ANALYTICS_SHARD_SIZE_GB)
        instance_type = override_instance_type or LOG_ANALYTICS_INSTANCE_TYPE
    else:
        shard_count = _compute_shard_count(data_volume_gb, SEARCH_SHARD_SIZE_GB)
        instance_type = override_instance_type or SEARCH_INSTANCE_TYPE

    node_count = _compute_node_count(shard_count, override_node_count)
    ebs_volume_gb = _compute_ebs_per_node(data_volume_gb, node_count)

    rationale = _build_rationale(
        workload_type, data_volume_gb, shard_count, node_count, instance_type, ebs_volume_gb
    )

    logger.info(
        "cluster_sizing_derived",
        workload_type=workload_type,
        data_volume_gb=data_volume_gb,
        shard_count=shard_count,
        node_count=node_count,
        instance_type=instance_type,
        ebs_volume_gb=ebs_volume_gb,
    )

    return {
        "instance_type": instance_type,
        "instance_count": node_count,
        "shard_count": shard_count,
        "ebs_volume_gb": ebs_volume_gb,
        "workload_type": workload_type,
        "data_volume_gb": data_volume_gb,
        "sizing_rationale": rationale,
    }


def _classify_workload(schema_output: dict) -> str:
    """Classify the primary workload type from schema output.

    - data_stream_designs present → log_analytics
    - otherwise → search
    """
    data_streams = schema_output.get("data_stream_designs", [])
    index_designs = schema_output.get("index_designs", [])

    if data_streams and not index_designs:
        return "log_analytics"
    if data_streams and index_designs:
        # Mixed: if data streams dominate by count, treat as log analytics
        if len(data_streams) > len(index_designs):
            return "log_analytics"
    return "search"


def _estimate_data_volume(
    schema_output: dict,
    collector_output: dict,
    override_data_volume_gb: float | None = None,
    override_daily_ingest_gb: float | None = None,
) -> float:
    """Estimate total data volume in GB for the OpenSearch cluster.

    For search: sum source table sizes from collector.
    For log analytics: use daily_ingest * retention_days from ISM policy.
    Falls back to defaults if metrics unavailable.
    """
    if override_data_volume_gb is not None:
        return override_data_volume_gb

    workload_type = _classify_workload(schema_output)

    if workload_type == "log_analytics":
        return _estimate_log_analytics_volume(schema_output, override_daily_ingest_gb)

    return _estimate_search_volume(schema_output, collector_output)


def _estimate_search_volume(schema_output: dict, collector_output: dict) -> float:
    """Estimate search index data volume from collector table sizes.

    OpenSearch indices are typically 1.5-3x larger than source due to
    inverted indices, doc values, and stored fields. Use 2x multiplier.
    """
    OPENSEARCH_EXPANSION_FACTOR = 2.0

    # Collect source table names from all index designs
    source_tables: set[str] = set()
    for idx in schema_output.get("index_designs", []):
        for table_id in idx.get("source_tables", []):
            source_tables.add(table_id)

    if not source_tables:
        return DEFAULT_SEARCH_DATA_GB

    # Look up table sizes from collector
    tables = collector_output.get("database_schema", {}).get("tables", [])
    table_sizes = {t["table_id"]: t.get("size_mb", 0) or 0 for t in tables}

    total_mb = sum(table_sizes.get(t, 0) for t in source_tables)
    if total_mb == 0:
        return DEFAULT_SEARCH_DATA_GB

    total_gb = (total_mb / 1024.0) * OPENSEARCH_EXPANSION_FACTOR
    return max(1.0, total_gb)


def _estimate_log_analytics_volume(
    schema_output: dict, override_daily_ingest_gb: float | None
) -> float:
    """Estimate log analytics volume from ISM retention + daily ingest.

    Total volume = daily_ingest_gb * hot_phase_days (only hot data is
    on the cluster for load testing purposes).
    """
    daily_ingest = override_daily_ingest_gb or DEFAULT_DAILY_INGEST_GB

    # Get retention from first data stream's ISM policy
    data_streams = schema_output.get("data_stream_designs", [])
    hot_days = 7  # default
    if data_streams:
        ism = data_streams[0].get("ism_policy", {})
        hot_days = ism.get("hot_phase_days", 7)

    return daily_ingest * hot_days


def _compute_shard_count(data_volume_gb: float, target_shard_size_gb: int) -> int:
    """Compute primary shard count from data volume and target shard size.

    Always at least 1 shard. Result is rounded up.
    """
    shards = math.ceil(data_volume_gb / target_shard_size_gb)
    return max(1, shards)


def _compute_node_count(shard_count: int, override: int | None = None) -> int:
    """Compute node count as a multiple of 3 that can hold the shards.

    Each node should hold at most 2 primary shards for balanced distribution.
    Node count is always a multiple of 3 for AZ distribution.
    """
    if override is not None:
        # Validate override is multiple of 3
        if override % 3 != 0:
            override = ((override // 3) + 1) * 3
        return max(MIN_NODES, min(override, MAX_NODES_LOAD_TEST))

    # Aim for shards to distribute evenly across nodes
    # Each node gets at most ceil(shard_count / node_count) shards
    min_nodes_for_shards = math.ceil(shard_count / 2)
    # Round up to multiple of 3
    node_count = max(MIN_NODES, ((min_nodes_for_shards + 2) // 3) * 3)
    return min(node_count, MAX_NODES_LOAD_TEST)


def _compute_ebs_per_node(data_volume_gb: float, node_count: int) -> int:
    """Compute EBS volume size per node in GB.

    Account for 1 replica (2x data), OS overhead (~15%), and headroom (~20%).
    Minimum 20GB, maximum 500GB per node for load testing.
    """
    replica_factor = 2.0
    overhead_factor = 1.35  # 15% OS overhead + 20% headroom
    total_storage = data_volume_gb * replica_factor * overhead_factor
    per_node = math.ceil(total_storage / node_count)
    return max(20, min(per_node, 500))


def _build_rationale(
    workload_type: str,
    data_volume_gb: float,
    shard_count: int,
    node_count: int,
    instance_type: str,
    ebs_volume_gb: int,
) -> dict:
    """Build human-readable sizing rationale for audit trail."""
    target_shard_size = (
        LOG_ANALYTICS_SHARD_SIZE_GB if workload_type == "log_analytics" else SEARCH_SHARD_SIZE_GB
    )
    return {
        "workload_type": workload_type,
        "strategy": (
            f"{data_volume_gb:.1f}GB data / {target_shard_size}GB target shard = "
            f"{shard_count} primary shards"
        ),
        "node_distribution": (
            f"{shard_count} shards across {node_count} nodes "
            f"({shard_count / node_count:.1f} shards/node), 3-AZ aligned"
        ),
        "instance_selection": (
            f"{instance_type} — "
            f"{'or2 for log analytics (cost-optimized storage)' if 'or2' in instance_type else 'r8g for search (memory-optimized)'}"
        ),
        "storage_per_node": (
            f"{ebs_volume_gb}GB EBS/node " f"(accounts for 1 replica + 35% overhead)"
        ),
    }
