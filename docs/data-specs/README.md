# Data Specifications Documentation

This directory contains specifications for what data to collect from source databases and how to collect it.

## 📚 Documentation Index

### Core Documents

| Document | Purpose | Audience |
|----------|---------|----------|
| [database-collection-matrix.md](database-collection-matrix.md) | Matrix of what data each database provides | Collector agent developers |
| [query-templates/](query-templates/) | SQL query templates for each database | Collector agent developers |

## 🎯 Purpose

This directory defines **WHAT data to collect** from source databases. It provides:

1. **Collection Matrix** - Comprehensive matrix showing what metadata each database type can provide
2. **Query Templates** - SQL queries for collecting that metadata from each database

## 📊 Database Collection Matrix

The [database-collection-matrix.md](database-collection-matrix.md) provides a comprehensive matrix showing:

- What metadata is available from each database type
- Field mappings across different databases
- Data type normalizations
- Collection priorities

### Coverage

| Database | Query Templates | Status |
|----------|----------------|--------|
| MySQL | [mysql-queries.md](query-templates/mysql-queries.md) | ✅ Implemented |
| PostgreSQL | [postgresql-queries.md](query-templates/postgresql-queries.md) | ✅ Implemented |
| MariaDB | Uses MySQL queries | ✅ Implemented |
| SQL Server | [sqlserver-queries.md](query-templates/sqlserver-queries.md) | [PLANNED] Template only |
| Oracle & DB2 | [oracle-db2-queries.md](query-templates/oracle-db2-queries.md) | [PLANNED] Template only |

## 📝 Query Templates

Each query template document provides SQL queries for collecting:

### 1. Database Metadata

- Version and configuration
- Database size
- System settings

### 2. Schema Information

- Tables (with row counts, sizes)
- Columns (data types, constraints)
- Indexes (types, sizes, usage)
- Foreign keys (relationships)

### 3. Query Patterns

- Query execution statistics
- Performance metrics
- Table and index usage
- Slow queries

### 4. Database Objects

- Views (definitions, dependencies)
- Stored procedures (definitions, execution stats)
- Functions (definitions, parameters)
- Triggers (definitions, timing)

### 5. Performance Metrics

- Connection statistics
- Resource usage
- Wait statistics
- I/O statistics

### 6. Sample Data

- Sample rows (PII anonymization is [ROADMAP])
- Data distribution
- Cardinality estimates

## 🚀 Quick Start

### For Collector Agent Developers

1. **Read the collection matrix**: Understand what data to collect
2. **Review query templates**: See how to collect that data
3. **Implement collector**: Use Strands SDK with these queries
4. **Validate output**: Ensure output matches collector contract

### For Database Experts

1. **Review query templates**: Verify queries are optimal
2. **Suggest improvements**: Propose better queries
3. **Add database-specific queries**: Extend templates for specific features
4. **Document limitations**: Note any collection constraints

## 🔑 Key Concepts

### Data Collection Principles

1. **Read-Only Access** - All queries use SELECT only
2. **Minimal Impact** - Queries designed for minimal performance impact
3. **Comprehensive Coverage** - Collect all metadata needed for analysis
4. **PII Protection** - [ROADMAP] Support for anonymizing sensitive data
5. **Configurable Depth** - Allow users to control collection scope

### Query Template Structure

Each query template follows this structure:

```markdown
# Database Query Templates

## Database Metadata Queries
- Version and configuration
- Database size

## Schema Collection Queries
- Tables metadata
- Columns metadata
- Indexes
- Foreign keys

## Query Pattern Collection
- Query statistics
- Table I/O statistics
- Index usage statistics

## Views, Procedures, and Triggers
- Views
- Stored procedures and functions
- Triggers

## Performance Metrics
- Connection statistics
- Query performance

## Sample Data Collection
- Sample rows
```

### Database-Specific Features

Each database has unique features that require special handling:

| Database | Special Features | Query Source |
|----------|------------------|--------------|
| **MySQL** | performance_schema | events_statements_summary_by_digest |
| **PostgreSQL** | pg_stat_statements | pg_stat_statements extension |
| **SQL Server** | DMVs | sys.dm_exec_query_stats |
| **Oracle** | AWR, ASH | DBA_HIST_* views |
| **DB2** | MON_* functions | SYSIBMADM views |

## 📖 Using Query Templates

### Example: MySQL Collector

