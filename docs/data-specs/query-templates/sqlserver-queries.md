# SQL Server Query Templates

**Version:** 1.0
**Date:** January 22, 2026
**Status:** PLANNED — SQL Server collector is not yet implemented
**Purpose:** SQL query templates for collecting metadata from SQL Server databases

---

## Overview

This document provides SQL query templates for the SQL Server Collector Agent to collect comprehensive metadata from SQL Server databases.

## Database Metadata Queries

### Database Version and Configuration

```sql
-- Get SQL Server version
SELECT @@VERSION as version;

-- Get database size
SELECT
    DB_NAME() as database_name,
    SUM(size * 8.0 / 1024 / 1024) as size_gb
FROM sys.database_files;

-- Get database configuration
EXEC sp_configure;
```

## Schema Collection Queries

### Tables Metadata

```sql
-- Get all tables with metadata
SELECT
    s.name as schema_name,
    t.name as table_name,
    p.rows as row_count,
    SUM(a.total_pages) * 8 / 1024.0 as total_size_mb,
    SUM(a.used_pages) * 8 / 1024.0 as data_size_mb,
    (SUM(a.total_pages) - SUM(a.used_pages)) * 8 / 1024.0 as unused_size_mb,
    t.create_date,
    t.modify_date
FROM sys.tables t
INNER JOIN sys.schemas s ON t.schema_id = s.schema_id
INNER JOIN sys.indexes i ON t.object_id = i.object_id
INNER JOIN sys.partitions p ON i.object_id = p.object_id AND i.index_id = p.index_id
INNER JOIN sys.allocation_units a ON p.partition_id = a.container_id
WHERE t.is_ms_shipped = 0
AND i.index_id <= 1
GROUP BY s.name, t.name, p.rows, t.create_date, t.modify_date
ORDER BY s.name, t.name;
```

### Columns Metadata

```sql
-- Get columns for a specific table
SELECT
    c.name as column_name,
    c.column_id as ordinal_position,
    t.name as data_type,
    c.max_length,
    c.precision as numeric_precision,
    c.scale as numeric_scale,
    c.is_nullable,
    c.is_identity,
    dc.definition as default_value,
    ep.value as column_comment
FROM sys.columns c
INNER JOIN sys.types t ON c.user_type_id = t.user_type_id
LEFT JOIN sys.default_constraints dc ON c.default_object_id = dc.object_id
LEFT JOIN sys.extended_properties ep ON ep.major_id = c.object_id
    AND ep.minor_id = c.column_id
    AND ep.name = 'MS_Description'
WHERE c.object_id = OBJECT_ID(@schema_name + '.' + @table_name)
ORDER BY c.column_id;
```

### Indexes

```sql
-- Get indexes for a specific table
SELECT
    i.name as index_name,
    i.type_desc as index_type,
    i.is_unique,
    i.is_primary_key,
    COL_NAME(ic.object_id, ic.column_id) as column_name,
    ic.key_ordinal as column_position,
    ic.is_descending_key,
    ic.is_included_column,
    ps.used_page_count * 8 / 1024.0 as index_size_mb
FROM sys.indexes i
INNER JOIN sys.index_columns ic ON i.object_id = ic.object_id AND i.index_id = ic.index_id
LEFT JOIN sys.dm_db_partition_stats ps ON i.object_id = ps.object_id AND i.index_id = ps.index_id
WHERE i.object_id = OBJECT_ID(@schema_name + '.' + @table_name)
AND i.type > 0
ORDER BY i.name, ic.key_ordinal;
```

### Foreign Keys

```sql
-- Get foreign key constraints
SELECT
    fk.name as constraint_name,
    OBJECT_SCHEMA_NAME(fk.parent_object_id) as table_schema,
    OBJECT_NAME(fk.parent_object_id) as table_name,
    COL_NAME(fkc.parent_object_id, fkc.parent_column_id) as column_name,
    OBJECT_SCHEMA_NAME(fk.referenced_object_id) as referenced_table_schema,
    OBJECT_NAME(fk.referenced_object_id) as referenced_table_name,
    COL_NAME(fkc.referenced_object_id, fkc.referenced_column_id) as referenced_column_name,
    fk.delete_referential_action_desc as on_delete,
    fk.update_referential_action_desc as on_update
FROM sys.foreign_keys fk
INNER JOIN sys.foreign_key_columns fkc ON fk.object_id = fkc.constraint_object_id
WHERE OBJECT_SCHEMA_NAME(fk.parent_object_id) = @schema_name
ORDER BY table_name, constraint_name;
```

