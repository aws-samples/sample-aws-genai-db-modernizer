# Context Graph Query Cookbook

**Purpose:** Ready-to-run Cypher queries for the context graph, covering
dashboard-style summaries and multi-hop impact/provenance analysis.

**Audience:** Developers, solutions architects, anyone exploring an assessment.

See [ADR-023: Context Graph Layer](../architecture/decisions/ADR-023-context-graph-layer.md)
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
{"columns": ["..."], "rows": [{"...": "..."}], "row_count": 12}
```

The graph is built lazily on first access (or explicitly via
`POST /api/v1/assessments/{job_id}/graph/rebuild`) from the assessment's S3
artifacts, then persisted and reused. If a query returns no rows, the
corresponding pipeline stage may not have produced artifacts yet (see
[Empty results](#empty-results)).

## Curated endpoints (preferred)

For the highest-value questions there are typed `GET` endpoints that return
purpose-shaped JSON, so you don't hand-write Cypher. Prefer these; use raw
`POST /graph/query` for anything not covered.

| Endpoint | Returns |
|----------|---------|
| `GET /api/v1/assessments/{job_id}/graph/tables/{table_id}/impact` | Queries affected if the table changes (blast radius), with destinations, access patterns, anti-patterns |
| `GET /api/v1/assessments/{job_id}/graph/queries/{query_id}/provenance` | Why a query migrated where it did, plus the producing agent/phase |
| `GET /api/v1/assessments/{job_id}/graph/engines/{engine}` | Destinations and source tables migrating to an engine |
| `GET /api/v1/assessments/{job_id}/graph/risks` | Risk hotspots: tables carrying risk and anti-patterns, weighted by traffic |
| `GET /api/v1/assessments/{job_id}/graph/load-test-results` | Load-test results grouped by access pattern (filters: `engine`, `version`, `prefix`) |

Unknown ids and not-yet-run stages return `200` with empty collections, not
`404`, so consumers distinguish "no data" from an error.

**Adding a curated view later** is a repeatable three-step pattern: add a pure
function to `src/graph/queries.py` (Cypher + shaping), a Pydantic model to
`src/api/models/graph_responses.py`, and a thin handler in
`src/api/routes/graph.py` using `Depends(get_graph_for_job)` and
`response_model=`.

## Graph model

Node types: `Query`, `SourceTable`, `Destination`, `Engine`, `Signal`,
`CoDependencyGroup`, `Decision`, `LoadTestRun`, `AntiPattern`, `Risk`,
`AccessPattern`.

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
```

## Summary queries (reproduce UI views)

### 1. Node census

```json
{"cypher":"MATCH (n) RETURN labels(n)[0] AS type, count(*) AS n ORDER BY n DESC"}
```

### 2. Engine distribution — queries per target engine

```json
{"cypher":"MATCH (q:Query)-[:MIGRATES_TO]->(d:Destination)-[:HOSTED_ON]->(e:Engine) RETURN e.id AS engine, count(DISTINCT q) AS queries ORDER BY queries DESC"}
```

### 3. Table → engine mapping

```json
{"cypher":"MATCH (q:Query)-[:READS_FROM]->(st:SourceTable), (q)-[:MIGRATES_TO]->(d:Destination) RETURN st.id AS table, collect(DISTINCT d.engine) AS engines, count(DISTINCT q) AS queries ORDER BY queries DESC"}
```

### 4. Access patterns per engine

```json
{"cypher":"MATCH (ap:AccessPattern) RETURN ap.engine AS engine, count(*) AS patterns, sum(ap.design_rps) AS total_rps ORDER BY total_rps DESC"}
```

### 5. Anti-patterns by type

```json
{"cypher":"MATCH (a:AntiPattern)-[:OBSERVED_IN_QUERY]->(q:Query) RETURN a.anti_pattern_type AS type, count(DISTINCT q) AS queries, avg(a.severity_weight) AS avg_severity ORDER BY queries DESC"}
```

### 6. Risk register

```json
{"cypher":"MATCH (r:Risk)-[:IMPACTS]->(st:SourceTable) RETURN r.severity AS severity, r.risk_type AS type, r.description AS description, collect(st.id) AS affected_tables ORDER BY severity"}
```

### 7. Signals from triage

```json
{"cypher":"MATCH (q:Query)-[:EMITS_SIGNAL]->(s:Signal) RETURN s.id AS signal, s.category AS category, count(q) AS queries ORDER BY queries DESC"}
```

### 8. Decisions / provenance summary

```json
{"cypher":"MATCH (d:Decision) RETURN d.category AS category, count(*) AS n, collect(d.description)[0..3] AS examples ORDER BY n DESC"}
```

### 9. Load-test results per pattern

