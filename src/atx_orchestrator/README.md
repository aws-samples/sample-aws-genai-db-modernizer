# AWS Transform Integration (`atx_orchestrator`)

Wraps the existing deterministic DB modernization pipeline so it can run on
AWS Transform as an orchestrator coordinating purpose-built subagents.

- **Owner / contact:** `wwso-database-modernizer`
- **Status:** PoC, experimental branch. Collector subagent + orchestrator built
  and verified locally. **Not yet deployed to AWS** (account allowlisting pending).

## Architecture

One agent per image, mirroring the existing `AGENT_TYPE` boundaries:

```
AWS Transform WebApp
     │  MCP / A2A
     ▼
DBModernizationOrchestrator        (image: db-modernization-orchestrator)
     │  invokes subagents via A2A (Agentic API)
     ├─────────────► Collector subagent   (image: db-modernization-collector)
     │                    │ ingest offline collection → collector/output.json
     ├─────────────► Triage subagent      (future image)
     │                    │ collector/output.json → referee-triage/triage.json
     └─────────────► (analysis, assignment, reality-check, schema-design, synthesis — future)

All agents read/write through the ArtifactStore abstraction:
  - local dir  (ARTIFACT_DIR)  for testing
  - S3 bucket  (S3_BUCKET)     for cloud
```

The pipeline logic is **unchanged** — these wrappers call the existing handlers
(`run_collector`, `run_triage`, …) via shared functions in `core.py`. The whole
deterministic path (collect → triage → assignment → reality-check) is byte-for-byte
reproducible; only the orchestrator LLM's *routing* is non-deterministic.

## Files

| File | Purpose |
|---|---|
| `core.py` | Shared, storage-agnostic phase functions (single source of truth) |
| `subagent_base.py` | Shared A2A message parsing + status management; one factory per agent |
| `collector_subagent.py` | Collector subagent (`AGENT_TYPE='collector'`) |
| `collector_app.py` | Collector container entry point |
| `Dockerfile.collector` | Collector image |
| `orchestrator.py` | Orchestrator class + tool registration |
| `app.py` | Orchestrator container entry point |
| `Dockerfile` | Orchestrator image |
| `tools.py` | Orchestrator tools (`run_collect`, `run_triage`, `run_assignment`, …) |
| `requirements.txt` | Container Python deps (SDK + project runtime deps) |

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `ARTIFACT_DIR` | `/app/artifacts` | Local artifact dir (used when `S3_BUCKET` unset) |
| `S3_BUCKET` | (unset) | Set to use S3 instead of local filesystem |
| `OFFLINE_S3_BUCKET` / `OFFLINE_S3_KEY` | (unset) | Only needed if using the legacy ECS collector handler (not the ATX path) |
| `LLM_MODE` | `none` | `none` keeps everything deterministic; `bedrock` enables optional LLM phases |
| `MODEL_ID` | `us.anthropic.claude-sonnet-4-5-20250929-v1:0` | Orchestrator/subagent LLM (cross-region profile) |
| `AWS_REGION` | `us-east-1` | AWS region |

## Build (ARM64 — required for Bedrock AgentCore / Graviton)

```bash
# Orchestrator
docker build --platform linux/arm64 \
  -t db-modernization-orchestrator:latest \
  -f src/atx_orchestrator/Dockerfile .

# Collector subagent
docker build --platform linux/arm64 \
  -t db-modernization-collector:latest \
  -f src/atx_orchestrator/Dockerfile.collector .
```

## Local tests (no AWS, no Docker required)

```bash
uv run python scripts/atx_smoke_test.py       # imports + wiring
uv run python scripts/atx_contract_test.py    # raw handlers reproduce reference
uv run python scripts/atx_tool_test.py        # orchestrator tool reproduces reference
uv run python scripts/atx_subagent_test.py    # collector|triage split reproduces reference
```

## Deploy

See `docs/aws-transform-handoff.md` for the full deployment runbook, including the
allowlisting prerequisite and the `deploy_agent_full_pipeline` invocations.
