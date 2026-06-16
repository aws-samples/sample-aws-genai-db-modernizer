-- ==========================================================================
-- Database Modernizer — SQL Server Offline Collection Script
-- ==========================================================================
-- Run this against your SQL Server database to produce a JSON snapshot that
-- the collector agent can ingest in "offline" mode. No IAM permissions, no
-- EC2 automation instance, no AWS API calls required.
--
-- Requirements:
--   SQL Server 2016+ (uses FOR JSON PATH)
--   User needs SELECT on sys.* DMVs/catalog views and VIEW SERVER STATE
--   for the query patterns section. Without VIEW SERVER STATE the
--   queries section will be empty but everything else still works.
--
-- Usage:
--   sqlcmd -S <host>,<port> -U <user> -P <pass> -d <database> \
--          -C -y 0 -i collect-sqlserver.sql > collection.json
--
--   -y 0:  unlimited column width (NVARCHAR(MAX) results)
--   -C:    trust server certificate (for RDS)
--
--   sqlcmd has mutual-exclusion conflicts: -h cannot be used with -y 0,
--   and -W cannot be used with -y 0. So we use -y 0 alone and let the
--   collector's offline_parser strip the leading non-JSON line.
-- ==========================================================================

SET NOCOUNT ON;
SET ANSI_WARNINGS OFF;  -- suppress warnings about NULL in aggregates

DECLARE @metadata NVARCHAR(MAX);
DECLARE @tables NVARCHAR(MAX);
DECLARE @columns NVARCHAR(MAX);
DECLARE @indexes NVARCHAR(MAX);
DECLARE @foreign_keys NVARCHAR(MAX);
DECLARE @primary_keys NVARCHAR(MAX);
DECLARE @views NVARCHAR(MAX);
DECLARE @procedures NVARCHAR(MAX);
DECLARE @triggers NVARCHAR(MAX);
DECLARE @queries NVARCHAR(MAX);
DECLARE @global_stats NVARCHAR(MAX);

