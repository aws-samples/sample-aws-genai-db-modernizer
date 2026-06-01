# MySQL Query Templates

**Version:** 1.0
**Date:** January 22, 2026
**Purpose:** SQL query templates for collecting metadata from MySQL databases

---

## Overview

This document provides SQL query templates for the MySQL Collector Agent to collect comprehensive metadata from MySQL databases.

## Database Metadata Queries

### Database Version and Configuration

```sql
-- Get MySQL version
SELECT VERSION() as version;

-- Get database size
SELECT
    table_schema as database_name,
    ROUND(SUM(data_length + index_length) / 1024 / 1024 / 1024, 2) as size_gb
FROM information_schema.tables
WHERE table_schema = DATABASE()
GROUP BY table_schema;

-- Get database configuration
SHOW VARIABLES;
```

## Schema Collection Queries

### Tables Metadata

```sql
-- Get all tables with metadata
SELECT
    table_name,
    table_rows as row_count,
    data_length as data_size_bytes,
    index_length as index_size_bytes,
    ROUND((data_length + index_length) / 1024 / 1024, 2) as total_size_mb,
    engine,
    table_collation,
    create_time,
    update_time,
    table_comment
FROM information_schema.tables
WHERE table_schema = DATABASE()
AND table_type = 'BASE TABLE'
ORDER BY table_name;
```

### Columns Metadata

```sql
-- Get columns for a specific table
SELECT
    column_name,
    ordinal_position,
    column_default,
    is_nullable,
    data_type,
    character_maximum_length as max_length,
    numeric_precision,
    numeric_scale,
    column_type,
    column_key,
    extra,
    column_comment
FROM information_schema.columns
WHERE table_schema = DATABASE()
AND table_name = ?
ORDER BY ordinal_position;
```

### Indexes

```sql
-- Get indexes for a specific table
SELECT
    index_name,
    column_name,
    seq_in_index,
    non_unique,
    index_type,
    cardinality
FROM information_schema.statistics
WHERE table_schema = DATABASE()
AND table_name = ?
ORDER BY index_name, seq_in_index;
```

### Foreign Keys

```sql
-- Get foreign key constraints
SELECT
    constraint_name,
    table_name,
    column_name,
    referenced_table_name,
    referenced_column_name
FROM information_schema.key_column_usage
WHERE table_schema = DATABASE()
AND referenced_table_name IS NOT NULL
ORDER BY table_name, constraint_name;
```

## Query Pattern Collection

### Performance Schema Queries

```sql
-- Enable performance schema (if not already enabled)
-- Note: This requires SUPER privilege and may require restart
-- SET GLOBAL performance_schema = ON;

-- Get query statistics from performance_schema
SELECT
    DIGEST_TEXT as query_text,
    SCHEMA_NAME as database_name,
    COUNT_STAR as execution_count,
    SUM_TIMER_WAIT / 1000000000000 as total_time_seconds,
    AVG_TIMER_WAIT / 1000000000000 as avg_time_seconds,
    MIN_TIMER_WAIT / 1000000000000 as min_time_seconds,
    MAX_TIMER_WAIT / 1000000000000 as max_time_seconds,
    SUM_ROWS_EXAMINED as total_rows_examined,
    SUM_ROWS_SENT as total_rows_sent,
    SUM_ROWS_AFFECTED as total_rows_affected,
    SUM_NO_INDEX_USED as queries_without_index,
    SUM_NO_GOOD_INDEX_USED as queries_with_bad_index,
    FIRST_SEEN,
    LAST_SEEN
FROM performance_schema.events_statements_summary_by_digest
WHERE SCHEMA_NAME = DATABASE()
AND COUNT_STAR >= 10  -- Filter out rarely-run queries
ORDER BY SUM_TIMER_WAIT DESC
LIMIT 1000;
```

### Table I/O Statistics

```sql
-- Get table I/O statistics
SELECT
    OBJECT_SCHEMA as database_name,
    OBJECT_NAME as table_name,
    COUNT_READ,
    COUNT_WRITE,
    COUNT_FETCH,
    COUNT_INSERT,
    COUNT_UPDATE,
    COUNT_DELETE,
    SUM_TIMER_WAIT / 1000000000000 as total_wait_seconds
FROM performance_schema.table_io_waits_summary_by_table
WHERE OBJECT_SCHEMA = DATABASE()
ORDER BY SUM_TIMER_WAIT DESC;
```

### Index Usage Statistics

```sql
-- Get index usage statistics
SELECT
    OBJECT_SCHEMA as database_name,
    OBJECT_NAME as table_name,
    INDEX_NAME as index_name,
    COUNT_STAR as access_count,
    COUNT_READ,
    COUNT_WRITE,
    COUNT_FETCH,
    COUNT_INSERT,
    COUNT_UPDATE,
    COUNT_DELETE
FROM performance_schema.table_io_waits_summary_by_index_usage
WHERE OBJECT_SCHEMA = DATABASE()
ORDER BY COUNT_STAR DESC;
```

## Views, Procedures, and Triggers

### Views

```sql
-- Get view definitions
SELECT
    table_name as view_name,
    view_definition,
    check_option,
    is_updatable,
    definer,
    security_type
FROM information_schema.views
WHERE table_schema = DATABASE()
ORDER BY table_name;
```

### Stored Procedures and Functions

```sql
-- Get stored procedures and functions
SELECT
    routine_name,
    routine_type,
    data_type as return_type,
    routine_definition,
    is_deterministic,
    sql_data_access,
    security_type,
    definer,
    created,
    last_altered
FROM information_schema.routines
WHERE routine_schema = DATABASE()
ORDER BY routine_type, routine_name;
```

### Triggers

```sql
-- Get triggers
SELECT
    trigger_name,
    event_manipulation as event_type,
    event_object_table as table_name,
    action_timing as timing,
    action_statement as definition,
    created
FROM information_schema.triggers
WHERE trigger_schema = DATABASE()
ORDER BY event_object_table, trigger_name;
```

## Performance Metrics

### Connection Statistics

```sql
-- Get current connections
SHOW PROCESSLIST;

-- Get connection statistics
SHOW STATUS LIKE 'Threads_%';
SHOW STATUS LIKE 'Connections';
SHOW STATUS LIKE 'Max_used_connections';
```

### Query Performance

```sql
-- Get slow query statistics
SHOW STATUS LIKE 'Slow_queries';

-- Get query cache statistics
SHOW STATUS LIKE 'Qcache%';
```

## Sample Data Collection

### Sample Rows

```sql
-- Get sample rows from a table (with PII anonymization consideration)
SELECT *
FROM {table_name}
LIMIT 1000;
```

## Notes

- All queries use parameterized placeholders (?) where table names are dynamic
- Performance schema queries require `performance_schema` to be enabled
- Some queries require specific privileges (SELECT, SHOW VIEW, etc.)
- Sample data collection should respect PII anonymization settings
- Query execution counts and times are cumulative since last server restart

## Query Execution Order

1. Database metadata (version, size, configuration)
2. Schema collection (tables, columns, indexes, foreign keys)
3. Query patterns (performance_schema)
4. Views, procedures, triggers
5. Performance metrics
6. Sample data (if enabled)

---

**Status:** Template - To be populated with complete queries
**Next Steps:** Add detailed query templates based on collection matrix requirements
