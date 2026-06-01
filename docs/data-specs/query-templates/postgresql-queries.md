# PostgreSQL Query Templates

**Version:** 1.0
**Date:** January 22, 2026
**Purpose:** SQL query templates for collecting metadata from PostgreSQL databases

---

## Overview

This document provides SQL query templates for the PostgreSQL Collector Agent to collect comprehensive metadata from PostgreSQL databases.

## Database Metadata Queries

### Database Version and Configuration

```sql
-- Get PostgreSQL version
SELECT version();

-- Get database size
SELECT
    pg_database.datname as database_name,
    pg_size_pretty(pg_database_size(pg_database.datname)) as size_pretty,
    pg_database_size(pg_database.datname) / 1024 / 1024 / 1024.0 as size_gb
FROM pg_database
WHERE datname = current_database();

-- Get database configuration
SHOW ALL;
```

## Schema Collection Queries

### Tables Metadata

```sql
-- Get all tables with metadata
SELECT
    schemaname,
    tablename as table_name,
    n_live_tup as row_count,
    pg_total_relation_size(schemaname||'.'||tablename) as total_size_bytes,
    pg_relation_size(schemaname||'.'||tablename) as data_size_bytes,
    pg_indexes_size(schemaname||'.'||tablename) as index_size_bytes,
    pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename)) as total_size_pretty
FROM pg_stat_user_tables
ORDER BY schemaname, tablename;
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
    udt_name,
    is_identity,
    identity_generation
FROM information_schema.columns
WHERE table_schema = $1
AND table_name = $2
ORDER BY ordinal_position;
```

### Indexes

```sql
-- Get indexes for a specific table
SELECT
    i.relname as index_name,
    a.attname as column_name,
    ix.indisunique as is_unique,
    ix.indisprimary as is_primary,
    am.amname as index_type,
    pg_size_pretty(pg_relation_size(i.oid)) as index_size
FROM pg_class t
JOIN pg_index ix ON t.oid = ix.indrelid
JOIN pg_class i ON i.oid = ix.indexrelid
JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(ix.indkey)
JOIN pg_am am ON i.relam = am.oid
WHERE t.relname = $1
AND t.relnamespace = (SELECT oid FROM pg_namespace WHERE nspname = $2)
ORDER BY i.relname, a.attnum;
```

### Foreign Keys

```sql
-- Get foreign key constraints
SELECT
    tc.constraint_name,
    tc.table_schema,
    tc.table_name,
    kcu.column_name,
    ccu.table_schema AS referenced_table_schema,
    ccu.table_name AS referenced_table_name,
    ccu.column_name AS referenced_column_name,
    rc.update_rule as on_update,
    rc.delete_rule as on_delete
FROM information_schema.table_constraints AS tc
JOIN information_schema.key_column_usage AS kcu
    ON tc.constraint_name = kcu.constraint_name
    AND tc.table_schema = kcu.table_schema
JOIN information_schema.constraint_column_usage AS ccu
    ON ccu.constraint_name = tc.constraint_name
    AND ccu.table_schema = tc.table_schema
JOIN information_schema.referential_constraints AS rc
    ON rc.constraint_name = tc.constraint_name
WHERE tc.constraint_type = 'FOREIGN KEY'
AND tc.table_schema = $1
ORDER BY tc.table_name, tc.constraint_name;
```

## Query Pattern Collection

### pg_stat_statements Queries

```sql
-- Enable pg_stat_statements extension (if not already enabled)
-- CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- Get query statistics from pg_stat_statements
SELECT
    queryid,
    query,
    calls,
    total_exec_time / 1000 as total_time_seconds,
    mean_exec_time as avg_time_ms,
    min_exec_time as min_time_ms,
    max_exec_time as max_time_ms,
    stddev_exec_time as stddev_time_ms,
    rows,
    shared_blks_hit,
    shared_blks_read,
    shared_blks_dirtied,
    shared_blks_written,
    local_blks_hit,
    local_blks_read,
    temp_blks_read,
    temp_blks_written,
    blk_read_time,
    blk_write_time,
    CASE
        WHEN (shared_blks_hit + shared_blks_read) > 0
        THEN (shared_blks_hit::float / (shared_blks_hit + shared_blks_read) * 100)
        ELSE 0
    END as cache_hit_ratio_pct
FROM pg_stat_statements
WHERE calls >= 10  -- Filter out rarely-run queries
ORDER BY total_exec_time DESC
LIMIT 1000;
```

### Table I/O Statistics

