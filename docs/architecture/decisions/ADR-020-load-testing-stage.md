# ADR-020: Load Testing Stage Architecture

**Status:** Approved
**Date:** 2026-05-08
**Deciders:** Database Modernizer Architecture Team

## Context

The modernization pipeline produces schema designs and access pattern recommendations, but currently offers no empirical validation that the recommended target database will meet performance requirements. Customers need concrete evidence — measured latency percentiles, real cost data, and production-ready code — before committing to a migration.

Project requirements explicitly require: execute load tests against target databases, measure P50/P95/P99 latency, and compare to source baseline. The pipeline already has placeholder hooks: `Phase.LOAD_TEST` enum, `load_test: null` in query journey files, and a comment in the Step Functions state machine marking where this phase inserts.

The load testing stage must:

1. Deploy real target infrastructure (DynamoDB tables with designed schema, GSIs, on-demand capacity)
2. Seed synthetic data at realistic volumes with proper cardinality
3. Generate best-practice code per access pattern (multi-step operations, pagination, scatter-gather)
4. Execute all access patterns concurrently at source call rates for 15-30 minutes
5. Measure per-pattern latency (p50 through p999) under realistic contention
6. Capture real cost via ConsumedCapacity (verified async via Cost Explorer)
7. Provide the generated code as a customer deliverable (copy-paste ready)
8. Tear down all infrastructure after test completion

## Decision

### Execution Model: k6 on ECS with Coordinator

The load testing stage uses **k6** as the load generation engine, running on ECS Fargate. An ECS coordinator task orchestrates infrastructure provisioning, data seeding, script generation, k6 execution, and teardown.

**Why k6 on ECS over alternatives:**

| Alternative                                             | Rejection reason                                                                                                                     |
| ------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| Single ECS task with custom async Python load generator | Reinvents rate control, percentile calculation, and reporting that k6 provides out of the box                                        |
| Lambda Fleet (one Lambda per pattern)                   | Higher complexity: dynamic IAM roles, zip packaging, 15-min timeout re-invocation, metric collection from N writers, Lambda teardown |
| Step Functions Distributed Map                          | Over-engineered for v1; adds state transition costs; more complex to debug                                                           |

**Why k6 on ECS wins:**

- **Built-in load testing primitives** — `constant-arrival-rate` executor, percentile calculation (p50-p999), thresholds, warmup support
- **k6 AWS jslib** — native DynamoDB client with SigV4 signing; supports `ReturnConsumedCapacity` per request (no custom Go extension needed)
- **Consistent infrastructure** — runs on ECS Fargate like all other agents; no new deployment model
- **Proven** — already used in this project to generate realistic traffic for Discourse and WordPress scenarios
- **The test script IS the customer deliverable** — k6 scenarios are JavaScript that implements the access pattern with the AWS SDK; customer gets working code that produced the exact results
- **Single process, simple orchestration** — one k6 container runs all scenarios concurrently; no N-Lambda coordination
- **Scalable when needed** — split scenarios across multiple ECS tasks if total RPS exceeds single-container capacity

### Architecture

```
ECS Coordinator (AGENT_TYPE=load_test)
├── Reads: schema design output, collector output from S3
├── Provisions: target infrastructure (DynamoDB tables, GSIs)
├── Seeds: synthetic data with entity relationships
├── Generates: k6 test scripts per access pattern
├── Stores: scripts in S3 (auditability + customer deliverable)
├── Dry-runs: k6 with --iterations 1 per scenario (fail-fast validation)
├── Executes: k6 with all scenarios concurrent (15-30 min sustained)
├── Collects: k6 JSON output + custom ConsumedCapacity metrics
├── Computes: before/after comparison, cost aggregation
├── Writes: results to S3 artifact store + enriches query journeys
└── Tears down: all deployed resources
```

### k6 Execution Model

k6 runs all access pattern scenarios concurrently within a single process. Each scenario uses the `constant-arrival-rate` executor configured to match the source `calls_per_second` from the collector output.

