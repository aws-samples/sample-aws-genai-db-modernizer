# Database Collection Matrix

## Data Collection Capabilities by Source Database

This document defines what data the Collector Agent can collect from each source database type, guiding contract design and implementation.

---

## Collection Strategy: Common + Optional Pattern

### **Philosophy:**

- **Common Fields**: Guaranteed to be collected from ALL source databases
- **Optional Fields**: Collected when available, marked as optional in contract
- **Graceful Degradation**: Analysis agents work with varying data richness

### **Why This Approach:**

1. ✅ **Maximizes analysis quality** when rich data is available
2. ✅ **Works with minimal data** when features unavailable
3. ✅ **Future-proof** for Oracle, DB2, and new databases
4. ✅ **Clear expectations** via contract documentation

---

## Schema Information (COMMON - All Databases)

| Data Type | MySQL | PostgreSQL | SQL Server | Oracle* | DB2* | Collection Method |
|-----------|-------|------------|------------|---------|------|-------------------|
| **Tables** | ✅ Yes | ✅ Yes | ✅ Yes | ✅ Yes | ✅ Yes | INFORMATION_SCHEMA.TABLES |
| **Columns** | ✅ Yes | ✅ Yes | ✅ Yes | ✅ Yes | ✅ Yes | INFORMATION_SCHEMA.COLUMNS |
| **Primary Keys** | ✅ Yes | ✅ Yes | ✅ Yes | ✅ Yes | ✅ Yes | INFORMATION_SCHEMA.KEY_COLUMN_USAGE |
| **Foreign Keys** | ✅ Yes | ✅ Yes | ✅ Yes | ✅ Yes | ✅ Yes | INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS |
| **Indexes** | ✅ Yes | ✅ Yes | ✅ Yes | ✅ Yes | ✅ Yes | Database-specific system tables |
| **Views** | ✅ Yes | ✅ Yes | ✅ Yes | ✅ Yes | ✅ Yes | INFORMATION_SCHEMA.VIEWS |
| **Stored Procedures** | ✅ Yes | ✅ Yes | ✅ Yes | ✅ Yes | ✅ Yes | Database-specific system tables |
| **Functions** | ✅ Yes | ✅ Yes | ✅ Yes | ✅ Yes | ✅ Yes | Database-specific system tables |
| **Triggers** | ✅ Yes | ✅ Yes | ✅ Yes | ✅ Yes | ✅ Yes | Database-specific system tables |

**Status:** ✅ GUARANTEED from all databases

\* Oracle and DB2 support is [PLANNED]. Current implementation: MySQL, PostgreSQL, MariaDB.

---

## Query Performance Metrics

### **Common Fields (GUARANTEED)**

| Field | Description | MySQL | PostgreSQL | SQL Server | Collection Source |
|-------|-------------|-------|------------|------------|-------------------|
| `query_id` | Unique identifier | ✅ | ✅ | ✅ | Query digest/hash |
| `query_text` | Normalized query | ✅ | ✅ | ✅ | Performance schema/DMVs |
| `query_type` | SELECT/INSERT/UPDATE/DELETE | ✅ | ✅ | ✅ | Query parsing |
| `frequency_per_hour` | Executions per hour | ✅ | ✅ | ✅ | Calculated from execution count |
| `calls_per_second` | Queries per second | ✅ | ✅ | ✅ | Calculated from timestamps |
| `tables_accessed` | Tables in query | ✅ | ✅ | ✅ | Query parsing |
| `execution_time_ms_avg` | Average execution time | ✅ | ✅ | ✅ | performance_schema/pg_stat_statements/DMVs |
| `execution_time_ms_min` | Minimum execution time | ✅ | ✅ | ✅ | performance_schema/pg_stat_statements/DMVs |
| `execution_time_ms_max` | Maximum execution time | ✅ | ✅ | ✅ | performance_schema/pg_stat_statements/DMVs |
| `total_time_ms` | Total time spent | ✅ | ✅ | ✅ | Calculated |
| `rows_returned_avg` | Average rows returned | ✅ | ✅ | ✅ | All sources track this |
| `rows_affected_avg` | Average rows modified | ✅ | ✅ | ✅ | All sources track this |
| `first_seen` | First execution timestamp | ✅ | ✅ | ✅ | Tracked by all |
| `last_seen` | Last execution timestamp | ✅ | ✅ | ✅ | Tracked by all |

