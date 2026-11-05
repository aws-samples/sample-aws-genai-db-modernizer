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
  row_estimate: INT64,           -- estimated row count if available
  placeholder: BOOLEAN,          -- a collector's name for "no table" ('unknown', a dummy table such as DUAL), #306
  placeholder_reason: STRING     -- why it is a placeholder; null for a real table
}

Destination {
  id: STRING,                    -- the query's engine in the effective assignment (#306)
  engine: STRING,                -- dynamodb | documentdb | opensearch | elasticache | aurora_mysql | aurora_postgresql
  artifact_type: STRING,         -- engine
  artifact_name: STRING          -- the engine's display name
}

TargetObject {
  id: STRING,                    -- "<engine>:<kind>:<name>@v<schema version>", "<engine>:<kind>" for a fallback
  engine: STRING,
  kind: STRING,                  -- table | gsi | collection | index | key_pattern | not_designed | not_in_design
  name: STRING,                  -- the name in the design, or the fallback's label
  parent: STRING,                -- a GSI's table
  schema_version: INT64,         -- null for a fallback
  fallback: BOOLEAN
}

Engine {
  id: STRING,                    -- e.g., "dynamodb"
  display_name: STRING,          -- e.g., "Amazon DynamoDB"
  monthly_cost_usd: DOUBLE,      -- the effective report's facts.engines figure, never recomputed (#306)
  role: STRING, rationale: STRING, workload_percent: DOUBLE,  -- the same report's facts.engines
  cost_version: INT64            -- the assignment version that report was built from
}

