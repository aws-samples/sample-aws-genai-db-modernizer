-- ==========================================================================
-- Database Modernizer — PostgreSQL Offline Collection Script
-- ==========================================================================
-- Run this against your PostgreSQL database to produce a JSON snapshot that
-- the collector agent can ingest in "offline" mode.  No IAM permissions, no
-- EC2 automation instance, no AWS API calls required.
--
-- Requirements:
--   PostgreSQL 12+  (uses json_agg / json_build_object)
--   pg_stat_statements extension must be installed and loaded
--   User needs SELECT on pg_catalog and information_schema
--
-- Usage:
--   psql -U <user> -h <host> -d <database> -t -A -f collect-postgresql.sql > collection.json
--
-- The -t (tuples only) and -A (unaligned) flags ensure clean JSON output.
-- ==========================================================================

\set db_name :DBNAME

WITH

-- ===================== 1. Metadata =====================
metadata AS (
  SELECT json_build_object(
    'version', version(),
    'database_name', current_database(),
    'database_size_gb', round(pg_database_size(current_database()) / 1024.0 / 1024.0 / 1024.0, 4)
  ) AS data
),

-- ===================== 2. Tables =====================
tables_data AS (
  SELECT coalesce(json_agg(t ORDER BY t.table_name), '[]'::json) AS data
  FROM (
    SELECT
      c.relname AS table_name,
      c.reltuples::bigint AS row_count,
      round(pg_table_size(c.oid) / 1024.0 / 1024.0, 2) AS data_size_mb,
      round(pg_indexes_size(c.oid) / 1024.0 / 1024.0, 2) AS index_size_mb,
      n.nspname AS schema_name
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind = 'r'
      AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
  ) t
),

-- ===================== 3. Columns =====================
columns_data AS (
  SELECT coalesce(json_agg(c ORDER BY c.table_name, c.ordinal_position), '[]'::json) AS data
  FROM (
    SELECT
      c.table_name,
      c.column_name,
      c.ordinal_position,
      c.udt_name AS data_type,
      c.data_type AS column_type,
      c.character_maximum_length AS max_length,
      c.is_nullable,
      c.column_default,
      '' AS column_key,
      CASE WHEN c.column_default LIKE 'nextval%' THEN 'auto_increment' ELSE '' END AS extra
    FROM information_schema.columns c
    JOIN information_schema.tables t
      ON c.table_schema = t.table_schema AND c.table_name = t.table_name
    WHERE c.table_schema NOT IN ('pg_catalog', 'information_schema')
      AND t.table_type = 'BASE TABLE'
  ) c
),

-- ===================== 4. Indexes =====================
indexes_data AS (
  SELECT coalesce(json_agg(i ORDER BY i.table_name, i.index_name, i.seq_in_index), '[]'::json) AS data
  FROM (
    SELECT
      t.relname AS table_name,
      i.relname AS index_name,
      a.attname AS column_name,
      array_position(ix.indkey, a.attnum) AS seq_in_index,
      CASE WHEN ix.indisunique THEN 0 ELSE 1 END AS non_unique,
      am.amname AS index_type
    FROM pg_index ix
    JOIN pg_class t ON t.oid = ix.indrelid
    JOIN pg_class i ON i.oid = ix.indexrelid
    JOIN pg_namespace n ON n.oid = t.relnamespace
    JOIN pg_am am ON am.oid = i.relam
    JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(ix.indkey)
    WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
  ) i
),

-- ===================== 5. Foreign Keys =====================
foreign_keys_data AS (
  SELECT coalesce(json_agg(fk ORDER BY fk.table_name, fk.constraint_name, fk.ordinal_position), '[]'::json) AS data
  FROM (
    SELECT
      kcu.table_name,
      kcu.constraint_name,
      kcu.column_name,
      kcu.ordinal_position,
      ccu.table_name AS referenced_table_name,
      ccu.column_name AS referenced_column_name,
      rc.delete_rule AS on_delete,
      rc.update_rule AS on_update
    FROM information_schema.key_column_usage kcu
    JOIN information_schema.referential_constraints rc
      ON kcu.constraint_name = rc.constraint_name
     AND kcu.constraint_schema = rc.constraint_schema
    JOIN information_schema.constraint_column_usage ccu
      ON rc.unique_constraint_name = ccu.constraint_name
     AND rc.unique_constraint_schema = ccu.constraint_schema
    WHERE kcu.table_schema NOT IN ('pg_catalog', 'information_schema')
  ) fk
),

