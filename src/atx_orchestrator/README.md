# AWS Transform Integration (`atx_orchestrator`)

Wraps the existing deterministic DB modernization pipeline so it can run on
AWS Transform: an LLM orchestrator coordinates each pipeline phase as a subagent
over A2A, while the analysis stays deterministic and auditable.

- **Owner / contact:** `wwso-database-modernizer`

## Architecture

**One image, dispatched by `AGENT_TYPE`.** A single container image
(`Dockerfile.atx`) can run any agent; `atx_entrypoint.py` reads `AGENT_TYPE` at
startup and serves the matching factory. The AWS Transform runtime provisions one
instance per agent, and the orchestrator invokes the others over A2A.

```
AWS Transform WebApp
     │  MCP / A2A
     ▼
orchestrator (AGENT_TYPE=orchestrator)
     │  invokes subagents by name over A2A (Agentic API)
     ├─► collector            → collector/output.json
     ├─► referee-triage       → referee-triage/triage.json
     ├─► analysis-<engine>    (dynamodb, documentdb, elasticache, opensearch, aurora-pg, aurora-mysql)
     ├─► assignment-resolver  → assignment/v1/assignment.json
     ├─► schema-<engine>      (six targets) → schema-<engine>/v1/schema_output.json
     └─► referee-synthesis    → synthesis/v1/report.json (+ Decision & Engineering reports)

All agents read/write through the ArtifactStore abstraction:
  - local dir  (ARTIFACT_DIR)  for testing
  - S3 bucket  (S3_BUCKET)     for cloud
```

The deterministic pipeline logic is **unchanged** — these wrappers call the
existing handlers via shared functions in `core.py`. Only the orchestrator LLM's
routing is non-deterministic; every engine and query recommendation is produced
deterministically.

## Files

| File | Purpose |
|---|---|
| `atx_entrypoint.py` | Single container entry point; dispatches on `AGENT_TYPE` |
| `Dockerfile.atx` | The one image for every agent (ARM64 / Graviton) |
| `core.py` | Shared, storage-agnostic phase functions (single source of truth) |
| `orchestrator.py` | Orchestrator class, tool registration, system prompt |
| `app.py` | `build_agent_factory` for the orchestrator agent |
| `tools.py` | Orchestrator A2A tools (`run_*_via_a2a`) |
| `a2a.py` | A2A invoke-and-poll primitive |
| `subagent_base.py` | Shared subagent factory (A2A message parsing + status management) |
| `collector_subagent.py`, `triage_subagent.py`, `analysis_*_subagent.py`, `assignment_subagent.py`, `synthesis_subagent.py` | Per-phase subagents (one `AGENT_TYPE` each) |
| `schema_subagent.py` | Schema-design subagents (six targets, one parametrized factory) |
| `job_plan.py` | WebApp progress-panel updates |
| `artifacts.py` | Artifacts-panel publishing + Decision/Engineering report renderers |
| `store.py` | Transform storage subclasses (adds `write_text`) |
| `requirements.txt` | Container Python deps (SDK + project runtime deps) |

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `AGENT_TYPE` | (required) | Which agent this container serves; no default |
| `ARTIFACT_DIR` | `/app/artifacts` | Local artifact dir (used when `S3_BUCKET` unset) |
| `S3_BUCKET` | (unset) | Set to use S3 instead of local filesystem |
| `MODEL_ID` | `us.anthropic.claude-sonnet-4-6` | Orchestrator/subagent LLM (cross-region profile) |
| `AWS_REGION` | `us-east-1` | AWS region |

## Build (ARM64 — required for Bedrock AgentCore / Graviton)

```bash
finch build --platform linux/arm64 \
  -t db-modernization-atx:latest \
  -f src/atx_orchestrator/Dockerfile.atx .
```

`AGENT_TYPE` is set per AgentCore runtime, so this one image backs every agent.

## Local tests (no AWS, no Docker required)

```bash
uv run python scripts/atx_smoke_test.py       # imports + wiring
uv run python scripts/atx_contract_test.py    # raw handlers reproduce reference
uv run python scripts/atx_tool_test.py        # orchestrator tool reproduces reference
uv run python scripts/atx_subagent_test.py    # collector | triage split reproduces reference
```

## Deploy

See `docs/aws-transform-handoff.md` for the deployment runbook.
