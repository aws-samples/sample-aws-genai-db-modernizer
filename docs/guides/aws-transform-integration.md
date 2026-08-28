# AWS Transform Integration Guide

How the existing deterministic database modernization pipeline runs on AWS
Transform, and how to work on the integration. The code lives in
`src/atx_orchestrator/`; this guide is the reader-oriented companion to that
module's [`README.md`](../../src/atx_orchestrator/README.md).

**Audience:** developers and solutions architects picking up the AWS Transform
integration.

## What this is

AWS Transform runs an LLM orchestrator that coordinates a workflow by invoking
purpose-built subagents. This integration maps the existing pipeline onto that
model without rewriting it: each pipeline phase becomes a subagent, and the
orchestrator drives them in order.

The hard constraint is unchanged from the rest of the project: the analysis stays
deterministic and auditable. The only non-deterministic element is the
orchestrator LLM deciding which tool to call next (routing). Every engine and
query recommendation is produced by the same deterministic Python that the Step
Functions pipeline uses. The wrappers call the existing handlers through shared
functions in `core.py`; no existing pipeline logic was moved into the LLM.

## Architecture: one image, dispatched by `AGENT_TYPE`

A single container image (`Dockerfile.atx`) can run any agent. At startup
`atx_entrypoint.py` reads the `AGENT_TYPE` environment variable and serves the
matching factory. The AWS Transform runtime provisions one instance per agent,
and the orchestrator invokes the others over A2A (agent-to-agent).

```
AWS Transform WebApp
     |  MCP / A2A
     v
orchestrator  (AGENT_TYPE=orchestrator)
     |  invokes subagents by name over A2A (Agentic API)
     |
     +-> collector             -> collector/output.json
     +-> referee-triage        -> referee-triage/triage.json
     +-> analysis-<engine>     -> analysis-<target>/analysis.json (+ decision-trace, +er-diagram)
     +-> assignment-resolver   -> assignment/v1/assignment.json
     +-> schema-<engine>       -> schema-<engine>/v1/schema_output.json
     +-> referee-synthesis     -> synthesis/v1/report.json (+ Decision & Engineering reports)
```

`<engine>` is one of `dynamodb`, `documentdb`, `elasticache`, `opensearch`,
`aurora-pg`, `aurora-mysql`. Every agent reads and writes through the
`ArtifactStore` abstraction: a local directory (`ARTIFACT_DIR`) for testing, or an
S3 bucket (`S3_BUCKET`) in the cloud. A job started by either the Step Functions
pipeline or the AWS Transform path is readable by the other because they share the
same artifact layout.

`AGENT_TYPE` values match the artifact key prefix an operator sees in storage
(for example `analysis-dynamodb`), which keeps them unambiguous. Two of them map
to different names in the core pipeline: `analysis-aurora-pg` and
`analysis-aurora-mysql` correspond to the core pipeline's `aurora_postgresql` and
`aurora_mysql`.

## The pipeline, phase by phase

The orchestrator exposes one tool per phase. It calls `declare_pipeline_plan`
first to register the plan with the WebApp progress panel, then drives the phases:

| Order | Tool | Subagent (`AGENT_TYPE`) | Primary artifact |
|---|---|---|---|
| 1 | `run_collect_via_a2a` | `collector` | `collector/output.json` |
| 2 | `run_triage_via_a2a` | `referee-triage` | `referee-triage/triage.json` |
| 3 | `run_analysis_<engine>_via_a2a` (x6) | `analysis-<engine>` | `analysis-<target>/analysis.json` |
| 4 | `run_assignment_via_a2a` | `assignment-resolver` | `assignment/v1/assignment.json` |
| 5 | `run_schema_design_<engine>_via_a2a` (x6) | `schema-<engine>` | `schema-<engine>/v1/schema_output.json` |
| 6 | `run_synthesis_via_a2a` | `referee-synthesis` | `synthesis/v1/report.json` |

Two read-only helper tools round out the set: `get_job_status` reports per-phase
progression, and `get_synthesis_report` returns the finished report. Reality Check
has no deployed subagent yet.

Analysis runs six engines from one table-driven core (`run_analysis_core` in
`core.py`). DynamoDB and DocumentDB run the optional Bedrock LLM advisor and can
take up to ~90 minutes on a large workload; the other four are purely
deterministic and finish in minutes. Synthesis renders two audience-shaped
reports (an executive Decision Report and a build-team Engineering Report)
alongside the raw report JSON and publishes all three to the WebApp Artifacts
panel.

