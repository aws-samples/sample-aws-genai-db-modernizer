# Database collection matrix

What each collection script emits. The scripts (`scripts/collect-*.sql`) are the source of truth; this page was checked against them. All four produce the same top-level sections, so analysis does not depend on the source engine.

## Requirements

| Source | Minimum version | Needs |
|---|---|---|
| MySQL, MariaDB | MySQL 8.0, MariaDB 10.5 (`JSON_ARRAYAGG`) | `SELECT` on `performance_schema` and `information_schema` |
| PostgreSQL | 12 (a psql 10 or later client) | `pg_stat_statements` installed and loaded; `SELECT` on `pg_catalog` and `information_schema` |
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
| `extensions` | no | yes | no | no <!-- claim: tests/unit/tools/database/test_postgres_extensions_end_to_end.py::test_the_script_reads_every_installed_extension_and_its_version --> |
| `enum_types` | no | yes | no | no <!-- claim: tests/unit/tools/database/test_postgres_enum_types.py::test_the_script_reads_each_enum_with_its_labels_in_sort_order --> |

### Input check

Before the parser transforms anything, every ingest path reads and checks the file (#131, `src/tools/database/offline_collection_check.py`). The check refuses only what the parser cannot read, so every file that loaded before still loads. It refuses with one error that names the file, never a value from it:

- text that is not valid JSON, with the line and column in the file;
- a document with neither `tables` nor `queries`;
- a section the parser cannot read: a non-empty `tables`, `columns`, `indexes`, `foreign_keys`, `primary_keys`, `views`, `procedures`, `triggers` or `queries` that is not a list, a non-empty `global_stats` or `os_stats` that is not an object, or a `metadata` string that does not hold JSON;
- entries that are not objects, or lack a key the parser reads. The error lists every such entry with its section and position.

| Section | Required keys |
|---|---|
| `tables` | `table_name` |
| `columns` | `table_name`; `column_name` for a column of a table in `tables` |
| `indexes` | `table_name`, `index_name`; `non_unique` on each index's first row |
| `foreign_keys` | `table_name`, `constraint_name`, `column_name`, `referenced_column_name`; `referenced_table_name` on each constraint's first row |
| `primary_keys` | `table_name`, `column_name` |

Every other field is optional, and extra fields and sections are kept. `io_stats` and `wait_events` are passed on as given. A section that is missing, `null`, or empty in another type (`{}` for a list, `""`), and a `metadata` that is not an object, reads as empty, as before. The run's log and the collect status line (`absent_sections`) name it. `tables` or `queries` is reported the same way when the other is there. `io_stats`, `wait_events` and `os_stats` are never reported, since only SQL Server and Oracle write them. A collection with empty `tables` and `queries` lists is valid. The trailing `{"_sentinel": true}` the Oracle script ends each list with is dropped, and its unescaped control characters are read as spaces, on every path.

`metadata.settings` holds the source settings that change how SQL text is read (#1482): MySQL's and MariaDB's `@@GLOBAL.sql_mode` and PostgreSQL's `standard_conforming_strings`. With `NO_BACKSLASH_ESCAPES` a backslash has no meaning in a MySQL string, with `ANSI_QUOTES` (or `ANSI`) a `"..."` is a name rather than a string, and with `standard_conforming_strings = off` a backslash is an escape in a plain PostgreSQL `'...'` string. MySQL's `procedures` and `triggers` rows also carry their own `sql_mode` (`information_schema` `SQL_MODE`), which the server parses each body under. The collector keeps them on `metadata.source_database.settings` and on each routine and trigger, and the facts read each text with them. SQL Server and Oracle report none, and a collection without them is read with each engine's defaults.

Redaction never relies on these settings to mask less: a statement may have run under another value (a framework that sets its own session `sql_mode`, a role-level `ALTER ROLE ... SET standard_conforming_strings`), so every character that either reading puts inside a string or a comment is masked, and a MySQL `"..."` is always redacted as a string. So `ANSI_QUOTES` helps the facts only for text read before redaction: every stored query text and definition is already redacted, and a quoted name such as `"created_at"` is stored as `"?"`, so a fact read later from the stored text (a retention delete's `"created_at" < now()`) does not see the column. The facts are only as good as the setting read: `@@GLOBAL.sql_mode` is the default sessions start with, not a session's own; `current_setting('standard_conforming_strings')` is the collecting session's value, which includes a database-level override and the collecting role's, but not another role's (`pg_db_role_setting`). For an application whose sessions run another value, the facts of a statement with a backslash in a string can be read the other way; redaction is unaffected.

## Tables

| Field | MySQL | PostgreSQL | SQL Server | Oracle |
|---|---|---|---|---|
| `table_name` | yes | yes | yes | yes |
| `schema_name` | no | yes | yes | yes |
| `row_count` | yes | yes | yes | yes |
| `row_count_basis` | yes | yes | yes | yes |
| `stats_analyzed_at` | no | yes | no | yes |
| `data_size_mb` | yes | yes | yes | yes |
| `index_size_mb` | yes | yes | yes | no |
| `engine`, `table_collation` | yes | no | no | no |
| `table_id` | no | no | yes | no |

Oracle skips tables that have no entry in `ALL_TAB_COLUMNS` (internal metadata tables).

`row_count` comes from the catalogue, never from the table (#1555), and is `null` when the source has no count, never 0 in its place (#1636): MySQL `TABLE_ROWS` (NULL only for views and `INFORMATION_SCHEMA` tables; an InnoDB 0 on a table whose data is more than one 16 KB page is statistics not yet refreshed, so unknown), PostgreSQL `pg_class.reltuples` (unknown: -1 on 14+; before 14, 0 with no pages and no analyse or vacuum in `pg_stat_user_tables`), SQL Server `sys.partitions.rows` summed over the table's partitions, Oracle `ALL_TABLES.NUM_ROWS`. `row_count_basis` is `exact` for MySQL MyISAM and Aria and `estimate` for the rest (Microsoft documents `sys.partitions.rows` as approximate), and `stats_analyzed_at` is when the statistics were refreshed (PostgreSQL's latest analyse or vacuum, Oracle `LAST_ANALYZED`). Oracle's `data_size_mb` sums the table's `TABLE`, `TABLE PARTITION` and `TABLE SUBPARTITION` segments: 0 for a table whose segment was never created (`SEGMENT_CREATED` `NO`), `null` when none is found (an index-organized table keeps its data in an `INDEX` segment; LOB segments are not counted). SQL Server's `data_size_mb` sums the pages of the heap or clustered index (`index_id` 0 or 1, every allocation unit of its partitions) and `index_size_mb` those of the nonclustered indexes (`index_id` above 1), in the offline script and the live collector alike (#1873). The reading rule, also for collections written before #1636, is `src/shared/row_counts.py`.

## Query patterns

Field names follow the MySQL convention; each script translates its engine's counters to them. Rows keep a query shape each: the SQL Server and Oracle scripts group literal variants of the same statement, and every script drops shapes executed fewer than its minimum execution count (10 by default). There is no cap on the list by default; an optional cap keeps the costliest shapes by total time, and both settings are recorded in `metadata.query_capture` (see "Analyzing your own database" in `docs/use-claude-code.md`, #384).

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

PostgreSQL adds `extensions`: every extension installed in the database, from `pg_extension`, as `name` and `version` (#1078). <!-- claim: tests/unit/tools/database/test_postgres_extensions_end_to_end.py::test_the_script_reads_every_installed_extension_and_its_version --> The collector keeps them on `metadata.extensions`, <!-- claim: tests/unit/tools/database/test_postgres_extensions_end_to_end.py::test_the_offline_builder_keeps_each_extension_and_its_version, tests/unit/tools/database/test_postgres_extensions_end_to_end.py::test_a_live_collection_keeps_the_extensions_it_read --> and the relational-move gap check compares them with the extensions Aurora PostgreSQL offers per version line (`src/catalog/aurora_postgresql_extensions.toml`). <!-- claim: tests/unit/tools/database/test_postgres_extensions_end_to_end.py::test_run_collect_carries_the_extensions_to_the_gap_check, tests/unit/tools/analysis/test_relational_gaps.py::test_an_unknown_extension_blocks_the_move_and_is_named --> A collection without the section (one written before #1078) has no extension list, and the extension rules then find nothing. <!-- claim: tests/unit/tools/database/test_postgres_extensions_end_to_end.py::test_a_collection_without_the_section_has_no_extension_list, tests/unit/tools/analysis/test_relational_gaps.py::test_no_extension_list_or_an_empty_one_raises_no_extension_gap -->

PostgreSQL also adds `enum_types`: each enum type outside the system schemas, from `pg_enum`, as `schema_name`, `type_name` and its `labels` in sort order (#1834). <!-- claim: tests/unit/tools/database/test_postgres_enum_types.py::test_the_script_reads_each_enum_with_its_labels_in_sort_order, tests/unit/tools/database/test_postgres_enum_types_live_server.py::test_the_offline_query_lists_each_enum_with_its_labels_in_sort_order --> The collector keeps them on `database_schema.enum_types`, <!-- claim: tests/unit/tools/database/test_postgres_enum_types.py::test_the_offline_builder_keeps_each_enum_and_its_labels_in_order, tests/unit/tools/database/test_postgres_enum_types.py::test_a_live_collection_keeps_the_enum_types_it_read --> and a PostgreSQL to Aurora PostgreSQL step 1 creates each enum its moved columns use before the tables, so those columns keep their type. <!-- claim: tests/unit/tools/data_movement/test_dms_step_pg_enums.py::test_the_ddl_creates_the_type_before_the_tables_and_uses_it --> A collection without the section (one written before #1834) has no enum list: those columns stay `TEXT`, and the step 1 contract and DDL header say so. <!-- claim: tests/unit/tools/database/test_postgres_enum_types.py::test_a_collection_without_the_section_has_no_enum_list, tests/unit/tools/data_movement/test_dms_step_pg_enums.py::test_an_old_collection_keeps_text_and_the_header_says_why -->

Views, procedures, triggers, columns, indexes and keys carry the fields the contract needs (names, definitions, parameters, referenced tables); read the scripts for the exact list per engine.

## Cloud metadata

Instance class, storage, Multi-AZ and CloudWatch metrics are not in the scripts. They come from the optional live path or from the customer, and sit in the collector contract's optional fields (`src/contracts/collector_output.py`).

## Contract design

- **Do:** add fields as optional with a defined default, so a script that cannot supply them still produces a valid collection; document which engines supply a field; keep common fields identical across engines.
- **Don't:** make an engine-specific field required, rename a common field per engine, or add a field the analysis does not use.

A new field in the collector contract is a MINOR bump (ADR-008).