-- Schema filter shared across sections
DECLARE @ExcludeSystemSchemas NVARCHAR(200) =
  'AND s.name NOT IN (''sys'', ''INFORMATION_SCHEMA'', ''guest'')
   AND s.name NOT LIKE ''db[_]%'' ESCAPE ''[''';

-- ===================== 1. Query Patterns (run FIRST) =====================
-- This section runs FIRST to capture user queries from sys.dm_exec_query_stats
-- BEFORE the schema-collection queries (sys.tables, sys.columns, etc.) pollute
-- the plan cache. On SQL Server Express the cache is small and aggressive
-- eviction can drop user workload entries within seconds of being inserted.
SET @queries = (
    SELECT TOP 1000
        CONVERT(VARCHAR(40), qs.query_hash, 1) AS digest,
        REPLACE(REPLACE(REPLACE(
            SUBSTRING(st.text,
                (qs.statement_start_offset / 2) + 1,
                ((CASE qs.statement_end_offset
                    WHEN -1 THEN DATALENGTH(st.text)
                    ELSE qs.statement_end_offset
                    END - qs.statement_start_offset) / 2) + 1),
            CHAR(13), ' '), CHAR(10), ' '), CHAR(9), ' ') AS query_text,
        qs.execution_count,
        ROUND(qs.total_elapsed_time / 1000.0, 3) AS total_time_ms,
        ROUND(qs.total_elapsed_time / 1000.0 / qs.execution_count, 3) AS avg_time_ms,
        ROUND(qs.min_elapsed_time / 1000.0, 3) AS min_time_ms,
        ROUND(qs.max_elapsed_time / 1000.0, 3) AS max_time_ms,
        qs.total_rows AS total_rows_sent,
        qs.total_logical_reads AS total_rows_examined,
        qs.total_rows AS total_rows_affected,
        ROUND(qs.total_worker_time / 1000.0, 3) AS total_cpu_ms,
        ROUND(qs.total_worker_time / 1000.0 / qs.execution_count, 3) AS avg_cpu_time_ms,
        ROUND(qs.total_logical_reads * 1.0 / qs.execution_count, 3) AS avg_logical_reads,
        ROUND(qs.total_physical_reads * 1.0 / qs.execution_count, 3) AS avg_physical_reads,
        CONVERT(VARCHAR(30), qs.creation_time, 121) AS first_seen,
        CONVERT(VARCHAR(30), qs.last_execution_time, 121) AS last_seen
    FROM sys.dm_exec_query_stats qs
    CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st
    WHERE qs.execution_count >= 10
      AND (st.dbid = DB_ID() OR st.dbid IS NULL OR st.dbid = 0)
      AND st.text NOT LIKE '%rdsadmin%'
      AND st.text NOT LIKE '%rds_configuration%'
      AND st.text NOT LIKE '%rds_database_tracking%'
      AND st.text NOT LIKE '%rds_is_db_writable%'
      AND st.text NOT LIKE '%rds_component_version%'
      AND st.text NOT LIKE '%dm_os_sys_info%'
      AND st.text NOT LIKE '%dm_exec_query_stats%'
      AND st.text NOT LIKE '%dm_exec_sql_text%'
      AND st.text NOT LIKE '%information_schema%'
      AND st.text NOT LIKE '%@@VERSION%'
      AND st.text NOT LIKE '%msdb.%'
      AND st.text NOT LIKE '%msdb..%'
      AND st.text NOT LIKE '%sys.server_role_members%'
      AND st.text NOT LIKE '%sys.server_principals%'
      AND st.text NOT LIKE '%sys.server_triggers%'
      AND st.text NOT LIKE '%sys.configurations%'
      AND st.text NOT LIKE '%sys.databases%'
      AND st.text NOT LIKE '%sys.dm_%'
    ORDER BY qs.total_elapsed_time DESC
    FOR JSON PATH
);

-- ===================== 2. Metadata =====================
SET @metadata = (
    SELECT
        DB_NAME() AS database_name,
        'sqlserver' AS engine,
        CONVERT(NVARCHAR(MAX), @@VERSION) AS version_full,
        CONVERT(NVARCHAR(50), SERVERPROPERTY('ProductVersion')) AS product_version,
        CONVERT(NVARCHAR(50), SERVERPROPERTY('Edition')) AS edition,
        CONVERT(NVARCHAR(50), SERVERPROPERTY('ProductLevel')) AS product_level,
        CONVERT(BIGINT, (
            SELECT SUM(size) * 8.0 / 1024 / 1024 / 1024
            FROM sys.master_files
            WHERE database_id = DB_ID()
        )) AS database_size_gb,
        CONVERT(NVARCHAR(30), GETDATE(), 121) AS collected_at
    FOR JSON PATH, WITHOUT_ARRAY_WRAPPER
);

-- ===================== 3. Tables =====================
SET @tables = (
    SELECT
        s.name AS schema_name,
        t.name AS table_name,
        s.name + '.' + t.name AS table_id,
        ISNULL(p.rows, 0) AS row_count,
        CAST(SUM(a.total_pages) * 8.0 / 1024 AS DECIMAL(15, 2)) AS data_size_mb,
        CAST(SUM(CASE WHEN i.index_id > 1 THEN a.total_pages ELSE 0 END)
             * 8.0 / 1024 AS DECIMAL(15, 2)) AS index_size_mb
    FROM sys.tables t
    INNER JOIN sys.schemas s ON t.schema_id = s.schema_id
    INNER JOIN sys.partitions p ON t.object_id = p.object_id AND p.index_id IN (0, 1)
    INNER JOIN sys.allocation_units a ON p.partition_id = a.container_id
    LEFT JOIN sys.indexes i ON t.object_id = i.object_id AND p.index_id = i.index_id
    WHERE s.name NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest')
      AND s.name NOT LIKE 'db[_]%' ESCAPE '['
    GROUP BY s.name, t.name, p.rows
    ORDER BY s.name, t.name
    FOR JSON PATH
);

-- ===================== 4. Columns =====================
SET @columns = (
    SELECT
        t.name AS table_name,
        s.name AS schema_name,
        c.name AS column_name,
        c.column_id AS ordinal_position,
        ty.name AS data_type,
        ty.name AS udt_name,
        CASE
            WHEN c.max_length = -1 THEN NULL
            WHEN ty.name IN ('nvarchar', 'nchar', 'ntext') THEN c.max_length / 2
            ELSE c.max_length
        END AS max_length,
        CASE WHEN c.is_nullable = 1 THEN 'YES' ELSE 'NO' END AS is_nullable,
        CASE WHEN c.is_identity = 1 THEN 'YES' ELSE 'NO' END AS is_identity,
        dc.definition AS column_default
    FROM sys.columns c
    INNER JOIN sys.tables t ON c.object_id = t.object_id
    INNER JOIN sys.schemas s ON t.schema_id = s.schema_id
    INNER JOIN sys.types ty ON c.user_type_id = ty.user_type_id
    LEFT JOIN sys.default_constraints dc
        ON dc.parent_object_id = t.object_id AND dc.parent_column_id = c.column_id
    WHERE s.name NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest')
      AND s.name NOT LIKE 'db[_]%' ESCAPE '['
    ORDER BY s.name, t.name, c.column_id
    FOR JSON PATH
);

-- ===================== 5. Indexes =====================
-- offline_parser expects one row per (table, index, column) with non_unique 0/1.
-- index_type is pre-normalized to the contract's IndexType enum values
-- (btree/hash/spatial/fulltext/other) — the offline_parser passes it through
-- as-is and Pydantic's Index model rejects unknown values.
SET @indexes = (
    SELECT
        t.name AS table_name,
        s.name AS schema_name,
        i.name AS index_name,
        c.name AS column_name,
        ic.key_ordinal,
        CASE WHEN i.is_unique = 1 THEN 0 ELSE 1 END AS non_unique,
        CASE
            WHEN i.type_desc LIKE '%SPATIAL%' THEN 'spatial'
            WHEN i.type_desc LIKE '%FULLTEXT%' OR i.type_desc LIKE '%FULL-TEXT%' THEN 'fulltext'
            WHEN i.type_desc LIKE '%HASH%' THEN 'hash'
            WHEN i.type_desc LIKE '%CLUSTERED%' OR i.type_desc LIKE '%NONCLUSTERED%' THEN 'btree'
            ELSE 'other'
        END AS index_type
    FROM sys.indexes i
    INNER JOIN sys.tables t ON i.object_id = t.object_id
    INNER JOIN sys.schemas s ON t.schema_id = s.schema_id
    INNER JOIN sys.index_columns ic
        ON i.object_id = ic.object_id AND i.index_id = ic.index_id
    INNER JOIN sys.columns c
        ON ic.object_id = c.object_id AND ic.column_id = c.column_id
    WHERE i.name IS NOT NULL
      AND i.type > 0
      AND s.name NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest')
      AND s.name NOT LIKE 'db[_]%' ESCAPE '['
    ORDER BY s.name, t.name, i.name, ic.key_ordinal
    FOR JSON PATH
);

-- ===================== 6. Foreign Keys =====================
SET @foreign_keys = (
    SELECT
        t.name AS table_name,
        s.name AS schema_name,
        fk.name AS constraint_name,
        col.name AS column_name,
        rt.name AS referenced_table_name,
        rcol.name AS referenced_column_name,
        REPLACE(fk.update_referential_action_desc, '_', ' ') AS on_update,
        REPLACE(fk.delete_referential_action_desc, '_', ' ') AS on_delete,
        fkc.constraint_column_id
    FROM sys.foreign_keys fk
    INNER JOIN sys.tables t ON fk.parent_object_id = t.object_id
    INNER JOIN sys.schemas s ON t.schema_id = s.schema_id
    INNER JOIN sys.foreign_key_columns fkc
        ON fkc.constraint_object_id = fk.object_id
    INNER JOIN sys.columns col
        ON col.object_id = fkc.parent_object_id AND col.column_id = fkc.parent_column_id
    INNER JOIN sys.tables rt ON fk.referenced_object_id = rt.object_id
    INNER JOIN sys.columns rcol
        ON rcol.object_id = fkc.referenced_object_id
        AND rcol.column_id = fkc.referenced_column_id
    WHERE s.name NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest')
      AND s.name NOT LIKE 'db[_]%' ESCAPE '['
    ORDER BY s.name, t.name, fk.name, fkc.constraint_column_id
    FOR JSON PATH
);

-- ===================== 7. Primary Keys =====================
SET @primary_keys = (
    SELECT
        t.name AS table_name,
        s.name AS schema_name,
        c.name AS column_name,
        ic.key_ordinal AS ordinal_position
    FROM sys.indexes i
    INNER JOIN sys.tables t ON i.object_id = t.object_id
    INNER JOIN sys.schemas s ON t.schema_id = s.schema_id
    INNER JOIN sys.index_columns ic
        ON i.object_id = ic.object_id AND i.index_id = ic.index_id
    INNER JOIN sys.columns c
        ON ic.object_id = c.object_id AND ic.column_id = c.column_id
    WHERE i.is_primary_key = 1
      AND s.name NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest')
      AND s.name NOT LIKE 'db[_]%' ESCAPE '['
    ORDER BY s.name, t.name, ic.key_ordinal
    FOR JSON PATH
);

-- ===================== 8. Views =====================
SET @views = (
    SELECT
        v.name AS view_name,
        s.name AS schema_name,
        m.definition,
        p.name AS owner
    FROM sys.views v
    INNER JOIN sys.schemas s ON v.schema_id = s.schema_id
    LEFT JOIN sys.sql_modules m ON v.object_id = m.object_id
    LEFT JOIN sys.database_principals p ON v.principal_id = p.principal_id
    WHERE s.name NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest')
      AND s.name NOT LIKE 'db[_]%' ESCAPE '['
    ORDER BY s.name, v.name
    FOR JSON PATH
);

-- ===================== 9. Procedures =====================
SET @procedures = (
    SELECT
        o.name AS routine_name,
        s.name AS schema_name,
        CASE o.type
            WHEN 'P' THEN 'PROCEDURE'
            WHEN 'FN' THEN 'FUNCTION'
            WHEN 'IF' THEN 'INLINE_TABLE_FUNCTION'
            WHEN 'TF' THEN 'TABLE_FUNCTION'
            ELSE 'OTHER'
        END AS routine_type,
        'TSQL' AS language
    FROM sys.objects o
    INNER JOIN sys.schemas s ON o.schema_id = s.schema_id
    WHERE o.type IN ('P', 'FN', 'IF', 'TF')
      AND s.name NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest')
      AND s.name NOT LIKE 'db[_]%' ESCAPE '['
    ORDER BY routine_type, s.name, o.name
    FOR JSON PATH
);

-- ===================== 10. Triggers =====================
SET @triggers = (
    SELECT
        tr.name AS trigger_name,
        t.name AS table_name,
        s.name AS schema_name,
        CASE WHEN OBJECTPROPERTY(tr.object_id, 'ExecIsInsteadOfTrigger') = 1
             THEN 'INSTEAD_OF'
             ELSE 'AFTER'
        END AS timing,
        CASE
            WHEN OBJECTPROPERTY(tr.object_id, 'ExecIsInsertTrigger') = 1 THEN 'INSERT'
            WHEN OBJECTPROPERTY(tr.object_id, 'ExecIsUpdateTrigger') = 1 THEN 'UPDATE'
            WHEN OBJECTPROPERTY(tr.object_id, 'ExecIsDeleteTrigger') = 1 THEN 'DELETE'
            ELSE 'OTHER'
        END AS event_type
    FROM sys.triggers tr
    INNER JOIN sys.tables t ON tr.parent_id = t.object_id
    INNER JOIN sys.schemas s ON t.schema_id = s.schema_id
    WHERE tr.is_ms_shipped = 0
      AND s.name NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest')
      AND s.name NOT LIKE 'db[_]%' ESCAPE '['
    ORDER BY s.name, t.name, tr.name
    FOR JSON PATH
);

-- ===================== 11. Global Stats =====================
-- Map SQL Server's perfmon counters to the MySQL-flavored keys the
-- offline_parser expects. The cache_hit_ratio_pct will be re-derived
-- by the parser from buffer_pool_read_requests vs buffer_pool_reads.
SET @global_stats = (
    SELECT
        ISNULL((
            SELECT cntr_value FROM sys.dm_os_performance_counters
            WHERE object_name LIKE '%Buffer Manager%'
              AND counter_name = 'Buffer cache hit ratio base'
        ), 0) AS innodb_buffer_pool_read_requests,
        ISNULL((
            SELECT cntr_value FROM sys.dm_os_performance_counters
            WHERE object_name LIKE '%Buffer Manager%'
              AND counter_name = 'Page reads/sec'
        ), 0) AS innodb_buffer_pool_reads,
        0 AS created_tmp_disk_tables,
        0 AS created_tmp_tables,
        ISNULL((SELECT COUNT(*) FROM sys.dm_exec_connections), 0) AS threads_connected,
        ISNULL((
            SELECT cntr_value FROM sys.dm_os_performance_counters
            WHERE object_name LIKE '%SQL Statistics%'
              AND counter_name = 'Batch Requests/sec'
        ), 0) AS questions
    FOR JSON PATH, WITHOUT_ARRAY_WRAPPER
);

-- ===================== 12. Assemble Final JSON =====================
SELECT
    '{"collection_version":"1.0",' +
    '"collected_at":"' + CONVERT(VARCHAR(30), GETUTCDATE(), 127) + 'Z",' +
    '"metadata":' + ISNULL(@metadata, 'null') + ',' +
    '"tables":' + ISNULL(@tables, '[]') + ',' +
    '"columns":' + ISNULL(@columns, '[]') + ',' +
    '"indexes":' + ISNULL(@indexes, '[]') + ',' +
    '"foreign_keys":' + ISNULL(@foreign_keys, '[]') + ',' +
    '"primary_keys":' + ISNULL(@primary_keys, '[]') + ',' +
    '"views":' + ISNULL(@views, '[]') + ',' +
    '"procedures":' + ISNULL(@procedures, '[]') + ',' +
    '"triggers":' + ISNULL(@triggers, '[]') + ',' +
    '"queries":' + ISNULL(@queries, '[]') + ',' +
    '"global_stats":' + ISNULL(@global_stats, '{}') +
    '}' AS collection_output;
