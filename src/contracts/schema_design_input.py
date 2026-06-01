"""
Schema Design Agent — Input Contract

Defines exactly what the schema design agent receives as validated input.
This is a projection of CollectorOutputContract (v3.0) and
AnalysisOutputContract (v2.1) — only the fields the agent actually uses.

The projection function in this module validates that the full contracts
can be projected into these models. If validation fails, the schema design
step must abort — the agent cannot produce a correct design from invalid input.

Version History:
- 1.0 (2026-03-23): Initial version
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from src.contracts.analysis_output import AnalysisOutputContract
    from src.contracts.collector_output import CollectorOutputContract

# ---------------------------------------------------------------------------
# Enums (projected from source contracts)
# ---------------------------------------------------------------------------


class QueryType(str, Enum):
    SELECT = "SELECT"
    INSERT = "INSERT"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    MERGE = "MERGE"
    OTHER = "OTHER"


class NormalizedDataType(str, Enum):
    string = "string"
    integer = "integer"
    decimal = "decimal"
    boolean = "boolean"
    date = "date"
    datetime = "datetime"
    timestamp = "timestamp"
    binary = "binary"
    json = "json"
    xml = "xml"
    uuid = "uuid"
    text = "text"
    blob = "blob"


class ForeignKeyAction(str, Enum):
    CASCADE = "CASCADE"
    SET_NULL = "SET NULL"
    NO_ACTION = "NO ACTION"
    RESTRICT = "RESTRICT"
    SET_DEFAULT = "SET DEFAULT"


class QueryLogSource(str, Enum):
    performance_insights = "performance_insights"
    performance_schema = "performance_schema"
    pg_stat_statements = "pg_stat_statements"
    slow_query_log = "slow_query_log"
    general_log = "general_log"
    query_store = "query_store"
    dmv_query_stats = "dmv_query_stats"


class Confidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class MigrationComplexity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


# ---------------------------------------------------------------------------
# Schema projection — tables, columns, indexes, foreign keys
# ---------------------------------------------------------------------------


class AgentColumn(BaseModel):
    column_name: str
    ordinal_position: int | None = None
    normalized_data_type: NormalizedDataType | None = None
    max_length: int | None = Field(None, description="Used for item_size_bytes estimation")
    nullable: bool
    default_value: str | int | float | bool | None = None
    is_auto_increment: bool | None = False
    cardinality: int | None = Field(
        None,
        description="Approximate distinct value count. Used for hot partition analysis.",
    )


class AgentIndex(BaseModel):
    index_name: str
    columns: list[str]
    is_unique: bool
    is_primary: bool | None = False


class AgentForeignKey(BaseModel):
    constraint_name: str
    columns: list[str]
    referenced_table: str
    referenced_columns: list[str]
    on_delete: ForeignKeyAction | None = None


class AgentTable(BaseModel):
    table_id: str
    table_name: str
    schema_name: str | None = None
    row_count: int
    size_mb: float | None = None
    columns: list[AgentColumn]
    indexes: list[AgentIndex] | None = None
    primary_key: list[str] | None = None
    foreign_keys: list[AgentForeignKey] | None = None


# ---------------------------------------------------------------------------
# Query pattern projection
# ---------------------------------------------------------------------------


class AgentQueryPattern(BaseModel):
    """Projected query pattern — only fields that inform schema design."""

    query_id: str
    query_text: str
    query_type: QueryType | None = None
    frequency_per_hour: float = Field(..., ge=0)
    calls_per_second: float | None = Field(None)
    tables_accessed: list[str]

    rows_returned_avg: float | None = None
    rows_returned_p95: float | None = None
    rows_affected_avg: float | None = None
    rows_examined_avg: float | None = None
    scan_efficiency_pct: float | None = Field(None, ge=0, le=100)
    full_table_scans: int | None = Field(None, ge=0)
    queries_without_index: int | None = Field(None, ge=0)

    execution_time_ms_avg: float | None = Field(None, ge=0)
    execution_time_ms_p95: float | None = Field(None, ge=0)
    execution_time_ms_p99: float | None = Field(None, ge=0)
    total_time_ms: float | None = Field(None, ge=0)
    db_load_contribution_percent: float | None = Field(None, ge=0, le=100)

    lock_time_ms: float | None = Field(None, ge=0)
    lock_time_pct: float | None = Field(None, ge=0, le=100)

    has_joins: bool | None = None
    join_count: int | None = Field(None, ge=0)
    has_aggregations: bool | None = None
    has_subqueries: bool | None = None
    has_text_search: bool | None = None
    text_search_type: str | None = None
    has_time_range_filter: bool | None = None
    filter_columns: list[str] | None = None
    sort_columns: list[str] | None = None

    errors: int | None = Field(None, ge=0)
    warnings: int | None = Field(None, ge=0)
    first_seen: datetime | None = None
    last_seen: datetime | None = None


class AgentQueriesInput(BaseModel):
    query_patterns: list[AgentQueryPattern]
    total_queries_analyzed: int | None = None
    query_log_source: QueryLogSource | None = None
    collection_start_time: datetime | None = None
    collection_end_time: datetime | None = None


# ---------------------------------------------------------------------------
# Analysis signals projection
# ---------------------------------------------------------------------------


class AgentPattern(BaseModel):
    pattern_id: str
    pattern_type: str
    confidence: Confidence
    description: str | None = None
    query_ids: list[str] | None = None
    table_ids: list[str] | None = None
    frequency_percent: float | None = None


class AgentAntiPattern(BaseModel):
    anti_pattern_id: str
    anti_pattern_type: str
    severity_weight: float = Field(..., ge=0.0, le=1.0)
    description: str | None = None
    query_ids: list[str] | None = None
    table_ids: list[str] | None = None
    recommendation: str | None = None


class AgentTableRecommendation(BaseModel):
    table_id: str
    confidence_score: int = Field(..., ge=0, le=100)
    concerns: list[str] | None = None


class AgentAggregateRecommendation(BaseModel):
    aggregate_id: str
    root_table: str
    member_tables: list[str]
    co_access_confidence: int = Field(..., ge=0, le=100)
    combined_migration_complexity: MigrationComplexity


# ---------------------------------------------------------------------------
# Root input contracts
# ---------------------------------------------------------------------------


class AgentCollectorInput(BaseModel):
    """Projection of CollectorOutputContract for schema design."""

    contract_version: str = Field(..., description="Must start with '3.'")
    job_id: str
    source_database_name: str
    source_database_engine: str
    collection_timestamp: datetime
    tables: list[AgentTable]
    queries: AgentQueriesInput

    model_config = ConfigDict(extra="ignore")


class AgentAnalysisInput(BaseModel):
    """Projection of AnalysisOutputContract for schema design."""

    contract_version: str = Field(..., description="Must start with '2.'")
    patterns_detected: list[AgentPattern]
    anti_patterns_detected: list[AgentAntiPattern] | None = None
    table_recommendations: list[AgentTableRecommendation]
    aggregate_recommendations: list[AgentAggregateRecommendation] | None = None

    model_config = ConfigDict(extra="ignore")


class AgentContextInput(BaseModel):
    """Optional human overrides for growth/SLO parameters."""

    growth_multiplier: float = Field(default=10.0, gt=0)
    peak_to_avg_ratio: float = Field(default=3.0, gt=0)
    default_read_slo_ms: float = Field(default=10.0, gt=0)
    default_write_slo_ms: float = Field(default=20.0, gt=0)
    slo_overrides: dict[str, float] = Field(default_factory=dict)

    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------------------
# Groups manifest — written by prepare_schema_input.py --split
# ---------------------------------------------------------------------------


class SchemaDesignGroupEntry(BaseModel):
    """One group in the split schema design manifest."""

    group_index: int = Field(..., ge=0)
    group_name: str
    primary_tables: list[str]
    query_count: int = Field(..., ge=0)
    table_count: int = Field(..., ge=0)
    input_file: str = Field(..., description="Filename relative to the schema version dir")


class SchemaDesignGroupsManifest(BaseModel):
    """Manifest produced by prepare_schema_input.py --split.

    Lives at: .artifacts/{db}/{job}/schema-{engine}/v1/groups_manifest.json
    """

    job_id: str
    database_name: str
    target_engine: str
    total_queries: int = Field(..., ge=0)
    total_groups: int = Field(..., ge=0)
    groups: list[SchemaDesignGroupEntry]

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Projection function — maps full contracts to agent input
# ---------------------------------------------------------------------------


def project_schema_design_input(
    collector_output: CollectorOutputContract,
    analysis_output: AnalysisOutputContract,
    context: dict | None = None,
) -> tuple[AgentCollectorInput, AgentAnalysisInput, AgentContextInput]:
    """Project full contracts into validated schema design agent input.

    This is the gate — if the projection fails validation, the schema
    design step must abort. The agent cannot produce a correct design
    from invalid input.

    Args:
        collector_output: Full CollectorOutputContract from the collector step.
        analysis_output: Full AnalysisOutputContract from the analysis step.
        context: Optional dict of human overrides (growth_multiplier, etc.).

    Returns:
        Tuple of (AgentCollectorInput, AgentAnalysisInput, AgentContextInput).

    Raises:
        pydantic.ValidationError: if projection produces invalid input.
    """
    # --- Collector projection ---
    tables = []
    for t in collector_output.database_schema.tables:
        tables.append(
            AgentTable(
                table_id=t.table_id,
                table_name=t.table_name,
                schema_name=t.schema_name,
                row_count=t.row_count,
                size_mb=t.size_mb,
                columns=[
                    AgentColumn(
                        column_name=c.column_name,
                        ordinal_position=c.ordinal_position,
                        normalized_data_type=(
                            NormalizedDataType(c.normalized_data_type.value)
                            if c.normalized_data_type
                            else None
                        ),
                        max_length=c.max_length,
                        nullable=c.nullable,
                        default_value=c.default_value,
                        is_auto_increment=c.is_auto_increment,  # nosemgrep: is-function-without-parentheses
                        cardinality=c.cardinality,
                    )
                    for c in t.columns
                ],
                indexes=[
                    AgentIndex(
                        index_name=idx.index_name,
                        columns=idx.columns,
                        is_unique=idx.is_unique,  # nosemgrep: is-function-without-parentheses
                        is_primary=idx.is_primary,  # nosemgrep: is-function-without-parentheses
                    )
                    for idx in (t.indexes or [])
                ]
                or None,
                primary_key=t.primary_key,
                foreign_keys=[
                    AgentForeignKey(
                        constraint_name=fk.constraint_name,
                        columns=fk.columns,
                        referenced_table=fk.referenced_table,
                        referenced_columns=fk.referenced_columns,
                        on_delete=(ForeignKeyAction(fk.on_delete.value) if fk.on_delete else None),
                    )
                    for fk in (t.foreign_keys or [])
                ]
                or None,
            )
        )

    query_patterns = []
    for q in collector_output.queries.query_patterns:
        query_patterns.append(
            AgentQueryPattern(
                query_id=q.query_id,
                query_text=q.query_text,
                query_type=QueryType(q.query_type.value) if q.query_type else None,
                frequency_per_hour=q.frequency_per_hour,
                calls_per_second=q.calls_per_second,
                tables_accessed=q.tables_accessed,
                rows_returned_avg=q.rows_returned_avg,
                rows_returned_p95=q.rows_returned_p95,
                rows_affected_avg=q.rows_affected_avg,
                rows_examined_avg=q.rows_examined_avg,
                scan_efficiency_pct=q.scan_efficiency_pct,
                full_table_scans=q.full_table_scans,
                queries_without_index=q.queries_without_index,
                execution_time_ms_avg=q.execution_time_ms_avg,
                execution_time_ms_p95=q.execution_time_ms_p95,
                execution_time_ms_p99=q.execution_time_ms_p99,
                total_time_ms=q.total_time_ms,
                db_load_contribution_percent=q.db_load_contribution_percent,
                lock_time_ms=q.lock_time_ms,
                lock_time_pct=q.lock_time_pct,
                has_joins=q.has_joins,
                join_count=q.join_count,
                has_aggregations=q.has_aggregations,
                has_subqueries=q.has_subqueries,
                has_text_search=q.has_text_search,
                text_search_type=q.text_search_type,
                has_time_range_filter=q.has_time_range_filter,
                filter_columns=q.filter_columns,
                sort_columns=q.sort_columns,
                errors=q.errors,
                warnings=q.warnings,
                first_seen=q.first_seen,
                last_seen=q.last_seen,
            )
        )

    queries = collector_output.queries
    agent_collector = AgentCollectorInput(
        contract_version=collector_output.contract_version,
        job_id=collector_output.job_id,
        source_database_name=collector_output.metadata.source_database.database_name or "",
        source_database_engine=collector_output.metadata.source_database.engine.value,
        collection_timestamp=collector_output.metadata.collection_timestamp,
        tables=tables,
        queries=AgentQueriesInput(
            query_patterns=query_patterns,
            total_queries_analyzed=queries.total_queries_analyzed,
            query_log_source=(
                QueryLogSource(queries.query_log_source.value) if queries.query_log_source else None
            ),
            collection_start_time=queries.collection_start_time,
            collection_end_time=queries.collection_end_time,
        ),
    )

    # --- Analysis projection ---
    agent_analysis = AgentAnalysisInput(
        contract_version=analysis_output.contract_version,
        patterns_detected=[
            AgentPattern(
                pattern_id=p.pattern_id,
                pattern_type=p.pattern_type,
                confidence=Confidence(
                    p.confidence.value if hasattr(p.confidence, "value") else p.confidence
                ),
                description=p.description,
                query_ids=p.query_ids,
                table_ids=p.table_ids,
                frequency_percent=p.frequency_percent,
            )
            for p in analysis_output.workload_analysis.patterns_detected
        ],
        anti_patterns_detected=[
            AgentAntiPattern(
                anti_pattern_id=ap.anti_pattern_id,
                anti_pattern_type=ap.anti_pattern_type,
                severity_weight=ap.severity_weight,
                description=ap.description,
                query_ids=ap.query_ids,
                table_ids=ap.table_ids,
                recommendation=ap.recommendation,
            )
            for ap in (analysis_output.workload_analysis.anti_patterns_detected or [])
        ]
        or None,
        table_recommendations=[
            AgentTableRecommendation(
                table_id=tr.table_id,
                confidence_score=tr.confidence_score,
                concerns=tr.concerns,
            )
            for tr in analysis_output.table_recommendations
        ],
        aggregate_recommendations=[
            AgentAggregateRecommendation(
                aggregate_id=ar.aggregate_id,
                root_table=ar.root_table,
                member_tables=ar.member_tables,
                co_access_confidence=ar.co_access_confidence,
                combined_migration_complexity=MigrationComplexity(
                    ar.combined_migration_complexity.value
                    if hasattr(ar.combined_migration_complexity, "value")
                    else ar.combined_migration_complexity
                ),
            )
            for ar in (analysis_output.aggregate_recommendations or [])
        ]
        or None,
    )

    # --- Context (defaults if not provided) ---
    agent_context = AgentContextInput.model_validate(context or {})

    return agent_collector, agent_analysis, agent_context
