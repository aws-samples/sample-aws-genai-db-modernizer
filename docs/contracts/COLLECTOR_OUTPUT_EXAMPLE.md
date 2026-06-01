# Collector Output Contract - Real-World Example

**Version:** 2.0
**Date:** February 2, 2026
**Purpose:** Show concrete example of collector output with all performance metrics

---

## Complete Example Output

This example shows what a MySQL collector would output for a production e-commerce database:

```json
{
  "contract_version": "2.0",
  "job_id": "job-abc-123-2026-02-02",
  "metadata": {
    "collection_timestamp": "2026-02-02T14:23:45Z",
    "collector_version": "2.3.1",
    "collection_duration_seconds": 847.5,
    "source_database": {
      "engine": "mysql",
      "version": "8.0.32",
      "hostname": "prod-ecommerce-db.abc123.us-east-1.rds.amazonaws.com",
      "database_name": "ecommerce",
      "database_size_gb": 24.6,
      "deployment_type": "rds_instance",
      "rds_instance_metadata": {
        "db_instance_identifier": "prod-ecommerce-db",
        "instance_class": "db.r5.2xlarge",
        "vcpu_count": 8,
        "memory_gb": 64,
        "storage_type": "gp3",
        "storage_size_gb": 500,
        "storage_iops": 12000,
        "storage_throughput_mbps": 500,
        "multi_az": true,
        "region": "us-east-1",
        "availability_zone": "us-east-1a",
        "read_replica_count": 2,
        "backup_retention_days": 7,
        "performance_insights_enabled": true,
        "enhanced_monitoring_interval": 60
      }
    }
  },
  "schema": {
    "tables": [
      {
        "table_id": "ecommerce.users",
        "table_name": "users",
        "schema_name": "ecommerce",
        "row_count": 5000000,
        "size_mb": 2048.5,
        "columns": [
          {
            "column_name": "user_id",
            "ordinal_position": 1,
            "data_type": "bigint",
            "normalized_data_type": "integer",
            "max_length": null,
            "nullable": false,
            "default_value": null,
            "is_auto_increment": true,
            "cardinality": 5000000
          },
          {
            "column_name": "email",
            "ordinal_position": 2,
            "data_type": "varchar(255)",
            "normalized_data_type": "string",
            "max_length": 255,
            "nullable": false,
            "default_value": null,
            "is_auto_increment": false,
            "cardinality": 4950000
          },
          {
            "column_name": "created_at",
            "ordinal_position": 3,
            "data_type": "timestamp",
            "normalized_data_type": "timestamp",
            "max_length": null,
            "nullable": false,
            "default_value": "CURRENT_TIMESTAMP",
            "is_auto_increment": false,
            "cardinality": 1825
          }
        ],
        "indexes": [
          {
            "index_name": "PRIMARY",
            "columns": ["user_id"],
            "is_unique": true,
            "is_primary": true,
            "index_type": "btree"
          },
          {
            "index_name": "idx_email",
            "columns": ["email"],
            "is_unique": true,
            "is_primary": false,
            "index_type": "btree"
          }
        ],
        "primary_key": ["user_id"],
        "foreign_keys": [],
        "sample_data": [
          {
            "user_id": 1,
            "email": "user1@example.com",
            "created_at": "2024-01-15T10:30:00Z"
          },
          {
            "user_id": 2,
            "email": "user2@example.com",
            "created_at": "2024-01-16T14:22:00Z"
          }
        ]
      },
      {
        "table_id": "ecommerce.orders",
        "table_name": "orders",
        "schema_name": "ecommerce",
        "row_count": 15000000,
        "size_mb": 8192.3,
        "columns": [
          {
            "column_name": "order_id",
            "ordinal_position": 1,
            "data_type": "bigint",
            "normalized_data_type": "integer",
            "max_length": null,
            "nullable": false,
            "default_value": null,
            "is_auto_increment": true,
            "cardinality": 15000000
          },
          {
            "column_name": "user_id",
            "ordinal_position": 2,
            "data_type": "bigint",
            "normalized_data_type": "integer",
            "max_length": null,
            "nullable": false,
            "default_value": null,
            "is_auto_increment": false,
            "cardinality": 4500000
          },
          {
            "column_name": "total_amount",
            "ordinal_position": 3,
            "data_type": "decimal(10,2)",
            "normalized_data_type": "decimal",
            "max_length": null,
            "nullable": false,
            "default_value": null,
            "is_auto_increment": false,
            "cardinality": 50000
          }
        ],
        "indexes": [
          {
            "index_name": "PRIMARY",
            "columns": ["order_id"],
            "is_unique": true,
            "is_primary": true,
            "index_type": "btree"
          },
          {
            "index_name": "idx_user_id",
            "columns": ["user_id"],
            "is_unique": false,
            "is_primary": false,
            "index_type": "btree"
          }
        ],
        "primary_key": ["order_id"],
        "foreign_keys": [
          {
            "constraint_name": "fk_orders_user",
            "columns": ["user_id"],
            "referenced_table": "ecommerce.users",
            "referenced_columns": ["user_id"],
            "on_delete": "CASCADE",
            "on_update": "CASCADE"
          }
        ],
        "sample_data": null
      }
    ],
    "views": [],
    "procedures": [],
    "triggers": []
  },
  "queries": {
    "query_patterns": [
      {
        "query_id": "pi-digest-abc123",
        "query_text": "SELECT * FROM users WHERE email = ?",
        "query_type": "SELECT",
        "frequency_per_hour": 12500.0,
        "calls_per_second": 3.47,
        "tables_accessed": ["ecommerce.users"],
        "rows_returned_avg": 1.0,
        "rows_returned_p95": 1.0,
        "rows_affected_avg": null,
        "rows_examined_avg": 1.0,
        "execution_time_ms_avg": 2.3,
        "execution_time_ms_min": 0.8,
        "execution_time_ms_max": 45.2,
        "execution_time_ms_p50": 1.9,
        "execution_time_ms_p95": 4.5,
        "execution_time_ms_p99": 8.7,
        "total_time_ms": 28750.0,
        "db_load_contribution_percent": 15.2,
        "has_joins": false,
        "join_count": 0,
        "has_aggregations": false,
        "has_subqueries": false,
        "filter_columns": ["email"],
        "sort_columns": null,
        "full_table_scans": 0,
        "range_scans": 0,
        "scan_efficiency_pct": 100.0,
        "queries_without_index": 0,
        "queries_with_bad_index": 0,
        "lock_time_ms": 0.1,
        "lock_time_pct": 0.04,
        "cache_hit_ratio_pct": null,
        "shared_blks_hit": null,
        "shared_blks_read": null,
        "io_read_time_ms": null,
        "io_write_time_ms": null,
        "temp_blocks_read": null,
        "temp_blocks_written": null,
        "avg_logical_reads": null,
        "avg_physical_reads": null,
        "avg_cpu_time_ms": null,
        "read_write_ratio_pct": null,
        "errors": 0,
        "warnings": 0,
        "first_seen": "2026-01-26T00:00:00Z",
        "last_seen": "2026-02-02T14:00:00Z",
        "wait_events": [
          {
            "event_name": "io/file/innodb/innodb_data_file",
            "wait_time_ms": 0.3,
            "wait_time_percent": 13.0
          }
        ]
      },
      {
        "query_id": "pi-digest-def456",
        "query_text": "SELECT o.*, u.email FROM orders o JOIN users u ON o.user_id = u.user_id WHERE o.created_at > ?",
        "query_type": "SELECT",
        "frequency_per_hour": 8500.0,
        "calls_per_second": 2.36,
        "tables_accessed": ["ecommerce.orders", "ecommerce.users"],
        "rows_returned_avg": 15.3,
        "rows_returned_p95": 42.0,
        "rows_affected_avg": null,
        "rows_examined_avg": 18.7,
        "execution_time_ms_avg": 8.5,
        "execution_time_ms_min": 3.2,
        "execution_time_ms_max": 125.4,
        "execution_time_ms_p50": 7.1,
        "execution_time_ms_p95": 18.9,
        "execution_time_ms_p99": 35.6,
        "total_time_ms": 72250.0,
        "db_load_contribution_percent": 38.1,
        "has_joins": true,
        "join_count": 1,
        "has_aggregations": false,
        "has_subqueries": false,
        "filter_columns": ["created_at"],
        "sort_columns": null,
        "full_table_scans": 0,
        "range_scans": 8500,
        "scan_efficiency_pct": 81.8,
        "queries_without_index": 0,
        "queries_with_bad_index": 0,
        "lock_time_ms": 0.2,
        "lock_time_pct": 0.02,
        "cache_hit_ratio_pct": null,
        "shared_blks_hit": null,
        "shared_blks_read": null,
        "io_read_time_ms": null,
        "io_write_time_ms": null,
        "temp_blocks_read": null,
        "temp_blocks_written": null,
        "avg_logical_reads": null,
        "avg_physical_reads": null,
        "avg_cpu_time_ms": null,
        "read_write_ratio_pct": null,
        "errors": 0,
        "warnings": 0,
        "first_seen": "2026-01-26T00:00:00Z",
        "last_seen": "2026-02-02T14:00:00Z",
        "wait_events": [
          {
            "event_name": "io/file/innodb/innodb_data_file",
            "wait_time_ms": 1.2,
            "wait_time_percent": 14.1
          }
        ]
      }
    ],
    "total_queries_analyzed": 125000,
    "query_log_source": "performance_insights",
    "collection_start_time": "2026-01-26T00:00:00Z",
    "collection_end_time": "2026-02-02T14:00:00Z"
  },
  "metrics": {
    "performance_metrics": {
      "avg_query_time_ms": 5.2,
      "p50_query_time_ms": 3.8,
      "p95_query_time_ms": 12.5,
      "p99_query_time_ms": 28.3,
      "queries_per_second": 1250.5,
      "connection_pool_usage_percent": 65.3,
      "active_connections_avg": 392.0,
      "active_connections_max": 600.0,
      "transactions_per_second": 845.2,
      "read_iops_avg": 141442.7,
      "write_iops_avg": 38558.6,
      "network_throughput_mbps_avg": 3087.5
    },
    "rds_cloudwatch_metrics": {
      "cpu_utilization": {
        "avg": 45.2,
        "max": 78.5,
        "min": 12.3,
        "p95": 68.9
      },
      "freeable_memory_gb": {
        "avg": 28.5,
        "max": 52.3,
        "min": 8.7,
        "p95": 15.2
      },
      "database_connections": {
        "avg": 392.0,
        "max": 600.0,
        "min": 150.0,
        "p95": 550.0
      },
      "read_iops": {
        "avg": 141442.7,
        "max": 185000.0,
        "min": 95000.0,
        "p95": 175000.0
      },
      "write_iops": {
        "avg": 38558.6,
        "max": 52000.0,
        "min": 25000.0,
        "p95": 48000.0
      },
      "read_latency_ms": {
        "avg": 0.6,
        "max": 2.3,
        "min": 0.2,
        "p95": 1.2
      },
      "write_latency_ms": {
        "avg": 3.8,
        "max": 12.5,
        "min": 1.2,
        "p95": 8.7
      },
      "network_receive_throughput_mbps": {
        "avg": 2563.2,
        "max": 3500.0,
        "min": 1200.0,
        "p95": 3200.0
      },
      "network_transmit_throughput_mbps": {
        "avg": 640.5,
        "max": 950.0,
        "min": 300.0,
        "p95": 850.0
      },
      "free_storage_space_gb": 125.3,
      "replica_lag_ms": {
        "avg": 45.2,
        "max": 125.0,
        "min": 15.0,
        "p95": 95.0
      }
    }
  }
}
```

