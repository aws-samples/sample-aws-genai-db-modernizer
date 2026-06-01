# Oracle and DB2 Query Collection Templates

## Document Status

**⚠️ PLACEHOLDER DOCUMENT - NOT YET VALIDATED ⚠️**

**Status:** Template/Draft
**Last Updated:** January 22, 2026
**Owner:** Database Modernizer Team
**Priority:** Phase 1 (Post-MySQL/PostgreSQL/SQL Server)

**Purpose:** This document provides SQL query templates for Oracle and DB2 data collection. These queries follow the same pattern as the validated MySQL, PostgreSQL, and SQL Server queries but **require testing and validation** before production use.

---

## Oracle Database Query Templates

### Connection Requirements

- **Minimum Version:** Oracle 11g+ (recommend 12c+ for better performance views)
- **Required Privileges:**
  - `SELECT` on `DBA_TABLES`, `DBA_TAB_COLUMNS`, `DBA_CONSTRAINTS`, `DBA_INDEXES`
  - `SELECT` on `V$SQL`, `V$SQLAREA`, `V$SQL_PLAN_STATISTICS_ALL`
  - `SELECT` on `DBA_SEGMENTS`, `DBA_TAB_STATISTICS`
  - `SELECT` on `DBA_PROCEDURES`, `DBA_TRIGGERS`, `DBA_VIEWS`
  - Alternative: Grant `SELECT_CATALOG_ROLE` or `DBA` role

### Oracle Analysis Queries

