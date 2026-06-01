# Database Modernizer - High-Level Design (HLD)

## Document Information

**Version:** 11.1 (Open-Source Release Candidate Review)
**Date:** June 1, 2026
**Status:** Approved
**Owner:** Database Modernizer Engineering Team

---

## Executive Summary

Customers modernizing off monolithic relational databases ask us the same question: **which purpose-built database should I choose?** Should this workload go to DynamoDB? Does it need a document store like DocumentDB? Would a cache layer in ElastiCache solve the problem? Should full-text search move to OpenSearch? Or does the query pattern actually belong in Aurora?

Getting the answer wrong means failed modernizations, re-architecture mid-project, and wasted months. Getting it right requires deep analysis of every query pattern, understanding access patterns at scale, and mapping them to the right engine — a process that traditionally takes weeks of specialist time per database.

**Database Modernizer answers that question automatically.** It analyzes every query pattern in your relational database, scores each one against AWS purpose-built engines, validates the overall architecture for operational complexity, and produces ready-to-implement schema designs with load-tested performance data. You can run it from your laptop, with Claude Code, or deploy to your own AWS account.

The core pipeline is **fully deterministic** — pattern detection, scoring, assignment, and consolidation all run without any LLM dependency. GenAI enhances the pipeline at key decision points (analysis advisors, consolidation validation, executive summaries) but is never required. You get reproducible, auditable results every time, with AI refinement layered on top when available.

**Supported sources:** PostgreSQL, MySQL, MariaDB (Redis planned)

**Target engines:** DynamoDB, DocumentDB, ElastiCache/Redis, OpenSearch, Aurora PostgreSQL, Aurora MySQL

### Key Design Decisions

- **AWS-native deployment**: Runs on ECS Fargate in customer VPC with direct private connectivity to RDS instances
- **Deterministic core**: Full pipeline runs without LLM calls (`--llm-mode none`); GenAI is an enhancement layer, not a dependency
- **Strands SDK agent framework**: Open-source agentic framework for agent orchestration, tool management, and LLM integration
- **Multi-agent architecture**: Independent, composable agents (Collector, Referee-Triage, Analysis, Assignment Resolution, Reality Check, Referee-Synthesis, Schema Design, Load Testing)
- **Three-layer orchestration**: Step Functions for job workflow, EventBridge for notifications, Strands SDK for intra-agent logic ([ADR-016](decisions/ADR-016-compute-and-orchestration-strategy.md))
- **Two human-in-the-loop gates**: Pipeline pauses after triage (assignment review) and after reality check (schema design approval) via `waitForTaskToken`
- **Reality check consolidation**: CTO-level engine consolidation eliminates low-value engines, reducing operational complexity
- **Load testing validation**: k6-on-ECS empirical performance testing against real target infrastructure with per-pattern latency and cost measurement ([ADR-020](decisions/ADR-020-load-testing-stage.md))
- **Query journey materialization**: Progressive per-query S3 files tracking each query from source through assignment, schema design, and load testing ([ADR-019](decisions/ADR-019-query-journey-materialization.md))
- **Hybrid analysis selection**: AI-driven triage selects relevant analysis agents; Step Functions executes them deterministically
- **IAM-based security**: IAM roles for AWS service access, IAM authentication for RDS connections
- **Bedrock integration**: AWS Bedrock for AI-powered analysis (optional, enhances results)
- **Open-source distribution**: GitHub repository with MIT-0 license

### Phase 1 Definition

Phase 0 (Q1-Q2 2026) established the foundational architecture: collector agents, analysis agents for seven destination databases, schema design, and DynamoDB load testing. Phase 1 (Q3-Q4 2026) extends load testing to remaining engines, adds code generation, Neptune/Keyspaces/Aurora schema agents, EventBridge real-time notifications, and production hardening.

### Design Philosophy

- **Security by design**: Runs in customer VPC with private RDS connectivity
- **No vendor lock-in**: Customers control their deployment and data
- **Transparent**: Open-source code for full transparency
- **AWS-optimized**: Leverage AWS-native services (RDS, CloudWatch, Performance Insights, Secrets Manager, IAM)

### Performance Targets

- Complete analysis in <6 hours for 1,000 table RDS database
- <3 second API response time for any query type
- Leverage Performance Insights for query-level data
- CloudWatch metrics for instance-level performance
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
- [VPC Deployment Architecture](diagrams/02-vpc-deployment.md)
- [Agent Framework Architecture](diagrams/04-agent-framework.md)

### Architecture Components

