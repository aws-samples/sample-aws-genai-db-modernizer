"""
MariaDB Collector Orchestrator — Reuses MySQL collector infrastructure.

MariaDB is MySQL-compatible: same INFORMATION_SCHEMA, same performance_schema,
same mysql CLI. The only difference is version-dependent feature detection
(no QUANTILE_95/99, different version numbering for SUM_ERRORS/FIRST_SEEN).

This module delegates to the MySQL collector for all modes except live,
where it swaps in MariaDBRemoteCollector for correct feature flags.
"""

import logging
import time

from src.agents.collector.mysql_collector import (
    _build_cloudwatch,
    _build_cred_mgr,
    _build_metrics,
    _build_output,
    _build_procedures,
    _build_queries,
    _build_rds_metadata,
    _build_tables,
    _build_triggers,
    _build_views,
    _collect_aws_raw,
    _collect_ddl,
    _collect_offline,
    _collect_queries_raw,
    _collect_schema_raw,
    _enrich_patterns_from_pi_and_cw,
    _init_checkpoint_store,
    _merge_into_queries,
)
from src.contracts.collector_input import CollectionMode, CollectorInput
from src.contracts.collector_output import CollectorOutputContract
from src.tools.aws.ssm_executor import SSMExecutor
from src.tools.database.mariadb_tools import MariaDBRemoteCollector

logger = logging.getLogger(__name__)


def collect(input_contract: CollectorInput) -> CollectorOutputContract:
    """Entry point — same as MySQL collector but uses MariaDBRemoteCollector for live mode."""
    ckpt = _init_checkpoint_store(input_contract)

    if ckpt.exists("output"):
        logger.info("Job %s: final output already exists, returning cached", input_contract.job_id)
        result: CollectorOutputContract = CollectorOutputContract.model_validate(
            ckpt.load("output")
        )
        return result

    if input_contract.mode == CollectionMode.live:
        result = _collect_live_mariadb(input_contract, ckpt)
    elif input_contract.mode == CollectionMode.ddl:
        result = _collect_ddl(input_contract, ckpt)
    else:
        result = _collect_offline(input_contract, ckpt)

    ckpt.save("output", result.model_dump(mode="json"))
    return result


def _collect_live_mariadb(inp: CollectorInput, ckpt) -> CollectorOutputContract:
    """Live collection using MariaDBRemoteCollector for correct version feature flags."""
    start = time.monotonic()
    cred_mgr = _build_cred_mgr(inp)
    region = inp.aws_config.region if inp.aws_config else "us-east-1"

    ssm = SSMExecutor(cred_mgr, inp.live_config.automation_instance_id)
    db = MariaDBRemoteCollector(
        ssm=ssm,
        host=inp.cluster_endpoint,
        port=inp.port,
        database=inp.database_name,
        secret_arn=inp.live_config.secret_arn,
        region=region,
    )

    meta_raw = ckpt.load_or_run(
        "metadata",
        lambda: {"version": db.get_version(), "db_size_gb": db.get_database_size_gb()},
    )
    schema_raw = ckpt.load_or_run("schema", lambda: _collect_schema_raw(db, inp.collection_options))
    queries_raw = ckpt.load_or_run(
        "queries", lambda: _collect_queries_raw(db, inp.collection_options)
    )
    aws_raw = ckpt.load_or_run("aws_metrics", lambda: _collect_aws_raw(inp, cred_mgr))

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
