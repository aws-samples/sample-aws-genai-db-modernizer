"""
MySQL Collector Orchestrator — Checkpoint-based idempotent collection.

Each collection step saves its result to S3. On failure/retry, completed
steps are loaded from S3 and skipped. Only incomplete steps are re-executed.

Stages (LIVE mode):
  1. metadata     — version, db size
  2. schema       — tables, columns, indexes, FKs
  3. queries      — performance_schema patterns
  4. aws_metrics  — RDS metadata, CloudWatch, PI, Database Insights
  5. output       — final assembled CollectorOutputContract

Stages (DDL mode):
  1. ddl_schema   — parse DDL from S3
  2. aws_metrics  — RDS metadata, CloudWatch, PI, Database Insights
  3. output       — final assembled CollectorOutputContract
"""

import logging
import time
from datetime import UTC, datetime

from src.contracts.collector_input import CollectionMode, CollectorInput
from src.contracts.collector_output import (
    CollectorOutputContract,
    Column,
    DeploymentType,
    ForeignKey,
    Index,
    Metadata,
    Metrics,
    MetricStats,
    NormalizedDataType,
    PerformanceMetrics,
    Procedure,
    ProcedureType,
    Queries,
    QueryLogSource,
    QueryPattern,
    QueryType,
    RDSCloudWatchMetrics,
    RDSInstanceMetadata,
    Schema,
    SourceDatabase,
    Table,
    Trigger,
    View,
)
from src.tools.aws.credentials import AWSCredentialManager
from src.tools.aws.rds_tools import (
    get_cloudwatch_metrics,
    get_database_insights,
    get_enhanced_monitoring_cpu,
    get_performance_insights_queries,
    get_pi_counter_metrics,
    get_rds_instance_metadata,
)
from src.tools.aws.s3_storage import init_storage, load_json, save_json
from src.tools.aws.ssm_executor import SSMExecutor
from src.tools.database.ddl_parser import fetch_ddl_from_s3, parse_ddl
from src.tools.database.mysql_tools import MySQLRemoteCollector

logger = logging.getLogger(__name__)

COLLECTOR_VERSION = "1.0.0"

_TYPE_MAP: dict[str, NormalizedDataType] = {
    "int": NormalizedDataType.integer,
    "tinyint": NormalizedDataType.integer,
    "smallint": NormalizedDataType.integer,
    "mediumint": NormalizedDataType.integer,
    "bigint": NormalizedDataType.integer,
    "float": NormalizedDataType.decimal,
    "double": NormalizedDataType.decimal,
    "decimal": NormalizedDataType.decimal,
    "char": NormalizedDataType.string,
    "varchar": NormalizedDataType.string,
    "text": NormalizedDataType.text,
    "tinytext": NormalizedDataType.text,
    "mediumtext": NormalizedDataType.text,
    "longtext": NormalizedDataType.text,
    "blob": NormalizedDataType.blob,
    "tinyblob": NormalizedDataType.blob,
    "mediumblob": NormalizedDataType.blob,
    "longblob": NormalizedDataType.blob,
    "binary": NormalizedDataType.binary,
    "varbinary": NormalizedDataType.binary,
    "date": NormalizedDataType.date,
    "datetime": NormalizedDataType.datetime,
    "timestamp": NormalizedDataType.timestamp,
    "json": NormalizedDataType.json,
    "boolean": NormalizedDataType.boolean,
    "bool": NormalizedDataType.boolean,
    "enum": NormalizedDataType.string,
    "set": NormalizedDataType.string,
    "bit": NormalizedDataType.integer,
    "time": NormalizedDataType.string,
    "year": NormalizedDataType.integer,
}


# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------


class CheckpointStore:
    """Read/write stage checkpoints to S3."""

    def __init__(self, cred_mgr: AWSCredentialManager | None, bucket: str, prefix: str):
        self._cred_mgr = cred_mgr
        self._bucket = bucket
        self._prefix = prefix  # e.g. "mydb/collector/"

    def _key(self, stage: str) -> str:
        return f"{self._prefix}{stage}.json"

    def exists(self, stage: str) -> bool:
        if not self._cred_mgr:
            return False
        try:
            s3 = self._cred_mgr.client("s3")
            s3.head_object(Bucket=self._bucket, Key=self._key(stage))
            return True
        except Exception:
            return False

    def load(self, stage: str):
        assert self._cred_mgr is not None
        return load_json(self._cred_mgr, self._bucket, self._key(stage))

    def save(self, stage: str, data):
        assert self._cred_mgr is not None
        save_json(self._cred_mgr, self._bucket, self._key(stage), data)
        logger.info("Checkpoint saved: %s", stage)

    def load_or_run(self, stage: str, fn):
        """Load from checkpoint if exists, otherwise run fn and save."""
        if self.exists(stage):
            logger.info("Resuming from checkpoint: %s", stage)
            return self.load(stage)
        result = fn()
        self.save(stage, result)
        return result


