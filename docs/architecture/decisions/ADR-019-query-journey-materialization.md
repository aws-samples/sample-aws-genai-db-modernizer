# ADR-019: Query Journey Materialization

**Status:** Approved
**Date:** 2026-05-04
**Deciders:** Database Modernizer Assessment Architecture Team

## Context

The modernization pipeline produces data about each query across multiple stages: the collector captures the original SQL and performance metrics, the assignment resolver routes each query to a target engine, and the schema design agent translates each query into the new engine's data model. Future stages will add load test results and generated SDK code.

Today, this data lives in separate S3 artifacts (`collector/output.json`, `assignment/v{N}/assignment.json`, `schema-{engine}/v{N}/schema_output.json`), each with its own structure. To show the full modernization story for one query, the API would need to:

1. Read collector output (can contain 500+ queries), scan for the matching `query_id`
2. Read the latest assignment, scan `query_assignments[]` for the matching `query_id`
3. Read the schema design output for the assigned engine, scan `access_patterns[]` for entries where `query_ids` contains the target `query_id`
4. Filter `trade_offs[]` for entries referencing the `query_id`

That's 3 S3 reads and 4 linear scans per API call — and it gets worse. When load testing and code generation are added, it becomes 5+ reads. Every UI page view repeats this work.

The UI needs a drill-down view: the user sees a query in the assignments table or results view, clicks it, and expects instant detail. A multi-artifact join on every click is not the right read path for this.

## Decision

**Materialize per-query journey files in S3, written progressively by each pipeline stage.**

### S3 path

```
s3://<bucket>/<database-name>/<job-id>/query-journeys/<query_id>.json
```

### Write strategy

Each pipeline stage's handler already has the relevant data in memory after producing its own artifact. After writing its primary artifact, the handler also writes (or updates) the corresponding query journey files:

| Pipeline stage             | Writes                                   | Journey section populated |
| -------------------------- | ---------------------------------------- | ------------------------- |
| Collector                  | Creates `<query_id>.json` for each query | `source`                  |
| Assignment resolution      | Reads + updates each file                | `assignment`              |
| Schema design (per engine) | Reads + updates each file                | `design`                  |
| Load test (future)         | Reads + updates each file                | `load_test`               |
| Code generation (future)   | Reads + updates each file                | `sdk_code`                |

The materializer is **not** a separate Lambda or Step Functions state. It runs inline in each handler — a small function that projects the relevant fields and writes the journey file. This keeps the pipeline topology unchanged.

### Read paths

**Detail (single query):**

```
GET /api/v1/assessments/{job_id}/query-journeys/{query_id}
```

No query params. The backend resolves `database_name` from the Step Functions execution input (same pattern as `results.py` via `_get_database_name(job_id)`), then does a single S3 GET. No joins, no scans, no assembly.

**List (all queries, paginated):**

```
GET /api/v1/assessments/{job_id}/query-journeys?page=1&page_size=50
```

Lists all files under `{db}/{job}/query-journeys/` via `list_prefix()`, applies offset-based pagination, and reads each file in the current page. Designed for bulk download (offline report generation — PDF/HTML). For a 1600-query workload at `page_size=200`, the UI makes 8 requests to download everything.

| Param       | Default | Max | Description         |
| ----------- | ------- | --- | ------------------- |
| `page`      | 1       | —   | 1-based page number |
| `page_size` | 50      | 200 | Items per page      |

Response includes `total`, `page`, `page_size`, `total_pages`, and `items[]`.

### Schema revisions

When a schema revision produces v2+, the materializer overwrites the `design` section with the latest version's data. The journey file always reflects the current design state. Historical versions remain accessible via the existing `GET /{job_id}/schema/{engine}/versions` endpoint.

### Consistency model

Pipeline stages run sequentially in Step Functions. There are no concurrent writes to the same journey file. The read-modify-write cycle per file is safe without locking.

If a stage fails and retries, the materializer is idempotent — writing the same data again produces the same result.

## Alternatives Considered

### 1. Join at read time (no materialization)

The API reads collector, assignment, and schema design artifacts on every request and joins them in memory.

**Rejected because:**

- 3 S3 reads per request today, 5+ when load test and code gen are added
- Every UI page view repeats the same work
- Collector output can be large (500+ queries); scanning it for one `query_id` is wasteful on every click
- The join logic must be updated every time a new pipeline stage is added

### 2. DynamoDB with GSI on query_id

