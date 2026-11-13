# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
SQL Server Collector Orchestrator — reuses shared infrastructure from MySQL collector.

Same checkpoint-based idempotent collection, same AWS metrics, same S3 storage.
Only the DB-specific collection (schema, queries, global stats) differs.

DDL mode in this sub-step (2B) inherits the MySQL DDL parser used by the
shared ``_parse_ddl_raw`` helper. T-SQL dialect support is added in a follow-up
sub-step that introduces a ``dialect`` parameter to ``parse_ddl``.
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
    _build_source_host,
    _build_tables_from_ddl,
    _collect_aws_raw,
    _collect_offline,
    _dict_to_query_pattern,
    _enrich_patterns_from_pi_and_cw,
    _live_os_stats,
    _parse_ddl_raw,
    ddl_routines_stage,
    trigger_shape_fields,
)
from src.agents.collector.routine_rows import (
    attach_routine_stats,
    dml_trigger,
    execution_stats,
    log_skipped_triggers,
    routine_stats_status,
)
from src.agents.collector.routine_sources import (
    body_fields,
    mark_body_readability,
    non_dml_triggers,
    routine_capture,
    routine_capture_model,
)
from src.contracts.collector_input import CollectionMode, CollectorInput
from src.contracts.collector_output import (
    CollectorOutputContract,
    Column,
    DeploymentType,
    ForeignKey,
    Index,
    IndexType,
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
    TriggerEventType,
    TriggerTiming,
    View,
)
from src.shared.contract_versioning import load_contract
from src.shared.query_capture import live_query_capture, query_stage_rows, statement_query_failed
from src.shared.row_counts import read_data_size_mb, read_row_count, read_size_mb
from src.shared.server_counters import live_server_counters, server_counters_model
from src.tools.aws.s3_storage import init_storage
from src.tools.aws.ssm_executor import SSMExecutor
from src.tools.database.sqlserver_max import max_type
from src.tools.database.sqlserver_tools import SQLServerRemoteCollector, _normalize_index_type
from src.tools.sql.view_lineage import apply_view_lineage

logger = logging.getLogger(__name__)


# SQL Server native type → normalized type. Native types come back as
# lowercase strings from sys.types (e.g. "nvarchar", "datetime2").
_SQLSERVER_TYPE_MAP: dict[str, NormalizedDataType] = {
    "tinyint": NormalizedDataType.integer,
    "smallint": NormalizedDataType.integer,
    "int": NormalizedDataType.integer,
    "bigint": NormalizedDataType.integer,
    "decimal": NormalizedDataType.decimal,
    "numeric": NormalizedDataType.decimal,
    "money": NormalizedDataType.decimal,
    "smallmoney": NormalizedDataType.decimal,
    # FLOAT/REAL are approximate, not exact numerics (#156): resolving them
    # as decimal would reach a target NUMERIC(n,0) that truncates every
    # fractional value.
    "float": NormalizedDataType.floating_point,
    "real": NormalizedDataType.floating_point,
    "char": NormalizedDataType.string,
    "varchar": NormalizedDataType.string,
    "nchar": NormalizedDataType.string,
    "nvarchar": NormalizedDataType.string,
    "text": NormalizedDataType.text,
    "ntext": NormalizedDataType.text,
    "bit": NormalizedDataType.boolean,
    "date": NormalizedDataType.date,
    "datetime": NormalizedDataType.timestamp,
    "datetime2": NormalizedDataType.timestamp,
    "smalldatetime": NormalizedDataType.timestamp,
    "datetimeoffset": NormalizedDataType.timestamp,
    "time": NormalizedDataType.string,
    "binary": NormalizedDataType.binary,
    "varbinary": NormalizedDataType.binary,
    "image": NormalizedDataType.binary,
    "uniqueidentifier": NormalizedDataType.uuid,
    "xml": NormalizedDataType.xml,
    "sql_variant": NormalizedDataType.string,
    "hierarchyid": NormalizedDataType.string,
    "geography": NormalizedDataType.string,
    "geometry": NormalizedDataType.string,
    "rowversion": NormalizedDataType.binary,
    "timestamp": NormalizedDataType.binary,  # SQL Server's "timestamp" is actually rowversion
}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def collect(input_contract: CollectorInput) -> CollectorOutputContract:
    """Entry point for SQL Server collection."""
    ckpt = _init_checkpoint_store(input_contract)

    if ckpt.exists("output"):
        logger.info("Job %s: returning cached output", input_contract.job_id)
        result: CollectorOutputContract = load_contract(
            CollectorOutputContract,
            ckpt.load("output"),
            artifact=f"collector checkpoint {input_contract.job_id}/output",
        )
        return result

    if input_contract.mode == CollectionMode.live:
        # #1875: a statement on a view is scored on the view's base tables.
        result = apply_view_lineage(_collect_live(input_contract, ckpt), "sqlserver")
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
        return CheckpointStore(
            cred_mgr, storage["bucket"], storage["collector_prefix"], inp.engine.value
        )
    except Exception:
        logger.warning("Failed to init S3 storage, running without checkpoints")
        return NoopCheckpointStore()


