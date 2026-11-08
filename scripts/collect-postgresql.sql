-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0

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
--
-- Optional settings (#384), as psql variables:
--   -v query_limit=20000    keep only the 20,000 costliest statements by total time
--                           (default: no cap, every statement is kept)
--   -v min_executions=5     keep statements that ran at least 5 times (default: 10)
--   -v lob_scan_max_mb=0    measure no LOB column (default: 1024). For each table of at
--                           most this many MB (pg_total_relation_size) that the user may
--                           SELECT, the script records each LOB column's longest value in
--                           bytes (max_value_bytes), which sizes step 1's DMS LobMaxSize
--                           (#1918); on a server without XML support it is not measured. It reads each such table once per LOB column; text and
--                           bytea lengths come from the stored header, without detoasting.
-- Both are recorded in the collection's metadata.query_capture. The defaults use
-- \if and :{?var}, which need psql 10 or later (the client; the server is 12+). A
-- value that is not a number stops the script at its CAST.
--
-- Call rates over the load period (#1908), as a psql variable:
--   -v rate_sample_seconds=300   read the statement counters, wait this many seconds
--                                (pg_sleep), read them again (default: 300)
--   -v rate_sample_seconds=0     one read, no wait
-- Each statement's rate is the change in its count between the two reads, over the
-- time between them by the server's clock, so run the script while the application
-- carries its usual load. The value and the measured time are recorded in
-- metadata.rate_sample. A value that is not a whole number of seconds, 0 or more,
-- stops the script. Both reads are SELECTs: nothing is changed or reset. A
-- statement_timeout shorter than the wait cancels pg_sleep: the measured time is then
-- too short, and the rates come from one read. A first read that fails (no
-- pg_stat_statements) also gives one read.
--
-- Do not reset the statement statistics before collecting (#1991), for example
-- with SELECT pg_stat_statements_reset(), and do not collect right after a
-- restart: the counters then cover only the minutes since, often a load test,
-- and every rate is that short window's load. The assessment states the window
-- and labels its costs and risks a scenario when the window is short, starts at
-- a reset, or runs well above the server's own rate.
--
-- Procedure, function and trigger code (#1652), as a psql variable:
--   -v include_routine_bodies=0  (or false) list functions, procedures and triggers by name and
--                                size only, without their code (default: 1, kept)
-- Each routine's whole CREATE statement (pg_get_functiondef, pg_get_triggerdef) needs
-- no privilege beyond reading the catalog, and the routines and triggers are read from
-- pg_proc and pg_trigger, which show a read-only user every one. Function call counts
-- come from pg_stat_user_functions, which PostgreSQL fills only when track_functions is
-- pl or all; the script never changes the setting and records which it was.
-- ==========================================================================

\set db_name :DBNAME

-- The settings: a variable psql was not given gets its default.
\if :{?min_executions}
\else
  \set min_executions 10
\endif
\if :{?lob_scan_max_mb}
\else
  \set lob_scan_max_mb 1024
\endif
-- The LOB measurement below reads each column's longest value through query_to_xml, which
-- needs a server built with XML support. Probe it without failing the script (ON_ERROR_STOP):
-- on a server without it, max_value_bytes stays null ("not measured") instead (#1918).
DO $probe$
BEGIN
  PERFORM query_to_xml('SELECT 1', false, true, '');
  PERFORM set_config('modernizer.lob_xml_ok', 'on', false);
EXCEPTION WHEN OTHERS THEN
  PERFORM set_config('modernizer.lob_xml_ok', 'off', false);
END
$probe$;
SELECT coalesce(current_setting('modernizer.lob_xml_ok', true), 'off') = 'on' AS lob_xml_ok \gset
\if :{?include_routine_bodies}
\else
  \set include_routine_bodies 1
\endif
-- 0/1, false/true, no/yes or off/on, as INCLUDE_ROUTINE_BODIES reads them; anything else
-- keeps the bodies.
SELECT lower(:'include_routine_bodies') NOT IN ('0', 'false', 'f', 'no', 'off') AS routine_bodies_on \gset
\if :{?query_limit}
  \set query_limit_clause 'LIMIT CAST(' :'query_limit' ' AS bigint)'
  \set query_limit_value 'CAST(' :'query_limit' ' AS bigint)'
\else
  \set query_limit_clause ''
  \set query_limit_value 'NULL::bigint'
\endif

-- #1893: the window pg_stat_statements' counters cover is the time since it was last
-- reset (pg_stat_statements_info.stats_reset, PostgreSQL 14+). Not the uptime: with
-- pg_stat_statements.save on (the default) the counters survive a clean restart. Before
-- 14 the view does not exist, the figure is NULL and the rates assume 24 hours.
SELECT to_regclass('pg_stat_statements_info') IS NOT NULL AS has_pgss_info \gset
\if :has_pgss_info
  \set pgss_reset_age '(SELECT round(extract(epoch FROM now() - stats_reset)::numeric, 3) FROM pg_stat_statements_info)'
\else
  \set pgss_reset_age 'NULL::numeric'
\endif

-- #1908: the first read of the statement counters, then the wait. Each entry is its
-- user, query id, top-level flag (PostgreSQL 14+) and stats_since (PostgreSQL 17+, so an
-- entry reset in between is a new one), with its calls; clock_timestamp() is the
-- server's clock when it was read (now() would be the transaction's start).
\if :{?rate_sample_seconds}
\else
  \set rate_sample_seconds 300
\endif
SELECT :'rate_sample_seconds' ~ '^[0-9]+$' AS rate_sample_valid \gset
\if :rate_sample_valid
\else
  SELECT CAST('rate_sample_seconds must be a whole number of seconds, 0 or more' AS integer);
  \quit
\endif
SELECT CAST(:'rate_sample_seconds' AS integer) > 0 AS rate_sample_on \gset
\if :rate_sample_on
  SELECT
    coalesce(json_agg(json_build_object(
      'k', s.userid::text || '/' || s.queryid::text || '/'
           || coalesce(to_jsonb(s) ->> 'toplevel', '') || '/'
           || coalesce(to_jsonb(s) ->> 'stats_since', ''),
      'c', s.calls)), '[]'::json)::text AS rate_sample_start,
    extract(epoch FROM clock_timestamp())::text AS rate_sample_t0
  FROM pg_stat_statements s
  WHERE s.dbid = (SELECT oid FROM pg_database WHERE datname = current_database()) \gset
  -- A first read that failed sets no variable: the collection then takes one read.
  \if :{?rate_sample_t0}
    SELECT pg_sleep(CAST(:'rate_sample_seconds' AS integer)) AS rate_sample_waited \gset
    \set rate_sample_elapsed 'round((extract(epoch FROM clock_timestamp()) - CAST(' :'rate_sample_t0' ' AS double precision))::numeric, 3)'
  \else
    \set rate_sample_start '[]'
    \set rate_sample_elapsed 'NULL::numeric'
  \endif
\else
  \set rate_sample_start '[]'
  \set rate_sample_elapsed 'NULL::numeric'
\endif

WITH

-- #1908: the first read's count of each entry, and the time since it (the second read
-- is this query's).
rate_sample_start AS (
  SELECT k, c FROM json_to_recordset(CAST(:'rate_sample_start' AS json)) AS x(k text, c bigint)
),
rate_sample_clock AS (
  SELECT :rate_sample_elapsed AS elapsed_seconds
),

-- ===================== 1. Metadata =====================
-- settings.standard_conforming_strings: 'off' makes a backslash an escape in a plain
-- '...' string, and the collected statements are read that way (#1482).
metadata AS (
  SELECT json_build_object(
    'version', version(),
    'database_name', current_database(),
    'database_size_gb', round(pg_database_size(current_database()) / 1024.0 / 1024.0 / 1024.0, 4),
    'settings', json_build_object(
      'standard_conforming_strings', current_setting('standard_conforming_strings')
    ),
    -- The capture settings (#384): no cap (null) unless query_limit is set.
    'query_capture', json_build_object(
      'query_limit', :query_limit_value,
      'min_executions', CAST(:'min_executions' AS bigint)
    ),
    -- #1908: the wait asked for and the seconds the server's clock measured between
    -- the two reads of the counters (null with no wait).
    'rate_sample', json_build_object(
      'seconds', CAST(:'rate_sample_seconds' AS integer),
      'elapsed_seconds', (SELECT elapsed_seconds FROM rate_sample_clock)
    ),
    -- How routine bodies were read (#1652): include_routine_bodies = 0 keeps none.
    'routine_capture', json_build_object(
      'include_bodies', CAST(:'routine_bodies_on' AS boolean),
      'privileges_not_seen', '[]'::json
    )
  ) AS data
),

-- ===================== 2. Tables =====================
-- row_count is the planner's estimate in pg_class.reltuples, never a count of the
-- table (no table is read). It is null when PostgreSQL has none: reltuples is -1
-- on PostgreSQL 14+ for a table never vacuumed or analysed; before 14 such a table
-- has reltuples 0 and relpages 0 and no analyse or vacuum on record in
-- pg_stat_user_tables. stats_analyzed_at is the latest of those four times.
tables_data AS (
  SELECT coalesce(json_agg(t ORDER BY t.table_name), '[]'::json) AS data
  FROM (
    SELECT
      c.relname AS table_name,
      CASE
        WHEN c.reltuples < 0 THEN NULL
        WHEN c.reltuples = 0 AND c.relpages = 0
             AND current_setting('server_version_num')::int < 140000
             AND s.last_analyze IS NULL AND s.last_autoanalyze IS NULL
             AND s.last_vacuum IS NULL AND s.last_autovacuum IS NULL THEN NULL
        ELSE c.reltuples::bigint
      END AS row_count,
      'estimate' AS row_count_basis,
      greatest(s.last_analyze, s.last_autoanalyze, s.last_vacuum, s.last_autovacuum)
        AS stats_analyzed_at,
      round(pg_table_size(c.oid) / 1024.0 / 1024.0, 2) AS data_size_mb,
      round(pg_indexes_size(c.oid) / 1024.0 / 1024.0, 2) AS index_size_mb,
      n.nspname AS schema_name
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
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
      -- The longest value of a LOB column, in bytes (#1918): only on a table of at most
      -- lob_scan_max_mb MB that the user may SELECT; null when not measured, 0 when empty.
      CASE WHEN CAST(:'lob_scan_max_mb' AS bigint) > 0 AND CAST(:'lob_xml_ok' AS boolean)
            AND (c.udt_name IN ('text', 'citext', 'bytea', 'json', 'jsonb', 'xml', 'hstore')
                 OR c.data_type = 'ARRAY')
            AND has_table_privilege(format('%I.%I', c.table_schema, c.table_name), 'SELECT')
            AND pg_total_relation_size(format('%I.%I', c.table_schema, c.table_name)::regclass)
                <= CAST(:'lob_scan_max_mb' AS bigint) * 1048576
        THEN coalesce((xpath('/row/m/text()', query_to_xml(format(
               'SELECT max(octet_length(%I%s)) AS m FROM %I.%I', c.column_name,
               CASE WHEN c.udt_name IN ('text', 'citext', 'bytea') THEN '' ELSE '::text' END,
               c.table_schema, c.table_name), false, true, '')))[1]::text::bigint, 0)
      END AS max_value_bytes,
      c.numeric_precision AS numeric_precision,
      c.numeric_scale AS numeric_scale,
      c.datetime_precision AS datetime_precision,
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
      am.amname AS index_type,
      pg_get_expr(ix.indpred, ix.indrelid) AS predicate,
      (ix.indexprs IS NOT NULL) AS has_expressions,
      pg_get_expr(ix.indexprs, ix.indrelid) AS index_expressions,
      pg_get_indexdef(ix.indexrelid) AS index_def
    FROM pg_index ix
    JOIN pg_class t ON t.oid = ix.indrelid
    JOIN pg_class i ON i.oid = ix.indexrelid
    JOIN pg_namespace n ON n.oid = t.relnamespace
    JOIN pg_am am ON am.oid = i.relam
    -- LEFT, not INNER: an expression key has attnum 0 and matches no
    -- pg_attribute row, and an inner join would drop the whole index.
    LEFT JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(ix.indkey)
    WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
  ) i
),

-- ===================== 5. Foreign Keys =====================
-- Read from pg_catalog, not information_schema: a role with only SELECT sees no
-- constraint it does not own there, a foreign key onto a unique index has no
-- referenced constraint to join, and its views join on the constraint name alone
-- (unique only per table), so duplicate names and partitions mixed up the column
-- pairs (#1868). unnest() WITH ORDINALITY gives the position of each column pair in
-- the constraint, so every row is one aligned pair. conparentid = 0 keeps the
-- constraint a partitioned table declares and drops its per-partition copies (and the
-- internal constraints on the partitions of a referenced table); conparentid needs
-- PostgreSQL 11 or later.
foreign_keys_data AS (
  SELECT coalesce(json_agg(fk ORDER BY fk.table_name, fk.constraint_name, fk.ordinal_position), '[]'::json) AS data
  FROM (
    SELECT
      cl.relname AS table_name,
      c.conname AS constraint_name,
      a.attname AS column_name,
      k.ordinal_position,
      rcl.relname AS referenced_table_name,
      ra.attname AS referenced_column_name,
      CASE c.confdeltype
        WHEN 'a' THEN 'NO ACTION' WHEN 'r' THEN 'RESTRICT' WHEN 'c' THEN 'CASCADE'
        WHEN 'n' THEN 'SET NULL' WHEN 'd' THEN 'SET DEFAULT' END AS on_delete,
      CASE c.confupdtype
        WHEN 'a' THEN 'NO ACTION' WHEN 'r' THEN 'RESTRICT' WHEN 'c' THEN 'CASCADE'
        WHEN 'n' THEN 'SET NULL' WHEN 'd' THEN 'SET DEFAULT' END AS on_update
    FROM pg_catalog.pg_constraint c
    JOIN pg_catalog.pg_class cl ON cl.oid = c.conrelid
    JOIN pg_catalog.pg_namespace n ON n.oid = cl.relnamespace
    JOIN pg_catalog.pg_class rcl ON rcl.oid = c.confrelid
    CROSS JOIN LATERAL unnest(c.conkey, c.confkey) WITH ORDINALITY AS k(attnum, rattnum, ordinal_position)
    JOIN pg_catalog.pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
    JOIN pg_catalog.pg_attribute ra ON ra.attrelid = c.confrelid AND ra.attnum = k.rattnum
    WHERE c.contype = 'f'
      AND c.conparentid = 0
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
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
-- #1652: read from pg_proc, not information_schema.routines, which hides a routine whose
-- EXECUTE the collection user lacks. definition is the whole CREATE (pg_get_functiondef)
-- in every language. Aggregates and window functions are not listed (pg_get_functiondef
-- cannot print one), nor a routine an extension owns (the extension creates it; the
-- extensions section names it). Catalog reads, no table data.
procedures_data AS (
  SELECT coalesce(json_agg(p ORDER BY p.routine_type, p.routine_name, p.specific_name), '[]'::json) AS data
  FROM (
    SELECT
      pr.proname AS routine_name,
      n.nspname AS schema_name,
      pr.proname || '_' || pr.oid AS specific_name,
      pg_get_function_identity_arguments(pr.oid) AS identity_arguments,
      CASE pr.prokind WHEN 'p' THEN 'PROCEDURE' ELSE 'FUNCTION' END AS routine_type,
      pg_get_function_result(pr.oid) AS return_type,
      upper(l.lanname) AS language,
      pg_get_userbyid(pr.proowner) AS owner,
      pr.prosecdef AS security_definer,
      coalesce((
        SELECT json_agg(json_build_object('kind', dep.kind, 'name', dep.name) ORDER BY dep.kind, dep.name)
        FROM (
          SELECT DISTINCT
            CASE
              WHEN d.refclassid = 'pg_proc'::regclass THEN 'function'
              WHEN d.refclassid = 'pg_type'::regclass THEN 'type'
              WHEN c.relkind IN ('r', 'p', 'f') THEN 'table'
              WHEN c.relkind IN ('v', 'm') THEN 'view'
              WHEN c.relkind = 'S' THEN 'sequence'
            END AS kind,
            CASE
              WHEN d.refclassid = 'pg_proc'::regclass THEN d.refobjid::regprocedure::text
              WHEN d.refclassid = 'pg_type'::regclass THEN d.refobjid::regtype::text
              ELSE d.refobjid::regclass::text
            END AS name
          FROM pg_depend d
          LEFT JOIN pg_class c ON d.refclassid = 'pg_class'::regclass AND c.oid = d.refobjid
          WHERE d.classid = 'pg_proc'::regclass AND d.objid = pr.oid AND d.deptype = 'n'
            AND d.refclassid IN ('pg_proc'::regclass, 'pg_type'::regclass, 'pg_class'::regclass)
        ) dep
        WHERE dep.kind IS NOT NULL
      ), '[]'::json) AS depends_on,
      length(pg_get_functiondef(pr.oid)) AS body_chars,
      CASE WHEN CAST(:'routine_bodies_on' AS boolean) THEN pg_get_functiondef(pr.oid) END AS definition,
      CASE WHEN CAST(:'routine_bodies_on' AS boolean) THEN 'collected' ELSE 'opted_out' END AS body_status
    FROM pg_proc pr
    JOIN pg_namespace n ON n.oid = pr.pronamespace
    JOIN pg_language l ON l.oid = pr.prolang
    WHERE pr.prokind IN ('f', 'p')
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND n.nspname NOT LIKE 'pg_toast%'
      AND n.nspname NOT LIKE 'pg_temp%'
      AND pr.proname NOT LIKE 'pg_%'
      AND NOT EXISTS (
        SELECT 1 FROM pg_depend x
        WHERE x.classid = 'pg_proc'::regclass AND x.objid = pr.oid
          AND x.refclassid = 'pg_extension'::regclass AND x.deptype = 'e')
  ) p
),

-- ===================== 9. Triggers =====================
triggers_data AS (
  SELECT coalesce(json_agg(tr ORDER BY tr.table_name, tr.trigger_name), '[]'::json) AS data
  FROM (
    -- pg_trigger, not information_schema.triggers: that view lists only the triggers
    -- on tables the user owns or holds more than SELECT on, so a read-only user saw
    -- none (#1651). One row per trigger, with every event it fires on. #1652: its
    -- orientation, state and function, and its whole CREATE (pg_get_triggerdef).
    -- tgtype bits: 1 ROW, 2 BEFORE, 4 INSERT, 8 DELETE, 16 UPDATE, 32 TRUNCATE,
    -- 64 INSTEAD OF.
    SELECT
      t.tgname AS trigger_name,
      n.nspname AS schema_name,
      c.relname AS table_name,
      concat_ws(' OR ',
        CASE WHEN t.tgtype & 4 <> 0 THEN 'INSERT' END,
        CASE WHEN t.tgtype & 16 <> 0 THEN 'UPDATE' END,
        CASE WHEN t.tgtype & 8 <> 0 THEN 'DELETE' END,
        CASE WHEN t.tgtype & 32 <> 0 THEN 'TRUNCATE' END) AS event_type,
      CASE t.tgtype & 66 WHEN 2 THEN 'BEFORE' WHEN 64 THEN 'INSTEAD OF' ELSE 'AFTER' END
        AS timing,
      CASE WHEN t.tgtype & 1 <> 0 THEN 'ROW' ELSE 'STATEMENT' END AS for_each,
      t.tgenabled::text AS is_enabled,
      t.tgfoid::regprocedure::text AS function_signature,
      length(pg_get_triggerdef(t.oid)) AS body_chars,
      CASE WHEN CAST(:'routine_bodies_on' AS boolean) THEN pg_get_triggerdef(t.oid) END AS definition,
      CASE WHEN CAST(:'routine_bodies_on' AS boolean) THEN 'collected' ELSE 'opted_out' END AS body_status
    FROM pg_trigger t
    JOIN pg_class c ON c.oid = t.tgrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE NOT t.tgisinternal
      -- a partition's clone of its parent's trigger is the parent's trigger: one row
      -- (tgparentid is PostgreSQL 13+; on 12 the clones are internal)
      AND coalesce(to_jsonb(t) ->> 'tgparentid', '0') = '0'
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  ) tr
),

-- ===================== 9b. Routine stats (#1652) =====================
-- The call counts PostgreSQL keeps per function and procedure: pg_stat_user_functions
-- is filled only when track_functions is pl or all. The collection never changes the
-- setting and records which it was.
routine_stats_data AS (
  SELECT coalesce(json_agg(f ORDER BY f.schema_name, f.routine_name), '[]'::json) AS data
  FROM (
    SELECT funcname AS routine_name, schemaname AS schema_name,
           'FUNCTION' AS object_type, calls AS execution_count,
           round(total_time::numeric, 3) AS total_time_ms
    FROM pg_stat_user_functions
  ) f
),
routine_stats_status_data AS (
  SELECT CASE WHEN current_setting('track_functions') = 'none'
              THEN json_build_object('status', 'not_tracked', 'reason', 'track_functions is none')
              ELSE json_build_object('status', 'tracked')
         END AS data
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
      temp_blks_written,
      -- #1893: PostgreSQL 17's per-entry start (stats_since); NULL before 17, where the
      -- row has no such column, and the rates use the stats reset age instead.
      round(extract(epoch FROM now() - (to_jsonb(s) ->> 'stats_since')::timestamptz)::numeric, 3)
        AS stats_since_age_seconds,
      -- #1908: the entry's calls at the first read; null when it was not there then.
      r.c AS sample_first_count,
      -- #1652: false when the statement ran inside a function or procedure (PostgreSQL
      -- 14+ with pg_stat_statements.track = all); read through to_jsonb, so an older
      -- server, which has no such column, reports null.
      (to_jsonb(s) ->> 'toplevel')::boolean AS toplevel
    FROM pg_stat_statements s
    LEFT JOIN rate_sample_start r
      ON r.k = s.userid::text || '/' || s.queryid::text || '/'
               || coalesce(to_jsonb(s) ->> 'toplevel', '') || '/'
               || coalesce(to_jsonb(s) ->> 'stats_since', '')
    WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
      AND calls >= CAST(:'min_executions' AS bigint)
    ORDER BY total_exec_time DESC
    :query_limit_clause
  ) q
),

-- ===================== 11. Global Stats =====================
global_stats_data AS (
  SELECT json_build_object(
    'uptime', extract(epoch FROM (now() - pg_postmaster_start_time()))::bigint::text,
    'stats_reset_age_seconds', (:pgss_reset_age)::text,
    'questions', (SELECT sum(calls) FROM pg_stat_statements WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database()))::text,
    'threads_connected', (SELECT count(*) FROM pg_stat_activity WHERE datname = current_database())::text,
    'threads_running', (SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND state = 'active')::text,
    'innodb_buffer_pool_read_requests', (SELECT sum(shared_blks_hit + shared_blks_read) FROM pg_stat_statements WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database()))::text,
    'innodb_buffer_pool_reads', (SELECT sum(shared_blks_read) FROM pg_stat_statements WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database()))::text,
    'created_tmp_disk_tables', (SELECT sum(temp_blks_read) FROM pg_stat_statements WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database()))::text,
    'created_tmp_tables', (SELECT sum(temp_blks_written) FROM pg_stat_statements WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database()))::text
  ) AS data
),

-- ===================== 12. Installed Extensions =====================
-- Every extension installed in this database and its version (#1078): the
-- relational-move gap check compares them with the extensions Aurora PostgreSQL offers.
extensions_data AS (
  SELECT coalesce(json_agg(e ORDER BY e.name), '[]'::json) AS data
  FROM (
    SELECT extname AS name, extversion AS version
    FROM pg_extension
  ) e
),

-- ===================== 13. Enum Types =====================
-- Every enum type and its labels in the order PostgreSQL sorts them (#1834): a
-- same-engine move creates each type on Aurora PostgreSQL before its tables.
enum_types_data AS (
  SELECT coalesce(json_agg(et ORDER BY et.schema_name, et.type_name), '[]'::json) AS data
  FROM (
    SELECT
      n.nspname AS schema_name,
      t.typname AS type_name,
      json_agg(e.enumlabel ORDER BY e.enumsortorder) AS labels
    FROM pg_type t
    JOIN pg_namespace n ON n.oid = t.typnamespace
    JOIN pg_enum e ON e.enumtypid = t.oid
    WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
    GROUP BY n.nspname, t.typname
  ) et
)

-- ===================== 14. Assemble Final JSON =====================
SELECT json_build_object(
  'collection_version', '1.0',
  -- #1991: UTC, as the Z says (now() prints in the session's TimeZone).
  'collected_at', to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
  'metadata', (SELECT data FROM metadata),
  'tables', (SELECT data FROM tables_data),
  'columns', (SELECT data FROM columns_data),
  'indexes', (SELECT data FROM indexes_data),
  'foreign_keys', (SELECT data FROM foreign_keys_data),
  'primary_keys', (SELECT data FROM primary_keys_data),
  'views', (SELECT data FROM views_data),
  'procedures', (SELECT data FROM procedures_data),
  'triggers', (SELECT data FROM triggers_data),
  'routine_stats', (SELECT data FROM routine_stats_data),
  'routine_stats_status', (SELECT data FROM routine_stats_status_data),
  'queries', (SELECT data FROM queries_data),
  'global_stats', (SELECT data FROM global_stats_data),
  'extensions', (SELECT data FROM extensions_data),
  'enum_types', (SELECT data FROM enum_types_data)
);
