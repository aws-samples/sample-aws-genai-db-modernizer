"""Build a collector output contract from a parsed offline collection (#1177).

Every ingest path that turns a raw collection-script file into
``collector/output.json`` builds it here:

- the library ingest (``src/atx_orchestrator/core.py::ingest_offline_collection``),
  ``scripts/run_assessment.py``, ``scripts/run_collect.py`` and
  ``scripts/test_local_phased.py`` call :func:`build_offline_collector_output`;
- the collector handler's offline mode (``mysql_collector._collect_offline``, which
  every engine collector shares) calls :func:`build_offline_contract` with the input
  it built from :func:`resolve_offline_engine`, plus the AWS metrics it collected.

So the source engine is always the one
:func:`src.tools.database.offline_parser.detect_source_engine` reads from the
collection's metadata (#381), never a guess of the caller's own. Before #1177 the
library ingest kept its own inline check ("postgres" in the version string, else
MySQL) and recorded every SQL Server, Oracle and DB2 collection as MySQL.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.contracts.collector_input import CollectorInput
    from src.contracts.collector_output import CollectorOutputContract


def resolve_offline_engine(
    metadata: dict | None, declared: str | None = None
) -> tuple[str, int, str | None]:
    """The source engine and port of an offline collection, and a note when the
    operator's declared engine was overruled.

    The collection's own metadata wins when it names an engine
    (:func:`~src.tools.database.offline_parser.recognised_source_engine`): an upload
    form's engine is a guess about a file the collector can read (#1177 review). A
    declared engine is used when the file names none; with neither, the detector's
    default (MySQL).
    """
    from src.tools.database.offline_parser import (
        default_port,
        detect_source_engine,
        normalize_engine,
        recognised_source_engine,
    )

    declared_engine = normalize_engine(declared)
    from_file = recognised_source_engine(metadata if isinstance(metadata, dict) else None)
    if from_file is not None:
        engine, port = from_file
        note = None
        if declared_engine and declared_engine != engine:
            note = (
                f"the collection's metadata says {engine}, not the declared {declared_engine}: "
                f"using {engine}"
            )
        return engine, port, note
    if declared_engine:
        return declared_engine, default_port(declared_engine) or 0, None
    engine, port = detect_source_engine(None)
    return engine, port, None


def table_database_name(parsed: dict[str, Any], database_name: str) -> str:
    """The qualifier of ``parsed``'s table ids: the database the parser qualified the
    statements' table references with (``collected_database_name``, #1572), so every
    statement matches its tables whatever the run is named. ``database_name`` (the
    run's name) only when the collection names no database, or for a parse saved
    before the parser recorded it (the collection's own name first, as then)."""
    if "collected_database_name" in parsed:
        return str(parsed["collected_database_name"] or database_name)
    meta = parsed.get("metadata")
    stated = meta.get("database_name") if isinstance(meta, dict) else None
    return str(stated or database_name)


def build_offline_contract(
    parsed: dict[str, Any],
    inp: CollectorInput,
    *,
    aws_raw: dict[str, Any] | None = None,
) -> CollectorOutputContract:
    """The collector output for ``parsed`` (``parse_offline_collection``'s result).

    The table ids are prefixed with ``table_database_name(parsed, ...)``, never
    ``inp.database_name`` alone (the run's name, #1572), and
    ``metadata.source_database.database_name`` is that same name. ``aws_raw`` is what the
    collector handler gathered from the account (RDS metadata, CloudWatch,
    Performance Insights); when it is given, those metrics are merged in and the
    collection's own instance counters, wait events and I/O stats enrich the query
    patterns and metrics, as the handler always did. The script and library paths
    pass none and keep their output as it was.
    """
    from src.agents.collector.mysql_collector import (
        _build_cloudwatch,
        _build_metrics,
        _build_output,
        _build_procedures,
        _build_queries,
        _build_rds_metadata,
        _build_source_host,
        _build_tables,
        _build_triggers,
        _build_views,
        _enrich_patterns_from_pi_and_cw,
        _merge_into_queries,
        _offline_query_log_source,
    )
    from src.tools.database.offline_parser import offline_version_label

    start = time.monotonic()
    offline_meta = parsed.get("metadata") or {}
    if not isinstance(offline_meta, dict):
        offline_meta = {}
    # One name for the table ids and the source database (#1572): the system-schema
    # check (#835) and schema design read the latter to tell the database from a schema.
    collected_name = table_database_name(parsed, inp.database_name)
    tables = _build_tables(parsed["tables"], collected_name)
    views = _build_views(parsed.get("views", []))
    procedures = _build_procedures(parsed.get("procedures", []))
    triggers = _build_triggers(parsed.get("triggers", []))
    queries = _build_queries(
        parsed.get("queries", []), query_log_source=_offline_query_log_source(inp.engine.value)
    )
    # #381: a short, human-readable version string (product_version, or the first
    # line only of version/version_full) -- the full @@VERSION banner (newlines,
    # tabs, build date) must never reach wave 1's user-visible rationale text.
    version = offline_version_label(offline_meta)

    if aws_raw is None:
        # #1197: the buffer-cache hit ratio and host figures the parser read.
        metrics = _build_metrics(queries, None, source_host=_build_source_host(parsed))
        rds_meta = None
    else:
        queries = _merge_into_queries(queries, aws_raw.get("query_patterns", []))
        cw_raw = aws_raw.get("cloudwatch", {})
        pi_counters = parsed.get("global_stats", {}) or aws_raw.get("pi_counters", {})
        _enrich_patterns_from_pi_and_cw(
            queries, pi_counters, cw_raw, aws_raw.get("enhanced_monitoring")
        )
        _apply_native_waits(queries, parsed.get("wait_events", []))
        rds_meta = _build_rds_metadata(aws_raw.get("rds_metadata"))
        # #1197: the instance's ratio once, unrounded, never copied onto the queries.
        metrics = _build_metrics(
            queries,
            _build_cloudwatch(cw_raw),
            source_host=_build_source_host(
                {"global_stats": pi_counters, "os_stats": parsed.get("os_stats")}
            ),
        )
        _apply_native_io_stats(metrics, parsed.get("io_stats", {}))
        if version == "unknown":
            # The AWS-reported engine version when the collection itself has none.
            version = aws_raw.get("engine_version", "unknown")

    return _build_output(
        inp,
        start,
        version=version,
        db_size=offline_meta.get("database_size_gb"),
        tables=tables,
        queries=queries,
        metrics=metrics,
        rds_meta=rds_meta,
        views=views,
        procedures=procedures,
        triggers=triggers,
        # #949: AWS engines the application already runs next to the source.
        engines_in_use=offline_meta.get("engines_in_use"),
        database_name=collected_name,
    )


def _apply_native_waits(queries: Any, native_waits: list[dict]) -> None:
    """On-premises enrichment: the collection's own wait events on every pattern that
    has none (the top five)."""
    if not native_waits or not queries.query_patterns:
        return
    from src.contracts.collector_output import WaitEvent

    # Percent of the total wait time across all events, when the SQL script did not
    # provide one (avg_wait_ms is milliseconds, never a percent).
    total_wait_ms = sum(float(w.get("time_waited_ms") or 0) for w in native_waits) or 1.0

    def _wait_percent(w: dict) -> float:
        # Prefer the script-provided `wait_time_percent` field when present (added to
        # collect-oracle.sql / collect-sqlserver.sql).
        explicit = w.get("wait_time_percent")
        if explicit is not None:
            return min(100.0, max(0.0, float(explicit)))
        time_ms = float(w.get("time_waited_ms") or 0)
        return min(100.0, max(0.0, (time_ms / total_wait_ms) * 100))

    wait_list = [
        WaitEvent(
            event_name=w.get("event", ""),
            wait_time_ms=float(w.get("time_waited_ms") or 0),
            wait_time_percent=_wait_percent(w),
        )
        for w in native_waits[:5]
    ]
    for p in queries.query_patterns:
        if not p.wait_events:
            p.wait_events = wait_list


def _apply_native_io_stats(metrics: Any, io_stats: dict) -> None:
    """The collection's own I/O rates when CloudWatch reported none."""
    if not io_stats or not metrics.performance_metrics:
        return
    perf = metrics.performance_metrics
    if not perf.read_iops_avg and io_stats.get("physical_reads_per_sec"):
        perf.read_iops_avg = float(io_stats["physical_reads_per_sec"])
    if not perf.write_iops_avg and io_stats.get("physical_writes_per_sec"):
        perf.write_iops_avg = float(io_stats["physical_writes_per_sec"])


def build_offline_collector_output(
    parsed: dict[str, Any],
    *,
    job_id: str,
    database_name: str,
    s3_key: str,
    s3_bucket: str = "local",
) -> dict[str, Any]:
    """The collector output dict for ``parsed``, with the engine the collection's
    metadata names (the script and library paths).

    ``s3_bucket``/``s3_key`` only label where the raw file was read from: an offline
    collection is never connected to.
    """
    from src.contracts.collector_input import CollectorInput

    metadata = parsed.get("metadata")
    engine, port, _ = resolve_offline_engine(metadata if isinstance(metadata, dict) else None)
    inp = CollectorInput.model_validate(
        {
            "job_id": job_id,
            "engine": engine,
            "cluster_endpoint": "offline",
            "port": port,
            "database_name": database_name,
            "mode": "offline",
            "offline_config": {"s3_bucket": s3_bucket, "s3_key": s3_key},
        }
    )
    output: dict[str, Any] = json.loads(build_offline_contract(parsed, inp).model_dump_json())
    return output