class NoopCheckpointStore:
    """No-op store when S3 is unavailable (local dev)."""

    def exists(self, stage: str) -> bool:
        return False

    def load(self, stage: str):
        raise FileNotFoundError(stage)

    def save(self, stage: str, data):
        pass

    def load_or_run(self, stage: str, fn):
        return fn()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def collect(input_contract: CollectorInput) -> CollectorOutputContract:
    """Entry point — init storage, dispatch to live or ddl mode with checkpoints."""
    ckpt = _init_checkpoint_store(input_contract)

    # If final output already exists, return it directly
    if ckpt.exists("output"):
        logger.info("Job %s: final output already exists, returning cached", input_contract.job_id)
        result: CollectorOutputContract = CollectorOutputContract.model_validate(
            ckpt.load("output")
        )
        return result

    if input_contract.mode == CollectionMode.live:
        result = _collect_live(input_contract, ckpt)
    elif input_contract.mode == CollectionMode.ddl:
        result = _collect_ddl(input_contract, ckpt)
    else:
        result = _collect_offline(input_contract, ckpt)

    # Save final output
    ckpt.save("output", result.model_dump(mode="json"))
    return result


def _init_checkpoint_store(inp: CollectorInput) -> CheckpointStore | NoopCheckpointStore:
    cred_mgr = _build_cred_mgr(inp)
    if not cred_mgr:
        return NoopCheckpointStore()
    try:
        storage = init_storage(cred_mgr, inp.cluster_endpoint, inp.database_name, inp.job_id)
        return CheckpointStore(cred_mgr, storage["bucket"], storage["collector_prefix"])
    except Exception:
        logger.warning("Failed to init S3 storage, running without checkpoints")
        return NoopCheckpointStore()


# ---------------------------------------------------------------------------
# LIVE mode
# ---------------------------------------------------------------------------


def _collect_live(inp: CollectorInput, ckpt) -> CollectorOutputContract:
    start = time.monotonic()
    cred_mgr = _build_cred_mgr(inp)
    region = inp.aws_config.region if inp.aws_config else "us-east-1"

    # SSM-based remote collector — credentials resolved ON the automation instance
    # Password never leaves the automation instance or appears in SSM command history
    assert inp.live_config is not None
    assert cred_mgr is not None
    ssm = SSMExecutor(cred_mgr, inp.live_config.automation_instance_id)
    db = MySQLRemoteCollector(
        ssm=ssm,
        host=inp.cluster_endpoint,
        port=inp.port,
        database=inp.database_name,
        secret_arn=inp.live_config.secret_arn,
        region=region,
    )

    # Stage 1: metadata
    meta_raw = ckpt.load_or_run(
        "metadata",
        lambda: {
            "version": db.get_version(),
            "db_size_gb": db.get_database_size_gb(),
        },
    )

    # Stage 2: schema
    schema_raw = ckpt.load_or_run("schema", lambda: _collect_schema_raw(db, inp.collection_options))

    # Stage 3: queries from performance_schema
    queries_raw = ckpt.load_or_run(
        "queries", lambda: _collect_queries_raw(db, inp.collection_options)
    )

    # Stage 4: AWS metrics (RDS, CloudWatch, PI, DI)
    aws_raw = ckpt.load_or_run("aws_metrics", lambda: _collect_aws_raw(inp, cred_mgr))

    # Assemble
    tables = _build_tables(
        schema_raw["tables"] if isinstance(schema_raw, dict) else schema_raw, inp.database_name
    )
    schema_views = (
        _build_views(schema_raw.get("views", [])) if isinstance(schema_raw, dict) else None
    )
    schema_procs = (
        _build_procedures(schema_raw.get("procedures", []))
        if isinstance(schema_raw, dict)
        else None
    )
    schema_triggers = (
        _build_triggers(schema_raw.get("triggers", [])) if isinstance(schema_raw, dict) else None
    )
    live_queries = _build_queries(queries_raw)
    aws_queries = aws_raw.get("query_patterns", [])
    queries = _merge_into_queries(live_queries, aws_queries)
    # Prefer DB-direct global_stats over PI counters (LIVE mode has DB access)
    global_stats = schema_raw.get("global_stats", {}) if isinstance(schema_raw, dict) else {}
    pi_counters = global_stats or aws_raw.get("pi_counters", {})
    cw_raw = aws_raw.get("cloudwatch", {})
    em_data = aws_raw.get("enhanced_monitoring")
    _enrich_patterns_from_pi_and_cw(queries, pi_counters, cw_raw, em_data)
    rds_meta = _build_rds_metadata(aws_raw.get("rds_metadata"))
    cw_metrics = _build_cloudwatch(cw_raw)
    metrics = _build_metrics(queries, cw_metrics)

    return _build_output(
        inp,
        start,
        version=meta_raw["version"],
        db_size=meta_raw["db_size_gb"],
        tables=tables,
        queries=queries,
        metrics=metrics,
        rds_meta=rds_meta,
        views=schema_views,
        procedures=schema_procs,
        triggers=schema_triggers,
    )


