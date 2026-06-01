"""
AWS RDS Tools

Secrets Manager, RDS API, CloudWatch Metrics, Performance Insights,
and CloudWatch Database Insights.

All OPTIONAL tools return {'available': False} on failure for graceful degradation.
"""

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from src.tools.aws.credentials import AWSCredentialManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Secrets Manager (CRITICAL)
# ---------------------------------------------------------------------------


def get_database_credentials(cred_mgr: AWSCredentialManager, secret_arn: str) -> dict[str, Any]:
    """Retrieve DB credentials from Secrets Manager. Raises on failure."""
    client = cred_mgr.client("secretsmanager")
    resp = client.get_secret_value(SecretId=secret_arn)
    secret = json.loads(resp["SecretString"])
    return {
        "username": secret["username"],
        "password": secret["password"],
        "host": secret.get("host"),
        "port": secret.get("port"),
        "dbname": secret.get("dbname"),
    }


# ---------------------------------------------------------------------------
# RDS Describe Instance (OPTIONAL)
# ---------------------------------------------------------------------------


def get_rds_instance_metadata(cred_mgr: AWSCredentialManager, instance_id: str) -> dict[str, Any]:
    """Describe an RDS instance. Returns available=False on failure."""
    try:
        rds = cred_mgr.client("rds")
        resp = rds.describe_db_instances(DBInstanceIdentifier=instance_id)
        inst = resp["DBInstances"][0]
        return {
            "available": True,
            "db_instance_identifier": inst["DBInstanceIdentifier"],
            "instance_class": inst["DBInstanceClass"],
            "engine": inst["Engine"],
            "engine_version": inst["EngineVersion"],
            "resource_arn": inst.get("DbiResourceId"),
            "vcpu_count": _vcpu_for_class(inst["DBInstanceClass"]),
            "memory_gb": _memory_for_class(inst["DBInstanceClass"]),
            "storage_type": inst.get("StorageType"),
            "storage_size_gb": inst.get("AllocatedStorage"),
            "storage_iops": inst.get("Iops"),
            "storage_throughput_mbps": inst.get("StorageThroughput"),
            "multi_az": inst.get("MultiAZ", False),
            "region": cred_mgr.region,
            "availability_zone": inst.get("AvailabilityZone"),
            "read_replica_count": len(inst.get("ReadReplicaDBInstanceIdentifiers", [])),
            "backup_retention_days": inst.get("BackupRetentionPeriod"),
            "performance_insights_enabled": inst.get("PerformanceInsightsEnabled", False),
            "enhanced_monitoring_interval": inst.get("MonitoringInterval", 0),
        }
    except Exception as e:
        logger.warning("Failed to get RDS metadata for %s: %s", instance_id, e)
        return {"available": False, "error": str(e)}


# ---------------------------------------------------------------------------
# CloudWatch Metrics (OPTIONAL)
# ---------------------------------------------------------------------------

_CW_METRICS = [
    ("CPUUtilization", "Percent", "cpu_utilization"),
    ("FreeableMemory", "Bytes", "freeable_memory_gb"),
    ("DatabaseConnections", "Count", "database_connections"),
    ("ReadIOPS", "Count/Second", "read_iops"),
    ("WriteIOPS", "Count/Second", "write_iops"),
    ("ReadLatency", "Seconds", "read_latency_ms"),
    ("WriteLatency", "Seconds", "write_latency_ms"),
    ("NetworkReceiveThroughput", "Bytes/Second", "network_receive_throughput_mbps"),
    ("NetworkTransmitThroughput", "Bytes/Second", "network_transmit_throughput_mbps"),
    ("FreeStorageSpace", "Bytes", "free_storage_space_gb"),
]


def get_cloudwatch_metrics(
    cred_mgr: AWSCredentialManager, instance_id: str, days: int = 7
) -> dict[str, Any]:
    """Collect CloudWatch metrics for an RDS instance."""
    try:
        cw = cred_mgr.client("cloudwatch")
        end = datetime.now(UTC)
        start = end - timedelta(days=days)
        period = 3600

        results: dict[str, Any] = {"available": True}
        for metric_name, unit, key in _CW_METRICS:
            stats = _get_metric_stats(cw, instance_id, metric_name, unit, start, end, period)
            if stats:
                results[key] = _convert_metric(key, stats)
        return results
    except Exception as e:
        logger.warning("Failed to get CloudWatch metrics for %s: %s", instance_id, e)
        return {"available": False, "error": str(e)}


