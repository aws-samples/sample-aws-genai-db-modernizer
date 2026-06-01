"""
CollectorOutputContract Pydantic Models
Version: 3.0
Last Modified: 2026-02-06
Description: Output contract for all Collector agents with RDS-specific enhancements
"""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DatabaseEngine(str, Enum):
    mysql = "mysql"
    postgresql = "postgresql"
    mariadb = "mariadb"
    sqlserver = "sqlserver"
    oracle = "oracle"
    db2 = "db2"


class DeploymentType(str, Enum):
    rds_instance = "rds_instance"
    on_premises = "on_premises"
    ec2_self_managed = "ec2_self_managed"


class StorageType(str, Enum):
    gp2 = "gp2"
    gp3 = "gp3"
    io1 = "io1"
    io2 = "io2"
    magnetic = "magnetic"


class QueryLogSource(str, Enum):
    performance_insights = "performance_insights"
    performance_schema = "performance_schema"
    pg_stat_statements = "pg_stat_statements"
    slow_query_log = "slow_query_log"
    general_log = "general_log"
    query_store = "query_store"
    dmv_query_stats = "dmv_query_stats"


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


class IndexType(str, Enum):
    btree = "btree"
    hash = "hash"
    gist = "gist"
    gin = "gin"
    fulltext = "fulltext"
    spatial = "spatial"
    other = "other"


class ForeignKeyAction(str, Enum):
    CASCADE = "CASCADE"
    SET_NULL = "SET NULL"
    NO_ACTION = "NO ACTION"
    RESTRICT = "RESTRICT"
    SET_DEFAULT = "SET DEFAULT"


class QueryType(str, Enum):
    SELECT = "SELECT"
    INSERT = "INSERT"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    MERGE = "MERGE"
    OTHER = "OTHER"


class ProcedureType(str, Enum):
    PROCEDURE = "PROCEDURE"
    FUNCTION = "FUNCTION"
    TRIGGER_FUNCTION = "TRIGGER_FUNCTION"


class ParameterMode(str, Enum):
    IN = "IN"
    OUT = "OUT"
    INOUT = "INOUT"


class TriggerEventType(str, Enum):
    INSERT = "INSERT"
    UPDATE = "UPDATE"
    DELETE = "DELETE"


class TriggerTiming(str, Enum):
    BEFORE = "BEFORE"
    AFTER = "AFTER"
    INSTEAD_OF = "INSTEAD OF"


class TriggerForEach(str, Enum):
    ROW = "ROW"
    STATEMENT = "STATEMENT"


class RDSInstanceMetadata(BaseModel):
    db_instance_identifier: str | None = Field(None, description="RDS instance identifier")
    instance_class: str | None = Field(
        None, description="Instance type (e.g., db.r5.xlarge, db.t3.medium)"
    )
    vcpu_count: int | None = Field(None, ge=1, description="Number of vCPUs")
    memory_gb: float | None = Field(None, ge=0, description="Memory in gigabytes")
    storage_type: StorageType | None = Field(None, description="EBS storage type")
    storage_size_gb: int | None = Field(None, ge=0, description="Allocated storage in GB")
    storage_iops: int | None = Field(
        None, ge=0, description="Provisioned IOPS (for io1/io2 storage)"
    )
    storage_throughput_mbps: int | None = Field(
        None, ge=0, description="Storage throughput in MB/s (for gp3)"
    )
    multi_az: bool | None = Field(None, description="Whether Multi-AZ is enabled")
    region: str | None = Field(None, description="AWS region")
    availability_zone: str | None = Field(None, description="Primary availability zone")
    read_replica_count: int | None = Field(None, ge=0, description="Number of read replicas")
    backup_retention_days: int | None = Field(
        None, ge=0, le=35, description="Backup retention period in days"
    )
    performance_insights_enabled: bool | None = Field(
        None, description="Whether Performance Insights is enabled"
    )
    enhanced_monitoring_interval: int | None = Field(
        None, description="Enhanced Monitoring interval in seconds (0 = disabled)"
    )

    @field_validator("enhanced_monitoring_interval")
    @classmethod
    def validate_monitoring_interval(cls, v):
        if v is not None and v not in [0, 1, 5, 10, 15, 30, 60]:
            raise ValueError("Enhanced monitoring interval must be one of: 0, 1, 5, 10, 15, 30, 60")
        return v