```python
_oracle_analysis_queries = {
    'comprehensive_table_analysis': {
        'name': 'Comprehensive Table Analysis',
        'description': 'Complete table statistics including structure, size, and I/O',
        'category': 'information_schema',
        'sql': """SELECT
  t.TABLE_NAME as table_name,
  t.NUM_ROWS as row_count,
  t.AVG_ROW_LEN as avg_row_length_bytes,
  s.BYTES as data_size_bytes,
  ROUND(s.BYTES/1024/1024, 2) as data_size_mb,
  idx_size.INDEX_SIZE_BYTES as index_size_bytes,
  ROUND(idx_size.INDEX_SIZE_BYTES/1024/1024, 2) as index_size_mb,
  ROUND((s.BYTES + NVL(idx_size.INDEX_SIZE_BYTES, 0))/1024/1024, 2) as total_size_mb,
  t.BLOCKS as blocks,
  t.EMPTY_BLOCKS as empty_blocks,
  t.AVG_SPACE as avg_free_space_per_block,
  t.CHAIN_CNT as chained_rows,
  t.SAMPLE_SIZE as sample_size,
  t.LAST_ANALYZED as last_analyzed,
  t.COMPRESSION as compression,
  t.PARTITIONED as is_partitioned,
  -- I/O statistics (requires AWR or Statspack, may need licensing)
  io.PHYSICAL_READS as physical_reads,
  io.PHYSICAL_WRITES as physical_writes,
  io.LOGICAL_READS as logical_reads,
  io.TABLE_SCANS as table_scans
FROM DBA_TABLES t
LEFT JOIN DBA_SEGMENTS s
  ON s.OWNER = t.OWNER AND s.SEGMENT_NAME = t.TABLE_NAME AND s.SEGMENT_TYPE = 'TABLE'
LEFT JOIN (
  SELECT
    TABLE_OWNER,
    TABLE_NAME,
    SUM(BYTES) as INDEX_SIZE_BYTES
  FROM DBA_SEGMENTS
  WHERE SEGMENT_TYPE IN ('INDEX', 'INDEX PARTITION')
  GROUP BY TABLE_OWNER, TABLE_NAME
) idx_size ON idx_size.TABLE_OWNER = t.OWNER AND idx_size.TABLE_NAME = t.TABLE_NAME
LEFT JOIN (
  SELECT
    OBJECT_OWNER,
    OBJECT_NAME,
    SUM(PHYSICAL_READS_TOTAL) as PHYSICAL_READS,
    SUM(PHYSICAL_WRITES_TOTAL) as PHYSICAL_WRITES,
    SUM(LOGICAL_READS_TOTAL) as LOGICAL_READS,
    SUM(DECODE(OPERATION, 'TABLE ACCESS FULL', 1, 0)) as TABLE_SCANS
  FROM V$SEGMENT_STATISTICS
  WHERE STATISTIC_NAME IN ('physical reads', 'physical writes', 'logical reads')
  GROUP BY OBJECT_OWNER, OBJECT_NAME
) io ON io.OBJECT_OWNER = t.OWNER AND io.OBJECT_NAME = t.TABLE_NAME
WHERE t.OWNER = :target_schema
  AND t.TEMPORARY = 'N'
  AND t.NESTED = 'NO'
ORDER BY t.NUM_ROWS DESC NULLS LAST;""",
        'parameters': ['target_schema'],
        'notes': [
            'V$SEGMENT_STATISTICS may require AWR/Diagnostic Pack license',
            'Alternative: Use DBA_TAB_MODIFICATIONS for DML activity',
            'Consider using DBMS_STATS.GATHER_TABLE_STATS before collection'
        ]
    },
    'comprehensive_index_analysis': {
        'name': 'Comprehensive Index Analysis',
        'description': 'Complete index statistics including structure and usage',
        'category': 'information_schema',
        'sql': """SELECT
  i.TABLE_OWNER as table_owner,
  i.TABLE_NAME as table_name,
  i.INDEX_NAME as index_name,
  i.INDEX_TYPE as index_type,
  i.UNIQUENESS as is_unique,
  i.COMPRESSION as compression,
  i.STATUS as status,
  i.NUM_ROWS as num_rows,
  i.DISTINCT_KEYS as distinct_keys,
  i.LEAF_BLOCKS as leaf_blocks,
  i.AVG_LEAF_BLOCKS_PER_KEY as avg_leaf_blocks_per_key,
  i.AVG_DATA_BLOCKS_PER_KEY as avg_data_blocks_per_key,
  i.CLUSTERING_FACTOR as clustering_factor,
  i.BLEVEL as btree_level,
  s.BYTES as index_size_bytes,
  ROUND(s.BYTES/1024/1024, 2) as index_size_mb,
  LISTAGG(ic.COLUMN_NAME, ', ') WITHIN GROUP (ORDER BY ic.COLUMN_POSITION) as columns,
  -- Index usage statistics (requires monitoring)
  iu.TOTAL_ACCESS_COUNT as total_accesses,
  iu.TOTAL_EXEC_COUNT as total_executions,
  iu.BUCKET_0_ACCESS_COUNT as never_used_count,
  iu.LAST_USED as last_used
FROM DBA_INDEXES i
LEFT JOIN DBA_SEGMENTS s
  ON s.OWNER = i.OWNER AND s.SEGMENT_NAME = i.INDEX_NAME AND s.SEGMENT_TYPE LIKE 'INDEX%'
LEFT JOIN DBA_IND_COLUMNS ic
  ON ic.INDEX_OWNER = i.OWNER AND ic.INDEX_NAME = i.INDEX_NAME
LEFT JOIN (
  SELECT
    OBJECT_OWNER,
    OBJECT_NAME,
    SUM(TOTAL_ACCESS_COUNT) as TOTAL_ACCESS_COUNT,
    SUM(TOTAL_EXEC_COUNT) as TOTAL_EXEC_COUNT,
    SUM(BUCKET_0_ACCESS_COUNT) as BUCKET_0_ACCESS_COUNT,
    MAX(LAST_USED) as LAST_USED
  FROM V$INDEX_USAGE_INFO
  GROUP BY OBJECT_OWNER, OBJECT_NAME
) iu ON iu.OBJECT_OWNER = i.OWNER AND iu.OBJECT_NAME = i.INDEX_NAME
WHERE i.TABLE_OWNER = :target_schema
  AND i.INDEX_TYPE NOT IN ('LOB', 'CLUSTER')
GROUP BY
  i.TABLE_OWNER, i.TABLE_NAME, i.INDEX_NAME, i.INDEX_TYPE, i.UNIQUENESS,
  i.COMPRESSION, i.STATUS, i.NUM_ROWS, i.DISTINCT_KEYS, i.LEAF_BLOCKS,
  i.AVG_LEAF_BLOCKS_PER_KEY, i.AVG_DATA_BLOCKS_PER_KEY, i.CLUSTERING_FACTOR,
  i.BLEVEL, s.BYTES, iu.TOTAL_ACCESS_COUNT, iu.TOTAL_EXEC_COUNT,
  iu.BUCKET_0_ACCESS_COUNT, iu.LAST_USED
ORDER BY i.TABLE_NAME, i.INDEX_NAME;""",
        'parameters': ['target_schema'],
        'notes': [
            'Index monitoring must be enabled: ALTER INDEX index_name MONITORING USAGE',
            'V$INDEX_USAGE_INFO available in Oracle 12c+',
            'For Oracle 11g, use DBA_OBJECT_USAGE instead'
        ]
    },
    'column_analysis': {
        'name': 'Column Information Analysis',
        'description': 'Returns all column definitions including data types, nullability, and defaults',
        'category': 'information_schema',
        'sql': """SELECT
  TABLE_NAME as table_name,
  COLUMN_NAME as column_name,
  COLUMN_ID as position,
  DATA_DEFAULT as default_value,
  NULLABLE as nullable,
  DATA_TYPE as data_type,
  DATA_LENGTH as data_length,
  DATA_PRECISION as numeric_precision,
  DATA_SCALE as numeric_scale,
  CHAR_LENGTH as char_max_length,
  VIRTUAL_COLUMN as is_virtual,
  HIDDEN_COLUMN as is_hidden,
  NUM_DISTINCT as distinct_values,
  NUM_NULLS as null_count,
  HISTOGRAM as has_histogram,
  AVG_COL_LEN as avg_column_length,
  LAST_ANALYZED as last_analyzed
FROM DBA_TAB_COLUMNS
WHERE OWNER = :target_schema
ORDER BY TABLE_NAME, COLUMN_ID;""",
        'parameters': ['target_schema'],
        'notes': [
            'NUM_DISTINCT requires statistics to be gathered',
            'Virtual columns (11g+) computed from expressions'
        ]
    },
    'foreign_key_analysis': {
        'name': 'Foreign Key Relationship Analysis',
        'description': 'Returns foreign key relationships with constraint names and table/column mappings',
        'category': 'information_schema',
        'sql': """SELECT
  c.CONSTRAINT_NAME as constraint_name,
  c.TABLE_NAME as child_table,
  cc_child.COLUMN_NAME as child_column,
  r.TABLE_NAME as parent_table,
  cc_parent.COLUMN_NAME as parent_column,
  c.DELETE_RULE as delete_rule,
  c.STATUS as status,
  c.VALIDATED as validated,
  c.DEFERRABLE as deferrable,
  c.DEFERRED as deferred,
  CASE
    WHEN EXISTS (
      SELECT 1 FROM DBA_CONSTRAINTS pc
      WHERE pc.OWNER = r.OWNER
      AND pc.TABLE_NAME = r.TABLE_NAME
      AND pc.CONSTRAINT_NAME = c.R_CONSTRAINT_NAME
      AND pc.CONSTRAINT_TYPE = 'U'
    ) THEN '1:1 or 1:0..1'
    ELSE '1:Many'
  END as estimated_cardinality
FROM DBA_CONSTRAINTS c
JOIN DBA_CONSTRAINTS r
  ON r.OWNER = c.R_OWNER AND r.CONSTRAINT_NAME = c.R_CONSTRAINT_NAME
JOIN DBA_CONS_COLUMNS cc_child
  ON cc_child.OWNER = c.OWNER
  AND cc_child.CONSTRAINT_NAME = c.CONSTRAINT_NAME
JOIN DBA_CONS_COLUMNS cc_parent
  ON cc_parent.OWNER = r.OWNER
  AND cc_parent.CONSTRAINT_NAME = r.CONSTRAINT_NAME
  AND cc_parent.POSITION = cc_child.POSITION
WHERE c.OWNER = :target_schema
  AND c.CONSTRAINT_TYPE = 'R'
ORDER BY c.TABLE_NAME, c.CONSTRAINT_NAME, cc_child.POSITION;""",
        'parameters': ['target_schema'],
        'notes': [
            'Oracle supports ON DELETE CASCADE, ON DELETE SET NULL',
            'No explicit UPDATE_RULE (Oracle does not support ON UPDATE CASCADE)'
        ]
    },
    'query_performance_stats': {
        'name': 'Query Performance Statistics',
        'description': 'Query execution statistics with performance metrics from V$SQL',
        'category': 'performance_schema',
        'sql': """SELECT
  'QUERY' as source_type,
  s.SQL_ID as query_id,
  SUBSTR(s.SQL_FULLTEXT, 1, 2000) as query_pattern,
  NULL as procedure_name,
  s.EXECUTIONS as total_executions,
  ROUND(s.ELAPSED_TIME / NULLIF(s.EXECUTIONS, 0) / 1000, 2) as avg_latency_ms,
  ROUND(s.ELAPSED_TIME / 1000, 2) as total_time_ms,
  ROUND((s.ELAPSED_TIME / NULLIF(s.EXECUTIONS, 0)) / 1000, 2) as avg_elapsed_time_ms,
  ROUND((s.CPU_TIME / NULLIF(s.EXECUTIONS, 0)) / 1000, 2) as avg_cpu_time_ms,
  ROUND(s.ROWS_PROCESSED / NULLIF(s.EXECUTIONS, 0), 2) as avg_rows_returned,
  ROUND((s.DISK_READS / NULLIF(s.EXECUTIONS, 0)), 2) as avg_physical_reads,
  ROUND((s.BUFFER_GETS / NULLIF(s.EXECUTIONS, 0)), 2) as avg_logical_reads,
  ROUND(((s.BUFFER_GETS - s.DISK_READS) / NULLIF(s.BUFFER_GETS, 0) * 100), 2) as cache_hit_ratio_pct,
  s.PARSE_CALLS as parse_calls,
  s.SORTS as sorts,
  s.LOADS as loads,
  s.INVALIDATIONS as invalidations,
  s.FIRST_LOAD_TIME as first_seen,
  s.LAST_ACTIVE_TIME as last_seen,
  s.MODULE as application_module,
  s.ACTION as application_action,
  s.PARSING_SCHEMA_NAME as parsing_schema,
  -- Calculate estimated RPS
  CASE
    WHEN (s.LAST_ACTIVE_TIME - TO_DATE(s.FIRST_LOAD_TIME, 'YYYY-MM-DD/HH24:MI:SS')) > 0
    THEN ROUND(s.EXECUTIONS /
         ((s.LAST_ACTIVE_TIME - TO_DATE(s.FIRST_LOAD_TIME, 'YYYY-MM-DD/HH24:MI:SS')) * 24 * 60 * 60), 2)
    ELSE NULL
  END as estimated_rps
FROM V$SQL s
WHERE s.PARSING_SCHEMA_NAME = :target_schema
  AND s.EXECUTIONS > 0
  -- Filter out recursive SQL
  AND s.COMMAND_TYPE NOT IN (47, 170)  -- PL/SQL, Recursive
  -- Filter out Oracle internal operations
  AND s.SQL_FULLTEXT NOT LIKE '%SYS.%'
  AND s.SQL_FULLTEXT NOT LIKE '%V$%'
  AND s.SQL_FULLTEXT NOT LIKE '%DBA_%'
  AND s.SQL_FULLTEXT NOT LIKE '%ALL_%'
  AND s.SQL_FULLTEXT NOT LIKE '%USER_%'
  AND s.SQL_FULLTEXT NOT LIKE 'BEGIN DBMS_%'
  AND s.SQL_FULLTEXT NOT LIKE '%DBMS_OUTPUT%'
  -- Filter out empty or very short queries
  AND LENGTH(TRIM(s.SQL_FULLTEXT)) > 10

UNION ALL

-- Stored procedures and functions
SELECT
  'PROCEDURE' as source_type,
  NULL as query_id,
  'PROCEDURE: ' || o.OBJECT_NAME as query_pattern,
  o.OBJECT_NAME as procedure_name,
  ps.EXECUTIONS as total_executions,
  ROUND(ps.ELAPSED_TIME / NULLIF(ps.EXECUTIONS, 0) / 1000, 2) as avg_latency_ms,
  ROUND(ps.ELAPSED_TIME / 1000, 2) as total_time_ms,
  ROUND((ps.ELAPSED_TIME / NULLIF(ps.EXECUTIONS, 0)) / 1000, 2) as avg_elapsed_time_ms,
  ROUND((ps.CPU_TIME / NULLIF(ps.EXECUTIONS, 0)) / 1000, 2) as avg_cpu_time_ms,
  NULL as avg_rows_returned,
  ROUND((ps.DISK_READS / NULLIF(ps.EXECUTIONS, 0)), 2) as avg_physical_reads,
  ROUND((ps.BUFFER_GETS / NULLIF(ps.EXECUTIONS, 0)), 2) as avg_logical_reads,
  ROUND(((ps.BUFFER_GETS - ps.DISK_READS) / NULLIF(ps.BUFFER_GETS, 0) * 100), 2) as cache_hit_ratio_pct,
  ps.PARSE_CALLS as parse_calls,
  ps.SORTS as sorts,
  ps.LOADS as loads,
  ps.INVALIDATIONS as invalidations,
  ps.FIRST_LOAD_TIME as first_seen,
  ps.LAST_ACTIVE_TIME as last_seen,
  NULL as application_module,
  NULL as application_action,
  o.OWNER as parsing_schema,
  NULL as estimated_rps
FROM DBA_OBJECTS o
LEFT JOIN V$SQL ps
  ON ps.PROGRAM_ID = o.OBJECT_ID
WHERE o.OWNER = :target_schema
  AND o.OBJECT_TYPE IN ('PROCEDURE', 'FUNCTION', 'PACKAGE', 'PACKAGE BODY')
  AND ps.EXECUTIONS > 0

ORDER BY total_time_ms DESC NULLS LAST;""",
        'parameters': ['target_schema'],
        'notes': [
            'V$SQL shows currently cached SQL statements',
            'For historical data, use AWR views (DBA_HIST_SQLSTAT) - requires Diagnostic Pack license',
            'FIRST_LOAD_TIME is a VARCHAR2, need to convert for date operations',
            'Consider using V$SQLAREA for aggregated stats across child cursors'
        ]
    },
    'stored_procedures_analysis': {
        'name': 'Stored Procedures and Functions Analysis',
        'description': 'Detailed information about stored procedures, functions, and packages',
        'category': 'information_schema',
        'sql': """SELECT
  o.OWNER as schema_name,
  o.OBJECT_NAME as procedure_name,
  o.OBJECT_TYPE as procedure_type,
  o.STATUS as status,
  o.CREATED as created_date,
  o.LAST_DDL_TIME as last_modified,
  s.LINE as line_count,
  DBMS_LOB.GETLENGTH(s.TEXT) as definition_length,
  -- Get parameters
  p.ARGUMENT_NAME as parameter_name,
  p.DATA_TYPE as parameter_type,
  p.IN_OUT as parameter_mode,
  p.POSITION as parameter_position
FROM DBA_OBJECTS o
LEFT JOIN (
  SELECT OWNER, NAME, TYPE, MAX(LINE) as LINE
  FROM DBA_SOURCE
  GROUP BY OWNER, NAME, TYPE
) s ON s.OWNER = o.OWNER AND s.NAME = o.OBJECT_NAME AND s.TYPE = o.OBJECT_TYPE
LEFT JOIN DBA_ARGUMENTS p
  ON p.OWNER = o.OWNER AND p.OBJECT_NAME = o.OBJECT_NAME AND p.PACKAGE_NAME IS NULL
WHERE o.OWNER = :target_schema
  AND o.OBJECT_TYPE IN ('PROCEDURE', 'FUNCTION', 'PACKAGE', 'PACKAGE BODY')
ORDER BY o.OBJECT_NAME, p.POSITION;""",
        'parameters': ['target_schema'],
        'notes': [
            'DBA_SOURCE contains actual PL/SQL code',
            'May need to retrieve full text separately for large procedures',
            'Packages contain multiple procedures/functions'
        ]
    },
    'triggers_analysis': {
        'name': 'Triggers Analysis',
        'description': 'Detailed information about database triggers',
        'category': 'information_schema',
        'sql': """SELECT
  OWNER as schema_name,
  TRIGGER_NAME as trigger_name,
  TRIGGER_TYPE as timing,
  TRIGGERING_EVENT as event_type,
  TABLE_OWNER as table_owner,
  TABLE_NAME as table_name,
  BASE_OBJECT_TYPE as base_object_type,
  COLUMN_NAME as column_name,
  WHEN_CLAUSE as when_clause,
  STATUS as status,
  DESCRIPTION as description,
  ACTION_TYPE as action_type,
  TRIGGER_BODY as definition
FROM DBA_TRIGGERS
WHERE OWNER = :target_schema
  AND BASE_OBJECT_TYPE = 'TABLE'
ORDER BY TABLE_NAME, TRIGGER_NAME;""",
        'parameters': ['target_schema'],
        'notes': [
            'TRIGGER_TYPE shows BEFORE/AFTER/INSTEAD OF and FOR EACH ROW/STATEMENT',
            'TRIGGERING_EVENT shows INSERT, UPDATE, DELETE, or combinations',
            'Oracle triggers can be complex with compound triggers (11g+)'
        ]
    },
    'views_analysis': {
        'name': 'Views Analysis',
        'description': 'Detailed information about database views',
        'category': 'information_schema',
        'sql': """SELECT
  OWNER as schema_name,
  VIEW_NAME as view_name,
  TEXT_LENGTH as definition_length,
  TEXT as definition,
  READ_ONLY as is_read_only,
  -- Get column information
  vc.COLUMN_NAME as column_name,
  vc.DATA_TYPE as data_type,
  vc.NULLABLE as nullable,
  vc.COLUMN_ID as column_position
FROM DBA_VIEWS v
LEFT JOIN DBA_TAB_COLUMNS vc
  ON vc.OWNER = v.OWNER AND vc.TABLE_NAME = v.VIEW_NAME
WHERE v.OWNER = :target_schema
ORDER BY v.VIEW_NAME, vc.COLUMN_ID;""",
        'parameters': ['target_schema'],
        'notes': [
            'TEXT column contains full view definition',
            'Materialized views are separate (DBA_MVIEWS)',
            'Consider checking DBA_DEPENDENCIES for view dependencies'
        ]
    }
}
```

