# Database Modernizer Assessment — API Specification

**Version:** 1.3
**Date:** April 24, 2026
**Base URL:** `/api/v1`

---

## Overview

The parent API is a read/write orchestration layer between the UI and the underlying infrastructure. It does NOT run agents directly — it starts Step Functions executions and reads agent artifacts from S3. Each agent writes its output to a well-known S3 path per ADR-016:

```
s3://<bucket>/<database-name>/<job-id>/<agent-name>/artifact.json
```

The API surfaces these artifacts to the UI in a structured way.

### Workflow Overview

The Step Functions state machine runs a multi-phase pipeline with a human-in-the-loop approval gate between analysis and schema design:

```
RunCollector
  → RunRefereeTriage                              (picks target engines)
    → RunAnalysisPipelines (Map, per engine)      (per-engine analysis)
      → RunAssignmentResolution                   (finalizes query→engine assignment)
        → RunRealityCheck                         (CTO-level engine consolidation)
          → WaitForAssignmentApproval ⏸ PAUSED    (waitForTaskToken — human gate)
            → RunSchemaDesignPipelines (Map)      (per-engine schema design)
              → RunRefereeSynthesis               (final report)
                → CheckDeeperAnalysis (Choice)
                   ├─ loop back to SchemaDesign (up to 2 iterations)
                   └─ JobComplete
```

