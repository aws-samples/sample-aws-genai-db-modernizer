# ADR-016: Compute and Orchestration Strategy

**Status:** Accepted
**Date:** 2026-02-18
**Deciders:** Database Modernizer Assessment Architecture Team
**Related ADRs:** ADR-001 (State Management), ADR-003 (Progress Reporting), ADR-005 (Mini-Collectors), ADR-007 (Referee Orchestration)

---

## Context

The original HLD specified ECS Fargate for all compute and EventBridge as the sole orchestration layer. As we began implementing agents, four challenges emerged:

1. **Lambda Durable Functions** (re:Invent 2025) offer checkpoint/replay with async execution beyond 15 minutes, raising the question of whether ECS is still the right compute choice.

2. **EventBridge lacks coordination primitives** — no join/barrier ("wait for N agents"), no conditional branching, no job-level error aggregation. Building these requires custom Lambda + DynamoDB wiring.

3. **Mini-agent spawning** — collectors need to fan out mini-collectors for large databases. Who decides: the orchestrator or the agent?

4. **Analysis agent selection** — running all 7 analysis agents for every workload wastes compute. A key-value PostgreSQL database doesn't need Neptune or OpenSearch analysis. But giving an AI agent full workflow control creates auditability concerns.

---

## Decision

1. **ECS Fargate** for all agent compute (Phase 0)
2. **Step Functions** for job-level orchestration
3. **EventBridge** for notifications only (progress → WebSocket)
4. **Agents own their parallelism** — start with one big ECS task per agent; agents spawn their own sub-processes internally
5. **Generous default task sizes** — defer right-sizing to Phase 1
6. **Hybrid orchestration** — AI-driven triage selects which analyses to run; Step Functions executes them deterministically
7. **Restart from scratch per step** — no intra-step checkpointing; restarting a step cascades to all subsequent steps
8. **Agent-defined restart points** — each agent declares its mini-steps available for restart; restarting a previous mini-step invalidates all subsequent ones
9. **WebSocket status for everything** — all agent logic instrumented to report progress via EventBridge → WebSocket
10. **S3 naming convention** — `<database-name>/<job-id (KSUID)>/<agent-name>/artifact.json`

---

## Rationale

### Why ECS Fargate over Lambda Durable Functions

| Dimension | ECS Fargate | Lambda Durable Functions |
|-----------|-------------|--------------------------|
| VPC / DB connections | Held for entire run, no cold starts | Must reconnect at each step boundary |
| Resources | Up to 16 vCPU / 120 GB | Max 6 vCPU / 10 GB |
| Local dev parity | Docker Compose runs identical containers | No local equivalent for durable execution |
| Maturity | Production-proven, all regions | Dec 2025 launch, us-east-2 only, Python 3.13+ (we use 3.12) |
| Strands SDK | Runs natively as long-running process | Needs adaptation for checkpoint/replay |

**Re-evaluate when:** Python 3.12 support (or when dependencies support 3.13), 3+ regions, 6+ months track record.

### Why Step Functions over EventBridge-only

EventBridge routes events well but can't coordinate them. Step Functions provides native parallel-with-join, conditional branching, per-state retry, and `ecs:RunTask.sync` integration. Cost is ~$0.025 per 1,000 state transitions — negligible.

### Why one big ECS task per agent (not separate tasks per mini-collector)

Start simple. Each agent runs in a single ECS task with generous resources. If the agent needs parallelism (e.g., mini-collectors for 2000+ tables), it spawns sub-processes within the same task rather than launching separate ECS tasks. This avoids the overhead of task scheduling, IAM role passing, and cross-task coordination for Phase 0. If a single task's resources prove insufficient, we can revisit spawning sibling ECS tasks in Phase 1.

### Why hybrid orchestration (triage + synthesis)

| Pattern | Pros | Cons |
|---------|------|------|
| **Deterministic** (always run all 7) | Predictable, debuggable | Wastes compute on irrelevant analyses |
| **Fully AI-driven** (referee owns workflow) | Maximum intelligence | Non-deterministic, hard to debug, silent failures |
| **Hybrid** (AI selects, Step Functions executes) | Workload-aware + auditable + deterministic execution | Triage accuracy risk (mitigated by safeguards) |

The referee is split into two agents:

- **Referee-Triage**: Reads collector output, selects relevant analysis agents, returns a list with reasons
- **Referee-Synthesis**: Receives analysis outputs, produces weighted ranking, may request deeper analysis (capped at 2 iterations)

### Resume and Restart Strategy