**Application Layer:**

- Web UI (React SPA): Assessment report viewer, interactive recommendation review, query journey explorer
- API Server (FastAPI on ECS Fargate): REST endpoints, WebSocket for real-time progress
- Orchestrator (Step Functions + EventBridge): Step Functions for job workflow coordination, EventBridge for progress notifications

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

- Query Journey Materialization: Progressive per-query S3 files enriched at each pipeline stage (source, assignment, design, load test)

**Data Storage Layer:**

- S3: Collector outputs, reports, job artifacts, query journeys (path: `<database-name>/<ksuid>/<agent-name>/artifact.json`)
- DynamoDB: Job metadata, status tracking
- EventBridge: Event bus for progress notifications

**Source Databases (Customer-Owned, Read-Only Access):**

- RDS Instances: MySQL, PostgreSQL, MariaDB, SQL Server, Oracle, DB2 (customer private subnets)
- Redis Instances: ElastiCache for Redis or self-managed Redis (customer infrastructure)
- Direct VPC connectivity, CloudWatch metrics, Performance Insights enabled
- Database Modernizer does NOT deploy its own RDS/Redis instances

**AWS Services (via IAM Role):**

- CloudWatch API, Performance Insights API, RDS API, Secrets Manager, S3, DynamoDB, EventBridge, Step Functions
- Bedrock API for AI-powered analysis (required)
- Cognito for user authentication (required)

### System Context

**Primary Users:**

- Database Architects: Deploy tool in VPC, analyze RDS workloads, review recommendations
- IT Leaders: Review analysis reports, approve migrations
- Developers: Implement schema designs, execute migrations

**External Systems:**

- Amazon RDS instances and Redis (read-only access via VPC)
- AWS APIs (CloudWatch, Performance Insights, RDS API, Secrets Manager)
- AWS Bedrock for AI-powered analysis

**Key Design Principles:**

1. VPC-Native: Runs in customer VPC with direct database access
2. AWS-Optimized: Leverages AWS-native services for comprehensive data collection
3. Simple Deployment: CloudFormation deploy with automatic IAM role setup
4. Transparent: Open-source code for full auditability
5. IAM-Based Security: IAM roles for service access

---

## 2. Deployment Architecture

**For detailed deployment diagrams, see:**

- [VPC Deployment Diagram](diagrams/02-vpc-deployment.md)
- [Docker Compose Deployment Diagram](diagrams/03-docker-compose-deployment.md)

### 2.1 Primary Deployment: ECS Fargate in VPC

**Architecture:**

```
Customer AWS Account / VPC
├── Public Subnets
│   └── Application Load Balancer (ALB)
│       - HTTPS endpoint for Web UI and API (shared ALB)
│       - Cognito authentication on all routes
│       - Path-based routing: /api/* -> API, /* -> UI
│
├── Private Subnets (Application Tier)
│   ├── ECS Fargate Tasks
│   │   ├── UI Service (React SPA via serve, port 8080)
│   │   ├── API Service (FastAPI, port 8000)
│   │   ├── Agent Worker Tasks (Auto-scaling 1-10)
│   │   └── Load Test Tasks (k6 + coordinator, 4 vCPU / 16 GB)
│
├── Private Subnets (Data Tier)
│   ├── Customer RDS Instances (read-only access)
│   └── Customer Redis Instances (read-only access)
│   NOTE: Database Modernizer does NOT deploy its own RDS/Redis.
│         These subnets host the customer's existing database
│         instances that the tool analyzes.
│
├── Data Storage (AWS Managed Services, accessed via VPC Endpoints)
│   ├── S3 Bucket (Collector outputs, reports, query journeys)
│   ├── DynamoDB Table (Job metadata, status)
│   └── EventBridge (Event bus for orchestration)
│
└── IAM Roles
    ├── ECS Task Role (general agents)
    │   - Permissions: rds:*, cloudwatch:*, pi:*, bedrock:*,
    │     secretsmanager:*, s3:*, dynamodb:*, events:*,
    │     cognito-idp:*
    └── Load Test Task Role (scoped)
        - DynamoDB: CreateTable, DeleteTable, PutItem, GetItem,
          Query, BatchWriteItem, etc. — scoped to LoadTest_* tables
        - S3: read/write to artifact bucket
```

**Why ECS Fargate:**

- Direct VPC connectivity to private databases
- No VPN or public RDS endpoint required
- IAM roles for AWS service access (no credential management)
- Auto-scaling based on workload
- Secure by default (private subnets, security groups)