```python
from strands import Tool
import mysql.connector

def collect_mysql_schema(connection):
    """Collect schema using query templates"""
    cursor = connection.cursor(dictionary=True)

    # Use query from mysql-queries.md
    cursor.execute("""
        SELECT
            table_name,
            table_rows as row_count,
            data_length as data_size_bytes,
            index_length as index_size_bytes
        FROM information_schema.tables
        WHERE table_schema = DATABASE()
        AND table_type = 'BASE TABLE'
    """)

    tables = cursor.fetchall()
    cursor.close()
    return tables

# Create Strands Tool
collect_schema = Tool(
    name="collect_schema",
    description="Collect schema from MySQL database",
    function=collect_mysql_schema
)
```

### Example: PostgreSQL Collector

```python
def collect_postgresql_queries(connection):
    """Collect query patterns using pg_stat_statements"""
    cursor = connection.cursor()

    # Use query from postgresql-queries.md
    cursor.execute("""
        SELECT
            queryid,
            query,
            calls,
            total_exec_time / 1000 as total_time_seconds,
            mean_exec_time as avg_time_ms
        FROM pg_stat_statements
        WHERE calls >= 10
        ORDER BY total_exec_time DESC
        LIMIT 1000
    """)

    queries = cursor.fetchall()
    cursor.close()
    return queries
```

## 🔄 Collection Workflow

### Standard Collection Process

```
1. Connect to database
   ↓
2. Collect database metadata
   ↓
3. Collect schema information
   ↓
4. Collect query patterns
   ↓
5. Collect database objects (views, procedures, triggers)
   ↓
6. Collect performance metrics
   ↓
7. Collect sample data (if enabled)
   ↓
8. Validate and return results
```

### Error Handling

- **Connection failures**: Return error with partial results
- **Permission errors**: Skip restricted queries, continue with available data
- **Timeout**: Implement query timeouts, return partial results
- **Large datasets**: Use pagination, limit result sets

## 📊 Data Collection Matrix

The collection matrix maps:

- **Source database fields** → **Normalized fields**
- **Database-specific queries** → **Standard output format**
- **Optional fields** → **Availability by database**

### Example Matrix Entry

| Field | MySQL | PostgreSQL | SQL Server | Oracle | DB2 |
|-------|-------|------------|------------|--------|-----|
| table_name | ✅ | ✅ | ✅ | ✅ | ✅ |
| row_count | ✅ (approx) | ✅ (approx) | ✅ (exact) | ✅ (approx) | ✅ (approx) |
| data_size_mb | ✅ | ✅ | ✅ | ✅ | ✅ |
| query_execution_count | ✅ (perf_schema) | ✅ (pg_stat) | ✅ (DMV) | ✅ (AWR) | ✅ (MON) |

## 🔗 Related Documentation

- **Architecture**: [../architecture/high-level-design.md](../architecture/high-level-design.md)
- **Agent Contracts**: [../contracts/agent-contracts-spec.md](../contracts/agent-contracts-spec.md)

## 📝 Contributing

When updating data specifications:

1. **Update collection matrix**: Add new fields or databases
2. **Update query templates**: Add or modify SQL queries
3. **Test queries**: Verify queries work on target databases
4. **Document limitations**: Note any constraints or requirements
5. **Update contracts**: Ensure collector output contract reflects changes
6. **Review**: Get approval from database experts

### Adding a New Database

To add support for a new database:

1. Create query template: `query-templates/{database}-queries.md`
2. Update collection matrix: Add column for new database
3. Document special features: Note unique capabilities
4. Create test cases: Verify queries work correctly
5. Update collector contract: Add database to enum

## 🎯 Best Practices

### Query Design

- **Use parameterized queries**: Prevent SQL injection
- **Limit result sets**: Use TOP/LIMIT to prevent memory issues
- **Filter early**: Apply WHERE clauses to reduce data transfer
- **Use indexes**: Ensure queries use appropriate indexes
- **Avoid locks**: Use NOLOCK hints where appropriate

### Performance Considerations

- **Query timeouts**: Set reasonable timeouts (30-60 seconds)
- **Batch operations**: Collect data in batches for large databases
- **Parallel collection**: Collect different data types in parallel
- **Resource limits**: Monitor memory and CPU usage
- **Connection pooling**: Reuse database connections

### Security Considerations

- **Read-only access**: Use read-only database users
- **PII anonymization**: [ROADMAP] Anonymize sensitive data in sample rows
- **Credential encryption**: Encrypt credentials at rest
- **TLS connections**: Use encrypted connections
- **Audit logging**: Log all data collection activities

## ❓ Questions?

1. Check the [database-collection-matrix.md](database-collection-matrix.md) for field mappings
2. Review query templates for specific databases
3. Consult the collector contract specification
4. Open a GitHub issue for questions

---

**Last Updated:** June 2026
**Maintained By:** Database Modernizer Assessment Team