# ---------------------------------------------------------------------------
# DDL mode
# ---------------------------------------------------------------------------


def _collect_ddl(inp: CollectorInput, ckpt) -> CollectorOutputContract:
    start = time.monotonic()
    region = inp.aws_config.region if inp.aws_config else "us-east-1"
    cred_mgr = _build_cred_mgr(inp)

    # Stage 1: DDL schema
    schema_raw = ckpt.load_or_run("ddl_schema", lambda: _parse_ddl_raw(inp, region))

    # Stage 2: AWS metrics
    aws_raw = ckpt.load_or_run("aws_metrics", lambda: _collect_aws_raw(inp, cred_mgr))

    tables = _build_tables_from_ddl(schema_raw, inp.database_name)
    aws_queries = aws_raw.get("query_patterns", [])
    queries = _build_queries_from_aws(aws_queries)
    pi_counters = aws_raw.get("pi_counters", {})
    cw_raw = aws_raw.get("cloudwatch", {})
    em_data = aws_raw.get("enhanced_monitoring")
    _enrich_patterns_from_pi_and_cw(queries, pi_counters, cw_raw, em_data)
    rds_meta = _build_rds_metadata(aws_raw.get("rds_metadata"))
    cw_metrics = _build_cloudwatch(cw_raw)
    metrics = _build_metrics(queries, cw_metrics)

    return _build_output(
        inp,
        start,
        version=aws_raw.get("engine_version", "unknown"),
        db_size=None,
        tables=tables,
        queries=queries,
        metrics=metrics,
        rds_meta=rds_meta,
    )


# ---------------------------------------------------------------------------
# Offline mode
# ---------------------------------------------------------------------------


def _collect_offline(inp: CollectorInput, ckpt) -> CollectorOutputContract:
    start = time.monotonic()
    region = inp.aws_config.region if inp.aws_config else "us-east-1"
    cred_mgr = _build_cred_mgr(inp)
    assert inp.offline_config is not None

    from src.tools.database.offline_parser import fetch_offline_json, parse_offline_collection

    # Stage 1: parse offline JSON
    parsed = ckpt.load_or_run(
        "offline_schema",
        lambda: parse_offline_collection(
            fetch_offline_json(
                bucket=inp.offline_config.s3_bucket,
                key=inp.offline_config.s3_key,
                region=region,
            )
        ),
    )

    # Stage 2: AWS metrics
    aws_raw = ckpt.load_or_run("aws_metrics", lambda: _collect_aws_raw(inp, cred_mgr))

    # Use the database name from the offline JSON (metadata.database_name) rather than
    # the user-supplied inp.database_name. The offline JSON contains the real database
    # name used to prefix table IDs (e.g. "forum_db.users"). Using inp.database_name
    # here would create a mismatch (e.g. "mydb.users") that breaks downstream agents.
    offline_meta = parsed.get("metadata", {})
    actual_db_name = (
        offline_meta.get("database_name") if isinstance(offline_meta, dict) else None
    ) or inp.database_name

    tables = _build_tables(parsed["tables"], actual_db_name)
    views = _build_views(parsed.get("views", []))
    procedures = _build_procedures(parsed.get("procedures", []))
    triggers = _build_triggers(parsed.get("triggers", []))

    live_queries = _build_queries(parsed.get("queries", []))
    aws_queries = aws_raw.get("query_patterns", [])
    queries = _merge_into_queries(live_queries, aws_queries)

    global_stats = parsed.get("global_stats", {})
    pi_counters = global_stats or aws_raw.get("pi_counters", {})
    cw_raw = aws_raw.get("cloudwatch", {})
    em_data = aws_raw.get("enhanced_monitoring")
    _enrich_patterns_from_pi_and_cw(queries, pi_counters, cw_raw, em_data)
    rds_meta = _build_rds_metadata(aws_raw.get("rds_metadata"))
    cw_metrics = _build_cloudwatch(cw_raw)
    metrics = _build_metrics(queries, cw_metrics)

    offline_meta = parsed.get("metadata", {})
    return _build_output(
        inp,
        start,
        version=offline_meta.get("version") or aws_raw.get("engine_version", "unknown"),
        db_size=offline_meta.get("database_size_gb"),
        tables=tables,
        queries=queries,
        metrics=metrics,
        rds_meta=rds_meta,
        views=views,
        procedures=procedures,
        triggers=triggers,
    )


