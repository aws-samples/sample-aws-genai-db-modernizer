# Mini-Collectors

Parallel processing for large databases (1000+ tables).

```mermaid
graph TB
    ORCH[Collector] --> CHECK{Tables > 100?}
    CHECK -->|no| SINGLE[Single Collector] --> OUT1[CollectorOutput]
    CHECK -->|yes| SPLIT[Split into 100-table chunks]
    SPLIT --> M1[Mini 1<br/>1-100] & M2[Mini 2<br/>101-200] & MN[Mini N<br/>901-1000]
    M1 & M2 & MN --> MERGE[Merge Results] --> OUT2[CollectorOutput]

    style CHECK fill:#f96,stroke:#333,stroke-width:2px
    style MERGE fill:#fc9,stroke:#333,stroke-width:2px
```

## Performance

| Tables | Single | Mini-Collectors | Speedup |
|--------|--------|-----------------|---------|
| 100 | 15 min | 15 min | 1x |
| 1,000 | 2 hours | 15 min | 8x |
| 5,000 | 10 hours | 20 min | 30x |

## How It Works

- Threshold: >100 tables triggers splitting
- Chunk size: 100 tables per mini-collector
- Each mini-collector runs concurrently and saves its own checkpoint
- On failure: resume from the failed mini-collector only (completed ones are preserved)
- Merge: combine schemas, aggregate metrics, deduplicate query patterns

---

**Related:** [Agent Framework](04-agent-framework.md) | [Workflow Sequence](05-workflow-sequence.md)