**Deployment Method:**

- AWS CloudFormation for infrastructure as code
- One-command deployment: `aws cloudformation deploy`
- Automatic IAM role and security group setup

### 2.2 Secondary Deployment: Docker Compose (Development)

**Purpose:** Local development and testing only

**Limitations:**

- Requires publicly accessible RDS endpoint OR VPN setup
- Not suitable for production RDS instances (usually private)
- Limited scalability (1-2 concurrent jobs)

**Use Cases:**

- Local development
- Demos with test RDS instances
- Quick evaluation

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
- Supports two input modes: live (direct DB connection) and offline (pre-collected JSON from S3)

**2. Analysis Category (Modernization Analysis)**

- Individual agents: DynamoDB, DocumentDB, OpenSearch, ElastiCache, Neptune, Keyspaces, Aurora (7 total)
- Only agents selected by Referee-Triage run (not always all 7)
- Run in parallel via Step Functions Map state
- Can spawn sub-agents for specialized analysis

**3. Referee-Triage Agent (Workload Analysis & Agent Selection)**

- Reads collector output, detects workload signals (key-value lookups, text search, write-heavy, aggregations, etc.)
- Selects relevant analysis agents based on signals (e.g., skips Neptune for key-value workloads)
- Each signal maps to target engines and carries the `query_ids` / `table_ids` that triggered it
- Returns structured list with reasons for selection/skipping
- Logged to S3 for auditability

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
- Pipeline uses three ECS task types: `schema-split` (grouping), `schema-design` (per-group generation), `schema-merge` (combine groups)
- Runs per-engine, chained after human approval in the Map state
- Includes PE review loop for design validation; produces `design_trace.json` artifact

**8. Load Testing Category (Empirical Performance Validation)**

- Engine-agnostic coordinator orchestrates: provision → seed → generate → dry-run → execute → parse → teardown
- Uses **k6** as load generation engine running on ECS Fargate (4 vCPU, 16 GB)
- Separate Docker image (`agent-load-test`) with k6 binary bundled
- Abstract base classes (`BaseProvisioner`, `BaseSeeder`, `BaseScriptGenerator`, `BaseRunner`) enable engine extensibility
- **DynamoDB engine** (implemented): k6 AWS jslib with SigV4 signing, `ReturnConsumedCapacity` per request, Jinja2 templates for 7 operation types (GetItem, Query, PutItem, UpdateItem, DeleteItem, BatchGetItem, BatchWriteItem)
- **Future engines**: OpenSearch (k6 HTTP module), ElastiCache (xk6-redis), DocumentDB (xk6-mongo)
- Generated k6 scripts serve dual purpose: test execution AND customer deliverable (copy-paste ready code)
- Non-blocking: on failure, catches and proceeds to synthesis (results still valid without load test)
- Per [ADR-020](decisions/ADR-020-load-testing-stage.md)

**9. Query Journey Materialization (Cross-Cutting)**

- Progressive per-query S3 files enriched at each pipeline stage
- Four materialization functions called by respective handlers:
  - `materialize_source()` — Collector creates initial journey file per query
  - `materialize_assignment()` — Assignment resolver adds engine assignment
  - `materialize_design()` — Schema design adds access pattern design details
  - `materialize_load_test()` — Load testing adds latency/cost metrics
- Enables O(1) per-query API lookups (single S3 GET) instead of cross-referencing multiple artifacts
- Path: `{db}/{job}/query-journeys/{query_id}.json`
- Per [ADR-019](decisions/ADR-019-query-journey-materialization.md)

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

**Three-Layer Orchestration (per [ADR-016](decisions/ADR-016-compute-and-orchestration-strategy.md)):**

```
┌──────────────────────────────────────────────────────────────┐
│  Layer 1: Job Orchestration (Step Functions)                 │
│  Collector → Triage → [Human Gate 1] → Analysis Map          │
│  → Assignment Resolution → Reality Check                     │
│  → [Human Gate 2] → Schema Design Map (group split/merge)    │
│  → Load Test Map → Synthesis → [deeper analysis loop?]       │
└──────────────────────────────────────────────────────────────┘
                        │ progress events
                        ▼
┌──────────────────────────────────────────────────────────────┐
│  Layer 2: Notifications (EventBridge → WebSocket)            │
│  Agent mini-step progress → EventBridge → Lambda → WS → UI   │
└──────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────┐
│  Layer 3: Intra-Agent (Strands SDK + sub-processes)          │
│  Agent runs in one ECS task, spawns sub-processes internally │
│  Declares restart points (mini-steps)                        │
│  Reports status at each mini-step boundary                   │
└──────────────────────────────────────────────────────────────┘
```