# ---------------------------------------------------------------------------
# Raw data collectors (return serializable dicts for checkpointing)
# ---------------------------------------------------------------------------


def _collect_schema_raw(db: MySQLRemoteCollector, opts) -> dict:
    """Collect raw schema data via SSM as serializable dict."""
    raw_tables = db.collect_tables()
    tables = []
    for t in raw_tables:
        name = t["table_name"]
        sample = None
        if opts.collect_sample_data:
            try:
                sample = db.collect_sample_data(name, limit=opts.sample_row_count)
            except Exception:
                logger.warning("Failed to collect sample data for %s", name)
        tables.append(
            {
                **t,
                "columns": db.collect_columns(name),
                "indexes": db.collect_indexes(name),
                "foreign_keys": db.collect_foreign_keys(name),
                "primary_key": db.collect_primary_key(name),
                "sample_data": sample,
            }
        )

    views = []
    procedures = []
    triggers = []
    try:
        views = db.collect_views()
    except Exception:
        logger.warning("Failed to collect views")
    try:
        procedures = db.collect_procedures()
    except Exception:
        logger.warning("Failed to collect procedures")
    try:
        triggers = db.collect_triggers()
    except Exception:
        logger.warning("Failed to collect triggers")

    global_stats = {}
    try:
        global_stats = db.collect_global_stats()
    except Exception:
        logger.warning("Failed to collect global stats")

    return {
        "tables": tables,
        "views": views,
        "procedures": procedures,
        "triggers": triggers,
        "global_stats": global_stats,
    }


def _collect_queries_raw(db: MySQLRemoteCollector, opts) -> list[dict]:
    if not opts.collect_query_patterns:
        return []
    try:
        return db.collect_query_patterns()
    except Exception:
        logger.warning("Failed to collect query patterns")
        return []


def _parse_ddl_raw(inp, region) -> list[dict]:
    ddl_text = fetch_ddl_from_s3(
        bucket=inp.ddl_config.s3_bucket,
        key=inp.ddl_config.s3_key,
        region=region,
    )
    return parse_ddl(ddl_text, database_name=inp.database_name)


def _collect_aws_raw(inp, cred_mgr) -> dict:
    """Collect all AWS data as a single serializable dict."""
    result: dict = {}
    if not inp.aws_config or not cred_mgr:
        return result

    inst_id = inp.aws_config.db_instance_identifier or inp.aws_config.db_cluster_identifier
    if not inst_id:
        return result

    # RDS metadata
    rds_raw = get_rds_instance_metadata(cred_mgr, inst_id)
    if rds_raw.get("available"):
        result["rds_metadata"] = rds_raw
        result["engine_version"] = rds_raw.get("engine_version")

    # CloudWatch
    if inp.aws_config.collect_cloudwatch_metrics:
        cw_raw = get_cloudwatch_metrics(cred_mgr, inst_id, days=inp.aws_config.cloudwatch_days)
        if cw_raw.get("available"):
            cw_raw.pop("available", None)
            cw_raw.pop("error", None)
            result["cloudwatch"] = cw_raw

    # PI queries (PRIMARY) — needs DbiResourceId, not instance identifier
    pi_queries = []
    if inp.aws_config.collect_performance_insights:
        pi_resource_id = rds_raw.get("resource_arn") if rds_raw.get("available") else inst_id
        pi_raw = get_performance_insights_queries(
            cred_mgr, pi_resource_id, days=inp.aws_config.performance_insights_days
        )
        if pi_raw.get("available"):
            pi_queries = pi_raw.get("query_patterns", [])

    # Database Insights (FALLBACK — only if PI returned nothing)
    if inp.aws_config.collect_database_insights and not pi_queries:
        di_raw = get_database_insights(cred_mgr, inst_id, days=inp.aws_config.cloudwatch_days)
        if di_raw.get("available"):
            pi_queries = di_raw.get("query_patterns", [])

    if pi_queries:
        result["query_patterns"] = pi_queries

    # PI counter metrics (cache, temp tables — maps to cross-DB fields)
    pi_resource_id = rds_raw.get("resource_arn") if rds_raw.get("available") else inst_id
    pi_counters = get_pi_counter_metrics(
        cred_mgr, pi_resource_id, days=inp.aws_config.cloudwatch_days
    )
    if pi_counters.get("available"):
        pi_counters.pop("available", None)
        pi_counters.pop("error", None)
        result["pi_counters"] = pi_counters

    # Enhanced Monitoring (CPU per query)
    em = get_enhanced_monitoring_cpu(cred_mgr, pi_resource_id)
    if em.get("available"):
        result["enhanced_monitoring"] = em

    return result