---

## DB2 Database Query Templates

### Connection Requirements

- **Minimum Version:** DB2 10.5+ (recommend DB2 11.5+ for better monitoring)
- **Required Privileges:**
  - `SELECT` on `SYSCAT` catalog views
  - `SELECT` on `SYSSTAT` tables
  - `SELECT` on `SYSPROC.ADMIN_GET_TAB_INFO` table function
  - `EXECUTE` on monitoring table functions
  - Alternative: `DBADM` or `SQLADM` authority

### DB2 Analysis Queries

```python
_db2_analysis_queries = {
    'comprehensive_table_analysis': {
        'name': 'Comprehensive Table Analysis',
        'description': 'Complete table statistics including structure, size, and I/O',
        'category': 'information_schema',
        'sql': """SELECT
  t.TABNAME as table_name,
  t.CARD as row_count,
  t.NPAGES as pages,
  t.FPAGES as formatted_pages,
  t.OVERFLOW as overflow_pages,
  (t.NPAGES * 4) as data_size_kb,
  DECIMAL((t.NPAGES * 4.0 / 1024), 10, 2) as data_size_mb,
  t.AVGROWSIZE as avg_row_length_bytes,
  t.STATS_TIME as last_analyzed,
  t.COMPRESSION as compression,
  -- Index size
  idx.INDEX_SIZE_KB as index_size_kb,
  DECIMAL((idx.INDEX_SIZE_KB / 1024.0), 10, 2) as index_size_mb,
  -- Total size
  DECIMAL(((t.NPAGES * 4.0 + COALESCE(idx.INDEX_SIZE_KB, 0)) / 1024), 10, 2) as total_size_mb,
  -- Table organization
  t.TYPE as table_type,
  t.STATUS as status,
  t.TBSPACE as tablespace_name,
  t.PARTITION_MODE as partition_mode,
  -- I/O Statistics from MON_GET_TABLE
  io.ROWS_READ as rows_read,
  io.ROWS_INSERTED as rows_inserted,
  io.ROWS_UPDATED as rows_updated,
  io.ROWS_DELETED as rows_deleted,
  io.TABLE_SCANS as table_scans,
  io.OVERFLOW_ACCESSES as overflow_accesses,
  io.PAGE_REORGS as page_reorgs
FROM SYSCAT.TABLES t
LEFT JOIN (
  SELECT
    TABSCHEMA,
    TABNAME,
    SUM((NLEAF + NLEVELS) * 4) as INDEX_SIZE_KB
  FROM SYSCAT.INDEXES
  GROUP BY TABSCHEMA, TABNAME
) idx ON idx.TABSCHEMA = t.TABSCHEMA AND idx.TABNAME = t.TABNAME
LEFT JOIN TABLE(MON_GET_TABLE(:target_schema, NULL, -2)) AS io
  ON io.TABSCHEMA = t.TABSCHEMA AND io.TABNAME = t.TABNAME
WHERE t.TABSCHEMA = :target_schema
  AND t.TYPE = 'T'
ORDER BY t.CARD DESC NULLS LAST;""",
        'parameters': ['target_schema'],
        'notes': [
            'MON_GET_TABLE requires monitoring to be enabled',
            'Page size is typically 4KB, 8KB, 16KB, or 32KB (adjust multiplier if needed)',
            'RUNSTATS should be executed before collection for accurate statistics',
            'Use ADMIN_GET_TAB_INFO table function for more detailed info'
        ]
    },
    'comprehensive_index_analysis': {
        'name': 'Comprehensive Index Analysis',
        'description': 'Complete index statistics including structure and usage',
        'category': 'information_schema',
        'sql': """SELECT
  i.TABSCHEMA as table_schema,
  i.TABNAME as table_name,
  i.INDNAME as index_name,
  i.INDEXTYPE as index_type,
  i.UNIQUERULE as is_unique,
  i.COLCOUNT as column_count,
  i.UNIQUE_COLCOUNT as unique_column_count,
  i.NLEAF as leaf_pages,
  i.NLEVELS as btree_levels,
  i.FIRSTKEYCARD as first_key_cardinality,
  i.FULLKEYCARD as full_key_cardinality,
  i.CLUSTERRATIO as clustering_factor,
  i.SEQUENTIAL_PAGES as sequential_pages,
  i.DENSITY as density,
  ((i.NLEAF + i.NLEVELS) * 4) as index_size_kb,
  DECIMAL(((i.NLEAF + i.NLEVELS) * 4.0 / 1024), 10, 2) as index_size_mb,
  i.STATS_TIME as last_analyzed,
  i.COMPRESSION as compression,
  -- Index columns
  ic.COLNAME as column_name,
  ic.COLSEQ as column_position,
  ic.COLORDER as sort_order,
  -- Index usage statistics
  iu.INDEX_SCANS as index_scans,
  iu.INDEX_ONLY_SCANS as index_only_scans,
  iu.OBJECT_DATA_L_READS as logical_reads,
  iu.OBJECT_DATA_P_READS as physical_reads
FROM SYSCAT.INDEXES i
LEFT JOIN SYSCAT.INDEXCOLUSE ic
  ON ic.INDSCHEMA = i.INDSCHEMA AND ic.INDNAME = i.INDNAME
LEFT JOIN TABLE(MON_GET_INDEX(:target_schema, NULL, -2)) AS iu
  ON iu.TABSCHEMA = i.TABSCHEMA AND iu.INDNAME = i.INDNAME
WHERE i.TABSCHEMA = :target_schema
ORDER BY i.TABNAME, i.INDNAME, ic.COLSEQ;""",
        'parameters': ['target_schema'],
        'notes': [
            'MON_GET_INDEX requires monitoring to be enabled',
            'UNIQUERULE: D=Allow duplicates, U=Unique, P=Primary key, I=Unique allowing nulls',
            'CLUSTERRATIO: Higher values (near 100) indicate better physical clustering'
        ]
    },
    'column_analysis': {
        'name': 'Column Information Analysis',
        'description': 'Returns all column definitions including data types, nullability, and defaults',
        'category': 'information_schema',
        'sql': """SELECT
  TABNAME as table_name,
  COLNAME as column_name,
  COLNO as position,
  DEFAULT as default_value,
  NULLS as nullable,
  TYPENAME as data_type,
  LENGTH as data_length,
  SCALE as numeric_scale,
  IDENTITY as is_identity,
  GENERATED as is_generated,
  TEXT as column_comment,
  COMPRESS as compression,
  CODEPAGE as codepage,
  COLLATIONSCHEMA as collation_schema,
  COLLATIONNAME as collation_name,
  -- Column statistics
  COLCARD as distinct_values,
  HIGH2KEY as high_key_value,
  LOW2KEY as low_key_value
FROM SYSCAT.COLUMNS
WHERE TABSCHEMA = :target_schema
ORDER BY TABNAME, COLNO;""",
        'parameters': ['target_schema'],
        'notes': [
            'GENERATED: A=Always, D=By default, N=Not generated',
            'IDENTITY: Y=Identity column, N=Not identity',
            'Statistics require RUNSTATS to be current'
        ]
    },
    'foreign_key_analysis': {
        'name': 'Foreign Key Relationship Analysis',
        'description': 'Returns foreign key relationships with constraint names and table/column mappings',
        'category': 'information_schema',
        'sql': """SELECT
  r.CONSTNAME as constraint_name,
  r.TABSCHEMA as child_schema,
  r.TABNAME as child_table,
  fk.COLNAME as child_column,
  r.REFTABSCHEMA as parent_schema,
  r.REFTABNAME as parent_table,
  pk.COLNAME as parent_column,
  r.DELETERULE as delete_rule,
  r.UPDATERULE as update_rule,
  fk.COLSEQ as column_sequence,
  -- Determine cardinality
  CASE
    WHEN r.CONSTNAME IN (
      SELECT CONSTNAME FROM SYSCAT.TABCONST
      WHERE TYPE = 'U' AND TABSCHEMA = r.TABSCHEMA AND TABNAME = r.TABNAME
    ) THEN '1:1 or 1:0..1'
    ELSE '1:Many'
  END as estimated_cardinality
FROM SYSCAT.REFERENCES r
JOIN SYSCAT.KEYCOLUSE fk
  ON fk.CONSTNAME = r.CONSTNAME
  AND fk.TABSCHEMA = r.TABSCHEMA
  AND fk.TABNAME = r.TABNAME
JOIN SYSCAT.KEYCOLUSE pk
  ON pk.CONSTNAME = r.REFKEYNAME
  AND pk.TABSCHEMA = r.REFTABSCHEMA
  AND pk.TABNAME = r.REFTABNAME
  AND pk.COLSEQ = fk.COLSEQ
WHERE r.TABSCHEMA = :target_schema
ORDER BY r.TABNAME, r.CONSTNAME, fk.COLSEQ;""",
        'parameters': ['target_schema'],
        'notes': [
            'DELETERULE: A=No action, C=Cascade, N=Set null, R=Restrict',
            'UPDATERULE: A=No action, R=Restrict (DB2 does not support ON UPDATE CASCADE)',
            'Use SYSCAT.TABCONST to identify unique constraints'
        ]
    },
    'query_performance_stats': {
        'name': 'Query Performance Statistics',
        'description': 'Query execution statistics from package cache and monitoring',
        'category': 'performance_schema',
        'sql': """SELECT
  'QUERY' as source_type,
  s.STMT_TEXT as query_pattern,
  NULL as procedure_name,
  s.NUM_EXEC_WITH_METRICS as total_executions,
  DECIMAL(s.TOTAL_CPU_TIME / NULLIF(s.NUM_EXEC_WITH_METRICS, 0) / 1000.0, 10, 2) as avg_cpu_time_ms,
  DECIMAL(s.STMT_EXEC_TIME / NULLIF(s.NUM_EXEC_WITH_METRICS, 0) / 1000.0, 10, 2) as avg_latency_ms,
  DECIMAL(s.STMT_EXEC_TIME / 1000.0, 10, 2) as total_time_ms,
  DECIMAL(s.ROWS_RETURNED / NULLIF(s.NUM_EXEC_WITH_METRICS, 0), 10, 2) as avg_rows_returned,
  DECIMAL(s.ROWS_MODIFIED / NULLIF(s.NUM_EXEC_WITH_METRICS, 0), 10, 2) as avg_rows_modified,
  s.PREP_TIME as prep_time_ms,
  s.TOTAL_CPU_TIME / 1000.0 as total_cpu_time_ms,
  -- I/O metrics
  DECIMAL(s.POOL_DATA_L_READS / NULLIF(s.NUM_EXEC_WITH_METRICS, 0), 10, 2) as avg_logical_reads,
  DECIMAL(s.POOL_DATA_P_READS / NULLIF(s.NUM_EXEC_WITH_METRICS, 0), 10, 2) as avg_physical_reads,
  DECIMAL(s.POOL_INDEX_L_READS / NULLIF(s.NUM_EXEC_WITH_METRICS, 0), 10, 2) as avg_index_logical_reads,
  DECIMAL(s.POOL_INDEX_P_READS / NULLIF(s.NUM_EXEC_WITH_METRICS, 0), 10, 2) as avg_index_physical_reads,
  -- Cache hit ratio
  CASE
    WHEN (s.POOL_DATA_L_READS + s.POOL_INDEX_L_READS) > 0
    THEN DECIMAL(
      ((s.POOL_DATA_L_READS + s.POOL_INDEX_L_READS - s.POOL_DATA_P_READS - s.POOL_INDEX_P_READS) /
      NULLIF((s.POOL_DATA_L_READS + s.POOL_INDEX_L_READS), 0.0) * 100),
      5, 2
    )
    ELSE NULL
  END as cache_hit_ratio_pct,
  -- Sorts and overflows
  s.TOTAL_SORTS as total_sorts,
  s.SORT_OVERFLOWS as sort_overflows,
  -- Timestamps
  s.LAST_METRICS_UPDATE as last_seen
FROM TABLE(MON_GET_PKG_CACHE_STMT(NULL, NULL, NULL, -2)) AS s
WHERE s.DBPARTITIONNUM = DBPARTITIONNUM(CURRENT SERVER)
  -- Filter out system queries
  AND s.STMT_TEXT NOT LIKE '%SYSCAT%'
  AND s.STMT_TEXT NOT LIKE '%SYSSTAT%'
  AND s.STMT_TEXT NOT LIKE '%SYSIBM%'
  AND s.STMT_TEXT NOT LIKE '%SYSPROC%'
  AND s.STMT_TEXT NOT LIKE 'SET %'
  AND s.STMT_TEXT NOT LIKE 'CALL SYSPROC%'
  -- Only queries with meaningful execution
  AND s.NUM_EXEC_WITH_METRICS > 0
  AND LENGTH(TRIM(s.STMT_TEXT)) > 10

UNION ALL

-- Stored procedures
SELECT
  'PROCEDURE' as source_type,
  'PROCEDURE: ' || r.ROUTINENAME as query_pattern,
  r.ROUTINENAME as procedure_name,
  p.NUM_EXECUTIONS as total_executions,
  DECIMAL(p.TOTAL_CPU_TIME / NULLIF(p.NUM_EXECUTIONS, 0) / 1000.0, 10, 2) as avg_cpu_time_ms,
  DECIMAL(p.ROUTINE_INVOKE_TIME / NULLIF(p.NUM_EXECUTIONS, 0) / 1000.0, 10, 2) as avg_latency_ms,
  DECIMAL(p.ROUTINE_INVOKE_TIME / 1000.0, 10, 2) as total_time_ms,
  NULL as avg_rows_returned,
  NULL as avg_rows_modified,
  NULL as prep_time_ms,
  DECIMAL(p.TOTAL_CPU_TIME / 1000.0, 10, 2) as total_cpu_time_ms,
  NULL as avg_logical_reads,
  NULL as avg_physical_reads,
  NULL as avg_index_logical_reads,
  NULL as avg_index_physical_reads,
  NULL as cache_hit_ratio_pct,
  NULL as total_sorts,
  NULL as sort_overflows,
  p.LAST_METRICS_UPDATE as last_seen
FROM SYSCAT.ROUTINES r
LEFT JOIN TABLE(MON_GET_ROUTINE(NULL, :target_schema, -2)) AS p
  ON p.ROUTINESCHEMA = r.ROUTINESCHEMA AND p.ROUTINENAME = r.ROUTINENAME
WHERE r.ROUTINESCHEMA = :target_schema
  AND r.ROUTINETYPE IN ('P', 'F')
  AND p.NUM_EXECUTIONS > 0

ORDER BY total_time_ms DESC NULLS LAST;""",
        'parameters': ['target_schema'],
        'notes': [
            'MON_GET_PKG_CACHE_STMT shows statements in package cache',
            'Package cache may be flushed, losing historical data',
            'For long-term monitoring, consider using Event Monitors',
            'Times are in microseconds, converted to milliseconds'
        ]
    },
    'stored_procedures_analysis': {
        'name': 'Stored Procedures and Functions Analysis',
        'description': 'Detailed information about stored procedures and functions',
        'category': 'information_schema',
        'sql': """SELECT
  ROUTINESCHEMA as schema_name,
  ROUTINENAME as procedure_name,
  SPECIFICNAME as specific_name,
  ROUTINETYPE as procedure_type,
  LANGUAGE as language,
  DETERMINISTIC as is_deterministic,
  SQL_DATA_ACCESS as sql_data_access,
  ORIGIN as origin,
  CREATED as created_date,
  LAST_REGEN_TIME as last_regenerated,
  VALID as is_valid,
  TEXT as definition
FROM SYSCAT.ROUTINES
WHERE ROUTINESCHEMA = :target_schema
  AND ROUTINETYPE IN ('P', 'F')
ORDER BY ROUTINENAME;""",
        'parameters': ['target_schema'],
        'notes': [
            'ROUTINETYPE: P=Procedure, F=Function',
            'LANGUAGE: SQL, C, JAVA, CLR, etc.',
            'TEXT column may be null for external routines'
        ]
    },
    'triggers_analysis': {
        'name': 'Triggers Analysis',
        'description': 'Detailed information about database triggers',
        'category': 'information_schema',
        'sql': """SELECT
  TRIGSCHEMA as schema_name,
  TRIGNAME as trigger_name,
  TABSCHEMA as table_schema,
  TABNAME as table_name,
  TRIGTIME as timing,
  TRIGEVENT as event_type,
  GRANULARITY as for_each,
  VALID as status,
  CREATE_TIME as created_date,
  QUALIFIER as qualifier,
  TEXT as definition
FROM SYSCAT.TRIGGERS
WHERE TRIGSCHEMA = :target_schema
ORDER BY TABNAME, TRIGNAME;""",
        'parameters': ['target_schema'],
        'notes': [
            'TRIGTIME: A=After, B=Before, I=Instead of',
            'TRIGEVENT: I=Insert, U=Update, D=Delete',
            'GRANULARITY: R=Row, S=Statement',
            'DB2 supports compound triggers (multiple timing/event combinations)'
        ]
    },
    'views_analysis': {
        'name': 'Views Analysis',
        'description': 'Detailed information about database views',
        'category': 'information_schema',
        'sql': """SELECT
  VIEWSCHEMA as schema_name,
  VIEWNAME as view_name,
  VALID as is_valid,
  READONLY as is_read_only,
  CHECK_OPTION as check_option,
  TEXT as definition,
  -- Get column information
  c.COLNAME as column_name,
  c.TYPENAME as data_type,
  c.NULLS as nullable,
  c.COLNO as column_position
FROM SYSCAT.VIEWS v
LEFT JOIN SYSCAT.COLUMNS c
  ON c.TABSCHEMA = v.VIEWSCHEMA AND c.TABNAME = v.VIEWNAME
WHERE v.VIEWSCHEMA = :target_schema
ORDER BY v.VIEWNAME, c.COLNO;""",
        'parameters': ['target_schema'],
        'notes': [
            'TEXT contains full view definition',
            'Materialized Query Tables (MQTs) are in SYSCAT.TABLES with TYPE=S',
            'Check SYSCAT.VIEWDEP for view dependencies'
        ]
    }
}
```