## Query Pattern Collection

### DMV Query Statistics

```sql
-- Get query statistics from sys.dm_exec_query_stats
SELECT TOP 1000
    qs.query_hash,
    SUBSTRING(st.text, (qs.statement_start_offset/2)+1,
        ((CASE qs.statement_end_offset
            WHEN -1 THEN DATALENGTH(st.text)
            ELSE qs.statement_end_offset
        END - qs.statement_start_offset)/2) + 1) as query_text,
    qs.execution_count,
    qs.total_elapsed_time / 1000000.0 as total_time_seconds,
    qs.total_elapsed_time / qs.execution_count / 1000.0 as avg_time_ms,
    qs.min_elapsed_time / 1000.0 as min_time_ms,
    qs.max_elapsed_time / 1000.0 as max_time_ms,
    qs.total_worker_time / 1000000.0 as total_cpu_time_seconds,
    qs.total_worker_time / qs.execution_count / 1000.0 as avg_cpu_time_ms,
    qs.total_logical_reads as total_logical_reads,
    qs.total_logical_reads / qs.execution_count as avg_logical_reads,
    qs.total_physical_reads as total_physical_reads,
    qs.total_physical_reads / qs.execution_count as avg_physical_reads,
    qs.total_logical_writes as total_logical_writes,
    qs.total_rows as total_rows_returned,
    qs.total_rows / qs.execution_count as avg_rows_returned,
    qs.creation_time as first_seen,
    qs.last_execution_time as last_seen
FROM sys.dm_exec_query_stats qs
CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st
WHERE qs.execution_count >= 10  -- Filter out rarely-run queries
AND DB_NAME(st.dbid) = DB_NAME()
ORDER BY qs.total_elapsed_time DESC;
```

### Stored Procedure Statistics

```sql
-- Get stored procedure execution statistics
SELECT
    OBJECT_SCHEMA_NAME(ps.object_id) as schema_name,
    OBJECT_NAME(ps.object_id) as procedure_name,
    ps.execution_count,
    ps.total_elapsed_time / 1000000.0 as total_time_seconds,
    ps.total_elapsed_time / ps.execution_count / 1000.0 as avg_time_ms,
    ps.min_elapsed_time / 1000.0 as min_time_ms,
    ps.max_elapsed_time / 1000.0 as max_time_ms,
    ps.total_worker_time / 1000000.0 as total_cpu_time_seconds,
    ps.total_logical_reads,
    ps.total_logical_writes,
    ps.cached_time as first_cached,
    ps.last_execution_time
FROM sys.dm_exec_procedure_stats ps
WHERE DB_NAME(ps.database_id) = DB_NAME()
ORDER BY ps.total_elapsed_time DESC;
```

### Table I/O Statistics

```sql
-- Get table I/O statistics
SELECT
    OBJECT_SCHEMA_NAME(ddius.object_id) as schema_name,
    OBJECT_NAME(ddius.object_id) as table_name,
    SUM(ddius.user_seeks) as user_seeks,
    SUM(ddius.user_scans) as user_scans,
    SUM(ddius.user_lookups) as user_lookups,
    SUM(ddius.user_updates) as user_updates,
    MAX(ddius.last_user_seek) as last_user_seek,
    MAX(ddius.last_user_scan) as last_user_scan,
    MAX(ddius.last_user_lookup) as last_user_lookup,
    MAX(ddius.last_user_update) as last_user_update
FROM sys.dm_db_index_usage_stats ddius
WHERE ddius.database_id = DB_ID()
AND OBJECTPROPERTY(ddius.object_id, 'IsUserTable') = 1
GROUP BY ddius.object_id
ORDER BY SUM(ddius.user_seeks + ddius.user_scans + ddius.user_lookups) DESC;
```

### Index Usage Statistics

