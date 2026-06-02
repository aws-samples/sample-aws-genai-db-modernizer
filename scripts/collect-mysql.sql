-- ==========================================================================
-- Database Modernizer — MySQL Offline Collection Script
-- ==========================================================================
-- Run this against your MySQL database to produce a JSON snapshot that the
-- collector agent can ingest in "offline" mode.  No IAM permissions, no
-- EC2 automation instance, no AWS API calls required.
--
-- Requirements:
--   MySQL 8.0+  (uses JSON_ARRAYAGG / JSON_OBJECT / performance_schema)
--   User needs SELECT on performance_schema and information_schema
--
-- Usage:
--   mysql -N -u <user> -p -h <host> -D <database> < collect-mysql.sql > collection.json
--
-- The -N flag suppresses column headers so the output is valid JSON.
-- If you forgot -N, the parser will strip the header line automatically.
-- ==========================================================================

SET @db = DATABASE();
SET SESSION group_concat_max_len = 1048576;

-- We build each section as a JSON fragment, then combine at the end.

-- ===================== 1. Metadata =====================
SELECT JSON_OBJECT(
  'version', VERSION(),
  'database_name', @db,
  'database_size_gb', (
    SELECT ROUND(SUM(data_length + index_length) / 1024 / 1024 / 1024, 4)
    FROM information_schema.tables WHERE table_schema = @db
  )
) INTO @metadata;

-- ===================== 2. Tables =====================
SELECT JSON_ARRAYAGG(t_obj) INTO @tables FROM (
  SELECT JSON_OBJECT(
    'table_name', table_name,
    'row_count', IFNULL(table_rows, 0),
    'data_size_mb', ROUND(data_length / 1024 / 1024, 2),
    'index_size_mb', ROUND(index_length / 1024 / 1024, 2),
    'engine', engine,
    'table_collation', table_collation
  ) AS t_obj
  FROM information_schema.tables
  WHERE table_schema = @db AND table_type = 'BASE TABLE'
  ORDER BY table_name
) AS t;

-- ===================== 3. Columns =====================
SELECT JSON_ARRAYAGG(c_obj) INTO @columns FROM (
  SELECT JSON_OBJECT(
    'table_name', table_name,
    'column_name', column_name,
    'ordinal_position', ordinal_position,
    'data_type', data_type,
    'column_type', column_type,
    'max_length', character_maximum_length,
    'is_nullable', is_nullable,
    'column_default', column_default,
    'column_key', column_key,
    'extra', extra
  ) AS c_obj
  FROM information_schema.columns
  WHERE table_schema = @db
  ORDER BY table_name, ordinal_position
) AS c;

-- ===================== 4. Indexes =====================
SELECT JSON_ARRAYAGG(i_obj) INTO @indexes FROM (
  SELECT JSON_OBJECT(
    'table_name', table_name,
    'index_name', index_name,
    'column_name', column_name,
    'seq_in_index', seq_in_index,
    'non_unique', non_unique,
    'index_type', index_type
  ) AS i_obj
  FROM information_schema.statistics
  WHERE table_schema = @db
  ORDER BY table_name, index_name, seq_in_index
) AS i;

-- ===================== 5. Foreign Keys =====================
SELECT JSON_ARRAYAGG(fk_obj) INTO @foreign_keys FROM (
  SELECT JSON_OBJECT(
    'table_name', kcu.table_name,
    'constraint_name', kcu.constraint_name,
    'column_name', kcu.column_name,
    'ordinal_position', kcu.ordinal_position,
    'referenced_table_name', kcu.referenced_table_name,
    'referenced_column_name', kcu.referenced_column_name,
    'on_delete', rc.delete_rule,
    'on_update', rc.update_rule
  ) AS fk_obj
  FROM information_schema.key_column_usage kcu
  JOIN information_schema.referential_constraints rc
    ON kcu.constraint_name = rc.constraint_name
   AND kcu.constraint_schema = rc.constraint_schema
  WHERE kcu.table_schema = @db
    AND kcu.referenced_table_name IS NOT NULL
  ORDER BY kcu.table_name, kcu.constraint_name, kcu.ordinal_position
) AS fk;

-- ===================== 6. Primary Keys =====================
SELECT JSON_ARRAYAGG(pk_obj) INTO @primary_keys FROM (
  SELECT JSON_OBJECT(
    'table_name', table_name,
    'column_name', column_name,
    'ordinal_position', ordinal_position
  ) AS pk_obj
  FROM information_schema.key_column_usage
  WHERE table_schema = @db AND constraint_name = 'PRIMARY'
  ORDER BY table_name, ordinal_position
) AS pk;

-- ===================== 7. Views =====================
SELECT JSON_ARRAYAGG(v_obj) INTO @views FROM (
  SELECT JSON_OBJECT(
    'view_name', table_name,
    'definition', view_definition,
    'is_updatable', is_updatable
  ) AS v_obj
  FROM information_schema.views
  WHERE table_schema = @db
  ORDER BY table_name
) AS v;

