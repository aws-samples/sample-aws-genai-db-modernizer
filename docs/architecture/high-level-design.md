# Database Modernizer Assessment - High-Level Design (HLD)

## Document Information

**Version:** 13.0 (query-first routing, one approval gate)
**Date:** October 27, 2026
**Status:** Approved
**Owner:** Database Modernizer Assessment Engineering Team

---

## Executive Summary

Customers modernizing off monolithic relational databases ask us the same question: **which purpose-built database should I choose?** Should this workload go to DynamoDB? Does it need a document store like DocumentDB? Would a cache layer in ElastiCache solve the problem? Should full-text search move to OpenSearch? Or does the query pattern actually belong in Aurora?

Getting the answer wrong means failed modernizations, re-architecture mid-project, and wasted months. Getting it right requires deep analysis of every query pattern, understanding access patterns at scale, and mapping them to the right engine — a process that traditionally takes weeks of specialist time per database.

**Database Modernizer Assessment answers that question automatically.** It analyzes every query pattern in your relational database, scores each one against AWS purpose-built engines, validates the overall architecture for operational complexity, and produces ready-to-implement schema designs with load-tested performance data. You can run it from your laptop, from the command line or with Claude Code.

The core pipeline is **fully deterministic** — pattern detection, scoring, assignment, and consolidation all run without any LLM dependency. GenAI enhances the pipeline at key decision points (analysis advisors, consolidation validation, executive summaries) but is never required. You get reproducible, auditable results every time, with AI refinement layered on top when available.

**Supported sources:** PostgreSQL, MySQL, MariaDB, SQL Server and Oracle, through the read-only collection scripts `scripts/collect-*.sql`. There is no Redis source.

**Target engines:** DynamoDB, DocumentDB, OpenSearch (a search read model), ElastiCache (Valkey; a cache that owns no query), Aurora PostgreSQL and Aurora MySQL. Neptune and Keyspaces are catalog placeholders that are never analysed, designed or routed ([ADR-031](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/docs/architecture/decisions/ADR-031-query-first-routing-and-engine-rule-catalog.md) Decision 9).

### Key Design Decisions

- **Local-first deployment**: Runs entirely on the user's machine — via Claude Code (`/modernize`), the deterministic CLI, or a local FastAPI + React UI — against RDS instances the user already has access to. Offline and DDL collection need no database connection at all; live collection runs through an opt-in, not-yet-fully-wired SSM automation path (§7.1), not a direct connection. The hosted, AWS-account-deployed version of this tool was retired; see [#175](https://github.com/aws-samples/sample-aws-genai-db-modernizer/issues/175)
- **Deterministic core**: Full pipeline runs without LLM calls (`--llm-mode none`); GenAI is an enhancement layer, not a dependency
- **Strands SDK where a model is involved**: the model-backed steps (schema design and its reviewer, the reality-check review, the executive summary, the analysis advisor) build Strands agents on Bedrock; the same steps run through Claude Code with `--llm-mode external`. Collection, triage, analysis, assignment and reality-check scoring never touch Strands
- **Phased pipeline**: Collect, Triage, Analysis (one deterministic analysis per selected engine), Assignment, Reality Check, the assignment review gate, Schema Design, Load Test (optional) and Synthesis, in the order of the `Phase` enum
- **Query-first routing**: the query is the unit. One scorer (`query_scoring.score_query_for_engine`) reads the rule catalog in `src/catalog/`; a table has one writer; every engine change is a Move recorded in the move ledger, and only `move_ledger.apply` writes `assigned_engine` ([ADR-031](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/docs/architecture/decisions/ADR-031-query-first-routing-and-engine-rule-catalog.md))
- **Local orchestration**: a single `LocalOrchestrator` runs phases as direct function calls against the local artifact store, with no job-scheduling service, message bus, or hosted workflow engine
- **One human gate**: the pipeline pauses once, after the reality check, for the assignment review ([ADR-028 (customer assignment review gate)](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/docs/architecture/decisions/ADR-028-customer-assignment-review-gate.md)); the user resumes it through the UI, the API or the Claude Code command. Nothing waits for approval after triage
- **Engine scope**: the customer can limit the engines a plan may use (`--target-databases`); the scope is recorded under `decisions/engine_scope.json`, triage skips engines outside it, and a query no in-scope engine can serve has no engine
- **Reality check consolidation**: CTO-level engine consolidation removes engines that do not earn their cost, reducing operational complexity, and replays from the move ledger
- **One facts layer**: synthesis computes every customer-facing figure once into `report.json`'s `facts`, and the renderers (engineering Markdown, executive report PDF, the UI) only format them ([ADR-032](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/docs/architecture/decisions/ADR-032-one-facts-layer-renderers-only-format.md))
- **A library entry point**: `assess()` (`src/assessment/`) runs the phases in process up to a chosen `run_through` and returns a typed, versioned result ([contracts/assess.md](../contracts/assess.md))
- **Load testing validation**: k6 empirical performance testing against real target infrastructure with per-pattern latency and cost measurement ([ADR-020](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/docs/architecture/decisions/ADR-020-load-testing-stage.md))
- **Query journeys from the context graph**: per-query detail is served from the context graph read-model ([ADR-023](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/docs/architecture/decisions/ADR-023-context-graph-layer.md))
- **Deterministic triage**: triage selects every query-owning engine; its signals are evidence for scoring and reports, not a filter. Only ElastiCache, which owns no query, depends on a signal
- **AWS Transform integration**: `src/atx_orchestrator/` lets AWS Transform run the same phases over S3-backed storage, independent of the local-only API/UI path
- **Bedrock integration**: AWS Bedrock for the model-backed steps (optional)
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
3. Pipeline Design
4. Data Flow and Storage
5. API Architecture
6. Technology Stack
7. Security Architecture
8. Operational Considerations
9. CI/CD Pipeline

