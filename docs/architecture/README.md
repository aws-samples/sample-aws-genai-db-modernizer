# Architecture Documentation

This directory contains the high-level design and architecture documentation for the Database Modernizer system.

## 📚 Documentation Index

### Core Documents

| Document                                         | Purpose                                 | Audience                   |
| ------------------------------------------------ | --------------------------------------- | -------------------------- |
| [high-level-design.md](high-level-design.md)     | Complete system architecture and design | All developers, architects |
| [diagrams/](diagrams/) | Visual architecture diagrams            | All stakeholders           |

## 🏗️ Architecture Overview

Database Modernizer is an AI-powered database modernization analysis and recommendation system that customers deploy and run in their own environments.

### Key Design Decisions

1. **Local-First Deployment** - Customers deploy in their own environment (laptop, on-prem, or their AWS account)
2. **Strands SDK Agent Framework** - Uses Strands agents with custom tools for modular, composable architecture
3. **Agent-Based Architecture** - Independent agents (Collector, Analysis, Referee, Schema Design) coordinate via contracts
4. **Multiple Deployment Options** - Docker Compose (local), AWS CloudFormation (customer AWS), Kubernetes (enterprise)
5. **Customer-Controlled Data** - All data stays in customer's environment (S3, local filesystem, etc.)

### Architecture Layers

```
┌─────────────────────────────────────────────────────────────┐
│                    Web UI (React SPA)                       │
│  • Assessment Report Viewer                                 │
│  • Interactive Recommendation Review                        │
│  • Architecture Visualizer                                  │
└────────────────────────┬────────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────────┐
│              API Server (FastAPI)                           │
│  • REST endpoints for job management                        │
│  • WebSocket for real-time progress updates                 │
└────────────────────────┬────────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────────┐
│          Orchestrator (ECS Fargate + EventBridge)           │
│  • Job queue management                                     │
│  • Agent workflow coordination                              │
└────────────────────────┬────────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────────┐
│                   Agent Layer (Strands SDK)                 │
│  • Collector Agents (MySQL, PostgreSQL, SQL Server, etc.)   │
│  • Analysis Agents (DynamoDB, DocumentDB, etc.)             │
│  • Referee Agent (TCO, Risks, Recommendations)              │
│  • Schema Design Agents (Detailed schema designs)           │
└─────────────────────────────────────────────────────────────┘
```

## 🚀 Quick Start

### For Developers

1. **Read the HLD**: Start with [high-level-design.md](high-level-design.md)
2. **Understand deployment models**: Docker Compose, AWS CloudFormation, or Kubernetes
3. **Review agent architecture**: Strands SDK-based agent framework
4. **Check technology stack**: Python, FastAPI, ECS Fargate, React, Strands SDK

### For Architects

1. **System architecture**: Section 1 of HLD
2. **Deployment models**: Section 2 of HLD
3. **Agent framework**: Section 3 of HLD
4. **Security architecture**: Section 9 of HLD

## 📊 Architecture Diagrams

Visual diagrams are stored in the [diagrams/](diagrams/) directory:

- System architecture overview
- Agent communication flow
- Deployment architecture (Docker Compose, AWS CloudFormation, Kubernetes)
- Data flow diagrams
- Security architecture

## 🔑 Key Concepts

### Strands SDK Architecture

Database Modernizer uses the **Strands SDK** for agent implementation:

- **Strands Agent** - Framework-provided agent class
- **Custom Tools** - Database-specific operations (connect, collect schema, query patterns)
- **System Prompts** - Define agent behavior and execution logic
- **Contracts** - Pydantic models enforce input/output structure with type safety

### Agent Types

1. **Collector Agents** - Collect data from source databases (MySQL, PostgreSQL, etc.)
2. **Analysis Agents** - Analyze data for specific target databases (DynamoDB, DocumentDB, etc.)
3. **Referee Agent** - Ranks recommendations and produces final architecture
4. **Schema Design Agents** - Generate detailed schema designs for selected databases

### Deployment Models

| Model              | Best For         | Complexity | Scalability |
| ------------------ | ---------------- | ---------- | ----------- |
| Docker Compose     | Local dev, demos | Low        | 1-2 jobs    |
| AWS CloudFormation | AWS customers    | Medium     | 10+ jobs    |
| Kubernetes         | Enterprises      | High       | 50+ jobs    |

## 🛠️ Technology Stack

### Core Technologies

- **Backend**: Python 3.12+, FastAPI, ECS Fargate + EventBridge
- **Agent Framework**: Strands SDK
- **Frontend**: React 18, TypeScript, Cloudscape Design System
- **Storage**: S3 / Local FS / NFS
- **Containers**: Docker, ECS Fargate
- **Optional AI**: AWS Bedrock (customer's account)

### Database Drivers

- MySQL: mysql-connector-python
- PostgreSQL: psycopg2-binary
- SQL Server: pymssql
- Oracle: cx_Oracle
- DB2: ibm_db

## 📖 Document Sections

The high-level design document contains:

1. **System Architecture Overview** - High-level architecture and system context
2. **Deployment Models** - Docker Compose, AWS CloudFormation, Kubernetes options
3. **Agent Framework Design** - Strands SDK architecture and agent patterns
4. **Component Architecture** - Detailed component designs
5. **Data Model Design** - Metadata storage and file structure
6. **API Specifications** - REST API endpoints and WebSocket
7. **Technology Stack** - Complete technology choices and justifications
8. **Local Development Setup** - Getting started for developers
9. **Security Architecture** - Security model and credential management
10. **Operational Considerations** - Monitoring, logging, backup, troubleshooting

## 🔄 Architecture Evolution

### Version History

See [high-level-design.md](high-level-design.md) for the full revision history. Current version: **v11.0** (Load Testing, Query Journeys, CI/CD — May 2026).

### Architecture Decision Records (ADRs)

Key architectural decisions are documented in the [decisions/](decisions/) directory:

- ADR-001: State Management and Checkpoints
- ADR-002: Structured Output with Pydantic
- ADR-003: Progress Reporting Architecture (ECS Fargate + EventBridge)
- ADR-004: RDS Tools and AWS Integration
- ADR-005: Mini-Collectors for Large Databases
- ADR-006: Analysis Agent Patterns
- ADR-007: Referee Orchestration
- ADR-008: Contract Versioning
- ADR-009: Testing Infrastructure
- ADR-010: Release Management
- ADR-011: Monorepo Structure
- ADR-012: CloudFormation Over CDK

## 🔗 Related Documentation

- **Business Requirements**: project requirements
- **Agent Contracts**: [../contracts/agent-contracts-spec.md](../contracts/agent-contracts-spec.md)
- **Data Specifications**: [../data-specs/](../data-specs/)
- **Implementation Guides**: [../guides/](../guides/)

## 📝 Contributing

When updating architecture documentation:

1. **Update HLD**: Modify [high-level-design.md](high-level-design.md)
2. **Update diagrams**: Add/update diagrams in [diagrams/](diagrams/)
3. **Version bump**: Update version number and revision history
4. **Document decisions**: Add ADRs for significant architectural changes
5. **Review**: Get approval from architecture team
6. **Communicate**: Notify affected teams of changes

## ❓ Questions?

1. Check the [high-level-design.md](high-level-design.md) for detailed information
2. Review architecture diagrams for visual understanding
3. Ask the architecture team in your communication channel

---

**Last Updated:** June 1, 2026
**Maintained By:** Database Modernizer Engineering Team
