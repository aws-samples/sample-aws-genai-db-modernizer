# Database Modernizer Assessment - High-Level Design (HLD)

## Document Information

**Version:** 12.0 (Hosted Deployment Removed, #175)
**Date:** October 5, 2026
**Status:** Approved
**Owner:** Database Modernizer Assessment Engineering Team

---

## Executive Summary

Customers modernizing off monolithic relational databases ask us the same question: **which purpose-built database should I choose?** Should this workload go to DynamoDB? Does it need a document store like DocumentDB? Would a cache layer in ElastiCache solve the problem? Should full-text search move to OpenSearch? Or does the query pattern actually belong in Aurora?

Getting the answer wrong means failed modernizations, re-architecture mid-project, and wasted months. Getting it right requires deep analysis of every query pattern, understanding access patterns at scale, and mapping them to the right engine — a process that traditionally takes weeks of specialist time per database.

**Database Modernizer Assessment answers that question automatically.** It analyzes every query pattern in your relational database, scores each one against AWS purpose-built engines, validates the overall architecture for operational complexity, and produces ready-to-implement schema designs with load-tested performance data. You can run it from your laptop, from the command line or with Claude Code.

The core pipeline is **fully deterministic** — pattern detection, scoring, assignment, and consolidation all run without any LLM dependency. GenAI enhances the pipeline at key decision points (analysis advisors, consolidation validation, executive summaries) but is never required. You get reproducible, auditable results every time, with AI refinement layered on top when available.

**Supported sources:** PostgreSQL, MySQL, MariaDB (Redis planned)

**Target engines:** DynamoDB, DocumentDB, ElastiCache/Redis, OpenSearch, Aurora PostgreSQL, Aurora MySQL

### Key Design Decisions

- **Local-first deployment**: Runs entirely on the user's machine — via Claude Code (`/modernize`), the deterministic CLI, or a local FastAPI + React UI — against RDS instances the user already has access to. Offline and DDL collection need no database connection at all; live collection runs through an opt-in, not-yet-fully-wired SSM automation path (§7.1), not a direct connection. The hosted, AWS-account-deployed version of this tool was retired; see [#175](https://github.com/aws-samples/sample-aws-genai-db-modernizer/issues/175)
- **Deterministic core**: Full pipeline runs without LLM calls (`--llm-mode none`); GenAI is an enhancement layer, not a dependency
- **Strands SDK agent framework**: Open-source agentic framework for agent orchestration, tool management, and LLM integration
- **Multi-agent architecture**: Independent, composable agents (Collector, Referee-Triage, Analysis, Assignment Resolution, Reality Check, Referee-Synthesis, Schema Design, Load Testing)
- **Local orchestration**: A single `LocalOrchestrator` runs phases as direct function calls against the local artifact store — no job-scheduling service, message bus, or hosted workflow engine
- **Two human-in-the-loop gates**: Pipeline pauses after triage (assignment review) and after reality check (schema design approval); the user resumes each gate via the UI, API, or the relevant Claude Code command
- **Reality check consolidation**: CTO-level engine consolidation eliminates low-value engines, reducing operational complexity
- **Load testing validation**: k6 empirical performance testing against real target infrastructure with per-pattern latency and cost measurement ([ADR-020](decisions/ADR-020-load-testing-stage.md))
- **Query journey materialization**: Progressive per-query files tracking each query from source through assignment, schema design, and load testing ([ADR-019](decisions/ADR-019-query-journey-materialization.md)), now served from the context graph read-model ([ADR-023](decisions/ADR-023-context-graph-layer.md))
- **Hybrid analysis selection**: AI-driven triage selects relevant analysis agents; the local orchestrator executes them deterministically
- **AWS Transform integration**: `src/atx_orchestrator/` lets AWS Transform run the same agents over S3-backed storage, independent of the local-only API/UI path
- **Bedrock integration**: AWS Bedrock for AI-powered analysis (optional, enhances results)
- **Open-source distribution**: GitHub repository with MIT-0 license

### Design Philosophy

- **Local by design**: Runs on the user's machine, so there is no hosted service to secure, patch, or pay for
- **No vendor lock-in**: Customers control where the code runs and where their data goes
- **Transparent**: Open-source code for full transparency
- **AWS-optimized**: Leverage AWS-native services where they add value (RDS, Performance Insights, Bedrock) without requiring a hosted deployment

### Performance Targets

- Complete analysis in <6 hours for 1,000 table RDS database
- <3 second API response time for any query type
- Leverage Performance Insights for query-level data
- CloudWatch metrics for instance-level performance (when analyzing against a live, customer-owned RDS instance)
- Load test latency: 2-6ms p50 for DynamoDB access patterns

---

## Table of Contents

1. System Architecture Overview
2. Deployment Architecture
3. Agent Framework Design
4. Data Flow and Storage
5. API Architecture
6. Technology Stack
7. Security Architecture
8. Operational Considerations
9. CI/CD Pipeline

---

## 1. System Architecture Overview

### 1.1 High-Level Architecture

**For detailed architecture diagrams, see:**

- [System Context Diagram](diagrams/01-system-context.md)
- [Agent Framework Architecture](diagrams/04-agent-framework.md)

### Architecture Components

**Application Layer:**

- Web UI (React SPA, local build): Assessment report viewer, interactive recommendation review, query journey explorer
- API Server (FastAPI, run locally with `uvicorn`): REST endpoints for the UI, loopback-only by default (see [`src/api/host_guard.py`](../../src/api/host_guard.py))
- Orchestrator (`LocalOrchestrator`): runs phases as direct function calls against the local artifact store — no job-scheduling service or message bus

**Agent Layer (Strands SDK):**

- Collector Category: MySQL, PostgreSQL, MariaDB, SQL Server, Oracle, DB2, Redis collectors
- Analysis Category: DynamoDB, DocumentDB, OpenSearch, ElastiCache, Neptune, Keyspaces, Aurora analysis agents
- Referee-Triage Agent: Reads collector output, selects relevant analysis agents based on workload signals
- Assignment Resolution Agent: Maps each query to best-fit engine using signal-driven overrides and anti-pattern penalties
- Reality Check Agent: CTO-level engine consolidation — eliminates engines that add operational complexity without unique value
- Referee-Synthesis Agent: Produces weighted ranking with confidence scores, may request deeper analysis
- Schema Design Agents: DynamoDB, DocumentDB, OpenSearch, ElastiCache, Neptune, Keyspaces schema generators (with group splitting for large workloads)
- Load Testing Category: Engine-specific load test coordinators — provision target infrastructure, seed data, generate k6 scripts, execute, measure, teardown

**Cross-Cutting Concerns:**

- Query Journey Materialization: per-query detail served from the context graph read-model, with a fallback to legacy progressive per-query files for jobs that predate it ([ADR-023](decisions/ADR-023-context-graph-layer.md))

**Data Storage Layer:**

- Local filesystem (`LocalArtifactStore`, default `./artifacts`): collector outputs, reports, job artifacts, query journeys (path: `<database-name>/<job-id>/<agent-name>/artifact.json`)
- S3 (`S3ArtifactStore`, used only by the AWS Transform integration — `S3_BUCKET` env var): same path convention, for ATX-orchestrated jobs
- Local job metadata file (`.meta/<job-id>.json` under the artifact store): phase progression, status tracking

**Source Databases (Customer-Owned, Read-Only Access):**

- RDS Instances: MySQL, PostgreSQL, MariaDB, SQL Server, Oracle, DB2 (reachable from wherever the tool runs — a laptop, a bastion, or a customer VPC)
- Redis Instances: ElastiCache for Redis or self-managed Redis (customer infrastructure)
- CloudWatch metrics and Performance Insights read via the AWS APIs, when analyzing a live, customer-owned RDS instance
- Database Modernizer Assessment does NOT deploy its own RDS/Redis instances

**AWS Services (used directly by the user's own credentials, not a hosted role):**

- CloudWatch API, Performance Insights API, RDS API, Secrets Manager — only for live (not offline/DDL) collection against a real database
- Bedrock API for AI-powered analysis (optional)

### System Context

**Primary Users:**

- Database Architects: run the tool locally or via Claude Code, analyze RDS workloads, review recommendations
- IT Leaders: Review analysis reports, approve migrations
- Developers: Implement schema designs, execute migrations

**External Systems:**

- Amazon RDS instances and Redis (read-only access, live mode only)
- AWS APIs (CloudWatch, Performance Insights, RDS API, Secrets Manager) — live mode only
- AWS Bedrock for AI-powered analysis (optional)

**Key Design Principles:**

1. Local-First: Runs on the user's machine, no hosted service required
2. AWS-Optimized: Leverages AWS-native services where they add value, without requiring a hosted deployment
3. Simple to Run: `/modernize` in Claude Code, or `uv run python scripts/run_assessment.py`
4. Transparent: Open-source code for full auditability

---

## 2. Deployment Architecture

There is no hosted deployment. A prior version of this project deployed to a
customer's own AWS account via CloudFormation (ECS Fargate behind an ALB, with
Cognito auth, Step Functions orchestration, S3 and DynamoDB storage); that path
was retired in favor of running the tool directly where the user already is
(see [#175](https://github.com/aws-samples/sample-aws-genai-db-modernizer/issues/175)).
The CloudFormation templates, Docker images and deploy scripts for the hosted
service are gone. Two CloudFormation templates remain for unrelated reasons:
[`example-ci-runner-iam.yaml`](../../infrastructure/cloudformation/example-ci-runner-iam.yaml),
a sample for anyone standing up their own CI runner role, and
[`automation.yaml`](../../infrastructure/cloudformation/automation.yaml),
which provisions a per-VPC SSM automation instance for the opt-in live
collection path described in §2.2 and §7.1 (kept per a maintainer decision —
see [#342](https://github.com/aws-samples/sample-aws-genai-db-modernizer/issues/342)
for what's still missing to wire it end-to-end). `pipeline/iam-roles.yaml`
also remains — it is unrelated to the hosted deployment and provisions IAM
roles for the AWS Transform integration (§2.4).

### 2.1 Primary Mode: Claude Code

- `/modernize` drives the full pipeline from a Claude Code session, with
  per-phase approval gates and a choice of chat, UI, or both experience modes
- No infrastructure to deploy — Claude Code runs the deterministic scripts and
  reasons over their output directly on the user's machine

### 2.2 Secondary Mode: Local API + UI

**Architecture:**

```
User's machine
├── FastAPI (uv run uvicorn src.api.main:app, port 8000)
│   - Loopback-only by default (src/api/host_guard.py)
│   - Always backed by the local filesystem services
│     (LocalExecutionService, LocalS3Service, LocalOrchestrator)
├── React SPA (npm start / local build, port 3000)
└── ./artifacts/ (LocalArtifactStore)
```

**Purpose:** running the same pipeline through the web UI instead of a chat
session — local development, demos, and users who prefer a browser.

**Limitations:**

- Single machine, single job at a time in practice
- Live collection (`collection_mode=live` with `cluster_id`) is gated behind
  `MODERNIZER_ENABLE_AUTOMATION=1` and not fully wired yet — see §7.1 and
  [#342](https://github.com/aws-samples/sample-aws-genai-db-modernizer/issues/342).
  Offline and DDL collection modes need no live database connection of any
  kind and work today.

### 2.3 Deterministic CLI and Amazon Bedrock

`uv run python scripts/run_assessment.py` runs the same pipeline headlessly,
with `--llm-mode none` (fully deterministic, no model calls) or
`--llm-mode bedrock` (Strands agents call Amazon Bedrock directly with the
user's own AWS credentials — no hosted inference proxy).

### 2.4 AWS Transform Integration

`src/atx_orchestrator/` lets AWS Transform run the same agent code over
S3-backed storage (`S3ArtifactStore`), orchestrated over A2A instead of the
local API. This is the one path that still talks to S3 and is unaffected by
the hosted-deployment removal.

---

## 3. Agent Framework Design

**For detailed agent architecture diagrams, see:**

- [Agent Framework Architecture](diagrams/04-agent-framework.md)
- [Workflow Sequence Diagram](diagrams/05-workflow-sequence.md)
- [Data Flow Diagram](diagrams/06-data-flow.md)

### 3.1 Multi-Agent Architecture

**Agent Categories:**

**1. Collector Category (Source Database Collection)**

- Individual agents: MySQL, PostgreSQL, MariaDB, SQL Server, Oracle, DB2, Redis
- Can spawn mini-collector agents for large databases (1000+ tables)
- Mini-collectors run in parallel (100 tables each) for 8x speedup
- Supports two input modes: live (SQL run via SSM Run Command on a per-VPC automation instance — opt-in, gated, see §7.1) and offline (pre-collected JSON)

**2. Analysis Category (Modernization Analysis)**

- Individual agents: DynamoDB, DocumentDB, OpenSearch, ElastiCache, Neptune, Keyspaces, Aurora (7 total)
- Only agents selected by Referee-Triage run (not always all 7)
- Run concurrently as local function calls (`LocalOrchestrator`)
- Can spawn sub-agents for specialized analysis

**3. Referee-Triage Agent (Workload Analysis & Agent Selection)**

- Reads collector output, detects workload signals (key-value lookups, text search, write-heavy, aggregations, etc.)
- Selects relevant analysis agents based on signals (e.g., skips Neptune for key-value workloads)
- Each signal maps to target engines and carries the `query_ids` / `table_ids` that triggered it
- Returns structured list with reasons for selection/skipping
- Logged to the local artifact store for auditability

**4. Assignment Resolution Agent (Query-to-Engine Mapping)**

- Reads all analysis outputs and maps each query to its best-fit engine
- Uses signal-driven overrides: triage signals (e.g., `text_search`) can override per-engine confidence scores
- Applies anti-pattern penalties: penalizes engines when queries hit known anti-patterns (e.g., full table scans on DynamoDB)
- Resolves multi-engine tables: when a table's queries split across engines, applies a majority-engine heuristic
- Produces versioned assignment artifacts (`assignment/v{N}/assignment.json`)

**5. Reality Check Agent (CTO-Level Engine Consolidation)**

- Evaluates the assignment output with a practical, cost-conscious lens
- Eliminates engines that add operational complexity without providing unique capabilities
- For each eliminated engine, reassigns its queries to the best surviving engine
- Outputs: `before_distribution`, `after_distribution`, `consolidations[]`, `architectural_patterns[]`, `recommendations[]`
- Example: if DocumentDB handles 15 queries but DynamoDB can serve them all, DocumentDB is eliminated — saving ~$500/mo in operational overhead

**6. Referee-Synthesis Agent (Validation & Ranking)**

- Receives selected analysis outputs and the post-reality-check assignment
- Produces weighted ranking with confidence scores
- May request deeper analysis (capped at 2 iterations)
- Generates final modernization report

**7. Schema Design Category (Target Schema Generation)**

- Individual agents: DynamoDB, DocumentDB, OpenSearch, ElastiCache, Neptune, Keyspaces
- Generate target-specific schemas
- **Group splitting**: Large workloads (20+ tables per engine) are split into groups using analysis signals for intelligent clustering (co-accessed tables stay together)
- Groups are processed in parallel, then merged into a unified schema per engine
- Pipeline runs three stages locally: split (grouping), design (per-group generation), merge (combine groups)
- Runs per-engine, chained after human approval
- Includes PE review loop for design validation; produces `design_trace.json` artifact

**8. Load Testing Category (Empirical Performance Validation)**

- Engine-agnostic coordinator orchestrates: provision → seed → generate → dry-run → execute → parse → teardown
- Uses **k6** as load generation engine, run locally (`scripts/run_load_test.py`) or inside the `agent-load-test` container image (bundled k6 binary). No GitHub Actions workflow builds or runs this image today — it exists for parity with the former hosted path and for manual/future use
- Abstract base classes (`BaseProvisioner`, `BaseSeeder`, `BaseScriptGenerator`, `BaseRunner`) enable engine extensibility
- **DynamoDB engine** (implemented): k6 AWS jslib with SigV4 signing, `ReturnConsumedCapacity` per request, Jinja2 templates for 7 operation types (GetItem, Query, PutItem, UpdateItem, DeleteItem, BatchGetItem, BatchWriteItem)
- **Future engines**: OpenSearch (k6 HTTP module), ElastiCache (xk6-redis), DocumentDB (xk6-mongo)
- Generated k6 scripts serve dual purpose: test execution AND customer deliverable (copy-paste ready code)
- Non-blocking: on failure, catches and proceeds to synthesis (results still valid without load test)
- Per [ADR-020](decisions/ADR-020-load-testing-stage.md)

**9. Query Journey Read-Model (Cross-Cutting)**

- The per-query modernization story (source → assignment → design → load test) is
  materialized in the LadybugDB context graph, built at two single-writer pipeline
  boundaries (end of assessment-core, end of synthesis) and published as a
  downloadable `.lbug` artifact
- Readers (`analysis_report`, the query-journeys API) serve per-query detail from
  the graph, with a fallback to legacy per-query journey artifacts only for jobs
  that predate the graph
- Supersedes the progressive per-query journey files in [ADR-019]
  (decisions/ADR-019-query-journey-materialization.md), which caused artifact-API
  throttling on the ATX backend when writing ~1,654 files serially
- Per [ADR-023](decisions/ADR-023-context-graph-layer.md)

### 3.2 Strands SDK Architecture

**Strands SDK** is an open-source agentic framework that provides:

- Agent orchestration and lifecycle management
- Tool management and composition
- LLM integration and coordination
- Built-in hooks for progress tracking

**Why Strands SDK:**

- Simplified agent creation without inheritance hierarchies
- Reusable tools across different agents
- Clear separation between behavior (prompts) and capabilities (tools)
- Independently testable components

**Key Components:**

1. **Strands Agent**: Framework-provided agent class
2. **Custom Tools**: Database-specific operations (connect, collect schema, query patterns)
3. **System Prompts**: Define agent behavior and execution logic
4. **Contracts**: Pydantic models enforce input/output structure

### 3.3 Orchestration Pattern

**Local orchestration.** A prior version of this project used a three-layer
cloud orchestration (Step Functions for workflow, EventBridge for
notifications, ECS tasks per agent — see
[ADR-016](decisions/ADR-016-compute-and-orchestration-strategy.md), now
superseded). The current `LocalOrchestrator` (`src/orchestrator/`) replaces
all three layers with direct, in-process function calls against the local
artifact store:

```
┌──────────────────────────────────────────────────────────────┐
│  LocalOrchestrator                                            │
│  Collector → Triage → [Human Gate 1] → Analysis (concurrent)  │
│  → Assignment Resolution → Reality Check                      │
│  → [Human Gate 2] → Schema Design (split/design/merge)        │
│  → Load Test → Synthesis → [deeper analysis loop?]            │
└──────────────────────────────────────────────────────────────┘
```

- Each phase runs as a direct Python function call — no workflow service,
  message bus, or container boundary between phases
- Analysis and schema design run per-engine concurrently, via a
  `ThreadPoolExecutor` in `LocalOrchestrator`; load testing runs per-engine
  sequentially (`_run_load_test` is a plain loop, not parallelized)
- **Two human approval gates**, unchanged in intent from the original design:
  - **Gate 1 (after Triage):** the user reviews triage signals and proposed
    engine selections before analysis begins
  - **Gate 2 (after Reality Check):** the user reviews final query-to-engine
    assignments before schema design proceeds
  - Resumption happens through the UI/API (`POST /resume`), the deterministic
    CLI, or the matching Claude Code command — there is no task-token
    callback to a workflow service
- Progress is tracked in a local phase-progression file next to the job's
  artifacts, not in DynamoDB or EventBridge; the API's `/execution-history`
  and `/agents` endpoints derive status from the artifact directory layout
  (`LocalExecutionService`)

**Intra-Agent Orchestration (Strands SDK):**

- Manages tool execution within a single agent
- LLM-driven decision making
- Agents spawn sub-processes internally for parallelism (e.g., mini-collectors)
- Error handling and retries within the agent

### 3.4 Restart Strategy

**Restart-from-scratch per step:**

No intra-step checkpointing. If a step fails, it restarts from scratch. This keeps agent code simple — no partial state recovery logic.

**Agent-Defined Restart Points:**

Each agent declares its mini-steps (e.g., connect, collect_schema, collect_metrics, collect_samples). Restarting a previous mini-step triggers a cascade: all subsequent mini-steps and downstream agents are invalidated and must re-run.

**Why restart-from-scratch:**

- Simpler agent code — no partial state recovery
- Avoids stale data propagation
- Acceptable cost given <6 hour total job target
- `LocalOrchestrator` does not retry automatically — a failed phase stays
  failed until the user re-runs it (CLI, API, or Claude Code command); see
  §8

### 3.5 Progress Reporting

**Architecture:**

- The UI/API polls `GET /api/v1/assessments/{job_id}` and
  `/execution-history`, which derive progress from which artifact
  directories and files exist on disk (`LocalExecutionService`) — there is
  no event bus or push channel
- Claude Code sessions report progress directly as part of the conversation

**Progress Granularity:**

- Stage-level updates (e.g., collector, referee-triage, per-engine analysis) derived from artifact presence
- Users can override triage and request full analysis via API parameter

---

## 4. Data Flow and Storage

**For detailed data flow diagram, see:**

- [Data Flow Diagram](diagrams/06-data-flow.md)
- [Storage Architecture](diagrams/07-storage-architecture.md)

### 4.1 Data Collection Flow

```
RDS Instance → Collector Agent → LocalArtifactStore (raw data)
    ↓
CloudWatch API → Collector Agent → LocalArtifactStore (metrics)
    ↓
Performance Insights → Collector Agent → LocalArtifactStore (query patterns)
    ↓
Collector Output (JSON) → LocalArtifactStore + Query Journey files (source)
    ↓
LocalOrchestrator → Referee-Triage → detects workload signals, selects engines
    ↓
Human Gate 1 → User reviews engine selection
    ↓
LocalOrchestrator → Per-engine analysis agents (concurrent) → LocalArtifactStore
    ↓
LocalOrchestrator → Assignment Resolution → query-to-engine mapping → LocalArtifactStore
    ↓                                        + Query Journey files (assignment)
LocalOrchestrator → Reality Check → engine consolidation → LocalArtifactStore
    ↓
Human Gate 2 → User reviews/overrides assignments
    ↓
LocalOrchestrator → Per-engine: group split → schema design
    ↓                (+ PE review) → group merge → LocalArtifactStore
    ↓                + Query Journey files (design)
LocalOrchestrator → Per-engine: provision → seed → k6 execute
    ↓                → parse results → teardown → LocalArtifactStore
    ↓                + Query Journey files (load_test)
LocalOrchestrator → Referee-Synthesis → weighted ranking → LocalArtifactStore
    ↓
Deeper analysis loop (max 2 iterations) if needed
    ↓
Modernization Report → LocalArtifactStore → User
```

(The AWS Transform integration runs the same flow against `S3ArtifactStore`
instead of `LocalArtifactStore`, orchestrated over A2A rather than
`LocalOrchestrator` — see [§2.4](#24-aws-transform-integration).)

### 4.2 Storage Architecture

**Artifact Storage (`ArtifactStore` abstraction, `src/storage/`):**

`LocalArtifactStore` (default) writes to the local filesystem; `S3ArtifactStore`
(used only by the AWS Transform integration) writes to S3. Both use the same
path convention: `<database-name>/<job-id>/<agent-name>/artifact.json`

- Collector outputs: `<db-name>/<job-id>/collector/output.json`
- Triage decisions: `<db-name>/<job-id>/referee-triage/triage.json`
- Analysis outputs: `<db-name>/<job-id>/analysis-<type>/analysis.json`
- Assignment versions: `<db-name>/<job-id>/assignment/v{N}/assignment.json`
- Reality check: `<db-name>/<job-id>/reality-check/reality_check.json`
- Schema designs: `<db-name>/<job-id>/schema-<target>/v1/schema_output.json`
- Design traces: `<db-name>/<job-id>/schema-<target>/v1/design_trace.json`
- Load test results: `<db-name>/<job-id>/load-test-<engine>/v{N}/results/summary.json`
- Load test scripts: `<db-name>/<job-id>/load-test-<engine>/v{N}/scripts/` (customer deliverable)
- Load test per-pattern: `<db-name>/<job-id>/load-test-<engine>/v{N}/results/{query_id}.json`
- Query journeys: `<db-name>/<job-id>/query-journeys/{query_id}.json`
- Synthesis report: `<db-name>/<job-id>/referee-synthesis/report.json`
- Uploads (offline): `<db-name>/<job-id>/uploads/collector-output.json`

Job IDs are UUIDs: the API uses a full `uuid.uuid4()`; the local scripts and skills (`run_assessment.py`, `run_collect.py`) truncate to the first 8 hex characters for shorter paths. Neither generates a KSUID. The `<database-name>` prefix enables browsing and lifecycle management per source database.

**Local Job Metadata:**

- Stored in a per-job progression file under the artifact store (not a separate database)
- Progress tracking: current stage, percent complete — derived from which artifact files exist, or recorded explicitly for phase status
- Phase progression: `collect_triage → analysis → assignment → reality_check → assignment_review → schema_design → load_test → synthesis`
- Human gate state: phase status, assignment version, approval status
- User information: requester, configuration

### 4.3 Data Retention

**Analysis Results:**

- Persisted under the local artifact directory (`./artifacts/` by default) for as long as the user keeps it; there is no hosted lifecycle policy
- Users can delete a job's directory, or the whole artifact root, at any time

**Temporary Data:**

- No PII stored outside what the user's own database already contains

**Load Test Infrastructure:**

- All provisioned target resources (tables, indexes) torn down after test completion
- Only results and scripts persist in the local artifact store

---

## 5. API Architecture

### 5.1 API Design

**API Server:**

- FastAPI application, run locally with `uv run uvicorn src.api.main:app` (see [README.md](../../README.md))
- Loopback-only by default — `HostAllowlistMiddleware` (`src/api/host_guard.py`) rejects requests not addressed to a loopback host name; `MODERNIZER_ALLOWED_HOSTS` overrides it
- REST endpoints for job management; the UI is served separately (`npm start` / local build)
- CORS restricted to the local UI origin (`ALLOWED_ORIGIN`, default `http://localhost:3000`)
- Progress is polled (`GET /{job_id}` and `/execution-history`), not pushed — there is no WebSocket endpoint

### 5.2 API Endpoints

The listing below is a summary. There is no committed OpenAPI spec or Swagger
site for this API anymore (the generator and the GitHub Pages workflow that
published it were retired with the hosted deployment, #175); the FastAPI
app's auto-generated `/docs` and `/openapi.json` routes are the live
reference when running the API locally. The project's public documentation
site (#325) covers how to run and interpret an assessment, not the raw REST
schema.

**REST API — Assessment Lifecycle:**

- `POST /api/v1/assessments/prepare` - Pre-create job ID + presigned S3 URL (offline upload flow)
- `POST /api/v1/assessments` - Start new assessment (live or offline mode)
- `GET /api/v1/assessments/{job_id}` - Get job status and progress
- `GET /api/v1/assessments/{job_id}/phases` - Get full phase progression (`PhaseProgression` contract)
- `GET /api/v1/assessments/{job_id}/results` - Get synthesis report
- `GET /api/v1/assessments` - List all assessments
- `DELETE /api/v1/assessments/{job_id}` - Cancel assessment

**REST API — Upload Flow (Offline Mode):**

- `POST /api/v1/assessments/{job_id}/uploads/confirm` - Confirm presigned URL upload completed
- `GET /api/v1/assessments/{job_id}/uploads` - List uploaded files
- `DELETE /api/v1/assessments/{job_id}/uploads/{filename}` - Delete uploaded file

**REST API — Pipeline Artifacts:**

- `GET /api/v1/assessments/{job_id}/collector` - Raw collector output
- `GET /api/v1/assessments/{job_id}/triage` - Triage signals and engine selection
- `GET /api/v1/assessments/{job_id}/analysis/{engine}` - Per-engine analysis output
- `GET /api/v1/assessments/{job_id}/reality-check` - Engine consolidation results
- `GET /api/v1/assessments/{job_id}/assignments` - Query-to-engine assignments

**REST API — Human Gates & Schema Revision:**

- `PUT /api/v1/assessments/{job_id}/assignments` - Override assignments (supports per-query and table-level `scope_narrowing`)
- `POST /api/v1/assessments/{job_id}/resume` - Resume pipeline at a specific phase (accepts `ResumeRequest` body with phase enum + optional `scope_engines`)
- `GET /api/v1/assessments/{job_id}/schema/{engine}` - Schema output + version metadata
- `GET /api/v1/assessments/{job_id}/schema/{engine}/versions` - Schema version history
- `PUT /api/v1/assessments/{job_id}/schema/{engine}/revisions` - Submit schema revision request
- `POST /api/v1/assessments/{job_id}/schema/{engine}/confirm` - Confirm single engine schema
- `POST /api/v1/assessments/{job_id}/schema/confirm-all` - Confirm all engine schemas (transitions phase)

**REST API — Query Journeys:**

- `GET /api/v1/assessments/{job_id}/query-journeys` - Paginated query journey list (default 50, max 200)
- `GET /api/v1/assessments/{job_id}/query-journeys/{query_id}` - Single query journey detail

**REST API — Observability & Results:**

- `GET /api/v1/assessments/{job_id}/agents` - Per-agent status with artifact summaries
- `GET /api/v1/assessments/{job_id}/execution-history` - Full per-stage history, derived from the artifact directory layout (`LocalExecutionService`), in the same shape the old Step Functions-backed endpoint returned
- `GET /api/v1/assessments/{job_id}/results/table-mappings` - Paginated table mappings from synthesis
- `GET /api/v1/assessments/{job_id}/schema-designs` - Generated schema designs
- `GET /api/v1/dashboard/stats` - Dashboard statistics
- `GET /api/v1/settings` - Application settings

### 5.3 Authentication and Authorization

There is no user authentication layer. The API only runs locally, is not
exposed to the internet, and the loopback-only host allowlist
(`src/api/host_guard.py`) is the only access control. A prior design used
Amazon Cognito user pools behind the hosted ALB; that is gone with the hosted
deployment (#175).

**Live-mode database access:**

- Database credentials come from a Secrets Manager secret, fetched on the
  automation instance (§7.2) — not from the machine running the API, and
  not from IAM database authentication, which isn't implemented

---

## 6. Technology Stack

### 6.1 Core Technologies

| Component         | Technology               | Version | Justification                                                    |
| ----------------- | ------------------------ | ------- | ---------------------------------------------------------------- |
| Backend Language  | Python                   | 3.12+   | Rich ecosystem, database drivers, AI/ML libraries                |
| API Framework     | FastAPI                  | Latest  | Modern, async, auto-generated docs                                |
| Agent Framework   | Strands SDK              | Latest  | Open-source agentic framework, tool management                   |
| Load Testing      | k6                       | Latest  | Built-in rate control, percentiles, AWS jslib for DynamoDB       |
| Job Orchestration | `LocalOrchestrator`      | -       | Direct function calls over the local artifact store, no cloud workflow service |
| Frontend          | React + TypeScript       | 18+     | Component reusability, type safety                               |
| UI Library        | Cloudscape Design System | Latest  | AWS native, professional appearance                               |
| Container Runtime | Docker                   | Latest  | Used for the `agent-load-test` image and the AWS Transform integration — not for a hosted service |
| AI/ML             | AWS Bedrock              | -       | Managed LLM access (optional)                                     |

### 6.2 Database Drivers

| Database   | Python Driver          | Version |
| ---------- | ---------------------- | ------- |
| MySQL      | mysql-connector-python | 8.0+    |
| PostgreSQL | psycopg2-binary        | 2.9+    |
| MariaDB    | mysql-connector-python | 8.0+    |
| SQL Server | pymssql                | 2.2+    |
| Oracle     | cx_Oracle              | 8.3+    |
| DB2        | ibm_db                 | 3.1+    |
| Redis      | redis-py               | 5.0+    |

### 6.3 AWS Services

AWS services are now used directly, with the user's own credentials, only
where they add value — not through a hosted account the project manages.

**Storage:**

- S3, only via the AWS Transform integration's `S3ArtifactStore` — the local API/UI/CLI paths use the filesystem

**Live-mode collection (optional — offline/DDL collection needs none of this):**

- CloudWatch and Performance Insights APIs for instance- and query-level metrics
- Secrets Manager for database credentials, if the user already stores them there
- RDS API for metadata

**AI/ML:**

- Bedrock for LLM access (optional)

**Monitoring:**

- CloudWatch for RDS instance *metrics* (not application logs — the API has
  no log-viewing endpoint), when analyzing a live, customer-owned RDS
  instance

---

## 7. Security Architecture

### 7.1 Security Overview

Database Modernizer Assessment runs on the user's own machine, under the
user's own AWS credentials, against databases the user already has access
to. There is no hosted account, service role, or shared infrastructure to
secure — the attack surface is "whatever already runs on the user's machine
with their own credentials," not a separate deployed service.

**Responsibilities:**

- The user: RDS instance security, their own AWS credential management, and
  choosing a collection mode — offline/DDL (no connection, no live
  credentials, works today) or live (see below; gated and not fully wired)
- The project: safe handling of credentials in memory during a run,
  loopback-only binding for the local API (`src/api/host_guard.py`), and
  framing untrusted collector/LLM text before it reaches a prompt or a
  rendered report (`frame_untrusted`, `src/report/escaping.py`)

**Live mode is not a direct connection.** Every live collector (MySQL,
PostgreSQL, SQL Server, Oracle) runs its SQL through SSM Run Command on a
per-VPC EC2 "automation instance" (`src/tools/aws/automation.py`,
`src/tools/aws/ssm_executor.py`) — never a direct connection from wherever
the API or CLI runs. The local API's path to provision that instance
(`collection_mode=live` with `cluster_id`) is gated behind
`MODERNIZER_ENABLE_AUTOMATION=1` because it deploys billable AWS resources,
and even opted in it isn't fully wired yet (the provisioned
`automation_instance_id` doesn't reach the collector environment) — see
[#342](https://github.com/aws-samples/sample-aws-genai-db-modernizer/issues/342).
This is meant for a future option where the automation instance runs
collection on behalf of a customer who can't run the collector scripts
themselves.

### 7.2 Credential Handling

**Principles:**

- Credentials handled in memory only during job execution
- Never persisted to disk or logs

**Implementation:**

- Live-mode database credentials come from a Secrets Manager secret,
  fetched *on the automation instance* that runs the SSM commands — never
  on the machine running the API, and never from a `.env` file or an
  interactive prompt (neither exists in this codebase)
- Stored in memory only (Python variables) on the automation instance;
  connection objects closed after use
- No credential logging or debugging output

### 7.3 Network Security

**Local-only API:**

- The FastAPI server binds to loopback and rejects requests addressed to
  any other host name by default (`HostAllowlistMiddleware`)
- CORS restricted to the local UI's origin

**SSL/TLS Requirements:**

- All live database connections use SSL/TLS, certificate verification enabled
  by default; self-signed certificates supported (opt-in)

### 7.4 Data Retention

**Analysis Results:**

- Persisted under the local artifact directory for as long as the user keeps it

**Load Test Resources:**

- All provisioned target infrastructure (tables, indexes) torn down automatically after test
- Table names prefixed with `LoadTest_` for easy identification and manual cleanup if a run is interrupted

---

## 8. Operational Considerations

There is no hosted service to monitor, scale, or recover — the tool runs for
the duration of one assessment on the user's own machine. What used to be
"operational considerations" for the retired ECS/Step Functions deployment
(monitoring, scaling, disaster recovery, cost optimization for a running
service) no longer applies.

**What still matters locally:**

- **Logging**: structured logging (structlog) to stdout/stderr. There is no
  `/logs` API endpoint and no `CloudWatchLogsService` — that hosted-era route
  was removed along with the rest of the hosted path. Live-mode collection
  reads CloudWatch *metrics* (not application logs) directly from the
  user's RDS instance when it runs
- **Restart**: a failed phase restarts from scratch (§3.4) — the user re-runs
  the command or resumes the phase, there is no automatic retry service
- **Cost**: the only ongoing cost is Bedrock token usage, if the user opts
  into `--llm-mode bedrock`, and whatever target infrastructure a load test
  provisions (torn down immediately after)

---

## 9. CI/CD Pipeline

### 9.1 Overview

The project uses GitHub Actions (`.github/workflows/ci.yml`) to lint, test, and
run the deterministic end-to-end suite on every push and pull request against
`main`. There is nothing to build and deploy — the pipeline validates code,
it does not ship a running service.

### 9.2 Jobs

| Job                  | What it checks                                                           |
| --------------------- | ------------------------------------------------------------------------- |
| `lint`                | ruff, black, isort, mypy, Claude Code command/skill reference validation |
| `test`                | unit, contract, property and graph tests (`./ci/test.sh`)                |
| `e2e`                 | deterministic end-to-end pipeline run, HTML/PDF checks, UI smoke test     |
| `security`            | Gitleaks secret scan, Bandit, Semgrep, cfn-lint and Checkov on the surviving CloudFormation templates (`automation.yaml`, `example-ci-runner-iam.yaml`, `pipeline/iam-roles.yaml`), Checkov on both Dockerfiles (`Dockerfile.atx`, `agent-load-test/Dockerfile`) |
| `validate-contracts`  | Pydantic contract validation + contract tests                            |
| `ui-build`            | React unit tests (`npm run test:ci`) and a production build              |

Pre-commit hooks run a subset of these scans locally (`cfn-nag` and Checkov on
`infrastructure/`), so the `security` job in CI is the complete check.

### 9.3 Testing Strategy

- **Unit tests**: pytest with parallel execution, coverage reporting
- **UI tests**: React testing library
- **Contract validation**: `validate_contracts.py` and `validate_schemas.py` run on every change to `src/` or `src/contracts`
- **End-to-end tests**: the deterministic pipeline rendered against both sample databases, with HTML/PDF/UI checks (`ci/e2e.sh`); a separate headless-LLM suite (`ci/e2e-llm.sh`) exercises a real `/modernize` run and is not gated on every PR

---

## Appendices

### Appendix A: Architecture Decision Records

**Key ADRs:**

- [ADR-001: State Management and Checkpoints](decisions/ADR-001-state-management-and-checkpoints.md)
- [ADR-002: Structured Output and Validation](decisions/ADR-002-structured-output-and-validation.md)
- [ADR-003: Progress Reporting Architecture](decisions/ADR-003-progress-reporting-architecture.md)
- [ADR-004: RDS Tools and AWS Integration](decisions/ADR-004-rds-tools-and-aws-integration.md)
- [ADR-005: Mini-Collectors for Large Databases](decisions/ADR-005-mini-collectors-for-large-databases.md)
- [ADR-006: Analysis Agent Patterns](decisions/ADR-006-analysis-agent-patterns.md)
- [ADR-007: Referee Orchestration](decisions/ADR-007-referee-orchestration.md)
- [ADR-008: Contract Versioning](decisions/ADR-008-contract-versioning.md)
- [ADR-009: Testing Infrastructure](decisions/ADR-009-testing-infrastructure.md)
- [ADR-010: Release Management](decisions/ADR-010-release-management.md)
- [ADR-011: Monorepo Structure](decisions/ADR-011-monorepo-structure.md)
- [ADR-012: CloudFormation over CDK](decisions/ADR-012-cloudformation-over-cdk.md)
- [ADR-013: Core Infrastructure Cost Optimization](decisions/ADR-013-core-infrastructure-cost-optimization.md)
- [ADR-014: CI/CD Pipeline and Ephemeral Environments](decisions/ADR-014-cicd-pipeline-and-ephemeral-environments.md)
- [ADR-015: DNS Naming Convention](decisions/ADR-015-dns-naming-convention.md)
- [ADR-016: Compute and Orchestration Strategy](decisions/ADR-016-compute-and-orchestration-strategy.md)
- [ADR-017: Analysis Agent Scoring Framework](decisions/ADR-017-analysis-agent-scoring-framework.md)
- [ADR-018: Reality Check and Human Approval Gate](decisions/ADR-018-reality-check-and-human-gate.md)
- [ADR-019: Query Journey Materialization](decisions/ADR-019-query-journey-materialization.md)
- [ADR-020: Load Testing Stage Architecture](decisions/ADR-020-load-testing-stage.md)

### Appendix B: Related Documentation

**Architecture:**

- [Architecture Diagrams](diagrams/README.md) - Comprehensive Mermaid diagrams
- [Business Requirements](../01-requirements/business-requirements.md) - Business context and requirements

**Contracts:**

- [Agent Contracts Specification](../contracts/agent-contracts-spec.md) - Input/output contracts
- [Contract Schemas](../contracts/schemas/) - JSON schemas for validation

**Data Specifications:**

- [Database Collection Matrix](../data-specs/database-collection-matrix.md) - Data collection requirements
- [Redis Migration Patterns](../data-specs/redis-migration-patterns.md) - Redis-specific patterns

**Implementation:**

- [Implementation Guides](../guides/) - Detailed implementation guidance
- [Testing Guide](../guides/testing-guide.md) - Testing strategies
- [Load Testing New Engine Guide](../guides/load-testing-new-engine.md) - How to add load testing for new target engines

---

## Revision History

| Version | Date       | Author   | Changes                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| ------- | ---------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 1.0     | 2026-01-21 | tebanieo | Initial HLD (cloud-first)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| 2.0     | 2026-01-21 | tebanieo | Revised for local-first architecture                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| 3.0     | 2026-01-22 | tebanieo | Updated with Strands SDK architecture                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| 4.0     | 2026-02-02 | tebanieo | Added ADR-001 through ADR-010 decisions                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| 5.0     | 2026-02-06 | tebanieo | Simplified document, removed diagrams                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| 6.0     | 2026-02-06 | tebanieo | **Complete rewrite: Removed low-level details, replaced Redis/SQS with EventBridge for orchestration, made Bedrock and Cognito required, added OpenTelemetry for monitoring, updated to Python 3.12, clarified API architecture (ALB + ECS Fargate), removed cost estimates, removed code samples**                                                                                                                                                                                                                                                                                                                                        |
| 7.0     | 2026-02-18 | tebanieo | **ADR-016 update: Three-layer orchestration (Step Functions + EventBridge + Strands SDK), referee split into triage/synthesis, restart-from-scratch strategy, S3 naming convention, task sizing table, hybrid analysis selection**                                                                                                                                                                                                                                                                                                                                                                                                         |
| 8.0     | 2026-02-27 | tebanieo | **UI migration: Replaced App Runner with ECS Fargate behind shared ALB. UI and API use path-based routing on the same ALB with Cognito auth. Removed CORS dependency.**                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| 9.0     | 2026-03-27 | tebanieo | **Phase 0 close-out: Updated status to reflect implemented architecture. Offline upload flow via presigned URLs, execution history API with hierarchical Map iteration tracking, agent artifact summaries. Progress uses polling (WebSocket deferred to Phase 1). S3 CORS for browser uploads. CI pipeline job ordering fixed.**                                                                                                                                                                                                                                                                                                           |
| 10.0    | 2026-04-24 | tebanieo | **Reality Check & Human Gate: Added Assignment Resolution agent (signal-driven overrides, anti-pattern penalties), Reality Check agent (CTO-level engine consolidation), human-in-the-loop approval gate via Step Functions `waitForTaskToken`. Updated pipeline flow: Analysis → Assignment → Reality Check → Human Gate → Schema Design. Added group splitting pipeline for large workloads (MAX_GROUP_SIZE=20). Updated API endpoints to reflect full assessment lifecycle including `/reality-check`, `/assignments`, `/resume`. Added `assignment_review` phase to phase progression.**                                               |
| 11.0    | 2026-05-20 | tebanieo | **Load Testing & Query Journeys: Added Load Testing stage (k6-on-ECS, DynamoDB engine implemented, abstract base for engine extensibility). Added Query Journey Materialization (progressive per-query S3 files). Updated pipeline to 19 steps with two human gates. Added ~17 missing API endpoints (upload flow, schema revision, query journeys, observability). Added CI/CD pipeline section (Section 9). Updated task sizing table (load test: 4 vCPU / 16 GB). Updated S3 path listing. Updated phase progression to 8 phases. Added ADRs 011-020 to appendix. Named all 7 analysis agents and 6 schema design targets explicitly.** |
| 11.1    | 2026-06-01 | tebanieo | **Open-source release candidate review: Full architecture documentation audit. Sanitized internal references. Updated ADR statuses (superseded, accepted). Verified diagrams against current codebase. Corrected storage architecture to reflect ArtifactStore abstraction. Added Aurora Absorption (ADR-021) to accepted decisions.**                                                                                                                                                                                                                                                                                                     |
| 12.0    | 2026-10-05 | tebanieo | **Hosted deployment removed (#175): the ECS Fargate / ALB / Cognito / Step Functions / EventBridge / DynamoDB deployment described in sections 2, 5.3, 6.3, 7 and 8 is gone — the project is local-only (Claude Code, local API/UI, deterministic CLI) plus the separate AWS Transform integration. Rewrote Deployment Architecture, Orchestration Pattern, Progress Reporting, Storage Architecture, API Architecture, Technology Stack, Security Architecture, Operational Considerations and CI/CD Pipeline to describe the current local-first, GitHub-Actions-only reality.**                                                      |

---

**Document Status:** Approved
**Current Phase:** Local-first, post-hosted-removal (#175)
**Next Review:** When the AWS Transform integration or local UI/API architecture materially changes
