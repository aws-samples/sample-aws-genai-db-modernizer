# MySQL Collector Agent — README

## Overview

The MySQL Collector Agent extracts comprehensive metadata, schema, query patterns, and performance metrics from MySQL/MariaDB databases running on Amazon RDS. It produces a `CollectorOutputContract` JSON document used by downstream Analysis, Referee, and Schema Design agents.

**Key features:**

- Two collection modes: **LIVE** (SSM-based DB access) and **DDL** (parse uploaded DDL from S3)
- No direct database connection from the collector — all DB access via **SSM Run Command** on an automation EC2 instance
- Checkpoint-based idempotency — resumes from last successful stage on failure
- 100% contract field coverage for MySQL (47/47 query pattern fields)

---

## Architecture

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│  FastAPI      │     │  Automation  │     │  RDS MySQL   │
│  (Collector)  │────▶│  EC2 (SSM)   │────▶│  Instance    │
│  Port 8000    │     │  mysql CLI   │     │  Private VPC │
└──────┬───────┘     └──────────────┘     └──────────────┘
       │
       │  Also calls directly (no SSM needed):
       ├──▶ Secrets Manager (credentials)
       ├──▶ RDS API (instance metadata)
       ├──▶ CloudWatch Metrics (CPU, IOPS, latency)
       ├──▶ Performance Insights (top SQL, wait events)
       ├──▶ Enhanced Monitoring (per-process CPU)
       └──▶ S3 (checkpoints + output storage)
```

---

## Prerequisites

1. **Automation EC2 instance** — SSM-managed, in same VPC as RDS, with `mysql` client installed
2. **Secrets Manager secret** — containing `username` and `password` for the target database
3. **IAM permissions** — the caller needs: `ssm:SendCommand`, `ssm:GetCommandInvocation`, `secretsmanager:GetSecretValue`, `rds:DescribeDBInstances`, `cloudwatch:GetMetricStatistics`, `pi:DescribeDimensionKeys`, `pi:GetResourceMetrics`, `pi:GetDimensionKeyDetails`, `logs:GetLogEvents`, `s3:PutObject`, `s3:GetObject`, `s3:HeadObject`, `s3:HeadBucket`, `s3:CreateBucket`, `sts:GetCallerIdentity`
4. **RDS instance** — Performance Insights enabled (7-day free tier), Enhanced Monitoring enabled (60s interval)

---

## API Reference

### Start Server

```bash
cd code/
python -m uvicorn src.api.app:app --host 0.0.0.0 --port 8000
```

### Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/analyses` | Start a new collection job |
| `GET` | `/api/v1/analyses/{job_id}` | Get job status and results |
| `GET` | `/api/v1/analyses` | List all jobs |
| `DELETE` | `/api/v1/analyses/{job_id}` | Delete a job |
| `GET` | `/health` | Health check |

---

## LIVE Mode — Full Collection

Connects to the database via SSM Run Command and collects everything.

### Request

```bash
curl -X POST http://localhost:8000/api/v1/analyses \
  -H "Content-Type: application/json" \
  -d '{
    "engine": "mysql",
    "cluster_endpoint": "mydb.cluster-abc123.us-east-1.rds.amazonaws.com",
    "port": 3306,
    "database_name": "production",
    "mode": "live",
    "live_config": {
      "secret_arn": "arn:aws:secretsmanager:us-east-1:123456789:secret:mydb-creds",
      "automation_instance_id": "i-0abc123def456789"
    },
    "aws_config": {
      "region": "us-east-1",
      "db_instance_identifier": "mydb",
      "collect_cloudwatch_metrics": true,
      "collect_performance_insights": true,
      "collect_database_insights": false
    },
    "force_refresh": false
  }'
```