class SourceDatabase(BaseModel):
    engine: DatabaseEngine = Field(..., description="Database engine type")
    version: str = Field(..., description="Database engine version (e.g., '8.0.32', '14.7')")
    hostname: str = Field(
        ..., description="Database hostname (anonymized if PII protection enabled)"
    )
    database_name: str | None = Field(None, description="Database/schema name")
    database_size_gb: float | None = Field(
        None, ge=0, description="Total database size in gigabytes"
    )
    deployment_type: DeploymentType | None = Field(
        None, description="Database deployment type (Aurora is a target database, not a source)"
    )
    rds_instance_metadata: RDSInstanceMetadata | None = Field(
        None, description="RDS instance metadata (only present if deployment_type is rds_instance)"
    )


class Metadata(BaseModel):
    collection_timestamp: datetime = Field(
        ..., description="ISO 8601 timestamp when collection completed"
    )
    collector_version: str = Field(
        ..., pattern=r"^\d+\.\d+\.\d+$", description="Semantic version of collector agent"
    )
    collection_duration_seconds: float | None = Field(
        None, ge=0, description="Time taken to collect data"
    )
    source_database: SourceDatabase = Field(..., description="Source database information")


class Parameter(BaseModel):
    parameter_name: str | None = Field(None, description="Parameter name")
    data_type: str | None = Field(None, description="Parameter data type")
    parameter_mode: ParameterMode | None = Field(None, description="Parameter mode")


class ExecutionStats(BaseModel):
    total_executions: int | None = Field(None, ge=0, description="Total number of executions")
    avg_execution_time_ms: float | None = Field(
        None, ge=0, description="Average execution time in milliseconds"
    )
    last_executed: datetime | None = Field(None, description="Last execution timestamp")


class Column(BaseModel):
    column_name: str = Field(..., description="Column name")
    ordinal_position: int | None = Field(None, ge=1, description="Column position in table")
    data_type: str = Field(..., description="Native database data type")
    normalized_data_type: NormalizedDataType | None = Field(
        None, description="Normalized data type for cross-database comparison"
    )
    max_length: int | None = Field(None, ge=0, description="Maximum length for string types")
    nullable: bool = Field(..., description="Whether column allows NULL values")
    default_value: str | int | float | bool | None = Field(None, description="Default value")
    is_auto_increment: bool | None = Field(False, description="Whether column is auto-incrementing")
    cardinality: int | None = Field(None, ge=0, description="Approximate number of distinct values")


class Index(BaseModel):
    index_name: str = Field(..., description="Index name")
    columns: list[str] = Field(..., min_length=1, description="List of column names in index")
    is_unique: bool = Field(..., description="Whether index enforces uniqueness")
    is_primary: bool | None = Field(False, description="Whether this is the primary key index")
    index_type: IndexType | None = Field(None, description="Index type")


class ForeignKey(BaseModel):
    constraint_name: str = Field(..., description="Foreign key constraint name")
    columns: list[str] = Field(..., description="List of foreign key columns")
    referenced_table: str = Field(..., description="Referenced table name")
    referenced_columns: list[str] = Field(..., description="List of referenced columns")
    on_delete: ForeignKeyAction | None = Field(None, description="ON DELETE action")
    on_update: ForeignKeyAction | None = Field(None, description="ON UPDATE action")


class Table(BaseModel):
    table_id: str = Field(
        ..., description="Unique identifier for this table (format: {schema}.{table})"
    )
    table_name: str = Field(..., description="Table name")
    schema_name: str | None = Field(None, description="Schema or database name")
    row_count: int = Field(..., ge=0, description="Approximate number of rows")
    size_mb: float | None = Field(None, ge=0, description="Table size in megabytes")
    columns: list[Column] = Field(..., min_length=1, description="List of table columns")
    indexes: list[Index] | None = Field(None, description="List of table indexes")
    primary_key: list[str] | None = Field(None, description="List of column names in primary key")
    foreign_keys: list[ForeignKey] | None = Field(
        None, description="List of foreign key constraints"
    )
    sample_data: list[dict[str, Any]] | None = Field(
        None, max_length=1000, description="Sample rows (anonymized if PII protection enabled)"
    )


