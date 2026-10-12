"""OpenSearch schema design output contract.

Defines the structured output that the OpenSearch schema design agent
produces — index mappings (search workload) and data stream configurations
with ISM policies (time-series workload), plus access pattern translations.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .schema_design_output import TradeOff

# ---------------------------------------------------------------------------
# opensearch_dsl validation
#
# ``inner_hits`` only makes sense nested inside a ``nested``/``has_child``/
# ``has_parent`` query clause, or inside ``collapse``. A top-level
# ``inner_hits`` key in the search body is rejected by OpenSearch with an
# HTTP 400 ``parsing_exception`` ("Unknown key for a START_OBJECT in
# [inner_hits]") -- see issue #95. Since the LLM designer generates this DSL
# freehand, this is enforced as a contract-level gate rather than left to
# hope the prompt gets it right every time.
#
# ``opensearch_dsl`` is not always a single JSON object: ``get_by_id`` is a
# literal request line (``GET /<index>/_doc/<id>``) and ``bulk_index`` can be
# NDJSON (one JSON document per line, no enclosing object). Neither parses as
# a single JSON value, so this check only fires when the string *does* parse
# to a dict -- anything else is left alone rather than rejected.
# ---------------------------------------------------------------------------


class OpenSearchDSLError(ValueError):
    """Raised when generated OpenSearch query DSL is structurally invalid."""


def validate_opensearch_dsl(dsl: str) -> None:
    """Raise :class:`OpenSearchDSLError` when ``dsl`` has a top-level ``inner_hits`` key.

    ``dsl`` is only checked when it parses as a single JSON object (the
    ``search``/``aggregate`` shape). Forms that are not a single JSON object --
    ``get_by_id``'s request line, or ``bulk_index``'s NDJSON -- are not valid
    JSON on their own and are passed through untouched; there is nothing
    structural to check in those shapes. When ``dsl`` does parse to a dict,
    its own top-level keys must not include ``inner_hits``: ``inner_hits``
    nested under ``query`` (inside a ``nested``/``has_child``/``has_parent``
    clause) or under ``collapse`` is untouched -- only a bare top-level
    ``inner_hits`` key is rejected, since that is the shape OpenSearch always
    refuses.
    """
    try:
        parsed = json.loads(dsl)
    except (json.JSONDecodeError, TypeError):
        return

    if isinstance(parsed, dict) and "inner_hits" in parsed:
        raise OpenSearchDSLError(
            "opensearch_dsl has a top-level 'inner_hits' key, which OpenSearch "
            "rejects with HTTP 400 (parsing_exception: Unknown key for a "
            "START_OBJECT in [inner_hits]). inner_hits is only valid nested "
            "inside a 'nested', 'has_child', or 'has_parent' query clause, or "
            "inside 'collapse'. If the nested array is already returned via "
            "'_source', drop inner_hits entirely instead of leaving it at the "
            "top level."
        )


# ---------------------------------------------------------------------------
# Custom analyzers and settings
# ---------------------------------------------------------------------------


class CustomAnalyzer(BaseModel):
    """Custom analyzer definition for the settings.analysis block."""

    name: str = Field(..., description="Analyzer name, e.g. 'custom_autocomplete'")
    tokenizer: str = Field(..., description="Tokenizer name, e.g. 'standard', 'edge_ngram'")
    filter: list[str] = Field(
        default_factory=list, description="Token filter chain, e.g. ['lowercase', 'stop']"
    )
    char_filter: list[str] = Field(
        default_factory=list, description="Character filters, e.g. ['html_strip']"
    )

    model_config = ConfigDict(extra="ignore")


class IndexSettings(BaseModel):
    """Settings block for an OpenSearch index or index template."""

    number_of_shards: int = Field(..., ge=1, description="Primary shard count")
    number_of_replicas: int = Field(default=1, ge=0, description="Replica shard count")
    refresh_interval: str = Field(
        default="1s",
        description="Index refresh interval; use '30s' for time-series to reduce overhead",
    )
    assumed_node_count: int = Field(
        ..., ge=1, description="Cluster node count used for shard distribution calculation"
    )
    shard_sizing_rationale: str = Field(..., description="Explanation of the shard count math")
    custom_analyzers: list[CustomAnalyzer] = Field(
        default_factory=list,
        description="Custom analyzer definitions for settings.analysis block",
    )

    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------------------
# Field mapping
# ---------------------------------------------------------------------------


class FieldMapping(BaseModel):
    """A single field in an OpenSearch index mapping."""

    field_name: str = Field(..., description="OpenSearch field name, e.g. '@timestamp'")
    field_type: str = Field(
        ..., description="OpenSearch field type: text, keyword, integer, long, date, nested, etc."
    )
    source_column: str = Field(..., description="Source relational column this field comes from")
    analyzer: str | None = Field(
        default=None, description="Index-time analyzer, e.g. 'standard', 'english'"
    )
    search_analyzer: str | None = Field(
        default=None, description="Query-time analyzer if different from index analyzer"
    )
    multi_field: bool = Field(
        default=False,
        description="True when field has both text (full-text) and keyword (agg/sort) subfield",
    )
    doc_values: bool = Field(
        default=True,
        description="Enable doc_values; disable for text fields not used in aggregations",
    )
    index: bool = Field(default=True, description="Enable indexing; False for stored-only fields")

    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------------------
# Search workload: IndexMapping
# ---------------------------------------------------------------------------


class IndexMapping(BaseModel):
    """Index design for a search-workload table."""

    index_name: str = Field(..., description="OpenSearch index name, e.g. 'products'")
    source_tables: list[str] = Field(
        ..., description="Relational tables this index covers, e.g. ['public.products']"
    )
    settings: IndexSettings = Field(
        ..., description="Index settings including shards and analyzers"
    )
    field_mappings: list[FieldMapping] = Field(
        default_factory=list, description="Field mapping definitions"
    )
    aliases: list[str] = Field(
        default_factory=list, description="Index aliases for zero-downtime reindexing"
    )

    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------------------
# Time-series workload: ISMPolicy, IndexTemplate, DataStreamConfig
# ---------------------------------------------------------------------------


class ISMPolicy(BaseModel):
    """ISM (Index State Management) lifecycle policy for a data stream."""

    policy_name: str = Field(..., description="ISM policy name")
    hot_phase_days: int = Field(..., ge=1, description="Days to keep data in hot storage")
    warm_phase_days: int | None = Field(
        default=None, ge=1, description="Days before moving to UltraWarm (optional)"
    )
    cold_phase_days: int | None = Field(
        default=None, ge=1, description="Days before moving to cold storage (optional)"
    )
    delete_after_days: int | None = Field(
        default=None, ge=1, description="Days after which the index is deleted"
    )
    rollover_size_gb: int = Field(
        default=50, ge=1, description="Rollover index when it reaches this size in GB"
    )
    rollover_age_hours: int = Field(
        default=24, ge=1, description="Rollover index after this many hours"
    )

    model_config = ConfigDict(extra="ignore")


class IndexTemplate(BaseModel):
    """Index template used by a data stream."""

    template_name: str = Field(..., description="Template name, e.g. 'application-logs-template'")
    index_patterns: list[str] = Field(
        ..., description="Index patterns this template applies to, e.g. ['application-logs-*']"
    )
    settings: IndexSettings = Field(..., description="Index settings for all backing indices")
    field_mappings: list[FieldMapping] = Field(
        default_factory=list, description="Field mapping definitions applied to each backing index"
    )

    model_config = ConfigDict(extra="ignore")


class DataStreamConfig(BaseModel):
    """Data stream configuration for a time-series-workload table."""

    data_stream_name: str = Field(..., description="Data stream name, e.g. 'application-logs'")
    source_tables: list[str] = Field(
        ..., description="Source relational tables this data stream covers"
    )
    timestamp_field: str = Field(
        ..., description="Source column mapped to @timestamp in OpenSearch"
    )
    index_template: IndexTemplate = Field(..., description="Index template for backing indices")
    ism_policy: ISMPolicy = Field(..., description="Lifecycle policy for data retention")

    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------------------
# Access patterns
# ---------------------------------------------------------------------------


class AccessPattern(BaseModel):
    """A source SQL query translated to an OpenSearch DSL equivalent."""

    pattern_id: str = Field(
        ...,
        description="Stable identifier for this access pattern, prefixed with engine shorthand, e.g. 'OS-AP-1', 'OS-AP-2'",
    )
    name: str = Field(..., description="Human-readable name, e.g. 'Search products by description'")
    description: str = Field(..., description="Plain-English description of what this pattern does")
    query_ids: list[str] = Field(
        ..., min_length=1, description="Source query IDs from collector that map to this pattern"
    )
    source_tables: list[str] = Field(
        ..., description="Source table_ids accessed by the original queries"
    )
    source_query: str = Field(..., description="Original SQL query")
    opensearch_dsl: str = Field(
        ...,
        description=(
            "Equivalent OpenSearch query DSL as a JSON string (or, for get_by_id/"
            "bulk_index, the literal request line/NDJSON). Never a top-level "
            "inner_hits key -- inner_hits is only valid nested inside a nested/"
            "has_child/has_parent query clause, or inside collapse."
        ),
    )
    index_or_stream: str = Field(
        ..., description="Index or data stream name that serves this pattern"
    )
    operation: str = Field(
        ..., description="Operation type: search, aggregate, get_by_id, bulk_index"
    )
    design_rps: float = Field(
        ..., ge=0, description="calls_per_second * peak_to_avg_ratio * growth_multiplier"
    )
    in_scope: bool = Field(default=True, description="False for patterns OpenSearch cannot serve")
    out_of_scope_reason: str | None = Field(
        None, description="Reason this pattern is out of scope. Required when in_scope=False."
    )

    model_config = ConfigDict(extra="ignore")

    @field_validator("opensearch_dsl")
    @classmethod
    def _validate_dsl(cls, v: str) -> str:
        validate_opensearch_dsl(v)
        return v


class UnsupportedPattern(BaseModel):
    """A source query that cannot be served by OpenSearch."""

    query_ids: list[str] = Field(
        default_factory=list,
        description="Source query IDs from collector that map to this unsupported pattern",
    )
    source_query: str = Field(..., description="Original SQL query that cannot be served")
    reason: str = Field(..., description="Why this pattern cannot be served by OpenSearch")
    recommendation: str = Field(..., description="Alternative approach or workaround")

    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------------------
# Top-level contract
# ---------------------------------------------------------------------------


class OpenSearchModelOutputContract(BaseModel):
    """Output contract for the OpenSearch schema design agent.

    Produced by the designer agent and validated by the PE reviewer.
    Search-workload tables become IndexMapping entries; time-series-workload
    tables become DataStreamConfig entries.  NOT_SUITABLE tables are omitted.
    """

    contract_version: str = Field(
        default="1.0",
        description="Contract version (MAJOR.MINOR format)",
    )
    job_id: str = Field(..., description="job_id from collector output")
    source_database: str = Field(..., description="database_name from collector metadata")
    index_designs: list[IndexMapping] = Field(
        default_factory=list, description="Index designs for SEARCH-workload tables"
    )
    data_stream_designs: list[DataStreamConfig] = Field(
        default_factory=list,
        description="Data stream configurations for TIMESERIES-workload tables",
    )
    access_patterns: list[AccessPattern] = Field(
        default_factory=list, description="SQL queries translated to OpenSearch DSL"
    )
    unsupported_patterns: list[UnsupportedPattern] = Field(
        default_factory=list,
        description="Queries that cannot be served by OpenSearch",
    )
    trade_offs: list[TradeOff] = Field(
        default_factory=list, description="Design trade-off decisions and rationale"
    )
    validation_passed: bool = Field(..., description="Whether all validation checks passed")
    validation_failures: list[str] = Field(
        default_factory=list, description="Validation failures when validation_passed=False"
    )

    model_config = ConfigDict(extra="ignore")
