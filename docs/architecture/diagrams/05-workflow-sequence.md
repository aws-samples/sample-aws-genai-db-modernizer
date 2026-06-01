# Workflow Sequence

Job submission to completion using Step Functions orchestration per [ADR-016](../decisions/ADR-016-compute-and-orchestration-strategy.md).

```mermaid
sequenceDiagram
    participant User
    participant API
    participant SF as Step Functions
    participant Collector
    participant Triage as Referee-Triage
    participant Analysis as Analysis (selected)
    participant AR as Assignment Resolution
    participant RC as Reality Check
    participant Schema as Schema Design
    participant LT as Load Test (k6)
    participant Synthesis as Referee-Synthesis
    participant S3
    participant UI as Web UI

    opt Offline mode — file upload before job start
        User->>API: POST /api/v1/assessments/prepare
        API-->>User: job_id + presigned S3 URL
        User->>S3: Upload collector JSON (presigned PUT)
        User->>API: POST /api/v1/assessments/[job_id]/uploads/confirm
        API-->>User: 200 OK (file registered)
        Note over User,API: Optional: GET .../uploads (list) or DELETE .../uploads/[file]
    end

    User->>API: POST /api/v1/assessments
    API->>SF: StartExecution
    API-->>User: 202 Accepted (job_id)

    Note over SF: Step 1: Collection
    SF->>Collector: ecs:runTask.sync
    alt Live mode (default)
        Collector->>S3: Connect to DB, collect schema + metrics
    else Offline mode (collection_mode: "offline")
        Collector->>S3: Read pre-collected JSON from offline_s3_key
        Note over Collector: Still fetches CloudWatch/PI metrics via AWS APIs
    end
    Collector->>S3: Save collector/output.json
    Collector->>S3: Materialize query-journeys/[query_id].json (source)

    Note over SF: Step 2: Triage
    SF->>Triage: ecs:runTask.sync
    Triage->>S3: Read collector output
    Triage->>S3: Save referee-triage/triage.json
    Triage-->>SF: selected agent list

    Note over SF: Step 3: Human Gate 1 — Assignment Review (waitForTaskToken)
    SF->>S3: Store task token in DynamoDB (keyed by job_id)
    UI->>API: GET /api/v1/assessments/[job_id] (detects AWAITING_APPROVAL)
    UI->>API: GET /api/v1/assessments/[job_id]/triage
    API-->>UI: triage signals + proposed engine selections
    User->>API: POST /api/v1/assessments/[job_id]/resume
    API->>SF: SendTaskSuccess(taskToken)

    Note over SF: Step 4: Analysis Pipelines (RunAnalysisPipelines Map, per selected engine)
    par one iteration per selected engine
        SF->>Analysis: ecs:runTask.sync (analysis)
        Analysis->>S3: Save analysis-[engine]/analysis.json
    end

    Note over SF: Step 5: Assignment Resolution
    SF->>AR: ecs:runTask.sync
    AR->>S3: Read all analysis outputs, map queries to engines
    AR->>S3: Save assignment/v[N]/assignment.json
    AR->>S3: Materialize query-journeys/[query_id].json (assignment)

    Note over SF: Step 6: Reality Check
    SF->>RC: ecs:runTask.sync
    RC->>S3: Read assignments, consolidate engines
    RC->>S3: Save reality-check/reality_check.json

    Note over SF: Step 7: Human Gate 2 — Schema Design Approval (waitForTaskToken)
    SF->>S3: Store task token in DynamoDB (keyed by job_id)
    UI->>API: GET /api/v1/assessments/[job_id] (detects AWAITING_APPROVAL)
    UI->>API: GET /api/v1/assessments/[job_id]/assignments
    API->>S3: Read assignment + reality-check outputs
    API-->>UI: final query→engine assignments
    opt User overrides assignments or narrows scope
        User->>API: PUT /api/v1/assessments/[job_id]/assignments
        API->>S3: Persist overrides
    end
    User->>API: POST /api/v1/assessments/[job_id]/resume
    API->>SF: SendTaskSuccess(taskToken)

    Note over SF: Step 8: Schema Design Pipelines (RunSchemaDesignPipelines Map, per assigned engine)
    par one iteration per assigned engine
        SF->>Schema: ecs:runTask.sync (schema design)
        loop PE review loop
            Schema->>Schema: Validate design against PE criteria
        end
        Schema->>S3: Save schema-[engine]/schema_output.json
        Schema->>S3: Save schema-[engine]/design_trace.json
        Schema->>S3: Materialize query-journeys/[query_id].json (design)
    end

    Note over SF: Step 9: Load Test Pipelines (RunLoadTestPipelines Map, per engine)
    par one iteration per engine with schema output
        SF->>LT: ecs:runTask.sync (load-test)
        LT->>LT: Provision target infrastructure
        LT->>LT: Seed synthetic data
        LT->>LT: Generate k6 scripts
        LT->>LT: Dry-run validation
        LT->>LT: Execute k6 (15 min sustained)
        LT->>S3: Save load-test-[engine]/v[N]/results/
        LT->>S3: Save load-test-[engine]/v[N]/scripts/ (customer deliverable)
        LT->>S3: Materialize query-journeys/[query_id].json (load_test)
        LT->>LT: Teardown all provisioned resources
    end
    Note over SF,LT: Non-blocking: on failure, catches error and proceeds to synthesis

    Note over SF: Step 10: Synthesis
    SF->>Synthesis: ecs:runTask.sync
    Synthesis->>S3: Load analyses + schemas + load test results, produce ranking
    Synthesis->>S3: Save referee-synthesis/report.json

    opt Deeper analysis requested (max 2 iterations)
        Synthesis-->>SF: request deeper analysis
        SF->>Schema: re-run schema design pipelines
        SF->>LT: re-run load test pipelines
        SF->>Synthesis: re-run synthesis
    end

    SF-->>API: JobComplete

    loop Phase 0: UI polls at auto-refresh interval
        UI->>API: GET /api/v1/assessments/[job_id]
        API->>SF: DescribeExecution + GetExecutionHistory
        API-->>UI: status + per-agent progress
    end

    User->>API: GET /api/v1/assessments/[job_id]/results
    API->>S3: Read synthesis + analysis artifacts
    API-->>User: 200 OK (aggregated results)

    User->>API: GET /api/v1/assessments/[job_id]/query-journeys
    API->>S3: Read query-journeys/ prefix (paginated)
    API-->>User: 200 OK (per-query journey list)
```