---

## 1. System Architecture Overview

### 1.1 High-Level Architecture

**For diagrams, see:**

- [System Context Diagram](diagrams/01-system-context.md)
- [Pipeline and Data Flow](diagrams/06-data-flow.md)

### Architecture Components

**Application Layer:**

- Web UI (React SPA, local build): the results page and the deliverables download panel ([Understand the results](../understand-the-results.md))
- API Server (FastAPI, run locally with `uvicorn`): REST endpoints for the UI, loopback-only by default (see [`src/api/host_guard.py`](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/src/api/host_guard.py))
- Orchestrator (`LocalOrchestrator`): runs phases as direct function calls against the local artifact store, with no job-scheduling service or message bus
- Library (`assess()`): the same phases in process, returning a typed result

**Pipeline phases (`src/agents/`):**

- Collect: the offline parser builds the `CollectorOutputContract` from a script's output (MySQL, MariaDB, PostgreSQL, SQL Server, Oracle); DDL and live modes are the other inputs
- Referee-Triage: deterministic; selects the engines to analyse and records the signals as evidence
- Analysis: one per selected engine (DynamoDB, DocumentDB, OpenSearch, ElastiCache, Aurora PostgreSQL, Aurora MySQL), deterministic, with an optional model advisor
- Assignment: maps each query to its best-fit engine with the one scorer, write groups and tie-break; every change is a Move
- Reality Check: CTO-level engine consolidation, replayed from the move ledger, with a model review of the consolidation
- Assignment review: the one human gate
- Schema Design: one designer per assigned engine (including both Aurora engines), with group splitting and a reviewer loop
- Load Testing: engine-specific provisioners, seeders and k6 script generators (DynamoDB, DocumentDB, ElastiCache and OpenSearch)
- Referee-Synthesis: the weighted ranking, TCO, risk, migration waves and executive summary, and the customer deliverables

**Cross-Cutting Concerns:**

- The rule catalog (`src/catalog/`): engines, patterns, gates, thresholds and prices as TOML; the generated [rule-catalog.md](rule-catalog.md)
- The context graph: per-query detail served from a read-model ([ADR-023](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/docs/architecture/decisions/ADR-023-context-graph-layer.md))

**Data Storage Layer:**

- Local filesystem (`LocalArtifactStore`, default `./artifacts`): collector outputs, reports, job artifacts, query journeys (path: `<database-name>/<job-id>/<agent-name>/artifact.json`)
- S3 (`S3ArtifactStore`, used only by the AWS Transform integration — `S3_BUCKET` env var): same path convention, for ATX-orchestrated jobs
- Local job metadata file (`.meta/<job-id>.json` at the artifact store root): phase progression, status tracking

**Source Databases (Customer-Owned, Read-Only Access):**