```sql
-- Get index usage statistics
SELECT
    OBJECT_SCHEMA_NAME(ddius.object_id) as schema_name,
    OBJECT_NAME(ddius.object_id) as table_name,
    i.name as index_name,
    i.type_desc as index_type,
    ddius.user_seeks,
    ddius.user_scans,
    ddius.user_lookups,
    ddius.user_updates,
    ddius.last_user_seek,
    ddius.last_user_scan,
    ddius.last_user_lookup,
    ddius.last_user_update
FROM sys.dm_db_index_usage_stats ddius
INNER JOIN sys.indexes i ON ddius.object_id = i.object_id AND ddius.index_id = i.index_id
WHERE ddius.database_id = DB_ID()
AND OBJECTPROPERTY(ddius.object_id, 'IsUserTable') = 1
ORDER BY ddius.user_seeks + ddius.user_scans + ddius.user_lookups DESC;
```

## Views, Procedures, and Triggers

### Views

```sql
-- Get view definitions
SELECT
    s.name as schema_name,
    v.name as view_name,
    m.definition,
    v.create_date,
    v.modify_date
FROM sys.views v
INNER JOIN sys.schemas s ON v.schema_id = s.schema_id
INNER JOIN sys.sql_modules m ON v.object_id = m.object_id
WHERE v.is_ms_shipped = 0
ORDER BY s.name, v.name;
```

### Stored Procedures and Functions

```sql
-- Get stored procedures and functions
SELECT
    s.name as schema_name,
    o.name as routine_name,
    o.type_desc as routine_type,
    m.definition,
    o.create_date,
    o.modify_date
FROM sys.objects o
INNER JOIN sys.schemas s ON o.schema_id = s.schema_id
INNER JOIN sys.sql_modules m ON o.object_id = m.object_id
WHERE o.type IN ('P', 'FN', 'IF', 'TF')
AND o.is_ms_shipped = 0
ORDER BY o.type_desc, s.name, o.name;
```

### Triggers

```sql
-- Get triggers
SELECT
    s.name as schema_name,
    OBJECT_NAME(tr.parent_id) as table_name,
    tr.name as trigger_name,
    tr.type_desc,
    CASE tr.is_instead_of_trigger
        WHEN 1 THEN 'INSTEAD OF'
        ELSE 'AFTER'
    END as timing,
    m.definition,
    tr.is_disabled,
    tr.create_date,
    tr.modify_date
FROM sys.triggers tr
INNER JOIN sys.objects o ON tr.parent_id = o.object_id
INNER JOIN sys.schemas s ON o.schema_id = s.schema_id
INNER JOIN sys.sql_modules m ON tr.object_id = m.object_id
WHERE tr.parent_class = 1
ORDER BY s.name, OBJECT_NAME(tr.parent_id), tr.name;
```

## Performance Metrics

### Connection Statistics

```sql
-- Get current connections
SELECT
    session_id,
    login_name,
    host_name,
    program_name,
    status,
    cpu_time,
    memory_usage,
    total_elapsed_time,
    last_request_start_time,
    last_request_end_time
FROM sys.dm_exec_sessions
WHERE database_id = DB_ID()
AND is_user_process = 1;

-- Get connection statistics
SELECT
    DB_NAME(database_id) as database_name,
    COUNT(*) as connection_count
FROM sys.dm_exec_sessions
WHERE database_id = DB_ID()
GROUP BY database_id;
```

### Wait Statistics

```sql
-- Get wait statistics
SELECT TOP 20
    wait_type,
    wait_time_ms / 1000.0 as wait_time_seconds,
    waiting_tasks_count,
    wait_time_ms / waiting_tasks_count as avg_wait_time_ms
FROM sys.dm_os_wait_stats
WHERE waiting_tasks_count > 0
ORDER BY wait_time_ms DESC;
```

## Sample Data Collection

### Sample Rows

```sql
-- Get sample rows from a table (with PII anonymization consideration)
SELECT TOP 1000 *
FROM [{schema_name}].[{table_name}];
```

## Notes

- All queries use parameterized placeholders (@schema_name, @table_name) where values are dynamic
- DMV queries require VIEW SERVER STATE permission
- Some queries require specific privileges (SELECT, VIEW DEFINITION, etc.)
- Sample data collection should respect PII anonymization settings
- DMV statistics are cumulative since last server restart

## Query Execution Order

1. Database metadata (version, size, configuration)
2. Schema collection (tables, columns, indexes, foreign keys)
3. Query patterns (DMVs)
4. Views, procedures, triggers
5. Performance metrics
6. Sample data (if enabled)

---

**Status:** Template - To be populated with complete queries
**Next Steps:** Add detailed query templates based on collection matrix requirements