### Parameters

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `engine` | Yes | — | `mysql` or `mariadb` |
| `cluster_endpoint` | Yes | — | RDS/Aurora endpoint hostname |
| `port` | Yes | — | Database port (typically 3306) |
| `database_name` | Yes | — | Schema/database to analyze |
| `mode` | Yes | — | `live` or `ddl` |
| `live_config.secret_arn` | Yes (live) | — | Secrets Manager ARN with DB credentials |
| `live_config.automation_instance_id` | Yes (live) | — | SSM-managed EC2 instance ID |
| `aws_config.region` | Yes | — | AWS region |
| `aws_config.db_instance_identifier` | No | — | RDS instance ID (enables RDS API + CloudWatch + PI) |
| `aws_config.db_cluster_identifier` | No | — | Aurora cluster ID (alternative to instance ID) |
| `aws_config.collect_cloudwatch_metrics` | No | `true` | Collect CloudWatch metrics |
| `aws_config.collect_performance_insights` | No | `true` | Collect PI data |
| `aws_config.collect_database_insights` | No | `false` | Collect CloudWatch Database Insights (fallback if PI empty) |
| `force_refresh` | No | `false` | Clear S3 cache and re-collect from scratch |

### Response (202 Accepted)

```json
{
  "job_id": "fc65ce52-f923-44f9-85e1-ef19275bfa65",
  "status": "PENDING",
  "created_at": "2026-02-17T23:29:41.475523+00:00",
  "message": "Analysis started"
}
```

---

## DDL Mode — Schema from S3

Parses DDL scripts uploaded to S3 by the frontend. Metrics still collected via AWS APIs.

### Request

```bash
curl -X POST http://localhost:8000/api/v1/analyses \
  -H "Content-Type: application/json" \
  -d '{
    "engine": "mysql",
    "cluster_endpoint": "mydb.cluster-abc123.us-east-1.rds.amazonaws.com",
    "port": 3306,
    "database_name": "production",
    "mode": "ddl",
    "ddl_config": {
      "s3_bucket": "my-modernizer-bucket",
      "s3_key": "uploads/job-002/schema.sql"
    },
    "aws_config": {
      "region": "us-east-1",
      "db_instance_identifier": "mydb"
    }
  }'
```

| Parameter | Required | Description |
|-----------|----------|-------------|
| `ddl_config.s3_bucket` | Yes (ddl) | S3 bucket containing DDL files |
| `ddl_config.s3_key` | Yes (ddl) | S3 key to the DDL SQL file |

**DDL mode differences:**

- No DB connection needed (no `live_config`)
- Schema parsed from `CREATE TABLE` statements
- `row_count` and `size_mb` are 0 (not available from DDL)
- No `sample_data`
- Query patterns come from PI only (not performance_schema)

---

## Poll for Results

```bash
curl http://localhost:8000/api/v1/analyses/{job_id}
```

### Response (200 — Completed)

```json
{
  "job_id": "fc65ce52-...",
  "status": "COMPLETED",
  "created_at": "2026-02-17T23:29:41Z",
  "completed_at": "2026-02-17T23:31:22Z",
  "duration_seconds": 103.32,
  "result": { ... CollectorOutputContract ... }
}
```

Status values: `PENDING` → `RUNNING` → `COMPLETED` or `FAILED`

---

## Metrics Collected

### Metadata (100% coverage)

| Field | Source | Description |
|-------|--------|-------------|
| `collection_timestamp` | System | When collection completed |
| `collector_version` | Hardcoded | Semantic version of collector |
| `collection_duration_seconds` | Measured | Total collection time |
| `source_database.engine` | Input | mysql/mariadb |
| `source_database.version` | `SELECT VERSION()` via SSM | e.g. 8.0.43 |
| `source_database.hostname` | Input | Cluster endpoint |
| `source_database.database_name` | Input | Schema name |
| `source_database.database_size_gb` | `information_schema.tables` via SSM | Total data + index size |
| `source_database.deployment_type` | Derived | `rds_instance` if aws_config provided |

### RDS Instance Metadata (100% coverage)

