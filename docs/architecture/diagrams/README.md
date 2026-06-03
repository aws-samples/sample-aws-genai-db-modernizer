# Architecture Diagrams

Mermaid diagrams for the Database Modernizer Assessment architecture. Render in GitLab/GitHub, VS Code (with Mermaid extension), or [mermaid.live](https://mermaid.live/).

## Diagrams

| # | Diagram | Description |
|---|---------|-------------|
| 1 | [System Context](01-system-context.md) | External actors and systems |
| 2 | [ECS Fargate Deployment](02-vpc-deployment.md) | Customer VPC deployment (primary) |
| 3 | [Docker Compose](03-docker-compose-deployment.md) | Local development (secondary) |
| 4 | [Agent Framework](04-agent-framework.md) | Multi-agent architecture (Strands SDK) |
| 5 | [Workflow Sequence](05-workflow-sequence.md) | Job submission to completion |
| 6 | [Data Flow](06-data-flow.md) | Source databases to reports |
| 7 | [Storage Architecture](07-storage-architecture.md) | Storage abstraction layer |
| 8 | [Contract Validation](08-contract-validation.md) | Contract validation in agent execution |
| 9 | [Mini-Collectors](09-mini-collectors.md) | Parallel processing for large databases |
| 10 | [Progress Reporting](10-progress-reporting.md) | Real-time progress via WebSocket |
| 11 | [Orchestration Architecture](11-orchestration-architecture.md) | Three-layer orchestration (Step Functions + EventBridge + Strands SDK) |
| 11b | [EventBridge Orchestration](11-eventbridge-orchestration.md) | ⚠️ Superseded by 11 — retained for reference |
| 12 | [CloudFormation Deployment](12-cloudformation-deployment.md) | IaC deployment with CloudFormation |

**Related:** [High-Level Design](../high-level-design.md) · [ADRs](../decisions/) · [Implementation Guides](../guides/)
