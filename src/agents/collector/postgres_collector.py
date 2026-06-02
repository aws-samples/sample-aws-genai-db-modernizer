"""
PostgreSQL Collector Orchestrator — reuses shared infrastructure from MySQL collector.

Same checkpoint-based idempotent collection, same AWS metrics, same S3 storage.
Only the DB-specific collection (schema, queries, global stats) differs.
"""

import logging
import time
from datetime import UTC, datetime

# Reuse shared AWS/checkpoint infrastructure from mysql_collector
from src.agents.collector.mysql_collector import (
    COLLECTOR_VERSION,
    CheckpointStore,
    NoopCheckpointStore,
    _build_cloudwatch,
    _build_cred_mgr,
    _build_rds_metadata,
    _build_tables_from_ddl,
    _collect_aws_raw,
    _collect_offline,
    _dict_to_query_pattern,
    _enrich_patterns_from_pi_and_cw,
    _parse_ddl_raw,
)
from src.contracts.collector_input import CollectionMode, CollectorInput
from src.contracts.collector_output import (
    CollectorOutputContract,
    Column,
    DeploymentType,
    ForeignKey,
    Index,
    Metadata,
    Metrics,
    NormalizedDataType,
    PerformanceMetrics,
    Procedure,
    Queries,
    QueryLogSource,
    QueryPattern,
    Schema,
    SourceDatabase,
    Table,
    Trigger,
    View,
)
from src.tools.aws.s3_storage import init_storage
from src.tools.aws.ssm_executor import SSMExecutor
from src.tools.database.postgres_tools import PostgreSQLRemoteCollector

logger = logging.getLogger(__name__)

_PG_TYPE_MAP: dict[str, NormalizedDataType] = {
    "integer": NormalizedDataType.integer,
    "bigint": NormalizedDataType.integer,
    "smallint": NormalizedDataType.integer,
    "serial": NormalizedDataType.integer,
    "bigserial": NormalizedDataType.integer,
    "numeric": NormalizedDataType.decimal,
    "real": NormalizedDataType.decimal,
    "double precision": NormalizedDataType.decimal,
    "money": NormalizedDataType.decimal,
    "character varying": NormalizedDataType.string,
    "character": NormalizedDataType.string,
    "varchar": NormalizedDataType.string,
    "char": NormalizedDataType.string,
    "text": NormalizedDataType.text,
    "boolean": NormalizedDataType.boolean,
    "date": NormalizedDataType.date,
    "timestamp without time zone": NormalizedDataType.timestamp,
    "timestamp with time zone": NormalizedDataType.timestamp,
    "time without time zone": NormalizedDataType.string,
    "bytea": NormalizedDataType.binary,
    "json": NormalizedDataType.json,
    "jsonb": NormalizedDataType.json,
    "uuid": NormalizedDataType.uuid,
    "xml": NormalizedDataType.xml,
    "inet": NormalizedDataType.string,
    "cidr": NormalizedDataType.string,
    "macaddr": NormalizedDataType.string,
    "array": NormalizedDataType.string,
}


def collect(input_contract: CollectorInput) -> CollectorOutputContract:
    """Entry point for PostgreSQL collection."""
    ckpt = _init_checkpoint_store(input_contract)

    if ckpt.exists("output"):
        logger.info("Job %s: returning cached output", input_contract.job_id)
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


