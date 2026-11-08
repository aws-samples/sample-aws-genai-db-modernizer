-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0

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
--   mysql -N --max-allowed-packet=1G --raw -u <user> -p -h <host> -D <database> < collect-mysql.sql > collection.json
--
-- --max-allowed-packet=1G lets the client receive a large collection. The statement
-- list is built on the server, whose max_allowed_packet (64 MB by default) bounds it:
-- past that the server returns it as NULL with a warning. The script records how many
-- statements it selected, so the ingest reports a list that came back short.
--
-- The -N flag suppresses column headers so the output is valid JSON. --raw keeps the
-- client from escaping backslashes and newlines in the output, which would break the
-- JSON escapes of any text holding a double quote (routine bodies often do, #1652).
-- If you forgot -N, the parser will strip the header line automatically.
--
-- Optional settings (#384), as session variables set when the client connects:
--   mysql --init-command="SET @query_limit=20000, @min_executions=5" -N --max-allowed-packet=1G --raw ... < collect-mysql.sql
--   @query_limit     keep only the costliest statements by total time
--                    (default: no cap, every statement is kept)
--   @min_executions  keep statements that ran at least this often (default: 10)
-- Both are recorded in the collection's metadata.query_capture. A value that is not a
-- positive whole number falls back to the default (no cap, or 10): the client cannot
-- stop a script on a condition, so the metadata records the values the run used.
--
-- Call rates over the load period (#1908), as a session variable:
--   mysql --init-command="SET @rate_sample_seconds=300" ...
--   @rate_sample_seconds  read the statement counters, wait this many seconds
--                         (DO SLEEP), read them again (default: 300); 0: one read
-- Each statement's rate is the change in its count between the two reads, over the
-- time between them by the server's clock, so run the script while the application
-- carries its usual load. The value used and the measured time are recorded in
-- metadata.rate_sample; a value that is not a whole number of seconds takes the default.
-- Both reads are SELECTs into session variables: nothing is changed or reset. The
-- first read's counts go into metadata.rate_sample.first_counts, keyed as each
-- statement row's sample_key, and the ingest pairs them.
--
-- Do not reset the statement statistics before collecting (#1991), for example
-- with TRUNCATE performance_schema.events_statements_summary_by_digest, and do
-- not collect right after a restart: the counters then cover only the minutes
-- since, often a load test, and every rate is that short window's load. The
-- assessment states the window and labels its costs and risks a scenario when
-- the window is short, starts at a reset, or runs well above the server's own
-- rate.
--
-- Procedure, function, trigger and event code (#1652), as a session variable:
--   mysql --init-command="SET @include_routine_bodies=0" ...
--   @include_routine_bodies  0: list routines, triggers and events by name and size
--                            only, without their code (default: 1, code kept)
-- Privileges for routine code. Without them nothing fails: a routine whose body the
-- user may not read is listed with body_status 'not_readable', and
-- metadata.routine_capture.privileges_not_seen names what is not granted directly.
-- The script never uses them to write.
--   GRANT SHOW_ROUTINE ON *.* TO <user>;     -- MySQL 8.0.20+: read every routine body
--                                            -- (before 8.0.20: global SELECT ON *.*)
--   GRANT TRIGGER ON <database>.* TO <user>; -- list triggers (also allows creating them)
--   GRANT EVENT ON <database>.* TO <user>;   -- list events (also allows creating them)
-- SHOW CREATE cannot run inside a SELECT, so the script reads the information_schema
-- columns it prints from, and the ingest rebuilds each CREATE from them. Routine
-- execution counts come from performance_schema.events_statements_summary_by_program
-- when the server has it; no setting is changed.
-- ==========================================================================

SET @db = DATABASE();
-- The settings: an unset variable is NULL, so the cap stays off and the floor is 10. A
-- value that is not a positive whole number takes the default too, never 0 (a typo's
-- CAST) or a wrapped negative.
SET @min_executions = IF(CAST(@min_executions AS CHAR) REGEXP '^[1-9][0-9]*$', CAST(@min_executions AS UNSIGNED), 10);
SET @query_limit = IF(CAST(@query_limit AS CHAR) REGEXP '^[1-9][0-9]*$', CAST(@query_limit AS UNSIGNED), NULL);
SET @rate_sample_seconds = IF(CAST(@rate_sample_seconds AS CHAR) REGEXP '^[0-9]+$', CAST(@rate_sample_seconds AS UNSIGNED), 300);
SET SESSION group_concat_max_len = 1048576;
-- #1652: routine code is kept unless the session set @include_routine_bodies to 0.
SET @include_routine_bodies = IF(CAST(@include_routine_bodies AS CHAR) = '0', 0, 1);

-- We build each section as a JSON fragment, then combine at the end.

-- ===================== 1. Metadata =====================
-- settings.sql_mode: the server's default sql_mode, which the application's sessions
-- start with. With NO_BACKSLASH_ESCAPES a backslash has no meaning in a string, and
-- the collected statements are read that way (#1482).
SELECT JSON_OBJECT(
  'version', VERSION(),
  'database_name', @db,
  'database_size_gb', (
    SELECT ROUND(SUM(data_length + index_length) / 1024 / 1024 / 1024, 4)
    FROM information_schema.tables WHERE table_schema = @db
  ),
  'settings', JSON_OBJECT('sql_mode', @@GLOBAL.sql_mode),
  -- The capture settings (#384): no cap (null) unless @query_limit is set.
  'query_capture', JSON_OBJECT('query_limit', @query_limit, 'min_executions', @min_executions)
) INTO @metadata;

-- ===================== 2. Tables =====================
-- row_count is TABLE_ROWS as the server records it (no table is read): exact for
-- MyISAM and Aria, an estimate for InnoDB and the rest, and null when there is
-- none, never 0 in its place. information_schema.TABLES records no statistics
-- time, so none is reported.
SELECT JSON_ARRAYAGG(t_obj) INTO @tables FROM (
  SELECT JSON_OBJECT(
    'table_name', table_name,
    'row_count', table_rows,
    'row_count_basis', IF(engine IN ('MyISAM', 'Aria'), 'exact', 'estimate'),
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
    'index_type', index_type,
    'sub_part', sub_part
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
-- sql_mode: the mode each body was created with, which MySQL parses it under (#1482).
-- #1652: every column SHOW CREATE prints a routine from, and the session it was
-- created in; ROUTINE_DEFINITION is NULL when the user may not read the body (not its
-- DEFINER, no SHOW_ROUTINE).
SELECT JSON_ARRAYAGG(p_obj) INTO @procedures FROM (
  SELECT JSON_OBJECT(
    'routine_name', routine_name,
    'schema_name', routine_schema,
    'specific_name', specific_name,
    'routine_type', routine_type,
    'return_type', data_type,
    'definer', definer,
    'security_type', security_type,
    'is_deterministic', is_deterministic,
    'sql_data_access', sql_data_access,
    'routine_comment', routine_comment,
    'external_language', external_language,
    'sql_mode', sql_mode,
    'character_set_client', character_set_client,
    'collation_connection', collation_connection,
    'database_collation', database_collation,
    'body_chars', CHAR_LENGTH(routine_definition),
    'definition', IF(@include_routine_bodies = 1, routine_definition, NULL),
    'body_status', CASE WHEN @include_routine_bodies = 0 THEN 'opted_out'
                        WHEN routine_definition IS NULL THEN 'not_readable'
                        ELSE 'collected' END
  ) AS p_obj
  FROM information_schema.routines
  WHERE routine_schema = @db
  ORDER BY routine_type, routine_name
) AS p;

-- ===================== 9. Triggers =====================
-- #1652: listed only with the TRIGGER privilege; orientation, DEFINER, order among the
-- table's triggers, and the session each was created in.
SELECT JSON_ARRAYAGG(tr_obj) INTO @triggers FROM (
  SELECT JSON_OBJECT(
    'trigger_name', trigger_name,
    'schema_name', trigger_schema,
    'event_type', event_manipulation,
    'table_name', event_object_table,
    'timing', action_timing,
    'for_each', action_orientation,
    'sql_mode', sql_mode,
    'definer', definer,
    'action_order', action_order,
    'character_set_client', character_set_client,
    'collation_connection', collation_connection,
    'database_collation', database_collation,
    'body_chars', CHAR_LENGTH(action_statement),
    'definition', IF(@include_routine_bodies = 1, action_statement, NULL),
    'body_status', IF(@include_routine_bodies = 1, 'collected', 'opted_out')
  ) AS tr_obj
  FROM information_schema.triggers
  WHERE trigger_schema = @db
  ORDER BY event_object_table, trigger_name
) AS tr;

-- ===================== 9b. Routine stats (#1652) =====================
-- performance_schema keeps per-program counts (MySQL 5.7+); the script reads them only
-- when the table exists (MariaDB may not have it) and never changes a setting.
-- SUM_TIMER_WAIT is in picoseconds.
SELECT COUNT(*) INTO @has_by_program FROM information_schema.tables
 WHERE table_schema = 'performance_schema' AND table_name = 'events_statements_summary_by_program';
SET @routine_stats_sql = IF(@has_by_program > 0,
  'SELECT JSON_ARRAYAGG(JSON_OBJECT(''routine_name'', OBJECT_NAME, ''schema_name'', OBJECT_SCHEMA,
     ''object_type'', OBJECT_TYPE, ''execution_count'', COUNT_STAR,
     ''total_time_ms'', ROUND(SUM_TIMER_WAIT / 1000000000, 3)))
     INTO @routine_stats
     FROM performance_schema.events_statements_summary_by_program
    WHERE OBJECT_SCHEMA = @db',
  'SELECT NULL INTO @routine_stats');
PREPARE routine_stats_stmt FROM @routine_stats_sql;
EXECUTE routine_stats_stmt;
DEALLOCATE PREPARE routine_stats_stmt;
SET @routine_stats_status = IF(@has_by_program > 0 AND @@performance_schema = 1,
  JSON_OBJECT('status', 'tracked'),
  JSON_OBJECT('status', 'not_tracked', 'reason',
              'performance_schema program statistics are not available'));

-- ===================== 9c. Routine parameters (#1652) =====================
-- Each routine's parameters and a function's return type (ordinal 0), with the
-- character set and collation each resolved to: the CREATE is rebuilt from them.
SELECT JSON_ARRAYAGG(prm_obj) INTO @routine_parameters FROM (
  SELECT JSON_OBJECT(
    'routine_name', specific_name, 'schema_name', specific_schema, 'routine_type', routine_type,
    'ordinal', ordinal_position, 'mode', parameter_mode, 'parameter_name', parameter_name,
    'data_type', data_type, 'dtd_identifier', dtd_identifier,
    'character_set_name', character_set_name, 'collation_name', collation_name
  ) AS prm_obj
  FROM information_schema.parameters
  WHERE specific_schema = @db
  ORDER BY routine_type, specific_name, ordinal_position
) AS prm;

-- ===================== 9d. Events (#1652) =====================
-- The scheduled events, with what SHOW CREATE EVENT prints them from; the times are in
-- each event's own time_zone, which its CREATE is replayed in. Listed only with EVENT.
SELECT JSON_ARRAYAGG(e_obj) INTO @events FROM (
  SELECT JSON_OBJECT(
    'event_name', event_name, 'schema_name', event_schema, 'definer', definer,
    'time_zone', time_zone, 'event_type', event_type,
    'execute_at', DATE_FORMAT(execute_at, '%Y-%m-%d %H:%i:%s'),
    'interval_value', CAST(interval_value AS CHAR), 'interval_field', interval_field,
    'starts', DATE_FORMAT(starts, '%Y-%m-%d %H:%i:%s'),
    'ends', DATE_FORMAT(ends, '%Y-%m-%d %H:%i:%s'),
    'status', status, 'on_completion', on_completion, 'event_comment', event_comment,
    'sql_mode', sql_mode, 'character_set_client', character_set_client,
    'collation_connection', collation_connection, 'database_collation', database_collation,
    'body_chars', CHAR_LENGTH(event_definition),
    'definition', IF(@include_routine_bodies = 1, event_definition, NULL),
    'body_status', IF(@include_routine_bodies = 1, 'collected', 'opted_out')
  ) AS e_obj
  FROM information_schema.events
  WHERE event_schema = @db
  ORDER BY event_name
) AS e;

-- ===================== 9e. Routine capture (#1652) =====================
-- How many routines, triggers and events the server counts, so the ingest reports a
-- list max_allowed_packet turned into NULL (as for the statements), and which of the
-- privileges that decide what is visible this user lacks as a direct grant (a role may
-- still grant one: a hint, never a failure).
SET @grantee = CONCAT('''', SUBSTRING_INDEX(CURRENT_USER(), '@', 1), '''@''',
                      SUBSTRING_INDEX(CURRENT_USER(), '@', -1), '''');
SELECT (SELECT COUNT(*) FROM information_schema.routines WHERE routine_schema = @db)
     + (SELECT COUNT(*) FROM information_schema.triggers WHERE trigger_schema = @db)
     + (SELECT COUNT(*) FROM information_schema.events WHERE event_schema = @db)
  INTO @routines_counted;
-- The parameters it counts: a parameter list that comes back NULL is a cut one, never
-- an empty one, and the ingest then rebuilds no routine from it.
SELECT COUNT(*) INTO @parameters_counted FROM information_schema.parameters
 WHERE specific_schema = @db;
SELECT JSON_ARRAYAGG(p.privilege) INTO @privileges_not_seen FROM (
  SELECT 'SHOW_ROUTINE' AS privilege UNION ALL SELECT 'TRIGGER' UNION ALL SELECT 'EVENT'
) AS p
WHERE NOT EXISTS (
        SELECT 1 FROM information_schema.user_privileges u
         WHERE u.grantee = @grantee
           AND (u.privilege_type = p.privilege
                OR (p.privilege = 'SHOW_ROUTINE' AND u.privilege_type = 'SELECT')))
  AND NOT EXISTS (
        SELECT 1 FROM information_schema.schema_privileges s
         WHERE s.grantee = @grantee AND s.table_schema = @db
           AND s.privilege_type = p.privilege);
SET @routine_capture = JSON_OBJECT(
  -- JSON_EXTRACT, not CAST(... AS JSON), which MariaDB does not have.
  'include_bodies', JSON_EXTRACT(IF(@include_routine_bodies = 1, 'true', 'false'), '$'),
  'routines_counted', @routines_counted,
  'parameters_counted', @parameters_counted,
  'privileges_not_seen', IFNULL(JSON_EXTRACT(@privileges_not_seen, '$'), JSON_ARRAY()));

-- ===================== 10. Query Patterns (performance_schema) =====================
-- #1908: the first read of the counters, the wait, and the server's clock at each read
-- (UTC_TIMESTAMP(6), the statement's start in UTC, so a daylight-saving change in the
-- session's time zone does not move it; a KILL QUERY that ends the wait early leaves a
-- measured time too short to rate). An entry is its digest and FIRST_SEEN, so a digest
-- emptied and counted again in between is a new entry, not a lower count.
SET @rate_sample_t0 = IF(@rate_sample_seconds > 0, UTC_TIMESTAMP(6), NULL);
SELECT JSON_OBJECTAGG(
  CONCAT(DIGEST, '/', IFNULL(DATE_FORMAT(FIRST_SEEN, '%Y-%m-%d %H:%i:%s.%f'), '')),
  COUNT_STAR) INTO @rate_sample_start
FROM performance_schema.events_statements_summary_by_digest
WHERE @rate_sample_seconds > 0 AND SCHEMA_NAME = @db AND DIGEST IS NOT NULL;
DO SLEEP(@rate_sample_seconds);
SET @rate_sample_elapsed = IF(@rate_sample_seconds > 0, ROUND(TIMESTAMPDIFF(MICROSECOND, @rate_sample_t0, UTC_TIMESTAMP(6)) / 1000000, 3), NULL);

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
    'last_seen', DATE_FORMAT(LAST_SEEN, '%Y-%m-%d %H:%i:%s'),
    -- #1908: the key of the digest's count at the first read (first_counts).
    'sample_key', CONCAT(DIGEST, '/', IFNULL(DATE_FORMAT(FIRST_SEEN, '%Y-%m-%d %H:%i:%s.%f'), ''))
  ) AS q_obj
  -- LIMIT takes no variable outside a prepared statement, so the cap is a rank:
  -- the costliest statements first, all of them when @query_limit is NULL (#384).
  FROM (
    SELECT d.*, ROW_NUMBER() OVER (ORDER BY SUM_TIMER_WAIT DESC, DIGEST) AS time_rank
    FROM performance_schema.events_statements_summary_by_digest d
    WHERE SCHEMA_NAME = @db
      AND COUNT_STAR >= @min_executions
  ) AS ranked
  WHERE @query_limit IS NULL OR ranked.time_rank <= @query_limit
  ORDER BY ranked.time_rank
) AS q;

-- How many statements the list above holds when it comes back whole: the ingest
-- reports a list the server's max_allowed_packet turned into NULL (#384).
SELECT COUNT(*) INTO @query_rows
FROM performance_schema.events_statements_summary_by_digest
WHERE SCHEMA_NAME = @db
  AND COUNT_STAR >= @min_executions;
SET @query_rows = IF(@query_limit IS NULL, @query_rows, LEAST(@query_rows, @query_limit));

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
  -- #1991: UTC, as the Z says (NOW() is the session's time zone).
  'collected_at', DATE_FORMAT(UTC_TIMESTAMP(), '%Y-%m-%dT%H:%i:%sZ'),
  'metadata', JSON_SET(@metadata, '$.query_capture.statements', @query_rows,
    -- #1908: the wait used and the seconds the server's clock measured between the reads.
    '$.rate_sample', JSON_OBJECT('seconds', @rate_sample_seconds, 'elapsed_seconds', @rate_sample_elapsed,
      'first_counts', IFNULL(JSON_EXTRACT(@rate_sample_start, '$'), JSON_OBJECT())),
    -- #1652: how routine code was read.
    '$.routine_capture', JSON_EXTRACT(@routine_capture, '$')),
  'tables', IFNULL(JSON_EXTRACT(@tables, '$'), JSON_ARRAY()),
  'columns', IFNULL(JSON_EXTRACT(@columns, '$'), JSON_ARRAY()),
  'indexes', IFNULL(JSON_EXTRACT(@indexes, '$'), JSON_ARRAY()),
  'foreign_keys', IFNULL(JSON_EXTRACT(@foreign_keys, '$'), JSON_ARRAY()),
  'primary_keys', IFNULL(JSON_EXTRACT(@primary_keys, '$'), JSON_ARRAY()),
  'views', IFNULL(JSON_EXTRACT(@views, '$'), JSON_ARRAY()),
  'procedures', IFNULL(JSON_EXTRACT(@procedures, '$'), JSON_ARRAY()),
  'triggers', IFNULL(JSON_EXTRACT(@triggers, '$'), JSON_ARRAY()),
  'routine_stats', IFNULL(JSON_EXTRACT(@routine_stats, '$'), JSON_ARRAY()),
  'routine_stats_status', JSON_EXTRACT(@routine_stats_status, '$'),
  'routine_parameters', IF(@parameters_counted = 0, JSON_ARRAY(), JSON_EXTRACT(@routine_parameters, '$')),
  'events', IFNULL(JSON_EXTRACT(@events, '$'), JSON_ARRAY()),
  'queries', IFNULL(JSON_EXTRACT(@queries, '$'), JSON_ARRAY()),
  'global_stats', IFNULL(JSON_EXTRACT(@global_stats, '$'), JSON_OBJECT())
) AS collection_output;