### **MySQL-Specific Fields (OPTIONAL)**

| Field | Description | Source |
|-------|-------------|--------|
| `rows_examined_avg` | Average rows scanned | `performance_schema.events_statements_summary_by_digest.SUM_ROWS_EXAMINED` |
| `full_table_scans` | Number of full table scans | `performance_schema.events_statements_summary_by_digest.SUM_SELECT_SCAN` |
| `range_scans` | Number of range scans | `performance_schema.events_statements_summary_by_digest.SUM_SELECT_RANGE` |
| `scan_efficiency_pct` | Scan efficiency (rows returned / rows examined) | Calculated |
| `queries_without_index` | Queries that didn't use index | `performance_schema.events_statements_summary_by_digest.SUM_NO_INDEX_USED` |
| `queries_with_bad_index` | Queries with suboptimal index | `performance_schema.events_statements_summary_by_digest.SUM_NO_GOOD_INDEX_USED` |
| `lock_time_ms` | Total lock time | `performance_schema.events_statements_summary_by_digest.SUM_LOCK_TIME` |
| `lock_time_pct` | Lock time percentage | Calculated |

### **PostgreSQL-Specific Fields (OPTIONAL)**

| Field | Description | Source |
|-------|-------------|--------|
| `cache_hit_ratio_pct` | Cache hit ratio | `shared_blks_hit / (shared_blks_hit + shared_blks_read) * 100` |
| `shared_blks_hit` | Blocks found in cache | `pg_stat_statements.shared_blks_hit` |
| `shared_blks_read` | Blocks read from disk | `pg_stat_statements.shared_blks_read` |
| `io_read_time_ms` | Time reading from disk | `pg_stat_statements.blk_read_time` |
| `io_write_time_ms` | Time writing to disk | `pg_stat_statements.blk_write_time` |
| `temp_blocks_read` | Temp blocks read | `pg_stat_statements.temp_blks_read` |
| `temp_blocks_written` | Temp blocks written | `pg_stat_statements.temp_blks_written` |

### **SQL Server-Specific Fields (OPTIONAL)**

| Field | Description | Source |
|-------|-------------|--------|
| `avg_logical_reads` | Average logical reads (buffer cache) | `sys.dm_exec_query_stats.total_logical_reads / execution_count` |
| `avg_physical_reads` | Average physical reads (disk) | `sys.dm_exec_query_stats.total_physical_reads / execution_count` |
| `avg_cpu_time_ms` | Average CPU time | `sys.dm_exec_query_stats.total_worker_time / execution_count / 1000` |
| `read_write_ratio_pct` | Read vs write ratio | Calculated |

---

## Table I/O Statistics

### **Common Fields (GUARANTEED)**

| Field | MySQL | PostgreSQL | SQL Server | Description |
|-------|-------|------------|------------|-------------|
| `table_name` | ✅ | ✅ | ✅ | Table name |
| `row_count` | ✅ | ✅ | ✅ | Approximate row count |
| `data_size_mb` | ✅ | ✅ | ✅ | Table data size |
| `index_size_mb` | ✅ | ✅ | ✅ | Index size |
| `total_size_mb` | ✅ | ✅ | ✅ | Total size (data + indexes) |

### **Optional I/O Fields**

| Field | MySQL | PostgreSQL | SQL Server | Source |
|-------|-------|------------|------------|--------|
| `sequential_scans` | ✅ | ✅ | ✅ | Table scan operations |
| `index_scans` | ✅ | ✅ | ✅ | Index scan operations |
| `inserts` | ✅ | ✅ | ❌ | Insert operations |
| `updates` | ✅ | ✅ | ✅ | Update operations |
| `deletes` | ✅ | ✅ | ❌ | Delete operations |

**MySQL Source:**

```sql
-- From performance_schema.table_io_waits_summary_by_table
COUNT_STAR, COUNT_READ, COUNT_WRITE, COUNT_FETCH, COUNT_INSERT, COUNT_UPDATE, COUNT_DELETE
```

**PostgreSQL Source:**

```sql
-- From pg_stat_user_tables
seq_scan, seq_tup_read, idx_scan, idx_tup_fetch, n_tup_ins, n_tup_upd, n_tup_del
```

**SQL Server Source:**