class View(BaseModel):
    view_id: str = Field(
        ..., description="Unique identifier for this view (format: {schema}.{view})"
    )
    view_name: str = Field(..., description="View name")
    schema_name: str | None = Field(None, description="Schema or database name")
    definition: str = Field(..., description="View SQL definition")
    is_updatable: bool | None = Field(
        None, description="Whether the view supports INSERT/UPDATE/DELETE operations"
    )
    referenced_tables: list[str] | None = Field(
        None, description="List of table IDs referenced by this view"
    )
    column_list: list[str] | None = Field(None, description="List of column names in the view")


class Procedure(BaseModel):
    procedure_id: str = Field(
        ..., description="Unique identifier for this procedure (format: {schema}.{procedure})"
    )
    procedure_name: str = Field(..., description="Procedure or function name")
    schema_name: str | None = Field(None, description="Schema or database name")
    procedure_type: ProcedureType = Field(..., description="Type of database routine")
    definition: str | None = Field(
        None, description="Procedure/function SQL definition (anonymized if PII protection enabled)"
    )
    language: str | None = Field(None, description="Language (e.g., SQL, PL/pgSQL, T-SQL)")
    return_type: str | None = Field(
        None, description="Return type for functions (null for procedures)"
    )
    parameters: list[Parameter] | None = Field(None, description="List of parameters")
    referenced_tables: list[str] | None = Field(
        None, description="List of table IDs accessed by this procedure"
    )
    execution_stats: ExecutionStats | None = Field(
        None, description="Optional execution statistics if available"
    )


class TriggerExecutionStats(BaseModel):
    total_executions: int | None = Field(None, ge=0, description="Total number of executions")
    avg_execution_time_ms: float | None = Field(
        None, ge=0, description="Average execution time in milliseconds"
    )


class Trigger(BaseModel):
    trigger_id: str = Field(..., description="Unique identifier for this trigger")
    trigger_name: str = Field(..., description="Trigger name")
    schema_name: str | None = Field(None, description="Schema or database name")
    table_id: str = Field(..., description="Table ID this trigger is attached to")
    event_type: TriggerEventType = Field(..., description="Event that fires the trigger")
    timing: TriggerTiming = Field(..., description="When the trigger fires relative to the event")
    for_each: TriggerForEach | None = Field(
        None, description="Whether trigger fires per row or per statement"
    )
    definition: str | None = Field(
        None, description="Trigger SQL definition (anonymized if PII protection enabled)"
    )
    is_enabled: bool | None = Field(None, description="Whether the trigger is currently enabled")
    execution_stats: TriggerExecutionStats | None = Field(
        None, description="Optional execution statistics if available"
    )


class Schema(BaseModel):
    tables: list[Table] = Field(..., min_length=1, description="List of database tables")
    views: list[View] | None = Field(None, description="View definitions (all databases support)")
    procedures: list[Procedure] | None = Field(
        None, description="Stored procedures and functions (all databases support)"
    )
    triggers: list[Trigger] | None = Field(
        None, description="Trigger definitions (all databases support)"
    )


class WaitEvent(BaseModel):
    event_name: str | None = Field(None, description="Wait event name")
    wait_time_ms: float | None = Field(None, ge=0, description="Wait time in milliseconds")
    wait_time_percent: float | None = Field(
        None, ge=0, le=100, description="Wait time as percentage"
    )