-- ===================== 6. Primary Keys =====================
primary_keys_data AS (
  SELECT coalesce(json_agg(pk ORDER BY pk.table_name, pk.ordinal_position), '[]'::json) AS data
  FROM (
    SELECT
      kcu.table_name,
      kcu.column_name,
      kcu.ordinal_position
    FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage kcu
      ON tc.constraint_name = kcu.constraint_name
     AND tc.table_schema = kcu.table_schema
    WHERE tc.constraint_type = 'PRIMARY KEY'
      AND tc.table_schema NOT IN ('pg_catalog', 'information_schema')
  ) pk
),

-- ===================== 7. Views =====================
views_data AS (
  SELECT coalesce(json_agg(v ORDER BY v.view_name), '[]'::json) AS data
  FROM (
    SELECT
      table_name AS view_name,
      view_definition AS definition,
      is_updatable
    FROM information_schema.views
    WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
  ) v
),

-- ===================== 8. Procedures & Functions =====================
procedures_data AS (
  SELECT coalesce(json_agg(p ORDER BY p.routine_type, p.routine_name), '[]'::json) AS data
  FROM (
    SELECT
      routine_name,
      routine_type,
      data_type AS return_type,
      external_language AS language
    FROM information_schema.routines
    WHERE routine_schema NOT IN ('pg_catalog', 'information_schema')
      AND routine_name NOT LIKE 'pg_%'
  ) p
),

-- ===================== 9. Triggers =====================
triggers_data AS (
  SELECT coalesce(json_agg(tr ORDER BY tr.table_name, tr.trigger_name), '[]'::json) AS data
  FROM (
    SELECT
      trigger_name,
      event_manipulation AS event_type,
      event_object_table AS table_name,
      action_timing AS timing,
      action_statement AS definition
    FROM information_schema.triggers
    WHERE trigger_schema NOT IN ('pg_catalog', 'information_schema')
  ) tr
),

-- ===================== 10. Query Patterns (pg_stat_statements) =====================
queries_data AS (
  SELECT coalesce(json_agg(q ORDER BY q.total_time_ms DESC), '[]'::json) AS data
  FROM (
    SELECT
      queryid::text AS digest,
      query AS query_text,
      current_database() AS schema_name,
      calls AS execution_count,
      round(total_exec_time::numeric, 3) AS total_time_ms,
      round((total_exec_time / NULLIF(calls, 0))::numeric, 3) AS avg_time_ms,
      round(min_exec_time::numeric, 3) AS min_time_ms,
      round(max_exec_time::numeric, 3) AS max_time_ms,
      rows AS total_rows_sent,
      0 AS total_rows_examined,
      0 AS total_rows_affected,
      0 AS full_table_scans,
      0 AS range_scans,
      0 AS no_index_used,
      0 AS no_good_index_used,
      0 AS lock_time_ms,
      0 AS sum_errors,
      0 AS sum_warnings,
      shared_blks_hit,
      shared_blks_read,
      round(blk_read_time::numeric, 3) AS blk_read_time_ms,
      round(blk_write_time::numeric, 3) AS blk_write_time_ms,
      temp_blks_read,
      temp_blks_written
    FROM pg_stat_statements
    WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
      AND calls >= 10
    ORDER BY total_exec_time DESC
    LIMIT 5000
  ) q
),

-- ===================== 11. Global Stats =====================
global_stats_data AS (
  SELECT json_build_object(
    'uptime', extract(epoch FROM (now() - pg_postmaster_start_time()))::bigint::text,
    'questions', (SELECT sum(calls) FROM pg_stat_statements WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database()))::text,
    'threads_connected', (SELECT count(*) FROM pg_stat_activity WHERE datname = current_database())::text,
    'threads_running', (SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND state = 'active')::text,
    'innodb_buffer_pool_read_requests', (SELECT sum(shared_blks_hit + shared_blks_read) FROM pg_stat_statements WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database()))::text,
    'innodb_buffer_pool_reads', (SELECT sum(shared_blks_read) FROM pg_stat_statements WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database()))::text,
    'created_tmp_disk_tables', (SELECT sum(temp_blks_read) FROM pg_stat_statements WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database()))::text,
    'created_tmp_tables', (SELECT sum(temp_blks_written) FROM pg_stat_statements WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database()))::text
  ) AS data
)

-- ===================== 12. Assemble Final JSON =====================
SELECT json_build_object(
  'collection_version', '1.0',
  'collected_at', to_char(now(), 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
  'metadata', (SELECT data FROM metadata),
  'tables', (SELECT data FROM tables_data),
  'columns', (SELECT data FROM columns_data),
  'indexes', (SELECT data FROM indexes_data),
  'foreign_keys', (SELECT data FROM foreign_keys_data),
  'primary_keys', (SELECT data FROM primary_keys_data),
  'views', (SELECT data FROM views_data),
  'procedures', (SELECT data FROM procedures_data),
  'triggers', (SELECT data FROM triggers_data),
  'queries', (SELECT data FROM queries_data),
  'global_stats', (SELECT data FROM global_stats_data)
);