```sql
-- From sys.dm_db_index_usage_stats
user_seeks, user_scans, user_lookups, user_updates
```

---

## Index Usage Statistics

### **Common Fields (GUARANTEED)**

| Field | MySQL | PostgreSQL | SQL Server |
|-------|-------|------------|------------|
| `index_name` | ✅ | ✅ | ✅ |
| `table_name` | ✅ | ✅ | ✅ |
| `index_type` | ✅ | ✅ | ✅ |
| `is_unique` | ✅ | ✅ | ✅ |
| `columns` | ✅ | ✅ | ✅ |
| `cardinality` | ✅ | ✅ | ❌ |

### **Optional Index Fields**

| Field | MySQL | PostgreSQL | SQL Server | Description |
|-------|-------|------------|------------|-------------|
| `index_scans` | ✅ | ✅ | ✅ | Number of index scans |
| `index_seeks` | ❌ | ❌ | ✅ | Number of index seeks (SQL Server) |
| `index_lookups` | ❌ | ❌ | ✅ | Number of bookmark lookups |
| `tuples_read` | ❌ | ✅ | ❌ | Tuples read (PostgreSQL) |
| `tuples_fetched` | ❌ | ✅ | ❌ | Tuples fetched (PostgreSQL) |

---

## RDS/Cloud-Specific Metadata

### **RDS Instances (When Available)**

| Field | Description | Source |
|-------|-------------|--------|
| `db_instance_identifier` | RDS instance ID | RDS DescribeDBInstances API |
| `instance_class` | Instance type (e.g., db.r5.xlarge) | RDS API |
| `vcpu_count` | Number of vCPUs | RDS API |
| `memory_gb` | Memory in GB | RDS API |
| `storage_type` | EBS storage type (gp2/gp3/io1/io2) | RDS API |
| `storage_size_gb` | Allocated storage | RDS API |
| `storage_iops` | Provisioned IOPS | RDS API |
| `multi_az` | Multi-AZ enabled | RDS API |
| `region` | AWS region | RDS API |
| `performance_insights_enabled` | PI enabled | RDS API |
| `backup_retention_days` | Backup retention | RDS API |

### **CloudWatch Metrics (When Available)**

| Metric | Description | Period |
|--------|-------------|--------|
| `cpu_utilization` | CPU usage (avg, max, p95) | Last 7 days |
| `freeable_memory_gb` | Available memory | Last 7 days |
| `database_connections` | Active connections | Last 7 days |
| `read_iops` | Read IOPS | Last 7 days |
| `write_iops` | Write IOPS | Last 7 days |
| `read_latency_ms` | Read latency | Last 7 days |
| `write_latency_ms` | Write latency | Last 7 days |
| `network_throughput_mbps` | Network I/O | Last 7 days |
| `free_storage_space_gb` | Free storage | Current |
| `replica_lag_ms` | Replication lag (if replicas exist) | Last 7 days |

---

## Stored Procedures / Functions

### **Common Fields (ALL Databases)**

| Field | MySQL | PostgreSQL | SQL Server | Oracle* | DB2* |
|-------|-------|------------|------------|---------|------|
| `procedure_id` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `procedure_name` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `procedure_type` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `definition` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `parameters` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `return_type` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `language` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `referenced_tables` | ✅ | ✅ | ✅ | ✅ | ✅ |

### **Optional Execution Stats**

| Field | MySQL | PostgreSQL | SQL Server | Source |
|-------|-------|------------|------------|--------|
| `total_executions` | ✅ | ❌ | ✅ | `performance_schema.events_statements_summary_by_program` / `sys.dm_exec_procedure_stats` |
| `avg_execution_time_ms` | ✅ | ❌ | ✅ | Same as above |
| `last_executed` | ✅ | ❌ | ✅ | Same as above |

**Note:** PostgreSQL `pg_stat_statements` doesn't track stored procedure execution stats separately by default.

---

## Triggers

### **Common Fields (ALL Databases)**

| Field | MySQL | PostgreSQL | SQL Server | Oracle* | DB2* |
|-------|-------|------------|------------|---------|------|
| `trigger_id` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `trigger_name` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `table_id` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `event_type` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `timing` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `for_each` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `definition` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `is_enabled` | ✅ | ✅ | ✅ | ✅ | ✅ |

### **Optional Execution Stats**