---

## Key Performance Metrics Summary

### Database Size and Configuration

- **Database Size**: 24.6 TB
- **Instance Type**: db.r5.2xlarge (8 vCPU, 64 GB RAM)
- **Storage**: gp3 (500 GB, 12,000 IOPS, 500 MB/s throughput)
- **Multi-AZ**: Enabled
- **Read Replicas**: 2

### IOPS Metrics

- **Total IOPS**: 170,103.0 (avg: 141,442.7 read + 38,558.6 write)
- **Read IOPS**: 141,442.7 (avg), 175,000 (p95)
- **Write IOPS**: 38,558.6 (avg), 48,000 (p95)

### Throughput Metrics

- **Total Throughput**: 3,087.5 MB/s (avg: 2,563.2 read + 640.5 write)
- **Read Throughput**: 2,563.2 MB/s (avg), 3,200 MB/s (p95)
- **Write Throughput**: 640.5 MB/s (avg), 850 MB/s (p95)

### Response Time Metrics

- **Read Response Time**: 0.6 ms (avg), 1.2 ms (p95)
- **Write Response Time**: 3.8 ms (avg), 8.7 ms (p95)

### Connection Metrics

- **Total Sessions**: 6,000 (max configured)
- **Active Sessions**: 600 (max observed), 392 (avg)
- **Connection Pool Usage**: 65.3%

