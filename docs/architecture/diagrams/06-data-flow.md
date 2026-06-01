# Data Flow

Source databases → Collector → Triage → Human Gate 1 → Analysis Map → Assignment Resolution → Reality Check → Human Gate 2 → Schema Design Map → Load Test Map → Synthesis → Reports. Step Functions orchestrates; EventBridge carries progress events. The Collector supports two input modes: live (direct database connection) and offline (pre-collected JSON from S3). Query journey files are progressively enriched at each stage.

```mermaid
graph LR
    subgraph "Sources"
        RDS[(Customer RDS)] & REDIS[(Customer Redis)]
        CW[CloudWatch] & PI[Perf Insights]
        S3_UPLOAD[S3 Upload<br/>offline JSON]
    end

    subgraph "Pipeline (Step Functions)"
        COLLECTOR[Collector] -->|JSON| S3_1[S3]
        S3_1 --> TRIAGE[Referee-Triage]
        TRIAGE --> GATE1{Human Gate 1<br/>⏸ waitForTaskToken}
        GATE1 -.->|UI: POST /resume| ANALYSIS_MAP
        subgraph ANALYSIS_MAP[RunAnalysisPipelines Map]
            ANALYSIS[Analysis Agent] -->|JSON| S3_2[S3]
        end
        ANALYSIS_MAP --> AR[Assignment Resolution]
        AR --> RC[Reality Check]
        RC --> GATE2{Human Gate 2<br/>⏸ waitForTaskToken}
        GATE2 -.->|UI: POST /resume| SCHEMA_MAP
        subgraph SCHEMA_MAP[RunSchemaDesignPipelines Map]
            SCHEMA[Schema Design + PE review] -->|JSON/SQL| S3_3a[S3]
        end
        SCHEMA_MAP --> LOAD_MAP
        subgraph LOAD_MAP[RunLoadTestPipelines Map]
            LOADTEST[Load Test<br/>k6 on ECS] -->|JSON + scripts| S3_LT[S3]
        end
        LOAD_MAP --> SYNTH[Referee-Synthesis]
        SYNTH -->|JSON| S3_3[S3]
        SYNTH -.->|deeper analysis?| SCHEMA_MAP
    end

    subgraph "Query Journeys (progressive enrichment)"
        QJ[query-journeys/query_id.json]
        COLLECTOR -.->|source| QJ
        AR -.->|assignment| QJ
        SCHEMA -.->|design| QJ
        LOADTEST -.->|load_test| QJ
    end

    subgraph "Output"
        REPORT[PDF + HTML + Diagrams]
    end

    RDS & REDIS & CW & PI --> COLLECTOR
    S3_UPLOAD -.->|offline mode| COLLECTOR
    S3_3 --> REPORT --> S3_5[S3]

    style COLLECTOR fill:#9cf,stroke:#333,stroke-width:2px
    style TRIAGE fill:#fc9,stroke:#333,stroke-width:2px
    style ANALYSIS fill:#9f9,stroke:#333,stroke-width:2px
    style GATE1 fill:#fd7,stroke:#333,stroke-width:2px
    style GATE2 fill:#fd7,stroke:#333,stroke-width:2px
    style AR fill:#fc9,stroke:#333,stroke-width:2px
    style RC fill:#fc9,stroke:#333,stroke-width:2px
    style SYNTH fill:#fc9,stroke:#333,stroke-width:2px
    style LOADTEST fill:#c9f,stroke:#333,stroke-width:2px
    style QJ fill:#efe,stroke:#393,stroke-width:1px
```

## S3 Structure

Per [API specification](../api-specification.md), `job_id` is a UUID. `{database-name}` must exactly match the real database name — in live mode this comes from `connection.database`; in offline mode from `metadata.source_database.database_name` in the collected JSON. A mismatch causes table ID mismatches across collector, assignment resolver, and schema design agents.

```
s3://{bucket}/{database-name}/{job-id}/
├── collector/output.json
├── referee-triage/triage.json
├── analysis-{engine}/analysis.json          (one per selected engine)
├── assignment/v{N}/assignment.json          (versioned — increments on reality-check revisions)
├── reality-check/output.json
├── schema-{engine}/v{N}/schema_output.json  (one per assigned engine)
├── schema-{engine}/v{N}/design_trace.json
├── load-test-{engine}/v{N}/
│   ├── config.json
│   ├── infrastructure.json
│   ├── seed-manifest.json
│   ├── k6_diagnostics.json
│   ├── result.json
│   ├── scenarios/                           (customer deliverable — k6 scripts)
│   │   └── {query_id}.js
│   └── results/
│       ├── summary.json
│       └── {query_id}.json                  (per-pattern latency + cost)
├── query-journeys/
│   └── {query_id}.json                      (progressive: source → assignment → design → load_test)
├── uploads/                                 (offline mode only)
│   └── collector-output.json
├── referee-synthesis/report.json
├── report.pdf
└── report.html
```

DynamoDB tracks job metadata (job_id, status, timestamps), phase progression (`collect_triage → analysis → assignment → reality_check → assignment_review → schema_design → load_test → synthesis`), and the Step Functions task tokens for both human gates. All data formats: JSON (Pydantic validated), PDF, HTML, PNG, SQL, JavaScript (k6 scripts).

---

**Related:** [Storage Architecture](07-storage-architecture.md) | [Workflow Sequence](05-workflow-sequence.md) | [ADR-019](../decisions/ADR-019-query-journey-materialization.md) | [ADR-020](../decisions/ADR-020-load-testing-stage.md)