class QueryPattern(BaseModel):
    query_id: str = Field(
        ...,
        description="Unique identifier for this query pattern (Performance Insights digest or query hash)",
    )
    query_text: str = Field(
        ..., description="Normalized query text (anonymized if PII protection enabled)"
    )
    query_type: QueryType | None = Field(None, description="Primary operation type")
    frequency_per_hour: float = Field(
        ..., ge=0, description="Average frequency of this query pattern per hour"
    )
    calls_per_second: float | None = Field(None, ge=0, description="Average calls per second")
    tables_accessed: list[str] = Field(
        ..., min_length=1, description="List of table IDs accessed by this query"
    )
    rows_returned_avg: float | None = Field(
        None, ge=0, description="Average number of rows returned"
    )
    rows_returned_p95: float | None = Field(None, ge=0, description="95th percentile rows returned")
    rows_affected_avg: float | None = Field(
        None, ge=0, description="Average rows affected (for INSERT/UPDATE/DELETE)"
    )
    rows_examined_avg: float | None = Field(
        None, ge=0, description="Average rows examined/scanned (MySQL performance_schema)"
    )
    execution_time_ms_avg: float | None = Field(
        None, ge=0, description="Average execution time in milliseconds"
    )
    execution_time_ms_min: float | None = Field(None, ge=0, description="Minimum execution time")
    execution_time_ms_max: float | None = Field(None, ge=0, description="Maximum execution time")
    execution_time_ms_p50: float | None = Field(None, ge=0, description="Median execution time")
    execution_time_ms_p95: float | None = Field(
        None, ge=0, description="95th percentile execution time"
    )
    execution_time_ms_p99: float | None = Field(
        None, ge=0, description="99th percentile execution time"
    )
    total_time_ms: float | None = Field(
        None, ge=0, description="Total time spent executing this query pattern"
    )
    db_load_contribution_percent: float | None = Field(
        None, ge=0, le=100, description="Percentage of total database load from this query"
    )
    has_joins: bool | None = Field(None, description="Whether query contains joins")
    join_count: int | None = Field(None, ge=0, description="Number of joins in query")
    has_aggregations: bool | None = Field(None, description="Whether query contains aggregations")
    has_subqueries: bool | None = Field(None, description="Whether query contains subqueries")
    has_text_search: bool | None = Field(
        None, description="Whether query uses text search (LIKE '%%', MATCH AGAINST, tsvector)"
    )
    text_search_type: str | None = Field(
        None, description="Type of text search: like_wildcard, fulltext, tsvector, trigram"
    )
    has_time_range_filter: bool | None = Field(
        None, description="Whether query filters on date/timestamp with range or interval"
    )
    filter_columns: list[str] | None = Field(None, description="Columns used in WHERE clause")
    sort_columns: list[str] | None = Field(None, description="Columns used in ORDER BY clause")
    full_table_scans: int | None = Field(
        None, ge=0, description="Number of full table scans (MySQL)"
    )
    range_scans: int | None = Field(None, ge=0, description="Number of range scans (MySQL)")
    scan_efficiency_pct: float | None = Field(None, ge=0, le=100, description="Scan efficiency")
    queries_without_index: int | None = Field(
        None, ge=0, description="Queries without index (MySQL)"
    )
    queries_with_bad_index: int | None = Field(
        None, ge=0, description="Queries with suboptimal index (MySQL)"
    )
    lock_time_ms: float | None = Field(None, ge=0, description="Total lock time (MySQL)")
    lock_time_pct: float | None = Field(None, ge=0, le=100, description="Lock time percentage")
    cache_hit_ratio_pct: float | None = Field(
        None, ge=0, le=100, description="Cache hit ratio (PostgreSQL)"
    )
    shared_blks_hit: int | None = Field(
        None, ge=0, description="Shared blocks in cache (PostgreSQL)"
    )
    shared_blks_read: int | None = Field(
        None, ge=0, description="Shared blocks from disk (PostgreSQL)"
    )
    io_read_time_ms: float | None = Field(None, ge=0, description="I/O read time (PostgreSQL)")
    io_write_time_ms: float | None = Field(None, ge=0, description="I/O write time (PostgreSQL)")
    temp_blocks_read: int | None = Field(
        None, ge=0, description="Temporary blocks read (PostgreSQL)"
    )
    temp_blocks_written: int | None = Field(
        None, ge=0, description="Temporary blocks written (PostgreSQL)"
    )
    avg_logical_reads: float | None = Field(
        None, ge=0, description="Average logical reads (SQL Server)"
    )
    avg_physical_reads: float | None = Field(
        None, ge=0, description="Average physical reads (SQL Server)"
    )
    avg_cpu_time_ms: float | None = Field(None, ge=0, description="Average CPU time (SQL Server)")
    read_write_ratio_pct: float | None = Field(
        None, ge=0, le=100, description="Read/write ratio (SQL Server)"
    )
    errors: int | None = Field(None, ge=0, description="Number of errors encountered")
    warnings: int | None = Field(None, ge=0, description="Number of warnings generated")
    first_seen: datetime | None = Field(None, description="First occurrence of this query pattern")
    last_seen: datetime | None = Field(None, description="Last occurrence of this query pattern")
    wait_events: list[WaitEvent] | None = Field(
        None, description="Wait events from Performance Insights"
    )


