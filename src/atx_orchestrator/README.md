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
| `subagents/base.py` | Shared subagent factory (A2A message parsing + status management) |
| `subagents/collector.py`, `triage.py`, `assignment.py`, `synthesis.py` | Per-phase subagents (one `AGENT_TYPE` each) |
| `subagents/schema.py` | Schema-design subagents (six targets, one parametrized factory) |
| `subagents/analysis/{dynamodb,documentdb,elasticache,opensearch,aurora_pg,aurora_mysql}.py` | Per-engine analysis subagents |
| `runtime/job_plan.py` | WebApp progress-panel updates |
| `runtime/artifacts.py` | Artifacts-panel publishing + Decision/Engineering report renderers |
| `runtime/store.py` | Transform storage subclasses (adds `write_text`) |
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

## Testing your own fleet (before the pipeline)

You can stand up a personal, fully isolated fleet from your own alias — no branch,
no merge, no pipeline. Fleets are named `dbmod-<env>-<agent>`, so use your alias
as the env and it becomes `dbmod-<alias>-*`. It appears in the AWS Transform
WebApp as "DB Modernization Assessment - `<alias>`", separate from everyone else's.

Shared prerequisites (already set up in the account): the `AgentCoreExecutionRole`
and `AWSTransformAgentInvokeRole` roles, and an image you can pull (the pipeline
publishes `modernizer-dev-atx`). You need AWS credentials that can call
`bedrock-agentcore`, `transform-registry`, and `PassRole` those two roles (Admin
works locally).

### Full fleet — use the harness

`pipeline/atx_deploy.py` deploys all 16 agents from one image and wires the
orchestrator's `AGENT_NAME_PREFIX` for you, so it is the simplest way to get an
end-to-end personal fleet. The common case reuses the image the pipeline already
built — no rebuild needed:

```bash
# deploy the full fleet under your alias, reusing the pipeline's image by digest
python pipeline/atx_deploy.py apply --env <alias> \
  --image-uri <acct>.dkr.ecr.us-east-1.amazonaws.com/modernizer-dev-atx@sha256:<digest>

# ...run your assessment in the WebApp ("DB Modernization Assessment - <alias>")...

# tear it all down when finished (full-fleet reap)
python pipeline/atx_deploy.py destroy --env <alias> --force
```

To test your own code changes, build+push an image first (`atx_deploy.py build`)
and pass its digest instead. Keep `<alias>` short: AgentCore runtime names are
capped at 48 chars and the longest is `dbmod-<alias>-analysis-aurora-mysql`, so
aliases up to ~20 characters fit.

### Single agent — use the AWS Transform MCP toolkit

When you are iterating on or debugging one subagent, the `aws-transform-agent-
toolkit` MCP power is better than redeploying the whole fleet. It builds, deploys,
and registers a single agent and gives you `fetch_logs`, `list_log_streams`, and
`validate_agent_setup`. Name the agent `dbmod-<alias>-<suffix>` so it joins your
alias fleet, and — for an orchestrator — set its `AGENT_NAME_PREFIX` to
`dbmod-<alias>` so it resolves your subagents. See the power's
`deploy-agent-workflow.md` steering for the conversational flow.

## Deploy

See `docs/aws-transform-handoff.md` for the deployment runbook.