| Field | MySQL | PostgreSQL | SQL Server | Source |
|-------|-------|------------|------------|--------|
| `total_executions` | ✅ | ❌ | ❌ | `performance_schema.events_statements_summary_by_program` |
| `avg_execution_time_ms` | ✅ | ❌ | ❌ | Same as above |

---

## Views

### **Common Fields (ALL Databases)**

| Field | MySQL | PostgreSQL | SQL Server | Oracle* | DB2* |
|-------|-------|------------|------------|---------|------|
| `view_id` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `view_name` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `definition` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `is_updatable` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `column_list` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `referenced_tables` | ✅ | ✅ | ✅ | ✅ | ✅ |

---

## Collection Methods by Database

### **MySQL**

| Data Type | Primary Collection Method | Alternative Method |
|-----------|---------------------------|-------------------|
| Schema | `INFORMATION_SCHEMA` tables | `SHOW` commands |
| Query Performance | `performance_schema.events_statements_summary_by_digest` | `slow_query_log`, `general_log` |
| Table I/O | `performance_schema.table_io_waits_summary_by_table` | N/A |
| Index Usage | `performance_schema.table_io_waits_summary_by_index_usage` | N/A |
| Stored Procedures | `INFORMATION_SCHEMA.ROUTINES` + `performance_schema.events_statements_summary_by_program` | N/A |
| Triggers | `INFORMATION_SCHEMA.TRIGGERS` + `performance_schema.events_statements_summary_by_program` | N/A |

### **PostgreSQL**

| Data Type | Primary Collection Method | Alternative Method |
|-----------|---------------------------|-------------------|
| Schema | `information_schema` tables | `pg_catalog` system tables |
| Query Performance | `pg_stat_statements` (extension) | N/A |
| Table I/O | `pg_stat_user_tables` | N/A |
| Index Usage | `pg_stat_user_indexes` | N/A |
| Stored Procedures | `information_schema.routines` | `pg_proc` |
| Triggers | `information_schema.triggers` | `pg_trigger` |

### **SQL Server**

| Data Type | Primary Collection Method | Alternative Method |
|-----------|---------------------------|-------------------|
| Schema | `INFORMATION_SCHEMA` views | `sys` catalog views |
| Query Performance | `sys.dm_exec_query_stats` + `sys.dm_exec_sql_text` | Query Store (if enabled) |
| Table I/O | `sys.dm_db_partition_stats` + `sys.dm_db_index_usage_stats` | N/A |
| Index Usage | `sys.dm_db_index_usage_stats` | N/A |
| Stored Procedures | `INFORMATION_SCHEMA.ROUTINES` + `sys.dm_exec_procedure_stats` | N/A |
| Triggers | `sys.triggers` + `sys.sql_modules` | N/A |

### **Redis** [PLANNED]

| Data Type | Primary Collection Method | Alternative Method |
|-----------|---------------------------|-------------------|
| Data Structures | `TYPE` command + `SCAN` for key sampling | `KEYS` (not recommended for production) |
| Command Performance | `MONITOR` command (real-time) | `SLOWLOG` (slow commands only) |
| Memory Usage | `INFO memory` + `MEMORY USAGE` per key | `DEBUG OBJECT` (deprecated) |
| Key Patterns | `SCAN` with pattern matching | `KEYS` pattern (not recommended) |
| Data Sampling | `GET`, `HGETALL`, `LRANGE`, `SMEMBERS`, `ZRANGE` | N/A |
| Persistence Config | `INFO persistence` + `CONFIG GET` | N/A |
| Replication Status | `INFO replication` | N/A |
| CloudWatch Metrics (ElastiCache) | CloudWatch API | N/A |

**Redis Collection Notes:**

- **MONITOR**: Provides real-time command stream but has performance impact. Use sparingly and for limited duration.
- **SCAN**: Safe for production use, iterates keys without blocking. Preferred over KEYS command.
- **SLOWLOG**: Captures slow commands without performance impact. Configure threshold appropriately.
- **INFO**: Provides comprehensive server statistics including memory, persistence, replication, and stats.
- **CloudWatch**: For ElastiCache, provides managed metrics without direct Redis access.

---

## Redis Data Collection Specifications [PLANNED]

### **Redis Command Patterns (COMMON)**