| Field | Source |
|-------|--------|
| `db_instance_identifier` | RDS DescribeDBInstances API |
| `instance_class` | RDS API |
| `vcpu_count` | Lookup table (t3/t4g/r5/r6g/r7g/r8g/m5/m6g/m7g families) |
| `memory_gb` | Lookup table |
| `storage_type` | RDS API (gp2/gp3/io1/io2) |
| `storage_size_gb` | RDS API |
| `storage_iops` | RDS API |
| `storage_throughput_mbps` | RDS API |
| `multi_az` | RDS API |
| `region`, `availability_zone` | RDS API |
| `read_replica_count` | RDS API |
| `backup_retention_days` | RDS API |
| `performance_insights_enabled` | RDS API |
| `enhanced_monitoring_interval` | RDS API |

### Schema (100% coverage per table)

| Field | Source | Notes |
|-------|--------|-------|
| `table_id` | Derived | `{database}.{table}` |
| `table_name` | `information_schema.tables` via SSM | |
| `schema_name` | Input | |
| `row_count` | `information_schema.tables` via SSM | Approximate |
| `size_mb` | `data_length + index_length` via SSM | |
| `columns` | `information_schema.columns` via SSM | name, type, nullable, default, auto_increment |
| `indexes` | `information_schema.statistics` via SSM | Grouped by index, composite-aware |
| `primary_key` | `information_schema.key_column_usage` via SSM | |
| `foreign_keys` | `key_column_usage + referential_constraints` via SSM | Grouped by constraint |
| `sample_data` | `SELECT * LIMIT N` via SSM | Configurable (default 10 rows) |
| `views` | `information_schema.views` via SSM | Definition, is_updatable |
| `procedures` | `information_schema.routines` via SSM | Name, type, definition |
| `triggers` | `information_schema.triggers` via SSM | Event, timing, definition |

### Query Patterns (47/47 fields — 100% for MySQL)

#### Always Filled (all patterns)

| Field | Source | Description |
|-------|--------|-------------|
| `query_id` | performance_schema DIGEST / PI sql.id | Unique query hash |
| `query_text` | performance_schema + PI `get_dimension_key_details` | Full SQL (PI replaces truncated perf_schema text) |
| `query_type` | Parsed from SQL | SELECT/INSERT/UPDATE/DELETE/OTHER |
| `frequency_per_hour` | `COUNT_STAR / hours` | Executions per hour |
| `calls_per_second` | `COUNT_STAR / (hours * 3600)` | Executions per second |
| `tables_accessed` | Parsed from SQL (FROM/JOIN/INTO/UPDATE) | List of table names |
| `rows_affected_avg` | `SUM_ROWS_AFFECTED / COUNT_STAR` | Avg rows modified |
| `has_joins` | Parsed from SQL | Contains JOIN keyword |
| `has_aggregations` | Parsed from SQL | Contains COUNT/SUM/AVG/GROUP BY |
| `has_subqueries` | Parsed from SQL | Multiple SELECT keywords |
| `cache_hit_ratio_pct` | MySQL GLOBAL STATUS | `(read_requests - reads) / read_requests * 100` |
| `shared_blks_hit` | MySQL GLOBAL STATUS | InnoDB buffer pool hits |
| `shared_blks_read` | MySQL GLOBAL STATUS | InnoDB buffer pool disk reads |
| `io_read_time_ms` | CloudWatch ReadLatency | Avg read latency (ms) |
| `io_write_time_ms` | CloudWatch WriteLatency | Avg write latency (ms) |
| `temp_blocks_read` | MySQL GLOBAL STATUS | Created_tmp_disk_tables |
| `temp_blocks_written` | MySQL GLOBAL STATUS | Created_tmp_tables |
| `avg_logical_reads` | MySQL GLOBAL STATUS | Buffer pool read requests |
| `avg_physical_reads` | MySQL GLOBAL STATUS | Buffer pool disk reads |
| `avg_cpu_time_ms` | Enhanced Monitoring (RDSOSMetrics) | `(mysqld_cpu% / 100) * vcpus * 1000 / qps` |
| `read_write_ratio_pct` | CloudWatch ReadIOPS/WriteIOPS | `read / (read + write) * 100` |

