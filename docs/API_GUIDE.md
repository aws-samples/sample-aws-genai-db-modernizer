# Database Modernizer Assessment — API Guide

Base URL: `/api/v1/`

All endpoints return JSON. Errors return `{ "detail": "..." }` with appropriate HTTP status codes.

---

## Table of Contents

1. [Job Lifecycle](#1-job-lifecycle)
2. [Pipeline Artifacts](#2-pipeline-artifacts)
3. [Assignment Gate (Human-in-the-Loop)](#3-assignment-gate-human-in-the-loop)
4. [Dashboard](#4-dashboard)
5. [Data Relationships](#5-data-relationships)
6. [Engine Reference](#6-engine-reference)

---

## 1. Job Lifecycle

### List Assessments

```
GET /assessments?status={status}&limit=25&offset=0
```

**Query params:** `status` (optional): `RUNNING`, `SUCCEEDED`, `FAILED`, `ABORTED`

**Response:**

```json
{
  "assessments": [
    {
      "job_id": "f5698748-...",
      "status": "SUCCEEDED",
      "created_at": "2026-04-24T13:15:16.185000+00:00",
      "completed_at": "2026-04-24T14:02:33.000000+00:00",
      "duration_seconds": 2837
    }
  ],
  "total_count": 12,
  "limit": 25,
  "offset": 0
}
```

---

### Get Job Detail

```
GET /assessments/{jobId}
```

Returns job status, database info, and pipeline stage progress.

**Response:**

```json
{
  "job_id": "f5698748-9df0-4822-9db0-3c024f827bf5",
  "status": "SUCCEEDED",
  "source_database_type": "mysql",
  "database_name": "wordpress",
  "created_at": "2026-04-24T13:15:16.185000+00:00",
  "execution_arn": "arn:aws:states:...",
  "progress": {
    "percent_complete": 100,
    "current_stage": null,
    "current_activity": null,
    "stages": [
      { "name": "RunCollector", "status": "completed", "duration_seconds": 60 },
      { "name": "RunRefereeTriage", "status": "completed", "duration_seconds": 59 },
      { "name": "RunAnalysis", "status": "completed", "duration_seconds": 180 },
      { "name": "RunAssignmentResolution", "status": "completed", "duration_seconds": 45 },
      { "name": "RunRealityCheck", "status": "completed", "duration_seconds": 30 },
      { "name": "HumanApprovalGate", "status": "completed", "duration_seconds": null },
      { "name": "RunSchemaDesign", "status": "completed", "duration_seconds": 120 },
      { "name": "RunRefereeSynthesis", "status": "completed", "duration_seconds": 90 }
    ]
  },
  "error": null
}
```

**Key field:** `database_name` — you need this for most other API calls.

**Stage status values:** `completed`, `in-progress`, `pending`, `failed`

---

### Get Phase Progression

```
GET /assessments/{jobId}/phases
```

Returns the phased execution state. Use this to know if the pipeline is waiting for human approval.

**Response:**

```json
{
  "job_id": "f5698748-...",
  "current_phase": "assignment_review",
  "assignment_version": 1,
  "total_iterations": 1,
  "phases": {
    "collect_triage": { "phase": "collect_triage", "status": "completed", ... },
    "analysis": { "phase": "analysis", "status": "completed", ... },
    "assignment": { "phase": "assignment", "status": "completed", ... },
    "reality_check": { "phase": "reality_check", "status": "completed", ... },
    "assignment_review": { "phase": "assignment_review", "status": "awaiting_review", ... },
    "schema_design": { "phase": "schema_design", "status": "not_started", ... },
    "synthesis": { "phase": "synthesis", "status": "not_started", ... }
  }
}
```

**Phase order:** `collect_triage → analysis → assignment → reality_check → assignment_review → schema_design → synthesis`

**Phase status values:** `not_started`, `in_progress`, `completed`, `failed`, `awaiting_review`, `awaiting_input`, `skipped`

**When to show the Assignment Gate:** `current_phase === "assignment_review"` and phase status is `awaiting_review`.

---

### Create Assessment

```
POST /assessments
```

**Request body:**

```json
{
  "database_name": "my_database",
  "source_database_type": "mysql",
  "collection_mode": "offline",
  "offline_s3_key": "my_database/uploads/collector-output.json",
  "full_analysis": true
}
```

**Response (202):**

```json
{
  "job_id": "new-uuid-here",
  "status": "PENDING",
  "created_at": "...",
  "estimated_completion_time": "...",
  "execution_arn": "arn:aws:states:..."
}
```

---

### Cancel Assessment

```
DELETE /assessments/{jobId}
```

---

## 2. Pipeline Artifacts

These endpoints return raw pipeline outputs. All require the job to have completed the relevant phase.

### Collector Output

```
GET /assessments/{jobId}/collector
```

Database schema + query patterns captured from the source database.

**Key fields:**

```json
{
  "metadata": {
    "source_database": {
      "engine": "mysql",
      "version": "8.0.45",
      "database_name": "wordpress",
      "database_size_gb": 0.135
    }
  },
  "database_schema": {
    "tables": [
      {
        "table_id": "wordpress.wp_posts",
        "table_name": "wp_posts",
        "schema_name": "wordpress",
        "row_count": 5000,
        "size_mb": 12.5,
        "columns": [...],
        "indexes": [...],
        "primary_key": {...},
        "foreign_keys": [...]
      }
    ]
  },
  "queries": {
    "query_patterns": [
      {
        "query_id": "0452bed28b31...",
        "query_type": "UPDATE",
        "query_text": "UPDATE `wp_woocommerce_api_keys` SET `last_access` = ? WHERE `key_id` = ?",
        "calls_per_second": 8.27,
        "execution_time_ms_avg": 14.07,
        "tables_accessed": ["wordpress.wp_woocommerce_api_keys"],
        "has_joins": false,
        "has_aggregations": false,
        "has_text_search": null,
        "join_count": 0,
        "rows_examined_avg": 1.0
      }
    ]
  }
}
```

**`query_id`** is a SHA-256 hash — stable across runs for the same query pattern. Use it to join data across endpoints.

---

### Triage Output

```
GET /assessments/{jobId}/triage
```

Which target engines were selected and why (signal-based).

**Key fields:**

```json
{
  "selected_agents": [
    {
      "agent_type": "dynamodb",
      "reasons": [
        "key_value_lookups: 37 key-value lookup queries",
        "write_heavy: 1 high-frequency write queries"
      ]
    }
  ],
  "skipped_agents": [
    { "agent_type": "elasticache", "reasons": ["..."] }
  ],
  "signals": [
    {
      "signal": "key_value_lookups",
      "targets": ["dynamodb", "elasticache", "documentdb"],
      "query_ids": ["66c96f95...", "3dbc7d9a..."],
      "table_ids": []
    },
    {
      "signal": "text_search",
      "targets": ["opensearch"],
      "query_ids": ["a1b2c3d4..."],
      "table_ids": []
    }
  ],
  "confidence_score": 99
}
```

**Important:** The key is `signals` (NOT `signal_results`).

**Signal types:** `key_value_lookups`, `status_filters`, `write_heavy`, `low_frequency_writes`, `low_frequency_reads`, `complex_joins`, `aggregations`, `session_store`, `metadata_config`, `text_search`, `leaderboard_pattern`, `subqueries`, `high_frequency_reads`

Each signal has `query_ids` (queries that triggered it) and `targets` (which engines it points to). Some signals are table-based (`table_ids` populated, `query_ids` empty).

---

### Reality Check Output

```
GET /assessments/{jobId}/reality-check
```

CTO-level engine consolidation — what was eliminated and why.

**Response:**

```json
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
  "recommendations": [
    "Use DynamoDB as the primary engine for all transactional workloads...",
    "Set up DynamoDB-to-OpenSearch zero-ETL integration..."
  ],
  "unique_value_assessment": {}
}
```

**`before_distribution`** = what the analysis agents recommended (3 engines).
**`after_distribution`** = what the reality check optimized it to (2 engines).
**`consolidations`** = which engines were eliminated, where their queries went, and estimated savings.

---

### Analysis Output (per engine)

```
GET /assessments/{jobId}/analysis/{agentType}
```

Where `agentType` is: `dynamodb`, `documentdb`, `opensearch`, `elasticache`

Returns per-table recommendations, workload patterns, cost estimates for that engine.

---

### Synthesis Report

```
GET /assessments/{jobId}/results
```

Final report with executive summary, architecture recommendation, TCO, risks.

---

### Table Mappings (paginated)

```
GET /assessments/{jobId}/results/table-mappings?limit=25&offset=0&recommended_db=dynamodb&sort=confidence:desc
```

---

### Schema Designs

```
GET /assessments/{jobId}/schema-designs
```

Returns all generated schema designs (DynamoDB tables, OpenSearch indexes, etc.).

---

## 3. Assignment Gate (Human-in-the-Loop)

These are the endpoints that power the Assignment Review page — the human approval gate.

### Read Assignments

```
GET /assessments/{jobId}/assignments?database_name={dbName}
```

**`database_name` is required** — get it from `GET /assessments/{jobId}` → `database_name`.

**Response:**

```json
{
  "assignment": {
    "query_assignments": [
      {
        "query_id": "0452bed28b31...",
        "assigned_engine": "dynamodb",
        "confidence": 85,
        "source_tables": ["wordpress.wp_woocommerce_api_keys"],
        "assignment_reason": "highest confidence for dynamodb",
        "in_scope": true,
        "customer_override": false,
        "warnings": []
      }
    ],
    "table_assignments": [
      {
        "table_id": "wordpress.wp_posts",
        "primary_engine": "dynamodb",
        "engines": ["dynamodb"],
        "query_count": 12,
        "multi_engine_reason": null
      }
    ]
  },
  "validation": null,
  "skipped_engines": []
}
```

**`query_id`** joins to collector's `query_patterns[].query_id` and triage's `signals[].query_ids[]`.

---

### Override Assignments

```
PUT /assessments/{jobId}/assignments?database_name={dbName}
```

**Request body:**

```json
{
  "overrides": [
    { "query_id": "0452bed28b31...", "assigned_engine": "opensearch" },
    { "query_id": "126679dc6aae...", "assigned_engine": "dynamodb" }
  ]
}
```

Optional scope narrowing (exclude entire tables):

```json
{
  "overrides": [],
  "scope": {
    "exclude_tables": ["wordpress.wp_options"],
    "reason": "Not migrating config tables"
  }
}
```

**Response:** Same shape as GET, but with `validation` populated:

```json
{
  "assignment": { ... },
  "validation": {
    "valid": true,
    "errors": [],
    "warnings": ["WARNING [LOW]: Query X has low confidence (45%) for opensearch"]
  },
  "skipped_engines": ["documentdb"]
}
```

**Error (422):** Returned if overrides cause hard validation errors (e.g., assigning to an engine that wasn't analyzed).

---

### Resume Pipeline

```
POST /assessments/{jobId}/resume
```

**Request body:**

```json
{
  "phase": "assignment_review"
}
```

**Response (200):**

```json
{
  "job_id": "f5698748-...",
  "phase": "assignment_review",
  "status": "resumed"
}
```

**Error (409):** Phase prerequisites not met, or task token not found.

Call this after the user approves the assignments. The pipeline will continue to schema design.

---

## 4. Dashboard

### Dashboard Stats

```
GET /dashboard/stats
```

**Response:**

```json
{
  "total_assessments": 12,
  "active_jobs": 1,
  "success_rate_percent": 83.3,
  "average_duration_hours": 1.2,
  "completed_today": 0,
  "last_analysis_at": "2026-04-24T13:15:16.185000+00:00"
}
```

---

## 5. Data Relationships

```
                    ┌──────────────────────────────────┐
                    │  GET /assessments/{jobId}        │
                    │  → database_name                 │
                    └───────────┬──────────────────────┘
                                │
          ┌─────────────────────┼──────────────────────┐
          │                     │                      │
          ▼                     ▼                      ▼
   GET /collector        GET /triage          GET /reality-check
   → query_patterns[]    → signals[]          → before/after distribution
   → database_schema     → selected_agents    → consolidations
          │                     │                      │
          │                     │                      │
          └─────────┬───────────┘                      │
                    │                                  │
                    ▼                                  │
          GET /assignments?database_name=X             │
          → query_assignments[]                        │
          │                                            │
          │  JOIN ON: query_id ────────────────────────┘
          │  (query_id links all four endpoints)
          │
          ├──► PUT /assignments  (override)
          │
          └──► POST /resume      (approve & continue)
                    │
                    ▼
          GET /schema-designs
          GET /results
```

### How to join data across endpoints

1. **`query_id`** is the universal join key — same ID in collector, triage signals, assignments
2. **Get database_name first:** `GET /assessments/{jobId}` → use `response.database_name` for assignments endpoint
3. **Signals → queries:** `triage.signals[].query_ids[]` maps to `collector.queries.query_patterns[].query_id`
4. **Signals → engines:** `triage.signals[].targets[]` tells you which engines each signal points to (use first target as primary for color-coding)
5. **Reality check → assignments:** Queries moved by reality check have `assignment_reason` containing "reality check"

---

## 6. Engine Reference

| Engine | Color | Badge | Key |
|--------|-------|-------|-----|
| DynamoDB | `#3184e8` | blue | `dynamodb` |
| DocumentDB | `#1d8102` | green | `documentdb` |
| OpenSearch | `#879596` | grey | `opensearch` |
| ElastiCache | `#d13212` | red | `elasticache` |
| Neptune | `#7d2105` | grey | `neptune` |
| Keyspaces | `#8b6ccb` | grey | `keyspaces` |
| Aurora | `#ec7211` | grey | `aurora` |

Use these engine keys consistently across UI components for badges, charts, and filters.
