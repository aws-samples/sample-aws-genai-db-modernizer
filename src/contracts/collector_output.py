"""
CollectorOutputContract Pydantic Models
Version: 3.4
Last Modified: 2026-10-18
Description: Output contract for all Collector agents with RDS-specific enhancements

Version History:
- 3.0: Prior version.
- 3.1 (#156): Added Column.numeric_precision/numeric_scale (optional) and the
  NormalizedDataType.floating_point enum value - both backward-compatible
  additions (MINOR per docs/contracts/agent-contracts-spec.md: new optional
  field, new enum value).
- 3.2 (2026-10-16, #949): Added optional ``Metadata.engines_in_use``: the AWS
  database engines (engine ids such as ``elasticache``) the application already
  runs today next to its source. Only these, and the source engine itself, can be
  retired for a saving; an engine the plan analysed and dropped never is.
  Backward compatible: defaults to empty (the source is the only engine running).
- 3.3 (2026-10-17, #934): Added optional ``QueryPattern.parsed_facts``.
- 3.4 (2026-10-18, #471): Added optional ``Table.storage_engine`` and
  ``Table.collation`` (the MySQL family reports both), read by the relational-move gap
  check. Backward compatible: both default to None (not reported), and a rule that
  needs them then finds nothing.
- 3.5 (2026-10-21, #1180): Added optional ``QueryPattern.query_tags`` (each distinct
  leading-comment text of every row merged into the shape, in first-seen order) and
  ``QueryPattern.query_tags_dropped`` (how many tags the count and length caps dropped),
  so a caller can join a statement to its own code. Backward compatible: both default to
  empty (no tags).
- 3.6 (2026-10-24, #1197): Added optional ``Metrics.source_host``: the instance-wide
  buffer-cache hit ratio (and the counts it comes from) and the host CPU count and
  memory the collection script reported. Backward compatible: defaults to None (not
  reported), and a cost estimate then has no collection-wide hit ratio to fall back on.
"""

import re
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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
    v_dollar_sql = "v_dollar_sql"


class NormalizedDataType(str, Enum):
    """``decimal`` is exact (DECIMAL/NUMERIC/MONEY, Oracle NUMBER): precision and
    scale are significant and are carried in the column's own fields.
    ``floating_point`` is approximate (FLOAT/REAL/DOUBLE, Oracle BINARY_FLOAT /
    BINARY_DOUBLE): it never resolves to a constrained NUMERIC/DECIMAL target
    type, because that would misstate a float as exact (#156)."""

    string = "string"
    integer = "integer"
    decimal = "decimal"
    floating_point = "floating_point"
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
    engines_in_use: list[str] = Field(
        default_factory=list,
        description=(
            "Engine ids (e.g. elasticache, documentdb) of the AWS databases the application "
            "already runs today next to the source. Only these and the source engine can be "
            "retired for a saving (#949)."
        ),
    )

    @field_validator("engines_in_use")
    @classmethod
    def validate_engines_in_use(cls, v: list[str]) -> list[str]:
        out = [e.strip().lower() for e in v]
        for e in out:
            if not re.fullmatch(r"[a-z][a-z0-9_]*", e):
                raise ValueError(f"engines_in_use holds engine ids, got {e!r}")
        return out


class Parameter(BaseModel):
    parameter_name: str | None = Field(None, description="Parameter name")
    data_type: str | None = Field(None, description="Parameter data type")
    parameter_mode: ParameterMode | None = Field(None, description="Parameter mode")


def _try_parse_int(v: object) -> int | None:
    """Parse ``v`` to ``int`` with no range check, or ``None`` if blank or
    unparseable. ``bool`` is excluded even though it is an ``int`` subclass,
    because a boolean here means the payload is wrong, not that the value is 1.

    Collectors shell out to database CLIs and parse the output, so numeric
    metadata arrives as a string -- and as an empty string for types that
    have neither.
    """
    if v is None or v == "" or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str | float | Decimal):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None
    return None