def _get_metric_stats(cw, instance_id, metric_name, unit, start, end, period):
    resp = cw.get_metric_statistics(
        Namespace="AWS/RDS",
        MetricName=metric_name,
        Dimensions=[{"Name": "DBInstanceIdentifier", "Value": instance_id}],
        StartTime=start,
        EndTime=end,
        Period=period,
        Statistics=["Average", "Maximum", "Minimum"],
        Unit=unit,
    )
    dp = resp.get("Datapoints", [])
    if not dp:
        return None
    return {
        "avg": sum(d["Average"] for d in dp) / len(dp),
        "max": max(d["Maximum"] for d in dp),
        "min": min(d["Minimum"] for d in dp),
    }


def _convert_metric(key: str, stats: dict) -> dict:
    if "memory_gb" in key or "storage_space_gb" in key:
        return {k: v / (1024**3) for k, v in stats.items()}
    if "latency_ms" in key:
        return {k: v * 1000 for k, v in stats.items()}
    if "throughput_mbps" in key:
        return {k: v / (1024 * 1024) for k, v in stats.items()}
    return stats


# ---------------------------------------------------------------------------
# Performance Insights — Full Query Metrics (OPTIONAL)
# ---------------------------------------------------------------------------


def get_performance_insights_queries(
    cred_mgr: AWSCredentialManager,
    resource_id: str,
    days: int = 7,
    max_results: int = 25,
) -> dict[str, Any]:
    """
    Collect per-query metrics from Performance Insights.

    Uses:
    - describe_dimension_keys: get top SQL by db.load.avg
    - get_resource_metrics: get per-SQL metrics (calls/sec, rows affected, latency)

    Returns query patterns with execution stats matching QueryPattern contract.
    """
    try:
        pi = cred_mgr.client("pi")
        end = datetime.now(UTC)
        start = end - timedelta(days=days)

        # Step 1: Get top SQL IDs ranked by load
        dim_resp = pi.describe_dimension_keys(
            ServiceType="RDS",
            Identifier=resource_id,
            StartTime=start,
            EndTime=end,
            Metric="db.load.avg",
            GroupBy={
                "Group": "db.sql",
                "Dimensions": ["db.sql.id", "db.sql.statement", "db.sql.tokenized_id"],
                "Limit": max_results,
            },
            PeriodInSeconds=3600,
        )

        keys = dim_resp.get("Keys", [])
        if not keys:
            return {
                "available": True,
                "query_patterns": [],
                "period_start": start.isoformat(),
                "period_end": end.isoformat(),
            }

        # Calculate total load for percentage
        total_load = sum(k.get("Total", 0) for k in keys)

        # Step 2: Collect full SQL text via get_dimension_key_details
        sql_ids = []
        sql_map: dict[str, dict] = {}
        for k in keys:
            dims = k.get("Dimensions", {})
            sql_id = dims.get("db.sql.id", "")
            if not sql_id:
                continue
            sql_ids.append(sql_id)
            sql_map[sql_id] = {
                "query_id": sql_id,
                "query_text": dims.get("db.sql.statement", ""),  # truncated, will be replaced
                "tokenized_id": dims.get("db.sql.tokenized_id", ""),
                "db_load_avg": k.get("Total", 0),
                "db_load_contribution_percent": (k.get("Total", 0) / total_load * 100)
                if total_load > 0
                else 0,
            }

        # Get full SQL text for each query (describe_dimension_keys truncates)
        for sql_id in sql_ids:
            try:
                detail_resp = pi.get_dimension_key_details(
                    ServiceType="RDS",
                    Identifier=resource_id,
                    Group="db.sql",
                    GroupIdentifier=sql_id,
                    RequestedDimensions=["db.sql.statement"],
                )
                dims = detail_resp.get("Dimensions", [])
                for d in dims:
                    if d.get("Dimension") == "db.sql.statement":
                        full_text = d.get("Value", "")
                        if full_text and len(full_text) > len(sql_map[sql_id]["query_text"]):
                            sql_map[sql_id]["query_text"] = full_text
                        break
            except Exception:  # nosec B110
                pass  # Keep truncated text from describe_dimension_keys

        # Step 3: Get wait events (available for MySQL)
        wait_events = _get_wait_events(pi, resource_id, start, end)

        # Step 4: Build query patterns
        # Note: db.sql.stats.* metrics (calls/sec, latency) are PostgreSQL-only.
        # For MySQL, we only get db.load.avg per SQL from PI.
        # performance_schema provides the detailed per-query stats for MySQL.
        query_patterns = []
        for sql_id, info in sql_map.items():
            query_text = info["query_text"]

            query_patterns.append(
                {
                    "query_id": sql_id,
                    "query_text": query_text,
                    "query_type": _extract_query_type(query_text),
                    "frequency_per_hour": 0,  # Not available from PI for MySQL
                    "calls_per_second": None,
                    "tables_accessed": _extract_tables(query_text),
                    "execution_time_ms_avg": None,
                    "db_load_contribution_percent": info["db_load_contribution_percent"],
                    "has_joins": " join " in query_text.lower(),
                    "has_aggregations": bool(
                        re.search(r"\b(count|sum|avg|min|max|group\s+by)\b", query_text, re.I)
                    ),
                    "has_subqueries": query_text.lower().count("select") > 1,
                    "wait_events": wait_events,  # Global wait events (not per-SQL for MySQL)
                }
            )

        return {
            "available": True,
            "query_patterns": query_patterns,
            "total_load": total_load,
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
        }
    except Exception as e:
        logger.warning("Failed to get Performance Insights queries: %s", e)
        return {"available": False, "error": str(e)}