| Field | Description | Collection Source |
|-------|-------------|-------------------|
| `command_name` | Redis command (GET, SET, ZADD, etc.) | MONITOR or SLOWLOG |
| `command_type` | Read/Write/Admin | Command classification |
| `frequency_per_hour` | Executions per hour | Calculated from command logs |
| `calls_per_second` | Commands per second | Calculated from timestamps |
| `keys_accessed` | Key patterns accessed | Command parsing |
| `execution_time_ms_avg` | Average execution time | SLOWLOG (for slow commands) |
| `first_seen` | First execution timestamp | Tracked during collection |
| `last_seen` | Last execution timestamp | Tracked during collection |

### **Redis Data Structure Metadata (COMMON)**

| Field | Description | Collection Source |
|-------|-------------|-------------------|
| `data_type` | String/Hash/List/Set/Sorted Set/Stream | TYPE command |
| `key_pattern` | Key naming pattern | Pattern analysis |
| `key_count` | Number of keys of this type | Counted during SCAN |
| `avg_size_bytes` | Average size per key | MEMORY USAGE command |
| `total_size_bytes` | Total size for this type | Calculated |
| `ttl_usage` | Percentage of keys with TTL | TTL command sampling |
| `avg_ttl_seconds` | Average TTL value | TTL command sampling |

### **Redis Memory Statistics (COMMON)**

| Field | Description | Collection Source |
|-------|-------------|-------------------|
| `used_memory_bytes` | Total memory used | INFO memory |
| `used_memory_rss_bytes` | Resident set size | INFO memory |
| `used_memory_peak_bytes` | Peak memory usage | INFO memory |
| `used_memory_overhead_bytes` | Redis overhead | INFO memory |
| `used_memory_dataset_bytes` | Actual data size | INFO memory |
| `mem_fragmentation_ratio` | Fragmentation ratio | INFO memory |
| `maxmemory_bytes` | Max memory limit | CONFIG GET maxmemory |
| `maxmemory_policy` | Eviction policy | CONFIG GET maxmemory-policy |

### **Redis Persistence Configuration (COMMON)**

| Field | Description | Collection Source |
|-------|-------------|-------------------|
| `rdb_enabled` | RDB snapshots enabled | CONFIG GET save |
| `rdb_last_save_time` | Last RDB save timestamp | INFO persistence |
| `rdb_changes_since_save` | Changes since last save | INFO persistence |
| `aof_enabled` | AOF enabled | CONFIG GET appendonly |
| `aof_rewrite_in_progress` | AOF rewrite status | INFO persistence |
| `aof_last_rewrite_time_sec` | Last rewrite duration | INFO persistence |

### **ElastiCache CloudWatch Metrics (OPTIONAL)**

| Metric | Description | Period |
|--------|-------------|--------|
| `cpu_utilization` | CPU usage (avg, max, p95) | Last 7 days |
| `freeable_memory_bytes` | Available memory | Last 7 days |
| `network_bytes_in` | Network input | Last 7 days |
| `network_bytes_out` | Network output | Last 7 days |
| `cache_hits` | Cache hit count | Last 7 days |
| `cache_misses` | Cache miss count | Last 7 days |
| `cache_hit_rate` | Hit rate percentage | Calculated |
| `evictions` | Evicted keys count | Last 7 days |
| `replication_lag` | Replication lag (ms) | Last 7 days |
| `curr_connections` | Current connections | Last 7 days |
| `new_connections` | New connections | Last 7 days |

### **Redis Sample Data Collection**

**Data Structure Sampling Strategy:**

| Data Type | Sampling Method | Max Samples | Notes |
|-----------|----------------|-------------|-------|
| **String** | `GET` command | 1000 keys | Truncate values >1KB |
| **Hash** | `HGETALL` command | 1000 keys | Limit to 100 fields per hash |
| **List** | `LRANGE 0 99` | 1000 keys | First 100 elements only |
| **Set** | `SMEMBERS` or `SSCAN` | 1000 keys | Limit to 100 members |
| **Sorted Set** | `ZRANGE 0 99` | 1000 keys | First 100 elements with scores |
| **Stream** | `XRANGE` | 100 keys | Last 100 entries only |

**Anonymization Rules:**

- Key names: Preserve pattern structure, anonymize identifiers (user:123 → user:XXX)
- String values: Detect and anonymize email, phone, SSN patterns
- Hash fields: Anonymize field values, preserve field names
- Large values: Truncate and store size metadata