**Layer 1: Job Orchestration (Step Functions)**

- Step Functions Standard Workflow orchestrates the macro pipeline
- `ecs:RunTask.sync` integration — waits for each ECS task to complete
- Analysis Map state runs per-engine analysis agents in parallel
- Assignment Resolution and Reality Check run sequentially after all analysis completes
- **Two human approval gates** using `waitForTaskToken`:
  - **Gate 1 (after Triage):** User reviews triage signals and proposed engine selections before analysis begins
  - **Gate 2 (after Reality Check):** User reviews final query-to-engine assignments before schema design proceeds
- Schema Design Map state runs per-engine with group splitting for large workloads
- Load Test Map state runs per-engine after schema design (non-blocking on failure)
- PE review loop within schema design for design validation
- Per-state retry and error handling
- Visual execution history for debugging

**Layer 2: Notifications (EventBridge)**

- EventBridge carries progress events only — not workflow coordination
- Agents publish mini-step progress to EventBridge at each boundary
- Lambda function forwards events to API Gateway WebSocket
- Real-time UI updates for every agent mini-step

**Layer 3: Intra-Agent Orchestration (Strands SDK)**

- Manages tool execution within a single agent
- LLM-driven decision making
- Each agent runs in one ECS task with generous resources
- Agents spawn sub-processes internally for parallelism (e.g., mini-collectors)
- Error handling and retries within the agent

**Workflow Execution:**

1. Job submitted via API
2. API starts Step Functions execution (`states:StartExecution`)
3. Step Functions runs Collector ECS task
4. Collector completes, writes output to S3, materializes query journey source files
5. Step Functions runs Referee-Triage ECS task
6. Triage reads collector output, detects workload signals, returns list of selected engines
7. **Human Gate 1** (`waitForTaskToken`): Pipeline pauses. User reviews triage signals and engine selections via UI
8. User approves → API calls `SendTaskSuccess` → pipeline resumes
9. Step Functions RunAnalysisPipelines Map state runs per-engine analysis agents in parallel (ECS tasks)
10. All analysis completes, outputs written to S3
11. Step Functions runs Assignment Resolution ECS task — maps each query to best-fit engine
12. Step Functions runs Reality Check ECS task — eliminates low-value engines, consolidates queries
13. **Human Gate 2** (`waitForTaskToken`): Pipeline pauses. User reviews final assignments via UI, optionally overrides engine assignments or excludes tables
14. User approves → API calls `SendTaskSuccess` with the task token → pipeline resumes
15. Step Functions RunSchemaDesignPipelines Map state runs per-engine schema design (ECS tasks):
    - Large workloads split into groups (MAX_GROUP_SIZE=20 tables) using analysis signals for intelligent clustering
    - Groups processed in parallel, then merged into unified schema per engine
    - Schema Design includes PE review loop for design validation
    - Produces `schema_output.json` and `design_trace.json` per engine
    - Materializes query journey design section
16. Step Functions RunLoadTestPipelines Map state runs per-engine load testing (ECS tasks):
    - Provisions target infrastructure (e.g., DynamoDB tables with GSIs)
    - Seeds synthetic data at realistic volumes
    - Generates k6 test scripts per access pattern
    - Dry-runs scripts for fail-fast validation
    - Executes 15-minute sustained load test at source call rates
    - Collects per-pattern latency percentiles (p50-p999) and ConsumedCapacity costs
    - Tears down all provisioned infrastructure
    - Materializes query journey load test section
    - **Non-blocking**: on failure, catches error and proceeds to synthesis
17. Step Functions runs Referee-Synthesis ECS task
18. Synthesis produces weighted ranking; may request deeper analysis (max 2 iterations via loop back to step 15)
19. Job complete

### 3.4 Restart Strategy

**Restart-from-scratch per step (per [ADR-016](decisions/ADR-016-compute-and-orchestration-strategy.md)):**

No intra-step checkpointing. If a step fails, it restarts from scratch. This keeps agent code simple — no partial state recovery logic.

**Agent-Defined Restart Points:**

Each agent declares its mini-steps (e.g., connect, collect_schema, collect_metrics, collect_samples). Restarting a previous mini-step triggers a cascade: all subsequent mini-steps and downstream agents are invalidated and must re-run.

**Why restart-from-scratch:**

