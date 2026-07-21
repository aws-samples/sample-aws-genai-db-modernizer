# ADR-023: Context Graph Layer

**Status:** Experimental
**Date:** 2026-06-29
**Deciders:** Database Modernizer Assessment Architecture Team

## Context

The modernization pipeline produces relational data across its stages. Queries get assigned to engines, engines host destinations (tables, collections, indexes, key patterns), decisions carry reasoning, load tests validate performance, and agents interact with humans for design choices.

Today this data lives in flat JSON artifacts in S3 (ADR-016, ADR-019). That works for sequential pipeline execution but falls short for:

1. **Relationship exploration.** "Which queries connect the orders table to the customers collection in DocumentDB, and why?" requires joining 3+ artifacts and scanning linearly.
2. **Impact analysis.** "If I change the partition key for the orders table, which queries are affected?" is a graph traversal, not a file scan.
3. **Visualization.** The Visual Explorer and future UIs need pre-computed relationship data that doesn't exist in a single artifact.
4. **Decision provenance.** "Why was this query assigned to DynamoDB instead of DocumentDB?" requires tracing through assignment reasoning, signals, and co-dependency groups spread across multiple files.

ADR-019 (Query Journey Materialization) solves the per-query detail view, but it's still a document-per-query model. It can't answer multi-hop relationship questions efficiently.

## Decision

Introduce LadybugDB as an embedded graph database layer alongside the existing pipeline. The graph lives on the same node as the API, populated from S3 artifacts. Each pipeline stage triggers a graph rebuild or incremental update after producing its artifacts.

### Principles

1. The graph layer is additive. Existing S3 artifacts, API endpoints, and pipeline stages stay unchanged. The graph is populated *from* those artifacts, not instead of them.
2. LadybugDB runs embedded in the API process. No separate database server, no sidecar container, no additional ECS tasks.
3. The graph models what happened to queries (assignment, migration, testing), not which agent did what. Agent provenance is an optional edge layer you can add later.
4. In future iterations, the graph may become the primary read path for the UI. Initially, it's a parallel materialization that can be rebuilt from S3 artifacts at any time.
5. Cypher is the query language. If the project outgrows an embedded database, queries port directly to Neo4j or Neptune (openCypher endpoint) without code changes.

### Why LadybugDB

LadybugDB is an embedded columnar graph database (MIT license, open source). It's the community successor to Kuzu after Apple acquired and archived that project. Same architecture, same Cypher support, actively maintained.

| Criteria | LadybugDB | Neptune | Neo4j (sidecar) | NetworkX |
|----------|-----------|---------|-----------------|----------|
| Query language | Cypher | Cypher/Gremlin/SPARQL | Cypher | Python API |
| Deployment | Embedded (pip install) | Managed service | Separate container | In-memory only |
| Persistence | Disk-backed | Managed | Disk-backed | None |
| Cost | $0 | $0.10+/hr minimum | $0 (+ RAM overhead) | $0 |
| Infrastructure | None (same process) | VPC, IAM, cluster | Sidecar container | None |
| RAM overhead | ~50-100MB for <5K nodes | N/A | 768MB+ minimum | ~5MB |
| Portability | Cypher queries portable | N/A | Same queries | Rewrite needed |

LadybugDB fits because:

- Embedded. Runs in the API process, no additional containers or services to deploy.
- Cypher. Queries are portable to Neo4j or Neptune if scale demands it later.
- Disk-backed. Graph persists across API restarts without rebuild.
- MIT licensed. No commercial restrictions.
- Lightweight. For <5K nodes, the memory and disk footprint is negligible.

### Deployment Model

The graph database lives on the same node as the API. This simplifies both local development and ECS deployment.

**Local development.** The API process opens the LadybugDB database directory directly. The pipeline (running on the same machine) populates the graph after each stage completes. No configuration needed beyond the artifact directory path.