Wave {
  id: STRING,                    -- "v<assignment version>:wave-<n>" (#306)
  version: INT64, effective: BOOLEAN,
  wave: INT64, title: STRING, rationale: STRING, gate: STRING,
  engines_json: STRING, migrating: BOOLEAN, query_count: INT64,
  cutover: BOOLEAN,              -- the plan's cutover wave: a first wave with a single engine whose cutover count is every moving query
  table_groups_json: STRING      -- the wave plan's table groups
}

MoveUnit {
  id: STRING,                    -- "v<assignment version>:unit-<i>" (#306)
  version: INT64, effective: BOOLEAN,
  unit: STRING, size: INT64      -- a placement unit routing places as one (ADR-031 Amendment 9)
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

(Query)-[WRITES {locking}]->(SourceTable)
  -- Which source tables does this query write or lock? (#306)

(Query)-[MOVES_IN | CUTS_OVER_IN | CACHED_IN {effective}]->(Wave)
(Wave)-[INCLUDES]->(Engine)
  -- Which wave does this query move in, cut over in first, or get a cache in front in? (#306)

(Query)-[MEMBER_OF]->(MoveUnit)
  -- Which queries does routing place together? (#306)

(SourceTable)-[SOURCE_OF_TRUTH {version, effective}]->(Engine)
(Query)-[READS_REPLICA {version, effective, engine}]->(SourceTable)
  -- Which engine holds this table's writes, and which reads use a replica? (#306)

(Query)-[CACHED_BY {version, effective, pattern, reason, dropped}]->(Engine)
  -- Which cache fronts this query, for which pattern? (#306)

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
(TargetObject)-[HOSTED_ON]->(Engine)
  -- Which engine hosts this destination artifact?

(Query)-[TARGETS {version, schema_version, effective, pattern_ids_json}]->(TargetObject)
  -- Which design objects does this query land on, per assignment version? (#306)

(Decision)-[AFFECTS]->(Destination)
  -- Which destinations were shaped by this decision?

(Decision)-[INFORMED_BY]->(Query)
  -- Which queries informed this design decision?

(Decision)-[SUPERSEDES]->(Decision)
  -- Which decision replaced a previous one? (versioning)

(AssignmentVersion)-[SUPERSEDES]->(AssignmentVersion)
  -- Which assignment version did this one replace? (#306)

(AssignmentVersion)-[RESTORES]->(AssignmentVersion)
  -- Which version does this rollback restate? (#306)

(AssignmentVersion)-[AFFECTS {role}]->(Query | Engine)
(Decision)-[AFFECTS {role}]->(Engine)
  -- Which queries did this version's Moves change, and which engines did they
  -- leave (role 'from') or land on ('to')? Which engines does a consolidation
  -- or reroute move queries between? (#306)

(Query)-[SCORED {version, score, gate, assigned, current, ladder_version, move_seq, pass, ladder_copied_from, dimensions_json}]->(Engine)
  -- What did routing score this query on every engine, per version? (#306)
```

### Schema Design Rationale

**Traffic representation.** Traffic (`calls_per_second`) is a property on the `Query` node. Table-level traffic is computed on read by aggregating across all queries that `READS_FROM` a given `SourceTable`. At this scale (hundreds to low-thousands of queries), that aggregation is sub-millisecond and does not need materialization.

**Waves, units, sources of truth, cache layer and cost (#306).** These come from each lineage version's assignment, access-pattern record and synthesis report, built by the graph explorer's own overlay code (`explorer_overlays.build_overlays`, on the inputs `read_overlays` reads), so the graph and the explorer cannot disagree. The effective version uses the report `read_overlays` reads (the versioned report, then the legacy keys); an older version only its own `synthesis/v<N>/report.json`, so a version without a report has no waves or sources of truth, while its cache layer and units, which come from its assignment, are there; `AssignmentVersion.has_report` says which. `Wave` and `MoveUnit` nodes carry their assignment version (in their id and `version`) and `effective`; `SOURCE_OF_TRUTH`, `READS_REPLICA` and `CACHED_BY` edges carry `version` and `effective`; `MOVES_IN`, `CUTS_OVER_IN` and `CACHED_IN` carry `effective` and take their version from their wave; `INCLUDES` and the `MoveUnit` `MEMBER_OF` take it from their node. A wave is the wave plan's (`migration_waves`, with its table groups) with `migrating` from `facts.waves`; a query `MOVES_IN` the wave of its engine and is `CACHED_IN` its cache wave. When the plan's first wave has a single engine and its `cutover_query_count` equals the number of queries that move (`explorer_overlays._cutover`, #1756's path rule; all three samples' plans qualify), every query that moves on later also `CUTS_OVER_IN` it first. A `MoveUnit` is a placement unit of two or more queries (`assignment_placement_groups`); `CoDependencyGroup` stays the assignment's co-dependency groups. `SOURCE_OF_TRUTH` and `READS_REPLICA` are the report's `table_sources`; the writers are `WRITES`, which `tests/e2e/test_graph_explorer_figures.py` holds equal to the report's writers. `CACHED_BY` is each query's `cache_engine` with its `cache_pattern`, `cache_reason` and `cache_dropped`. An engine's `monthly_cost_usd`, `role`, `rationale` and `workload_percent` are the effective report's `facts.engines` figures, and `cost_version` is the assignment version that report was built from (older than the effective version when synthesis has not run since a reroute). A query's `access_pattern_class` and `access_pattern_reason` are the effective version's access-pattern record. The explorer still reads these through its overlays (switching every explorer read onto the graph is its own change); `tests/graph/test_overlays_graph.py` holds the graph equal to `read_overlays` on real fixtures, and `tests/e2e/test_graph_complete.py` checks every node and edge type and how they relate on every sample.

**Read/write split (#306).** `READS_FROM` links a query to every table it accesses (`tables_accessed`): an UPDATE or DELETE reads the rows it changes and a locking read reads what it locks, so a table a query only reads is one with `READS_FROM` and no `WRITES`. Keeping `READS_FROM` on every accessed table also keeps the readers that count a table's queries through it (the blast radius, the hotspots, the explorer's bundles) unchanged. `WRITES` links a query to every table it writes or locks, computed by routing's own one-writer rule (`write_groups.written_tables`, ADR-031 Amendment 9) in the collection's source dialect, so the graph never disagrees with routing; `locking` marks a table the statement locks by a locking read and does not write (an `INSERT ... SELECT ... FOR UPDATE` target is a write, the tables its read locks are locking). It is named like the `WRITES` edge planned from procedures and triggers to tables (#1677), which can add its pair to the same relationship. A source table named by a collector placeholder (`placeholder_tables.is_placeholder_table`: exactly one of `PLACEHOLDER_TABLE_NAMES`, `unknown` when no table resolved or a dummy table of a statement with no `FROM`, or an empty name) stays in the graph, flagged `placeholder` with its reason. The graph copies routing here too: `written_tables` never counts a placeholder as written (#1817), so a placeholder never gets `WRITES`, and a real table that only looks like one (`Null`, `Unknown`) is a table like any other.

**Destination abstraction.** A query's `Destination` is the engine it migrates to in the effective assignment (`artifact_type` `engine`). The design's own objects (DynamoDB tables and GSIs, DocumentDB collections, OpenSearch indexes and data streams, ElastiCache key patterns, Aurora tables) are `TargetObject` nodes told apart by `kind`, which avoids engine-specific node types while preserving semantic clarity; see the next paragraph.

**Versioned target objects (#306).** A query's `Destination` is its engine in the effective assignment (`id` the engine id, `artifact_type` `engine`), with one `MIGRATES_TO` per query, so every reader of `MIGRATES_TO` keeps its one-edge-per-query shape. It used to be guessed from the query's first source table, which was wrong for about a quarter of the queries. The design's real objects are `TargetObject` nodes, one `kind` per engine contract: a DynamoDB `table_name`, or the `gsi_name` on it; a DocumentDB `collection_name`; an OpenSearch `index_or_stream`; an ElastiCache `key_pattern`; or the Aurora `table_definitions` an access pattern's `source_tables` name, matched as the Aurora derivation matches them. Each is at the schema version it comes from, and engine-specific node types stay out. `TARGETS` links each query of each lineage version to the objects of the engines it uses in that version (its assigned engine and its cache engine), from `schema-<engine>/v<N>` for that version N. An object counts only when the design defines it: a DynamoDB table in `table_definitions` and a GSI in that table's `gsis`, a DocumentDB `collections` entry, an OpenSearch `index_designs` or `data_stream_designs` entry, an ElastiCache `key_designs` entry, an Aurora `table_definitions` entry; an access pattern marked `in_scope: false` serves none of its queries, as the scope check reads it. A query can target several objects in one version, and the effective version's edges are `effective`. With no design for an engine at that version, the query targets the `not_designed` fallback ("not designed yet"); with a design that marks its patterns out of scope or lists it as unsupported, the `out_of_scope` fallback ("the design does not serve it"); with a design that has no access pattern for it, or names an object it does not define, the `not_in_design` fallback ("no target in the design"). A fallback is never a guessed object. A `TARGETS` edge is `cache` when the engine is the query's cache engine in that version and not its assigned engine. The engine detail view and catalog entry B read the effective version's edges with one counting rule: an engine's objects count every effective query that lands on them, assigned or cache, and each count says how many of those are cache. Engine detail returns, per object and per fallback, `query_count` and `cache_query_count`, with a GSI's `parent` (the table's name) and `parent_id` (its `target_id`); it keeps `destinations`, the engine's `Destination` with the source tables and access patterns of the queries assigned to it, so a cache-only engine has none. Catalog B returns one row for the engine: `queries` and `cache_queries` over all the queries that land on it, the `targets` and `fallbacks` ids, and `target_counts`, the `{id, queries, cache_queries}` of each, with the source tables and access patterns of those queries; its engine choices are the engines with a `TargetObject`, so a cache-only engine is reachable. A load test `VALIDATES` the Destination of its own engine, so a cache load test validates the cache layer, not the engine the query is assigned to.

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
| COLLECT_TRIAGE | Query, SourceTable (placeholders flagged, #306) | READS_FROM, WRITES (#306) |
| ANALYSIS | Signal | EMITS_SIGNAL |
| ASSIGNMENT | Destination (the query's engine), Engine, CoDependencyGroup, GraphMeta, AssignmentVersion (one per version of the effective lineage, #306), TargetObject (each version's design objects and fallbacks, #306) | TARGETS (each query to its design objects per version), MIGRATES_TO, HOSTED_ON, MEMBER_OF, MOVED (one per move-ledger Move of every version in the effective lineage, #339), SUPERSEDES and RESTORES between versions, AFFECTS from a version to its Moves' queries and engines, SCORED (each query's candidate ladder per version) |
| REALITY_CHECK | Decision (consolidation) | INFORMED_BY (to the queries its ledger Moves moved), AFFECTS (to the engines it moves from and to) |
| POST_SCHEMA_ROUTER | Decision (reroute) | INFORMED_BY (to its query), AFFECTS (to the engines it moves from and to) |
| SCHEMA_DESIGN | Decision | AFFECTS, INFORMED_BY |
| LOAD_TEST | LoadTestRun | TESTED_IN, VALIDATES |
| ASSIGNMENT_REVIEW | Decision (overrides) | SUPERSEDES |
| SYNTHESIS (overlays) | Wave, MoveUnit, Engine cost and facts, Query access-pattern class (#306) | MOVES_IN, CUTS_OVER_IN, CACHED_IN, INCLUDES, MEMBER_OF, SOURCE_OF_TRUTH, READS_REPLICA, CACHED_BY (per assignment version) |
| SYNTHESIS | Risk: the report's open risks (`status = 'active'`) and the risks the assignment resolved (`status = 'resolved'`, #306) | IMPACTS (to its tables), EVIDENCED_BY (to each query it names that has a Query node) |

Analysis, schema design and load test are read for every active engine in the rule catalog (Aurora included). Schema design and load test are read at the effective assignment version only: `schema-<engine>/v<N>` and `load-test-<engine>/v<N>`, where N is the version the assignment resolver picks (v1 when there is no assignment yet). A design of an older or abandoned version is never read (#306). A rollback restates the designs of the version it restores on its own version; a design that fails its output contract is not restated, so that engine has no design in the graph, as in synthesis. A rollback does not restate load tests: the load test runs after post-schema routing, so the restored version has none in a normal run, and a load test run on it earlier is not read after the rollback.

The version chain follows the move ledger and assignment versioning (ADR-028, #306). An `AssignmentVersion` records the pass that made it (`kind`): a rollback from its `status`; otherwise from its `source` (first assignment, reality check, post-schema reroute, source-of-truth reroute, auto-decided, or a customer edit). A version written before `source` existed has no Moves either, so it is read from the legacy shapes ADR-028's own readers use (`is_reality_check_produced`, then an override edit's int `previous_version`, else a first assignment). A customer edit is `customer_scope` when no query changed engine and only the in-scope set did, else `customer_pick`. It also records its Moves' passes and actors, whether it is the effective version and its timestamp. `AFFECTS` and `SUPERSEDES` keep their meaning and gain versioned pairs, rather than new relationship names. A query whose Moves in a version changed its engine is `AFFECTS` role `moved`; one whose Moves changed no engine (a cleared tie) is role `tie` and adds no engine edge. A rollback version `RESTORES` the version it restates: the one the abandoned reroute (`rolled_back_version`) came from; only a rollback records `rolled_back_version`. A consolidation's queries are the queries whose net engine change over the Moves of the version the reality check wrote (first engine to last, in `seq` order) goes from its from engine to its to engine: the per-query count its record's `query_count` holds. A query consolidated A to B and flagged on to C in the same version counts under A to C.

`SCORED` edges carry each query's candidate ladder per assignment version, as routing recorded it (#306). A query's ladder is the one its last Move with recorded candidates (`evidence.candidates`) holds, in that version or an earlier one: a customer pick, its group propagation, an auto pick or a source-of-truth reroute records no candidates, so the query keeps its earlier ladder, and each edge says which version and Move the ladder came from (`ladder_version`, `move_seq`, `pass`). Only the last ladder of a version is kept as edges, so each query, version and engine has one edge; an earlier Move's ladder in the same version stays in that MOVED edge's evidence. `pass` is that Move's own pass, as on its MOVED edge. A rollback's Moves copy the abandoned reroute's candidates: `ladder_copied_from` names the reroute version they came from, so its scores are the reroute's, not a fresh scoring. Edges are written for each version a query has a Move in, and for every query in the effective version (`current`). An edge has the rung's score (null when a gate excluded the engine, with the `gate`); `assigned` is the engine the version's own assignment gives the query, never read from the ladder. `dimensions_json` holds the score dimensions the version's access-pattern record holds for that query and engine (#1471: pattern, cost, base, penalty, fit, method, and per table complexity, performance and weights), read through the record writer's own reader, only when the record's score for them (its `fit`, else its `base`) equals the edge's score, the rule the detailed report's facts use: a reroute or rollback version carries the previous record's dimensions while its scores come from the router, so its edges get none, as does a record written before them (schema version 3 or older). Nothing is rescored, and routing never reads these edges.

Each populator is idempotent. Re-running it with the same artifact data produces the same graph state. You can rebuild the graph from scratch by replaying all artifacts in order.

Measured on a developer laptop, a full rebuild of the discourse sample (1,654 queries, two assignment versions) takes about 1.2 s without SCORED edges and about 1.9 s with them (16,540 SCORED edges, #306). The waves, units, sources of truth, cache layer, cost and engine facts add about 0.3 s (5 to 15%): rebuilding the same discourse files four times each, alternating, took 2.54 to 3.00 s without them and 2.81 to 3.18 s with them (load average 4 to 7), and the graph grew from 11.6 MB to 13.1 MB. Rebuild time grows with the number of queries times the number of lineage versions, so a 3,500-query assessment with a few versions takes a few seconds. The earlier "< 2 seconds for 3,500 queries" is not met even without SCORED edges on that machine. By 26f2486c2 the rest of #306 (the WRITES split parses each write statement's SQL) had brought the discourse rebuild to about 3.5 to 4.7 s on a laptop, and the results page's first query-journeys read, which built it, to 3.7 s (0.04 s once built); on a slower runner under load that read passed the UI's 30 s request timeout (#1911). So a run builds the graph when it ends: `scripts/run_report.py` builds the command line's copy (`graph/cli/context.lbug`, below) and never the API's file. <!-- claim: tests/graph/test_prebuilt_graph.py::test_run_report_prebuilds_the_jobs_graph, tests/graph/test_prebuilt_graph.py::test_prebuild_builds_the_command_lines_copy_never_the_apis_file --> When the API has no current graph of its own, on a job's first read or when its open graph is behind a re-run's artifacts, it copies a current copy over its own file instead of building, so the first read of a finished job opens a built graph; with no current copy it builds as before. <!-- claim: tests/graph/test_prebuilt_graph.py::test_the_first_read_of_a_prebuilt_job_opens_it_without_building, tests/graph/test_prebuilt_graph.py::test_an_open_graph_left_behind_by_a_rerun_takes_the_new_copy, tests/graph/test_prebuilt_graph.py::test_the_first_read_of_a_job_with_no_copy_still_builds, tests/graph/test_prebuilt_graph.py::test_a_stale_copy_is_rebuilt_not_served, tests/graph/test_prebuilt_graph.py::test_a_stale_copy_is_not_adopted --> A graph failure there never fails the report. <!-- claim: tests/graph/test_prebuilt_graph.py::test_a_graph_failure_never_fails_the_report --> A copy the API cannot read or write (a lock file another user owns, a full disk, a file Windows will not replace) is logged and the read builds instead. <!-- claim: tests/graph/test_prebuilt_graph.py::test_a_copy_that_cannot_be_locked_falls_back_to_a_build, tests/graph/test_prebuilt_graph.py::test_a_copy_that_cannot_be_read_or_written_is_not_adopted --> Once adopted, the copy is removed unless a CLI run is reading it, so the job keeps one graph; on Windows, where the CLI never reads the API's file, it stays. <!-- claim: tests/graph/test_prebuilt_graph.py::test_the_adopted_copy_is_removed_so_the_job_keeps_one_graph, tests/graph/test_prebuilt_graph.py::test_a_copy_a_cli_run_is_reading_stays, tests/graph/test_prebuilt_graph.py::test_on_windows_the_adopted_copy_stays_for_the_cli --> Each build and each adoption writes its own temporary file next to the graph, so two processes working on one job never share one. <!-- claim: tests/graph/test_prebuilt_graph.py::test_two_processes_building_one_job_each_use_their_own_folder, tests/graph/test_prebuilt_graph.py::test_two_processes_adopting_one_job_each_use_their_own_temporary_file --> A saved graph is reused until its inputs change.

### Storage

**Local mode.** LadybugDB database directory at `./artifacts/{db_name}/{job_id}/graph/`. Both the API and pipeline access this directly since they run on the same machine.

**ECS mode.** Graph stored on the API task's local volume (ephemeral storage or EFS mount). Rebuilt from S3 artifacts when needed. The graph is disposable since artifacts are the source of truth.

**Rebuild.** A `rebuild_graph.py` script re-reads all S3 artifacts and regenerates the graph from scratch. Used for schema migrations, disaster recovery, or backfilling graphs for assessments that ran before this feature existed. Every graph carries a `GraphMeta` stamp with the schema revision it was built with (`GRAPH_SCHEMA_REVISION` in `src/graph/schema.py`); a saved graph with another revision, or none, is rebuilt from the artifacts before it is read (#339). The stamp also records the effective assignment version the graph was built from; when a later version exists (a customer edit, a reality check, a post-schema reroute), the saved or open graph is rebuilt on its next read (#819). Only a complete build is stamped: if an artifact that exists can't be read, the previous graph is kept (or, with none, the new one is served unstamped and never uploaded), so the next read retries. The effective version is cached per job for a couple of seconds, and when the store can't be listed the current graph is served and the check runs again on the next read.

### Query catalog

The questions the graph answers live in one catalog, `src/graph/catalog.py` (#307). Each entry is one fixed read statement with its question in plain words, typed parameters (each with a read-only choice query that lists its values from the graph), a row bound, a node/edge projection for drawing the result, and the stages it needs (schema design, load test, synthesis, ...), so an empty result is explained by a stage that has not run. Parameter values are bound as parameters, never formatted into the statement. `tests/graph/test_catalog.py` checks that every entry and choice query passes the raw endpoint's statement filter (`cypher_guard`) and runs on graphs rebuilt from real job fixtures, returning rows within its bound or an empty result its needs explain. The query section of the cookbook (`docs/guides/context-graph-query-cookbook.md`) is generated from the catalog by `scripts/render_graph_catalog.py`; its `--check` runs in lint. `scripts/graph_query.py` (`list`, `params`, `run`) answers catalog questions from the command line. It opens the job's graph read-only through `src/graph/job_graph.py`, the build and freshness logic it shares with the API. When the API's graph is current it reads that; otherwise it builds its own copy (`graph/cli/context.lbug`) under a file lock, and never writes the file the API serves. Reading the API's file while the API swaps it relies on POSIX rename semantics, so on Windows the CLI always reads its own copy; the lock module is imported only on that path, so the API needs neither `fcntl` nor `msvcrt`. The API serves the same catalog: `GET /api/v1/assessments/{job_id}/graph/catalog` lists the entries with each parameter's choices on the job's graph, and `POST .../graph/catalog/{entry_id}` runs one with its parameters bound, after checking them (and each value against its parameter's choices, with a one-row lookup). Both lease the job's read-only graph like the curated views, take no Cypher and return none, and sit behind the same app-wide gate. The `/explore` Claude Code command (`.claude/commands/explore.md`) answers a question in plain words by picking a catalog entry, binding its parameters from their choices and running it through `scripts/graph_query.py`; it never writes Cypher, and the headless e2e-llm run checks its answer against the entry's own result.

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
| API task replacement loses graph | Rebuild from artifacts on startup or first read. About 3.5 to 4.7 s on the discourse sample (1,654 queries) on a laptop; more for a 3,500-query assessment. A local run builds it when the run ends instead (#1911, above). |

## References

- [ADR-016: Compute and Orchestration Strategy](ADR-016-compute-and-orchestration-strategy.md) (S3 artifact path conventions)
- [ADR-019: Query Journey Materialization](ADR-019-query-journey-materialization.md) (per-query detail view, complementary)
- [LadybugDB](https://ladybugdb.com/) (embedded columnar graph database, MIT license)
- `src/contracts/assignment_models.py` (QueryAssignment, TableAssignment, CoDependencyGroup)
- `src/contracts/load_test_models.py` (PatternResult, LoadTestOutput)
- `src/contracts/agent_interaction_models.py` (AgentQuestion, AgentAnswers)
- `src/contracts/schema_design_output.py` (TradeOff, SchemaDesignOutputBase)