-- ===================== 8. Procedures & Functions =====================
SELECT JSON_ARRAYAGG(p_obj) INTO @procedures FROM (
  SELECT JSON_OBJECT(
    'routine_name', routine_name,
    'routine_type', routine_type,
    'return_type', data_type,
    'definition', routine_definition
  ) AS p_obj
  FROM information_schema.routines
  WHERE routine_schema = @db
  ORDER BY routine_type, routine_name
) AS p;

-- ===================== 9. Triggers =====================
SELECT JSON_ARRAYAGG(tr_obj) INTO @triggers FROM (
  SELECT JSON_OBJECT(
    'trigger_name', trigger_name,
    'event_type', event_manipulation,
    'table_name', event_object_table,
    'timing', action_timing,
    'definition', action_statement
  ) AS tr_obj
  FROM information_schema.triggers
  WHERE trigger_schema = @db
  ORDER BY event_object_table, trigger_name
) AS tr;

-- ===================== 10. Query Patterns (performance_schema) =====================
SELECT JSON_ARRAYAGG(q_obj) INTO @queries FROM (
  SELECT JSON_OBJECT(
    'digest', DIGEST,
    'query_text', DIGEST_TEXT,
    'schema_name', SCHEMA_NAME,
    'execution_count', COUNT_STAR,
    'total_time_ms', ROUND(SUM_TIMER_WAIT / 1000000000, 3),
    'avg_time_ms', ROUND(AVG_TIMER_WAIT / 1000000000, 3),
    'min_time_ms', ROUND(MIN_TIMER_WAIT / 1000000000, 3),
    'max_time_ms', ROUND(MAX_TIMER_WAIT / 1000000000, 3),
    'total_rows_sent', SUM_ROWS_SENT,
    'total_rows_examined', SUM_ROWS_EXAMINED,
    'total_rows_affected', SUM_ROWS_AFFECTED,
    'full_table_scans', SUM_SELECT_SCAN,
    'range_scans', SUM_SELECT_RANGE,
    'no_index_used', SUM_NO_INDEX_USED,
    'no_good_index_used', SUM_NO_GOOD_INDEX_USED,
    'lock_time_ms', ROUND(SUM_LOCK_TIME / 1000000000, 3),
    'sum_errors', SUM_ERRORS,
    'sum_warnings', SUM_WARNINGS,
    'first_seen', DATE_FORMAT(FIRST_SEEN, '%Y-%m-%d %H:%i:%s'),
    'last_seen', DATE_FORMAT(LAST_SEEN, '%Y-%m-%d %H:%i:%s')
  ) AS q_obj
  FROM performance_schema.events_statements_summary_by_digest
  WHERE SCHEMA_NAME = @db
    AND COUNT_STAR >= 10
  ORDER BY SUM_TIMER_WAIT DESC
  LIMIT 5000
) AS q;

-- ===================== 11. Global Stats =====================
SELECT JSON_OBJECTAGG(name, val) INTO @global_stats FROM (
  SELECT LOWER(VARIABLE_NAME) AS name, VARIABLE_VALUE AS val
  FROM performance_schema.global_status
  WHERE VARIABLE_NAME IN (
    'Innodb_buffer_pool_read_requests',
    'Innodb_buffer_pool_reads',
    'Created_tmp_disk_tables',
    'Created_tmp_tables',
    'Threads_connected',
    'Threads_running',
    'Questions',
    'Uptime'
  )
) AS gs;

-- ===================== 12. Assemble Final JSON =====================
SELECT JSON_OBJECT(
  'collection_version', '1.0',
  'collected_at', DATE_FORMAT(NOW(), '%Y-%m-%dT%H:%i:%sZ'),
  'metadata', JSON_EXTRACT(@metadata, '$'),
  'tables', IFNULL(JSON_EXTRACT(@tables, '$'), JSON_ARRAY()),
  'columns', IFNULL(JSON_EXTRACT(@columns, '$'), JSON_ARRAY()),
  'indexes', IFNULL(JSON_EXTRACT(@indexes, '$'), JSON_ARRAY()),
  'foreign_keys', IFNULL(JSON_EXTRACT(@foreign_keys, '$'), JSON_ARRAY()),
  'primary_keys', IFNULL(JSON_EXTRACT(@primary_keys, '$'), JSON_ARRAY()),
  'views', IFNULL(JSON_EXTRACT(@views, '$'), JSON_ARRAY()),
  'procedures', IFNULL(JSON_EXTRACT(@procedures, '$'), JSON_ARRAY()),
  'triggers', IFNULL(JSON_EXTRACT(@triggers, '$'), JSON_ARRAY()),
  'queries', IFNULL(JSON_EXTRACT(@queries, '$'), JSON_ARRAY()),
  'global_stats', IFNULL(JSON_EXTRACT(@global_stats, '$'), JSON_OBJECT())
) AS collection_output;
