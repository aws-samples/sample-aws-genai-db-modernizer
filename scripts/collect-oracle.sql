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

    -- Pre-compute total elapsed for db_load_contribution_percent
    SELECT NVL(SUM(ELAPSED_TIME), 1) INTO v_total_elapsed
    FROM V$SQL
    WHERE EXECUTIONS >= 10 AND PARSING_SCHEMA_NAME = v_owner;

    DBMS_OUTPUT.PUT_LINE('{');

    -- ===================================================================
    -- 1. Query Patterns (FIRST — avoid cache pollution from catalog queries)
    -- Field names: MySQL offline_parser convention
    -- ===================================================================
    DBMS_OUTPUT.PUT_LINE('"queries": [');
    FOR rec IN (
        SELECT JSON_OBJECT(
            'digest' VALUE SQL_ID,
            'query_text' VALUE REGEXP_REPLACE(DBMS_LOB.SUBSTR(SQL_FULLTEXT, 2000, 1), '[[:cntrl:]]', ' '),
            'execution_count' VALUE EXECUTIONS,
            'total_time_ms' VALUE ROUND(ELAPSED_TIME / 1000, 3),
            'avg_time_ms' VALUE ROUND(ELAPSED_TIME / GREATEST(EXECUTIONS, 1) / 1000, 3),
            'min_time_ms' VALUE 0,
            'max_time_ms' VALUE 0,
            'total_rows_sent' VALUE ROWS_PROCESSED,
            'total_rows_examined' VALUE BUFFER_GETS,
            'total_rows_affected' VALUE 0,
            'lock_time_ms' VALUE ROUND(APPLICATION_WAIT_TIME / 1000, 3),
            'total_cpu_time_ms' VALUE ROUND(CPU_TIME / 1000, 3),
            'total_logical_reads' VALUE BUFFER_GETS,
            'total_physical_reads' VALUE DISK_READS,
            'db_load_contribution_percent' VALUE ROUND(ELAPSED_TIME / v_total_elapsed * 100, 2),
            'first_seen' VALUE REPLACE(FIRST_LOAD_TIME, '/', ' '),
            'last_seen' VALUE TO_CHAR(LAST_ACTIVE_TIME, 'YYYY-MM-DD HH24:MI:SS')
            RETURNING CLOB
        ) AS json_row
        FROM (
            SELECT SQL_ID, SQL_FULLTEXT, EXECUTIONS, ELAPSED_TIME,
                   ROWS_PROCESSED, BUFFER_GETS, CPU_TIME, DISK_READS,
                   APPLICATION_WAIT_TIME, FIRST_LOAD_TIME, LAST_ACTIVE_TIME
            FROM V$SQL
            WHERE EXECUTIONS >= 10
              AND PARSING_SCHEMA_NAME = v_owner
            ORDER BY ELAPSED_TIME DESC
            FETCH FIRST 1000 ROWS ONLY
        )
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
        ORDER BY t.TABLE_NAME
    ) LOOP
        DBMS_OUTPUT.PUT_LINE(rec.j || ',');
    END LOOP;
    DBMS_OUTPUT.PUT_LINE('{"_sentinel": true}');
    DBMS_OUTPUT.PUT_LINE('],');

    -- ===================================================================
    -- 4. Columns
    -- ===================================================================
    DBMS_OUTPUT.PUT_LINE('"columns": [');
    FOR rec IN (
        SELECT JSON_OBJECT(
            'table_name' VALUE LOWER(TABLE_NAME),
            'column_name' VALUE LOWER(COLUMN_NAME),
            'ordinal_position' VALUE COLUMN_ID,
            'data_type' VALUE LOWER(DATA_TYPE),
            'max_length' VALUE DATA_LENGTH,
            'char_used' VALUE CHAR_USED,
            'data_precision' VALUE DATA_PRECISION,
            'data_scale' VALUE DATA_SCALE,
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
                WHEN i.INDEX_TYPE LIKE '%FUNCTION%' THEN 'functional'
                WHEN i.INDEX_TYPE LIKE '%BITMAP%' THEN 'bitmap'
                ELSE 'btree'
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
    -- ===================================================================
    DBMS_OUTPUT.PUT_LINE('"triggers": [');
    FOR rec IN (
        SELECT JSON_OBJECT(
            'trigger_name' VALUE LOWER(TRIGGER_NAME),
            'table_name' VALUE LOWER(TABLE_NAME),
            'schema_name' VALUE LOWER(OWNER),
            'timing' VALUE TRIGGER_TYPE,
            'event_type' VALUE TRIGGERING_EVENT
        ) AS j
        FROM ALL_TRIGGERS WHERE OWNER = v_owner ORDER BY TABLE_NAME, TRIGGER_NAME
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
    -- ===================================================================
    DBMS_OUTPUT.PUT_LINE('"wait_events": [');
    FOR rec IN (
        SELECT JSON_OBJECT(
            'event' VALUE EVENT,
            'total_waits' VALUE TOTAL_WAITS,
            'time_waited_ms' VALUE ROUND(TIME_WAITED_MICRO / 1000, 2),
            'avg_wait_ms' VALUE ROUND(TIME_WAITED_MICRO / GREATEST(TOTAL_WAITS, 1) / 1000, 3),
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