**ECS deployment.** The API task holds the graph on its local storage (or EFS if persistence across task replacements is needed). When the pipeline completes a stage, the API receives a notification (EventBridge event or polling) and rebuilds/updates the graph from the latest S3 artifacts. The graph is always rebuildable from artifacts, so losing the local copy on task replacement is acceptable (rebuild takes milliseconds for <5K nodes).

```
Pipeline stage completes → writes S3 artifact → notifies API
API receives notification → reads artifact from S3 → updates graph in-process
API serves graph queries → reads from local embedded DB → no network hop
```

### Node Schema

```
Query {
  id: STRING,                    -- query_id from collector
  sql_text: STRING,              -- original SQL
  calls_per_second: FLOAT,       -- traffic volume
  operation_type: STRING,        -- SELECT/INSERT/UPDATE/DELETE
  in_scope: BOOL                 -- from assignment (false = out of scope)
}

SourceTable {
  id: STRING,                    -- table name (e.g., "orders")
  database: STRING,              -- source database name
  row_estimate: INT64            -- estimated row count if available
}

Destination {
  id: STRING,                    -- e.g., "orders-by-customer"
  engine: STRING,                -- dynamodb | documentdb | opensearch | elasticache | aurora
  artifact_type: STRING,         -- table | collection | index | key_pattern
  artifact_name: STRING          -- the actual name in the target engine
}

Engine {
  id: STRING,                    -- e.g., "dynamodb"
  display_name: STRING           -- e.g., "Amazon DynamoDB"
}

Signal {
  id: STRING,                    -- e.g., "high-write-ratio"
  category: STRING,              -- access_pattern | data_shape | operational
  description: STRING
}

CoDependencyGroup {
  id: STRING,                    -- group identifier
  reason: STRING                 -- why these queries are grouped
}

Decision {
  id: STRING,                    -- unique decision identifier
  category: STRING,              -- partition_key | consolidation | trade_off | index | override
  description: STRING,
  rationale: STRING,
  phase: STRING,                 -- pipeline phase that produced this
  metadata: STRING               -- JSON blob for category-specific data
}

LoadTestRun {
  id: STRING,                    -- run identifier
  timestamp: STRING,             -- ISO datetime
  query_id: STRING,              -- which query was tested
  source_latency_ms: FLOAT,
  target_latency_ms: FLOAT,
  improvement_factor: FLOAT,
  throughput_rps: FLOAT,
  error_rate_pct: FLOAT,
  cost_per_operation_usd: FLOAT
}
```

### Edge Schema

```
(Query)-[READS_FROM]->(SourceTable)
  -- Which source tables does this query access?

(Query)-[MIGRATES_TO {
  confidence: FLOAT,
  assignment_reason: STRING
}]->(Destination)
  -- Where does this query end up after migration?

(Query)-[EMITS_SIGNAL {
  strength: FLOAT
}]->(Signal)
  -- What access pattern characteristics does this query exhibit?

(Query)-[MEMBER_OF]->(CoDependencyGroup)
  -- Which queries must be assigned to the same engine?

(Query)-[TESTED_IN]->(LoadTestRun)
  -- Which load test runs validated this query?

(LoadTestRun)-[VALIDATES]->(Destination)
  -- Which destination was the load test run against?

(Destination)-[HOSTED_ON]->(Engine)
  -- Which engine hosts this destination artifact?

(Decision)-[AFFECTS]->(Destination)
  -- Which destinations were shaped by this decision?

(Decision)-[INFORMED_BY]->(Query)
  -- Which queries informed this design decision?

(Decision)-[SUPERSEDES]->(Decision)
  -- Which decision replaced a previous one? (versioning)
```

### Schema Design Rationale

**Traffic representation.** Traffic (`calls_per_second`) is a property on the `Query` node. Table-level traffic is computed on read by aggregating across all queries that `READS_FROM` a given `SourceTable`. At this scale (hundreds to low-thousands of queries), that aggregation is sub-millisecond and does not need materialization.