- PostgreSQL, MySQL, MariaDB, SQL Server and Oracle, read by the collection scripts the user runs (or, in the opt-in live mode, by SSM Run Command on an automation instance). Db2 is an enum value with no script
- CloudWatch metrics and Performance Insights read via the AWS APIs, only in live mode
- Database Modernizer Assessment does NOT deploy its own database instances

**AWS Services (used directly by the user's own credentials, not a hosted role):**

- CloudWatch API, Performance Insights API, RDS API, Secrets Manager — only for live (not offline/DDL) collection against a real database
- Bedrock API for AI-powered analysis (optional)

### System Context

**Primary Users:**

- Database Architects: run the tool locally or via Claude Code, analyze RDS workloads, review recommendations
- IT Leaders: Review analysis reports, approve migrations
- Developers: Implement schema designs, execute migrations

**External Systems:**

- Source databases (read-only access, live mode only)
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
[`example-ci-runner-iam.yaml`](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/infrastructure/cloudformation/example-ci-runner-iam.yaml),
a sample for anyone standing up their own CI runner role, and
[`automation.yaml`](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/infrastructure/cloudformation/automation.yaml),
which provisions a per-VPC SSM automation instance for the opt-in live
collection path described in §2.2 and §7.1 (kept per a maintainer decision —
see [#342](https://github.com/aws-samples/sample-aws-genai-db-modernizer/issues/342)
for what's still missing to wire it end-to-end). `pipeline/iam-roles.yaml`
also remains — it is unrelated to the hosted deployment and provisions IAM
roles for the AWS Transform integration (§2.4).

### 2.1 Primary Mode: Claude Code

- `/modernize` drives the full pipeline from a Claude Code session, with
  the assignment-review gate; the default experience is chat and the local UI
  together, `--mode chat` is chat only, and `--mode ui` is UI-first (chat
  stays to the approval-gate numbers, UI pointers, and the result line)
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

## 3. Pipeline Design

**For diagrams, see:** [Pipeline and Data Flow](diagrams/06-data-flow.md).

### 3.1 Phases

The phases are the `Phase` enum in `src/contracts/phase_models.py`, run in this order by `LocalOrchestrator`: `collect_triage`, `analysis`, `assignment`, `reality_check`, `assignment_review`, `schema_design`, `load_test`, `synthesis`.

**1. Collect and Triage**

- The user runs a read-only script (`scripts/collect-postgresql.sql`, `collect-mysql.sql`, `collect-sqlserver.sql`, `collect-oracle.sql`) against their own database and gives the tool the JSON it writes. `parse_offline_collection` and `build_offline_collector_output` turn it into a `CollectorOutputContract` (sources: PostgreSQL, MySQL, MariaDB, SQL Server, Oracle). DDL parsing and the opt-in live mode (SQL run via SSM Run Command on a per-VPC automation instance, §7.1) are the other inputs. No collector reads table rows (#387)
- Referee-Triage is deterministic (`src/agents/referee/triage.py`): it detects workload signals (key-value lookups, text search, write-heavy, aggregations and so on), each naming the engines it is evidence for and the `query_ids` and `table_ids` that triggered it. It selects every query-owning engine; signals are evidence, not a filter, and only ElastiCache, which owns no query, runs when one of its signals fires. Engines outside the customer's engine scope are skipped with a stated reason. Neptune and Keyspaces are placeholders and are never selected

**2. Analysis**

- One deterministic analysis per selected engine (DynamoDB, DocumentDB, OpenSearch, ElastiCache, Aurora PostgreSQL, Aurora MySQL), run concurrently as local function calls. Each writes `analysis.json` and a decision trace. An optional model advisor comments on unmatched queries (`--llm-mode bedrock`)
- The patterns, scores, penalties, weights and prices come from the rule catalog, not from per-engine code ([ADR-031](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/docs/architecture/decisions/ADR-031-query-first-routing-and-engine-rule-catalog.md) Decision 8; [rule-catalog.md](rule-catalog.md))

**3. Assignment (query-to-engine mapping)**

- The query is the unit. The one scorer, `query_scoring.score_query_for_engine`, scores each query on each engine (pattern, complexity, performance and cost); a query's pattern score is its access-pattern class's score on the engine, not its tables'
- A table has one writer ([ADR-031](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/docs/architecture/decisions/ADR-031-query-first-routing-and-engine-rule-catalog.md) Amendment 9): its writes and locking reads form a write group placed as one unit, with any co-dependency group it touches. Its reads stay per query
- Exact ties go through `tie_break.break_tie`, using settled counts only. No engine is preferred by name
- Every engine change is a Move: only `move_ledger.apply` and `apply_to_assignment` write `assigned_engine`, and each Move records its pass, rule, scores and runner-up. The result is a versioned artifact, `assignment/v<N>/assignment.json`, and each query's access-pattern record, `access_patterns.json`

**4. Reality Check (CTO-level engine consolidation)**

- Asks, per engine, whether it earns its place given its standing cost, its operational weight and whether another engine already in the architecture serves its queries well enough. Default posture: at most two committed engines, and a third needs a capability no other committed engine has
- Steps: Settle, Place, Justification floors, Records (derived from the move ledger) and Architectural patterns. A moved query goes to its highest-scoring remaining engine, using the same per-query scores as the first assignment
- A model reviews the consolidation (can the target engine really serve the moved queries?) and flags, never chooses a destination. It writes a new assignment version and `reality-check/output.json`

**5. Assignment review (the one human gate)**

- The pipeline pauses once. The customer reviews the plan, edits the engine of any query or takes a query out of scope, and approves; a feasibility review can block an unworkable edit ([ADR-028 (customer assignment review gate)](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/docs/architecture/decisions/ADR-028-customer-assignment-review-gate.md), [ADR-029](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/docs/architecture/decisions/ADR-029-assignment-reentry-and-feasibility-review.md)). A customer edit writes a new assignment version and never overwrites one
- Resumption happens through the UI/API (`POST /resume`), the deterministic CLI (`run_assessment.py --resume-reality-check`), or the matching Claude Code command

**6. Schema Design (target schema generation)**

- One designer per assigned engine: DynamoDB, DocumentDB, OpenSearch, ElastiCache, Aurora PostgreSQL and Aurora MySQL. Schema design always uses a model, so `--llm-mode none` skips it with a stated reason
- **Group splitting:** DynamoDB always splits into groups of about 20 queries (co-accessed tables stay together), designs them independently and merges them. The pipeline runs three stages locally: split, design (per group), merge
- A reviewer loop (capped at `MAX_PE_ITERATIONS = 2`) validates each design, and a design trace (`design_trace.json`) is written

**7. Load Testing (optional empirical validation)**

- An engine-agnostic coordinator provisions, seeds, generates scripts, dry-runs, executes, parses and tears down. It uses k6, run locally (`scripts/run_load_test.py`) or inside the `agent-load-test` image; abstract base classes (`BaseProvisioner`, `BaseSeeder`, `BaseScriptGenerator`, `BaseRunner`) make an engine pluggable
- Implemented for DynamoDB, DocumentDB, ElastiCache and OpenSearch ([load-testing guide](../guides/load-testing-new-engine.md)). The generated scripts are also a customer deliverable. Non-blocking: on failure the pipeline continues to synthesis
- Per [ADR-020](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/docs/architecture/decisions/ADR-020-load-testing-stage.md)

**8. Synthesis and deliverables**

- Deterministic: the weighted ranking, table mappings, TCO, risk assessment, migration waves and the `facts` layer, written to `synthesis/v<N>/report.json`. A model writes only the executive summary, which is checked against the facts; with no model, a deterministic summary is kept
- Renderers only format the facts. The latest version's customer folder, `deliverables/`, holds the executive report PDF, the engineering report, the detailed engineering report, `assessment-data.json` and `index.md` (the list is `FOLDER_ENTRIES` in `src/report/deliverables.py`)

**Query journeys (cross-cutting).** The per-query story (source, assignment, design, load test) is a read-model in the LadybugDB context graph, served by the query-journeys API; see [ADR-023](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/docs/architecture/decisions/ADR-023-context-graph-layer.md) and the [graph cookbook](../guides/context-graph-query-cookbook.md).

### 3.2 Strands SDK

**Strands SDK** is the open-source agentic framework used where a model is involved: the schema designers and reviewers, the reality-check review, the executive summary and the analysis advisor. Collection, triage, assignment and the reality-check scoring are plain Python and never import it ([strands-agent-development-guide.md](../guides/strands-agent-development-guide.md)). Every model-backed phase follows the LLM seam pattern (`run_*_deterministic`, `prepare_*_llm_input`, `apply_*_llm_output`), so the same code serves Bedrock, Claude Code and deterministic runs. Contracts (Pydantic models) enforce the structure of every input and output.

### 3.3 Orchestration Pattern

**Local orchestration.** A prior version of this project used cloud orchestration (Step Functions, EventBridge and ECS tasks per agent; see [ADR-016](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/docs/architecture/decisions/ADR-016-compute-and-orchestration-strategy.md), partly superseded). `LocalOrchestrator` (`src/orchestrator/`) replaces it with direct, in-process function calls against the local artifact store:

```text
Collect and Triage -> Analysis (per engine, concurrent) -> Assignment -> Reality Check
  -> [assignment review: the one human gate] -> Schema Design (per engine, concurrent)
  -> Load Test (optional) -> Synthesis
```

- Each phase is a direct Python function call. Analysis and schema design run per engine concurrently through a `ThreadPoolExecutor`; load testing runs per engine sequentially
- There is **one** human approval gate, after the reality check. Resumption happens through the UI/API, the CLI or the Claude Code command; there is no task-token callback to a workflow service
- Progress is tracked in a local phase-progression file (`.meta/<job_id>.json`), not in DynamoDB or EventBridge. The API's `/execution-history` and `/agents` endpoints derive status from the artifact layout (`LocalExecutionService`)
- The `AWS Transform` integration drives the same phases as a four-agent fleet over A2A ([guide](../guides/aws-transform-integration.md))

### 3.4 Restart Strategy

**Restart-from-scratch per step.** There is no intra-step checkpointing above the collector. If a step fails, it restarts from scratch, which keeps agent code simple: no partial state recovery, and no stale data propagating. `LocalOrchestrator` does not retry automatically; a failed phase stays failed until the user re-runs it (CLI, API, or Claude Code command). A revision or customer edit writes a new version of the downstream artifacts (§4.2).

### 3.5 Progress Reporting

- The UI/API polls `GET /api/v1/assessments/{job_id}` and `/execution-history`, which derive progress from which artifact directories and files exist on disk (`LocalExecutionService`). There is no event bus or push channel
- Claude Code sessions report progress directly as part of the conversation
- Updates are stage-level (collect, triage, per-engine analysis, and so on), derived from artifact presence

---

## 4. Data Flow and Storage

**For the data flow diagram, see:** [Pipeline and Data Flow](diagrams/06-data-flow.md).

### 4.1 Data Collection Flow

```text
Source database --(user runs scripts/collect-*.sql)--> collection JSON
    |
    v
Collect: offline parser -> CollectorOutputContract -> collector/output.json
    |
    v
Triage -> referee-triage/triage.json (engines selected, signals as evidence)
    |
    v
Analysis (per engine, concurrent) -> analysis-<engine>/analysis.json + decision-trace.json
    |
    v
Assignment -> assignment/v<N>/assignment.json (+ access_patterns.json); every change a Move
    |
    v
Reality Check -> reality-check/output.json and a new assignment version
    |
    v
Assignment review (the one human gate) -> decisions/
    |
    v
Schema Design (per engine: split -> design + review -> merge) -> schema-<engine>/v<N>/
    |
    v
Load Test (optional) -> load-test-<engine>/v<N>/
    |
    v
Synthesis -> synthesis/v<N>/report.json -> deliverables/
```

(The AWS Transform integration runs the same flow against `S3ArtifactStore` instead of `LocalArtifactStore`, orchestrated over A2A rather than `LocalOrchestrator`; see [§2.4](#24-aws-transform-integration).)

### 4.2 Storage Architecture

**Artifact Storage (`ArtifactStore` abstraction, `src/storage/`):**

`LocalArtifactStore` (default) writes to the local filesystem; `S3ArtifactStore` (used only by the AWS Transform integration) writes to S3. Both use the same path convention: `<database-name>/<job-id>/<agent-name>/<file>`. The layout, with the files each phase writes, is in the [storage guide](../guides/storage-architecture-guide.md); every JSON file a job writes, by kind, is in [contracts/artifacts.md](../contracts/artifacts.md). In short:

- `collector/output.json`, `referee-triage/triage.json`, `analysis-<engine>/analysis.json`
- `assignment/v{N}/assignment.json` and `access_patterns.json`, `reality-check/output.json`, `decisions/`
- `schema-<engine>/v{N}/schema_output.json` and `design_trace.json`
- `load-test-<engine>/v{N}/` (results, scripts as a customer deliverable)
- `synthesis/v{N}/report.json` (`referee-synthesis/report.json` only for assignment version 0)
- `deliverables/`: the customer folder for the latest version
- `uploads/`: the raw collection

Job IDs are UUIDs: the API uses a full `uuid.uuid4()`; the local scripts and skills (`run_assessment.py`, `run_collect.py`) truncate to the first 8 hex characters for shorter paths. Neither generates a KSUID. The `<database-name>` prefix enables browsing and lifecycle management per source database.

**Local Job Metadata:**

- Stored in a per-job progression file under the artifact store (not a separate database)
- Progress tracking: derived from which artifact files exist, or recorded explicitly for phase status
- Phase progression: `collect_triage → analysis → assignment → reality_check → assignment_review → schema_design → load_test → synthesis`
- Gate state: phase status, assignment version, approval status (recorded under `decisions/`)
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

- FastAPI application, run locally with `uv run uvicorn src.api.main:app` (see [README.md](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/README.md))
- Loopback-only by default — `HostAllowlistMiddleware` (`src/api/host_guard.py`) rejects requests not addressed to a loopback host name; `MODERNIZER_ALLOWED_HOSTS` overrides it
- REST endpoints for job management; the UI is served separately (`npm start` with hot reload, or a local build and `npm run serve`)
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

- `POST /api/v1/assessments/prepare` - Pre-create a job ID and an upload URL (offline upload flow; the local file service)
- `POST /api/v1/assessments` - Start new assessment (live or offline mode)
- `GET /api/v1/assessments/{job_id}` - Get job status and progress
- `GET /api/v1/assessments/{job_id}/phases` - Get full phase progression (`PhaseProgression` contract)
- `GET /api/v1/assessments/{job_id}/results` - Get synthesis report
- `GET /api/v1/assessments` - List all assessments
- `DELETE /api/v1/assessments/{job_id}` - Cancel assessment
- `DELETE /api/v1/assessments/{job_id}/artifacts` - Delete a job's artifacts

**REST API — Upload Flow (Offline Mode):**

- `POST /api/v1/assessments/{job_id}/uploads/confirm` - Confirm the upload completed
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

**REST API — Deliverables:**

- `GET /api/v1/assessments/{job_id}/deliverables` - The deliverables folder's entries
- `GET /api/v1/assessments/{job_id}/deliverables/{filename}` - One deliverable

**REST API — Query Journeys and the context graph:**

- `GET /api/v1/assessments/{job_id}/query-journeys` - Paginated query journey list (default 50, max 200)
- `GET /api/v1/assessments/{job_id}/query-journeys/{query_id}` - Single query journey detail
- `GET /api/v1/assessments/{job_id}/graph/...` - Curated graph views (table impact, query provenance and trace, engine detail, risks), plus `POST .../graph/query` for read-only Cypher; see the [graph cookbook](../guides/context-graph-query-cookbook.md)
- `GET /api/v1/assessments/{job_id}/agents/{agent}/questions` and `PUT .../answers` - Agent interaction

**REST API — Observability & Results:**

- `GET /api/v1/assessments/{job_id}/agents` - Per-agent status with artifact summaries
- `GET /api/v1/assessments/{job_id}/execution-history` - Full per-stage history, derived from the artifact directory layout (`LocalExecutionService`), in the same shape the old Step Functions-backed endpoint returned
- `GET /api/v1/assessments/{job_id}/results/table-mappings` - Paginated table mappings from synthesis
- `GET /api/v1/assessments/{job_id}/schema-designs` - Generated schema designs
- `GET /api/v1/dashboard/stats` - Dashboard statistics

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
| Agent Framework   | Strands SDK              | Latest  | Open-source agentic framework for the model-backed steps         |
| Load Testing      | k6                       | Latest  | Built-in rate control, percentiles, AWS jslib for DynamoDB       |
| Job Orchestration | `LocalOrchestrator`      | -       | Direct function calls over the local artifact store, no cloud workflow service |
| Frontend          | React (JavaScript)       | 18+     | Component reusability                                            |
| UI Library        | Cloudscape Design System | Latest  | AWS native, professional appearance                               |
| Container Runtime | Docker                   | Latest  | Used for the `agent-load-test` image and the AWS Transform integration — not for a hosted service |
| AI/ML             | AWS Bedrock              | -       | Managed LLM access (optional)                                     |

### 6.2 Database Drivers

None. No pipeline module imports a database driver. The collection scripts run in the user's own client (`psql`, `mysql`, `sqlcmd`, `sqlplus`), and live mode runs SQL through SSM Run Command on the automation instance (`src/tools/aws/ssm_executor.py`). The only driver-like import is `redis` in the ElastiCache load-test script generator.

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

**Database connections:**

- The tool opens no direct connection to a source database. The collection scripts run in the user's own client under the user's own credentials, and live mode runs SQL on the automation instance through SSM Run Command (§7.1)

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
| `lint`                | commit messages and the lint tier (`./ci/lint.sh`: ruff, black, isort, mypy, command/skill reference validation, the generated-file checks) |
| `test-shard`, `test`  | unit, contract, property and graph tests (`./ci/test.sh`), sharded, then one gate job |
| `e2e-shard`, `e2e`    | the deterministic end-to-end pipeline run, HTML/PDF checks, UI smoke test (`./ci/e2e.sh`), sharded, then one gate job |
| `security`            | Gitleaks secret scan, Bandit, Semgrep, cfn-lint and Checkov on the surviving CloudFormation templates, Checkov on both Dockerfiles |
| `validate-contracts`  | Pydantic contract validation, the snapshot tests and the version-pin check |
| `ui-build`            | React unit tests (`npm run test:ci`) and a production build              |

Every job calls a CI-agnostic entry point in `ci/` rather than inlining commands, so a passing local run means a passing CI run ([ci/README.md](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/ci/README.md)). `main` has a merge queue that re-runs the required checks on top of the queue. Before pushing, `./ci/pre_merge.sh` fetches main, checks conflicts and contract versions, runs the branch's new tests against main's code, lints and tests, and prints whether a real-model run is needed (`ci/needs_llm_run.sh`). The headless-LLM suite (`ci/e2e-llm.sh`) runs on a mirror, not on every PR.

### 9.3 Testing Strategy

- **Unit tests**: pytest with parallel execution, coverage reporting
- **UI tests**: React testing library
- **Contract validation**: `validate_contracts.py` and the snapshot tests run on every change to `src/contracts`
- **End-to-end tests**: the deterministic pipeline rendered against both sample databases, with HTML/PDF/UI checks (`ci/e2e.sh`); a separate headless-LLM suite (`ci/e2e-llm.sh`) exercises a real `/modernize` run and is not gated on every PR

---

## Appendices

### Appendix A: Architecture Decision Records

The ADR index, [decisions/README.md](decisions/README.md), lists every ADR with its status (active, partly superseded, or superseded, retired or not implemented) and its successor. The routing model is [ADR-031](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/docs/architecture/decisions/ADR-031-query-first-routing-and-engine-rule-catalog.md); the facts layer is ADR-032; the one human gate is ADR-028 (customer assignment review gate).

### Appendix B: Related Documentation

**Architecture:**

- [Architecture Diagrams](diagrams/README.md) - The system context and the pipeline
- [Rule catalog](rule-catalog.md) - Engines, patterns, gates and thresholds (generated)
- [Adoption paths design](designs/adoption-paths.md) - Design for epic #1128

**Contracts:**

- [Agent Contracts Specification](../contracts/agent-contracts-spec.md) - Input/output contracts
- [Contract Schemas](../contracts/schemas/README.md) - JSON schemas for validation

**Data Specifications:**

- [Data specifications](../data-specs/README.md) and the [Database Collection Matrix](../data-specs/database-collection-matrix.md) - what the collection scripts gather

**Implementation:**

- [Implementation Guides](../guides/README.md) - Detailed implementation guidance
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
| 13.0    | 2026-10-27 | maintainers | **Documentation audit (#1600): rewrote sections 1, 3, 4 and 6.2 and the appendices around query-first routing (ADR-031), the move ledger, the engine scope, the facts layer (ADR-032), `assess()` and the one approval gate; fixed sections 5 and 9; removed the Redis, Db2, mini-collector, Neptune and Keyspaces claims and the deeper-analysis loop.** |

---

**Document Status:** Approved
**Current Phase:** Local-first, post-hosted-removal (#175)
**Next Review:** When the AWS Transform integration or local UI/API architecture materially changes