- Simpler agent code — no partial state recovery
- Avoids stale data propagation
- Acceptable cost given <6 hour total job target
- Step Functions handles retry at the workflow level

### 3.5 Progress Reporting

**Architecture:**

- Agents publish progress events to EventBridge at each mini-step boundary
- EventBridge triggers Lambda function
- Lambda sends updates via API Gateway WebSocket
- Real-time client notifications

**Progress Granularity:**

- Mini-step level updates per agent (e.g., "collector: collect_schema started")
- Each event includes: `job_id`, `agent_name`, `mini_step`, `status`, `timestamp`, `metadata`
- Users can override triage and request full analysis via API parameter

---

## 4. Data Flow and Storage

**For detailed data flow diagram, see:**

- [Data Flow Diagram](diagrams/06-data-flow.md)
- [Storage Architecture](diagrams/07-storage-architecture.md)

### 4.1 Data Collection Flow

```
RDS Instance → Collector Agent → S3 (raw data)
    ↓
CloudWatch API → Collector Agent → S3 (metrics)
    ↓
Performance Insights → Collector Agent → S3 (query patterns)
    ↓
Collector Output (JSON) → S3 + Query Journey files (source)
    ↓
Step Functions → Referee-Triage → detects workload signals, selects engines
    ↓
Human Gate 1 → User reviews engine selection
    ↓
Step Functions RunAnalysis Map → Per-engine analysis agents → S3
    ↓
Step Functions → Assignment Resolution → query-to-engine mapping → S3
    ↓                                    + Query Journey files (assignment)
Step Functions → Reality Check → engine consolidation → S3
    ↓
Human Gate 2 → User reviews/overrides assignments
    ↓
Step Functions RunSchemaDesign Map → Per-engine: group split → schema design
    ↓                                (+ PE review) → group merge → S3
    ↓                                + Query Journey files (design)
Step Functions RunLoadTest Map → Per-engine: provision → seed → k6 execute
    ↓                            → parse results → teardown → S3
    ↓                            + Query Journey files (load_test)
Step Functions → Referee-Synthesis → weighted ranking → S3
    ↓
Deeper analysis loop (max 2 iterations) if needed
    ↓
Modernization Report → S3 → User
```

### 4.2 Storage Architecture

**S3 Storage (per [ADR-016](decisions/ADR-016-compute-and-orchestration-strategy.md)):**

Path convention: `<database-name>/<job-id (KSUID)>/<agent-name>/artifact.json`

- Collector outputs: `<db-name>/<ksuid>/collector/output.json`
- Triage decisions: `<db-name>/<ksuid>/referee-triage/triage.json`
- Analysis outputs: `<db-name>/<ksuid>/analysis-<type>/analysis.json`
- Assignment versions: `<db-name>/<ksuid>/assignment/v{N}/assignment.json`
- Reality check: `<db-name>/<ksuid>/reality-check/reality_check.json`
- Schema designs: `<db-name>/<ksuid>/schema-<target>/v1/schema_output.json`
- Design traces: `<db-name>/<ksuid>/schema-<target>/v1/design_trace.json`
- Load test results: `<db-name>/<ksuid>/load-test-<engine>/v{N}/results/summary.json`
- Load test scripts: `<db-name>/<ksuid>/load-test-<engine>/v{N}/scripts/` (customer deliverable)
- Load test per-pattern: `<db-name>/<ksuid>/load-test-<engine>/v{N}/results/{query_id}.json`
- Query journeys: `<db-name>/<ksuid>/query-journeys/{query_id}.json`
- Synthesis report: `<db-name>/<ksuid>/referee-synthesis/report.json`
- Uploads (offline): `<db-name>/<ksuid>/uploads/collector-output.json`

KSUID provides time-ordered, globally unique job IDs. The `<database-name>` prefix enables browsing and lifecycle policies per source database.

**DynamoDB Storage:**

- Job metadata: job_id, status, timestamps
- Progress tracking: current stage, percent complete
- Phase progression: `collect_triage → analysis → assignment → reality_check → assignment_review → schema_design → load_test → synthesis`
- Human gate state: task token for `waitForTaskToken`, assignment version, approval status
- User information: requester, configuration

**EventBridge:**

- Event bus for progress notifications (not orchestration)
- Agent mini-step events forwarded to WebSocket via Lambda
- Step Functions execution status change events (optional SNS)

### 4.3 Data Retention

**Analysis Results:**

- Stored in S3 with configurable lifecycle policy
- Default: 90-day retention
- Customer configurable (30-365 days)