---

## Redis to AWS Service Mapping Patterns [PLANNED]

### **Pattern Detection for Analysis Agents**

| Redis Pattern | Detection Criteria | Recommended AWS Service | Confidence Factors |
|---------------|-------------------|------------------------|-------------------|
| **Key-Value Store** | >80% GET/SET commands, simple string values | DynamoDB | High if low TTL usage |
| **Session Store** | Keys with TTL, hash data structures, user: prefix | DynamoDB, ElastiCache | High if durability needed |
| **Caching Layer** | High read:write ratio (>10:1), frequent GET | ElastiCache for Redis | High if ephemeral OK |
| **Leaderboards** | ZADD, ZRANGE, ZRANK on sorted sets | DynamoDB with sort keys | High if serverless preferred |
| **Message Queue** | LPUSH, RPOP, BRPOP on lists | Amazon SQS, Amazon MQ | High if guaranteed delivery needed |
| **Document Store** | Hash data structures, HGETALL, HMGET | DocumentDB | High if complex queries needed |
| **Full-Text Search** | String values with text content, pattern matching | OpenSearch | High if search features needed |
| **Graph Data** | Complex key relationships, multi-hop lookups | Neptune | Medium if relationship-heavy |
| **Time-Series** | Keys with timestamps, sorted sets by time | Timestream (Phase 1) | High if analytics needed |
| **Pub/Sub** | PUBLISH, SUBSCRIBE commands | Amazon SNS, EventBridge | High if event-driven |

### **Anti-Patterns for Redis Migration**

| Anti-Pattern | Description | Impact | Mitigation |
|--------------|-------------|--------|------------|
| **Large Values** | Values >1MB | Performance degradation | Split into smaller keys or use S3 |
| **Hot Keys** | Single key with very high access rate | Bottleneck | Shard data or use caching strategies |
| **No TTL** | All keys persistent without expiration | Memory exhaustion | Implement TTL or migrate to persistent DB |
| **Complex Transactions** | MULTI/EXEC with many keys | Limited atomicity | Use DynamoDB transactions or Aurora |
| **Lua Scripts** | Heavy use of Lua scripting | Migration complexity | Rewrite logic in application layer |

---

## Contract Design Recommendations

### ✅ **DO:**

1. **Mark database-specific fields as optional** (not in `required` array)
2. **Document field availability** in field descriptions
3. **Provide field availability matrix** in QueryPattern description
4. **Use normalized data types** for cross-database comparison
5. **Include all possible fields** even if not all databases provide them

### ❌ **DON'T:**

1. **Don't create separate contracts per database** (violates unified agent interface)
2. **Don't make database-specific fields required** (breaks other database collectors)
3. **Don't assume specific tools** (e.g., don't require Performance Insights)
4. **Don't invent data** - leave fields null/undefined if unavailable

### **Example: Optional Field Pattern**

```json
{
  "QueryPattern": {
    "required": [
      "query_id",
      "query_text",
      "frequency_per_hour",
      "tables_accessed"
    ],
    "properties": {
      "query_id": { "type": "string" },
      "query_text": { "type": "string" },
      "frequency_per_hour": { "type": "number" },
      "tables_accessed": { "type": "array" },

      // Optional MySQL-specific fields
      "rows_examined_avg": {
        "type": "number",
        "description": "Average rows examined (MySQL performance_schema only)"
      },

      // Optional PostgreSQL-specific fields
      "cache_hit_ratio_pct": {
        "type": "number",
        "description": "Cache hit ratio (PostgreSQL pg_stat_statements only)"
      },

      // Optional SQL Server-specific fields
      "avg_logical_reads": {
        "type": "number",
        "description": "Average logical reads (SQL Server DMVs only)"
      }
    }
  }
}
```

---

## Implementation Guidance for Collector Agents

### **Collector Agent Pattern:**