**Destination abstraction.** The `Destination` node uses `artifact_type` to distinguish DynamoDB tables from DocumentDB collections from OpenSearch indexes from ElastiCache key patterns. This avoids engine-specific node types while preserving semantic clarity.

**Load test as separate node.** `LoadTestRun` is a first-class node rather than edge properties. This supports multiple test runs per query. Even if there's typically only one run, you can compare results across iterations without schema changes.

**Single Decision type with metadata.** Rather than subtyping decisions (PartitionKeyDecision, ConsolidationDecision, etc.), there's one `Decision` node with a `category` field and a `metadata` JSON blob for category-specific properties. Traversal is always by relationship (`AFFECTS`, `INFORMED_BY`), not by node label. Queries stay uniform regardless of decision type.

**No materialized MAPS_TO edge.** The aggregate relationship between `SourceTable` and `Destination` (how many queries flow from one to the other) is computed on read:

```cypher
MATCH (q:Query)-[:READS_FROM]->(st:SourceTable {id: $table_id}),
      (q)-[:MIGRATES_TO]->(d:Destination)
RETURN d.id, d.engine, COUNT(q) AS query_count, SUM(q.calls_per_second) AS total_traffic
```

At this data volume, that's a sub-millisecond query. No need to materialize it.

**Agent-agnostic core with optional provenance.** The graph does not model which agent produced which artifact. If you need provenance tracing later, add:

```
(Decision)-[PRODUCED_BY {phase: STRING}]->(Agent)
(LoadTestRun)-[EXECUTED_BY]->(Agent)
```

This keeps the core graph clean for consumers (UI, reports) while allowing debugging traces when needed.

### Population Strategy

The graph is populated on the API node after each pipeline stage completes. The API reads the relevant S3 artifact and updates the graph in-process:

| Pipeline Stage | Nodes Created | Edges Created |
|---------------|---------------|---------------|
| COLLECT_TRIAGE | Query, SourceTable | READS_FROM |
| ANALYSIS | Signal | EMITS_SIGNAL |
| ASSIGNMENT | Destination, Engine, CoDependencyGroup | MIGRATES_TO, HOSTED_ON, MEMBER_OF |
| SCHEMA_DESIGN | Decision | AFFECTS, INFORMED_BY |
| LOAD_TEST | LoadTestRun | TESTED_IN, VALIDATES |
| ASSIGNMENT_REVIEW | Decision (overrides) | SUPERSEDES |

Each populator is idempotent. Re-running it with the same artifact data produces the same graph state. You can rebuild the graph from scratch by replaying all artifacts in order.

For a full rebuild of a 3,500-query assessment (the largest workload tested), expect < 2 seconds. This is fast enough to run on API startup or on-demand when an assessment completes.

### Storage

**Local mode.** LadybugDB database directory at `./artifacts/{db_name}/{job_id}/graph/`. Both the API and pipeline access this directly since they run on the same machine.

**ECS mode.** Graph stored on the API task's local volume (ephemeral storage or EFS mount). Rebuilt from S3 artifacts when needed. The graph is disposable since artifacts are the source of truth.

**Rebuild.** A `rebuild_graph.py` script re-reads all S3 artifacts and regenerates the graph from scratch. Used for schema migrations, disaster recovery, or backfilling graphs for assessments that ran before this feature existed.

### Portability

All graph queries are standard Cypher. If the project outgrows an embedded database:

1. Swap LadybugDB for Neo4j (change the connection URI, keep all queries)
2. Swap for Neptune openCypher endpoint (same queries, managed infrastructure)

The Python driver interface is compatible across all three. Application code stays the same.

## Alternatives Considered

### Extend Query Journey files (ADR-019)

Add relationship data to per-query JSON files so each file includes its connections to other queries, tables, and decisions.