# ---------------------------------------------------------------------------
# Live mode
# ---------------------------------------------------------------------------


def _collect_live(inp: CollectorInput, ckpt) -> CollectorOutputContract:
    start = time.monotonic()
    cred_mgr = _build_cred_mgr(inp)
    region = inp.aws_config.region if inp.aws_config else "us-east-1"

    assert inp.live_config is not None  # nosec B101 — type narrowing for mypy
    assert cred_mgr is not None  # nosec B101 — type narrowing for mypy
    ssm = SSMExecutor(cred_mgr, inp.live_config.automation_instance_id)
    db = SQLServerRemoteCollector(
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
    # #1652: include_routine_bodies = False reads no body.
    include = inp.collection_options.include_routine_bodies
    schema_raw = ckpt.load_or_run("schema", lambda: _collect_schema_raw(db, include_bodies=include))

    # Stage 3: queries
    queries_raw = ckpt.load_or_run(
        "queries", lambda: _collect_queries_raw(db, inp.collection_options)
    )
    queries_raw, query_error = query_stage_rows(queries_raw)

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
    # #1197: the instance-wide buffer-cache hit ratio, kept once, not copied per query.
    metrics.source_host = _build_source_host(
        {"global_stats": pi_counters, "os_stats": _live_os_stats(schema_raw)}, rds_meta
    )

    elapsed = time.monotonic() - start
    version_str = _parse_sqlserver_version(meta_raw["version"])

    return CollectorOutputContract(
        job_id=inp.job_id,
        metadata=Metadata(
            collection_timestamp=datetime.now(UTC),
            collector_version=COLLECTOR_VERSION,
            collection_duration_seconds=round(elapsed, 2),
            source_database=SourceDatabase(
                engine="sqlserver",  # type: ignore[arg-type]
                version=version_str,
                hostname=inp.cluster_endpoint,
                database_name=inp.database_name,
                database_size_gb=meta_raw["db_size_gb"],
                deployment_type=DeploymentType.rds_instance if inp.aws_config else None,
                rds_instance_metadata=rds_meta,
            ),
            query_capture=live_query_capture(len(queries_raw), inp.collection_options, query_error),
            # #1991: the server's uptime or reset age, which the rate window check reads.
            server_counters=server_counters_model(live_server_counters(db)),
            # #1652: an older checkpoint's schema stage has none.
            routine_stats_source=routine_stats_status(schema_raw.get("routine_stats_status")),
            # #1933: how routine code was read (none in an older checkpoint).
            routine_capture=routine_capture_model(
                routine_capture(
                    schema_raw.get("routine_capture"),
                    listed=len(schema_raw.get("procedures") or [])
                    + len(schema_raw.get("triggers") or []),
                )
            ),
        ),
        database_schema=Schema(
            tables=tables,
            views=views,
            procedures=procs,
            triggers=triggers,
            # #2023: none today (sys.triggers is read on tables and views only), but a
            # row on no DML event is kept apart, never dropped.
            non_dml_triggers=non_dml_triggers(schema_raw.get("triggers", []), qualified=True),
        ),
        queries=queries,
        metrics=metrics,
    )


# ---------------------------------------------------------------------------
# DDL mode
# ---------------------------------------------------------------------------


def _collect_ddl(inp: CollectorInput, ckpt) -> CollectorOutputContract:
    """Collect schema from DDL text + AWS metrics for query patterns.

    Note: T-SQL dialect support in ``parse_ddl`` is added in a follow-up
    sub-step. Until then, this calls the shared MySQL-flavored parser, which
    handles common CREATE TABLE syntax but may misclassify SQL Server-specific
    constructs ([brackets], IDENTITY, nvarchar(MAX), uniqueidentifier).
    Same best-effort precedent as PostgreSQL DDL mode today.
    """
    start = time.monotonic()
    region = inp.aws_config.region if inp.aws_config else "us-east-1"
    cred_mgr = _build_cred_mgr(inp)

    schema_raw = ckpt.load_or_run("ddl_schema", lambda: _parse_ddl_raw(inp, region))
    # #1652: the procedures, functions and triggers the upload defines (bodies, no counts).
    routines_raw = ddl_routines_stage(inp, ckpt, region)
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
    # #1197: the instance-wide buffer-cache hit ratio, kept once, not copied per query.
    metrics.source_host = _build_source_host({"global_stats": pi_counters}, rds_meta)

    elapsed = time.monotonic() - start
    return CollectorOutputContract(
        job_id=inp.job_id,
        metadata=Metadata(
            collection_timestamp=datetime.now(UTC),
            collector_version=COLLECTOR_VERSION,
            collection_duration_seconds=round(elapsed, 2),
            source_database=SourceDatabase(
                engine="sqlserver",  # type: ignore[arg-type]
                version=aws_raw.get("engine_version", "unknown"),
                hostname=inp.cluster_endpoint,
                database_name=inp.database_name,
                database_size_gb=None,
                deployment_type=DeploymentType.rds_instance if inp.aws_config else None,
                rds_instance_metadata=rds_meta,
            ),
            routine_stats_source=routine_stats_status(routines_raw.get("routine_stats_status")),
        ),
        database_schema=Schema(
            tables=tables,
            procedures=_build_procedures(routines_raw.get("procedures") or []),
            triggers=_build_triggers(routines_raw.get("triggers") or []),
            non_dml_triggers=non_dml_triggers(routines_raw.get("triggers") or [], qualified=True),
        ),
        queries=queries,
        metrics=metrics,
    )


# ---------------------------------------------------------------------------
# Raw collectors (live mode)
# ---------------------------------------------------------------------------


def _collect_schema_raw(db: SQLServerRemoteCollector, include_bodies: bool = True) -> dict:
    raw_tables = db.collect_tables()
    tables = []
    for t in raw_tables:
        # SQL Server tables are addressed as 'schema.table' (default schema 'dbo')
        schema_name = t.get("schema_name", "dbo")
        table_name = t["table_name"]
        qualified = f"{schema_name}.{table_name}"
        cols = db.collect_columns(qualified)

        tables.append(
            {
                **t,
                "columns": cols,
                "indexes": db.collect_indexes(qualified),
                "foreign_keys": db.collect_foreign_keys(qualified),
                "primary_key": db.collect_primary_key(qualified),
            }
        )

    views, procedures, triggers = [], [], []
    try:
        views = db.collect_views()
    except Exception:
        logger.warning("Failed to collect views")
    try:
        procedures = db.collect_procedures(include_bodies=include_bodies)
    except Exception:
        logger.warning("Failed to collect procedures")
    try:
        triggers = db.collect_triggers(include_bodies=include_bodies)
    except Exception:
        logger.warning("Failed to collect triggers")
    # #1652: the execution counts the engine keeps, and how they were read.
    # #1933: a body the user cannot read is not_readable, with the reason; never fails.
    mark_body_readability("sqlserver", procedures, triggers)
    capture: dict | None = None
    try:
        capture = db.collect_routine_capture(include_bodies=include_bodies)
    except Exception:
        logger.warning("Failed to read which routine privileges the user holds")
    routine_status: dict | None = None
    try:
        routine_stats, routine_status = db.collect_routine_stats()
        attach_routine_stats(procedures, triggers, routine_stats)
    except Exception:
        logger.warning("Failed to collect routine stats")

    global_stats = {}
    try:
        global_stats = db.collect_global_stats()
    except Exception:
        logger.warning("Failed to collect global stats")

    os_stats: dict = {}
    try:
        os_stats = db.collect_os_stats()
    except Exception:
        logger.warning("Failed to collect host stats")

    return {
        "tables": tables,
        "views": views,
        "procedures": procedures,
        "triggers": triggers,
        "routine_stats_status": routine_status,
        "routine_capture": capture,
        "global_stats": global_stats,
        "os_stats": os_stats,
    }


def _collect_queries_raw(db: SQLServerRemoteCollector, opts) -> list[dict] | dict[str, str]:
    if not opts.collect_query_patterns:
        return []
    try:
        # #1908: the rates over the interval between two snapshots (0: one snapshot).
        return db.collect_query_patterns(
            min_executions=opts.min_executions,
            limit=opts.query_limit,
            rate_sample_seconds=opts.rate_sample_seconds,
        )
    except Exception as exc:
        logger.warning("Failed to collect query patterns")
        # #384: a failed query is not a complete capture of nothing.
        return statement_query_failed(exc)


# ---------------------------------------------------------------------------
# Model builders
# ---------------------------------------------------------------------------


def _build_tables(schema_raw: dict) -> list[Table]:
    tables = []
    for t in schema_raw.get("tables", []):
        name = t["table_name"]
        schema_name = t.get("schema_name", "dbo")

        columns = [
            Column(
                column_name=c["column_name"],
                ordinal_position=c.get("ordinal_position"),
                # (max) kept in the type (#2219): the contract has no -1 length.
                data_type=max_type(c.get("data_type"), c.get("max_length"), absent_is_max=False)
                or c.get("data_type", ""),
                normalized_data_type=_SQLSERVER_TYPE_MAP.get(str(c.get("data_type", "")).lower()),
                max_length=_normalize_max_length(c.get("max_length"), c.get("data_type", "")),
                numeric_precision=c.get("numeric_precision"),
                numeric_scale=c.get("numeric_scale"),
                nullable=str(c.get("is_nullable", "YES")).upper() in ("YES", "TRUE", "1"),
                default_value=c.get("column_default"),
                is_auto_increment=str(c.get("is_identity", "NO")).upper() == "YES",
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
                # Defensive normalization: even if the cached dict has a raw
                # SQL Server index type, coerce it to a valid IndexType enum value.
                index_type=IndexType(_normalize_index_type(i.get("index_type") or "btree")),
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
                # #1636: None when the source has no count, never a 0 standing in for it.
                **read_row_count(t, "sqlserver").fields(),
                size_mb=read_size_mb(t, "sqlserver"),
                data_size_mb=read_data_size_mb(t, "sqlserver"),
                columns=columns,
                indexes=indexes,
                primary_key=t.get("primary_key") or None,
                foreign_keys=fks,
            )
        )
    return tables


def _build_views(raw: list[dict]) -> list[View] | None:
    if not raw:
        return None
    return [
        View(
            view_id=f"{v.get('schema_name', 'dbo')}.{v.get('view_name', '')}",
            view_name=v.get("view_name", ""),
            definition=v.get("definition") or "",
        )
        for v in raw
    ]


def _build_procedures(raw: list[dict]) -> list[Procedure] | None:
    if not raw:
        return None
    from src.contracts.collector_output import ProcedureType

    out = []
    for p in raw:
        rtype = str(p.get("routine_type") or "").upper()
        if rtype.endswith("FUNCTION") or rtype == "FUNCTION":
            ptype = ProcedureType.FUNCTION
        else:
            ptype = ProcedureType.PROCEDURE
        out.append(
            Procedure(
                procedure_id=f"{p.get('schema_name', 'dbo')}.{p.get('routine_name', '')}",
                procedure_name=p.get("routine_name", ""),
                schema_name=p.get("schema_name") or "dbo",
                procedure_type=ptype,
                language=p.get("language") or "TSQL",
                # #1652: the body (None for an encrypted module) and the counts kept.
                definition=p.get("definition"),
                execution_stats=execution_stats(p),
                **body_fields(p),
            )
        )
    return out


def _build_triggers(raw: list[dict]) -> list[Trigger] | None:
    """Every DML trigger with its timing and events in any spelling (#1651); one with no
    DML event, no known timing or no table is skipped, as on the offline path."""
    if not raw:
        return None
    result = []
    skipped: list[dict] = []
    for t in raw:
        shape = dml_trigger(t)
        if shape is None:
            skipped.append(t)
            continue
        events, timing = shape
        result.append(
            Trigger(
                trigger_id=f"{t.get('schema_name', 'dbo')}.{t.get('trigger_name', '')}",
                trigger_name=t.get("trigger_name", ""),
                schema_name=t.get("schema_name") or "dbo",
                table_id=f"{t.get('schema_name', 'dbo')}.{t.get('table_name', '')}",
                event_type=TriggerEventType(events[0]),
                events=[TriggerEventType(e) for e in events],
                timing=TriggerTiming(timing),
                # #1652: orientation, state, body and the counts the engine keeps.
                **trigger_shape_fields(t),
                **body_fields(t),
            )
        )
    log_skipped_triggers(logger, skipped)
    return result or None


def _build_queries(raw: list[dict]) -> Queries:
    if not raw:
        return Queries(query_patterns=[])
    patterns = [_dict_to_query_pattern(p, source_engine="sqlserver") for p in raw]
    return Queries(
        query_patterns=patterns,
        total_queries_analyzed=len(patterns),
        query_log_source=QueryLogSource.dmv_query_stats,
    )


def _build_queries_from_aws(aws_patterns: list[dict]) -> Queries:
    if not aws_patterns:
        return Queries(query_patterns=[])
    patterns = [_dict_to_query_pattern(p, source_engine="sqlserver") for p in aws_patterns]
    return Queries(
        query_patterns=patterns,
        total_queries_analyzed=len(patterns),
        query_log_source=QueryLogSource.performance_insights,
    )


def _merge_queries(live: Queries, aws_patterns: list[dict]) -> Queries:
    """Merge AWS PI patterns into live DMV-collected queries.

    Patterns from PI that match by ``query_id`` enrich the live row
    (e.g. fill in full SQL text, ``db_load_contribution_percent``,
    ``wait_events``). PI-only patterns are appended.
    """
    by_id: dict[str, QueryPattern] = {p.query_id: p for p in live.query_patterns}
    for aws in aws_patterns:
        qid = aws.get("query_id")
        if not qid:
            continue
        if qid in by_id:
            existing = by_id[qid]
            # Prefer PI's fuller text if it's longer
            if len(str(aws.get("query_text") or "")) > len(existing.query_text or ""):
                existing.query_text = aws["query_text"]
            if aws.get("db_load_contribution_percent") is not None:
                existing.db_load_contribution_percent = aws["db_load_contribution_percent"]
            if aws.get("wait_events"):
                existing.wait_events = aws["wait_events"]
        else:
            by_id[qid] = _dict_to_query_pattern(aws, source_engine="sqlserver")
    return Queries(
        query_patterns=list(by_id.values()),
        total_queries_analyzed=len(by_id),
        query_log_source=live.query_log_source or QueryLogSource.dmv_query_stats,
    )


def _enrich_table_scans(queries: Queries, raw_tables: list[dict]) -> None:
    """Best-effort table scan stats from sys.dm_db_index_usage_stats.

    Each query pattern that touches a table picks up the table's scan/seek
    counts. Multi-table queries get aggregated counts.
    """
    scan_map: dict[str, dict] = {}
    for t in raw_tables:
        name = t.get("table_name", "")
        schema = t.get("schema_name", "dbo")
        entry = {
            "seq_scan": int(t.get("full_table_scans") or 0),
            "idx_scan": int(t.get("index_scans") or 0),
        }
        # Index by both bare and qualified names so tables_accessed parsing
        # matches whichever form the SQL used.
        scan_map[name] = entry
        scan_map[f"{schema}.{name}"] = entry

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
    """Build the top-level Metrics block with per-query aggregates."""
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_sqlserver_version(raw: str) -> str:
    """Extract a short version string from ``@@VERSION``.

    ``@@VERSION`` returns text like:
        Microsoft SQL Server 2022 (RTM-CU13) (KB...) - 16.0.4135.4 (X64) ...

    We extract the build number (16.0.4135.4) when present; otherwise
    fall back to the marketing year (2022) or the raw string.
    """
    if not raw:
        return "unknown"
    import re

    # Build number like 16.0.4135.4 or 15.0.2000.5
    build_match = re.search(r"\b(\d+\.\d+\.\d+\.\d+)\b", raw)
    if build_match:
        return build_match.group(1)
    # Marketing year like "SQL Server 2022"
    year_match = re.search(r"SQL Server (\d{4})", raw)
    if year_match:
        return year_match.group(1)
    return raw[:50]


def _normalize_max_length(raw: int | str | None, data_type: str) -> int | None:
    """Normalize sys.columns.max_length for the contract.

    sys.columns reports max_length in *bytes*. For nchar/nvarchar that's
    twice the character count (each char is 2 bytes). ``-1`` means MAX
    (varchar(MAX), nvarchar(MAX), varbinary(MAX)) and we surface that as
    ``None`` (unbounded) per the contract convention.

    The constant 2 is correct here and should not be "fixed" to match the
    Oracle collector, which reads a catalog column instead. SQL Server's
    national types are always UCS-2/UTF-16, so bytes-per-character really is
    fixed at 2. Oracle's ratio depends on the database and national character
    sets (AL32UTF8 up to 4, AL16UTF16 exactly 2, single-byte 1), so it has no
    constant to divide by and must read ALL_TAB_COLUMNS.CHAR_LENGTH.
    """
    if raw is None:
        return None
    try:
        ival = int(raw)
    except (TypeError, ValueError):
        return None
    if ival == -1:
        return None  # MAX
    dt = (data_type or "").lower()
    if dt in ("nvarchar", "nchar", "ntext"):
        return ival // 2
    return ival
