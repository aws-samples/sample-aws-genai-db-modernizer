-- ==========================================================================
-- Database Modernizer — Oracle Offline Collection Script
-- ==========================================================================
-- Run this against your Oracle database (12c+) to produce a JSON snapshot
-- that the collector agent can ingest in "offline" mode. No IAM permissions,
-- no EC2 automation instance, no AWS API calls required.
--
-- Requirements:
--   Oracle 12c+ (uses JSON_OBJECT/JSON_ARRAYAGG)
--   User needs SELECT on V$SQL, ALL_* catalog views, DBA_SEGMENTS
--   Grant SELECT_CATALOG_ROLE for full visibility, or minimum:
--     GRANT SELECT ON V$SQL TO <user>;
--     GRANT SELECT ON V$SESSION TO <user>;
--     GRANT SELECT ON V$SYSSTAT TO <user>;
--     GRANT SELECT ON DBA_SEGMENTS TO <user>;
--
-- Usage:
--   sqlplus -S <user>/<pass>@<host>:<port>/<service> @collect-oracle.sql > collection.json
--
--   Or via SSM/script: set the variables below and pipe through sqlplus.
--
-- Output: Single JSON object with sections matching offline_parser expectations.
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
SET SERVEROUTPUT ON SIZE UNLIMITED

-- Use DBMS_OUTPUT for unlimited-length JSON output
DECLARE
    v_owner VARCHAR2(128);
    v_clob  CLOB;
    v_line  VARCHAR2(32767);
    v_pos   INTEGER := 1;
    v_len   INTEGER;
BEGIN
    -- Determine schema to collect
    v_owner := SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA');

    -- Build JSON output into a CLOB using DBMS_OUTPUT
    DBMS_OUTPUT.PUT_LINE('{');

    -- ===================================================================
    -- 1. Query Patterns (run FIRST — same rationale as SQL Server: avoid
    --    cache pollution from catalog queries displacing user workload)
    -- ===================================================================
    DBMS_OUTPUT.PUT_LINE('"queries": [');
    FOR rec IN (
        SELECT JSON_OBJECT(
            'digest' VALUE SQL_ID,
            'query_text' VALUE DBMS_LOB.SUBSTR(SQL_FULLTEXT, 4000, 1),
            'total_count' VALUE EXECUTIONS,
            'total_time_us' VALUE ELAPSED_TIME,
            'total_rows_sent' VALUE ROWS_PROCESSED,
            'total_rows_examined' VALUE BUFFER_GETS,
            'total_cpu_time_us' VALUE CPU_TIME,
            'total_logical_reads' VALUE BUFFER_GETS,
            'total_physical_reads' VALUE DISK_READS,
            'first_seen' VALUE FIRST_LOAD_TIME,
            'last_seen' VALUE TO_CHAR(LAST_ACTIVE_TIME, 'YYYY-MM-DD HH24:MI:SS')
        ) AS json_row
        FROM (
            SELECT SQL_ID, SQL_FULLTEXT, EXECUTIONS, ELAPSED_TIME,
                   ROWS_PROCESSED, BUFFER_GETS, CPU_TIME, DISK_READS,
                   FIRST_LOAD_TIME, LAST_ACTIVE_TIME
            FROM V$SQL
            WHERE EXECUTIONS >= 10
              AND PARSING_SCHEMA_NAME = v_owner
            ORDER BY ELAPSED_TIME DESC
            FETCH FIRST 1000 ROWS ONLY
        )
    ) LOOP
        DBMS_OUTPUT.PUT_LINE(rec.json_row || ',');
    END LOOP;
    -- Remove trailing comma via a sentinel
    DBMS_OUTPUT.PUT_LINE('{"_sentinel": true}');
    DBMS_OUTPUT.PUT_LINE('],');

    -- ===================================================================
    -- 2. Metadata
    -- ===================================================================
    DBMS_OUTPUT.PUT_LINE('"metadata": {');
    FOR rec IN (
        SELECT JSON_OBJECT(
            'version' VALUE BANNER,
            'database_name' VALUE SYS_CONTEXT('USERENV', 'DB_NAME'),
            'current_schema' VALUE v_owner
        ) AS j FROM V$VERSION WHERE ROWNUM = 1
    ) LOOP
        DBMS_OUTPUT.PUT_LINE(rec.j);
    END LOOP;
    DBMS_OUTPUT.PUT_LINE('},');

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
            'column_default' VALUE DATA_DEFAULT,
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
    -- 5. Indexes
    -- ===================================================================
    DBMS_OUTPUT.PUT_LINE('"indexes": [');
    FOR rec IN (
        SELECT JSON_OBJECT(
            'table_name' VALUE LOWER(i.TABLE_NAME),
            'index_name' VALUE LOWER(i.INDEX_NAME),
            'column_name' VALUE LOWER(ic.COLUMN_NAME),
            'column_position' VALUE ic.COLUMN_POSITION,
            'uniqueness' VALUE i.UNIQUENESS,
            'index_type' VALUE i.INDEX_TYPE,
            'is_primary' VALUE CASE WHEN c.CONSTRAINT_TYPE = 'P' THEN 'YES' ELSE 'NO' END
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
    -- 6. Foreign Keys
    -- ===================================================================
    DBMS_OUTPUT.PUT_LINE('"foreign_keys": [');
    FOR rec IN (
        SELECT JSON_OBJECT(
            'table_name' VALUE LOWER(a.TABLE_NAME),
            'constraint_name' VALUE LOWER(a.CONSTRAINT_NAME),
            'column_name' VALUE LOWER(ac.COLUMN_NAME),
            'position' VALUE ac.POSITION,
            'referenced_table' VALUE LOWER(rc.TABLE_NAME),
            'referenced_column' VALUE LOWER(rcc.COLUMN_NAME),
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
    -- 11. Global Stats
    -- ===================================================================
    DBMS_OUTPUT.PUT_LINE('"global_stats": ');
    FOR rec IN (
        SELECT JSON_OBJECT(
            'active_connections' VALUE (SELECT COUNT(*) FROM V$SESSION WHERE TYPE = 'USER'),
            'db_block_gets' VALUE (SELECT VALUE FROM V$SYSSTAT WHERE NAME = 'db block gets'),
            'consistent_gets' VALUE (SELECT VALUE FROM V$SYSSTAT WHERE NAME = 'consistent gets'),
            'physical_reads' VALUE (SELECT VALUE FROM V$SYSSTAT WHERE NAME = 'physical reads'),
            'user_commits' VALUE (SELECT VALUE FROM V$SYSSTAT WHERE NAME = 'user commits')
        ) AS j FROM DUAL
    ) LOOP
        DBMS_OUTPUT.PUT_LINE(rec.j);
    END LOOP;

    DBMS_OUTPUT.PUT_LINE('}');
END;
/
EXIT;
