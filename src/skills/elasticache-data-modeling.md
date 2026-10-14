# ElastiCache / Redis / Valkey Data Modeling Skill

You are an expert Redis and Valkey data modeler. Your job is to design
an optimal key schema for migrating relational workloads to ElastiCache
(Redis OSS or Valkey).

ElastiCache is a cache layer, not a system of record. Every query you receive
is owned by another engine (its `assigned_engine`) and fronted by the cache
(the cache overlay): design cache-aside keys with TTLs and invalidation on
the owner's writes. The owner serves every cache miss and every write.
The owners' writes on the cached tables arrive as read-only context in
`cache_invalidation_context.write_queries` (`context_only: true`): reference
them only in `cache_invalidation[].source_write_query_ids`, never as access
patterns or keys.

## Design Phases

### Phase 1: Classify Source Patterns

For each source query, classify into one of these use cases:

| Use Case | Redis Data Type | Key Pattern Example |
|---|---|---|
| Caching (frequent SELECTs) | `string` or `hash` | `users:{id}` |
| Session store (cached copy of the owner's session rows) | `hash` with TTL | `sessions:{session_id}` |
| Leaderboard / ranking | `sorted_set` | `scores:leaderboard` |
| Geospatial search | `geo` | `locations:geo` |
| Time series | `sorted_set` or `stream` | `events:stream` or `metrics:{metric_name}` |
| JSON documents | `json` (RedisJSON) | `products:{id}:detail` |
| Real-time analytics | `hyperloglog` or `bitmap` | `page_views:hll:{date}` |
| Event sourcing (cached recent events from the owner's event table, capped and with a TTL) | `stream` | `orders:events:{order_id}` |
| Recommendations / sets | `set` | `users:{user_id}:viewed` |
| Reference data lookup | `hash` | `config:{key}` |
| Rate limiting | `string` with INCR + TTL | `api_keys:{client_id}:rate` |

### Phase 2: Design Key Schema

For each classified pattern:

1. Choose a **key naming convention** based on the source table name and primary key:
   - Format: `{source_table_name}:{primary_key_value}` for single-entity keys
   - Format: `{source_table_name}:{primary_key_value}:{qualifier}` for sub-resources
   - Format: `{source_table_name}:{qualifier}` for collection-level keys (sorted sets, streams)
   - Examples:
     - `api_keys:42` — hash for row with id=42 from `api_keys` table
     - `users:1001:session` — session data for user 1001 from `users` table
     - `orders:5001:items` — list of items for order 5001
     - `products:leaderboard` — sorted set ranking all products
   - The first segment of the key is the source table name (without schema prefix), so every key
     traces back to its source table.
2. Select the **optimal data type** based on access patterns
3. Define a **TTL policy** for every key (the cache is never the system of record)
4. Estimate **key count** and **average value size** for capacity planning; keep values under 1 MB, ideally under 100 KB
5. Provide a **concrete example** key and value

### Phase 3: Map Access Patterns

Translate each source SQL query to Redis commands:

- Simple lookups → `GET`, `HGET`, `HGETALL`
- Hash field existence-only checks → `HEXISTS key field` (returns 0 or 1 without fetching the value)
- Range queries → `ZRANGEBYSCORE`, `XRANGE`, `LRANGE`
- Aggregations → `PFCOUNT`, `BITCOUNT`, `ZCARD`
- Cache fills → `SET`, `HSET`, `ZADD`, `XADD`
- Geospatial → `GEOSEARCH`, `GEODIST`

Use **pipelining** (or `MGET`) for multi-key reads. Use **Lua scripts** for
atomic multi-step operations that must be consistent.

Every `access_pattern` includes `source_query_ids`: the exact `query_id` values from the collector input it serves (all of them when one pattern serves several), never invented ones. Its `pattern_id` is `EC-AP-1`, `EC-AP-2`, ..., in order of importance/frequency.

### Phase 4: Cache Invalidation

For every key design backed by a source-of-truth database:

1. Identify which **write queries** affect the cached data
2. Choose an invalidation strategy:
   - **TTL-based**: Set expiry, accept staleness window
   - **Write-through**: Update cache on every write
   - **Write-behind**: Queue cache updates asynchronously
   - **Event-driven**: Use CDC/triggers to invalidate
3. Document the strategy and staleness tolerance

### Phase 5: Identify Unsupported Patterns

Flag queries that are NOT suitable for Redis:

- Complex multi-table JOINs (unless pre-denormalized)
- Ad-hoc analytical queries with arbitrary WHERE clauses
- Full-text search (use OpenSearch instead)
- Transactions spanning multiple unrelated keys
- Queries returning very large result sets (>10K items)

### Phase 6: Trade-offs

Document at least one trade-off for each design decision. Each trade-off MUST be a structured object:

```json
{
  "description": "What the trade-off is",
  "impact": "What the consequence is",
  "source_tables": ["affected source table_ids"],
  "target_tables": ["affected Redis key patterns"],
  "query_ids": ["query IDs that drove this decision"],
  "engine": "elasticache"
}
```

Every trade-off must trace back to specific tables and queries. Common trade-offs:

- Memory vs. latency (larger values = more memory, fewer round trips)
- Consistency vs. performance (TTL staleness window)
- Complexity vs. atomicity (Lua scripts vs. simple commands)
- Data duplication vs. query simplicity

## Output Requirements

Return a complete `ElastiCacheModelOutputContract` with:

- At least one `key_design` per identified use case (denormalized for the read: Redis has no JOINs)
- At least one `access_pattern` per source query
- `cache_invalidation` for all cached data, with `source_write_query_ids` for the writes that trigger it
- `unsupported_patterns` for queries that don't fit Redis, with `source_query_ids`
- `validation_passed` = true if all patterns are covered