---

## Implementation Checklist

### Phase 1 - Oracle Implementation

- [ ] **Validate connection requirements**
  - [ ] Test privilege requirements
  - [ ] Confirm minimum Oracle version support (11g vs 12c vs 19c)
  - [ ] Test with and without AWR/Diagnostic Pack license

- [ ] **Test and refine queries**
  - [ ] Test on Oracle 11g, 12c, 19c, 21c
  - [ ] Verify V$SQL vs V$SQLAREA vs DBA_HIST_SQLSTAT
  - [ ] Validate table/index statistics accuracy
  - [ ] Test stored procedure/function extraction
  - [ ] Test trigger definition extraction
  - [ ] Verify view dependency handling

- [ ] **Handle Oracle-specific features**
  - [ ] Partitioned tables (RANGE, LIST, HASH, COMPOSITE)
  - [ ] Materialized views vs regular views
  - [ ] Index-organized tables (IOTs)
  - [ ] Clusters
  - [ ] Packages (multiple procedures/functions)
  - [ ] Object types and collections
  - [ ] Synonyms (public and private)
  - [ ] Database links

- [ ] **Performance optimization**
  - [ ] Add query hints if needed
  - [ ] Consider parallel execution for large schemas
  - [ ] Implement pagination for large result sets
  - [ ] Cache frequently accessed catalog data