```python
class MySQLCollectorAgent(Agent):
    def collect_query_patterns(self, connection, options):
        """Collect query patterns with MySQL-specific fields"""
        query_patterns = []

        # Collect from performance_schema
        queries = self._query_performance_schema(connection)

        for query in queries:
            pattern = {
                # REQUIRED fields (all databases)
                "query_id": query['digest'],
                "query_text": query['digest_text'],
                "frequency_per_hour": self._calculate_frequency(query),
                "tables_accessed": self._parse_tables(query['digest_text']),

                # GUARANTEED fields (all databases provide)
                "execution_time_ms_avg": query['avg_timer_wait'] / 1000000000,
                "total_time_ms": query['sum_timer_wait'] / 1000000000,
                "calls_per_second": query['estimated_rps'],

                # OPTIONAL: MySQL-specific fields
                "rows_examined_avg": query['avg_rows_examined'],
                "full_table_scans": query['sum_select_scan'],
                "lock_time_ms": query['sum_lock_time'] / 1000000000,
                "scan_efficiency_pct": self._calculate_scan_efficiency(query)
            }

            query_patterns.append(pattern)

        return query_patterns
```

```python
class PostgreSQLCollectorAgent(Agent):
    def collect_query_patterns(self, connection, options):
        """Collect query patterns with PostgreSQL-specific fields"""
        query_patterns = []

        # Collect from pg_stat_statements
        queries = self._query_pg_stat_statements(connection)

        for query in queries:
            pattern = {
                # REQUIRED fields (all databases)
                "query_id": self._generate_query_hash(query['query']),
                "query_text": query['query'],
                "frequency_per_hour": self._calculate_frequency(query),
                "tables_accessed": self._parse_tables(query['query']),

                # GUARANTEED fields (all databases provide)
                "execution_time_ms_avg": query['mean_exec_time'],
                "total_time_ms": query['total_exec_time'],
                "calls_per_second": query['estimated_rps'],

                # OPTIONAL: PostgreSQL-specific fields
                "cache_hit_ratio_pct": self._calculate_cache_hit_ratio(query),
                "shared_blks_hit": query['shared_blks_hit'],
                "shared_blks_read": query['shared_blks_read'],
                "io_read_time_ms": query['blk_read_time'],
                "io_write_time_ms": query['blk_write_time']

                # MySQL-specific fields are NOT included (undefined)
            }

            query_patterns.append(pattern)

        return query_patterns
```

### **Analysis Agent Pattern:**

```python
class DynamoDBAnalysisAgent(Agent):
    def analyze_query(self, query_pattern: dict):
        """Analyze query suitability for DynamoDB"""

        # Use guaranteed fields
        frequency = query_pattern['frequency_per_hour']
        execution_time = query_pattern.get('execution_time_ms_avg', 0)

        # Use optional fields when available
        full_table_scans = query_pattern.get('full_table_scans', 0)  # MySQL only
        cache_hit_ratio = query_pattern.get('cache_hit_ratio_pct')  # PostgreSQL only

        # Analysis logic handles missing data gracefully
        if full_table_scans and full_table_scans > 100:
            concerns.append("High number of full table scans detected")

        if cache_hit_ratio is not None and cache_hit_ratio < 80:
            concerns.append("Low cache hit ratio suggests large data scans")

        # ... rest of analysis
```

---

## Summary

### **Guaranteed Data (ALL Relational Databases):**

- ✅ Schema (tables, columns, PKs, FKs, indexes)
- ✅ Views, stored procedures, functions, triggers
- ✅ Basic query metrics (frequency, execution time, rows)
- ✅ Table sizes and row counts

### **Guaranteed Data (Redis):**

- ✅ Data structures (strings, hashes, lists, sets, sorted sets)
- ✅ Command patterns (GET, SET, ZADD, etc.)
- ✅ Memory usage and statistics
- ✅ Key patterns and sampling
- ✅ Persistence configuration

### **Optional Data (Database-Specific):**

- 🔶 MySQL: rows examined, lock time, full table scans
- 🔶 PostgreSQL: cache hit ratio, I/O timing, temp blocks
- 🔶 SQL Server: logical/physical reads, CPU time
- 🔶 RDS: CloudWatch metrics, instance metadata
- 🔶 Redis: Command execution times (SLOWLOG), ElastiCache CloudWatch metrics

### **Contract Philosophy:**
>
> **"Include everything, require only the essentials, document availability clearly"**

This approach gives us:

- ✅ Rich analysis when data is available
- ✅ Functional analysis even with minimal data
- ✅ Clear expectations for implementers
- ✅ Easy Oracle/DB2/Redis integration
- ✅ Support for both relational and non-relational sources

---

**End of Database Collection Matrix**