## How the orchestrator drives a subagent (A2A)

All phase tools go through one primitive: `invoke_and_wait(agent_id, message)` in
`a2a.py`. Given a registered agent name, it:

1. **Discovers or spawns** the subagent instance. It first looks for a
   pre-provisioned instance by name via `list_agent_instances`. If none exists it
   spawns one with `invoke_agent` and waits for it to reach `RUNNING`/`IDLE`.
2. **Sends** the work with `send_message`. A `-32603` (JSON-RPC "Internal error")
   after roughly 25 seconds is normal for a long-running subagent; the container
   is still processing, so the primitive keeps polling rather than treating it as
   a failure.
3. **Polls** `get_agent_instance` until the status is `COMPLETED` or `FAILED`,
   then parses `agentOutput.serializedPayload` (a JSON string) into a dict.

The tools in `tools.py` are thin: each builds the message envelope and delegates
the invoke, progress-marking, and error handling to a shared helper. Failures come
back as a JSON error dict rather than an exception, so the orchestrator LLM can
read and relay them. The LLM only ever knows subagent names; instance-id
resolution happens inside `invoke_and_wait`.

## Determinism and the golden fixture

The reference job `artifacts/discourse/15e6403d/` (311 tables, 1654 queries) is
the golden fixture. Its committed offline collection input feeds through the AWS
Transform path, and the output is diffed against the committed reference, ignoring
only volatile fields (timestamps and the job id). Any change to this integration
must keep that reproduction byte-identical.

## Running locally (no AWS, no Docker)

```bash
uv run python scripts/atx_smoke_test.py       # imports + tool wiring
uv run python scripts/atx_contract_test.py    # raw handlers reproduce the reference
uv run python scripts/atx_tool_test.py        # orchestrator tool path reproduces the reference
uv run python scripts/atx_subagent_test.py    # collector | triage split reproduces the reference
```

The unit and contract suites run in a SDK-absent environment that mirrors CI:

```bash
uv run --no-sync pytest tests/unit/ tests/contract/ --cov=src
```

## Extending: adding a new subagent

Adding a phase touches four places, mirroring the existing subagents:

1. A subagent module exposing `SYSTEM_PROMPT`, a `_work(params)` function that
   calls into `core.py`, and `agent_factory = make_subagent_factory(...)`.
2. A row in `_AGENTS` and a dispatch branch in `atx_entrypoint.py`.
3. A storage directory created and owned in `Dockerfile.atx` (the entrypoint row
   and the Dockerfile directory have no compile-time guard; keep them in sync).
4. An orchestrator tool in `tools.py` that calls `invoke_and_wait`, plus its
   registration in `orchestrator.py`.

The six schema-design targets share one parametrized factory
(`schema_subagent.make_schema_agent_factory`) rather than six near-identical
modules; the six analysis engines share `run_analysis_core` driven by a per-engine
table. Prefer that pattern over copy-paste when a phase has per-engine variants.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `AGENT_TYPE` | (required) | Which agent this container serves; no default |
| `ARTIFACT_DIR` | `/app/artifacts` | Local artifact directory (used when `S3_BUCKET` is unset) |
| `S3_BUCKET` | (unset) | Set to use S3 instead of the local filesystem |
| `MODEL_ID` | `us.anthropic.claude-sonnet-4-6` | Orchestrator/subagent LLM (cross-region inference profile). The analysis LLM advisor is set to its intended production model at deploy time via this variable. |
| `AWS_REGION` | `us-east-1` | AWS region |

## Build

The image must be `linux/arm64` (Bedrock AgentCore runs on Graviton); an x86 image
fails at runtime with `exec format error`.

```bash
finch build --platform linux/arm64 \
  -t db-modernization-atx:latest \
  -f src/atx_orchestrator/Dockerfile.atx .
```

`AGENT_TYPE` is set per AgentCore runtime, so this one image backs every agent.
Deployment (image build/push, AgentCore runtime, and AWS Transform registration)
uses the AWS Transform Agent Toolkit; account-specific deploy steps are kept in
the team's operational handoff rather than in this repo.

## Related documentation

- Module reference: [`src/atx_orchestrator/README.md`](../../src/atx_orchestrator/README.md)
- Compute and orchestration strategy: [ADR-016](../architecture/decisions/ADR-016-compute-and-orchestration-strategy.md)
- Agent contracts: [../contracts/agent-contracts-spec.md](../contracts/agent-contracts-spec.md)
