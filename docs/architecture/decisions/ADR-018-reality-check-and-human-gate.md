# ADR-018: Reality Check and Human Approval Gate

**Status:** Accepted
**Date:** 2026-04-24
**Deciders:** Database Modernizer Architecture Team

## Context

The original pipeline flow was: Collector → Triage → Analysis (parallel) → Schema Design (parallel) → Synthesis. This had two problems:

1. **Engine proliferation**: Analysis agents independently recommend engines for each query. Without a consolidation step, the pipeline often recommended 3-4 engines when 2 would suffice. Each additional engine adds ~$300-500/mo in operational overhead (monitoring, patching, expertise, on-call).

2. **No human oversight before schema generation**: Schema design is the most expensive pipeline step (LLM-heavy, 2-4 minutes per engine). Once schema design runs, rework is costly. Users had no opportunity to review or override engine assignments before committing to schema generation.

## Decision

### 1. Reality Check Agent (CTO-Level Engine Consolidation)

Add a deterministic post-processing step after assignment resolution that evaluates the assignment with a practical, cost-conscious lens:

- For each engine, assess whether it provides **unique capabilities** not available from other assigned engines
- If an engine can be eliminated, reassign its queries to the best surviving engine
- Output: `before_distribution`, `after_distribution`, `consolidations[]`, `architectural_patterns[]`, `recommendations[]`

**Why "Reality Check" and not just better triage?** Triage operates on collector signals alone (pre-analysis). The reality check operates on fully-scored analysis output — it knows actual confidence scores, anti-pattern counts, and cross-engine query overlap. This is fundamentally different information.

### 2. Human Approval Gate (Assignment Review Phase)

Add a `waitForTaskToken` pause point in the Step Functions workflow between reality check and schema design:

- Pipeline writes assignments + reality check output to S3, then pauses
- API exposes `GET /assignments`, `PUT /assignments` (override), `POST /resume` (approve)
- UI shows two views:
  - **CTO Summary**: Executive summary, before/after engine distribution, architectural patterns, one-click approval
  - **Advanced View**: Full query assignment table with filtering, sorting, and per-query engine override
- On approval, API calls `SendTaskSuccess` with the stored task token

**Why `waitForTaskToken` instead of polling?** Step Functions native callback pattern — no polling infrastructure, no state management, no timeouts to manage. The workflow simply pauses until the token is returned.

### 3. Signal-Driven Assignment Resolution

Add an assignment resolution step between analysis and reality check:

- Maps each query to its best-fit engine based on analysis confidence scores
- Applies **signal-driven overrides**: triage signals (e.g., `text_search`) can override raw confidence scores when a signal strongly indicates a specific engine
- Applies **anti-pattern penalties**: penalizes engines when queries hit known anti-patterns (e.g., full table scans on DynamoDB, complex joins on DynamoDB)
- Resolves multi-engine tables using majority-engine heuristic

### 4. Group Splitting for Schema Design

For large workloads (20+ tables per engine), split into groups before schema design:

- Uses analysis signals for intelligent clustering (co-accessed tables stay together)
- MAX_GROUP_SIZE = 20 tables (empirically determined — balances LLM context limits with design quality)
- Groups processed in parallel, then merged into a unified schema per engine

## Updated Pipeline Flow

```
Collector → Triage → Analysis (parallel per engine)
  → Assignment Resolution → Reality Check
  → Human Approval Gate (waitForTaskToken)
  → Schema Design (parallel per engine, with group split/merge)
  → Synthesis
```

**Phase progression**: `collect_triage → analysis → assignment → reality_check → assignment_review → schema_design → synthesis`

## Consequences

### Positive

- Engine count reduced by ~30% on average (3 engines → 2 in WordPress pilot)
- Human oversight before the most expensive pipeline step
- Users can exclude tables from scope (e.g., config tables not worth migrating)
- Users can override engine assignments based on organizational constraints
- Group splitting enables the pipeline to handle 300+ table databases without hitting LLM context limits

### Negative

- Pipeline duration increases by ~2 minutes for assignment + reality check
- Human gate adds unbounded wait time (mitigated: users can auto-approve)
- Group splitting introduces merge complexity (table deduplication, index consolidation)

### Risks

- Reality check may be too aggressive in eliminating engines — mitigated by human gate allowing override
- Group boundaries may split related tables — mitigated by signal-based clustering heuristic

## Alternatives Considered

1. **Better triage instead of reality check**: Rejected — triage lacks analysis confidence scores and cross-engine overlap data
2. **Automatic approval with email notification**: Rejected — schema design is too expensive to run speculatively
3. **Human gate before analysis**: Rejected — users need analysis results to make informed decisions
4. **Fixed group sizes without signal clustering**: Rejected — led to poor schema quality when related tables were split across groups