#### Filled from performance_schema (LIVE mode patterns)

| Field | Source | Description |
|-------|--------|-------------|
| `rows_returned_avg` | `SUM_ROWS_SENT / COUNT_STAR` | |
| `rows_returned_p95` | Estimated: `avg_rows * (p95_lat / avg_lat)` | |
| `rows_examined_avg` | `SUM_ROWS_EXAMINED / COUNT_STAR` | |
| `execution_time_ms_avg` | `AVG_TIMER_WAIT / 1e9` | |
| `execution_time_ms_min` | `MIN_TIMER_WAIT / 1e9` | |
| `execution_time_ms_max` | `MAX_TIMER_WAIT / 1e9` | |
| `execution_time_ms_p50` | ≈ avg (approximation) | |
| `execution_time_ms_p95` | `QUANTILE_95 / 1e9` (MySQL 8.0.25+) | |
| `execution_time_ms_p99` | `QUANTILE_99 / 1e9` (MySQL 8.0.25+) | |
| `total_time_ms` | `SUM_TIMER_WAIT / 1e9` | |
| `join_count` | Parsed from SQL | Number of JOIN keywords |
| `filter_columns` | Parsed from WHERE clause | Columns in WHERE/AND/OR |
| `sort_columns` | Parsed from ORDER BY clause | Columns in ORDER BY |
| `full_table_scans` | `SUM_SELECT_SCAN` | |
| `range_scans` | `SUM_SELECT_RANGE` | |
| `scan_efficiency_pct` | `rows_sent / rows_examined * 100` (capped at 100) | |
| `queries_without_index` | `SUM_NO_INDEX_USED` | |
| `queries_with_bad_index` | `SUM_NO_GOOD_INDEX_USED` | |
| `lock_time_ms` | `SUM_LOCK_TIME / 1e9` | |
| `lock_time_pct` | `lock_time / total_time * 100` | |
| `errors` | `SUM_ERRORS` (MySQL 5.7+) | |
| `warnings` | `SUM_WARNINGS` (MySQL 5.7+) | |
| `first_seen` | `FIRST_SEEN` (MySQL 5.7.9+) | |
| `last_seen` | `LAST_SEEN` (MySQL 5.7.9+) | |

#### Filled from Performance Insights (PI patterns)

| Field | Source | Description |
|-------|--------|-------------|
| `db_load_contribution_percent` | PI `describe_dimension_keys` | % of total db.load.avg |
| `wait_events` | PI `describe_dimension_keys` (db.wait_event group) | Top 10 wait events with % |

### CloudWatch Metrics (10/11 — replica_lag only if replicas exist)

| Metric | Unit | Aggregation |
|--------|------|-------------|
| `cpu_utilization` | % | avg, max, min |
| `freeable_memory_gb` | GB | avg, max, min |
| `database_connections` | count | avg, max, min |
| `read_iops` | count/s | avg, max, min |
| `write_iops` | count/s | avg, max, min |
| `read_latency_ms` | ms | avg, max, min |
| `write_latency_ms` | ms | avg, max, min |
| `network_receive_throughput_mbps` | MB/s | avg, max, min |
| `network_transmit_throughput_mbps` | MB/s | avg, max, min |
| `free_storage_space_gb` | GB | avg |
| `replica_lag_ms` | ms | Only if read replicas exist |

### Performance Metrics (derived)