No intra-step checkpointing. If a step fails, it restarts from scratch. This keeps the agent code simple — no partial state recovery logic.

Each agent declares its **restart points** (mini-steps). Restarting a previous mini-step triggers a cascade: all subsequent mini-steps and downstream agents are invalidated and must re-run.

```
Example: Collector declares mini-steps:
  1. connect
  2. collect_schema
  3. collect_metrics
  4. collect_samples

If "collect_metrics" is restarted:
  → collect_metrics re-runs from scratch
  → collect_samples is invalidated and re-runs
  → All downstream agents (triage, analysis, synthesis) are invalidated
```

This is simpler than partial resume and avoids stale data propagation. The cost of re-running a step is acceptable given our <6 hour total job target.

### WebSocket Status Instrumentation

Every agent must report progress via EventBridge at each mini-step boundary. The status flow:

```
Agent mini-step → EventBridge event → Lambda → API Gateway WebSocket → UI
```

Each event includes: `job_id`, `agent_name`, `mini_step`, `status` (started/completed/failed), `timestamp`, `metadata` (e.g., tables processed, percent complete).

### S3 Storage Convention

All agent artifacts follow this path structure:

```
s3://<bucket>/<database-name>/<job-id (KSUID)>/<agent-name>/artifact.json
```

Examples:

```
s3://modernizer-dev-data/myapp-postgres/2GxZLsnP00Y2BwR0000000001/collector/output.json
s3://modernizer-dev-data/myapp-postgres/2GxZLsnP00Y2BwR0000000001/referee-triage/triage.json
s3://modernizer-dev-data/myapp-postgres/2GxZLsnP00Y2BwR0000000001/analysis-dynamodb/analysis.json
s3://modernizer-dev-data/myapp-postgres/2GxZLsnP00Y2BwR0000000001/referee-synthesis/report.json
```

KSUID provides time-ordered, globally unique job IDs without coordination. The `<database-name>` prefix enables easy browsing and lifecycle policies per source database.

---

## Architecture

### Three-Layer Separation

```
┌──────────────────────────────────────────────────────────────┐
│  Layer 1: Job Orchestration (Step Functions)                 │
│  Collector → Triage → Map(selected analyses) → Synthesis     │
│  → [deeper analysis loop?] → Schema Design                   │
└──────────────────────────────────────────────────────────────┘
                        │ progress events
                        ▼
┌──────────────────────────────────────────────────────────────┐
│  Layer 2: Notifications (EventBridge → WebSocket)            │
│  Agent mini-step progress → EventBridge → Lambda → WS → UI  │
└──────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────┐
│  Layer 3: Intra-Agent (Strands SDK + sub-processes)          │
│  Agent runs in one ECS task, spawns sub-processes internally │
│  Declares restart points (mini-steps)                        │
│  Reports status at each mini-step boundary                   │
└──────────────────────────────────────────────────────────────┘
```

### Step Functions Workflow

```
┌──────────────┐
│  Collector   │  Writes to s3://<db>/<ksuid>/collector/
└──────┬───────┘
       ▼
┌──────────────────┐
│  Referee-Triage  │  Reads collector output
│                  │  Returns: selected_agents[], skipped_agents[]
└──────┬───────────┘
       ▼
┌──────────────────────────────────────────┐
│  Map State (dynamic parallel)            │
│  Only runs agents selected by triage     │
│  Each writes to s3://<db>/<ksuid>/<agent>/
│  MaxConcurrency: 7                       │
└──────────────────┬───────────────────────┘
                   ▼
┌──────────────────────┐
│  Referee-Synthesis   │  Weighted ranking + confidence scores
│                      │  May request deeper analysis (max 2x)
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│  Choice: Schema      │
│  Design needed?      │  Map State: selected schema agents
└──────────────────────┘
```

### Triage Output Example

```json
{
  "selected_agents": [
    {"agent_type": "dynamodb", "reason": "95% key-value access patterns"},
    {"agent_type": "elasticache", "reason": "Hot key patterns, TTL usage"},
    {"agent_type": "documentdb", "reason": "Nested JSON columns"}
  ],
  "skipped_agents": [
    {"agent_type": "neptune", "reason": "No graph traversal patterns"},
    {"agent_type": "opensearch", "reason": "No full-text search queries"},
    {"agent_type": "keyspaces", "reason": "No wide-column access patterns"},
    {"agent_type": "aurora", "reason": "Source is PostgreSQL — lateral move"}
  ],
  "confidence": 0.87
}
```

