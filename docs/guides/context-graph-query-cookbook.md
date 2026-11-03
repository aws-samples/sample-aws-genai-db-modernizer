# Context Graph Query Cookbook

**Purpose:** Ready-to-run Cypher queries for the context graph, covering
dashboard-style summaries and multi-hop impact/provenance analysis.

**Audience:** Developers, solutions architects, anyone exploring an assessment.

See [ADR-023: Context Graph Layer](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/docs/architecture/decisions/ADR-023-context-graph-layer.md)
for why the graph exists. This guide is how to query it.

## How to run a query

All queries go to the graph query endpoint as a JSON POST:

```
POST /api/v1/assessments/{job_id}/graph/query
Content-Type: application/json

{"cypher": "<query>", "params": {}}
```

Response shape:

```json
{"columns": ["..."], "rows": [{"...": "..."}], "row_count": 12, "truncated": false}
```

The raw query endpoint and the rebuild endpoint (below) are off by default.
Start the API with `MODERNIZER_ENABLE_RAW_GRAPH_QUERY=1` to enable them;
otherwise they return `403`. The curated `GET` endpoints below are always on.

Limits on the raw endpoint:

- Requests must be sent with `Content-Type: application/json` (`415`
  otherwise); request bodies are at most 64 KiB.
- One read statement per request: `MATCH`, `OPTIONAL MATCH`, `WITH`, `UNWIND`,
  `RETURN`, `ORDER BY`, `SKIP`, `LIMIT`, `EXISTS { }` / `COUNT { }`
  subqueries, and `CALL` of `show_tables`, `table_info`, `show_connection` or
  `db_version`. `UNION` and other statements return `400`.
- Queries are at most 10,000 characters of ASCII text outside quoted strings,
  with bounded nesting.
- `range`, `repeat`, `lpad` and `rpad` take integer-literal sizes of at most
  1,000 elements.
- The engine returns at most 1,000 rows. `truncated: true` means more rows
  matched; add `ORDER BY` with `SKIP`/`LIMIT` to page. A final `LIMIT` must be
  an integer literal.
- Each query runs in its own short-lived process with a 10-second timeout
  (`504`), a memory cap and a 16 MiB result cap (`400`). At most two run at
  once; more return `429`.

The curated endpoints run fixed queries on a read-only handle with the same 10-second timeout (`504`). The handle has a 256 MiB buffer pool (`MODERNIZER_GRAPH_BUFFER_POOL_MB`), runs each statement on 2 engine threads (`MODERNIZER_GRAPH_QUERY_THREADS`) and runs at most 4 statements at once (`MODERNIZER_GRAPH_MAX_CONCURRENT_READS`); further reads wait for a slot. A read that still runs out of buffer pool memory, or waits more than 30 seconds for a slot, returns `503` with `Retry-After: 1`. `GET /query-journeys` reads only the requested page from the graph.