| Metric | Source | Formula |
|--------|--------|---------|
| `avg_query_time_ms` | Query patterns | Mean of all pattern avg times |
| `p50_query_time_ms` | Query patterns | Median of all pattern avg times |
| `p95_query_time_ms` | Query patterns | Max of all pattern p95 times |
| `p99_query_time_ms` | Query patterns | Max of all pattern p99 times |
| `queries_per_second` | Query patterns | `sum(frequency_per_hour) / 3600` |
| `active_connections_avg` | CloudWatch | `database_connections.avg` |
| `active_connections_max` | CloudWatch | `database_connections.max` |
| `read_iops_avg` | CloudWatch | `read_iops.avg` |
| `write_iops_avg` | CloudWatch | `write_iops.avg` |
| `network_throughput_mbps_avg` | CloudWatch | `receive + transmit avg` |

---

## Version Adaptivity

The collector adapts SQL queries based on MySQL version:

| Feature | Minimum Version | Fields |
|---------|----------------|--------|
| Core metrics | 5.6+ | All basic query stats |
| `SUM_ERRORS`, `SUM_WARNINGS` | 5.7.0+ | `errors`, `warnings` |
| `FIRST_SEEN`, `LAST_SEEN` | 5.7.9+ | `first_seen`, `last_seen` |
| `QUANTILE_95`, `QUANTILE_99` | 8.0.25+ | `execution_time_ms_p95/p99` |

Older versions gracefully omit unavailable fields — no errors.

---

## S3 Storage & Checkpoints

### Bucket Structure

```
s3://db-modernizer-{account_id}/
  └── {cluster_name}/
      ├── collector/
      │   ├── metadata.json      ← Stage 1
      │   ├── schema.json        ← Stage 2
      │   ├── queries.json       ← Stage 3
      │   ├── aws_metrics.json   ← Stage 4
      │   └── output.json        ← Final output
      ├── analysis/
      ├── referee/
      └── schema-design/
```

### Resume Behavior

- On start: if `output.json` exists → return immediately (cached)
- Each stage: `load_or_run(stage, fn)` → skip if checkpoint exists
- On failure at stage 3: stages 1-2 already in S3 → next run skips them
- `force_refresh=true` clears all checkpoints before running

---

## Example: Full End-to-End

```bash
# 1. Start collection
JOB_ID=$(curl -s -X POST http://localhost:8000/api/v1/analyses \
  -H "Content-Type: application/json" \
  -d '{
    "engine": "mysql",
    "cluster_endpoint": "mysql-loadtest.crg1zkwikflb.us-east-1.rds.amazonaws.com",
    "port": 3306,
    "database_name": "loadtest",
    "mode": "live",
    "live_config": {
      "secret_arn": "mysql-loadtest",
      "automation_instance_id": "i-0c59bcd83ea62e911"
    },
    "aws_config": {
      "region": "us-east-1",
      "db_instance_identifier": "mysql-loadtest"
    }
  }' | jq -r '.job_id')

echo "Job started: $JOB_ID"

# 2. Poll until complete (~90-120s)
while true; do
  STATUS=$(curl -s "http://localhost:8000/api/v1/analyses/$JOB_ID" | jq -r '.status')
  echo "Status: $STATUS"
  [ "$STATUS" = "COMPLETED" ] || [ "$STATUS" = "FAILED" ] && break
  sleep 10
done

# 3. Get results
curl -s "http://localhost:8000/api/v1/analyses/$JOB_ID" | jq '.result' > output.json
```

---

## Data Sources Summary

| Data Source | Connection Method | What It Provides |
|-------------|------------------|------------------|
| **MySQL DB** | SSM Run Command → mysql CLI | Schema, query patterns, global stats, sample data |
| **Secrets Manager** | boto3 (direct) | Database credentials |
| **RDS API** | boto3 (direct) | Instance metadata (class, storage, Multi-AZ) |
| **CloudWatch Metrics** | boto3 (direct) | CPU, memory, IOPS, latency, connections (7-day) |
| **Performance Insights** | boto3 (direct) | Top SQL by load, wait events, full SQL text |
| **Enhanced Monitoring** | CloudWatch Logs (RDSOSMetrics) | Per-process CPU (mysqld) |
| **S3** | boto3 (direct) | Checkpoint storage, DDL files (DDL mode) |
