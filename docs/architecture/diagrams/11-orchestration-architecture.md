# Orchestration Architecture

Three-layer orchestration per [ADR-016](../decisions/ADR-016-compute-and-orchestration-strategy.md): Step Functions for workflow (with two human-in-the-loop approval gates — after triage and after reality check), EventBridge for notifications, agents for internal parallelism.

```mermaid
graph TB
    subgraph "Layer 1: Job Orchestration — Step Functions"
        API[API Service] -->|StartExecution| SF[Step Functions]
        SF -->|ecs:runTask.sync| COLLECTOR[Collector Task]
        SF -->|ecs:runTask.sync| TRIAGE_TASK[Referee-Triage]
        SF -->|⏸ waitForTaskToken| GATE1[WaitForTriageApproval]
        SF -->|Map per selected engine| ANALYSIS_MAP[Analysis Pipelines]
        SF -->|ecs:runTask.sync| AR[Assignment Resolution]
        SF -->|ecs:runTask.sync| RC[Reality Check]
        SF -->|⏸ waitForTaskToken| GATE2[WaitForAssignmentApproval]
        SF -->|Map per assigned engine| SCHEMA_MAP[Schema Design + PE review]
        SF -->|Map per assigned engine| LOAD_MAP[Load Test Pipelines]
        SF -->|ecs:runTask.sync| REFEREE[Referee-Synthesis]
    end

    subgraph "Approval Gates — Human-in-the-Loop"
        GATE1 -->|store token| DDB[(DynamoDB)]
        GATE2 -->|store token| DDB
        CLIENT_GATE[Web UI] -->|POST /resume| API_RESUME[API]
        API_RESUME -->|read token| DDB
        API_RESUME -->|SendTaskSuccess| SF
    end

    subgraph "Layer 2: Progress — Phase 0 Polling"
        CLIENT[Web UI] -->|GET /assessments/job_id| API
        API -->|GetExecutionHistory| SF
        API -->|Read artifact summaries| S3
    end

    subgraph "Layer 2: Progress — Phase 1 Push (deferred)"
        COLLECTOR & TRIAGE_TASK & ANALYSIS_MAP & SCHEMA_MAP & LOAD_MAP & REFEREE -.->|progress events| EB[EventBridge]
        EB -.->|trigger| LAMBDA[Lambda] -.-> WS[WebSocket] -.-> CLIENT
    end

    subgraph "Layer 3: Intra-Agent — ECS"
        COLLECTOR -.->|ecs:RunTask| MINI[Mini-Collectors]
    end

    COLLECTOR & ANALYSIS_MAP & AR & RC & SCHEMA_MAP & LOAD_MAP & REFEREE -->|save| S3[S3]
    MINI -->|save| S3

    style SF fill:#f96,stroke:#333,stroke-width:3px
    style GATE1 fill:#fd7,stroke:#333,stroke-width:2px
    style GATE2 fill:#fd7,stroke:#333,stroke-width:2px
    style CLIENT fill:#9cf,stroke:#333,stroke-width:2px
    style CLIENT_GATE fill:#9cf,stroke:#333,stroke-width:2px
```

## Step Functions Workflow

```
StartExecution
  → RunCollector (ecs:runTask.sync, retry 2x)
  → RunRefereeTriage (ecs:runTask.sync)
  → WaitForTriageApproval (waitForTaskToken — execution pauses indefinitely)
      → token stored in DynamoDB via states:dynamodb:updateItem
      → resumes when UI calls POST /assessments/{job_id}/resume
  → RunAnalysisPipelines (Map state, per triage-selected engine):
      → RunAnalysis (ecs:runTask.sync)
  → RunAssignmentResolution (ecs:runTask.sync)
  → RunRealityCheck (ecs:runTask.sync, includes Aurora Absorption Pass)
  → WaitForAssignmentApproval (waitForTaskToken — execution pauses indefinitely)
      → token stored in DynamoDB via states:dynamodb:updateItem
      → resumes when UI calls POST /assessments/{job_id}/resume
  → RunSchemaDesignPipelines (Map state, per assigned engine):
      → RunSchemaDesign (ecs:runTask.sync, includes PE review loop)
  → RunLoadTestPipelines (Map state, per assigned engine):
      → RunLoadTest (ecs:runTask.sync, k6-based)
  → RunRefereeSynthesis (ecs:runTask.sync)
      → [deeper analysis loop, max 2 iterations — loops back to RunSchemaDesignPipelines]
  → JobComplete / JobFailed
```

Step Functions uses `ecs:runTask.sync` integration — it launches the ECS task and waits for completion. No polling needed. `WaitForTriageApproval` and `WaitForAssignmentApproval` use the `waitForTaskToken` pattern — no compute runs during the pause.

## Progress Reporting

Phase 0: UI polls `GET /api/v1/assessments/{job_id}`, API reads Step Functions execution history and S3 artifact summaries. No agent code changes needed.

Phase 1 (deferred): EventBridge `StageProgress` events → Lambda → WebSocket push.

## Agent-Owned Parallelism

Collectors decide at runtime whether to spawn mini-collectors via `ecs:RunTask`. Step Functions sees the collector as a single step. See [Mini-Collectors](09-mini-collectors.md).

---

**Related:** [Workflow Sequence](05-workflow-sequence.md) | [Progress Reporting](10-progress-reporting.md) | [ADR-016](../decisions/ADR-016-compute-and-orchestration-strategy.md)
