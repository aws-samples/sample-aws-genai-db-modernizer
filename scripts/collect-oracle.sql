-- ==========================================================================
-- Database Modernizer — Oracle Offline Collection Script
-- ==========================================================================
-- Run this against your Oracle database (12c+) to produce a JSON snapshot
-- that the collector agent can ingest in "offline" mode. No IAM permissions,
-- no EC2 automation instance, no AWS API calls required.
--
-- Works for: RDS Oracle, EC2 self-managed, on-premises Oracle.
--
-- Field names follow MySQL convention for offline_parser compatibility.
-- The script performs the Oracle→MySQL field name translation inline.
--
-- Requirements:
--   Oracle 12.2+ (12c Release 2 or later — uses JSON_OBJECT, FETCH FIRST N ROWS ONLY)
--   Does NOT work on 11g or 12.1 (12c R1) — JSON_OBJECT requires 12.2.
--   For 11g/12.1 support, a separate script using string concatenation would be needed.
--   User needs SELECT on V$SQL, V$SESSION, V$SYSSTAT, V$SYSTEM_EVENT,
--   V$OSSTAT, ALL_* catalog views, DBA_SEGMENTS.
--   Grant SELECT_CATALOG_ROLE for full visibility.
--
-- Usage:
--   sqlplus -S <user>/<pass>@<host>:<port>/<service> @collect-oracle.sql > collection.json
--
-- Output: Single JSON object matching offline_parser expectations.
-- ==========================================================================

SET PAGESIZE 0
SET LINESIZE 32767
SET LONG 32767
SET LONGCHUNKSIZE 32767
SET FEEDBACK OFF
SET HEADING OFF
SET TRIMSPOOL ON
SET TRIMOUT ON
SET VERIFY OFF
SET DEFINE OFF
SET SERVEROUTPUT ON SIZE UNLIMITED

DECLARE
    v_owner       VARCHAR2(128);
    v_total_elapsed NUMBER := 0;