**Temporary Data:**

- Checkpoints deleted after job completion
- EventBridge events archived (configurable retention)
- No PII stored in checkpoints

**Load Test Infrastructure:**

- All provisioned target resources (tables, indexes) torn down after test completion
- Only results and scripts persist in S3

---

## 5. API Architecture

### 5.1 API Design

**API Server:**

- FastAPI application running on ECS Fargate
- Shared Application Load Balancer with UI (path-based routing)
- ALB routes: `/api/*` → API target group, `/*` → UI target group
- REST endpoints for job management
- WebSocket endpoint for real-time progress updates (mini-step granularity)
- Starts Step Functions execution on job submission (`states:StartExecution`)
- No CORS middleware needed (UI and API share the same origin)

**Why ALB + ECS Fargate (not API Gateway):**

- Simpler architecture for Phase 0
- FastAPI runs identically locally and in AWS
- Easier debugging and development
- Consistent with other ECS services
- Can add API Gateway in Phase 1 if needed

### 5.2 API Endpoints

**Canonical API Reference:** The full OpenAPI specification is auto-generated on every commit and available at [`docs/architecture/openapi.json`](openapi.json). The listing below is a summary — always defer to the OpenAPI spec for exact request/response schemas, query parameters, and error codes.

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
- `GET /api/v1/assessments/{job_id}/execution-history` - Full Step Functions execution history
- `GET /api/v1/assessments/{job_id}/logs` - CloudWatch logs (filterable by agent)
- `GET /api/v1/assessments/{job_id}/results/table-mappings` - Paginated table mappings from synthesis
- `GET /api/v1/assessments/{job_id}/schema-designs` - Generated schema designs
- `GET /api/v1/dashboard/stats` - Dashboard statistics
- `GET /api/v1/settings` - Application settings

**WebSocket:**

- `wss://{alb-url}/ws/assessments/{job_id}` - Real-time progress updates

### 5.3 Authentication and Authorization

**Authentication (Cognito - Required):**

- Amazon Cognito User Pools for user authentication
- Username/password or federated identity (SAML, OIDC)
- Multi-factor authentication (MFA) support
- JWT tokens for API access

**Authorization:**

- IAM roles for service-to-service communication
- Cognito groups for role-based access control (RBAC)
- Fine-grained permissions per user/group

**API Security:**

- ALB Cognito authentication on all listener rules (except `/health`)
- Single Cognito session cookie shared between UI and API (same ALB)
- API endpoints protected by authentication
- WebSocket connections require valid JWT

---

## 6. Technology Stack

### 6.1 Core Technologies

| Component         | Technology               | Version | Justification                                                    |
| ----------------- | ------------------------ | ------- | ---------------------------------------------------------------- |
| Backend Language  | Python                   | 3.12+   | Rich ecosystem, database drivers, AI/ML libraries                |
| API Framework     | FastAPI                  | Latest  | Modern, async, auto-generated docs, WebSocket support            |
| Agent Framework   | Strands SDK              | Latest  | Open-source agentic framework, tool management                   |
| Load Testing      | k6                       | Latest  | Built-in rate control, percentiles, AWS jslib for DynamoDB       |
| Task Queue        | Amazon EventBridge       | -       | Progress notifications, agent status events                      |
| Job Orchestration | AWS Step Functions       | -       | Workflow coordination, parallel-with-join, conditional branching |
| Frontend          | React + TypeScript       | 18+     | Component reusability, type safety                               |
| UI Library        | Cloudscape Design System | Latest  | AWS native, professional appearance                              |
| Container Runtime | Docker                   | Latest  | Standard containerization                                        |
| Orchestration     | ECS Fargate              | -       | Serverless containers, auto-scaling                              |
| AI/ML             | AWS Bedrock              | -       | Managed LLM access, Claude 3 Sonnet                              |

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

**Compute:**

- ECS Fargate for container execution
- Lambda for event processing (progress updates)

**Storage:**

- S3 for object storage (outputs, reports, query journeys)
- DynamoDB for metadata and job tracking

**Messaging:**

- EventBridge for progress notifications and agent status events
- Step Functions for job workflow orchestration
- SNS for notifications (optional)

**Networking:**

- VPC for network isolation
- ALB for load balancing and HTTPS
- Security Groups for access control

**Security:**

- IAM for authentication and authorization
- Cognito for user authentication
- Secrets Manager for credential storage
- KMS for encryption at rest

**AI/ML:**

- Bedrock for LLM access

**Monitoring:**