class Queries(BaseModel):
    query_patterns: list[QueryPattern] = Field(
        ..., description="List of query patterns with performance metrics"
    )
    total_queries_analyzed: int | None = Field(
        None, ge=0, description="Total number of queries in log analyzed"
    )
    query_log_source: QueryLogSource | None = Field(None, description="Source of query data")
    collection_start_time: datetime | None = Field(
        None, description="Start time of query log collection period"
    )
    collection_end_time: datetime | None = Field(
        None, description="End time of query log collection period"
    )


class PerformanceMetrics(BaseModel):
    avg_query_time_ms: float | None = Field(
        None, ge=0, description="Average query execution time in milliseconds"
    )
    p50_query_time_ms: float | None = Field(None, ge=0, description="Median query execution time")
    p95_query_time_ms: float | None = Field(
        None, ge=0, description="95th percentile query execution time"
    )
    p99_query_time_ms: float | None = Field(
        None, ge=0, description="99th percentile query execution time"
    )
    queries_per_second: float | None = Field(None, ge=0, description="Average queries per second")
    connection_pool_usage_percent: float | None = Field(
        None, ge=0, le=100, description="Average connection pool utilization percentage"
    )
    active_connections_avg: float | None = Field(
        None, ge=0, description="Average number of active connections"
    )
    active_connections_max: float | None = Field(
        None, ge=0, description="Maximum active connections observed"
    )
    transactions_per_second: float | None = Field(
        None, ge=0, description="Average transactions per second"
    )
    read_iops_avg: float | None = Field(None, ge=0, description="Average read IOPS")
    write_iops_avg: float | None = Field(None, ge=0, description="Average write IOPS")
    network_throughput_mbps_avg: float | None = Field(
        None, ge=0, description="Average network throughput in MB/s"
    )


class MetricStats(BaseModel):
    avg: float | None = Field(None, ge=0, description="Average value")
    max: float | None = Field(None, ge=0, description="Maximum value")
    min: float | None = Field(None, ge=0, description="Minimum value")
    p95: float | None = Field(None, ge=0, description="95th percentile value")


class RDSCloudWatchMetrics(BaseModel):
    cpu_utilization: MetricStats | None = Field(None, description="CPU utilization percentage")
    freeable_memory_gb: MetricStats | None = Field(None, description="Freeable memory in GB")
    database_connections: MetricStats | None = Field(None, description="Database connections")
    read_iops: MetricStats | None = Field(None, description="Read IOPS")
    write_iops: MetricStats | None = Field(None, description="Write IOPS")
    read_latency_ms: MetricStats | None = Field(None, description="Read latency in milliseconds")
    write_latency_ms: MetricStats | None = Field(None, description="Write latency in milliseconds")
    network_receive_throughput_mbps: MetricStats | None = Field(
        None, description="Network receive throughput in MB/s"
    )
    network_transmit_throughput_mbps: MetricStats | None = Field(
        None, description="Network transmit throughput in MB/s"
    )
    free_storage_space_gb: float | None = Field(
        None, ge=0, description="Current free storage space"
    )
    replica_lag_ms: MetricStats | None = Field(
        None, description="Replication lag (for read replicas)"
    )


class Metrics(BaseModel):
    performance_metrics: PerformanceMetrics = Field(
        ..., description="Performance metrics from database"
    )
    rds_cloudwatch_metrics: RDSCloudWatchMetrics | None = Field(
        None, description="RDS CloudWatch metrics (only present if collect_rds_metrics is true)"
    )


class CollectorOutputContract(BaseModel):
    """
    Output contract for all Collector agents with RDS-specific enhancements
    Version: 3.0
    """

    contract_version: str = Field(
        default="3.0",
        pattern=r"^\d+\.\d+$",
        description="Contract version (MAJOR.MINOR format)",
    )
    job_id: str = Field(..., description="Unique job identifier for traceability")
    metadata: Metadata = Field(
        ..., description="Collection metadata and source database information"
    )
    database_schema: Schema = Field(
        ...,
        description="Database schema information including tables, views, procedures, and triggers",
    )
    queries: Queries = Field(..., description="Query patterns and performance data")
    metrics: Metrics = Field(..., description="Performance and CloudWatch metrics")

    model_config = ConfigDict(json_encoders={datetime: lambda v: v.isoformat()})
