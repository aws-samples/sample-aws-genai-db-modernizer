# Architecture Diagrams

Mermaid diagrams for the Database Modernizer Assessment architecture. Render in GitHub, VS Code (with a Mermaid extension), or [mermaid.live](https://mermaid.live/).

## Diagrams

| Diagram | Description |
|---------|-------------|
| [System Context](01-system-context.md) | The user, the source database, the optional model and the opt-in live path |
| [Pipeline and Data Flow](06-data-flow.md) | The current pipeline: one approval gate, query-first routing, the artifacts and deliverables |

Other diagrams of the hosted deployment (ECS, Docker Compose, CloudFormation, Step Functions and EventBridge orchestration, progress reporting over WebSocket) and of designs that were never built (mini-collectors, an output-retry contract loop) were removed; git history keeps them. The storage layer is described in the [storage guide](../../guides/storage-architecture-guide.md).

**Related:** [High-Level Design](../high-level-design.md) · [ADR index](../decisions/README.md) · [Implementation Guides](../../guides/README.md)