At `WaitForAssignmentApproval`, a Lambda stores the Step Functions task token in DynamoDB keyed by `job_id`. The execution pauses indefinitely until the UI calls `POST /assessments/{job_id}/resume` with `{"phase": "assignment_review"}`, which reads the token and calls `SendTaskSuccess`. The human gate is placed **after** the reality check so the customer sees the optimized recommendation (consolidated engines, cost savings) before approving. See [§4. Assignment Approval Gate (Human-in-the-Loop)](#4-assignment-approval-gate-human-in-the-loop) for the UI contract.

---

## Design Principles

### 1. Thin Read Layer

The parent API is a thin orchestration and read layer. It never runs agents, transforms data, or adds intelligence. It has exactly two jobs:

- Write: Start Step Functions executions (one endpoint: `POST /assessments`)
- Read: Stitch together data from Step Functions state + S3 artifacts + CloudWatch logs into UI-friendly responses

If the customer wants raw agent output, the artifact proxy endpoints (`/collector`, `/triage`, `/analysis/{type}`) return the S3 files verbatim. The higher-level endpoints (`/results`, `/results/table-mappings`) aggregate across multiple artifacts but don't alter the underlying data.

### 2. Three Data Sources, No Agent Cooperation Required

The parent API reads from three infrastructure-level data sources. Agents don't need to report progress to the API — everything comes from infrastructure:

```
┌─────────────────────────────────────────────────────────────┐
│  Step Functions Execution History                            │
│  → Job-level status (RUNNING, SUCCEEDED, FAILED)            │
│  → Per-agent status (started, completed, failed + timestamps)│
│  → Pipeline progress (which state is active)                 │
│  → ECS task ARNs per Map iteration (for log correlation)     │
└─────────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────────┐
│  S3 Artifacts (contract-compliant JSON)                      │
│  → Collector output, triage decisions, analysis results      │
│  → Synthesis report, schema designs                          │
│  → Available as soon as each agent writes them               │
│  → Path: <db-name>/<job-id>/<agent-name>/artifact.json       │
└─────────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────────┐
│  CloudWatch Logs                                             │
│  → Per-agent stdout/stderr via ECS awslogs driver            │
│  → Automatic — agents just print(), CloudWatch captures it   │
│  → Filter by stream prefix + ECS task ID                     │
└─────────────────────────────────────────────────────────────┘
```

### 3. Phase 0 Progress Strategy

Per-agent status comes from Step Functions `GetExecutionHistory` — each state in the state machine maps to an agent, so we get `started`, `completed`, `failed` with timestamps per agent for free. No agent code changes needed.

What Phase 0 provides:

- Job status: PENDING → RUNNING → SUCCEEDED / FAILED
- Per-agent status: pending → in-progress → completed / failed (with duration)
- Pipeline view: which agents have run, which are running, which are pending
- Results: available immediately when each agent writes to S3

What Phase 0 does NOT provide (deferred to Phase 1):

- Intra-agent progress ("collector is 60% done, processing table 742 of 1247")
- Mini-step reporting (ADR-016 EventBridge → WebSocket)
- Real-time push updates (UI uses polling via auto-refresh interval from settings)

### 4. Log Architecture

ECS Fargate's `awslogs` driver automatically captures everything agents write to stdout/stderr and sends it to CloudWatch. Agents don't proxy to CloudWatch — they just `print()`.

Current setup:

- All agents write to one log group: `/ecs/{project}-{env}`
- Each agent type has a stream prefix: `collector`, `referee-triage`, `analysis`, `referee-synthesis`, `schema-design`
- All analysis agents (DynamoDB, DocumentDB, ElastiCache, etc.) share the `analysis` prefix since they use one shared task definition
- Each ECS task invocation gets a unique task ID in the stream name

To filter logs for a specific analysis agent (e.g., DynamoDB):

1. Step Functions execution history gives you the ECS task ARN for each Map iteration
2. The task ARN contains the task ID
3. The CloudWatch stream name is `analysis/<task-id>`
4. The `/logs` endpoint uses this mapping to filter per agent type

### 5. Separation of Concerns

```
UI → Parent API (aggregation + formatting for UI consumption)
       ↓ reads from
     Step Functions (execution state — pipeline progress)
     S3 artifacts (contract outputs — results data)
     CloudWatch (logs — debugging and transparency)
       ↓ written by
     Individual agents (collector, analysis, referee, schema-design)
```

The parent API never talks to agents directly. Agents never talk to the parent API. They communicate through shared infrastructure (S3 artifacts + Step Functions state). This means:

- Agents can be developed and tested independently
- The parent API can be developed against mock S3 artifacts
- The UI can be developed against mock API responses
- All three can integrate later without coupling

---

## Endpoints Summary

| Method | Path | UI Page | Purpose |
| ------ | ---- | ------- | ------- |
| `GET` | `/health` | — | ALB health check |
| `POST` | `/api/v1/assessments/prepare` | CreateAnalysis | Pre-create job ID + presigned upload URL |
| `POST` | `/api/v1/assessments/{job_id}/uploads/confirm` | CreateAnalysis | Confirm file upload completed |
| `GET` | `/api/v1/assessments/{job_id}/uploads` | CreateAnalysis | List uploaded files |
| `DELETE` | `/api/v1/assessments/{job_id}/uploads/{filename}` | CreateAnalysis | Delete uploaded file |
| `POST` | `/api/v1/assessments` | CreateAnalysis | Start new assessment |
| `GET` | `/api/v1/assessments` | Dashboard | List all assessments |
| `GET` | `/api/v1/assessments/{job_id}` | JobMonitoring | Assessment status + progress |
| `DELETE` | `/api/v1/assessments/{job_id}` | Dashboard | Cancel/delete assessment |
| `GET` | `/api/v1/assessments/{job_id}/agents` | JobMonitoring | Per-agent status + artifact summaries |
| `GET` | `/api/v1/assessments/{job_id}/execution-history` | JobMonitoring | Full SFN execution history (all state types) |
| `GET` | `/api/v1/assessments/{job_id}/logs` | JobMonitoring | Execution logs |
| `GET` | `/api/v1/assessments/{job_id}/phases` | JobMonitoring | Phase progression state |
| `GET` | `/api/v1/assessments/{job_id}/reality-check` | AssignmentReview | Reality check output (consolidations, before/after distribution) |
| `GET` | `/api/v1/assessments/{job_id}/assignments` | AssignmentReview | Current query→engine assignment |
| `PUT` | `/api/v1/assessments/{job_id}/assignments` | AssignmentReview | Override assignments or narrow scope |
| `POST` | `/api/v1/assessments/{job_id}/resume` | AssignmentReview | Release the approval gate and continue execution |
| `GET` | `/api/v1/assessments/{job_id}/results` | AnalysisResults | Full results (recommendations, TCO, risks) |
| `GET` | `/api/v1/assessments/{job_id}/results/table-mappings` | AnalysisResults | Paginated table mappings |
| `GET` | `/api/v1/assessments/{job_id}/collector` | ExecutionDetails | Collector output artifact |
| `GET` | `/api/v1/assessments/{job_id}/triage` | ExecutionDetails | Triage decisions |
| `GET` | `/api/v1/assessments/{job_id}/schema-designs` | AnalysisResults | Schema design artifacts |
| `GET` | `/api/v1/settings` | Settings | Current settings |
| `PUT` | `/api/v1/settings` | Settings | Update settings |
| `POST` | `/api/v1/settings/test-connection` | Settings | Test AWS connectivity |

---

## 1. Health

### `GET /health`

```json
// Response 200
{
  "status": "healthy",
  "version": "a1b2c3d4e5f6"  <!-- pragma: allowlist secret -->
}
```

---

## 2. Assessments — Offline Upload Flow

The offline assessment flow allows users to upload pre-collected database output (from our collection scripts) and run the analysis pipeline without a direct database connection.

### Flow

```
1. POST /assessments/prepare        → Get job ID + presigned upload URL
2. PUT  <presigned_url>              → Upload collector output file to S3
3. POST /assessments/{id}/uploads/confirm → Verify upload landed in S3
4. POST /assessments                 → Start assessment with job_id + offline mode
```

### `POST /api/v1/assessments/prepare`

**UI:** CreateAnalysis page → "Upload offline collection" mode
**Backend:** Creates job ID, generates presigned S3 upload URL

```json
// Request
{
  "database_name": "forum_db",
  "source_database_type": "mysql"
}
```

```json
// Response 201
{
  "job_id": "417d966a-7bc2-4c33-a4f5-fedf6cff4c1f",
  "upload_prefix": "forum_db/417d966a-7bc2-4c33-a4f5-fedf6cff4c1f/uploads/",
  "upload_bucket": "modernizer-dev-storage-bucket",
  "upload_url": "https://modernizer-dev-storage-bucket.s3.amazonaws.com/forum_db/417d966a.../uploads/collector-output.json?AWSAccessKeyId=...&Signature=...&Expires=...",
  "upload_key": "forum_db/417d966a-7bc2-4c33-a4f5-fedf6cff4c1f/uploads/collector-output.json",
  "status": "PREPARED",
  "created_at": "2026-03-23T20:34:14.208490+00:00",
  "expires_in_seconds": 3600
}
```

The client uploads the file directly to S3 using the presigned URL (PUT request with `Content-Type: application/json`). No AWS credentials needed on the client side.

### `POST /api/v1/assessments/{job_id}/uploads/confirm`

**UI:** Called automatically after presigned URL upload completes
**Backend:** Verifies the file exists in S3

```json
// Query params: ?database_name=forum_db

// Response 200
{
  "job_id": "417d966a-7bc2-4c33-a4f5-fedf6cff4c1f",
  "status": "confirmed",
  "filename": "collector-output.json",
  "size_bytes": 34636,
  "upload_key": "forum_db/417d966a-7bc2-4c33-a4f5-fedf6cff4c1f/uploads/collector-output.json"
}
```

### `GET /api/v1/assessments/{job_id}/uploads`

**UI:** Shows uploaded file before starting analysis
**Backend:** Lists files in the upload prefix

```json
// Query params: ?database_name=forum_db

// Response 200
{
  "job_id": "417d966a-7bc2-4c33-a4f5-fedf6cff4c1f",
  "uploads": [
    {
      "key": "forum_db/417d966a.../uploads/collector-output.json",
      "filename": "collector-output.json",
      "size_bytes": 34636,
      "last_modified": "2026-03-23T20:35:00+00:00"
    }
  ]
}
```

### `DELETE /api/v1/assessments/{job_id}/uploads/{filename}`

**UI:** "Remove file" button if user uploaded the wrong file
**Backend:** Deletes the file from S3

```json
// Query params: ?database_name=forum_db

// Response 200
{
  "job_id": "417d966a-7bc2-4c33-a4f5-fedf6cff4c1f",
  "filename": "collector-output.json",
  "status": "deleted"
}
```

---

## 3. Assessments — Lifecycle

### `POST /api/v1/assessments`

**UI:** CreateAnalysis page → "Start analysis" button
**Backend:** Triggers Step Functions execution

> **⚠️ Critical: `database_name` must match the actual database name**
>
> `database_name` is used as the table ID prefix throughout the entire pipeline
> (e.g., `{database_name}.users`, `{database_name}.orders`). It must exactly
> match the real database name in MySQL/PostgreSQL — not an arbitrary label.
>
> - **Live mode:** use the database name from the connection string (`connection.database`)
> - **Offline mode:** use the database name embedded in the collected JSON file
>   (visible in `metadata.source_database.database_name` of the collector output)
>
> Passing the wrong name causes table ID mismatches between the collector,
> assignment resolver, and schema design agents, resulting in all schema designs
> being skipped and `access_patterns: 0` in the final results.

**Live mode** — connects to the database directly via an automation instance:

```json
// Request (live mode — default)
{
  "source_database_type": "mysql",
  "database_name": "ecommerce_prod",
  "connection": {
    "host": "mysql-prod-01.example.com",
    "port": 3306,
    "database": "ecommerce",
    "credentials_type": "secrets-arn",
    "secret_arn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:prod/db/credentials"  <!-- pragma: allowlist secret -->
  },
  "options": {
    "anonymize_pii": true,
    "include_sample_data": true,
    "sample_size": 1000,
    "query_log_period_days": 7,
    "query_log_source": "performance-insights",
    "include_table_patterns": "orders%, customer%",
    "exclude_table_patterns": "temp_%, test_%",
    "top_sql_queries": 100,
    "top_wait_events": 50
  },
  "target_databases": ["dynamodb", "documentdb", "elasticache", "aurora"],
  "full_analysis": false
}
```

**Offline mode** — parses a pre-collected JSON file from S3 instead of connecting to the database. Useful when direct database access is not available or when working with data collected by the `collect-mysql.sql` / `collect-postgres.sql` scripts. The collector reads the JSON from S3, extracts schema and metadata, and still pulls CloudWatch/Performance Insights metrics via AWS APIs using the connection endpoint:

```json
// Request (offline mode — with pre-created job ID from /prepare)
{
  "source_database_type": "mysql",
  "database_name": "forum_db",
  "collection_mode": "offline",
  "offline_s3_key": "forum_db/417d966a-7bc2-4c33-a4f5-fedf6cff4c1f/uploads/collector-output.json",
  "job_id": "417d966a-7bc2-4c33-a4f5-fedf6cff4c1f",
  "target_databases": ["dynamodb"],
  "full_analysis": false
}
```

```json
// Request (offline mode — without pre-created job ID, auto-generates one)
{
  "source_database_type": "mysql",
  "database_name": "ecommerce_prod",
  "collection_mode": "offline",
  "offline_s3_key": "uploads/ecommerce_prod/collection-output.json",
  "target_databases": ["dynamodb", "documentdb", "elasticache"],
  "full_analysis": false
}
```

```json
// Response 202
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "PENDING",
  "created_at": "2026-02-23T14:30:00Z",
  "estimated_completion_time": "2026-02-23T20:30:00Z",
  "execution_arn": "arn:aws:states:us-east-1:123456789012:execution:modernizer-dev-job-orchestrator:550e8400..."
}
```

### `GET /api/v1/assessments`

**UI:** Dashboard → Recent analyses table
**Backend:** Query DynamoDB job metadata table (or Step Functions ListExecutions)

```json
// Query params: ?status=RUNNING&limit=25&offset=0&sort=created_at:desc

// Response 200
{
  "assessments": [
    {
      "job_id": "550e8400-e29b-41d4",
      "source_database_type": "mysql",
      "database_name": "ecommerce_prod",
      "status": "COMPLETED",
      "created_at": "2026-02-23T10:00:00Z",
      "completed_at": "2026-02-23T14:12:00Z",
      "duration_seconds": 15120,
      "progress_percent": 100
    },
    {
      "job_id": "6ba7b810-9dad-11d1",
      "source_database_type": "postgresql",
      "database_name": "analytics_db",
      "status": "RUNNING",
      "created_at": "2026-02-23T13:30:00Z",
      "completed_at": null,
      "duration_seconds": 1800,
      "progress_percent": 45
    }
  ],
  "total_count": 24,
  "limit": 25,
  "offset": 0
}
```

### `GET /api/v1/assessments/{job_id}`

**UI:** JobMonitoring page header + pipeline progress
**Backend:** Step Functions DescribeExecution + S3 artifact existence checks

```json
// Response 200
{
  "job_id": "550e8400-e29b-41d4",
  "status": "RUNNING",
  "source_database_type": "mysql",
  "database_name": "ecommerce_prod",
  "created_at": "2026-02-23T14:23:15Z",
  "execution_arn": "arn:aws:states:...",
  "progress": {
    "percent_complete": 45,
    "current_stage": "analysis",
    "current_activity": "Running analysis agents (5 of 7 completed)",
    "estimated_remaining_seconds": 8100,
    "stages": [
      { "name": "collector", "status": "completed", "duration_seconds": 900 },
      { "name": "referee-triage", "status": "completed", "duration_seconds": 120 },
      { "name": "analysis-dynamodb", "status": "completed", "duration_seconds": 480 },
      { "name": "analysis-documentdb", "status": "completed", "duration_seconds": 360 },
      { "name": "analysis-elasticache", "status": "in-progress", "duration_seconds": 480 },
      { "name": "analysis-aurora", "status": "pending", "duration_seconds": null },
      { "name": "analysis-opensearch", "status": "pending", "duration_seconds": null },
      { "name": "referee-synthesis", "status": "pending", "duration_seconds": null },
      { "name": "schema-design-dynamodb", "status": "pending", "duration_seconds": null }
    ]
  }
}
```

### `DELETE /api/v1/assessments/{job_id}`

**UI:** Dashboard → Actions dropdown → Delete / Cancel job button
**Backend:** StopExecution (if running) + cleanup S3 artifacts

```json
// Response 200
{
  "job_id": "550e8400-e29b-41d4",
  "status": "CANCELLED",
  "message": "Assessment cancelled successfully"
}
```

---

## 4. Assignment Approval Gate (Human-in-the-Loop)

After analysis, assignment, and reality check complete, the Step Functions execution pauses at `WaitForAssignmentApproval` until the UI explicitly releases it. This gate lets customers review the optimized recommendation (which engines survived, what was consolidated, cost savings), optionally override individual query assignments, and narrow scope before schema design runs.

### Pipeline Phase Order

```
collect_triage → analysis → assignment → reality_check → assignment_review → schema_design → synthesis
```

The reality check runs **before** the human gate so the customer sees the consolidated picture (e.g., "we eliminated DocumentDB, saving $500/mo") rather than the raw analysis output.

### Flow

```
1. GET  /assessments/{job_id}/phases          → check if current_phase is "assignment_review" with status "awaiting_review"
2. GET  /assessments/{job_id}/reality-check   → load engine consolidations, before/after distribution, patterns
3. GET  /assessments/{job_id}/assignments     → load query-level assignments for detailed review
4. GET  /assessments/{job_id}/triage          → load signals for each query (join via query_id)
5. GET  /assessments/{job_id}/collector       → load query details: SQL text, type, metrics (join via query_id)
6. PUT  /assessments/{job_id}/assignments     → (optional) submit overrides or scope narrowing
7. POST /assessments/{job_id}/resume          → release the gate with {"phase": "assignment_review"}, SFN advances
```

The execution waits indefinitely. No timeout, no auto-approval.

### `GET /api/v1/assessments/{job_id}/reality-check`

**UI:** AssignmentReview page → CTO summary view (hero headline, distribution bars, consolidation cards)
**Backend:** Read `s3://<bucket>/<db>/<job>/reality-check/output.json`

```json
// Response 200
{
  "source_assignment_version": 1,
  "before_distribution": {
    "dynamodb": 73,
    "opensearch": 19,
    "documentdb": 15
  },
  "after_distribution": {
    "dynamodb": 87,
    "opensearch": 20
  },
  "consolidations": [
    {
      "from_engine": "documentdb",
      "to_engine": "dynamodb",
      "query_count": 14,
      "reason": "documentdb provides no unique capabilities — all 15 queries can be served by existing engines",
      "saved_cost_estimate": 500.0
    }
  ],
  "architectural_patterns": [
    {
      "name": "Command Query Responsibility Segregation (CQRS)",
      "description": "Separate write operations from read operations across different databases...",
      "when": "One engine handles most writes and another handles reads/search/analytics",
      "example": "DynamoDB handles all CRUD operations. OpenSearch serves as a read-optimized view...",
      "applies_to": { "write_engine": "dynamodb", "read_engines": ["opensearch"] }
    }
  ],
  "executive_summary": "Your wordpress workload has 105 access patterns across 50 tables. DynamoDB handles the transactional CRUD while OpenSearch serves full-text search queries. Recommended integration pattern: CQRS with DynamoDB Streams feeding OpenSearch.",
  "recommendations": [
    "Use DynamoDB as the primary engine for all transactional workloads...",
    "Set up DynamoDB-to-OpenSearch zero-ETL integration..."
  ],
  "unique_value_assessment": {}
}
```

`executive_summary` = LLM-generated workload summary for the CTO hero card (nullable, UI falls back to client-side summary when absent).
`before_distribution` = what the analysis agents recommended (may include 3+ engines).
`after_distribution` = what the reality check optimized it to (typically 1-2 engines).
`consolidations` = which engines were eliminated, where their queries went, and estimated monthly savings.

Returns 404 if the reality check phase has not completed yet.

### `GET /api/v1/assessments/{job_id}/assignments`

**UI:** AssignmentReview page → advanced view (query-level assignments with overrides)
**Backend:** Read `s3://<bucket>/<db>/<job>/assignment/v{latest}/assignment.json`

Query params: `?database_name={db}` (required)

```json
// Response 200
{
  "assignment": {
    "job_id": "550e8400-...",
    "version": 1,
    "status": "auto_generated",
    "timestamp": "2026-04-23T15:12:00Z",
    "query_assignments": [
      {
        "query_id": "q_001",
        "assigned_engine": "dynamodb",
        "confidence": 87,
        "source_tables": ["users", "sessions"],
        "assignment_reason": "Key-value access pattern; no joins",
        "in_scope": true,
        "customer_override": false,
        "warnings": []
      }
    ],
    "table_assignments": [
      {
        "table_id": "users",
        "primary_engine": "dynamodb",
        "engines": ["dynamodb"],
        "query_count": 12,
        "multi_engine_reason": null
      }
    ],
    "co_dependency_groups": [["q_003", "q_007"]],
    "validation_warnings": [],
    "previous_version": null
  },
  "validation": null,
  "skipped_engines": []
}
```

Returns 404 if no assignment artifact exists yet (the approval gate has not been reached).

### `PUT /api/v1/assessments/{job_id}/assignments`

**UI:** AssignmentReview page → "Save changes" button (optional — only call if the customer edits)
**Backend:** Apply overrides, validate, write new versioned artifact to S3

Query params: `?database_name={db}` (required)

```json
// Request body
{
  "overrides": [
    { "query_id": "q_042", "assigned_engine": "documentdb", "in_scope": true }
  ],
  "scope": {
    "exclude_tables": ["audit_log", "staging_temp"],
    "reason": "Out of migration scope for this iteration"
  }
}
```

Responses:

- **200** — new assignment version written. Response body matches `GET /assignments` with `validation` populated:

    ```json
    {
      "assignment": { "version": 2, "status": "customer_modified", ... },
      "validation": { "valid": true, "warnings": ["..."], "errors": [] },
      "skipped_engines": ["keyspaces"]
    }
    ```

    `skipped_engines` lists engines that now have zero in-scope queries — they will be SKIPPED during schema design.

- **400** — an override references a `query_id` that doesn't exist in the current assignment.
- **404** — no existing assignment to override.
- **422** — validation hard-errored (e.g., a query was reassigned to an engine that was never analyzed). Body:

    ```json
    {
      "detail": {
        "message": "Assignment validation failed with hard errors",
        "errors": ["Query q_042 assigned to 'neptune' but no analysis artifact exists for that engine"],
        "warnings": []
      }
    }
    ```

### `POST /api/v1/assessments/{job_id}/resume`

**UI:** AssignmentReview page → "Approve and continue" button
**Backend:** Read task token from DynamoDB, call `SendTaskSuccess` on SFN

```json
// Request body
{
  "phase": "assignment_review",
  "scope_engines": ["dynamodb", "opensearch"]
}
```

- `phase` (required): must be one of `collect_triage`, `analysis`, `assignment`, `reality_check`, `assignment_review`, `schema_design`, `load_test`, `synthesis`. For the current approval gate, use `"assignment_review"`.
- `scope_engines` (optional): narrows which engines proceed to schema design. Omit to let all engines with in-scope queries proceed.

Responses:

- **200** — `{"job_id": "...", "phase": "assignment_review", "status": "resumed"}` — task token sent, SFN advances to schema design.
- **409** — phase prerequisites are not met, or task token not found (gate not yet reached).
- **503** — orchestrator not configured on the API (deployment misconfig).

If the SFN execution has not actually reached `WaitForAssignmentApproval`, the underlying call fails because no task token has been stored yet. The UI should only offer the Approve button when `GET /assessments/{job_id}/phases` shows `assignment_review` in `awaiting_review` status.

### `GET /api/v1/assessments/{job_id}/phases`

**UI:** JobMonitoring → phase progression indicator
**Backend:** Read `phase_progression` from DynamoDB

```json
// Response 200
{
  "job_id": "550e8400-...",
  "current_phase": "assignment_review",
  "phases": {
    "collect_triage":    { "phase": "collect_triage",    "status": "completed",       "started_at": "...", "completed_at": "...", "iteration": 1 },
    "analysis":          { "phase": "analysis",          "status": "completed",       "started_at": "...", "completed_at": "...", "iteration": 1 },
    "assignment":        { "phase": "assignment",        "status": "completed",       "started_at": "...", "completed_at": "...", "iteration": 1 },
    "reality_check":     { "phase": "reality_check",     "status": "completed",       "started_at": "...", "completed_at": "...", "iteration": 1 },
    "assignment_review": { "phase": "assignment_review", "status": "awaiting_review", "started_at": "...", "iteration": 1 },
    "schema_design":     { "phase": "schema_design",     "status": "not_started",     "iteration": 1 },
    "load_test":         { "phase": "load_test",         "status": "not_started",     "iteration": 1 },
    "synthesis":         { "phase": "synthesis",         "status": "not_started",     "iteration": 1 }
  },
  "assignment_version": 1,
  "total_iterations": 1
}
```

Phase order: `collect_triage → analysis → assignment → reality_check → assignment_review → schema_design → load_test → synthesis`

Phase status values: `not_started`, `in_progress`, `completed`, `failed`, `awaiting_review`, `awaiting_input`, `skipped`.

The UI should show the Assignment Gate page when `current_phase === "assignment_review"` and its status is `awaiting_review`.

---

## 5. Assessments — Agent Details

### `GET /api/v1/assessments/{job_id}/agents`

**UI:** JobMonitoring → Agent status table
**Backend:** Step Functions execution history + S3 artifact summaries for completed agents

```json
// Response 200
{
  "agents": [
    {
      "agent_name": "RunCollector",
      "status": "completed",
      "started_at": "2026-03-23T20:35:00Z",
      "completed_at": "2026-03-23T20:36:30Z",
      "duration_seconds": 90,
      "output_size_bytes": 34636,
      "details": "Collected 6 tables, 24 queries, mysql",
      "artifact_summary": {
        "engine": "mysql",
        "tables_collected": 6,
        "queries_collected": 24,
        "database_size_gb": 0.02
      }
    },
    {
      "agent_name": "RunRefereeTriage",
      "status": "completed",
      "started_at": "2026-03-23T20:36:31Z",
      "completed_at": "2026-03-23T20:36:45Z",
      "duration_seconds": 14,
      "output_size_bytes": 4521,
      "details": "Selected 1 targets, skipped 2",
      "artifact_summary": {
        "selected_agents": ["dynamodb"],
        "skipped_agents": ["neptune", "keyspaces"],
        "confidence_score": 85,
        "signals_detected": 12
      }
    },
    {
      "agent_name": "RunAnalysis",
      "status": "completed",
      "started_at": "2026-03-23T20:36:46Z",
      "completed_at": "2026-03-23T20:37:16Z",
      "duration_seconds": 30,
      "output_size_bytes": 12800,
      "details": "6 tables analyzed, 76% confidence, $33.37/mo est.",
      "artifact_summary": {
        "target_database": "dynamodb",
        "tables_analyzed": 6,
        "avg_confidence_score": 76,
        "patterns_detected": 6,
        "anti_patterns_detected": 1,
        "monthly_cost_usd": 33.37
      }
    },
    {
      "agent_name": "RunRefereeSynthesis",
      "status": "completed",
      "started_at": "2026-03-23T20:37:17Z",
      "completed_at": "2026-03-23T20:37:25Z",
      "duration_seconds": 8,
      "output_size_bytes": 2100,
      "details": "Top recommendation: dynamodb (76% confidence)",
      "artifact_summary": {
        "ranking": [{"target": "dynamodb", "confidence_score": 76}],
        "recommended_schema_designs": ["dynamodb"],
        "summary_text": "Analyzed 6 tables across 1 target database(s). Top recommendation: dynamodb with 76% average confidence."
      }
    },
    {
      "agent_name": "RunSchemaAgent",
      "status": "pending",
      "started_at": null,
      "completed_at": null,
      "duration_seconds": null,
      "output_size_bytes": null,
      "details": null,
      "artifact_summary": null
    }
  ]
}
```

Completed agents include `artifact_summary` with key metrics extracted from their S3 output, and `details` with a short human-readable summary derived from the artifact. Pending and running agents have both fields as `null`.

### `GET /api/v1/assessments/{job_id}/logs`

**UI:** JobMonitoring → Execution logs panel
**Backend:** CloudWatch Logs (log group: `/ecs/{project}-{env}`, stream prefix per agent)

```json
// Query params: ?agent=collector&limit=100&next_token=...

// Response 200
{
  "logs": [
    {
      "timestamp": "2026-02-23T14:23:15Z",
      "agent": "collector",
      "level": "INFO",
      "message": "Starting analysis job 550e8400-e29b-41d4"
    },
    {
      "timestamp": "2026-02-23T14:23:16Z",
      "agent": "collector",
      "level": "INFO",
      "message": "Connecting to database endpoint..."
    }
  ],
  "next_token": "eyJ0..."
}
```

---

### `GET /api/v1/assessments/{job_id}/execution-history`

**UI:** JobMonitoring → Pipeline progress table (dynamic, replaces hardcoded steps)
**Backend:** Step Functions `GetExecutionHistory` — captures all state types in chronological order

Returns a flat list of all states from the Step Functions execution, including Task, Map, MapIteration, Pass, Parallel, Wait, Choice, and Succeed states. MapIteration entries include the `agent_type` parsed from the iteration input (e.g., `dynamodb`, `elasticache`).

```json
// Response 200
{
  "job_id": "fdaa8d7a-c58b-4576-b90b-f94df2ec0766",
  "status": "SUCCEEDED",
  "started_at": "2026-03-26T19:17:22Z",
  "stopped_at": "2026-03-26T19:24:45Z",
  "states": [
    {
      "name": "RunCollector",
      "type": "Task",
      "status": "completed",
      "duration_seconds": 174,
      "started_after_seconds": 0,
      "started_at": "2026-03-26T19:17:22Z",
      "completed_at": "2026-03-26T19:20:16Z"
    },
    {
      "name": "RunRefereeTriage",
      "type": "Task",
      "status": "completed",
      "duration_seconds": 56,
      "started_after_seconds": 175,
      "started_at": "2026-03-26T19:20:17Z",
      "completed_at": "2026-03-26T19:21:13Z"
    },
    {
      "name": "LoadTriageOutput",
      "type": "Task",
      "status": "completed",
      "duration_seconds": 0,
      "started_after_seconds": 231,
      "started_at": "2026-03-26T19:21:13Z",
      "completed_at": "2026-03-26T19:21:13Z"
    },
    {
      "name": "ExtractTriageFields",
      "type": "Pass",
      "status": "completed",
      "duration_seconds": 0,
      "started_after_seconds": 231,
      "started_at": "2026-03-26T19:21:13Z",
      "completed_at": "2026-03-26T19:21:13Z"
    },
    {
      "name": "RunEnginePipelines",
      "type": "Map",
      "status": "completed",
      "duration_seconds": 150,
      "started_after_seconds": 231,
      "started_at": "2026-03-26T19:21:13Z",
      "completed_at": "2026-03-26T19:23:43Z"
    },
    {
      "name": "dynamodb",
      "type": "MapIteration",
      "status": "completed",
      "duration_seconds": 150,
      "started_after_seconds": 231,
      "started_at": "2026-03-26T19:21:13Z",
      "completed_at": "2026-03-26T19:23:43Z"
    },
    {
      "name": "elasticache",
      "type": "MapIteration",
      "status": "completed",
      "duration_seconds": 109,
      "started_after_seconds": 231,
      "started_at": "2026-03-26T19:21:13Z",
      "completed_at": "2026-03-26T19:22:62Z"
    },
    {
      "name": "RunSchemaDesign",
      "type": "Task",
      "status": "completed",
      "duration_seconds": 63,
      "started_after_seconds": 381,
      "started_at": "2026-03-26T19:23:43Z",
      "completed_at": "2026-03-26T19:24:46Z"
    }
  ]
}
```

The UI groups MapIteration rows under their parent Map state using expandable rows. The `started_after_seconds` field is relative to the execution start time, matching the Step Functions console table view.

---

## 6. Assessments — Results

### `GET /api/v1/assessments/{job_id}/results`

**UI:** AnalysisResults page (executive summary, architecture, TCO, risks)
**Backend:** Read `referee-synthesis/report.json` from S3 + aggregate analysis artifacts

```json
// Response 200
{
  "job_id": "550e8400-e29b-41d4",
  "status": "COMPLETED",
  "executive_summary": {
    "architecture_type": "MULTI_DATABASE",
    "tables_analyzed": 1247,
    "confidence_score": 87,
    "confidence_level": "HIGH",
    "estimated_monthly_savings": 2200,
    "savings_percent": 44
  },
  "recommended_architecture": {
    "databases": [
      { "service": "DynamoDB", "table_count": 850, "confidence": 92, "pattern": "Key-value and single-table access" },
      { "service": "DocumentDB", "table_count": 297, "confidence": 88, "pattern": "Document-oriented and JSON workloads" },
      { "service": "ElastiCache", "table_count": 100, "confidence": 95, "pattern": "High-frequency caching patterns" }
    ]
  },
  "tco_analysis": {
    "current_monthly_cost": 5000,
    "projected_monthly_cost": 2800,
    "monthly_savings": 2200,
    "savings_percent": 44,
    "three_year_savings": 79200,
    "payback_period_months": 3.2,
    "roi_three_year_percent": 132,
    "cost_breakdown": {
      "current": { "rds_instance": 4200, "storage_backups": 500, "data_transfer": 300 },
      "projected": { "dynamodb": 1200, "documentdb": 950, "elasticache": 650 }
    }
  },
  "risk_assessment": {
    "overall_risk_level": "MEDIUM",
    "risks": [
      {
        "risk": "Data consistency",
        "severity": "MEDIUM",
        "likelihood": "MEDIUM",
        "impact": "Potential inconsistency across distributed databases",
        "mitigation": "Implement distributed transactions and eventual consistency patterns"
      }
    ]
  },
  "triage_summary": {
    "selected_agents": ["dynamodb", "documentdb", "elasticache"],
    "skipped_agents": ["neptune", "opensearch", "keyspaces", "aurora"],
    "triage_confidence": 0.87
  }
}
```

### `GET /api/v1/assessments/{job_id}/results/table-mappings`

**UI:** AnalysisResults → Table mappings table (paginated, filterable)
**Backend:** Read analysis artifacts from S3, aggregate per-table recommendations

```json
// Query params: ?limit=25&offset=0&recommended_db=DynamoDB&sort=confidence:desc

// Response 200
{
  "table_mappings": [
    {
      "source_table": "users",
      "recommended_db": "DynamoDB",
      "confidence": 95,
      "confidence_level": "HIGH",
      "access_pattern": "Key-value access",
      "alternatives": [
        { "service": "DocumentDB", "confidence": 82 },
        { "service": "Aurora", "confidence": 78 }
      ]
    }
  ],
  "total_count": 1247,
  "limit": 25,
  "offset": 0
}
```

---

## 7. Assessments — Raw Artifacts

These endpoints proxy S3 artifacts directly. The parent API reads from S3 and returns the agent's raw output. Useful for the execution details view and debugging.

### `GET /api/v1/assessments/{job_id}/collector`

**Backend:** Read `s3://<bucket>/<db-name>/<job-id>/collector/output.json`

### `GET /api/v1/assessments/{job_id}/triage`

**Backend:** Read `s3://<bucket>/<db-name>/<job-id>/referee-triage/triage.json`

> **Note:** The triage signals key is `signals` (not `signal_results`). Each signal has `query_ids` (queries that triggered it) and `targets` (which engines it points to).

### `GET /api/v1/assessments/{job_id}/reality-check`

**Backend:** Read `s3://<bucket>/<db-name>/<job-id>/reality-check/output.json`

See [§4. Assignment Approval Gate](#4-assignment-approval-gate-human-in-the-loop) for the full response schema.

### `GET /api/v1/assessments/{job_id}/analysis/{agent_type}`

**Backend:** Read `s3://<bucket>/<db-name>/<job-id>/analysis-{agent_type}/analysis.json`

### `GET /api/v1/assessments/{job_id}/schema-designs`

**Backend:** List and read `s3://<bucket>/<db-name>/<job-id>/schema-*/schema.json`

```json
// Response 200
{
  "schema_designs": [
    {
      "target_type": "dynamodb",
      "artifact_path": "ecommerce_prod/550e8400.../schema-dynamodb/schema.json",
      "content": { ... }
    }
  ]
}
```

---

## 8. Settings

### `GET /api/v1/settings`

**UI:** Settings page → load current values
**Backend:** Read from DynamoDB settings table or SSM Parameter Store

```json
// Response 200
{
  "aws_configuration": {
    "region": "us-east-1",
    "s3_bucket": "database-modernizer-results-us-east-1",
    "dynamodb_table": "database-modernizer-jobs",
    "iam_role": ""
  },
  "default_analysis_options": {
    "query_log_period_days": 7,
    "sample_size": 1000,
    "target_databases": ["dynamodb", "documentdb", "elasticache", "opensearch", "aurora"],
    "anonymize_pii": true,
    "include_sample_data": true
  },
  "ui_preferences": {
    "color_theme": "system",
    "auto_refresh_interval_seconds": 30,
    "browser_notifications": true,
    "email_notifications": false,
    "notification_events": {
      "completed": true,
      "failed": true,
      "warnings": false,
      "long_running": false
    },
    "compact_mode": false
  }
}
```

### `PUT /api/v1/settings`

**UI:** Settings page → "Save settings" button
**Backend:** Write to DynamoDB settings table or SSM Parameter Store

Same body as GET response. Returns 200 with updated settings.

### `POST /api/v1/settings/test-connection`

**UI:** Settings page → "Test AWS connection" button
**Backend:** Verify S3 bucket access, DynamoDB table access, IAM role assumption

```json
// Response 200
{
  "s3_bucket": { "status": "ok", "message": "Bucket accessible" },
  "dynamodb_table": { "status": "ok", "message": "Table accessible" },
  "iam_role": { "status": "skipped", "message": "No IAM role configured" }
}
```

---

## 9. Dashboard Aggregates

### `GET /api/v1/dashboard/stats`

**UI:** Dashboard → System status cards (total analyses, active jobs, success rate, avg time)
**Backend:** Aggregate from DynamoDB job metadata

```json
// Response 200
{
  "total_assessments": 24,
  "active_jobs": 2,
  "success_rate_percent": 95,
  "average_duration_hours": 4.2,
  "completed_today": 5,
  "last_analysis_at": "2026-02-23T12:15:00Z"
}
```

---

## Implementation Notes

### Data Sources per Endpoint

| Endpoint | Primary Data Source | Notes |
| -------- | ------------------- | ----- |
| `POST /assessments` | Step Functions StartExecution | Creates DynamoDB job record |
| `GET /assessments` | DynamoDB job metadata table | Indexed by status, created_at |
| `GET /assessments/{id}` | Step Functions DescribeExecution + S3 | Combines execution state with artifact checks |
| `GET /assessments/{id}/agents` | Step Functions history + S3 artifacts | Status from execution history, summaries from S3 |
| `GET /assessments/{id}/execution-history` | Step Functions GetExecutionHistory | All state types with timing, MapIteration inputs parsed |
| `GET /assessments/{id}/logs` | CloudWatch Logs | Filter by log stream prefix per agent |
| `GET /assessments/{id}/results` | S3 `referee-synthesis/report.json` | Aggregated from synthesis output |
| `GET /assessments/{id}/results/table-mappings` | S3 analysis artifacts | Aggregated from per-agent analysis outputs |
| `GET /assessments/{id}/collector` | S3 `collector/output.json` | Direct proxy |
| `GET /assessments/{id}/triage` | S3 `referee-triage/triage.json` | Direct proxy — key is `signals` (not `signal_results`) |
| `GET /assessments/{id}/reality-check` | S3 `reality-check/output.json` | Direct proxy — consolidations, before/after distribution |
| `GET /assessments/{id}/assignments` | S3 `assignment/v{latest}/assignment.json` | Latest versioned assignment |
| `PUT /assessments/{id}/assignments` | S3 `assignment/v{N+1}/assignment.json` | Writes new version after applying overrides |
| `POST /assessments/{id}/resume` | DynamoDB task token + SFN SendTaskSuccess | Phase must be `assignment_review` |
| `GET /settings` | DynamoDB or SSM Parameter Store | Per-deployment settings |
| `GET /dashboard/stats` | DynamoDB job metadata | Aggregation query |

### S3 Path Convention (ADR-016)

```
s3://<bucket>/<database-name>/<job-id>/collector/output.json
s3://<bucket>/<database-name>/<job-id>/referee-triage/triage.json
s3://<bucket>/<database-name>/<job-id>/analysis-dynamodb/analysis.json
s3://<bucket>/<database-name>/<job-id>/analysis-documentdb/analysis.json
s3://<bucket>/<database-name>/<job-id>/analysis-elasticache/analysis.json
s3://<bucket>/<database-name>/<job-id>/assignment/v{N}/assignment.json
s3://<bucket>/<database-name>/<job-id>/reality-check/output.json
s3://<bucket>/<database-name>/<job-id>/schema-dynamodb/v{N}/schema_output.json
s3://<bucket>/<database-name>/<job-id>/referee-synthesis/report.json
```

### Phase 0 Scope

All endpoints above are Phase 0 targets. WebSocket real-time updates (`/ws/assessments/{job_id}`) are deferred to Phase 1 — the UI will use polling via `GET /assessments/{job_id}` with the auto-refresh interval from settings.
