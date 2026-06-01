# Implementation Guides

Complete implementation guides for building Database Modernizer components using the Strands SDK.

## Architecture Overview

Database Modernizer uses a three-layer architecture (ADR-016):

1. **Job Orchestration (Step Functions):** Collector → Referee-Triage → Map(selected analyses) → Referee-Synthesis → Schema Design
2. **Notifications:** Phase 0 uses polling (`GET /api/v1/assessments/{job_id}`); EventBridge → WebSocket push is [PLANNED] for Phase 1
3. **Intra-Agent (Strands SDK + sub-processes):** Each agent runs in one ECS Fargate task, spawns sub-processes internally, reports status at mini-step boundaries

See [ADR-016: Compute and Orchestration Strategy](../architecture/decisions/ADR-016-compute-and-orchestration-strategy.md) for full rationale.

## Core Guides

| Guide | Purpose | Audience |
|-------|---------|----------|
| [strands-agent-development-guide.md](strands-agent-development-guide.md) | Strands SDK agent patterns and entrypoint | Developers, AI assistants |
| [strands-collector-guide.md](strands-collector-guide.md) | Collector agent implementation with Strands SDK | Developers, AI assistants |
| [new-analysis-agent-guide.md](new-analysis-agent-guide.md) | Analysis agent implementation for target databases | Developers, AI assistants |
| [referee-agent-guide.md](referee-agent-guide.md) | Referee-Triage and Referee-Synthesis agents | Developers, AI assistants |
| [testing-guide.md](testing-guide.md) | Testing strategies for all agent types | Developers, QA |
| [deployment-guide.md](deployment-guide.md) | Deployment for ECS Fargate, Docker Compose | DevOps |
| [storage-architecture-guide.md](storage-architecture-guide.md) | Storage abstraction layer patterns | Developers |
| [api-development-guide.md](api-development-guide.md) | FastAPI REST API and WebSocket | Developers |

## Strands SDK Architecture

Database Modernizer uses **Strands SDK** for agent implementation:

```python
from strands import Agent, Tool

# Create custom tools
connect_mysql = Tool(
    name="connect_mysql",
    description="Connect to MySQL database",
    function=connect_to_mysql
)

# Create agent with tools
mysql_collector = Agent(
    system_prompt="You are a MySQL collector agent...",
    tools=[connect_mysql, collect_schema, collect_queries]
)

# Execute
output = mysql_collector(input_contract)
```

## Quick Start

### For Developers

1. Read [strands-agent-development-guide.md](strands-agent-development-guide.md) for the agent entrypoint pattern
2. Read [strands-collector-guide.md](strands-collector-guide.md) for collector patterns
3. Review [analysis-agent-guide.md](analysis-agent-guide.md) for analysis patterns
4. Study [referee-agent-guide.md](referee-agent-guide.md) for triage + synthesis patterns
5. Check [testing-guide.md](testing-guide.md) for testing strategies

### For AI Assistants

Feed guides with design docs for context:

```bash
cat docs/guides/strands-agent-development-guide.md \
    docs/guides/strands-collector-guide.md \
    docs/guides/new-analysis-agent-guide.md \
    docs/guides/referee-agent-guide.md \
    docs/guides/testing-guide.md \
    docs/architecture/high-level-design.md \
    docs/contracts/agent-contracts-spec.md \
    > implementation-context.md
```

## Agent Types

### Collector Agents

**Purpose:** Collect metadata from source databases

**Tools:** Database connection, schema collection, query patterns, performance metrics, validation

**Examples:** MySQL Collector, PostgreSQL Collector, SQL Server Collector

### Referee-Triage Agent

**Purpose:** Read collector output, select which analysis agents are relevant for this workload

**Tools:** Workload classification, access pattern analysis, triage decision logging

**Output:** `triage.json` — list of selected agents with reasons, skipped agents with reasons, confidence score

### Analysis Agents

**Purpose:** Analyze compatibility with target databases (only triage-selected agents run)

**Tools:** Pattern detection, compatibility scoring, cost estimation, recommendation generation

**Examples:** DynamoDB Analysis, DocumentDB Analysis, Aurora Analysis, ElastiCache Analysis, OpenSearch Analysis, Neptune Analysis [PLANNED], Keyspaces Analysis [PLANNED]

### Referee-Synthesis Agent

**Purpose:** Read analysis outputs, produce weighted ranking with confidence scores

**Tools:** Recommendation ranking, TCO calculation, risk assessment, report generation. May request deeper analysis (max 2 iterations).

### Schema Design Agents

**Purpose:** Generate detailed schema designs for recommended targets

**Tools:** Schema transformation, DDL generation, migration scripts, SDK samples

## Implementation Workflow

1. **Create Tools** — Build database-specific operations as Strands Tools
2. **Create Agent** — Combine tools with system prompt
3. **Execute** — Agent reads input from S3 via env vars (`AGENT_TYPE`, `JOB_ID`, `DATABASE_NAME`)
4. **Write Output** — Agent writes results to S3 at `<database-name>/<job_id>/<agent-name>/artifact.json`
5. **Report Progress** — ProgressReporter publishes to EventBridge at each mini-step boundary
6. **Validate** — Verify output against contract

## Tool Development Best Practices

**Design Principles:**

- Single responsibility per tool
- Clear inputs/outputs
- Graceful error handling
- Easy to test in isolation
- Reusable across agents

**Tool Template:**

```python
from strands import Tool
from typing import Dict, Any

def my_tool_function(param1: str, param2: int) -> Dict[str, Any]:
    """Clear description of tool purpose."""
    try:
        result = do_something(param1, param2)
        return {'status': 'success', 'data': result}
    except Exception as e:
        return {'status': 'error', 'error': str(e)}

my_tool = Tool(
    name="my_tool",
    description="Clear description for LLM",
    function=my_tool_function
)
```

## Testing Patterns

**Unit Testing Tools:**

```python
def test_connect_mysql_success():
    config = {'host': 'localhost', 'port': 3306}
    result = connect_mysql.function(config)
    assert result['status'] == 'connected'
```

**Integration Testing Agents:**

```python
def test_mysql_collector_agent():
    input_contract = {'job_id': 'test-123', ...}
    output = mysql_collector(input_contract)
    validate_output(output, 'collector-output.json')
```

## Implementation Checklist

Before implementing any agent:

- [ ] Read relevant implementation guide
- [ ] Review contract specifications
- [ ] Identify required tools from examples
- [ ] Design system prompt using templates
- [ ] Plan error handling and mini-step progress reporting
- [ ] Write tests first (TDD)
- [ ] Implement tools
- [ ] Create agent with env var entrypoint pattern
- [ ] Validate against contracts
- [ ] Integration test

## Related Documentation

- **Architecture:** [../architecture/high-level-design.md](../architecture/high-level-design.md)
- **ADR-016:** [../architecture/decisions/ADR-016-compute-and-orchestration-strategy.md](../architecture/decisions/ADR-016-compute-and-orchestration-strategy.md)
- **Contracts:** [../contracts/agent-contracts-spec.md](../contracts/agent-contracts-spec.md)
- **Data Specs:** [../data-specs/](../data-specs/)

---

**Last Updated:** June 2026
**Maintained By:** Database Modernizer Team