Rejected. Multi-hop queries ("which queries are 2 hops from this table?") would require reading ALL journey files. Relationship data is bidirectional, so updating one query's relationships means updating all connected queries' files too. Query journey files are optimized for single-query detail views, not relationship traversal.

### Neptune (managed graph database)

Use Amazon Neptune for a production-grade graph database with SPARQL/Gremlin/Cypher support.

Rejected for now. Minimum cost ~$0.10/hr ($73/month) even when idle. Requires VPC configuration, IAM roles, and network access from the pipeline. Overkill for the data volume (hundreds to low-thousands of nodes per assessment). Adds operational complexity for a feature that's still experimental. Viable upgrade path if the graph layer proves valuable and scale demands grow.

### Neo4j Community (sidecar container)

Run Neo4j as a sidecar container in the same ECS task definition.

Rejected. Adds 768MB+ RAM overhead minimum. Requires container health checks, volume mounts, and Bolt protocol configuration. For <5K nodes, the overhead of running a full database server is disproportionate. The embedded approach gives the same Cypher queries without the container complexity.

### NetworkX (in-memory graph)

Use Python's NetworkX library for in-memory graph operations.

Rejected. No query language (traversals are imperative Python code, not portable). No persistence, so the graph must be rebuilt on every process start. Fine for one-off analysis scripts, but doesn't give you Cypher for future portability.

### Property store in DynamoDB

Store nodes and edges in DynamoDB tables with GSIs for traversal.

Rejected. Graph traversal in DynamoDB requires multiple round trips (one per hop). No native graph query language, so you'd implement traversal logic manually. Adds infrastructure cost for a use case that graph databases solve natively.

## Consequences

### What you gain

- "Show all queries connecting orders to customers" is one Cypher query, not a multi-file join
- "What breaks if I reassign this table?" is a graph traversal
- Visual Explorer can query the graph directly instead of client-side data transformation
- Zero infrastructure cost. LadybugDB is a pip dependency, not a service.
- No additional containers or ECS tasks. The graph lives on the API node.
- The graph is a materialized view of S3 artifacts. If lost, rebuild from source in seconds.
- Existing endpoints keep working. The graph is consumed only by new features initially.
- Cypher queries are portable. If you outgrow embedded, swap to Neo4j or Neptune without rewriting queries.

### What you take on

- `ladybugdb` becomes a new dependency
- The API node now holds state (the graph directory). Task replacements require a rebuild from artifacts.
- Changes to the graph schema require migration scripts or rebuilds (mitigated by the rebuild-from-artifacts capability).
- If the graph layer doesn't prove valuable, it becomes dead code that must be removed.

### Risks

| Risk | Mitigation |
|------|-----------|
| LadybugDB project becomes inactive | Cypher queries are portable. Swap to Neo4j or Neptune. The graph is rebuildable from S3 artifacts regardless of which database runs it. |
| Graph diverges from S3 artifacts | Rebuild script validates graph against artifacts. Can run as a CI check or on every API deployment. |
| Performance degrades at scale | Unlikely for <5K nodes per assessment. If it happens, swap to Neo4j sidecar or Neptune. Queries don't change. |
| API task replacement loses graph | Rebuild from artifacts on startup. Takes <2 seconds for the largest workloads. Acceptable cold-start cost. |

## References

- [ADR-016: Compute and Orchestration Strategy](ADR-016-compute-and-orchestration-strategy.md) (S3 artifact path conventions)
- [ADR-019: Query Journey Materialization](ADR-019-query-journey-materialization.md) (per-query detail view, complementary)
- [LadybugDB](https://ladybugdb.com/) (embedded columnar graph database, MIT license)
- `src/contracts/assignment_models.py` (QueryAssignment, TableAssignment, CoDependencyGroup)
- `src/contracts/load_test_models.py` (PatternResult, LoadTestOutput)
- `src/contracts/agent_interaction_models.py` (AgentQuestion, AgentAnswers)
- `src/contracts/schema_design_output.py` (TradeOff, SchemaDesignOutputBase)
