# Query Templates

SQL query templates for collecting metadata from different database systems.

## 📚 Available Templates

| Database | File | Status | Notes |
|----------|------|--------|-------|
| **MySQL** | [mysql-queries.md](mysql-queries.md) | ✅ Implemented | Includes performance_schema queries |
| **PostgreSQL** | [postgresql-queries.md](postgresql-queries.md) | ✅ Implemented | Includes pg_stat_statements queries |
| **MariaDB** | Uses MySQL queries | ✅ Implemented | MariaDB is MySQL-compatible |
| **SQL Server** | [sqlserver-queries.md](sqlserver-queries.md) | [PLANNED] | Includes DMV queries |
| **Oracle & DB2** | [oracle-db2-queries.md](oracle-db2-queries.md) | [PLANNED] | Combined template |

## 🎯 Purpose

These query templates provide SQL queries for collector agents to gather comprehensive metadata from source databases.

## 📋 What Each Template Includes

### 1. Database Metadata

- Version information
- Database size and configuration
- System settings

### 2. Schema Information

- Tables (names, row counts, sizes)
- Columns (data types, constraints, defaults)
- Indexes (types, columns, sizes)
- Foreign keys (relationships, referential actions)

### 3. Query Patterns

- Query execution statistics
- Performance metrics (execution time, CPU time, I/O)
- Query frequency and patterns
- Slow query identification

### 4. Database Objects

- Views (definitions, dependencies)
- Stored procedures (definitions, parameters, execution stats)
- Functions (definitions, return types)
- Triggers (definitions, timing, events)

### 5. Performance Metrics

- Connection statistics
- Resource usage (CPU, memory, I/O)
- Wait statistics
- Lock information

### 6. Sample Data

- Sample rows (PII anonymization is [ROADMAP])
- Data distribution
- Cardinality estimates

## 🔑 Database-Specific Features

### MySQL

- **Query Source**: `performance_schema.events_statements_summary_by_digest`
- **Requirements**: performance_schema must be enabled
- **Special Features**:
  - Table I/O statistics
  - Index usage tracking
  - Lock wait statistics

### PostgreSQL

- **Query Source**: `pg_stat_statements` extension
- **Requirements**: pg_stat_statements extension must be installed
- **Special Features**:
  - Cache hit ratio calculation
  - Shared block statistics
  - Temporary block usage

### SQL Server

- **Query Source**: Dynamic Management Views (DMVs)
- **Requirements**: VIEW SERVER STATE permission
- **Special Features**:
  - Query Store integration
  - Execution plan analysis
  - Wait statistics

### Oracle & DB2

- **Query Source**: System catalog views
- **Requirements**: SELECT_CATALOG_ROLE or equivalent
- **Special Features**:
  - AWR/ASH data (Oracle)
  - MON_* functions (DB2)
  - Tablespace information

## 🚀 Usage Example

### MySQL Collector

```python
from strands import Tool
import mysql.connector

def collect_tables(connection):
    """Collect table metadata using query template"""
    cursor = connection.cursor(dictionary=True)

    # Query from mysql-queries.md
    cursor.execute("""
        SELECT
            table_name,
            table_rows as row_count,
            data_length as data_size_bytes,
            index_length as index_size_bytes,
            ROUND((data_length + index_length) / 1024 / 1024, 2) as total_size_mb
        FROM information_schema.tables
        WHERE table_schema = DATABASE()
        AND table_type = 'BASE TABLE'
        ORDER BY table_name
    """)

    return cursor.fetchall()
```

### PostgreSQL Collector

```python
def collect_query_patterns(connection):
    """Collect query patterns using pg_stat_statements"""
    cursor = connection.cursor()

    # Query from postgresql-queries.md
    cursor.execute("""
        SELECT
            queryid,
            query,
            calls,
            total_exec_time / 1000 as total_time_seconds,
            mean_exec_time as avg_time_ms,
            CASE
                WHEN (shared_blks_hit + shared_blks_read) > 0
                THEN (shared_blks_hit::float / (shared_blks_hit + shared_blks_read) * 100)
                ELSE 0
            END as cache_hit_ratio_pct
        FROM pg_stat_statements
        WHERE calls >= 10
        ORDER BY total_exec_time DESC
        LIMIT 1000
    """)

    return cursor.fetchall()
```

## 📊 Query Template Structure

Each template follows this standard structure:

```markdown
# {Database} Query Templates

## Overview
Brief description and requirements

## Database Metadata Queries
Version, size, configuration

## Schema Collection Queries
Tables, columns, indexes, foreign keys

## Query Pattern Collection
Query statistics and performance

## Views, Procedures, and Triggers
Database object definitions

## Performance Metrics
Connection stats, resource usage

## Sample Data Collection
Sample row queries

## Notes
Important considerations and requirements
```

## 🔄 Query Execution Order

Recommended order for executing queries:

1. **Database Metadata** - Quick, establishes context
2. **Schema Collection** - Core metadata, relatively fast
3. **Query Patterns** - Can be slow, may need filtering
4. **Database Objects** - Moderate speed
5. **Performance Metrics** - Quick, current state
6. **Sample Data** - Can be slow, optional

## ⚠️ Important Considerations

### Performance Impact

- **Query patterns collection** can be resource-intensive
- Use **LIMIT** clauses to prevent excessive data transfer
- Implement **timeouts** for long-running queries
- Consider **off-peak hours** for large databases

### Permissions Required

| Database | Minimum Permissions |
|----------|-------------------|
| MySQL | SELECT on information_schema, performance_schema |
| PostgreSQL | SELECT on system catalogs, pg_read_all_stats |
| SQL Server | VIEW SERVER STATE, VIEW DEFINITION |
| Oracle | SELECT_CATALOG_ROLE |
| DB2 | SYSMON, DATAACCESS |

### Prerequisites

| Database | Prerequisites |
|----------|--------------|
| MySQL | performance_schema enabled |
| PostgreSQL | pg_stat_statements extension installed |
| SQL Server | Query Store enabled (optional) |
| Oracle | AWR license (for historical data) |
| DB2 | MON_* functions enabled |

## 📝 Customizing Queries

### Adding Filters

```sql
-- Add database filter
WHERE table_schema = DATABASE()

-- Add time range filter
AND last_execution_time >= DATEADD(day, -7, GETDATE())

-- Add execution count filter
AND execution_count >= 10
```

### Adjusting Limits

```sql
-- Increase result limit
LIMIT 1000  -- Default
LIMIT 5000  -- For comprehensive analysis

-- Add pagination
LIMIT 1000 OFFSET 0  -- First page
LIMIT 1000 OFFSET 1000  -- Second page
```

### Performance Tuning

```sql
-- Add query hints (SQL Server)
SELECT * FROM sys.tables WITH (NOLOCK)

-- Use parallel query (PostgreSQL)
SET max_parallel_workers_per_gather = 4;

-- Optimize buffer pool (MySQL)
SET SESSION optimizer_search_depth = 0;
```

## 🔗 Related Documentation

- **Collection Matrix**: [../database-collection-matrix.md](../database-collection-matrix.md)
- **Collector Contracts**: [../../contracts/agent-contracts-spec.md](../../contracts/agent-contracts-spec.md)

## ❓ Questions?

1. Check the specific query template for your database
2. Review the collection matrix for field mappings
3. Consult the collector contract specification
4. Ask the data engineering team

---

**Last Updated:** January 22, 2026
**Maintained By:** Database Modernizer Data Engineering Team