- CloudWatch for logs and metrics
- OpenTelemetry for distributed tracing and observability

---

## 7. Security Architecture

### 7.1 Security Overview

Database Modernizer analyzes existing RDS instances in customer environments. Security focuses on safe handling of temporary credentials and analysis results.

**Shared Responsibility Model:**

**Customer Responsibilities:**

- RDS instance security (encryption, backups, patching)
- Credential management (Secrets Manager, IAM)
- VPC configuration and network security
- CloudTrail logging for audit

**Database Modernizer Responsibilities:**

- Secure credential handling during job execution
- SSL/TLS for database connections
- Data retention policy for analysis results
- IAM permissions for ECS tasks
- Load test resource isolation (scoped IAM, tagged resources, automatic teardown)

### 7.2 Credential Handling

**Principles:**

- Credentials handled in memory only during job execution
- Never persisted to disk or logs
- Cleared immediately after job completion
- SSL/TLS required for all database connections

**Implementation:**

- Credentials retrieved from Secrets Manager at runtime
- Stored in memory only (Python variables)
- Connection objects closed and cleared after use
- No credential logging or debugging output

### 7.3 IAM Permissions

**ECS Task Role (General Agents):**

- `rds:DescribeDBInstances`, `rds:DescribeDBClusters` - Read RDS metadata
- `cloudwatch:GetMetricStatistics` - Read CloudWatch metrics
- `pi:GetResourceMetrics` - Read Performance Insights data
- `bedrock:InvokeModel` - Invoke Bedrock models
- `secretsmanager:GetSecretValue` - Retrieve database credentials
- `s3:PutObject`, `s3:GetObject` - Store/retrieve analysis results
- `dynamodb:PutItem`, `dynamodb:GetItem`, `dynamodb:UpdateItem` - Job metadata
- `events:PutEvents`, `events:PutRule`, `events:PutTargets` - EventBridge operations
- `states:StartExecution`, `states:DescribeExecution`, `states:SendTaskSuccess`, `states:SendTaskFailure` - Step Functions operations (including human gate callback)
- `cognito-idp:*` - Cognito user pool operations
- `logs:CreateLogStream`, `logs:PutLogEvents` - CloudWatch Logs

**Load Test Task Role (Scoped):**

- `dynamodb:CreateTable`, `dynamodb:DeleteTable`, `dynamodb:DescribeTable` - Provision/teardown (resource condition: `LoadTest_*`)
- `dynamodb:PutItem`, `dynamodb:GetItem`, `dynamodb:Query`, `dynamodb:BatchWriteItem`, `dynamodb:BatchGetItem`, `dynamodb:UpdateItem`, `dynamodb:DeleteItem` - Data operations (resource condition: `LoadTest_*`)
- `s3:PutObject`, `s3:GetObject` - Artifact storage
- `logs:CreateLogStream`, `logs:PutLogEvents` - CloudWatch Logs

### 7.4 Network Security

**VPC Configuration:**

- ECS tasks run in private subnets
- Security groups restrict inbound/outbound traffic
- No public internet access required (VPC endpoints for AWS services)

**SSL/TLS Requirements:**

- All database connections use SSL/TLS
- Certificate verification enabled by default
- Self-signed certificates supported (opt-in)

**Data in Transit:**

- HTTPS for all API communication (ALB termination)
- TLS for database connections
- EventBridge events encrypted in transit

**Data at Rest:**

- S3 server-side encryption (SSE-S3 or SSE-KMS)
- DynamoDB encryption at rest
- EBS volumes encrypted

### 7.5 Data Retention

**Analysis Results:**

- Stored in S3 with configurable lifecycle policy
- Default: 90-day retention
- Customer configurable (30-365 days)
- Encrypted at rest using S3 default encryption

**Temporary Data:**

- Checkpoints stored in S3 during job execution
- Deleted after job completion (success or failure)
- No PII stored in checkpoints (only metadata)

**Load Test Resources:**

- All provisioned target infrastructure (tables, indexes) torn down automatically after test
- Tagged with `job_id` and `run_id` for cost attribution and orphan detection
- Table names prefixed with `LoadTest_` for IAM scoping and easy identification

---

## 8. Operational Considerations

### 8.1 Monitoring and Observability

**Key Metrics:**

- Job completion rate and duration
- Agent execution time per stage
- API response time and error rate
- Step Functions execution duration, failure rate, state transition count
- EventBridge event delivery latency
- ECS task CPU and memory utilization
- Load test pattern pass/fail rate and latency percentiles

