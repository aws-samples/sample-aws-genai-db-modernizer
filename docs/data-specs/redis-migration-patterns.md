# Redis Migration Patterns and Use Cases

**Document Type:** Data Specification
**Last Updated:** February 6, 2026
**Status:** PLANNED — Redis collection is not yet implemented

---

## Overview

This document details Redis migration patterns, use cases, and modernization opportunities. Based on extensive customer interactions, Redis-to-purpose-built-database migrations represent a significant modernization opportunity.

---

## Business Opportunity

Many customers initially adopt Redis as a general-purpose data store but encounter scalability, durability, and operational challenges as their applications grow.

---

## Common Redis Usage Patterns

| Pattern | Current State (Redis) | Modernization Opportunity | Target AWS Service |
|---------|----------------------|---------------------------|-------------------|
| **Session Storage** | Redis with persistence enabled, manual backup/restore | Managed service with automatic backups, global replication | ElastiCache for Valkey, DynamoDB |
| **Caching Layer** | Redis as cache-aside pattern | Managed caching with automatic failover, cluster mode | ElastiCache for Valkey |
| **Real-time Leaderboards** | Redis Sorted Sets (ZADD, ZRANGE) | Serverless, auto-scaling sorted data | DynamoDB with sort keys |
| **Message Queues** | Redis Lists (LPUSH, RPOP) | Managed message queuing with guaranteed delivery | Amazon SQS, Amazon MQ |
| **Document Storage** | Redis with JSON strings | Native document database with indexing and queries | DocumentDB |
| **Full-Text Search** | Redis with RediSearch module | Managed search with advanced text analysis | OpenSearch |
| **Graph Relationships** | Redis with RedisGraph module | Purpose-built graph database | Neptune |
| **Time-Series Data** | Redis with RedisTimeSeries module | Purpose-built time-series database | Timestream (Phase 1) |

---

## Migration Drivers

### 1. Durability Requirements

Redis persistence (RDB/AOF) doesn't provide the same durability guarantees as purpose-built databases.

### 2. Operational Complexity

Managing Redis clusters, sharding, and replication requires specialized expertise.

### 3. Cost Optimization

Redis memory-only architecture can be expensive for large datasets; purpose-built databases offer tiered storage.

### 4. Feature Gaps

Redis lacks native support for complex queries, transactions across keys, and advanced indexing.

### 5. Compliance

Purpose-built databases offer better audit logging, encryption, and compliance certifications.

---

## Phase 0 Scope

| # | Capability | Description | Phase |
|---|------------|-------------|-------|
| 1 | **Redis Collector Agent** | Collect data from ElastiCache for Valkey and self-managed Redis instances | P0 - Q2 2026 |
| 2 | **Pattern Analysis** | Identify Redis data structures (strings, hashes, lists, sets, sorted sets) and access patterns | P0 - Q2 2026 |
| 3 | **Command Analysis** | Analyze Redis commands (GET, SET, ZADD, LPUSH, etc.) to understand workload characteristics | P0 - Q2 2026 |
| 4 | **Destination Mapping** | Map Redis patterns to optimal AWS services (ElastiCache, DynamoDB, DocumentDB, OpenSearch, Neptune) | P0 - Q2 2026 |
| 5 | **Schema Design** | Generate schema designs for DynamoDB, DocumentDB, and ElastiCache migrations | P0 - Q2 2026 |

---

## Example Customer Scenarios

### E-commerce Session Store

**Current State:** Customer using Redis for session storage
**Modernization:** Migrate to DynamoDB for better durability and global replication
**Benefits:** Automatic backups, multi-region replication, serverless scaling

### Gaming Leaderboards

**Current State:** Customer using Redis Sorted Sets
**Modernization:** Migrate to DynamoDB with sort keys
**Benefits:** Serverless scaling, no cluster management, lower operational overhead

### Real-time Analytics

**Current State:** Customer using Redis for caching
**Modernization:** Migrate to ElastiCache for Valkey (managed) + DynamoDB (primary store)
**Benefits:** Managed service, automatic failover, reduced operational burden

### Content Management

**Current State:** Customer using Redis for document storage
**Modernization:** Migrate to DocumentDB
**Benefits:** Native JSON querying, indexing, ACID transactions

---

## Data Collection Requirements

### Redis-Specific Metrics

1. **Command Patterns**
   - Command frequency distribution
   - Hot keys identification
   - Command latency percentiles

2. **Data Structures**
   - Type distribution (strings, hashes, lists, sets, sorted sets)
   - Key naming patterns
   - TTL usage patterns

3. **Memory Analysis**
   - Total memory usage
   - Memory by data type
   - Large key identification (>1MB)
   - Memory overhead estimation

4. **Persistence Configuration**
   - RDB snapshot frequency
   - AOF sync policy
   - Replication configuration

5. **Performance Metrics**
   - Operations per second
   - Cache hit rate
   - Eviction rate
   - Connection count

---

## Analysis Considerations

### When to Recommend ElastiCache for Valkey

- High read-to-write ratio (>10:1)
- Sub-millisecond latency requirements
- Existing Redis data structures heavily used
- Caching use case
- Session storage with simple access patterns

### When to Recommend DynamoDB

- Need for durability and automatic backups
- Global replication requirements
- Serverless scaling desired
- Key-value or simple sorted data patterns
- Cost optimization for large datasets

### When to Recommend DocumentDB

- JSON document storage
- Complex queries on nested data
- Need for secondary indexes
- ACID transaction requirements
- Schema flexibility needed

### When to Recommend OpenSearch

- Full-text search requirements
- Log analytics use cases
- Complex aggregations
- Time-series data with search

### When to Recommend Neptune

- Graph data structures (RedisGraph usage)
- Relationship-heavy queries
- Path traversal requirements
- Social network patterns

---

## Migration Patterns

### Pattern 1: Lift and Shift to ElastiCache

**Use Case:** Minimal changes, managed Redis
**Effort:** Low
**Benefits:** Reduced operational overhead, automatic backups, Multi-AZ

### Pattern 2: Refactor to DynamoDB

**Use Case:** Key-value patterns, need for durability
**Effort:** Medium
**Benefits:** Serverless, global tables, lower cost at scale

### Pattern 3: Hybrid Approach

**Use Case:** ElastiCache for caching + DynamoDB for persistence
**Effort:** Medium-High
**Benefits:** Best of both worlds, optimal performance and durability

### Pattern 4: Purpose-Built Migration

**Use Case:** Specialized workloads (search, graph, documents)
**Effort:** High
**Benefits:** Optimal service for specific use case, better features

---

## References

- [ElastiCache for Valkey Documentation](https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/WhatIs.html)
- [DynamoDB Developer Guide](https://docs.aws.amazon.com/dynamodb/)
- [DocumentDB Developer Guide](https://docs.aws.amazon.com/documentdb/)
- [Redis to DynamoDB Migration Guide](https://aws.amazon.com/blogs/database/)

---

**Related Documents:**

- [Database Collection Matrix](database-collection-matrix.md)
- [Architecture](../architecture/high-level-design.md)