def _coerce_optional_int(v: object, *, min_value: int) -> int | None:
    """``_try_parse_int(v)``, dropping anything below ``min_value``.

    A value below ``min_value`` is dropped rather than clamped: it means the
    source was misparsed, and ``None`` ("unknown") is honest where clamping
    would fabricate a constraint.
    """
    parsed = _try_parse_int(v)
    return parsed if parsed is not None and parsed >= min_value else None


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
    numeric_precision: int | None = Field(
        None,
        ge=0,
        description=(
            "Total significant digits for exact-numeric types, e.g. 10 in DECIMAL(10,2). "
            "Distinct from max_length, which is a character count for string types."
        ),
    )
    numeric_scale: int | None = Field(
        None,
        ge=-84,
        description=(
            "Digits right of the decimal point, e.g. 2 in DECIMAL(10,2). Can be "
            "negative: Oracle's NUMBER(p, s) rounds to the left of the decimal "
            "point when s < 0 (NUMBER(5,-2) holds multiples of 100, up to "
            "9,999,900) -- -84 is Oracle's own documented floor for s."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _unusable_scale_marks_precision_unusable(cls, data: object) -> object:
        """A present, parseable scale below Oracle's floor (-84) means the
        whole numeric metadata is unusable, not "scale unknown, precision
        still known".

        ``_coerce_numeric_scale`` already drops a value that far out of range
        to ``None``, which on its own would read as "no scale given" -- and
        the type map treats an absent scale as a bare ``DECIMAL(p)`` (scale
        0). That is a narrower claim than a real ``NUMBER(p, s)`` with ``s``
        this far negative actually holds, so it would fabricate a constraint
        from a value that was never trustworthy. With no usable scale to
        fold into it, precision is unusable too, so this flags the column
        (via precision/scale both ``None``) instead.
        """
        if not isinstance(data, dict):
            return data
        scale = _try_parse_int(data.get("numeric_scale"))
        if scale is not None and scale < -84:
            data = {**data, "numeric_precision": None}
        return data

    @field_validator("numeric_precision", mode="before")
    @classmethod
    def _coerce_numeric_precision(cls, v: object) -> int | None:
        """Precision is always a positive digit count; see ``_coerce_optional_int``."""
        return _coerce_optional_int(v, min_value=0)

    @field_validator("numeric_scale", mode="before")
    @classmethod
    def _coerce_numeric_scale(cls, v: object) -> int | None:
        """Unlike precision, scale can be legitimately negative (see the field's
        own description) -- a value is only dropped once it is below Oracle's
        documented floor, not merely for being negative. See ``_coerce_optional_int``.
        """
        return _coerce_optional_int(v, min_value=-84)

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
    predicate: str | None = Field(
        None, description="Partial-index predicate (PostgreSQL WHERE clause), if any"
    )


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
    storage_engine: str | None = Field(
        None,
        description=(
            "The table's storage engine where the source has several (MySQL family: "
            "information_schema.TABLES.ENGINE, e.g. InnoDB, MyISAM); read by the relational-move "
            "gap check (#471)"
        ),
    )
    collation: str | None = Field(
        None,
        description=(
            "The table's default collation where the source reports one (MySQL family: "
            "information_schema.TABLES.TABLE_COLLATION); read by the relational-move gap check (#471)"
        ),
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
    query_tags: list[str] = Field(
        default_factory=list,
        description=(
            "Each distinct leading-comment text (markers removed, trimmed) of every row "
            "merged into this shape, in first-seen order, such as sqlcommenter's "
            "controller='cart',action='add' or a plain 'endpoint: CartController.Add'; empty "
            "when there are none. A trailing comment is also a tag when it parses as "
            "sqlcommenter key='value' pairs (leading and trailing tags are deduplicated "
            "together); a free-text trailing comment and inline comments are not tags. Read from "
            "query_text as collected, so nothing new is collected. Untrusted customer text, "
            "for joining a statement to the caller's code: no routing or scoring step reads "
            "it, and every model input is built without this field (query_text keeps the "
            "representative row's leading comment, so that text still reaches prompts inside "
            "query_text). Capped (src.tools.sql.query_tags): see "
            "query_tags_dropped. Which sources fill it, from the text each collector reads: "
            "PostgreSQL every leading comment and sqlcommenter trailing comment "
            "(pg_stat_statements keeps the text); MySQL and MariaDB none (DIGEST_TEXT has its "
            "comments removed); SQL Server and Oracle block comments only, leading or "
            "sqlcommenter trailing (their collectors replace newlines with spaces, so a -- "
            "comment runs into the statement); RDS Performance Insights what its statement "
            "text keeps"
        ),
    )
    query_tags_dropped: int = Field(
        0,
        ge=0,
        description=(
            "How many tags the caps dropped (distinct tags past the count cap, or a tag longer "
            "than the length cap); 0 exactly when query_tags is complete"
        ),
    )
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
        None,
        description=(
            "Type of text search (the text_search query fact's parts): like_wildcard, "
            "fulltext, tsvector, oracle_text, trigram, regex"
        ),
    )
    parsed_facts: dict[str, Any] | None = Field(
        None,
        description=(
            "The SQL facade's readings of query_text (its query-shape and access-pattern "
            "facts), stored by the collector so later steps need not parse the query again. "
            "Written and read only by src.tools.sql.stored_facts, versioned by the code that "
            "reads them: a step parses the text itself when they are missing or from another "
            "version, engine or text"
        ),
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
        None, ge=0, description="Average logical reads (SQL Server / Oracle)"
    )
    avg_physical_reads: float | None = Field(
        None, ge=0, description="Average physical reads (SQL Server / Oracle)"
    )
    avg_cpu_time_ms: float | None = Field(
        None, ge=0, description="Average CPU time (SQL Server / Oracle)"
    )
    read_write_ratio_pct: float | None = Field(
        None, ge=0, le=100, description="Read/write ratio (SQL Server / Oracle)"
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


class SourceHost(BaseModel):
    """Instance-wide figures the source reported about its buffer cache and host (#1197).

    The buffer-cache figures cover every query the instance ran (the MySQL InnoDB
    buffer pool, PostgreSQL shared buffers, the SQL Server buffer manager, the Oracle
    buffer cache), not one query: a cost estimate uses the hit ratio only for a query
    that reports none of its own.
    """

    buffer_cache_hit_ratio_pct: float | None = Field(
        None,
        ge=0,
        le=100,
        description="Share of logical page reads served from the buffer cache (percent)",
    )
    buffer_cache_read_requests: int | None = Field(
        None, ge=0, description="Logical page reads the hit ratio is computed over"
    )
    buffer_cache_reads_from_disk: int | None = Field(
        None, ge=0, description="Page reads the buffer cache did not serve"
    )
    cpu_count: int | None = Field(None, ge=1, description="CPUs the source host reports")
    physical_memory_gb: float | None = Field(
        None, ge=0, description="Physical memory the source host reports, in GiB"
    )
    committed_memory_gb: float | None = Field(
        None, ge=0, description="Memory the database process has committed, in GiB"
    )


class Metrics(BaseModel):
    performance_metrics: PerformanceMetrics = Field(
        ..., description="Performance metrics from database"
    )
    rds_cloudwatch_metrics: RDSCloudWatchMetrics | None = Field(
        None, description="RDS CloudWatch metrics (only present if collect_rds_metrics is true)"
    )
    source_host: SourceHost | None = Field(
        None,
        description="Instance-wide buffer-cache and host figures the collection reported (#1197)",
    )


class CollectorOutputContract(BaseModel):
    """
    Output contract for all Collector agents with RDS-specific enhancements
    Version: 3.4
    """

    contract_version: str = Field(
        default="3.6",
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