# ---------------------------------------------------------------------------
# Model builders (from raw checkpoint data)
# ---------------------------------------------------------------------------


def _build_tables(schema_raw: list[dict], db_name: str) -> list[Table]:
    tables = []
    for t in schema_raw:
        name = t["table_name"]
        columns = _build_columns(t["columns"])
        indexes = [
            Index(
                index_name=i["index_name"],
                columns=i["columns"],
                is_unique=i["is_unique"],
                is_primary=i["is_primary"],
                index_type=i.get("index_type", "btree"),
            )
            for i in t.get("indexes", [])
        ] or None
        fks = [
            ForeignKey(
                constraint_name=fk["constraint_name"],
                columns=fk["columns"],
                referenced_table=fk["referenced_table"],
                referenced_columns=fk["referenced_columns"],
                on_delete=fk.get("on_delete"),
                on_update=fk.get("on_update"),
            )
            for fk in t.get("foreign_keys", [])
        ] or None

        tables.append(
            Table(
                table_id=f"{db_name}.{name}",
                table_name=name,
                schema_name=db_name,
                row_count=max(t.get("row_count") or 0, 0),
                size_mb=float(t.get("data_size_mb") or 0) + float(t.get("index_size_mb") or 0),
                columns=columns,
                indexes=indexes,
                primary_key=t.get("primary_key") or None,
                foreign_keys=fks,
                sample_data=t.get("sample_data"),
            )
        )
    return tables


def _build_tables_from_ddl(raw_tables: list[dict], db_name: str) -> list[Table]:
    tables = []
    for t in raw_tables:
        columns = _build_columns(t["columns"])
        indexes = [
            Index(
                index_name=i["index_name"],
                columns=i["columns"],
                is_unique=i["is_unique"],
                is_primary=i["is_primary"],
                index_type=i.get("index_type", "btree"),
            )
            for i in t.get("indexes", [])
        ] or None
        fks = [
            ForeignKey(
                constraint_name=fk["constraint_name"],
                columns=fk["columns"],
                referenced_table=fk["referenced_table"],
                referenced_columns=fk["referenced_columns"],
                on_delete=fk.get("on_delete"),
                on_update=fk.get("on_update"),
            )
            for fk in t.get("foreign_keys", [])
        ] or None
        tables.append(
            Table(
                table_id=t["table_id"],
                table_name=t["table_name"],
                schema_name=db_name,
                row_count=0,
                size_mb=0,
                columns=columns,
                indexes=indexes,
                primary_key=t.get("primary_key") or None,
                foreign_keys=fks,
            )
        )
    return tables


def _build_columns(raw: list[dict]) -> list[Column]:
    return [
        Column(
            column_name=c["column_name"],
            ordinal_position=c.get("ordinal_position"),
            data_type=c.get("column_type") or c.get("data_type", ""),
            normalized_data_type=_TYPE_MAP.get(c.get("data_type", "")),
            max_length=c.get("max_length"),
            nullable=c.get("is_nullable", "YES") == "YES",
            default_value=c.get("column_default"),
            is_auto_increment="auto_increment" in (c.get("extra") or ""),
        )
        for c in raw
    ]


def _build_queries(raw: list[dict]) -> Queries:
    if not raw:
        return Queries(query_patterns=[])
    patterns = [
        QueryPattern(
            query_id=p["query_id"],
            query_text=p["query_text"],
            query_type=QueryType(
                "INSERT" if p.get("query_type") == "REPLACE" else (p.get("query_type") or "SELECT")
            ),
            frequency_per_hour=p["frequency_per_hour"],
            calls_per_second=p.get("calls_per_second"),
            tables_accessed=p["tables_accessed"] or ["unknown"],
            execution_time_ms_avg=p.get("execution_time_ms_avg"),
            execution_time_ms_min=p.get("execution_time_ms_min"),
            execution_time_ms_max=p.get("execution_time_ms_max"),
            execution_time_ms_p50=p.get("execution_time_ms_p50"),
            execution_time_ms_p95=p.get("execution_time_ms_p95"),
            execution_time_ms_p99=p.get("execution_time_ms_p99"),
            total_time_ms=p.get("total_time_ms"),
            rows_returned_avg=p.get("rows_returned_avg"),
            rows_returned_p95=p.get("rows_returned_p95"),
            rows_examined_avg=p.get("rows_examined_avg"),
            rows_affected_avg=p.get("rows_affected_avg"),
            full_table_scans=p.get("full_table_scans"),
            range_scans=p.get("range_scans"),
            queries_without_index=p.get("queries_without_index"),
            queries_with_bad_index=p.get("queries_with_bad_index"),
            lock_time_ms=p.get("lock_time_ms"),
            lock_time_pct=p.get("lock_time_pct"),
            filter_columns=p.get("filter_columns"),
            sort_columns=p.get("sort_columns"),
            has_joins=p.get("has_joins"),
            join_count=p.get("join_count"),
            scan_efficiency_pct=p.get("scan_efficiency_pct"),
            has_aggregations=p.get("has_aggregations"),
            has_subqueries=p.get("has_subqueries"),
            has_text_search=p.get("has_text_search"),
            text_search_type=p.get("text_search_type"),
            has_time_range_filter=p.get("has_time_range_filter"),
            errors=p.get("errors"),
            warnings=p.get("warnings"),
            first_seen=p.get("first_seen"),
            last_seen=p.get("last_seen"),
        )
        for p in raw
    ]
    return Queries(
        query_patterns=patterns,
        total_queries_analyzed=len(patterns),
        query_log_source=QueryLogSource.performance_schema,
    )