### Task Sizing (Phase 0)

One big task per agent. Generous defaults — cost difference is cents per job at pilot scale.

| Agent Type | vCPU | Memory |
|------------|------|--------|
| API Server | 0.5 | 1 GB |
| Collector | 4 | 8 GB |
| Referee-Triage | 2 | 4 GB |
| Analysis Agent | 2 | 4 GB |
| Referee-Synthesis | 2 | 4 GB |
| Schema Design | 2 | 4 GB |

Right-size in Phase 1 after instrumenting 20+ real jobs.

---

## Triage Safeguards

1. **Full analysis override** — users can bypass triage and run all 7 agents
2. **Minimum 2 agents** — if triage selects fewer, fall back to full analysis
3. **Confidence threshold** — if triage confidence < 0.7, fall back to full analysis
4. **Triage logging** — all decisions persisted to S3, visible in UI
5. **Synthesis feedback loop** — can request deeper analysis (capped at 2 iterations)

---

## Consequences

**Positive:**

- No custom join/barrier logic — Step Functions handles natively
- Workload-aware analysis — skip irrelevant agents, save compute and Bedrock tokens
- Auditable AI decisions — triage output logged with reasons
- Visual debugging via Step Functions execution history
- Simple restart model — no partial state recovery complexity
- Full WebSocket visibility into every agent mini-step
- Clean S3 structure — browsable by database, job, and agent

**Neutral:**

- EventBridge stays with narrower role (notifications only)
- ADR-001 checkpoint strategy superseded by restart-from-scratch model
- ADR-005 mini-collector design updated — sub-processes within one task instead of separate ECS tasks
- ADR-007 referee orchestration superseded by triage/synthesis split

**Negative:**

- Step Functions adds a new infrastructure component
- Triage accuracy risk (mitigated by safeguards above)
- Restart-from-scratch means wasted work on failure (acceptable given <6h target)
- Lambda Durable Functions deferred — may revisit

---

## Alternatives Considered

| Alternative | Why not |
|-------------|---------|
| Lambda Durable Functions | us-east-2 only, Python 3.13+, Dec 2025 launch, DB reconnection overhead. Deferred to Phase 1 |
| EventBridge-only orchestration | Requires custom join logic (Lambda + DynamoDB). Undifferentiated work |
| Always run all 7 analyses | Wastes compute on irrelevant analyses |
| Fully AI-driven referee | Non-deterministic, hard to debug, silent failures |
| Separate ECS tasks per mini-collector | Over-engineered for Phase 0. Sub-processes within one task are simpler |
| Intra-step checkpointing | Adds complexity for marginal benefit. Restart-from-scratch is simpler and acceptable |

---

## Implementation Plan

1. Add Step Functions state machine to CloudFormation (`infrastructure/cloudformation/orchestration.yaml`)
2. Add ECS task definitions per agent type with generous sizing
3. Add IAM permissions for Step Functions → ECS
4. Update API server to start Step Functions execution
5. Implement triage agent (Strands SDK + Bedrock, structured output)
6. Implement synthesis agent (weighted ranking, confidence scoring, deeper analysis loop)
7. Add "full analysis" override parameter to API
8. Instrument all agents with mini-step progress reporting via EventBridge → WebSocket
9. Implement S3 naming convention: `<database-name>/<ksuid>/<agent-name>/artifact.json`
10. Define restart point declarations per agent
11. Update HLD to reflect three-layer architecture

---

## Related Documents

- [ADR-001: State Management and Checkpoints](ADR-001-state-management-and-checkpoints.md) — superseded by restart-from-scratch
- [ADR-003: Progress Reporting Architecture](ADR-003-progress-reporting-architecture.md)
- [ADR-005: Mini-Collectors for Large Databases](ADR-005-mini-collectors-for-large-databases.md) — updated: sub-processes, not separate tasks
- [ADR-007: Referee Orchestration](ADR-007-referee-orchestration.md) — superseded by triage/synthesis split
- [High-Level Design](../high-level-design.md)

---

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | 2026-02-18 | Architecture Team | Initial draft |
| 1.1 | 2026-02-18 | Architecture Team | Added hybrid triage/synthesis pattern, safeguards |
| 2.0 | 2026-02-18 | Architecture Team | Condensed — removed redundant detail |
| 3.0 | 2026-02-18 | Architecture Team | Team feedback: restart-from-scratch, one big task with sub-processes, WebSocket instrumentation, S3 naming convention. Status → Accepted |

**Status: Accepted**