- [ ] **Create Oracle Collector Agent**
  - [ ] Implement OracleCollectorAgent class
  - [ ] Add Oracle-specific field mapping to CollectorOutputContract
  - [ ] Handle Oracle data type conversions
  - [ ] Implement error handling for missing privileges

### Phase 1 - DB2 Implementation

- [ ] **Validate connection requirements**
  - [ ] Test privilege requirements
  - [ ] Confirm minimum DB2 version support (10.5 vs 11.5)
  - [ ] Enable monitoring (mon_get_* functions)

- [ ] **Test and refine queries**
  - [ ] Test on DB2 LUW (Linux/Unix/Windows)
  - [ ] Test on DB2 for z/OS (different catalog structure)
  - [ ] Verify MON_GET_TABLE/INDEX functions work
  - [ ] Test package cache queries
  - [ ] Validate RUNSTATS requirements
  - [ ] Test stored procedure extraction
  - [ ] Test trigger definition extraction

- [ ] **Handle DB2-specific features**
  - [ ] Table partitioning (range, hash, generated)
  - [ ] Materialized Query Tables (MQTs)
  - [ ] Multidimensional clustering (MDC)
  - [ ] Column-organized tables vs row-organized
  - [ ] Compression options (row, page, adaptive)
  - [ ] Federation (nicknames, remote tables)
  - [ ] Packages vs stored procedures

