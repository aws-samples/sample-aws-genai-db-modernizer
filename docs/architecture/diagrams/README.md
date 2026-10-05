# Architecture Diagrams

Mermaid diagrams for the Database Modernizer Assessment architecture. Render in GitLab/GitHub, VS Code (with Mermaid extension), or [mermaid.live](https://mermaid.live/).

## Diagrams

| # | Diagram | Description |
|---|---------|-------------|
| 1 | [System Context](01-system-context.md) | External actors and systems |
| 4 | [Agent Framework](04-agent-framework.md) | Multi-agent architecture (Strands SDK) |
| 5 | [Workflow Sequence](05-workflow-sequence.md) | Job submission to completion |
| 6 | [Data Flow](06-data-flow.md) | Source databases to reports |
| 7 | [Storage Architecture](07-storage-architecture.md) | Storage abstraction layer |
| 8 | [Contract Validation](08-contract-validation.md) | Contract validation in agent execution |
| 9 | [Mini-Collectors](09-mini-collectors.md) | Parallel processing for large databases |
| 10 | [Progress Reporting](10-progress-reporting.md) | Real-time progress via WebSocket |
| 11 | [Orchestration Architecture](11-orchestration-architecture.md) | ⚠️ Superseded — described the retired hosted deployment's Step Functions + EventBridge orchestration; see [High-Level Design §3.3](../high-level-design.md#33-orchestration-pattern) for the current `LocalOrchestrator` |
| 11b | [EventBridge Orchestration](11-eventbridge-orchestration.md) | ⚠️ Superseded by 11 — retained for reference |

Diagrams 2 (ECS Fargate deployment), 3 (Docker Compose deployment) and 12
(CloudFormation deployment) described the hosted deployment removed in
[#175](https://github.com/aws-samples/sample-aws-genai-db-modernizer/issues/175)
and have been deleted.

**Related:** [High-Level Design](../high-level-design.md) · [ADRs](../decisions/) · [Implementation Guides](../guides/)
