"""
Design Metrics Contract — numbers the pipeline computes for the schema designers.

Before #626 the schema-design skills asked the model to derive these figures
itself (``design_rps``, row and document sizes, OpenSearch shard counts, the
query coverage ratio, the DynamoDB hot-partition utilization). The pipeline now
computes them in ``src/tools/schema/design_metrics.py`` and passes them to the
designer as ``design_metrics`` next to the projected input, in Bedrock and
external (Claude Code) mode alike. The skills read the provided numbers, and
``check_design_numbers`` rejects a design whose numbers disagree with them.

Version History:
- 1.0 (2026-10-13): Initial version
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

#: Default AgentContextInput multipliers (``design_rps = base_rps × peak × growth``).
DEFAULT_GROWTH_MULTIPLIER = 10.0
DEFAULT_PEAK_TO_AVG_RATIO = 3.0


class QueryDesignMetrics(BaseModel):
    """Request rates for one source query."""

    query_id: str = Field(..., description="Collector query_id")
    base_rps: float = Field(
        ..., ge=0, description="Measured rate: calls_per_second, else frequency_per_hour / 3600"
    )
    design_rps: float = Field(
        ..., ge=0, description="base_rps × peak_to_avg_ratio × growth_multiplier, 4 decimals"
    )

    model_config = ConfigDict(extra="forbid")


class TableDesignMetrics(BaseModel):
    """Sizes for one source table."""

    table_id: str = Field(..., description="Collector table_id")
    row_count: int = Field(..., ge=0, description="Collector row_count")
    avg_row_size_bytes: int = Field(
        ..., ge=0, description="Measured: size_mb × 1,048,576 / max(row_count, 1)"
    )
    avg_row_size_kb: float = Field(..., ge=0, description="avg_row_size_bytes / 1024, 3 decimals")
    size_estimated: bool = Field(
        ...,
        description="True when size_mb is unknown and 0.01 MB stood in for it (approximate)",
    )
    schema_item_size_bytes: int = Field(
        ...,
        ge=0,
        description=(
            "Item size from the column types: per column len(column_name) + value size "
            "(string-like: max_length capped at 1024, or 50; numeric: 8; boolean: 1)"
        ),
    )
    data_gb: float = Field(..., ge=0, description="size_mb / 1024, 4 decimals (0 when unknown)")
    capped_text_columns: list[str] = Field(
        default_factory=list,
        description=(
            "String-like columns whose max_length exceeds 1024 (TEXT/BLOB) and were counted at "
            "1024 bytes in schema_item_size_bytes: a stored value can still approach DynamoDB's "
            "400 KB item limit"
        ),
    )

    model_config = ConfigDict(extra="forbid")


class CoverageMetrics(BaseModel):
    """How many of the analysed queries the projected input carries."""

    in_scope_patterns: int = Field(..., ge=0, description="Query patterns whose type is not OTHER")
    total_queries_analyzed: int = Field(
        ..., ge=1, description="The collector's whole-database count of analysed queries"
    )
    coverage_ratio: float = Field(
        ..., ge=0, description="in_scope_patterns / total_queries_analyzed, 4 decimals"
    )

    model_config = ConfigDict(extra="forbid")


class DynamoDBPartitionLimits(BaseModel):
    """Per-partition-key-value throughput limits for the hot-partition check."""

    read_units_per_second: float = Field(
        3000.0, description="Read capacity units per second per partition key value"
    )
    write_units_per_second: float = Field(
        1000.0, description="Write capacity units per second per partition key value"
    )
    at_risk_threshold_pct: float = Field(
        80.0, description="at_risk when utilization_pct is above this"
    )

    model_config = ConfigDict(extra="forbid")


class EmbeddingSizeMetrics(BaseModel):
    """Document-size figures for embedding one child table in its FK parent."""

    parent_table: str = Field(..., description="table_id the foreign key references")
    child_table: str = Field(..., description="table_id holding the foreign key")
    avg_children_per_parent: float = Field(
        ..., ge=0, description="child row_count / max(parent row_count, 1), 2 decimals"
    )
    parent_row_size_kb: float = Field(..., ge=0, description="Parent avg_row_size_kb")
    child_row_size_kb: float = Field(..., ge=0, description="Child avg_row_size_kb")
    embedded_avg_kb: float = Field(
        ..., ge=0, description="child_row_size_kb × avg_children_per_parent, 3 decimals"
    )
    doc_avg_kb_if_embedded: float = Field(
        ..., ge=0, description="parent_row_size_kb + embedded_avg_kb (this child only)"
    )

    model_config = ConfigDict(extra="forbid")


class OpenSearchShardMetrics(BaseModel):
    """Primary shard counts for one source table indexed on its own."""

    table_id: str = Field(..., description="Collector table_id")
    data_gb: float = Field(..., ge=0, description="The table's data_gb")
    search_shards: int = Field(..., ge=1, description="As a search index (≤ 30 GB per shard)")
    timeseries_shards: int = Field(
        ..., ge=1, description="As a time-series data stream (≤ 50 GB per shard)"
    )

    model_config = ConfigDict(extra="forbid")


class OpenSearchShardRules(BaseModel):
    """The shard-count rule ``search_shards`` / ``timeseries_shards`` follow."""

    assumed_node_count: int = Field(3, ge=1, description="Data nodes the shards spread over")
    search_max_shard_gb: float = Field(30.0, description="Largest primary shard, search index")
    timeseries_max_shard_gb: float = Field(
        50.0, description="Largest primary shard, time-series data stream"
    )
    rule: str = Field(
        "1 shard when data_gb <= max_shard_gb; else ceil(data_gb / max_shard_gb) "
        "rounded up to a multiple of assumed_node_count",
        description="The shard-count rule in words",
    )
    tables: list[OpenSearchShardMetrics] = Field(
        default_factory=list, description="Shard counts per source table"
    )

    model_config = ConfigDict(extra="forbid")


class DesignMetricsContract(BaseModel):
    """Every number the schema designer reads instead of computing (#626)."""

    contract_version: str = Field(
        default="1.0", pattern=r"^\d+\.\d+$", description="Contract version (MAJOR.MINOR)"
    )
    engine: str = Field(..., description="Target engine the metrics are for")
    growth_multiplier: float = Field(
        DEFAULT_GROWTH_MULTIPLIER, gt=0, description="AgentContextInput growth_multiplier"
    )
    peak_to_avg_ratio: float = Field(
        DEFAULT_PEAK_TO_AVG_RATIO, gt=0, description="AgentContextInput peak_to_avg_ratio"
    )
    queries: list[QueryDesignMetrics] = Field(
        default_factory=list, description="Per query, sorted by query_id"
    )
    tables: list[TableDesignMetrics] = Field(
        default_factory=list, description="Per source table, sorted by table_id"
    )
    coverage: CoverageMetrics | None = Field(
        None,
        description="None when the input has no total_queries_analyzed (a design group)",
    )
    dynamodb_partition_limits: DynamoDBPartitionLimits | None = Field(
        None, description="DynamoDB only"
    )
    documentdb_embeddings: list[EmbeddingSizeMetrics] | None = Field(
        None, description="DocumentDB only: one per FK parent/child pair in the input"
    )
    opensearch_shards: OpenSearchShardRules | None = Field(None, description="OpenSearch only")

    model_config = ConfigDict(extra="forbid")
