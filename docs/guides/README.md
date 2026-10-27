# Implementation Guides

Implementation guides for the agents, contracts and storage layer behind
Database Modernizer Assessment.

> **Note:** This project originally shipped with a hosted deployment option.
> Following customer feedback, it became a local tool built around Claude Code,
> to keep the open-source release simple to adopt. The retirement is recorded
> in [#175](https://github.com/aws-samples/sample-aws-genai-db-modernizer/issues/175).

## Architecture Overview

Database Modernizer Assessment runs locally. The orchestration ADR-016
originally specified (Step Functions + EventBridge + ECS) described the
hosted deployment retired in [#175](https://github.com/aws-samples/sample-aws-genai-db-modernizer/issues/175);
the current architecture is:

1. **Job Orchestration (`LocalOrchestrator`):** Collector → Referee-Triage → (one analysis per selected engine, concurrent) → Assignment → Reality Check → the assignment review gate (the one human gate) → Schema Design → (Load Test) → Synthesis — all direct function calls over the local artifact store
2. **Progress:** polling (`GET /api/v1/assessments/{job_id}`), derived from artifact presence — no event bus or push channel
3. **Intra-Agent (Strands SDK, `--llm-mode bedrock` only):** the designer, reviewer, advisor and referee steps that need a model build `strands.Agent` instances (the analysis advisor builds a fresh one per call) (see [strands-agent-development-guide.md](strands-agent-development-guide.md)); the deterministic phases (collector, triage, assignment, reality check's baseline) never touch Strands at all

See [High-Level Design §3.3](../architecture/high-level-design.md#33-orchestration-pattern)
for the current orchestration pattern, and
[ADR-016](../architecture/decisions/ADR-016-compute-and-orchestration-strategy.md)
for the original rationale; its orchestration mechanism is retired, while its principle that agents own their parallelism stands (see the [ADR index](../architecture/decisions/README.md)).

## Core Guides

| Guide | Purpose | Audience |
|-------|---------|----------|
| [strands-agent-development-guide.md](strands-agent-development-guide.md) | The LLM seam pattern and the Strands/Bedrock agent patterns used where a model is involved | Developers, AI assistants |
| [strands-collector-guide.md](strands-collector-guide.md) | Collector agent implementation (checkpointed, local-first) | Developers, AI assistants |
| [new-analysis-agent-guide.md](new-analysis-agent-guide.md) | Adding or changing an analysis engine | Developers, AI assistants |
| [referee-agent-guide.md](referee-agent-guide.md) | Referee-Triage, Reality Check and Referee-Synthesis agents | Developers, AI assistants |
| [testing-guide.md](testing-guide.md) | Testing strategies for all agent types | Developers, QA |
| [storage-architecture-guide.md](storage-architecture-guide.md) | The `ArtifactStore` abstraction (local filesystem / S3) and the job folder layout | Developers |
| [load-testing-new-engine.md](load-testing-new-engine.md) | Adding a load-test provisioner for an engine | Developers |
| [../architecture/rule-catalog.md](../architecture/rule-catalog.md) | The generated rule catalog: engines, patterns and gaps | Developers |
| [context-graph-query-cookbook.md](context-graph-query-cookbook.md) | Ready-to-run Cypher queries for the context graph | Developers, Solutions Architects |
| [aws-transform-integration.md](aws-transform-integration.md) | Running the pipeline on AWS Transform (subagents + A2A) | Developers, Solutions Architects |

## Quick Start

### For Developers

1. Read [strands-agent-development-guide.md](strands-agent-development-guide.md) for the LLM seam pattern and the agent entrypoint dispatch
2. Read [strands-collector-guide.md](strands-collector-guide.md) for collector patterns
3. Review [new-analysis-agent-guide.md](new-analysis-agent-guide.md) for analysis patterns
4. Study [referee-agent-guide.md](referee-agent-guide.md) for triage, reality check and synthesis patterns
5. Check [testing-guide.md](testing-guide.md) for testing strategies

### For AI Assistants

Start from [AGENTS.md](../../AGENTS.md) (the repo map, the invariants and the routing rules), then read the guide for the area you are changing. To add a command or skill, use `/add-modernizer-skill`.

## Agent Types

### Collector Agents

**Purpose:** Collect metadata from source databases (MySQL, PostgreSQL, MariaDB, SQL Server, Oracle)

**Pattern:** Checkpointed stages (metadata, schema, queries, AWS metrics, output), checkpointed to an S3 `CheckpointStore` when credentials are available or a `NoopCheckpointStore` (no checkpointing) for local runs; no LLM involved

### Referee-Triage Agent

**Purpose:** Read collector output and select the engines to analyse. Every query-owning engine is selected; signals are evidence, not a filter. ElastiCache is the one engine that runs only when one of its signals fires (`src/agents/referee/triage.py`). An engine scope (`--target-databases`) narrows the set

**Pattern:** Pure deterministic pattern matching (`src/agents/referee/triage.py`) — no LLM

**Output:** `triage.json` — the selected engines with evidence, skipped engines with reasons, and a reporting-only `deferred_agents` list that nothing routes on

### Analysis Agents

**Purpose:** Score query/table fit against target databases (one analysis per engine triage selected)

**Pattern:** Deterministic scoring from the rule catalog (TOML under `src/catalog/`, ADR-031 Decision 8). Routing scores each query with the one scorer, `query_scoring.score_query_for_engine`, and never adds a private fit function. An optional Strands/Bedrock LLM advisor for unmatched queries (`--llm-mode bedrock`) sits on top

**Examples:** DynamoDB, DocumentDB, Aurora MySQL, Aurora PostgreSQL, ElastiCache, OpenSearch analysis agents

### Referee-Synthesis Agent

**Purpose:** Read analysis, schema design and load test outputs; produce the final weighted recommendations and executive summary

**Pattern:** Deterministic synthesis always runs; the executive summary and narrative use the LLM seam pattern. The output carries a `needs_deeper_analysis` flag left over from the retired Step Functions flow — nothing in `LocalOrchestrator` reads it today.

### Schema Design Agents

**Purpose:** Generate detailed target schemas for the engines Reality Check assigned

**Pattern:** Strands/Bedrock designer + PE-reviewer loop, capped at `MAX_PE_ITERATIONS = 2`; DynamoDB always splits into groups that are designed independently and then merged (mandatory for every DynamoDB run, not just large workloads)

## Agent Dispatch

Agent code runs as direct Python function calls under `LocalOrchestrator`, not
as separate containers or tasks. `src/agents/entrypoint.py` still routes on an
`AGENT_TYPE` environment variable, but only to serve the `agent-load-test`
container image's run-to-completion dispatch. The AWS Transform integration
does not route through this file at all — it has its own sibling,
`src/atx_orchestrator/atx_entrypoint.py`, with its own `AGENT_TYPE`
vocabulary, dispatching to long-running A2A/AgentCore servers instead. See
[the orchestrator README](../../src/orchestrator/README.md) for the full
dispatch table and artifact paths.

## Implementation Checklist

Before implementing any agent:

- [ ] Read the relevant implementation guide above
- [ ] Review the contract specifications (`docs/contracts/agent-contracts-spec.md`, `src/contracts/`)
- [ ] For a routing change, read ADR-031 and AGENTS.md's "Routing changes" first
- [ ] For an LLM-backed phase, follow the LLM seam pattern (`run_*_deterministic`, `prepare_*_llm_input`, `apply_*_llm_output`)
- [ ] Write tests first (TDD) — see [testing-guide.md](testing-guide.md)
- [ ] Implement the deterministic path first; add the LLM seam only where a model genuinely adds value
- [ ] Validate output against the Pydantic contract in `src/contracts/`
- [ ] Run `make lint` and `make test`; add `make e2e` if the change touches pipeline output

## Related Documentation

- **Architecture:** [../architecture/high-level-design.md](../architecture/high-level-design.md)
- **Decisions:** [../architecture/decisions/README.md](../architecture/decisions/README.md) (the ADR index with each ADR's status)
- **Contracts:** [../contracts/agent-contracts-spec.md](../contracts/agent-contracts-spec.md)
- **Repo map:** [../../AGENTS.md](../../AGENTS.md)

---

**Last Updated:** October 2026
**Maintained By:** Database Modernizer Assessment Team
