# Database collection matrix

What each collection script emits. The scripts (`scripts/collect-*.sql`) are the source of truth; this page was checked against them. All four produce the same top-level sections, so analysis does not depend on the source engine.

## Requirements

| Source | Minimum version | Needs |
|---|---|---|
| MySQL, MariaDB | MySQL 8.0 | `SELECT` on `performance_schema` and `information_schema` |
| PostgreSQL | 12 | `pg_stat_statements` installed and loaded; `SELECT` on `pg_catalog` and `information_schema` |
| SQL Server | 2016 | `SELECT` on the `sys` catalog views and DMVs; `VIEW SERVER STATE` for the query section (without it the query section is empty and the rest still works) |
| Oracle | 12.2 | `SELECT` on `V$SQL`, `V$SESSION`, `V$SYSSTAT`, `V$SYSTEM_EVENT`, `V$OSSTAT`, the `ALL_*` views and `DBA_SEGMENTS` (`SELECT_CATALOG_ROLE` for full visibility) |

## Sections

| Section | MySQL | PostgreSQL | SQL Server | Oracle |
|---|---|---|---|---|
| `metadata` | yes | yes | yes | yes |
| `tables` | yes | yes | yes | yes |
| `columns` | yes | yes | yes | yes |
| `indexes` | yes | yes | yes | yes |
| `foreign_keys` | yes | yes | yes | yes |
| `primary_keys` | yes | yes | yes | yes |
| `views` | yes | yes | yes | yes |
| `procedures` | yes | yes | yes | yes |
| `triggers` | yes | yes | yes | yes |
| `queries` | yes | yes | yes | yes |
| `global_stats` | yes | yes | yes | yes |
| `io_stats`, `wait_events`, `os_stats` | no | no | yes | yes |

## Tables

| Field | MySQL | PostgreSQL | SQL Server | Oracle |
|---|---|---|---|---|
| `table_name` | yes | yes | yes | yes |
| `schema_name` | no | yes | yes | yes |
| `row_count` | yes | yes | yes | yes |
| `data_size_mb` | yes | yes | yes | yes |
| `index_size_mb` | yes | yes | yes | no |
| `engine`, `table_collation` | yes | no | no | no |
| `table_id` | no | no | yes | no |

Oracle skips tables that have no entry in `ALL_TAB_COLUMNS` (internal metadata tables).

## Query patterns

Field names follow the MySQL convention; each script translates its engine's counters to them. Rows keep a query shape each: the SQL Server and Oracle scripts group literal variants of the same statement, and every script drops shapes executed fewer than ten times. The cap on the list is 1000 shapes for SQL Server and Oracle and 5000 for MySQL and PostgreSQL.

| Field | MySQL | PostgreSQL | SQL Server | Oracle |
|---|---|---|---|---|
| `digest`, `query_text` | yes | yes | yes | yes |
| `execution_count` | yes | yes | yes | yes |
| `total_time_ms`, `avg_time_ms`, `min_time_ms`, `max_time_ms` | yes | yes | yes | yes (min and max are 0: `V$SQL` has no per-execution extremes) |
| `total_rows_sent` | yes | yes | yes | yes |
| `total_rows_examined` | yes | 0 | logical reads | yes |
| `total_rows_affected` | yes | 0 | yes | yes |
| `first_seen`, `last_seen` | yes | no | yes | yes |
| `schema_name` | yes | yes | no (`database_name`) | no |
| `full_table_scans`, `range_scans`, `no_index_used`, `no_good_index_used` | yes | 0 | no | no |
| `lock_time_ms`, `sum_errors`, `sum_warnings` | yes | 0 | no (`lock_time_ms` only on Oracle) | `lock_time_ms` |
| CPU and read counters | no | `shared_blks_hit`, `shared_blks_read`, `blk_read_time_ms`, `blk_write_time_ms`, `temp_blks_read`, `temp_blks_written` | `total_cpu_ms`, `avg_cpu_time_ms`, `avg_logical_reads`, `avg_physical_reads` | `total_cpu_time_ms`, `total_logical_reads`, `total_physical_reads`, `db_load_contribution_percent` |

"0" means the script writes a zero placeholder because the engine does not track the counter. A source column named in the MySQL convention is not evidence the engine measures it.

Sources: MySQL `performance_schema.events_statements_summary_by_digest`; PostgreSQL `pg_stat_statements`; SQL Server `sys.dm_exec_query_stats` with `sys.dm_exec_sql_text`; Oracle `V$SQL`, grouped by `FORCE_MATCHING_SIGNATURE`.

## Global statistics

`global_stats` is keyed by MySQL counter names; each script maps its engine's counters onto them:

| Key | MySQL | PostgreSQL | SQL Server | Oracle |
|---|---|---|---|---|
| `innodb_buffer_pool_read_requests`, `innodb_buffer_pool_reads` | yes | from `pg_stat_statements` block counters | yes | yes |
| `created_tmp_tables`, `created_tmp_disk_tables` | yes | from temp block counters | yes | yes |
| `threads_connected`, `questions` | yes | yes | yes | no (`active_connections`, `total_transactions`) |
| `threads_running`, `uptime` | yes | yes | no | no |

## Other sections

SQL Server and Oracle add `io_stats` (physical reads and writes per second, bytes per second, read and write latency), `wait_events` (event, total waits, time waited, average wait, wait class) and `os_stats` (memory and uptime; Oracle also CPU count and SGA and PGA sizes).

Views, procedures, triggers, columns, indexes and keys carry the fields the contract needs (names, definitions, parameters, referenced tables); read the scripts for the exact list per engine.

## Cloud metadata

Instance class, storage, Multi-AZ and CloudWatch metrics are not in the scripts. They come from the optional live path or from the customer, and sit in the collector contract's optional fields (`src/contracts/collector_output.py`).

## Contract design

- **Do:** add fields as optional with a defined default, so a script that cannot supply them still produces a valid collection; document which engines supply a field; keep common fields identical across engines.
- **Don't:** make an engine-specific field required, rename a common field per engine, or add a field the analysis does not use.

A new field in the collector contract is a MINOR bump (ADR-008).