def _get_wait_events(pi, resource_id, start, end) -> list[dict]:
    """Get top wait events from PI. Available for all engines."""
    try:
        resp = pi.describe_dimension_keys(
            ServiceType="RDS",
            Identifier=resource_id,
            StartTime=start,
            EndTime=end,
            Metric="db.load.avg",
            GroupBy={"Group": "db.wait_event", "Limit": 10},
            PeriodInSeconds=3600,
        )
        total = sum(k.get("Total", 0) for k in resp.get("Keys", []))
        return [
            {
                "event_name": k.get("Dimensions", {}).get("db.wait_event.name", ""),
                "wait_time_ms": k.get("Total", 0) * 1000,
                "wait_time_percent": (k.get("Total", 0) / total * 100) if total > 0 else 0,
            }
            for k in resp.get("Keys", [])
        ]
    except Exception:
        return []


def get_pi_counter_metrics(
    cred_mgr: AWSCredentialManager,
    resource_id: str,
    days: int = 7,
) -> dict[str, Any]:
    """
    Collect PI counter metrics (db.Cache, db.Temp, db.IO, db.SQL).
    Maps MySQL-specific counters to cross-database contract fields.
    Returns available=False on failure.
    """
    try:
        pi = cred_mgr.client("pi")
        end = datetime.now(UTC)
        start = end - timedelta(days=days)

        metrics_to_fetch = [
            "db.Cache.innoDB_buffer_pool_hit_rate",
            "db.Cache.innoDB_buffer_pool_hits",
            "db.Cache.Innodb_buffer_pool_reads",
            "db.Cache.Innodb_buffer_pool_read_requests",
            "db.Temp.Created_tmp_disk_tables",
            "db.Temp.Created_tmp_tables",
            "db.IO.Innodb_data_writes",
            "db.IO.Innodb_pages_written",
        ]

        resp = pi.get_resource_metrics(
            ServiceType="RDS",
            Identifier=resource_id,
            StartTime=start,
            EndTime=end,
            PeriodInSeconds=3600,
            MetricQueries=[{"Metric": m} for m in metrics_to_fetch],
        )

        values = {}
        for ml in resp.get("MetricList", []):
            metric = ml.get("Key", {}).get("Metric", "")
            dps = ml.get("DataPoints", [])
            vals = [dp["Value"] for dp in dps if dp.get("Value") is not None]
            if vals:
                values[metric] = sum(vals) / len(vals)

        return {
            "available": True,
            "cache_hit_ratio_pct": values.get("db.Cache.innoDB_buffer_pool_hit_rate"),
            "buffer_pool_hits": values.get("db.Cache.innoDB_buffer_pool_hits"),
            "buffer_pool_reads_from_disk": values.get("db.Cache.Innodb_buffer_pool_reads"),
            "buffer_pool_read_requests": values.get("db.Cache.Innodb_buffer_pool_read_requests"),
            "tmp_disk_tables": values.get("db.Temp.Created_tmp_disk_tables"),
            "tmp_tables": values.get("db.Temp.Created_tmp_tables"),
        }
    except Exception as e:
        logger.warning("Failed to get PI counter metrics: %s", e)
        return {"available": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Enhanced Monitoring (OPTIONAL)
# ---------------------------------------------------------------------------


def get_enhanced_monitoring_cpu(
    cred_mgr: AWSCredentialManager,
    resource_id: str,
    num_samples: int = 10,
) -> dict[str, Any]:
    """
    Get avg CPU time per query from Enhanced Monitoring (RDSOSMetrics log group).
    Uses mysqld process cpuUsedPc + numVCPUs to estimate per-query CPU ms.
    Returns available=False on failure.
    """
    try:
        logs = cred_mgr.client("logs")
        resp = logs.get_log_events(
            logGroupName="RDSOSMetrics",
            logStreamName=resource_id,
            limit=num_samples,
            startFromHead=False,
        )
        events = resp.get("events", [])
        if not events:
            return {"available": False, "error": "No Enhanced Monitoring data"}

        cpu_pcts = []
        num_vcpus = 1
        for evt in events:
            data = json.loads(evt["message"])
            num_vcpus = data.get("numVCPUs", 1)
            procs = data.get("processList", [])
            db_proc = next(
                (
                    p
                    for p in procs
                    if any(
                        n in p.get("name", "") for n in ("mysqld", "postgres", "sqlservr", "oracle")
                    )
                ),
                None,
            )
            if db_proc:
                cpu_pcts.append(db_proc.get("cpuUsedPc", 0))

        if not cpu_pcts:
            return {"available": False, "error": "No database process found"}

        avg_cpu_pct = sum(cpu_pcts) / len(cpu_pcts)

        return {
            "available": True,
            "avg_cpu_pct": avg_cpu_pct,
            "num_vcpus": num_vcpus,
            "samples": len(cpu_pcts),
        }
    except Exception as e:
        logger.warning("Failed to get Enhanced Monitoring data: %s", e)
        return {"available": False, "error": str(e)}


# ---------------------------------------------------------------------------
# CloudWatch Database Insights (OPTIONAL)
# ---------------------------------------------------------------------------


def get_database_insights(
    cred_mgr: AWSCredentialManager,
    resource_id: str,
    days: int = 7,
    limit: int = 100,
) -> dict[str, Any]:
    """
    Query CloudWatch Database Insights (Logs Insights on performance_insights log group).

    Returns top SQL with execution stats: avg latency, calls, rows affected.
    This supplements PI data with CloudWatch-native querying.
    """
    try:
        logs = cred_mgr.client("logs")
        end = datetime.now(UTC)
        start = end - timedelta(days=days)

        log_group = f"/aws/rds/database-insights/{resource_id}"

        # Query for top SQL by total execution time
        query = f"""
            fields @timestamp, sql.id, sql.statement, sql.stats.calls_per_sec,
                   sql.stats.rows_affected_per_sec, sql.stats.avg_latency_per_call,
                   sql.stats.total_time_per_sec
            | filter sql.id != ''
            | stats avg(sql.stats.calls_per_sec) as avg_calls_per_sec,
                    avg(sql.stats.avg_latency_per_call) as avg_latency_ms,
                    avg(sql.stats.rows_affected_per_sec) as avg_rows_affected_per_sec,
                    sum(sql.stats.total_time_per_sec) as total_time_sec,
                    latest(sql.statement) as query_text
              by sql.id
            | sort total_time_sec desc
            | limit {limit}
        """

        start_resp = logs.start_query(
            logGroupName=log_group,
            startTime=int(start.timestamp()),
            endTime=int(end.timestamp()),
            queryString=query,
        )
        query_id = start_resp["queryId"]

        # Poll for results
        import time

        for _ in range(30):  # max 30 seconds
            result = logs.get_query_results(queryId=query_id)
            if result["status"] == "Complete":
                break
            time.sleep(1)  # nosemgrep: arbitrary-sleep  # polling CloudWatch Logs Insights query
        else:
            return {"available": True, "query_patterns": [], "note": "Query timed out"}

        # Parse results
        query_patterns = []
        for row in result.get("results", []):
            fields = {f["field"]: f["value"] for f in row}
            sql_id = fields.get("sql.id", "")
            query_text = fields.get("query_text", "")

            calls_per_sec = _safe_float(fields.get("avg_calls_per_sec"))
            avg_latency_ms = _safe_float(fields.get("avg_latency_ms"))
            rows_affected_per_sec = _safe_float(fields.get("avg_rows_affected_per_sec"))

            query_patterns.append(
                {
                    "query_id": sql_id,
                    "query_text": query_text,
                    "query_type": _extract_query_type(query_text),
                    "frequency_per_hour": (calls_per_sec or 0) * 3600,
                    "calls_per_second": calls_per_sec,
                    "execution_time_ms_avg": avg_latency_ms,
                    "rows_affected_avg": (rows_affected_per_sec / calls_per_sec)
                    if calls_per_sec and calls_per_sec > 0
                    else 0,
                    "tables_accessed": _extract_tables(query_text),
                    "has_joins": " join " in query_text.lower(),
                    "has_aggregations": bool(
                        re.search(r"\b(count|sum|avg|min|max|group\s+by)\b", query_text, re.I)
                    ),
                    "has_subqueries": query_text.lower().count("select") > 1,
                }
            )

        return {
            "available": True,
            "query_patterns": query_patterns,
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
        }
    except Exception as e:
        logger.warning("Failed to get Database Insights: %s", e)
        return {"available": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_QUERY_TYPE_RE = re.compile(r"^\s*(SELECT|INSERT|UPDATE|DELETE|MERGE|REPLACE)\b", re.I)
_TABLE_RE = re.compile(r"(?:FROM|JOIN|INTO|UPDATE)\s+`?(\w+)`?", re.I)


def _extract_query_type(sql: str) -> str:
    m = _QUERY_TYPE_RE.match(sql)
    return m.group(1).upper() if m else "OTHER"


def _extract_tables(sql: str) -> list[str]:
    tables = list(dict.fromkeys(_TABLE_RE.findall(sql)))
    return tables if tables else ["unknown"]


def _safe_float(val) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


_INSTANCE_SPECS: dict[str, tuple[int, float]] = {
    "db.t3.micro": (2, 1),
    "db.t3.small": (2, 2),
    "db.t3.medium": (2, 4),
    "db.t3.large": (2, 8),
    "db.t3.xlarge": (4, 16),
    "db.t3.2xlarge": (8, 32),
    "db.t4g.micro": (2, 1),
    "db.t4g.small": (2, 2),
    "db.t4g.medium": (2, 4),
    "db.t4g.large": (2, 8),
    "db.t4g.xlarge": (4, 16),
    "db.t4g.2xlarge": (8, 32),
    "db.r5.large": (2, 16),
    "db.r5.xlarge": (4, 32),
    "db.r5.2xlarge": (8, 64),
    "db.r5.4xlarge": (16, 128),
    "db.r5.8xlarge": (32, 256),
    "db.r5.12xlarge": (48, 384),
    "db.r5.16xlarge": (64, 512),
    "db.r5.24xlarge": (96, 768),
    "db.r6g.large": (2, 16),
    "db.r6g.xlarge": (4, 32),
    "db.r6g.2xlarge": (8, 64),
    "db.r6g.4xlarge": (16, 128),
    "db.r6g.8xlarge": (32, 256),
    "db.r6g.12xlarge": (48, 384),
    "db.r6g.16xlarge": (64, 512),
    "db.r7g.large": (2, 16),
    "db.r7g.xlarge": (4, 32),
    "db.r7g.2xlarge": (8, 64),
    "db.r7g.4xlarge": (16, 128),
    "db.r7g.8xlarge": (32, 256),
    "db.r7g.12xlarge": (48, 384),
    "db.r7g.16xlarge": (64, 512),
    "db.r8g.large": (2, 16),
    "db.r8g.xlarge": (4, 32),
    "db.r8g.2xlarge": (8, 64),
    "db.r8g.4xlarge": (16, 128),
    "db.r8g.8xlarge": (32, 256),
    "db.r8g.12xlarge": (48, 384),
    "db.r8g.16xlarge": (64, 512),
    "db.r8g.24xlarge": (96, 768),
    "db.m5.large": (2, 8),
    "db.m5.xlarge": (4, 16),
    "db.m5.2xlarge": (8, 32),
    "db.m5.4xlarge": (16, 64),
    "db.m5.8xlarge": (32, 128),
    "db.m6g.large": (2, 8),
    "db.m6g.xlarge": (4, 16),
    "db.m6g.2xlarge": (8, 32),
    "db.m6g.4xlarge": (16, 64),
    "db.m6g.8xlarge": (32, 128),
    "db.m7g.large": (2, 8),
    "db.m7g.xlarge": (4, 16),
    "db.m7g.2xlarge": (8, 32),
    "db.m7g.4xlarge": (16, 64),
    "db.m7g.8xlarge": (32, 128),
}


def _vcpu_for_class(instance_class: str) -> int | None:
    spec = _INSTANCE_SPECS.get(instance_class)
    return spec[0] if spec else None


def _memory_for_class(instance_class: str) -> float | None:
    spec = _INSTANCE_SPECS.get(instance_class)
    return spec[1] if spec else None