## Key Design Points

- Step Functions orchestrates the workflow (not EventBridge)
- Referee-Triage selects which engines to evaluate (not always all 7)
- **Two human gates** pause execution via `waitForTaskToken`:
  - Gate 1 (after triage): user reviews engine selections before analysis
  - Gate 2 (after reality check): user reviews final assignments before schema design
- RunAnalysisPipelines Map runs analysis per selected engine
- RunSchemaDesignPipelines Map runs schema design per assigned engine (with group split/merge for large workloads)
- RunLoadTestPipelines Map runs load testing per engine — non-blocking on failure
- Query journey files are progressively enriched at each stage (collector, assignment, design, load test)
- Referee-Synthesis produces weighted ranking, may request deeper analysis (reruns schema design + load test + synthesis)
- Phase 0 uses polling (`GET /api/v1/assessments/{job_id}`) — per-agent status derived from Step Functions execution history
- EventBridge mini-step progress and WebSocket push deferred to Phase 1

---

**Related:** [Orchestration Architecture](11-orchestration-architecture.md) | [Progress Reporting](10-progress-reporting.md) | [ADR-016](../decisions/ADR-016-compute-and-orchestration-strategy.md) | [ADR-019](../decisions/ADR-019-query-journey-materialization.md) | [ADR-020](../decisions/ADR-020-load-testing-stage.md)