- [ ] **Performance optimization**
  - [ ] Add OPTIMIZE FOR clauses if needed
  - [ ] Consider using ADMIN_CMD procedures
  - [ ] Implement pagination for large schemas
  - [ ] Handle multi-partition environments

- [ ] **Create DB2 Collector Agent**
  - [ ] Implement DB2CollectorAgent class
  - [ ] Add DB2-specific field mapping to CollectorOutputContract
  - [ ] Handle DB2 data type conversions
  - [ ] Implement error handling for missing privileges

---

## Data Type Mapping Reference

### Oracle Data Types → Target Databases

| Oracle Type | DynamoDB | DocumentDB | PostgreSQL | MySQL |
|-------------|----------|------------|------------|-------|
| NUMBER | Number | Number | NUMERIC | DECIMAL |
| VARCHAR2 | String | String | VARCHAR | VARCHAR |
| CHAR | String | String | CHAR | CHAR |
| DATE | String (ISO8601) | Date | TIMESTAMP | DATETIME |
| TIMESTAMP | String (ISO8601) | Date | TIMESTAMP | DATETIME |
| CLOB | String (max 400KB) | String | TEXT | TEXT |
| BLOB | Binary | BinData | BYTEA | BLOB |
| RAW | Binary | BinData | BYTEA | VARBINARY |
| XMLTYPE | String | String | XML | TEXT |
| ROWID | String | String | N/A | N/A |