### Cache Metrics

- **Database Cache Size**: 64 GB (instance memory)
- **Freeable Memory**: 28.5 GB (avg), 8.7 GB (min)
- **Cache Hit Ratio**: 99% (inferred from low read latency)

---

## How This Data is Used

### 1. DynamoDB Analysis Agent

Uses this data to identify:

- High-throughput key-value queries (users table lookup by email)
- Read-heavy workloads (141K read IOPS)
- Low-latency requirements (0.6ms read response time)

**Recommendation**: Migrate `users` table to DynamoDB

- Current: 141K read IOPS on MySQL
- DynamoDB: On-demand capacity, sub-millisecond latency
- Estimated savings: $800/month

### 2. Aurora Analysis Agent

Uses this data to identify:

- Complex join queries (orders + users)
- Transaction requirements
- Multi-AZ configuration

**Recommendation**: Keep `orders` table in Aurora MySQL

- Maintains ACID transactions
- Supports complex joins
- Compatible with existing queries

### 3. Referee Agent

Uses this data to:

- Calculate TCO (current: $5,000/month)
- Prioritize recommendations (high IOPS = high priority)
- Assess migration complexity

---

## UI Display Example

This data would be displayed in the UI as:

```
📊 Source Database Overview

Database: MySQL 8.0.32 (prod-ecommerce-db)
Size: 24.6 TB | Tables: 250 | Rows: 20M+

Performance Metrics:
┌─────────────────────┬──────────┬──────────┬──────────┐
│ Metric              │ Average  │ P95      │ Max      │
├─────────────────────┼──────────┼──────────┼──────────┤
│ Read IOPS           │ 141,443  │ 175,000  │ 185,000  │
│ Write IOPS          │ 38,559   │ 48,000   │ 52,000   │
│ Read Throughput     │ 2,563 MB │ 3,200 MB │ 3,500 MB │
│ Write Throughput    │ 641 MB   │ 850 MB   │ 950 MB   │
│ Read Latency        │ 0.6 ms   │ 1.2 ms   │ 2.3 ms   │
│ Write Latency       │ 3.8 ms   │ 8.7 ms   │ 12.5 ms  │
│ Active Connections  │ 392      │ 550      │ 600      │
│ CPU Utilization     │ 45.2%    │ 68.9%    │ 78.5%    │
└─────────────────────┴──────────┴──────────┴──────────┘

Top Queries by DB Load:
1. Orders + Users JOIN (38.1% of DB load)
2. User lookup by email (15.2% of DB load)
```

---

## Related Documentation

- Collector Output Contract: `src/contracts/collector_output.py`
- High-Level Design: `../architecture/high-level-design.md`