```sql
-- Get table I/O statistics
SELECT
    schemaname,
    relname as table_name,
    seq_scan,
    seq_tup_read,
    idx_scan,
    idx_tup_fetch,
    n_tup_ins as inserts,
    n_tup_upd as updates,
    n_tup_del as deletes,
    n_tup_hot_upd as hot_updates,
    n_live_tup as live_rows,
    n_dead_tup as dead_rows,
    last_vacuum,
    last_autovacuum,
    last_analyze,
    last_autoanalyze
FROM pg_stat_user_tables
WHERE schemaname = $1
ORDER BY seq_scan + idx_scan DESC;
```

### Index Usage Statistics

```sql
-- Get index usage statistics
SELECT
    schemaname,
    tablename as table_name,
    indexrelname as index_name,
    idx_scan as index_scans,
    idx_tup_read as tuples_read,
    idx_tup_fetch as tuples_fetched,
    pg_size_pretty(pg_relation_size(indexrelid)) as index_size
FROM pg_stat_user_indexes
WHERE schemaname = $1
ORDER BY idx_scan DESC;
```

## Views, Procedures, and Triggers

### Views

```sql
-- Get view definitions
SELECT
    schemaname,
    viewname as view_name,
    definition,
    viewowner as owner
FROM pg_views
WHERE schemaname = $1
ORDER BY viewname;
```

### Stored Procedures and Functions

```sql
-- Get stored procedures and functions
SELECT
    n.nspname as schema_name,
    p.proname as routine_name,
    CASE p.prokind
        WHEN 'f' THEN 'FUNCTION'
        WHEN 'p' THEN 'PROCEDURE'
        WHEN 'a' THEN 'AGGREGATE'
        WHEN 'w' THEN 'WINDOW'
    END as routine_type,
    pg_get_function_result(p.oid) as return_type,
    pg_get_functiondef(p.oid) as definition,
    l.lanname as language
FROM pg_proc p
JOIN pg_namespace n ON p.pronamespace = n.oid
JOIN pg_language l ON p.prolang = l.oid
WHERE n.nspname = $1
ORDER BY routine_type, routine_name;
```

### Triggers

```sql
-- Get triggers
SELECT
    t.tgname as trigger_name,
    c.relname as table_name,
    CASE t.tgtype & 2
        WHEN 2 THEN 'BEFORE'
        ELSE 'AFTER'
    END as timing,
    CASE t.tgtype & 28
        WHEN 4 THEN 'INSERT'
        WHEN 8 THEN 'DELETE'
        WHEN 16 THEN 'UPDATE'
        ELSE 'MULTIPLE'
    END as event_type,
    CASE t.tgtype & 1
        WHEN 1 THEN 'ROW'
        ELSE 'STATEMENT'
    END as for_each,
    pg_get_triggerdef(t.oid) as definition,
    t.tgenabled as is_enabled
FROM pg_trigger t
JOIN pg_class c ON t.tgrelid = c.oid
JOIN pg_namespace n ON c.relnamespace = n.oid
WHERE n.nspname = $1
AND NOT t.tgisinternal
ORDER BY c.relname, t.tgname;
```

## Performance Metrics

### Connection Statistics

```sql
-- Get current connections
SELECT
    datname as database_name,
    usename as username,
    application_name,
    client_addr,
    state,
    query,
    state_change
FROM pg_stat_activity
WHERE datname = current_database();

-- Get connection statistics
SELECT
    numbackends as active_connections,
    xact_commit as transactions_committed,
    xact_rollback as transactions_rolled_back,
    blks_read as blocks_read,
    blks_hit as blocks_hit,
    tup_returned as tuples_returned,
    tup_fetched as tuples_fetched,
    tup_inserted as tuples_inserted,
    tup_updated as tuples_updated,
    tup_deleted as tuples_deleted
FROM pg_stat_database
WHERE datname = current_database();
```

### Query Performance

```sql
-- Get long-running queries
SELECT
    pid,
    usename,
    application_name,
    client_addr,
    state,
    query_start,
    now() - query_start as duration,
    query
FROM pg_stat_activity
WHERE state = 'active'
AND query_start < now() - interval '1 minute'
ORDER BY duration DESC;
```

## Sample Data Collection

### Sample Rows

```sql
-- Get sample rows from a table (with PII anonymization consideration)
SELECT *
FROM {schema_name}.{table_name}
LIMIT 1000;
```

## Notes

- All queries use parameterized placeholders ($1, $2, etc.) where values are dynamic
- pg_stat_statements extension must be enabled for query pattern collection
- Some queries require specific privileges (SELECT, pg_read_all_stats, etc.)
- Sample data collection should respect PII anonymization settings
- Statistics are cumulative since last server restart or stats reset

## Query Execution Order

1. Database metadata (version, size, configuration)
2. Schema collection (tables, columns, indexes, foreign keys)
3. Query patterns (pg_stat_statements)
4. Views, procedures, triggers
5. Performance metrics
6. Sample data (if enabled)

---

**Status:** Template - To be populated with complete queries
**Next Steps:** Add detailed query templates based on collection matrix requirements
