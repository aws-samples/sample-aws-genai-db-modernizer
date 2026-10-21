# Database Modernization: Engineering Report

Source database: `discourse`. This is the build companion to the executive report (`executive-report.pdf`): the source-to-target mapping, the per-engine target schemas, and the query groups.

## Executive summary

No executive summary was generated for this run.

*Engine and query assignments are produced deterministically. No language model decides which engine a table or query goes to. The executive summary is written over already-computed results and cannot change a recommendation.*

## Target engines

| Engine | Role | Workload | Scope | Why |
|---|---|---|---|---|
| Aurora PostgreSQL | Migration target | 76.5% | 0 source tables | - |
| DynamoDB | Migration target | 23.3% | 0 source tables | - |
| OpenSearch | Search read model | 0.2% | synced from owners | - |
| Total | | 100% | 0 tables migrate | |

Queries moved: 1654 of 1654 queries move to a new engine (a migration target or a search read model); the rest stay on the engine that runs them today.

**OpenSearch** is a search read model: it serves its queries from data synced from the engines that own those tables, holds no system-of-record data, and is built after its owners. The migration moves the 0 tables assigned to Aurora PostgreSQL, DynamoDB.

*Workload fit: how closely this engine's capabilities match the access patterns of the queries routed to it. It is not a probability that the migration will succeed.*

## Migration roadmap (3 waves)

*This is one suggested adoption path. To modernize in one step instead, adopt the target architecture in the Target engines table above directly.*

### Wave 1: Move to Aurora PostgreSQL

- Engines: Aurora PostgreSQL
- 1266 queries \(76.5% of query patterns\)
- Moves from: the source PostgreSQL database
- Why: The whole source database moves to Aurora PostgreSQL first, schema carried over 1:1 \(every collected table and view\): a homogeneous migration \(same engine family\). 1266 queries \(76.5% of query patterns\) are still assigned to Aurora PostgreSQL once every later wave in this roadmap has moved its own share away; every later wave's table count is a subset of this wave's tables, never an addition to them.
- Gate before the next wave: Schema and data parity validated against the source database before cutover; decommission the legacy source once replication lag is zero.

### Wave 2: Move key-value and point-lookup queries to DynamoDB

- Engines: DynamoDB
- 385 queries \(23.3% of query patterns\)
- Moves from: Aurora PostgreSQL
- Why: 385 queries \(23.3% of query patterns\) to DynamoDB.
- Gate before the next wave: Query parity confirmed before the next wave.

### Wave 3: Sync search and analytics read models to OpenSearch

- Engines: OpenSearch
- 3 queries \(0.2% of query patterns\)
- Synced from (durable owner): Aurora PostgreSQL
- Why: 3 search/analytics queries \(0.2% of query patterns\) build a read model in OpenSearch. The indexed tables could not be resolved from the table mappings, so Aurora PostgreSQL is treated as the owner of record until they can be.
- Gate before the next wave: Sync lag inside SLA and every served table still has a durable owner.

## Risk posture

- Overall risk: not assessed
- No risks were identified in this analysis.

*No open risks is not the same as no migration risk: this analysis reads schema and query patterns only and does not model traffic bursts, peak load or compliance requirements.*

---

Assignments are deterministic. The complete machine-readable assessment is the Assessment Data (JSON) artifact.