```javascript
export const options = {
  scenarios: {
    pattern_0452bed2: {
      executor: "constant-arrival-rate",
      rate: 8, // source calls_per_second (rounded)
      timeUnit: "1s",
      duration: "15m",
      preAllocatedVUs: 10,
      maxVUs: 50,
    },
    pattern_7a3f1c09: {
      executor: "constant-arrival-rate",
      rate: 2,
      timeUnit: "1s",
      duration: "15m",
      preAllocatedVUs: 5,
      maxVUs: 20,
    },
    // ... one scenario per access pattern
  },
};
```

**Splitting strategy:** For most workloads (15-30 patterns at source rates), a single ECS task (4 vCPU, 16GB) handles the load — k6 at 4 vCPUs sustains 5,000-10,000 RPS easily. If total target RPS exceeds capacity, the coordinator splits scenarios across multiple k6 ECS tasks, each running a subset of patterns.

### Data Seeding Strategy

- **Synthetic generation** — derive entity graph from schema design output (entities, relationships, cardinalities)
- **Scale** — match source cardinality from collector stats (configurable scale factor)
- **Write distribution** — uniform across partition key space (prevents artificial hot partitions during seeding)
- **Read distribution** — Zipfian (alpha=1.07) during test execution (realistic access skew)
- **Key registry** — stored in S3 after seeding; loaded by k6 scripts to select realistic keys

**Documented assumption:** Synthetic data with uniform write distribution and Zipfian read distribution. Not a replica of production data patterns.

### Code Generation and Customer Deliverable

The generated k6 scripts serve dual purpose: **test execution AND customer deliverable**. The same JavaScript code that runs the load test is what the customer receives.

Generated scripts use the k6 AWS jslib (`jslib.k6.io/aws`) which provides:

- DynamoDB client with SigV4 signing
- `ReturnConsumedCapacity: 'TOTAL'` on every operation
- Multi-step operations (Query + BatchGetItem, paginated reads, transactions)

Code generation uses a mix of deterministic templates (for common patterns like GetItem, Query) and LLM-assisted generation (for complex multi-step patterns like scatter-gather). The schema design output specifies the access pattern strategy; the code generator translates it to executable k6 JavaScript.

**Future:** Python deliverables as a translated alternative (deferred to later sprint).

### Cost Measurement: Hybrid Approach

- **Real-time (primary):** ConsumedCapacity returned on every AWS SDK call via k6 jslib — precise per-operation cost captured as k6 custom metrics
- **Async verification:** Cost Explorer query 24h after test — filter by `run_id` tag for precise attribution
- **Rationale:** Cost Explorer has 12-24h delay and hourly granularity. ConsumedCapacity is immediate and per-request accurate. Tags include `run_id` to isolate costs per test execution.

### Engine Extensibility

Abstract `LoadTestEngine` base class with engine-specific implementations:

- `DynamoDBLoadTestEngine` — first implementation (this ADR); uses k6 AWS jslib
- `OpenSearchLoadTestEngine` — future; uses k6 HTTP module (OpenSearch is REST-native)
- `ElastiCacheLoadTestEngine` — future; uses xk6-redis community extension or HTTP proxy
- `DocumentDBLoadTestEngine` — future; uses xk6-mongo community extension or HTTP proxy

Generic coordinator logic (k6 orchestration, results collection, percentile aggregation, artifact writing) stays engine-agnostic.

### Artifact Layout

```
{db}/{job}/load-test/v{N}/
├── config.json                     # TestConfig used for this run
├── infrastructure.json             # Deployed resources (ARNs, schemas, tags)
├── seed-manifest.json              # Seeding details + key registry reference
├── scripts/
│   ├── main.js                     # k6 entry point (imports all scenarios)
│   ├── {query_id}.js               # Per-pattern k6 scenario (customer deliverable)
│   └── lib/
│       ├── aws-client.js           # Shared DynamoDB client setup
│       ├── key-generator.js        # Zipfian/uniform key selection
│       └── metrics-collector.js    # Custom k6 metrics (ConsumedCapacity)
├── dry-run/
│   └── results.json                # Pass/fail per pattern
├── results/
│   ├── summary.json                # Aggregate: total cost, duration, assumptions
│   ├── {query_id}.json             # Per-pattern: latency percentiles, throughput, cost
│   └── raw/
│       └── k6-output.json          # Full k6 JSON output
├── comparison.json                 # Before/after: source vs target per pattern
└── teardown.json                   # Resource cleanup confirmation
```