The graph is built lazily on first access (or explicitly via
`POST /api/v1/assessments/{job_id}/graph/rebuild`) from the assessment's
artifacts (the artifact store, which is the local `artifacts/` folder by default), then persisted and reused. If a query returns no rows, the
corresponding pipeline stage may not have produced artifacts yet (see
[Empty results](#empty-results)).

## From the command line

`scripts/graph_query.py` asks a job's graph any catalog question below, without the API and without writing Cypher:

```bash
uv run python scripts/graph_query.py list
uv run python scripts/graph_query.py params table_blast_radius --job-id <job>
uv run python scripts/graph_query.py run table_blast_radius --job-id <job> --param table_id=wordpress.wp_posts
uv run python scripts/graph_query.py run engine_distribution --job-id <job> --format md
```

- Each command prints one compact JSON line (`--format md` prints Markdown). Any error, a usage error included, prints `{"status": "error", "message": ...}` and exits 1.
- `run` returns `columns`, `rows`, `row_count`, `truncated` (more rows matched than the entry's bound) and `graph`: its `source` (`api` or `cli`), `path` and whether the copy was `rebuilt`. An empty result also lists `unmet_needs` and an `explanation` that names the stages the job has not run.
- `params` lists each parameter's choices from the job's graph when `--job-id` is given.
- `--db` is found from the job id when only one database folder under `--artifact-root` (default `$ARTIFACT_DIR` or `./artifacts`) holds that job.
- The graph is opened read-only, with the read timeout and the entry's row bound.
  - When the API's graph (`<db>/<job>/graph/context.lbug`) is current, it is read as it is.
  - Otherwise the script builds its own copy (`graph/cli/context.lbug`) under a file lock and reads that, so it never writes the file the API serves while the API rebuilds it. Two runs at once build the copy once, and reads of a current copy run side by side.
  - The copy is removed by the next run that finds the API's graph current again. `--clean` removes `graph/cli/` (copy and lock file) once the command has answered.
  - Reading the API's file while the API replaces it relies on POSIX rename semantics: an open handle keeps the file it opened. On Windows the script always reads its own copy.
- Under `MODERNIZER_CI_SANDBOX=1`, an artifact root outside the repository or a database or job name that is not a plain name is refused.

## Curated endpoints (preferred)

For the highest-value questions there are typed `GET` endpoints that return
purpose-shaped JSON, so you don't hand-write Cypher. Prefer these; use raw
`POST /graph/query` for anything not covered.

| Endpoint | Returns |
|----------|---------|
| `GET /api/v1/assessments/{job_id}/graph/tables/{table_id}/impact` | Queries affected if the table changes (blast radius), with destinations, access patterns, anti-patterns |
| `GET /api/v1/assessments/{job_id}/graph/queries/{query_id}/provenance` | Why a query migrated where it did, plus the producing agent/phase |
| `GET /api/v1/assessments/{job_id}/graph/queries/{query_id}/trace` | Why a query ended on its engine and not another: its first assignment with each engine's score or gate, each later move with its reason, and the runner-up |
| `GET /api/v1/assessments/{job_id}/query-journeys` and `.../query-journeys/{query_id}` | The query journeys: one page of queries, or one query's journey, read from the graph |
| `GET /api/v1/assessments/{job_id}/graph/engines/{engine}` | Destinations and source tables migrating to an engine |
| `GET /api/v1/assessments/{job_id}/graph/risks` | Risk hotspots: tables carrying open risk and anti-patterns, weighted by traffic |
| `GET /api/v1/assessments/{job_id}/load-test-results` | Load-test results grouped by access pattern (filters: `engine`, `version`, `prefix`) |

Unknown ids and not-yet-run stages return `200` with empty collections, not
`404`, so consumers distinguish "no data" from an error.

**Adding a curated view later** is a repeatable three-step pattern: add a pure
function to `src/graph/queries.py` (Cypher + shaping), a Pydantic model to
`src/api/models/graph_responses.py`, and a thin handler in
`src/api/routes/graph.py` using `Depends(get_graph_for_job)` and
`response_model=`.

**Adding a catalog question** is one entry in `src/graph/catalog.py` (Cypher, parameters with their choice queries, row bound, graph view and needs); `uv run python scripts/render_graph_catalog.py` then writes it into the generated section below, and `tests/graph/test_catalog.py` runs it against the fixtures.

## Graph model

Node types: `Query`, `SourceTable`, `Destination`, `Engine`, `Signal`,
`CoDependencyGroup`, `Decision`, `LoadTestRun`, `AntiPattern`, `Risk`,
`AccessPattern`, `AssignmentVersion` (one per version of the effective assignment lineage),
`Agent` (the producing agent and phase), `GraphMeta` (the schema revision
and assignment version the graph was built from).

Relationships:

```text
(Query)-[:READS_FROM]->(SourceTable)
(Query)-[:MIGRATES_TO {confidence, assignment_reason}]->(Destination)
(Query)-[:EMITS_SIGNAL {strength}]->(Signal)
(Query)-[:MEMBER_OF]->(CoDependencyGroup)
(Query)-[:PART_OF]->(AccessPattern)
(Query)-[:TESTED_IN]->(LoadTestRun)
(LoadTestRun)-[:VALIDATES]->(Destination)
(Destination)-[:HOSTED_ON]->(Engine)
(Decision)-[:AFFECTS]->(Destination)
(Decision)-[:INFORMED_BY]->(Query)
(AntiPattern)-[:OBSERVED_IN_QUERY]->(Query)
(AntiPattern)-[:OBSERVED_IN_TABLE]->(SourceTable)
(Risk)-[:IMPACTS]->(SourceTable)
(Risk)-[:EVIDENCED_BY]->(Query)
(Decision)-[:SUPERSEDES]->(Decision)
(AssignmentVersion)-[:SUPERSEDES]->(AssignmentVersion)
(AssignmentVersion)-[:RESTORES]->(AssignmentVersion)
(AssignmentVersion)-[:AFFECTS {role}]->(Query|Engine)
(Decision)-[:AFFECTS {role}]->(Engine)
(Query)-[:MOVED {version, seq, pass, reason_code, from_engine, score_before, score_after, ...}]->(Engine)
(Decision|Signal|AntiPattern|AccessPattern|LoadTestRun|Risk)-[:PRODUCED_BY]->(Agent)
```

An `AssignmentVersion` (`id` `assignment-v<N>`) has `version`, `kind` (the pass that made it: `first_assignment`, `reality_check`, `customer_pick`, `customer_scope`, `post_schema_reroute`, `rollback`, `source_of_truth_reroute` or `auto_decided`), `source`, `status`, `effective`, `created_at`, `move_count`, `passes_json` and `actors_json` (its Moves' distinct passes and actors). It `SUPERSEDES` the version before it, and a rollback `RESTORES` the version it restates. It `AFFECTS` the queries its Moves changed (role `moved`, or `tie` when they changed only a tie) and the engines those Moves left (`from`) or landed on (`to`). A consolidation Decision is `INFORMED_BY` the queries whose net move in the reality-check version is its from and to engine (its `query_count`), and a consolidation or reroute Decision `AFFECTS` the engines it moves queries from and to.

`MOVED` is the move ledger in the graph: one edge per Move for every assignment version in the lineage, so a query's whole routing history stays.

A `Risk` is one of the synthesis report's risks. `status` is `active` for an open risk (`risk_assessment.risks`, with its `rule` and `wave` when a migration-risk rule raised it) or `resolved` for one the assignment resolved (`risk_assessment.resolved_risks`, ids `RESOLVED-001` on, positional within one report: the same id in another report or version can be another risk): `engine` is the engine whose analysis raised it, `resolved_on` the engine it is resolved on and `reason` why. Both kinds have `IMPACTS` edges to their tables and `EVIDENCED_BY` edges to their queries; a query id with no `Query` node gets no edge.

<!-- BEGIN GENERATED by scripts/render_graph_catalog.py from src/graph/catalog.py: edit the catalog and run the script, never this block -->

Every query below is an entry of the graph query catalog (`src/graph/catalog.py`).
Each one is a single read statement that passes the raw endpoint's filter, carries
a row bound, and runs against real job fixtures in `tests/graph/test_catalog.py`.
A parameter is bound through `params`, never pasted into the Cypher; its choice
query lists the values the graph holds.

## Summary queries (reproduce UI views)

### 1. Node census

Which kinds of nodes does the graph hold, and how many of each?

Catalog id `node_census`; at most 50 rows. Needs: nothing beyond the collector.

Graph view: none (a table of counts).

Run this first: a missing label means its stage has not run (see Empty results).

```json
{"cypher": "MATCH (n) RETURN label(n) AS type, count(*) AS n ORDER BY n DESC, type"}
```

### 2. Engine distribution: queries per target engine

How many queries go to each target engine?

Catalog id `engine_distribution`; at most 50 rows. Needs: Assignment (`Destination` nodes and `MIGRATES_TO` edges).

Graph view: nodes `Engine` (`engine`).

```json
{"cypher": "MATCH (q:Query)-[:MIGRATES_TO]->(d:Destination)-[:HOSTED_ON]->(e:Engine) RETURN e.id AS engine, count(DISTINCT q) AS queries ORDER BY queries DESC, engine"}
```

### 3. Table to engine mapping

Which engines serve each source table's queries?

Catalog id `table_engines`; at most 1,000 rows. Needs: Assignment (`Destination` nodes and `MIGRATES_TO` edges).

Graph view: nodes `SourceTable` (`table_id`), `Engine` (`engines`); edges `SourceTable` → `Engine` (served by).

```json
{"cypher": "MATCH (q:Query)-[:READS_FROM]->(st:SourceTable), (q)-[:MIGRATES_TO]->(d:Destination) RETURN st.id AS table_id, collect(DISTINCT d.engine) AS engines, count(DISTINCT q) AS queries ORDER BY queries DESC, table_id"}
```

### 4. Access patterns per engine

How many access patterns did each engine's schema design define, at what rate?

Catalog id `access_patterns_per_engine`; at most 50 rows. Needs: Schema design, per engine (`AccessPattern` nodes).

Graph view: nodes `Engine` (`engine`).

```json
{"cypher": "MATCH (ap:AccessPattern) RETURN ap.engine AS engine, count(*) AS patterns, sum(ap.design_rps) AS total_rps ORDER BY total_rps DESC, engine"}
```

### 5. Anti-patterns by type

Which anti-pattern types were found, in how many queries, and how severe?

Catalog id `anti_patterns_by_type`; at most 1,000 rows. Needs: Analysis, per engine (`AntiPattern` nodes).

Graph view: none (a table of counts).

```json
{"cypher": "MATCH (a:AntiPattern) WITH a.anti_pattern_type AS type, avg(a.severity_weight) AS avg_severity MATCH (b:AntiPattern {anti_pattern_type: type})-[:OBSERVED_IN_QUERY]->(q:Query) RETURN type, count(DISTINCT q) AS queries, avg_severity ORDER BY queries DESC, type"}
```

### 6. Risk register

Which migration risks did synthesis raise, and which tables do they touch?

Catalog id `risk_register`; at most 1,000 rows. Needs: Referee synthesis (active `Risk` nodes).

Graph view: nodes `Risk` (`risk_id`), `SourceTable` (`affected_tables`); edges `Risk` → `SourceTable` (IMPACTS).

```json
{"cypher": "MATCH (r:Risk {status: 'active'})-[:IMPACTS]->(st:SourceTable) WITH r, collect(st.id) AS affected_tables RETURN r.id AS risk_id, r.severity AS severity, r.risk_type AS type, r.description AS description, affected_tables ORDER BY CASE r.severity WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1 WHEN 'MEDIUM' THEN 2 WHEN 'LOW' THEN 3 ELSE 4 END, risk_id"}
```

### 6b. Resolved risks

Which risks did the assignment resolve, where and why, and which queries do they name?

Catalog id `resolved_risks`; at most 1,000 rows. Needs: Referee synthesis, when the assignment resolved a risk (resolved `Risk` nodes).

Graph view: nodes `Risk` (`risk_id`), `Query` (`queries`); edges `Risk` → `Query` (EVIDENCED_BY).

```json
{"cypher": "MATCH (r:Risk {status: 'resolved'}) OPTIONAL MATCH (r)-[:EVIDENCED_BY]->(q:Query) RETURN r.id AS risk_id, r.severity AS severity, r.engine AS raised_by, r.resolved_on AS resolved_on, r.reason AS reason, collect(DISTINCT q.id) AS queries ORDER BY risk_id"}
```

### 7. Signals from triage

Which triage signals did the queries emit, and how often?

Catalog id `signals`; at most 1,000 rows. Needs: Referee triage (`Signal` nodes).

Graph view: nodes `Signal` (`signal`).

```json
{"cypher": "MATCH (q:Query)-[:EMITS_SIGNAL]->(s:Signal) RETURN s.id AS signal, s.category AS category, count(q) AS queries ORDER BY queries DESC, signal"}
```

### 8. Decisions: provenance summary

Which kinds of decisions were recorded, with a few examples of each?

Catalog id `decisions_summary`; at most 50 rows. Needs: Reality check or schema design (`Decision` nodes).

Graph view: none (a table of counts).

```json
{"cypher": "MATCH (d:Decision) WITH d ORDER BY d.id SKIP 0 RETURN d.category AS category, count(*) AS n, collect(d.description)[0..3] AS examples ORDER BY n DESC, category"}
```

### 9. Load-test results per pattern

How did each access pattern's queries perform in the load test?

Catalog id `load_test_per_pattern`; at most 1,000 rows. Needs: Schema design, per engine (`AccessPattern` nodes); Load test, per engine (`LoadTestRun` nodes).

Graph view: nodes `AccessPattern` (`pattern`).

```json
{"cypher": "MATCH (ap:AccessPattern)<-[:PART_OF]-(q:Query)-[:TESTED_IN]->(lt:LoadTestRun) WHERE lt.engine = ap.engine AND lt.schema_version = ap.schema_version RETURN ap.id AS pattern, ap.engine AS engine, count(q) AS queries, avg(lt.improvement_factor) AS avg_improvement, avg(lt.target_p90) AS avg_target_p90 ORDER BY pattern, engine"}
```

### 10. Top-traffic queries

Which queries carry the most traffic?

Catalog id `top_traffic_queries`; at most 20 rows. Needs: nothing beyond the collector.

Graph view: nodes `Query` (`query_id`).

```json
{"cypher": "MATCH (q:Query) RETURN q.id AS query_id, q.operation_type AS operation_type, q.calls_per_second AS calls_per_second ORDER BY calls_per_second DESC, query_id LIMIT 20"}
```

### 11. Graph build stamp

Which schema revision and assignment version was this graph built from?

Catalog id `graph_build`; at most 1 rows. Needs: nothing beyond the collector.

Graph view: none (a table of counts).

```json
{"cypher": "MATCH (m:GraphMeta) RETURN m.schema_revision AS schema_revision, m.effective_version AS effective_version ORDER BY effective_version"}
```

## Impact and provenance queries

### A. Blast radius: what is affected if I change this table

Which queries, destinations and access patterns are affected if this table changes?

Catalog id `table_blast_radius`; at most 1,000 rows. Needs: nothing beyond the collector.

Graph view: nodes `SourceTable` (`table_id`), `Query` (`query_id`), `Destination` (`destinations`), `AccessPattern` (`access_patterns`); edges `Query` → `SourceTable` (READS_FROM), `Query` → `Destination` (MIGRATES_TO), `Query` → `AccessPattern` (PART_OF).

Parameters:

- `table_id` (table): A source table id, schema-qualified as the collector wrote it. Example `wordpress.wp_posts`. Choices: `MATCH (st:SourceTable) RETURN st.id AS value ORDER BY value`

```json
{"cypher": "MATCH (st:SourceTable {id: $table_id})<-[:READS_FROM]-(q:Query) OPTIONAL MATCH (q)-[:MIGRATES_TO]->(d:Destination) OPTIONAL MATCH (q)-[:PART_OF]->(ap:AccessPattern) OPTIONAL MATCH (a:AntiPattern)-[:OBSERVED_IN_QUERY]->(q) RETURN st.id AS table_id, q.id AS query_id, q.calls_per_second AS cps, collect(DISTINCT d.id) AS destinations, collect(DISTINCT ap.id) AS access_patterns, collect(DISTINCT a.anti_pattern_type) AS anti_patterns ORDER BY cps DESC, query_id", "params": {"table_id": "wordpress.wp_posts"}}
```

### B. Everything going to a given engine

Which destinations, source tables and access patterns does this engine get?

Catalog id `engine_destinations`; at most 1,000 rows. Needs: Assignment (`Destination` nodes and `MIGRATES_TO` edges).

Graph view: nodes `Destination` (`destination`), `SourceTable` (`source_tables`), `AccessPattern` (`access_patterns`); edges `SourceTable` → `Destination` (migrates to), `Destination` → `AccessPattern` (serves).

Parameters:

- `engine` (engine): An engine id the assignment migrates queries to. Example `dynamodb`. Change `dynamodb` to `aurora_mysql`, `aurora_postgresql`, `documentdb`, `elasticache`, `opensearch`. Choices: `MATCH (d:Destination) RETURN DISTINCT d.engine AS value ORDER BY value`

```json
{"cypher": "MATCH (q:Query)-[:MIGRATES_TO]->(d:Destination {engine: $engine}) OPTIONAL MATCH (q)-[:PART_OF]->(ap:AccessPattern) OPTIONAL MATCH (q)-[:READS_FROM]->(st:SourceTable) RETURN d.id AS destination, collect(DISTINCT st.id) AS source_tables, collect(DISTINCT ap.id) AS access_patterns, count(DISTINCT q) AS queries ORDER BY queries DESC, destination", "params": {"engine": "dynamodb"}}
```

### C. Full provenance for one query: why did it go where it went

Where did this query go, with what confidence, and what informed it?

Catalog id `query_provenance`; at most 50 rows. Needs: nothing beyond the collector.

Graph view: nodes `Query` (`query_id`), `Destination` (`destination`), `Signal` (`signals`), `Decision` (`decisions`), `CoDependencyGroup` (`co_dependency_groups`); edges `Query` → `Destination` (MIGRATES_TO), `Query` → `Signal` (EMITS_SIGNAL), `Decision` → `Query` (INFORMED_BY), `Query` → `CoDependencyGroup` (MEMBER_OF).

Parameters:

- `query_id` (query): A query id, busiest first. Example `66c96f951dd01698438c1be2887ff2553664cb0b99c1a1b0108b18ca22dcb799`. Choices: `MATCH (q:Query) RETURN q.id AS value ORDER BY q.calls_per_second DESC, value`

```json
{"cypher": "MATCH (q:Query {id: $query_id}) OPTIONAL MATCH (q)-[m:MIGRATES_TO]->(d:Destination) OPTIONAL MATCH (q)-[:EMITS_SIGNAL]->(s:Signal) OPTIONAL MATCH (dec:Decision)-[:INFORMED_BY]->(q) OPTIONAL MATCH (q)-[:MEMBER_OF]->(g:CoDependencyGroup) RETURN q.id AS query_id, d.id AS destination, m.confidence AS confidence, m.assignment_reason AS reason, collect(DISTINCT s.id) AS signals, collect(DISTINCT dec.id) AS decisions, collect(DISTINCT g.id) AS co_dependency_groups ORDER BY destination", "params": {"query_id": "66c96f951dd01698438c1be2887ff2553664cb0b99c1a1b0108b18ca22dcb799"}}
```

### D. Migration risk hotspots: high traffic and risk and anti-patterns

Which busy tables carry risks or anti-patterns?

Catalog id `risk_hotspots`; at most 1,000 rows. Needs: Analysis, per engine (`AntiPattern` nodes); Referee synthesis (active `Risk` nodes).

Graph view: nodes `SourceTable` (`table_id`).

```json
{"cypher": "MATCH (st:SourceTable)<-[:READS_FROM]-(q:Query) WITH st, sum(q.calls_per_second) AS total_cps OPTIONAL MATCH (r:Risk {status: 'active'})-[:IMPACTS]->(st) WITH st, total_cps, count(DISTINCT r) AS risks OPTIONAL MATCH (a:AntiPattern)-[:OBSERVED_IN_TABLE]->(st) WITH st, total_cps, risks, count(DISTINCT a) AS anti_patterns WHERE risks > 0 OR anti_patterns > 0 RETURN st.id AS table_id, total_cps, risks, anti_patterns ORDER BY total_cps DESC, table_id"}
```

### E. Cross-engine tables: a table split across more than one engine

Which source tables are served by more than one engine?

Catalog id `cross_engine_tables`; at most 1,000 rows. Needs: Assignment (`Destination` nodes and `MIGRATES_TO` edges).

Graph view: nodes `SourceTable` (`table_id`), `Engine` (`engines`); edges `SourceTable` → `Engine` (served by).

```json
{"cypher": "MATCH (q:Query)-[:READS_FROM]->(st:SourceTable), (q)-[:MIGRATES_TO]->(d:Destination) WITH st, count(DISTINCT d.engine) AS engine_count, collect(DISTINCT d.engine) AS engines WHERE engine_count > 1 RETURN st.id AS table_id, engine_count, engines ORDER BY engine_count DESC, table_id"}
```

### F. Load-test regressions: queries that got worse after migration

Which queries were slower on the target than on the source in the load test?

Catalog id `load_test_regressions`; at most 1,000 rows. Needs: Load test, per engine (`LoadTestRun` nodes).

Graph view: nodes `Query` (`query_id`), `LoadTestRun` (`run_id`); edges `Query` → `LoadTestRun` (TESTED_IN).

An empty result after a load test ran means no query regressed.

```json
{"cypher": "MATCH (q:Query)-[:TESTED_IN]->(lt:LoadTestRun) WHERE lt.improvement_factor < 1.0 RETURN q.id AS query_id, lt.id AS run_id, lt.engine AS engine, lt.source_p90 AS source_p90, lt.target_p90 AS target_p90, lt.improvement_factor AS improvement_factor ORDER BY improvement_factor, query_id"}
```

### G. Routing history of one query: every Move

Which steps moved this query between engines, and why?

Catalog id `query_moves`; at most 200 rows. Needs: Assignment move ledger (`MOVED` edges).

Graph view: nodes `Query` (`query_id`), `Engine` (`to_engine`); edges `Query` → `Engine` (MOVED).

Parameters:

- `query_id` (query): A query id, busiest first. Example `66c96f951dd01698438c1be2887ff2553664cb0b99c1a1b0108b18ca22dcb799`. Choices: `MATCH (q:Query) RETURN q.id AS value ORDER BY q.calls_per_second DESC, value`

The curated `GET .../graph/queries/{query_id}/trace` endpoint explains the same Moves with each engine's score.

```json
{"cypher": "MATCH (q:Query {id: $query_id})-[m:MOVED]->(e:Engine) RETURN q.id AS query_id, m.version AS version, m.seq AS seq, m.pass AS pass, m.from_engine AS from_engine, e.id AS to_engine, m.reason_code AS reason_code, m.score_before AS score_before, m.score_after AS score_after, m.runner_up_engine AS runner_up_engine, m.runner_up_score AS runner_up_score ORDER BY version, seq", "params": {"query_id": "66c96f951dd01698438c1be2887ff2553664cb0b99c1a1b0108b18ca22dcb799"}}
```

### H. Design decisions about one query, with the agent that made them

Which design decisions name this query, and which agent made each?

Catalog id `query_decisions`; at most 200 rows. Needs: Schema design trade-offs (`INFORMED_BY` edges from a `Decision` to a `Query`).

Graph view: nodes `Query` (`query_id`), `Decision` (`decision_id`), `Agent` (`agent_id`); edges `Decision` → `Query` (INFORMED_BY), `Decision` → `Agent` (PRODUCED_BY).

Parameters:

- `query_id` (query): A query id that a design decision names. Example `66c96f951dd01698438c1be2887ff2553664cb0b99c1a1b0108b18ca22dcb799`. Choices: `MATCH (:Decision)-[:INFORMED_BY]->(q:Query) RETURN DISTINCT q.id AS value ORDER BY value`

```json
{"cypher": "MATCH (dec:Decision)-[:INFORMED_BY]->(q:Query {id: $query_id}) OPTIONAL MATCH (dec)-[:PRODUCED_BY]->(a:Agent) RETURN q.id AS query_id, dec.id AS decision_id, dec.category AS category, dec.description AS description, a.id AS agent_id, a.phase AS phase ORDER BY decision_id, agent_id", "params": {"query_id": "66c96f951dd01698438c1be2887ff2553664cb0b99c1a1b0108b18ca22dcb799"}}
```

### I. Queries behind one access pattern

Which queries does this access pattern serve, and how did their load tests go?

Catalog id `access_pattern_queries`; at most 1,000 rows. Needs: Schema design, per engine (`AccessPattern` nodes).

Graph view: nodes `AccessPattern` (`pattern`), `Query` (`query_id`), `LoadTestRun` (`run_id`); edges `Query` → `AccessPattern` (PART_OF), `Query` → `LoadTestRun` (TESTED_IN).

Parameters:

- `access_pattern_id` (access_pattern): An access pattern id from a schema design. Example `DDB-AP-1`. Choices: `MATCH (ap:AccessPattern) RETURN ap.id AS value ORDER BY value`

```json
{"cypher": "MATCH (ap:AccessPattern {id: $access_pattern_id})<-[:PART_OF]-(q:Query) OPTIONAL MATCH (q)-[:TESTED_IN]->(lt:LoadTestRun) WHERE lt.engine = ap.engine AND lt.schema_version = ap.schema_version RETURN ap.id AS pattern, ap.engine AS engine, ap.operation AS operation, q.id AS query_id, q.calls_per_second AS cps, lt.id AS run_id, lt.improvement_factor AS improvement_factor ORDER BY cps DESC, query_id, run_id", "params": {"access_pattern_id": "DDB-AP-1"}}
```

### J. Assignment version chain

Which assignment versions does the lineage hold, which pass made each, and which one is effective?

Catalog id `version_chain`; at most 200 rows. Needs: Assignment version chain (`AssignmentVersion` nodes).

Graph view: nodes `AssignmentVersion` (`version_id`), `AssignmentVersion` (`supersedes`), `AssignmentVersion` (`restores`); edges `AssignmentVersion` → `AssignmentVersion` (SUPERSEDES), `AssignmentVersion` → `AssignmentVersion` (RESTORES).

```json
{"cypher": "MATCH (v:AssignmentVersion) OPTIONAL MATCH (v)-[:SUPERSEDES]->(p:AssignmentVersion) OPTIONAL MATCH (v)-[:RESTORES]->(r:AssignmentVersion) RETURN v.id AS version_id, v.version AS version, v.kind AS kind, v.status AS status, v.effective AS effective, v.move_count AS move_count, p.id AS supersedes, r.id AS restores ORDER BY version, version_id"}
```

### K. What one assignment version changed

Which queries did this assignment version move, from which engines and to which?

Catalog id `version_changes`; at most 1,000 rows. Needs: Assignment version chain (`AssignmentVersion` nodes).

Graph view: nodes `AssignmentVersion` (`version_id`), `Query` (`query_id`), `Engine` (`to_engines`); edges `AssignmentVersion` → `Query` (AFFECTS), `Query` → `Engine` (MOVED).

Parameters:

- `version_id` (version): An assignment version id (`assignment-v<N>`), newest first. Example `assignment-v2`. Choices: `MATCH (v:AssignmentVersion) RETURN v.id AS value ORDER BY v.version DESC, value`

Role `moved` is a query the version's Moves changed; `tie` one whose tie alone changed.

```json
{"cypher": "MATCH (v:AssignmentVersion {id: $version_id})-[a:AFFECTS]->(q:Query) OPTIONAL MATCH (q)-[m:MOVED]->(e:Engine) WHERE m.version = v.version RETURN v.id AS version_id, q.id AS query_id, a.role AS role, collect(DISTINCT m.from_engine) AS from_engines, collect(DISTINCT e.id) AS to_engines ORDER BY role, query_id", "params": {"version_id": "assignment-v2"}}
```

## Empty results

A query that returns `row_count: 0` usually means the pipeline stage that
produces those nodes has not run for the assessment, not that the query is
wrong. The graph only contains what the artifacts contain. Each entry above
names the stages it needs.

| Missing | Stage that produces them | Queries |
|---------|--------------------------|---------|
| `Signal` nodes | Referee triage | 7 |
| `AntiPattern` nodes | Analysis, per engine | 5, D |
| `Destination` nodes and `MIGRATES_TO` edges | Assignment | 2, 3, B, E |
| `MOVED` edges | Assignment move ledger | G |
| `AssignmentVersion` nodes | Assignment version chain | J, K |
| `Decision` nodes | Reality check or schema design | 8 |
| `INFORMED_BY` edges from a `Decision` to a `Query` | Schema design trade-offs | H |
| `AccessPattern` nodes | Schema design, per engine | 4, 9, I |
| `LoadTestRun` nodes | Load test, per engine | 9, F |
| active `Risk` nodes | Referee synthesis | 6, D |
| resolved `Risk` nodes | Referee synthesis, when the assignment resolved a risk | 6b |

<!-- END GENERATED by scripts/render_graph_catalog.py -->

To confirm what exists, run the node census (query 1). To force a fresh build
from current artifacts, `POST /api/v1/assessments/{job_id}/graph/rebuild`
(requires `MODERNIZER_ENABLE_RAW_GRAPH_QUERY=1`).