Store journey data in a DynamoDB table. Each pipeline stage writes a partial item; a GSI on `query_id` enables direct lookup.

**Rejected because:**

- Introduces a new data store that doesn't exist in the current architecture
- All other artifacts are in S3 — adding DynamoDB for one endpoint breaks the "S3 is the source of truth" pattern (ADR-016)
- The journey files are small (2-5KB) and read infrequently (on user click) — DynamoDB's sub-millisecond latency is unnecessary
- S3 GET latency (50-100ms) is more than adequate for a detail view

### 3. Pre-compute a single index file per job

Write one `query-journeys/index.json` containing all queries' journey data.

**Rejected because:**

- For 500 queries, the index file grows to 1-2MB — too large for a detail-view API that returns one query
- Progressive writes mean rewriting the entire index on every stage completion
- Per-file approach avoids read amplification and write contention

## Consequences

### Positive

- **One S3 read per API call** — fast, predictable, no scaling concerns
- **Progressive enrichment** — the journey file grows as the pipeline progresses; the UI can show partial state (e.g., "source metrics available, schema design pending")
- **Naturally extensible** — adding a new pipeline stage means adding one materializer function; no existing endpoints or join logic change
- **Consistent with ADR-016** — S3 remains the single source of truth for all artifacts

### Negative

- **Write amplification** — each stage does N S3 PUTs (one per query). For a 200-query workload across 3 stages: ~1000 S3 operations, costing < $0.01
- **Eventual consistency with primary artifacts** — if a handler crashes between writing its primary artifact and the journey files, the journey files may lag. This is acceptable: the primary artifacts are the source of truth, and a retry of the handler will catch up the journey files
- **Duplicate data** — journey files duplicate data from collector, assignment, and schema artifacts. This is intentional: the journey file is a read-optimized materialized view, not a source of truth. The primary artifacts remain authoritative

## Impact on Future Agents

Any new pipeline stage that produces per-query data **must** contribute to the query journey files. This is a small, mechanical requirement — not a design decision. The pattern is always the same:

### 1. Add a materializer function

In `src/agents/query_journey_materializer.py`, add one function that projects your stage's output into the journey file. Follow the existing pattern:

```python
def materialize_load_test(
    load_test_results: LoadTestOutput,
    db_name: str,
    job_id: str,
    store: ArtifactStore,
) -> None:
    """Enrich query journey files with load test results."""
    for result in load_test_results.query_results:
        path = f"{db_name}/{job_id}/query-journeys/{result.query_id}.json"
        journey = store.read_json(path)
        if journey is None:
            continue  # query not in pipeline — skip silently

        journey["load_test"] = {
            "tool": load_test_results.tool,
            "run_id": load_test_results.run_id,
            "timestamp": load_test_results.timestamp.isoformat(),
            "target_engine": result.target_engine,
            "results": {
                "latency_ms_avg": result.latency_ms_avg,
                "latency_ms_p50": result.latency_ms_p50,
                "latency_ms_p95": result.latency_ms_p95,
                "latency_ms_p99": result.latency_ms_p99,
                "throughput_rps": result.throughput_rps,
                "error_rate_percent": result.error_rate_percent,
                "throttle_count": result.throttle_count,
            },
            "comparison": {
                "latency_reduction_pct": _calc_reduction(
                    journey["source"]["performance"]["execution_time_ms_p95"],
                    result.latency_ms_p95,
                ),
                "source_p95_ms": journey["source"]["performance"]["execution_time_ms_p95"],
                "target_p95_ms": result.latency_ms_p95,
            },
        }
        store.write_json(path, journey)
```

### 2. Call it from your handler

After writing your primary artifact, add one line:

```python
# In your handler, after store.write_json(primary_artifact_key, output_data):
from src.agents.query_journey_materializer import materialize_load_test

materialize_load_test(load_test_output, database_name, job_id, store)
```

### 3. Checklist for new stages

- [ ] Define the journey section shape in the spec
- [ ] Add a `materialize_<stage>()` function in `query_journey_materializer.py`
- [ ] Call it from your handler after writing the primary artifact
- [ ] Add tests: verify the journey file has the new section populated, verify idempotency on retry
- [ ] Update the "Progressive response states" table in the spec

The materializer module, the journey file schema, and the API endpoint do **not** need to change — only the new function and the handler call.

## References

- [ADR-016: Compute and Orchestration Strategy](ADR-016-compute-and-orchestration-strategy.md) — S3 artifact path convention
- Query Journey Detail Endpoint Spec — full API specification