Versioned per test run (`v{N}`). Each run is immutable and fully auditable.

### Pipeline Integration

- **Decoupled** from main pipeline orchestration — triggered after schema design but does not block synthesis
- **Phase status** reported via existing `Phase.LOAD_TEST` enum and API phase endpoints
- **Query journey enrichment** via `materialize_load_test()` (pattern established in ADR-019)
- **No new UI** in v1 — results appear in existing query journey detail view

### Test Parameters

| Parameter                    | Default | Range    |
| ---------------------------- | ------- | -------- |
| `duration_minutes`           | 15      | 15-30    |
| `min_iterations_per_pattern` | 10,000  | 10,000+  |
| `scale_factor`               | 1.0     | 0.1-10.0 |
| `zipfian_alpha`              | 1.07    | 1.0-2.0  |
| `warmup_seconds`             | 30      | 10-60    |

### Failure Handling

| Failure mode                            | Detection                         | Behavior                                                           |
| --------------------------------------- | --------------------------------- | ------------------------------------------------------------------ |
| Provision fails (service limits)        | SDK/CloudFormation error          | Abort, write error to results, exit 1                              |
| Seed fails (throughput exceeded)        | BatchWrite exceptions             | Retry with backoff; if persistent, abort                           |
| Code generation produces invalid script | Dry-run catches it                | Abort, report failing patterns                                     |
| Dry-run fails (any pattern)             | k6 non-zero exit or error metrics | Abort entire test, teardown, exit 1                                |
| k6 scenario fails mid-test              | k6 error metrics per scenario     | Mark pattern as failed; other scenarios continue                   |
| Coordinator timeout                     | Duration exceeded by 2x           | Force-stop k6, collect available results, teardown                 |
| Teardown fails (resource stuck)         | Delete API error                  | Log warning, report in teardown.json, exit 0 (results still valid) |

## Consequences

### Positive

- Customers get empirical latency validation with real infrastructure
- k6 scripts are the customer deliverable — same code that produced the numbers
- Proven load testing tool with built-in rate control, percentiles, and reporting
- Consistent with existing ECS infrastructure (no new deployment model)
- k6 AWS jslib provides per-request ConsumedCapacity without custom extensions
- Engine-extensible: k6 HTTP module covers OpenSearch natively, community extensions for Redis/MongoDB
- Decoupled from main pipeline — can be re-run, skipped, or iterated independently
- Before/after comparison uses real source metrics (already captured by collector)

### Negative

- Incurs real AWS costs in customer account (DynamoDB on-demand + ECS task)
- Test duration adds 20-40 minutes to total pipeline wall-clock time
- k6 scripts are JavaScript — customers preferring Python need a future translation step
- DocumentDB and ElastiCache engines require community k6 extensions or proxy sidecar (future concern)
- Code generation quality is bounded by schema design quality

### Risks

- k6 single-container capacity may be insufficient for extremely high aggregate RPS workloads (mitigated by split strategy)
- Code generation quality is bounded by schema design quality — garbage schema in, garbage test out

## References

- Project requirements: Load Testing requirement
- ADR-016: Compute and Orchestration Strategy (ECS Fargate + Step Functions)
- ADR-019: Query Journey Materialization (progressive file enrichment pattern)
- k6 AWS jslib: `jslib.k6.io/aws` (DynamoDB client with SigV4)
- `src/contracts/phase_models.py`: Phase.LOAD_TEST enum
- `infrastructure/cloudformation/orchestration.yaml`: insertion point comment