def _build_queries_from_aws(aws_patterns: list[dict]) -> Queries:
    if not aws_patterns:
        return Queries(query_patterns=[])
    patterns = [_dict_to_query_pattern(p) for p in aws_patterns]
    return Queries(
        query_patterns=patterns,
        total_queries_analyzed=len(patterns),
        query_log_source=QueryLogSource.performance_insights,
    )


def _merge_into_queries(live_queries: Queries, aws_patterns: list[dict]) -> Queries:
    if not aws_patterns:
        return live_queries
    existing = {p.query_id: p for p in live_queries.query_patterns}
    for aq in aws_patterns:
        qid = aq["query_id"]
        if qid in existing:
            p = existing[qid]
            # PI has full SQL text — replace truncated performance_schema text
            pi_text = aq.get("query_text", "")
            if pi_text and len(pi_text) > len(p.query_text):
                p.query_text = pi_text
            if p.calls_per_second is None and aq.get("calls_per_second"):
                p.calls_per_second = aq["calls_per_second"]
            if p.db_load_contribution_percent is None and aq.get("db_load_contribution_percent"):
                p.db_load_contribution_percent = aq["db_load_contribution_percent"]
        else:
            existing[qid] = _dict_to_query_pattern(aq)
    return Queries(
        query_patterns=list(existing.values()),
        total_queries_analyzed=len(existing),
        query_log_source=live_queries.query_log_source or QueryLogSource.performance_schema,
    )


def _dict_to_query_pattern(d: dict) -> QueryPattern:
    query_text = d.get("query_text", "")
    return QueryPattern(
        query_id=d["query_id"],
        query_text=query_text,
        query_type=d.get("query_type"),
        frequency_per_hour=d.get("frequency_per_hour", 0),
        calls_per_second=d.get("calls_per_second"),
        tables_accessed=d.get("tables_accessed") or ["unknown"],
        execution_time_ms_avg=d.get("execution_time_ms_avg"),
        total_time_ms=d.get("total_time_ms"),
        rows_affected_avg=d.get("rows_affected_avg"),
        db_load_contribution_percent=d.get("db_load_contribution_percent"),
        has_joins=d.get("has_joins"),
        has_aggregations=d.get("has_aggregations"),
        has_subqueries=d.get("has_subqueries"),
        has_text_search=d.get("has_text_search") or _detect_text_search(query_text),
        text_search_type=d.get("text_search_type") or _detect_text_search_type(query_text),
        has_time_range_filter=d.get("has_time_range_filter") or _detect_time_range(query_text),
        wait_events=d.get("wait_events"),
    )


def _detect_text_search(sql: str) -> bool:
    """Detect text search in query text (works for all engines, parameterized or not)."""
    import re

    if re.search(r"\b(MATCH\s*\(|to_tsvector|to_tsquery|@@)\b", sql, re.I):
        return True
    if re.search(r"\b[I]?LIKE\s+('%.+|[\?\$])", sql, re.I):
        return True
    return False


def _detect_text_search_type(sql: str) -> str | None:
    import re

    if re.search(r"\b(MATCH\s*\()", sql, re.I):
        return "fulltext"
    if re.search(r"\b(to_tsvector|to_tsquery|@@)\b", sql, re.I):
        return "tsvector"
    if re.search(r"\s%\s", sql):
        return "trigram"
    if re.search(r"\b[I]?LIKE\s+('%.+|[\?\$])", sql, re.I):
        return "like_wildcard"
    return None


