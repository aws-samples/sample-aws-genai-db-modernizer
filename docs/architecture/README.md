# Architecture Documentation

This directory contains the high-level design and architecture documentation for the Database Modernizer Assessment system.

## Documentation Index

| Document                                     | Purpose                                 | Audience                   |
| ---------------------------------------------- | --------------------------------------- | --------------------------- |
| [high-level-design.md](high-level-design.md) | Complete system architecture and design | All developers, architects |
| [diagrams/](diagrams/README.md)              | Visual architecture diagrams            | All stakeholders           |
| [decisions/](https://github.com/aws-samples/sample-aws-genai-db-modernizer/tree/main/docs/architecture/decisions/) | Architecture Decision Records (history) | All developers, architects |

## Architecture Overview

Database Modernizer Assessment runs local-first: via Claude Code
(`/modernize`), the deterministic CLI, or a local FastAPI + React UI running
on the user's own machine. A prior hosted deployment (ECS Fargate, Cognito,
Step Functions, EventBridge, per-environment CloudFormation stacks) was
retired — see [#175](https://github.com/aws-samples/sample-aws-genai-db-modernizer/issues/175)
and [high-level-design.md §2](high-level-design.md#2-deployment-architecture).
The separate AWS Transform integration (`src/atx_orchestrator/`) runs the
same agent code over S3-backed storage, orchestrated over A2A.

### Key Design Decisions

See [high-level-design.md § Key Design Decisions](high-level-design.md#key-design-decisions)
for the current, maintained list — this file no longer duplicates it to
avoid drifting out of sync (which is what happened to the version removed
in #175/#341).

### Architecture Layers

```
┌─────────────────────────────────────────────────────────────┐
│                    Web UI (React SPA)                       │
│  • Assessment Report Viewer                                 │
│  • Interactive Recommendation Review                        │
│  • Query Journey Explorer                                   │
└────────────────────────┬────────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────────┐
│              API Server (FastAPI, local-only)                │
│  • REST endpoints for job management, polled for progress   │
└────────────────────────┬────────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────────┐
│          LocalOrchestrator (direct function calls)          │
│  • Phase sequencing, per-engine concurrency where used       │
└────────────────────────┬────────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────────┐
│                   Agent Layer (Strands SDK)                 │
│  • Collector Agents (MySQL, PostgreSQL, SQL Server, etc.)    │
│  • Analysis Agents (DynamoDB, DocumentDB, etc.)              │
│  • Referee Agents (Triage, Synthesis)                        │
│  • Schema Design Agents                                      │
└─────────────────────────────────────────────────────────────┘
```

## Quick Start

### For Developers

1. **Read the HLD**: Start with [high-level-design.md](high-level-design.md)
2. **Understand how it runs**: [high-level-design.md §2](high-level-design.md#2-deployment-architecture) — Claude Code, local API/UI, deterministic CLI, or the AWS Transform integration
3. **Review agent architecture**: Strands SDK-based agent framework, [§3](high-level-design.md#3-agent-framework-design)
4. **Check technology stack**: [§6](high-level-design.md#6-technology-stack)

### For Architects

1. **System architecture**: [§1](high-level-design.md#1-system-architecture-overview)
2. **How it runs**: [§2](high-level-design.md#2-deployment-architecture)
3. **Agent framework**: [§3](high-level-design.md#3-agent-framework-design)
4. **Security architecture**: [§7](high-level-design.md#7-security-architecture)

## Architecture Diagrams

Visual diagrams are in [diagrams/](diagrams/README.md): system context, agent
framework, workflow sequence, data flow, storage, contract validation,
mini-collectors, progress reporting and orchestration (the orchestration
diagram is marked superseded — it describes the retired hosted
orchestration; the current `LocalOrchestrator` is in the HLD).

## Related Documentation

- **Agent Contracts**: [../contracts/agent-contracts-spec.md](../contracts/agent-contracts-spec.md)
- **Data Specifications**: [data-specs/](https://github.com/aws-samples/sample-aws-genai-db-modernizer/tree/main/docs/data-specs/)
- **Implementation Guides**: [../guides/](../guides/README.md)

## Contributing

When updating architecture documentation:

1. **Update HLD**: Modify [high-level-design.md](high-level-design.md) — it's the canonical source, this file is just an index
2. **Update diagrams**: Add/update diagrams in [diagrams/](diagrams/README.md)
3. **Version bump**: Update the version number and revision history in the HLD
4. **Document decisions**: Add ADRs for significant architectural changes

---

**Last Updated:** October 2026
**Maintained By:** Database Modernizer Assessment Engineering Team