**Logging:**

- Structured logging with JSON format (structlog)
- CloudWatch Logs for centralized log aggregation
- Log levels: DEBUG, INFO, WARNING, ERROR, CRITICAL
- Correlation IDs for request tracing

**Alerting:**

- CloudWatch Alarms for critical metrics
- SNS notifications for failures
- PagerDuty integration (optional)

### 8.2 Scaling

**Horizontal Scaling:**

- Step Functions Map state runs selected analysis agents in parallel
- Each agent runs in one ECS Fargate task with generous resources
- Agents spawn sub-processes internally for additional parallelism

**Task Sizing (Phase 0 defaults, per [ADR-016](decisions/ADR-016-compute-and-orchestration-strategy.md)):**

| Agent Type                   | vCPU | Memory |
| ---------------------------- | ---- | ------ |
| API Server                   | 0.5  | 1 GB   |
| Collector                    | 4    | 8 GB   |
| Referee-Triage               | 2    | 4 GB   |
| Analysis Agent               | 2    | 4 GB   |
| Assignment Resolution        | 2    | 4 GB   |
| Reality Check                | 2    | 4 GB   |
| Schema Design                | 4    | 8 GB   |
| Load Test (k6 + coordinator) | 4    | 16 GB  |
| Referee-Synthesis            | 2    | 4 GB   |

Right-size in Phase 1 after instrumenting 20+ real jobs.

### 8.3 Disaster Recovery

**Backup Strategy:**

- S3 versioning enabled for all data
- DynamoDB point-in-time recovery enabled
- Cross-region replication (optional)

**Recovery Procedures:**

- Restart failed step from scratch (per ADR-016 restart strategy)
- Step Functions automatic retry per state
- Restore from S3 versioned objects

### 8.4 Cost Optimization

**Cost Drivers:**

- ECS Fargate compute time
- Step Functions state transitions (~$0.025 per 1,000 — negligible)
- S3 storage and data transfer
- DynamoDB read/write capacity
- Bedrock API calls (token usage)
- Load test target infrastructure (DynamoDB on-demand capacity during test)

**Optimization Strategies:**

- Use Fargate Spot for non-critical workloads
- S3 lifecycle policies for old data
- DynamoDB on-demand pricing
- Efficient Bedrock prompts to minimize tokens
- Load tests use on-demand capacity and tear down immediately (cost: ~$0.003/operation)

---

## 9. CI/CD Pipeline

### 9.1 Overview

The project uses GitLab CI with a multi-stage pipeline that builds, tests, and deploys four Docker images across development and production environments.

### 9.2 Pipeline Stages

```
security → lint → test → deploy-shared → build → deploy-services →
integration-test → promote-prod → deploy-prod → smoke-test-prod →
build-feature → deploy-feature → cleanup
```

### 9.3 Docker Images

| Image             | Contents                       | Purpose                                            |
| ----------------- | ------------------------------ | -------------------------------------------------- |
| `api`             | FastAPI application            | API server                                         |
| `ui`              | React SPA (built + served)     | Frontend                                           |
| `agent`           | Python agents + Strands SDK    | All pipeline agents (collector through synthesis)  |
| `agent-load-test` | Python coordinator + k6 binary | Load testing (separate image due to k6 dependency) |

### 9.4 Testing Strategy

- **Unit tests**: pytest with parallel execution, coverage reporting, JUnit XML
- **UI tests**: React testing library
- **Contract validation**: `validate_contracts.py` and `validate_schemas.py` run on every change to `src/` or `do../contracts/`
- **Integration tests**: Run against live dev stack — gate production deployment
- **Smoke tests**: Health check against production ALB after deploy

### 9.5 Ephemeral Environments

Per [ADR-014](decisions/ADR-014-cicd-pipeline-and-ephemeral-environments.md), feature branches (`feat/`, `feature/`, `fix/`, `hotfix/`, `ci/`) get ephemeral CloudFormation stacks:

- `build-feature` builds images tagged with branch name
- `deploy-feature` creates isolated stack
- `cleanup` tears down stack when branch is merged/deleted

### 9.6 Deployment Strategy

- **Image promotion** (not rebuild): dev ECR images promoted to prod ECR via `skopeo copy`
- **CloudFormation**: Infrastructure-as-code for all environments
- **GitLab Runner**: Self-hosted on ECS with dedicated IAM role

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

---

**Document Status:** Approved
**Current Phase:** Phase 1 (Q3-Q4 2026)
**Next Review:** Phase 1 mid-point