Requires `LoadTestRun` nodes (see [Empty results](#empty-results)).

```json
{"cypher":"MATCH (ap:AccessPattern)<-[:PART_OF]-(q:Query)-[:TESTED_IN]-(lt:LoadTestRun) RETURN ap.id AS pattern, count(q) AS queries, avg(lt.improvement_factor) AS avg_improvement, avg(lt.target_p90) AS avg_target_p90 ORDER BY pattern"}
```

### 10. Top-traffic queries

```json
{"cypher":"MATCH (q:Query) RETURN q.id, q.operation_type, q.calls_per_second ORDER BY q.calls_per_second DESC LIMIT 20"}
```

## Impact and provenance queries

These multi-hop traversals are the reason for the graph: each one would
otherwise require loading and joining several S3 artifacts in application code.

### A. Blast radius — what is affected if I change this table

Replace `topics` with a table from query 3.

```json
{"cypher":"MATCH (st:SourceTable {id: 'topics'})<-[:READS_FROM]-(q:Query) OPTIONAL MATCH (q)-[:MIGRATES_TO]->(d:Destination) OPTIONAL MATCH (q)-[:PART_OF]->(ap:AccessPattern) OPTIONAL MATCH (a:AntiPattern)-[:OBSERVED_IN_QUERY]->(q) RETURN q.id AS query, q.calls_per_second AS cps, collect(DISTINCT d.id) AS destinations, collect(DISTINCT ap.id) AS access_patterns, collect(DISTINCT a.anti_pattern_type) AS anti_patterns ORDER BY cps DESC"}
```

### B. Everything going to a given engine

Change `dynamodb` to `documentdb`, `opensearch`, or `elasticache`.

```json
{"cypher":"MATCH (q:Query)-[:MIGRATES_TO]->(d:Destination {engine: 'dynamodb'}) OPTIONAL MATCH (q)-[:PART_OF]->(ap:AccessPattern) OPTIONAL MATCH (q)-[:READS_FROM]->(st:SourceTable) RETURN d.id AS destination, collect(DISTINCT st.id) AS source_tables, collect(DISTINCT ap.id) AS access_patterns, count(DISTINCT q) AS queries ORDER BY queries DESC"}
```

### C. Full provenance for one query — why did it go where it went

Replace `<query_id>` with an id from query 10.

```json
{"cypher":"MATCH (q:Query {id: '<query_id>'}) OPTIONAL MATCH (q)-[m:MIGRATES_TO]->(d:Destination) OPTIONAL MATCH (q)-[:EMITS_SIGNAL]->(s:Signal) OPTIONAL MATCH (dec:Decision)-[:INFORMED_BY]->(q) OPTIONAL MATCH (q)-[:MEMBER_OF]->(g:CoDependencyGroup) RETURN d.id AS destination, m.confidence AS confidence, m.assignment_reason AS reason, collect(DISTINCT s.id) AS signals, collect(DISTINCT dec.description) AS decisions, collect(DISTINCT g.id) AS co_dependency_groups"}
```

### D. Migration risk hotspots — high traffic and risk and anti-patterns

```json
{"cypher":"MATCH (st:SourceTable)<-[:READS_FROM]-(q:Query) OPTIONAL MATCH (r:Risk)-[:IMPACTS]->(st) OPTIONAL MATCH (a:AntiPattern)-[:OBSERVED_IN_TABLE]->(st) WITH st, sum(q.calls_per_second) AS total_cps, count(DISTINCT r) AS risks, count(DISTINCT a) AS anti_patterns WHERE risks > 0 OR anti_patterns > 0 RETURN st.id AS table, total_cps, risks, anti_patterns ORDER BY total_cps DESC"}
```

### E. Cross-engine tables — a table split across more than one engine

```json
{"cypher":"MATCH (q:Query)-[:READS_FROM]->(st:SourceTable), (q)-[:MIGRATES_TO]->(d:Destination) WITH st, count(DISTINCT d.engine) AS engine_count, collect(DISTINCT d.engine) AS engines WHERE engine_count > 1 RETURN st.id AS table, engine_count, engines ORDER BY engine_count DESC"}
```

### F. Load-test regressions — queries that got worse after migration

Requires `LoadTestRun` nodes (see [Empty results](#empty-results)).

```json
{"cypher":"MATCH (q:Query)-[:TESTED_IN]->(lt:LoadTestRun) WHERE lt.improvement_factor < 1.0 RETURN q.id, lt.engine, lt.source_p90, lt.target_p90, lt.improvement_factor ORDER BY lt.improvement_factor ASC"}
```

## Empty results

A query that returns `row_count: 0` usually means the pipeline stage that
produces those nodes has not run for the assessment, not that the query is
wrong. The graph only contains what the S3 artifacts contain.

| Missing nodes | Stage that produces them |
|---------------|--------------------------|
| `LoadTestRun` (queries 9, F) | Load test stage (per engine). Absent if load testing did not run or failed. |
| `AccessPattern` (queries 4, 9, A, B) | Schema design stage (DynamoDB / DocumentDB / OpenSearch / ElastiCache). |
| `Risk` (queries 6, D) | Referee-synthesis stage. |
| `AntiPattern` (queries 5, A, D) | Analysis stage (per engine). |

To confirm what exists, run the node census (query 1). To force a fresh build
from current artifacts, `POST /api/v1/assessments/{job_id}/graph/rebuild`.