def _collect_live(inp: CollectorInput, ckpt) -> CollectorOutputContract:
    start = time.monotonic()
    cred_mgr = _build_cred_mgr(inp)
    region = inp.aws_config.region if inp.aws_config else "us-east-1"

    assert inp.live_config is not None  # nosec B101 — type narrowing for mypy
    assert cred_mgr is not None  # nosec B101 — type narrowing for mypy
    ssm = SSMExecutor(cred_mgr, inp.live_config.automation_instance_id)
    db = PostgreSQLRemoteCollector(
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

    # Stage 3: queries
    queries_raw = ckpt.load_or_run(
        "queries", lambda: _collect_queries_raw(db, inp.collection_options)
    )

    # Stage 4: AWS metrics
    aws_raw = ckpt.load_or_run("aws_metrics", lambda: _collect_aws_raw(inp, cred_mgr))

    # Assemble
    tables = _build_tables(schema_raw)
    views = _build_views(schema_raw.get("views", []))
    procs = _build_procedures(schema_raw.get("procedures", []))
    triggers = _build_triggers(schema_raw.get("triggers", []))

    queries = _build_queries(queries_raw)
    aws_queries = aws_raw.get("query_patterns", [])
    if aws_queries:
        queries = _merge_queries(queries, aws_queries)

    global_stats = schema_raw.get("global_stats", {})
    pi_counters = global_stats or aws_raw.get("pi_counters", {})
    cw_raw = aws_raw.get("cloudwatch", {})
    em_data = aws_raw.get("enhanced_monitoring")
    _enrich_patterns_from_pi_and_cw(queries, pi_counters, cw_raw, em_data)
    _enrich_table_scans(queries, schema_raw.get("tables", []))

    rds_meta = _build_rds_metadata(aws_raw.get("rds_metadata"))
    cw_metrics = _build_cloudwatch(cw_raw)
    metrics = _build_metrics(queries, cw_metrics)

    elapsed = time.monotonic() - start
    version_str = meta_raw["version"]
    # Extract short version from "PostgreSQL 14.17 on ..."
    if "PostgreSQL" in version_str:
        version_str = version_str.split()[1] if len(version_str.split()) > 1 else version_str

    return CollectorOutputContract(
        job_id=inp.job_id,
        metadata=Metadata(
            collection_timestamp=datetime.now(UTC),
            collector_version=COLLECTOR_VERSION,
            collection_duration_seconds=round(elapsed, 2),
            source_database=SourceDatabase(
                engine="postgresql",  # type: ignore[arg-type]
                version=version_str,
                hostname=inp.cluster_endpoint,
                database_name=inp.database_name,
                database_size_gb=meta_raw["db_size_gb"],
                deployment_type=DeploymentType.rds_instance if inp.aws_config else None,
                rds_instance_metadata=rds_meta,
            ),
        ),
        database_schema=Schema(tables=tables, views=views, procedures=procs, triggers=triggers),
        queries=queries,
        metrics=metrics,
    )


# ---------------------------------------------------------------------------
# DDL mode
# ---------------------------------------------------------------------------


def _collect_ddl(inp: CollectorInput, ckpt) -> CollectorOutputContract:
    start = time.monotonic()
    region = inp.aws_config.region if inp.aws_config else "us-east-1"
    cred_mgr = _build_cred_mgr(inp)

    schema_raw = ckpt.load_or_run("ddl_schema", lambda: _parse_ddl_raw(inp, region))
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

    elapsed = time.monotonic() - start
    return CollectorOutputContract(
        job_id=inp.job_id,
        metadata=Metadata(
            collection_timestamp=datetime.now(UTC),
            collector_version=COLLECTOR_VERSION,
            collection_duration_seconds=round(elapsed, 2),
            source_database=SourceDatabase(
                engine="postgresql",  # type: ignore[arg-type]
                version=aws_raw.get("engine_version", "unknown"),
                hostname=inp.cluster_endpoint,
                database_name=inp.database_name,
                database_size_gb=None,
                deployment_type=DeploymentType.rds_instance if inp.aws_config else None,
                rds_instance_metadata=rds_meta,
            ),
        ),
        database_schema=Schema(tables=tables),
        queries=queries,
        metrics=metrics,
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


# ---------------------------------------------------------------------------
# Raw collectors
# ---------------------------------------------------------------------------


def _collect_schema_raw(db: PostgreSQLRemoteCollector, opts) -> dict:
    raw_tables = db.collect_tables()
    tables = []
    for t in raw_tables:
        name = t["table_name"]
        cols = db.collect_columns(name)
        cardinality = db.collect_column_cardinality(name)
        for c in cols:
            c["cardinality"] = cardinality.get(c["column_name"])

        sample = None
        if opts.collect_sample_data:
            try:
                sample = db.collect_sample_data(name, limit=opts.sample_row_count)
            except Exception:
                logger.warning("Failed to collect sample data for %s", name)

        tables.append(
            {
                **t,
                "columns": cols,
                "indexes": db.collect_indexes(name),
                "foreign_keys": db.collect_foreign_keys(name),
                "primary_key": db.collect_primary_key(name),
                "sample_data": sample,
            }
        )

    views, procedures, triggers = [], [], []
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


def _collect_queries_raw(db: PostgreSQLRemoteCollector, opts) -> list[dict]:
    if not opts.collect_query_patterns:
        return []
    try:
        return db.collect_query_patterns()
    except Exception:
        logger.warning("Failed to collect query patterns")
        return []


# ---------------------------------------------------------------------------
# Model builders
# ---------------------------------------------------------------------------


def _build_tables(schema_raw: dict) -> list[Table]:
    tables = []
    for t in schema_raw.get("tables", []):
        name = t["table_name"]
        schema_name = t.get("schema_name", "public")
        columns = [
            Column(
                column_name=c["column_name"],
                ordinal_position=c.get("ordinal_position"),
                data_type=c.get("udt_name") or c.get("data_type", ""),
                normalized_data_type=_PG_TYPE_MAP.get(c.get("data_type", "")),
                max_length=c.get("max_length"),
                nullable=c.get("is_nullable", "YES") == "YES",
                default_value=c.get("column_default"),
                is_auto_increment=("nextval" in str(c.get("column_default") or "")),
                cardinality=c.get("cardinality"),
            )
            for c in t.get("columns", [])
        ]
        indexes = [
            Index(
                index_name=i["index_name"],
                columns=i["columns"],
                is_unique=i["is_unique"],
                is_primary=i.get("is_primary", False),
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
                table_id=f"{schema_name}.{name}",
                table_name=name,
                schema_name=schema_name,
                row_count=t.get("row_count") or 0,
                size_mb=float(t.get("data_size_mb") or 0) + float(t.get("index_size_mb") or 0),
                columns=columns,
                indexes=indexes,
                primary_key=t.get("primary_key") or None,
                foreign_keys=fks,
                sample_data=t.get("sample_data"),
            )
        )
    return tables


def _build_views(raw: list[dict]) -> list[View] | None:
    if not raw:
        return None
    return [
        View(
            view_id=v.get("view_name", ""),
            view_name=v.get("view_name", ""),
            definition=v.get("definition") or "",
        )
        for v in raw
    ]


def _build_procedures(raw: list[dict]) -> list[Procedure] | None:
    if not raw:
        return None
    from src.contracts.collector_output import ProcedureType

    return [
        Procedure(
            procedure_id=p.get("routine_name", ""),
            procedure_name=p.get("routine_name", ""),
            procedure_type=ProcedureType.FUNCTION
            if p.get("routine_type") == "FUNCTION"
            else ProcedureType.PROCEDURE,
            return_type=p.get("return_type"),
            language=p.get("language"),
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
        )
        for t in raw
    ]


def _build_queries(raw: list[dict]) -> Queries:
    if not raw:
        return Queries(query_patterns=[])
    patterns = [
        QueryPattern(
            query_id=p["query_id"],
            query_text=p["query_text"],
            query_type=p.get("query_type"),
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
            rows_affected_avg=p.get("rows_affected_avg"),
            rows_examined_avg=p.get("rows_examined_avg"),
            scan_efficiency_pct=p.get("scan_efficiency_pct"),
            filter_columns=p.get("filter_columns"),
            sort_columns=p.get("sort_columns"),
            has_joins=p.get("has_joins"),
            join_count=p.get("join_count"),
            has_aggregations=p.get("has_aggregations"),
            has_subqueries=p.get("has_subqueries"),
            has_text_search=p.get("has_text_search"),
            text_search_type=p.get("text_search_type"),
            has_time_range_filter=p.get("has_time_range_filter"),
            # PostgreSQL-specific
            cache_hit_ratio_pct=p.get("cache_hit_ratio_pct"),
            shared_blks_hit=p.get("shared_blks_hit"),
            shared_blks_read=p.get("shared_blks_read"),
            io_read_time_ms=p.get("io_read_time_ms"),
            io_write_time_ms=p.get("io_write_time_ms"),
            temp_blocks_read=p.get("temp_blocks_read"),
            temp_blocks_written=p.get("temp_blocks_written"),
        )
        for p in raw
    ]
    return Queries(
        query_patterns=patterns,
        total_queries_analyzed=len(patterns),
        query_log_source=QueryLogSource.pg_stat_statements,
    )


def _merge_queries(live: Queries, aws_patterns: list[dict]) -> Queries:
    existing = {p.query_id: p for p in live.query_patterns}
    for aq in aws_patterns:
        qid = aq["query_id"]
        if qid in existing:
            p = existing[qid]
            pi_text = aq.get("query_text", "")
            if pi_text and len(pi_text) > len(p.query_text):
                p.query_text = pi_text
            if p.db_load_contribution_percent is None and aq.get("db_load_contribution_percent"):
                p.db_load_contribution_percent = aq["db_load_contribution_percent"]
        else:
            existing[qid] = _dict_to_query_pattern(aq)
    return Queries(
        query_patterns=list(existing.values()),
        total_queries_analyzed=len(existing),
        query_log_source=live.query_log_source or QueryLogSource.pg_stat_statements,
    )


def _enrich_table_scans(queries: Queries, raw_tables: list[dict]):
    """Map per-table seq_scan/idx_scan to query patterns based on tables_accessed."""
    scan_map: dict[str, dict] = {}
    for t in raw_tables:
        name = t.get("table_name", "")
        scan_map[name] = {
            "seq_scan": int(t.get("full_table_scans") or 0),
            "idx_scan": int(t.get("index_scans") or 0),
        }

    for p in queries.query_patterns:
        if p.full_table_scans is not None:
            continue  # already set
        total_seq = 0
        total_idx = 0
        for tbl in p.tables_accessed:
            s = scan_map.get(tbl, {})
            total_seq += s.get("seq_scan", 0)
            total_idx += s.get("idx_scan", 0)
        if total_seq or total_idx:
            p.full_table_scans = total_seq
            p.range_scans = total_idx


def _build_metrics(queries: Queries, cw) -> Metrics:
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