### DB2 Data Types → Target Databases

| DB2 Type | DynamoDB | DocumentDB | PostgreSQL | MySQL |
|----------|----------|------------|------------|-------|
| INTEGER | Number | Int32 | INTEGER | INT |
| BIGINT | Number | Int64 | BIGINT | BIGINT |
| DECIMAL | Number | Decimal128 | NUMERIC | DECIMAL |
| DOUBLE | Number | Double | DOUBLE PRECISION | DOUBLE |
| CHAR | String | String | CHAR | CHAR |
| VARCHAR | String | String | VARCHAR | VARCHAR |
| CLOB | String (max 400KB) | String | TEXT | TEXT |
| BLOB | Binary | BinData | BYTEA | BLOB |
| DATE | String (ISO8601) | Date | DATE | DATE |
| TIME | String | String | TIME | TIME |
| TIMESTAMP | String (ISO8601) | Date | TIMESTAMP | DATETIME |
| XML | String | String | XML | TEXT |

---

## Known Limitations and Considerations

### Oracle

1. **Licensing**
   - AWR/Diagnostic Pack features (V$SQL_PLAN, DBA_HIST_*) require license
   - Performance monitoring views (V$) may have restrictions
   - Consider using Statspack as free alternative

2. **Security**
   - Many DBA_* views require elevated privileges
   - May need to grant SELECT_CATALOG_ROLE
   - CDB vs PDB (multitenant) adds complexity