BEGIN
    v_owner := SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA');

    -- Pre-compute total elapsed for db_load_contribution_percent, over the same
    -- statement shapes the queries section below emits (see there).
    SELECT NVL(SUM(shape_elapsed), 1) INTO v_total_elapsed
    FROM (
        SELECT SUM(ELAPSED_TIME) AS shape_elapsed
        FROM V$SQL
        WHERE PARSING_SCHEMA_NAME = v_owner
        GROUP BY CASE WHEN NVL(FORCE_MATCHING_SIGNATURE, 0) <> 0 THEN 'fms_' || TO_CHAR(FORCE_MATCHING_SIGNATURE) ELSE SQL_ID END
        HAVING SUM(EXECUTIONS) >= 10
    );

    DBMS_OUTPUT.PUT_LINE('{');

    -- ===================================================================
    -- 1. Query Patterns (FIRST — avoid cache pollution from catalog queries)
    -- Field names: MySQL offline_parser convention
    --
    -- V$SQL keeps one row per distinct SQL text (per SQL_ID), so an application
    -- that sends non-parameterized SQL gets a row per literal value
    -- (WHERE id = 1, WHERE id = 2, ...). SQL_ID hashes the exact text, so it
    -- cannot tell them apart from a different statement. FORCE_MATCHING_SIGNATURE
    -- is the signature Oracle computes with literals replaced by binds (the one
    -- CURSOR_SHARING=FORCE uses); literal variants share it. Oracle Database
    -- Reference, V$SQL: "FORCE_MATCHING_SIGNATURE: The signature used when the
    -- CURSOR_SHARING parameter is set to FORCE" (0 when the statement has none).
    -- We group by it, so one row == one query shape, and fall back to the
    -- SQL_ID when the signature is 0. The digest is 'fms_<signature>' or the
    -- SQL_ID. Counters are SUMmed across variants, first_seen / last_seen take
    -- MIN / MAX, and the query text is the variant with the highest
    -- ELAPSED_TIME (SQL_ID breaks ties). The 10-execution floor and the
    -- FETCH FIRST 1000 cap apply AFTER grouping, so the cap limits query shapes
    -- rather than literal-variant rows. V$SQL has no per-execution min/max, so
    -- those stay 0.
    -- ===================================================================
    DBMS_OUTPUT.PUT_LINE('"queries": [');
    FOR rec IN (
        SELECT JSON_OBJECT(
            'digest' VALUE g.query_key,
            'query_text' VALUE REGEXP_REPLACE(g.query_text, '[[:cntrl:]]', ' '),
            'execution_count' VALUE g.executions,
            'total_time_ms' VALUE ROUND(g.elapsed_time / 1000, 3),
            'avg_time_ms' VALUE ROUND(g.elapsed_time / GREATEST(g.executions, 1) / 1000, 3),
            'min_time_ms' VALUE 0,
            'max_time_ms' VALUE 0,
            'total_rows_sent' VALUE g.rows_processed,
            'total_rows_examined' VALUE g.buffer_gets,
            'total_rows_affected' VALUE 0,
            'lock_time_ms' VALUE ROUND(g.application_wait_time / 1000, 3),
            'total_cpu_time_ms' VALUE ROUND(g.cpu_time / 1000, 3),
            'total_logical_reads' VALUE g.buffer_gets,
            'total_physical_reads' VALUE g.disk_reads,
            'db_load_contribution_percent' VALUE ROUND(g.elapsed_time / v_total_elapsed * 100, 2),
            'first_seen' VALUE REPLACE(g.first_load_time, '/', ' '),
            'last_seen' VALUE TO_CHAR(g.last_active_time, 'YYYY-MM-DD HH24:MI:SS')
            RETURNING CLOB
        ) AS json_row
        FROM (
            SELECT v.query_key,
                   SUM(v.executions) AS executions,
                   SUM(v.elapsed_time) AS elapsed_time,
                   SUM(v.rows_processed) AS rows_processed,
                   SUM(v.buffer_gets) AS buffer_gets,
                   SUM(v.cpu_time) AS cpu_time,
                   SUM(v.disk_reads) AS disk_reads,
                   SUM(v.application_wait_time) AS application_wait_time,
                   MIN(v.first_load_time) AS first_load_time,
                   MAX(v.last_active_time) AS last_active_time,
                   MAX(CASE WHEN v.variant_rank = 1 THEN v.query_text END) AS query_text
            FROM (
                -- One row per cached statement (literal variant); variant_rank
                -- picks the costliest variant per query_key as the sample text.
                SELECT p.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY query_key
                           ORDER BY ELAPSED_TIME DESC, SQL_ID
                       ) AS variant_rank
                FROM (
                    SELECT SQL_ID,
                           CASE WHEN NVL(FORCE_MATCHING_SIGNATURE, 0) <> 0 THEN 'fms_' || TO_CHAR(FORCE_MATCHING_SIGNATURE) ELSE SQL_ID END AS query_key,
                           DBMS_LOB.SUBSTR(SQL_FULLTEXT, 2000, 1) AS query_text,
                           EXECUTIONS AS executions,
                           ELAPSED_TIME AS elapsed_time,
                           ROWS_PROCESSED AS rows_processed,
                           BUFFER_GETS AS buffer_gets,
                           CPU_TIME AS cpu_time,
                           DISK_READS AS disk_reads,
                           APPLICATION_WAIT_TIME AS application_wait_time,
                           FIRST_LOAD_TIME AS first_load_time,
                           LAST_ACTIVE_TIME AS last_active_time
                    FROM V$SQL
                    WHERE PARSING_SCHEMA_NAME = v_owner
                ) p
            ) v
            GROUP BY v.query_key
        ) g
        WHERE g.executions >= 10
        ORDER BY g.elapsed_time DESC, g.query_key
        FETCH FIRST 1000 ROWS ONLY
    ) LOOP
        DBMS_OUTPUT.PUT_LINE(rec.json_row || ',');
    END LOOP;
    DBMS_OUTPUT.PUT_LINE('{"_sentinel": true}');
    DBMS_OUTPUT.PUT_LINE('],');

    -- ===================================================================
    -- 2. Metadata
    -- ===================================================================
    DBMS_OUTPUT.PUT_LINE('"metadata": ');
    FOR rec IN (
        SELECT JSON_OBJECT(
            'version' VALUE BANNER,
            'database_name' VALUE SYS_CONTEXT('USERENV', 'DB_NAME'),
            'current_schema' VALUE v_owner
        ) AS j FROM V$VERSION WHERE ROWNUM = 1
    ) LOOP
        DBMS_OUTPUT.PUT_LINE(rec.j || ',');
    END LOOP;

    -- ===================================================================
    -- 3. Tables
    -- Filter to tables with at least one column entry — excludes Oracle
    -- internal metadata tables (SYS_IOT_OVER_*, HS_PARTITION_COL_*, etc.)
    -- which appear in ALL_TABLES but have no rows in ALL_TAB_COLUMNS and
    -- would fail Table.columns min_length=1 validation downstream.
    -- ===================================================================
    DBMS_OUTPUT.PUT_LINE('"tables": [');
    FOR rec IN (
        SELECT JSON_OBJECT(
            'table_name' VALUE LOWER(t.TABLE_NAME),
            'schema_name' VALUE LOWER(t.OWNER),
            'row_count' VALUE NVL(t.NUM_ROWS, 0),
            'data_size_mb' VALUE ROUND(NVL(s.bytes, 0)/1024/1024, 2)
        ) AS j
        FROM ALL_TABLES t
        LEFT JOIN (
            SELECT OWNER, SEGMENT_NAME, SUM(BYTES) AS bytes
            FROM DBA_SEGMENTS WHERE SEGMENT_TYPE = 'TABLE'
            GROUP BY OWNER, SEGMENT_NAME
        ) s ON s.OWNER = t.OWNER AND s.SEGMENT_NAME = t.TABLE_NAME
        WHERE t.OWNER = v_owner
          AND EXISTS (
              SELECT 1 FROM ALL_TAB_COLUMNS c
              WHERE c.OWNER = t.OWNER AND c.TABLE_NAME = t.TABLE_NAME
          )
        ORDER BY t.TABLE_NAME
    ) LOOP
        DBMS_OUTPUT.PUT_LINE(rec.j || ',');
    END LOOP;
    DBMS_OUTPUT.PUT_LINE('{"_sentinel": true}');
    DBMS_OUTPUT.PUT_LINE('],');

    -- ===================================================================
    -- 4. Columns
    -- max_length: NUMBER has none (its precision travels in numeric_precision
    -- below, never here -- a character count and a digit count are different
    -- things, #156). For character types, CHAR_LENGTH ("the length of the
    -- column in characters") is used directly rather than derived from the
    -- byte count DATA_LENGTH with a fixed divisor, which cannot be correct in
    -- general: the bytes-per-character ratio is a property of the database's
    -- character set, not a constant (#156). CHAR_LENGTH is 0, not NULL, for
    -- non-character types, so a positive value is required before using it;
    -- the fallback to DATA_LENGTH (unconverted) only applies to a collection
    -- captured before CHAR_LENGTH was selected here.
    -- numeric_precision/numeric_scale: emitted under the generic keys the
    -- shared offline builder (mysql_collector.py::_build_columns) reads for
    -- every engine, not Oracle's own data_precision/data_scale.
    -- ===================================================================
    DBMS_OUTPUT.PUT_LINE('"columns": [');
    FOR rec IN (
        SELECT JSON_OBJECT(
            'table_name' VALUE LOWER(TABLE_NAME),
            'column_name' VALUE LOWER(COLUMN_NAME),
            'ordinal_position' VALUE COLUMN_ID,
            'data_type' VALUE LOWER(DATA_TYPE),
            'max_length' VALUE CASE
                WHEN LOWER(DATA_TYPE) = 'number' THEN NULL
                WHEN NVL(CHAR_LENGTH, 0) > 0 THEN CHAR_LENGTH
                ELSE DATA_LENGTH
            END,
            'numeric_precision' VALUE DATA_PRECISION,
            'numeric_scale' VALUE DATA_SCALE,
            'is_nullable' VALUE NULLABLE,
            'column_default' VALUE NULL,
            'is_identity' VALUE IDENTITY_COLUMN
        ) AS j
        FROM ALL_TAB_COLUMNS
        WHERE OWNER = v_owner
        ORDER BY TABLE_NAME, COLUMN_ID
    ) LOOP
        DBMS_OUTPUT.PUT_LINE(rec.j || ',');
    END LOOP;
    DBMS_OUTPUT.PUT_LINE('{"_sentinel": true}');
    DBMS_OUTPUT.PUT_LINE('],');

    -- ===================================================================
    -- 5. Indexes (MySQL-compatible field names)
    -- ===================================================================
    DBMS_OUTPUT.PUT_LINE('"indexes": [');
    FOR rec IN (
        SELECT JSON_OBJECT(
            'table_name' VALUE LOWER(i.TABLE_NAME),
            'index_name' VALUE CASE WHEN c.CONSTRAINT_TYPE = 'P' THEN 'PRIMARY' ELSE LOWER(i.INDEX_NAME) END,
            'column_name' VALUE LOWER(ic.COLUMN_NAME),
            'non_unique' VALUE CASE WHEN i.UNIQUENESS = 'UNIQUE' THEN 0 ELSE 1 END,
            'index_type' VALUE CASE
                -- Contract enum: btree|hash|gist|gin|fulltext|spatial|other
                -- Map Oracle's INDEX_TYPE values into it.
                WHEN i.INDEX_TYPE LIKE '%FUNCTION%' THEN 'other'
                WHEN i.INDEX_TYPE LIKE '%BITMAP%' THEN 'other'
                WHEN i.INDEX_TYPE LIKE 'IOT%' THEN 'other'
                WHEN i.INDEX_TYPE LIKE '%CLUSTER%' THEN 'other'
                WHEN i.INDEX_TYPE LIKE '%DOMAIN%' THEN 'other'
                WHEN i.INDEX_TYPE LIKE 'NORMAL%' THEN 'btree'
                ELSE 'other'
            END
        ) AS j
        FROM ALL_INDEXES i
        JOIN ALL_IND_COLUMNS ic ON i.OWNER = ic.INDEX_OWNER AND i.INDEX_NAME = ic.INDEX_NAME
        LEFT JOIN ALL_CONSTRAINTS c ON c.OWNER = i.OWNER AND c.INDEX_NAME = i.INDEX_NAME AND c.CONSTRAINT_TYPE = 'P'
        WHERE i.TABLE_OWNER = v_owner
        ORDER BY i.TABLE_NAME, i.INDEX_NAME, ic.COLUMN_POSITION
    ) LOOP
        DBMS_OUTPUT.PUT_LINE(rec.j || ',');
    END LOOP;
    DBMS_OUTPUT.PUT_LINE('{"_sentinel": true}');
    DBMS_OUTPUT.PUT_LINE('],');

    -- ===================================================================
    -- 6. Foreign Keys (MySQL-compatible field names)
    -- ===================================================================
    DBMS_OUTPUT.PUT_LINE('"foreign_keys": [');
    FOR rec IN (
        SELECT JSON_OBJECT(
            'table_name' VALUE LOWER(a.TABLE_NAME),
            'constraint_name' VALUE LOWER(a.CONSTRAINT_NAME),
            'column_name' VALUE LOWER(ac.COLUMN_NAME),
            'referenced_table_name' VALUE LOWER(rc.TABLE_NAME),
            'referenced_column_name' VALUE LOWER(rcc.COLUMN_NAME),
            'delete_rule' VALUE a.DELETE_RULE
        ) AS j
        FROM ALL_CONSTRAINTS a
        JOIN ALL_CONS_COLUMNS ac ON a.OWNER = ac.OWNER AND a.CONSTRAINT_NAME = ac.CONSTRAINT_NAME
        JOIN ALL_CONSTRAINTS rc ON a.R_OWNER = rc.OWNER AND a.R_CONSTRAINT_NAME = rc.CONSTRAINT_NAME
        JOIN ALL_CONS_COLUMNS rcc ON rc.OWNER = rcc.OWNER AND rc.CONSTRAINT_NAME = rcc.CONSTRAINT_NAME AND ac.POSITION = rcc.POSITION
        WHERE a.CONSTRAINT_TYPE = 'R' AND a.OWNER = v_owner
        ORDER BY a.TABLE_NAME, a.CONSTRAINT_NAME, ac.POSITION
    ) LOOP
        DBMS_OUTPUT.PUT_LINE(rec.j || ',');
    END LOOP;
    DBMS_OUTPUT.PUT_LINE('{"_sentinel": true}');
    DBMS_OUTPUT.PUT_LINE('],');

    -- ===================================================================
    -- 7. Primary Keys
    -- ===================================================================
    DBMS_OUTPUT.PUT_LINE('"primary_keys": [');
    FOR rec IN (
        SELECT JSON_OBJECT(
            'table_name' VALUE LOWER(c.TABLE_NAME),
            'column_name' VALUE LOWER(cc.COLUMN_NAME),
            'position' VALUE cc.POSITION
        ) AS j
        FROM ALL_CONSTRAINTS c
        JOIN ALL_CONS_COLUMNS cc ON c.OWNER = cc.OWNER AND c.CONSTRAINT_NAME = cc.CONSTRAINT_NAME
        WHERE c.CONSTRAINT_TYPE = 'P' AND c.OWNER = v_owner
        ORDER BY c.TABLE_NAME, cc.POSITION
    ) LOOP
        DBMS_OUTPUT.PUT_LINE(rec.j || ',');
    END LOOP;
    DBMS_OUTPUT.PUT_LINE('{"_sentinel": true}');
    DBMS_OUTPUT.PUT_LINE('],');

    -- ===================================================================
    -- 8. Views
    -- ===================================================================
    DBMS_OUTPUT.PUT_LINE('"views": [');
    FOR rec IN (
        SELECT JSON_OBJECT(
            'view_name' VALUE LOWER(VIEW_NAME),
            'schema_name' VALUE LOWER(OWNER)
        ) AS j
        FROM ALL_VIEWS WHERE OWNER = v_owner ORDER BY VIEW_NAME
    ) LOOP
        DBMS_OUTPUT.PUT_LINE(rec.j || ',');
    END LOOP;
    DBMS_OUTPUT.PUT_LINE('{"_sentinel": true}');
    DBMS_OUTPUT.PUT_LINE('],');

    -- ===================================================================
    -- 9. Procedures & Functions
    -- ===================================================================
    DBMS_OUTPUT.PUT_LINE('"procedures": [');
    FOR rec IN (
        SELECT JSON_OBJECT(
            'routine_name' VALUE LOWER(OBJECT_NAME),
            'schema_name' VALUE LOWER(OWNER),
            'routine_type' VALUE OBJECT_TYPE
        ) AS j
        FROM ALL_OBJECTS
        WHERE OWNER = v_owner AND OBJECT_TYPE IN ('PROCEDURE', 'FUNCTION', 'PACKAGE')
        ORDER BY OBJECT_TYPE, OBJECT_NAME
    ) LOOP
        DBMS_OUTPUT.PUT_LINE(rec.j || ',');
    END LOOP;
    DBMS_OUTPUT.PUT_LINE('{"_sentinel": true}');
    DBMS_OUTPUT.PUT_LINE('],');

    -- ===================================================================
    -- 10. Triggers
    -- Filter to DML triggers (INSERT/UPDATE/DELETE) with a non-null
    -- table_name. Non-DML triggers (LOGON, DDL, STARTUP, DROP, etc.) are
    -- DB-level and can't map to Trigger.table_id + Trigger.event_type
    -- contract enums.
    -- Normalize compound events like 'INSERT OR UPDATE OR DELETE' to the
    -- first matching keyword. Normalize TRIGGER_TYPE like 'BEFORE EACH ROW'
    -- or 'AFTER STATEMENT' to the BEFORE/AFTER/INSTEAD OF prefix.
    -- ===================================================================
    DBMS_OUTPUT.PUT_LINE('"triggers": [');
    FOR rec IN (
        SELECT JSON_OBJECT(
            'trigger_name' VALUE LOWER(TRIGGER_NAME),
            'table_name' VALUE LOWER(TABLE_NAME),
            'schema_name' VALUE LOWER(OWNER),
            'timing' VALUE CASE
                WHEN TRIGGER_TYPE LIKE 'INSTEAD OF%' THEN 'INSTEAD OF'
                WHEN TRIGGER_TYPE LIKE 'BEFORE%' THEN 'BEFORE'
                WHEN TRIGGER_TYPE LIKE 'AFTER%' THEN 'AFTER'
                ELSE 'AFTER'
            END,
            'event_type' VALUE CASE
                WHEN UPPER(TRIGGERING_EVENT) LIKE '%INSERT%' THEN 'INSERT'
                WHEN UPPER(TRIGGERING_EVENT) LIKE '%UPDATE%' THEN 'UPDATE'
                WHEN UPPER(TRIGGERING_EVENT) LIKE '%DELETE%' THEN 'DELETE'
            END
        ) AS j
        FROM ALL_TRIGGERS
        WHERE OWNER = v_owner
          AND TABLE_NAME IS NOT NULL
          AND TRIGGER_TYPE NOT LIKE '%EVENT%'
          AND (   UPPER(TRIGGERING_EVENT) LIKE '%INSERT%'
               OR UPPER(TRIGGERING_EVENT) LIKE '%UPDATE%'
               OR UPPER(TRIGGERING_EVENT) LIKE '%DELETE%')
        ORDER BY TABLE_NAME, TRIGGER_NAME
    ) LOOP
        DBMS_OUTPUT.PUT_LINE(rec.j || ',');
    END LOOP;
    DBMS_OUTPUT.PUT_LINE('{"_sentinel": true}');
    DBMS_OUTPUT.PUT_LINE('],');

    -- ===================================================================
    -- 11. Global Stats (MySQL-compatible field names)
    -- Maps Oracle V$SYSSTAT to InnoDB-equivalent naming for parser compat
    -- ===================================================================
    DBMS_OUTPUT.PUT_LINE('"global_stats": ');
    FOR rec IN (
        SELECT JSON_OBJECT(
            'innodb_buffer_pool_read_requests' VALUE (
                (SELECT VALUE FROM V$SYSSTAT WHERE NAME = 'db block gets') +
                (SELECT VALUE FROM V$SYSSTAT WHERE NAME = 'consistent gets')
            ),
            'innodb_buffer_pool_reads' VALUE (SELECT VALUE FROM V$SYSSTAT WHERE NAME = 'physical reads'),
            'created_tmp_disk_tables' VALUE 0,
            'created_tmp_tables' VALUE 0,
            'active_connections' VALUE (SELECT COUNT(*) FROM V$SESSION WHERE TYPE = 'USER'),
            'total_transactions' VALUE (SELECT VALUE FROM V$SYSSTAT WHERE NAME = 'user commits')
        ) AS j FROM DUAL
    ) LOOP
        DBMS_OUTPUT.PUT_LINE(rec.j || ',');
    END LOOP;

    -- ===================================================================
    -- 12. I/O Stats (NEW — enables on-prem without CloudWatch)
    -- ===================================================================
    DBMS_OUTPUT.PUT_LINE('"io_stats": ');
    FOR rec IN (
        SELECT JSON_OBJECT(
            'physical_reads_per_sec' VALUE ROUND(
                (SELECT VALUE FROM V$SYSSTAT WHERE NAME = 'physical reads') /
                GREATEST((SELECT ROUND((SYSDATE - STARTUP_TIME) * 86400) FROM V$INSTANCE), 1), 2),
            'physical_writes_per_sec' VALUE ROUND(
                (SELECT VALUE FROM V$SYSSTAT WHERE NAME = 'physical writes') /
                GREATEST((SELECT ROUND((SYSDATE - STARTUP_TIME) * 86400) FROM V$INSTANCE), 1), 2),
            'read_bytes_per_sec' VALUE ROUND(
                (SELECT VALUE FROM V$SYSSTAT WHERE NAME = 'physical read bytes') /
                GREATEST((SELECT ROUND((SYSDATE - STARTUP_TIME) * 86400) FROM V$INSTANCE), 1), 2),
            'write_bytes_per_sec' VALUE ROUND(
                (SELECT VALUE FROM V$SYSSTAT WHERE NAME = 'physical write bytes') /
                GREATEST((SELECT ROUND((SYSDATE - STARTUP_TIME) * 86400) FROM V$INSTANCE), 1), 2),
            'avg_read_latency_ms' VALUE (
                SELECT ROUND(NVL(TIME_WAITED_MICRO / GREATEST(TOTAL_WAITS, 1) / 1000, 0), 3)
                FROM V$SYSTEM_EVENT WHERE EVENT = 'db file sequential read'),
            'avg_write_latency_ms' VALUE (
                SELECT ROUND(NVL(TIME_WAITED_MICRO / GREATEST(TOTAL_WAITS, 1) / 1000, 0), 3)
                FROM V$SYSTEM_EVENT WHERE EVENT = 'log file sync')
        ) AS j FROM DUAL
    ) LOOP
        DBMS_OUTPUT.PUT_LINE(rec.j || ',');
    END LOOP;

    -- ===================================================================
    -- 13. Wait Events (NEW — replaces Performance Insights on-prem)
    -- Top 10 wait events by time waited
    -- The `wait_time_percent` field is emitted as an explicit percent-of-total
    -- so the parser doesn't have to derive it from `avg_wait_ms` (which is a
    -- millisecond value, not a percent — this used to cause validation
    -- errors for any workload with mean wait latency >100ms).
    -- ===================================================================
    DBMS_OUTPUT.PUT_LINE('"wait_events": [');
    FOR rec IN (
        SELECT JSON_OBJECT(
            'event' VALUE EVENT,
            'total_waits' VALUE TOTAL_WAITS,
            'time_waited_ms' VALUE ROUND(TIME_WAITED_MICRO / 1000, 2),
            'avg_wait_ms' VALUE ROUND(TIME_WAITED_MICRO / GREATEST(TOTAL_WAITS, 1) / 1000, 3),
            'wait_time_percent' VALUE ROUND(
                100.0 * TIME_WAITED_MICRO / NULLIF(SUM(TIME_WAITED_MICRO) OVER (), 0), 3),
            'wait_class' VALUE WAIT_CLASS
        ) AS j
        FROM (
            SELECT EVENT, TOTAL_WAITS, TIME_WAITED_MICRO, WAIT_CLASS
            FROM V$SYSTEM_EVENT
            WHERE WAIT_CLASS != 'Idle'
            ORDER BY TIME_WAITED_MICRO DESC
            FETCH FIRST 10 ROWS ONLY
        )
    ) LOOP
        DBMS_OUTPUT.PUT_LINE(rec.j || ',');
    END LOOP;
    DBMS_OUTPUT.PUT_LINE('{"_sentinel": true}');
    DBMS_OUTPUT.PUT_LINE('],');

    -- ===================================================================
    -- 14. OS Stats (NEW — replaces RDS instance metadata on-prem)
    -- ===================================================================
    DBMS_OUTPUT.PUT_LINE('"os_stats": ');
    FOR rec IN (
        SELECT JSON_OBJECT(
            'cpu_count' VALUE (SELECT VALUE FROM V$OSSTAT WHERE STAT_NAME = 'NUM_CPUS'),
            'physical_memory_gb' VALUE ROUND(
                (SELECT VALUE FROM V$OSSTAT WHERE STAT_NAME = 'PHYSICAL_MEMORY_BYTES') / 1024/1024/1024, 2),
            'sga_size_gb' VALUE (SELECT ROUND(SUM(VALUE)/1024/1024/1024, 2) FROM V$SGA),
            'pga_allocated_gb' VALUE (SELECT ROUND(VALUE/1024/1024/1024, 2) FROM V$PGASTAT WHERE NAME = 'total PGA allocated'),
            'db_uptime_hours' VALUE (SELECT ROUND((SYSDATE - STARTUP_TIME) * 24, 1) FROM V$INSTANCE)
        ) AS j FROM DUAL
    ) LOOP
        DBMS_OUTPUT.PUT_LINE(rec.j);
    END LOOP;

    DBMS_OUTPUT.PUT_LINE('}');
END;
/
EXIT;