def _detect_time_range(sql: str) -> bool:
    import re

    if re.search(
        r"\b(NOW\s*\(\s*\)|CURRENT_TIMESTAMP|CURRENT_DATE|CURDATE\s*\(\s*\)|now\s*\(\s*\))\s*[-+]\s*(INTERVAL|[\?\$])",
        sql,
        re.I,
    ):
        return True
    if re.search(r"\bBETWEEN\s+'?\d{4}-\d{2}-\d{2}", sql, re.I):
        return True
    if re.search(r"[<>]=?\s+'?\d{4}-\d{2}-\d{2}", sql, re.I):
        return True
    if re.search(
        r"\b(date|time|created|updated|timestamp)\w*\b\s*(BETWEEN|[<>]=?)\s*[\?\$]", sql, re.I
    ):
        return True
    return False


def _build_rds_metadata(raw: dict | None) -> RDSInstanceMetadata | None:
    if not raw or not raw.get("available"):
        return None
    clean = {
        k: v
        for k, v in raw.items()
        if k not in ("available", "engine", "engine_version", "error", "resource_arn")
    }
    return RDSInstanceMetadata(**clean)


def _build_cloudwatch(raw: dict | None) -> RDSCloudWatchMetrics | None:
    if not raw:
        return None
    kwargs = {}
    for key, val in raw.items():
        if isinstance(val, dict):
            if key == "free_storage_space_gb":
                kwargs[key] = val.get("avg")
            else:
                kwargs[key] = MetricStats(**val)
        else:
            kwargs[key] = val
    return RDSCloudWatchMetrics(**kwargs)


def _enrich_patterns_from_pi_and_cw(
    queries: Queries, pi_counters: dict, cw_raw: dict, em_data: dict | None = None
):
    """
    Enrich query patterns with instance-level metrics from PI counters and CloudWatch.
    These are instance-wide values applied to all patterns (not per-query).
    """
    if not queries.query_patterns:
        return

    # PI counter metrics → cross-DB fields
    cache_hit = pi_counters.get("cache_hit_ratio_pct")
    blks_hit = pi_counters.get("buffer_pool_hits")
    blks_read = pi_counters.get("buffer_pool_reads_from_disk")
    logical_reads = pi_counters.get("buffer_pool_read_requests")
    tmp_disk = pi_counters.get("tmp_disk_tables")
    tmp_total = pi_counters.get("tmp_tables")

    # CloudWatch → io times and read/write ratio
    read_lat = cw_raw.get("read_latency_ms", {})
    write_lat = cw_raw.get("write_latency_ms", {})
    read_iops = cw_raw.get("read_iops", {})
    write_iops = cw_raw.get("write_iops", {})

    io_read_ms = read_lat.get("avg") if isinstance(read_lat, dict) else None
    io_write_ms = write_lat.get("avg") if isinstance(write_lat, dict) else None
    r_iops = read_iops.get("avg", 0) if isinstance(read_iops, dict) else 0
    w_iops = write_iops.get("avg", 0) if isinstance(write_iops, dict) else 0
    rw_ratio = round(r_iops / (r_iops + w_iops) * 100, 1) if (r_iops + w_iops) > 0 else None

    for p in queries.query_patterns:
        if p.cache_hit_ratio_pct is None and cache_hit is not None:
            p.cache_hit_ratio_pct = round(cache_hit, 2)
        if p.shared_blks_hit is None and blks_hit is not None:
            p.shared_blks_hit = int(blks_hit)
        if p.shared_blks_read is None and blks_read is not None:
            p.shared_blks_read = int(blks_read)
        if p.avg_logical_reads is None and logical_reads is not None:
            p.avg_logical_reads = round(logical_reads, 2)
        if p.avg_physical_reads is None and blks_read is not None:
            p.avg_physical_reads = round(blks_read, 2)
        if p.io_read_time_ms is None and io_read_ms is not None:
            p.io_read_time_ms = round(io_read_ms, 4)
        if p.io_write_time_ms is None and io_write_ms is not None:
            p.io_write_time_ms = round(io_write_ms, 4)
        if p.temp_blocks_read is None and tmp_disk is not None:
            p.temp_blocks_read = int(tmp_disk)
        if p.temp_blocks_written is None and tmp_total is not None:
            p.temp_blocks_written = int(tmp_total)
        if p.read_write_ratio_pct is None and rw_ratio is not None:
            p.read_write_ratio_pct = rw_ratio

    # Enhanced Monitoring: estimate avg_cpu_time_ms per query
    # Formula: (cpu_pct/100) * num_vcpus * 1000ms * interval / total_queries_in_interval
    if em_data and em_data.get("available"):
        avg_cpu_pct = em_data.get("avg_cpu_pct", 0)
        num_vcpus = em_data.get("num_vcpus", 1)
        total_qps = sum(p.calls_per_second or 0 for p in queries.query_patterns)
        if total_qps > 0:
            # Total CPU ms/sec used by mysqld = cpu_pct/100 * vcpus * 1000
            cpu_ms_per_sec = (avg_cpu_pct / 100) * num_vcpus * 1000
            avg_cpu_per_query = cpu_ms_per_sec / total_qps
            for p in queries.query_patterns:
                if p.avg_cpu_time_ms is None:
                    p.avg_cpu_time_ms = round(avg_cpu_per_query, 4)