3. **Performance**
   - Large shared pools mean V$SQL can have millions of rows
   - Consider filtering by PARSING_SCHEMA_NAME early
   - Use FIRST_ROWS hint for sampling

4. **Complex Features**
   - Partitioned tables have multiple segments
   - Packages contain multiple procedures/functions
   - Nested tables and VARRAYs need special handling
   - Object types (user-defined types) complicate migration

### DB2

1. **Monitoring**
   - MON_GET_* functions require monitoring enabled
   - Package cache can be flushed (losing data)
   - Event monitors provide better long-term data
   - DB2 for z/OS has different catalog structure

2. **Statistics**
   - Requires recent RUNSTATS for accurate data
   - Automatic statistics may not be current
   - REORGCHK output provides additional insights

3. **Platform Differences**
   - DB2 LUW (Linux/Unix/Windows) vs z/OS
   - Different catalog views and system procedures
   - Federation features (nicknames) complicate analysis

4. **Complex Features**
   - MDC (multidimensional clustering) tables
   - Column-organized vs row-organized tables
   - Compression types (row, page, adaptive)
   - Generated columns and identity columns

---

## Next Steps for Team

### Immediate Actions

1. **Oracle Expert Needed**
   - Recruit Oracle DBA or specialist SA
   - Validate privilege requirements
   - Test queries on real customer workloads
   - Identify AWR-dependent features

2. **DB2 Expert Needed**
   - Recruit DB2 DBA or specialist SA
   - Validate privilege requirements
   - Test on both DB2 LUW and z/OS if possible
   - Enable monitoring and test MON_GET_* functions

3. **Testing Environment**
   - Set up Oracle 19c test instance
   - Set up DB2 11.5 test instance
   - Create sample schemas with various object types
   - Populate with realistic data volumes

4. **Documentation**
   - Document privilege requirements
   - Create setup scripts for enabling monitoring
   - Document known limitations
   - Create troubleshooting guide

### Integration with Existing Code

Once validated, integrate these queries into the collector agent framework following the same pattern as MySQL/PostgreSQL/SQL Server:

```python
# agents/collector/oracle_collector.py
from agents.collector.base_collector import BaseCollector
from agents.collector.oracle_queries import _oracle_analysis_queries

class OracleCollectorAgent(BaseCollector):
    def __init__(self):
        super().__init__(database_type='oracle')
        self.queries = _oracle_analysis_queries

    def collect(self, input_contract):
        # Implementation following MySQL/PostgreSQL pattern
        pass

# agents/collector/db2_collector.py
from agents.collector.base_collector import BaseCollector
from agents.collector.db2_queries import _db2_analysis_queries

class DB2CollectorAgent(BaseCollector):
    def __init__(self):
        super().__init__(database_type='db2')
        self.queries = _db2_analysis_queries

    def collect(self, input_contract):
        # Implementation following MySQL/PostgreSQL pattern
        pass
```

---

## Testing Checklist

### Oracle Testing

- [ ] Test on Oracle 11g, 12c, 19c, 21c
- [ ] Test with and without AWR license
- [ ] Test on CDB (container database)
- [ ] Test on PDB (pluggable database)
- [ ] Test with various privilege levels
- [ ] Test with large schemas (10,000+ tables)
- [ ] Test with partitioned tables
- [ ] Test with packages
- [ ] Test with database links
- [ ] Test with synonyms
- [ ] Test with materialized views
- [ ] Validate query performance (<30 seconds per query)

### DB2 Testing

- [ ] Test on DB2 10.5, 11.5
- [ ] Test on DB2 LUW
- [ ] Test on DB2 for z/OS (if applicable)
- [ ] Test with monitoring enabled/disabled
- [ ] Test with various privilege levels
- [ ] Test with large schemas (10,000+ tables)
- [ ] Test with partitioned tables
- [ ] Test with MDC tables
- [ ] Test with column-organized tables
- [ ] Test with compressed tables
- [ ] Test with federation (nicknames)
- [ ] Validate query performance (<30 seconds per query)

---

**End of Oracle and DB2 Query Templates**
