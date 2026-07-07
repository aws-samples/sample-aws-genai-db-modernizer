"""
Oracle Collector Orchestrator — reuses shared infrastructure from MySQL collector.

Same checkpoint-based idempotent collection, same AWS metrics, same S3 storage.
Only the DB-specific collection (schema, queries, global stats) differs.

Key Oracle differences from SQL Server:
- V$SQLSTATS for query patterns (no AWR dependency, no Diagnostic Pack license)
- ALL_* catalog views filtered by OWNER
- OWNER-based schema qualification (not schema_id)
- All identifiers lowercased for downstream consistency
- No per-execution min/max → percentiles are None
- GENERATED AS IDENTITY (12c+) for auto-increment detection
"""

import logging
import time
from datetime import UTC, datetime

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
    View,
)
from src.tools.aws.s3_storage import init_storage
from src.tools.aws.ssm_executor import SSMExecutor
from src.tools.database.oracle_tools import OracleRemoteCollector, _normalize_index_type

logger = logging.getLogger(__name__)


# Oracle native type → normalized type.
_ORACLE_TYPE_MAP: dict[str, NormalizedDataType] = {
    "number": NormalizedDataType.decimal,
    "float": NormalizedDataType.decimal,
    "binary_float": NormalizedDataType.decimal,
    "binary_double": NormalizedDataType.decimal,
    "varchar2": NormalizedDataType.string,
    "nvarchar2": NormalizedDataType.string,
    "char": NormalizedDataType.string,
    "nchar": NormalizedDataType.string,
    "clob": NormalizedDataType.text,
    "nclob": NormalizedDataType.text,
    "long": NormalizedDataType.text,
    "blob": NormalizedDataType.binary,
    "raw": NormalizedDataType.binary,
    "long raw": NormalizedDataType.binary,
    "bfile": NormalizedDataType.binary,
    "date": NormalizedDataType.timestamp,  # Oracle DATE includes time component
    "timestamp": NormalizedDataType.timestamp,
    "timestamp with time zone": NormalizedDataType.timestamp,
    "timestamp with local time zone": NormalizedDataType.timestamp,
    "interval year to month": NormalizedDataType.string,
    "interval day to second": NormalizedDataType.string,
    "xmltype": NormalizedDataType.xml,
    "sdo_geometry": NormalizedDataType.string,
    "rowid": NormalizedDataType.string,
    "urowid": NormalizedDataType.string,
}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def collect(input_contract: CollectorInput) -> CollectorOutputContract:
    """Entry point for Oracle collection."""
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
    db = OracleRemoteCollector(
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

    rds_meta = _build_rds_metadata(aws_raw.get("rds_metadata"))
    cw_metrics = _build_cloudwatch(cw_raw)
    metrics = _build_metrics(queries, cw_metrics)

    elapsed = time.monotonic() - start
    version_str = _parse_oracle_version(meta_raw["version"])

    return CollectorOutputContract(
        job_id=inp.job_id,
        metadata=Metadata(
            collection_timestamp=datetime.now(UTC),
            collector_version=COLLECTOR_VERSION,
            collection_duration_seconds=round(elapsed, 2),
            source_database=SourceDatabase(
                engine="oracle",  # type: ignore[arg-type]
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
                engine="oracle",  # type: ignore[arg-type]
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


# ---------------------------------------------------------------------------
# Raw collectors (live mode)
# ---------------------------------------------------------------------------


def _collect_schema_raw(db: OracleRemoteCollector, opts) -> dict:
    raw_tables = db.collect_tables()
    tables = []
    for t in raw_tables:
        owner = t.get("schema_name", "")
        table_name = t["table_name"]
        qualified = f"{owner}.{table_name}" if owner else table_name

        sample = None
        if opts.collect_sample_data:
            try:
                sample = db.collect_sample_data(qualified, limit=opts.sample_row_count)
            except Exception:
                logger.warning("Failed to collect sample data for %s", qualified)

        tables.append(
            {
                **t,
                "columns": db.collect_columns(qualified),
                "indexes": db.collect_indexes(qualified),
                "foreign_keys": db.collect_foreign_keys(qualified),
                "primary_key": db.collect_primary_key(qualified),
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


def _collect_queries_raw(db: OracleRemoteCollector, opts) -> list[dict]:
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
        schema_name = t.get("schema_name", "")

        columns = [
            Column(
                column_name=c["column_name"],
                ordinal_position=c.get("ordinal_position"),
                data_type=c.get("data_type", ""),
                normalized_data_type=_ORACLE_TYPE_MAP.get(str(c.get("data_type", "")).lower()),
                max_length=_normalize_max_length(c),
                nullable=str(c.get("is_nullable", "Y")).upper() in ("Y", "YES", "TRUE", "1"),
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
                table_id=f"{schema_name}.{name}" if schema_name else name,
                table_name=name,
                schema_name=schema_name,
                row_count=int(t.get("row_count") or 0),
                size_mb=float(t.get("data_size_mb") or 0),
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
            view_id=f"{v.get('schema_name', '')}.{v.get('view_name', '')}",
            view_name=v.get("view_name", ""),
            definition="",  # Oracle views are CLOBs — text not collected for perf
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
        if rtype == "FUNCTION":
            ptype = ProcedureType.FUNCTION
        else:
            ptype = ProcedureType.PROCEDURE
        out.append(
            Procedure(
                procedure_id=f"{p.get('schema_name', '')}.{p.get('routine_name', '')}",
                procedure_name=p.get("routine_name", ""),
                procedure_type=ptype,
                language=p.get("language") or "PL/SQL",
            )
        )
    return out


def _build_triggers(raw: list[dict]) -> list[Trigger] | None:
    if not raw:
        return None
    result = []
    for t in raw:
        # Oracle event_type can be compound: "INSERT OR UPDATE" — take first
        raw_event = str(t.get("event_type") or "INSERT").split(" OR ")[0].strip().upper()
        event = (
            TriggerEventType(raw_event)
            if raw_event in ("INSERT", "UPDATE", "DELETE")
            else TriggerEventType.INSERT
        )
        result.append(
            Trigger(
                trigger_id=f"{t.get('schema_name', '')}.{t.get('trigger_name', '')}",
                trigger_name=t.get("trigger_name", ""),
                table_id=f"{t.get('schema_name', '')}.{t.get('table_name', '')}",
                event_type=event,
                timing=t.get("timing", "AFTER"),
            )
        )
    return result


def _build_queries(raw: list[dict]) -> Queries:
    if not raw:
        return Queries(query_patterns=[])
    patterns = [_dict_to_query_pattern(p) for p in raw]
    return Queries(
        query_patterns=patterns,
        total_queries_analyzed=len(patterns),
        query_log_source=QueryLogSource.v_dollar_sql,
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


def _merge_queries(live: Queries, aws_patterns: list[dict]) -> Queries:
    """Merge AWS PI patterns into live V$SQLSTATS queries."""
    by_id: dict[str, QueryPattern] = {p.query_id: p for p in live.query_patterns}
    for aws in aws_patterns:
        qid = aws.get("query_id")
        if not qid:
            continue
        if qid in by_id:
            existing = by_id[qid]
            if len(str(aws.get("query_text") or "")) > len(existing.query_text or ""):
                existing.query_text = aws["query_text"]
            if aws.get("db_load_contribution_percent") is not None:
                existing.db_load_contribution_percent = aws["db_load_contribution_percent"]
            if aws.get("wait_events"):
                existing.wait_events = aws["wait_events"]
        else:
            by_id[qid] = _dict_to_query_pattern(aws)
    return Queries(
        query_patterns=list(by_id.values()),
        total_queries_analyzed=len(by_id),
        query_log_source=live.query_log_source or QueryLogSource.v_dollar_sql,
    )


def _build_metrics(queries: Queries, cw) -> Metrics:
    patterns = queries.query_patterns
    times = [p.execution_time_ms_avg for p in patterns if p.execution_time_ms_avg is not None]
    total_freq = sum(p.frequency_per_hour for p in patterns)

    perf = PerformanceMetrics(
        avg_query_time_ms=sum(times) / len(times) if times else None,
        p50_query_time_ms=sorted(times)[len(times) // 2] if times else None,
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


def _parse_oracle_version(raw: str) -> str:
    """Extract version from V$VERSION BANNER.

    Examples:
        "Oracle Database 19c Enterprise Edition Release 19.0.0.0.0 - Production"
        → "19.0.0.0.0"
    """
    if not raw:
        return "unknown"
    import re

    version_match = re.search(r"(\d+\.\d+\.\d+\.\d+\.\d+)", raw)
    if version_match:
        return version_match.group(1)
    # Fallback: just the major version like "19c"
    short_match = re.search(r"(\d+c?)", raw)
    if short_match:
        return short_match.group(1)
    return raw[:50]


def _normalize_max_length(col: dict) -> int | None:
    """Normalize Oracle column length.

    Oracle reports DATA_LENGTH in bytes. For CHAR-semantic columns
    (CHAR_USED = 'C') with UTF-8, divide by 4 to get char count.
    For NUMBER, use DATA_PRECISION.
    """
    data_type = str(col.get("data_type") or "").lower()
    if data_type == "number":
        p = col.get("data_precision")
        return int(p) if p is not None else None
    raw = col.get("max_length")
    if raw is None:
        return None
    try:
        ival = int(raw)
    except (TypeError, ValueError):
        return None
    char_used = str(col.get("char_used") or "").upper()
    if char_used == "C" and data_type in ("nvarchar2", "nchar"):
        return ival // 4  # UTF-8 worst case
    if char_used == "C":
        return ival  # Already in chars
    # Byte-semantic nvarchar2/nchar: divide by char width
    if data_type in ("nvarchar2", "nchar"):
        return ival // 2
    return ival