def _build_metrics(queries: Queries, cw: RDSCloudWatchMetrics | None) -> Metrics:
    patterns = queries.query_patterns
    times = [p.execution_time_ms_avg for p in patterns if p.execution_time_ms_avg is not None]
    p95s = [p.execution_time_ms_p95 for p in patterns if p.execution_time_ms_p95 is not None]
    p99s = [p.execution_time_ms_p99 for p in patterns if p.execution_time_ms_p99 is not None]
    total_freq = sum(p.frequency_per_hour for p in patterns)

    perf = PerformanceMetrics(
        avg_query_time_ms=sum(times) / len(times) if times else None,
        p50_query_time_ms=sorted(times)[len(times) // 2] if times else None,
        p95_query_time_ms=max(p95s) if p95s else None,
        p99_query_time_ms=max(p99s) if p99s else None,
        queries_per_second=total_freq / 3600 if total_freq else None,
    )

    # Enrich from CloudWatch data
    if cw:
        if cw.database_connections:
            perf.active_connections_avg = cw.database_connections.avg
            perf.active_connections_max = cw.database_connections.max
        if cw.read_iops:
            perf.read_iops_avg = cw.read_iops.avg
        if cw.write_iops:
            perf.write_iops_avg = cw.write_iops.avg
        if cw.network_receive_throughput_mbps and cw.network_transmit_throughput_mbps:
            perf.network_throughput_mbps_avg = (cw.network_receive_throughput_mbps.avg or 0) + (
                cw.network_transmit_throughput_mbps.avg or 0
            )

    return Metrics(performance_metrics=perf, rds_cloudwatch_metrics=cw)


# ---------------------------------------------------------------------------
# View / Procedure / Trigger builders
# ---------------------------------------------------------------------------


def _build_views(raw: list[dict]) -> list[View] | None:
    if not raw:
        return None
    return [
        View(
            view_id=f"{v.get('view_name', '')}",
            view_name=v.get("view_name", ""),
            definition=v.get("definition") or "",
            is_updatable=v.get("is_updatable", "NO") == "YES",
        )
        for v in raw
    ]


def _build_procedures(raw: list[dict]) -> list[Procedure] | None:
    if not raw:
        return None
    return [
        Procedure(
            procedure_id=p.get("routine_name", ""),
            procedure_name=p.get("routine_name", ""),
            procedure_type=ProcedureType.FUNCTION
            if p.get("routine_type") == "FUNCTION"
            else ProcedureType.PROCEDURE,
            definition=p.get("definition"),
            return_type=p.get("return_type"),
        )
        for p in raw
    ]


def _build_triggers(raw: list[dict]) -> list[Trigger] | None:
    if not raw:
        return None
    return [
        Trigger(
            trigger_id=t.get("trigger_name", ""),
            trigger_name=t.get("trigger_name", ""),
            table_id=t.get("table_name", ""),
            event_type=t.get("event_type", "INSERT"),
            timing=t.get("timing", "AFTER"),
            definition=t.get("definition"),
        )
        for t in raw
    ]


# ---------------------------------------------------------------------------
# Output builder
# ---------------------------------------------------------------------------


def _build_output(
    inp: CollectorInput,
    start: float,
    *,
    version,
    db_size,
    tables,
    queries,
    metrics,
    rds_meta,
    views=None,
    procedures=None,
    triggers=None,
) -> CollectorOutputContract:
    elapsed = time.monotonic() - start
    return CollectorOutputContract(
        job_id=inp.job_id,
        metadata=Metadata(
            collection_timestamp=datetime.now(UTC),
            collector_version=COLLECTOR_VERSION,
            collection_duration_seconds=round(elapsed, 2),
            source_database=SourceDatabase(
                engine=inp.engine.value,  # type: ignore[arg-type]
                version=version,
                hostname=inp.cluster_endpoint,
                database_name=inp.database_name,
                database_size_gb=db_size,
                deployment_type=DeploymentType.rds_instance if inp.aws_config else None,
                rds_instance_metadata=rds_meta,
            ),
        ),
        database_schema=Schema(
            tables=tables, views=views, procedures=procedures, triggers=triggers
        ),
        queries=queries,
        metrics=metrics,
    )


def _build_cred_mgr(inp: CollectorInput) -> AWSCredentialManager | None:
    if not inp.aws_config:
        return None
    return AWSCredentialManager(
        region=inp.aws_config.region,
        role_arn=inp.aws_config.assume_role_arn,
        external_id=inp.aws_config.external_id,
    )
