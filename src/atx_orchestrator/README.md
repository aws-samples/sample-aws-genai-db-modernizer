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

The fleet is **four agents**: the orchestrator plus three subagents. The
assessment front-half was consolidated into one `assessment-core` agent (ADR-025,
ADR-026) and the six per-engine schema runtimes into one parametrized `schema`
agent (ADR-027), so `AGENT_TYPE` now takes one of `orchestrator`,
`assessment-core`, `schema`, `referee-synthesis`.

```
AWS Transform WebApp
     │  MCP / A2A
     ▼
orchestrator (AGENT_TYPE=orchestrator)
     │  invokes 3 subagents by name over A2A (Agentic API)
     │
     ├─► assessment-core   (AGENT_TYPE=assessment-core): whole front-half, one agent
     │        Collect       → collector/output.json
     │        Triage        → referee-triage/triage.json
     │        Analyze       → analysis-<engine>/...  (per triage-selected engine)
     │        Assign        → assignment/v{N}/assignment.json
     │        Reality Check → consolidated assignment (CTO-level engine trim; 1 LLM pass)
     │
     ├─◆ assignment-review GATE  (orchestrator tools; the one required pause)
     │        present_assignment_review → [optional] open_detailed_routing_review
     │                                  → finalize_assignment_review
     │        finalize runs the feasibility reviewer; BLOCKING findings loop the
     │        gate (status "infeasible") until fixed or explicitly accepted.
     │
     ├─► schema            (AGENT_TYPE=schema): one agent, invoked once per target
     │        engine IN PARALLEL → schema-<engine>/v{N}/schema_output.json
     │
     └─► referee-synthesis → synthesis/v{N}/report.json (+ Decision & Engineering reports)

Re-entry (ADR-029, after a report exists): reopen_assignment_review → finalize the
edit → redispatch_after_reroute re-runs ONLY the engines whose routing changed
(unchanged engines' schema is copied forward v{N} → v{N+1}), then synthesis reruns.

All agents read/write through the ArtifactStore abstraction:
  - local dir  (ARTIFACT_DIR)  for testing
  - S3 bucket  (S3_BUCKET)     for cloud
```

The per-query engine assignment stays **deterministic and auditable**: no LLM
decides which engine a table or query goes to. The LLM/advisory passes are the
Reality Check consolidation (validates the deterministic engine trim and writes a
CTO summary), the post-gate feasibility reviewer (flags routings that will not
work; it advises and can block, but never reroutes), and the synthesis executive
summary. Only the orchestrator's own tool-routing is non-deterministic. The
wrappers call the existing handlers via shared functions in `core.py`.

### Design decisions

The current shape of this integration is recorded in `docs/architecture/decisions/`:

- **ADR-025**: consolidate the deterministic front-half into one agent
- **ADR-026**: fold Reality Check into `assessment-core`
- **ADR-027**: consolidate the six schema-design runtimes into one `schema` agent
- **ADR-028**: customer assignment-review gate
- **ADR-029**: assignment re-entry + post-gate feasibility review

## Files

| File | Purpose |
|---|---|
| `atx_entrypoint.py` | Single container entry point; dispatches on `AGENT_TYPE` |
| `Dockerfile.atx` | The one image for every agent (ARM64 / Graviton) |
| `core.py` | Shared, storage-agnostic phase functions (single source of truth) |
| `orchestrator.py` | Orchestrator class, `PIPELINE_TOOLS` registration, `SYSTEM_PROMPT` |
| `app.py` | `build_agent_factory` for the orchestrator agent |
| `startup.py` | Orchestrator's proactive job-open welcome message |
| `tools.py` | Orchestrator tools: `run_*_via_a2a`, the assignment-review gate (`present_`/`open_detailed_routing_`/`finalize_assignment_review`), and re-entry (`reopen_assignment_review`, `redispatch_after_reroute`) |
| `a2a.py` | A2A invoke-and-poll primitive |
| `subagents/base.py` | Shared subagent factory (A2A message parsing + status management) + `run_server` |
| `subagents/assessment_core.py` | Consolidated front-half subagent: Collect → Triage → Analyze → Assign → Reality Check (ADR-025, ADR-026) |
| `subagents/schema.py` | Schema-design subagent: one parametrized factory, invoked once per target engine (ADR-027) |
| `subagents/synthesis.py` | Referee-synthesis subagent (final report) |
| `runtime/job_plan.py` | WebApp progress-panel plan + per-phase status updates |
| `runtime/job_status.py` | Job/phase status helpers |
| `runtime/hitl.py` | Human-in-the-loop transport for the detailed routing-review table |
| `runtime/artifacts.py` | Artifacts-panel publishing + Decision/Engineering report renderers |
| `runtime/analysis_report.py`, `pdf_report.py`, `pptx_report.py` | Report renderers (analysis markdown, PDF, executive PPTX) |
| `runtime/store.py` | Transform storage subclasses (adds `write_text`) |
| `runtime/assets/`, `runtime/templates/` | Report template + static assets |
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
uv run python scripts/atx_subagent_test.py    # consolidated assessment-core reproduces reference
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

`pipeline/atx_deploy.py` deploys the full four-agent fleet (orchestrator +
assessment-core + schema + synthesis) from one image and wires the orchestrator's
`AGENT_NAME_PREFIX` for you, so it is the simplest way to get an end-to-end
personal fleet. The common case reuses the image the pipeline already built — no
rebuild needed:

`apply`/`destroy`/`status` read environment/account/org-specific settings from the
environment (the deploy pipeline injects them; set them yourself for a personal
fleet). At minimum `apply` needs the registry endpoint, an artifact bucket, and the
publisher identity:

```bash
export ATX_REGISTRY_ENDPOINT="<aws-transform-registry-url>"
export ATX_S3_BUCKET="<your-artifact-bucket>"
export ATX_OWNER_NAME="<your-team-or-alias>"
export ATX_OWNER_CONTACT="<your-contact>"

# deploy the full fleet under your alias, reusing the pipeline's image by digest
python pipeline/atx_deploy.py apply --env <alias> \
  --image-uri <acct>.dkr.ecr.us-east-1.amazonaws.com/modernizer-dev-atx@sha256:<digest>

# ...run your assessment in the WebApp ("DB Modernization Assessment - <alias>")...

# tear it all down when finished (full-fleet reap)
python pipeline/atx_deploy.py destroy --env <alias> --force
```

To test your own code changes, build+push an image first (`atx_deploy.py build`)
and pass its digest instead. Keep `<alias>` short: AgentCore runtime names are
capped at 48 chars and the longest is now `dbmod-<alias>-assessment-core`, so
aliases up to ~26 characters fit.

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
