# AWS Transform Integration — Handoff

**Owner / contact:** `wwso-database-modernizer`
**Branch:** experimental (`exp/agents-in-transform` or successor)
**Status:** PoC. Orchestrator + Collector subagent built and verified locally.
**Not yet deployed to AWS** — blocked on AWS Transform account allowlisting.

This document is for the next person picking up the AWS Transform integration.
It covers what was built, why, the evidence that contracts are preserved, the
gotchas already discovered, and the exact steps to deploy once allowlisting lands.

---

## 1. Goal

Run the existing deterministic database modernization pipeline on **AWS Transform**
without rewriting it. AWS Transform's orchestrator (an LLM) coordinates the workflow;
each existing pipeline agent becomes a purpose-built **subagent** invoked over A2A.

The hard constraint from the team: **respect the existing contracts and agent
topology. The analysis must stay deterministic and auditable.** No moving
deterministic logic into the LLM.

---

## 2. What exists today

All new code lives in `src/atx_orchestrator/`. **No existing pipeline code was
modified** — the wrappers call the existing handlers via shared functions.

```
AWS Transform WebApp
     │  MCP / A2A
     ▼
DBModernizationOrchestrator        image: db-modernization-orchestrator   (PUBLIC)
     │  invokes subagents via A2A (Agentic API)
     ├──► Collector subagent        image: db-modernization-collector      (RESTRICTED)
     │       ingest offline collection → {db}/{job}/collector/output.json
     ├──► Triage subagent           (NOT YET BUILT — core logic ready)
     │       collector/output.json → {db}/{job}/referee-triage/triage.json
     └──► analysis / assignment / reality-check / schema-design / synthesis  (FUTURE)

Everything reads/writes through the ArtifactStore abstraction:
   ARTIFACT_DIR (local dir)  for testing   |   S3_BUCKET  for cloud
```

### Files in `src/atx_orchestrator/`

| File | Purpose |
|---|---|
| `core.py` | Shared, storage-agnostic phase functions — single source of truth |
| `subagent_base.py` | Shared A2A message parsing + status management; one factory per agent |
| `collector_subagent.py` | Collector subagent (maps to `AGENT_TYPE='collector'`) |
| `collector_app.py` | Collector container entry point |
| `Dockerfile.collector` | Collector image (ARM64) |
| `orchestrator.py` | Orchestrator class + tool registration |
| `app.py` | Orchestrator container entry point |
| `Dockerfile` | Orchestrator image (ARM64) |
| `tools.py` | Orchestrator tools: `run_collect`, `run_triage`, `run_assignment`, … |
| `requirements.txt` | Container Python deps (SDK + project runtime deps) |
| `README.md` | Quick reference |

### Tests (`scripts/`)

| Script | Proves |
|---|---|
| `atx_smoke_test.py` | All modules import and wire up; tool list intact |
| `atx_contract_test.py` | Raw handlers reproduce the reference job's artifacts |
| `atx_tool_test.py` | Orchestrator tool path reproduces reference via ArtifactStore |
| `atx_subagent_test.py` | Split collector \| triage subagents reproduce reference |

Run them all with no AWS and no Docker:

```bash
uv run python scripts/atx_smoke_test.py
uv run python scripts/atx_contract_test.py
uv run python scripts/atx_tool_test.py
uv run python scripts/atx_subagent_test.py
```

---

## 3. Evidence: contracts preserved + deterministic

The reference job `artifacts/discourse/15e6403d/` (311 tables, 1654 queries) is the
golden fixture. Its committed offline collection input lives at
`artifacts/discourse/15e6403d/uploads/collector-output.json`.

The tests feed that input through the **new ATX path** and diff the output against
the committed reference, ignoring only volatile fields (timestamps) and the job_id:

- Collector output: identical table/query counts; validates against `CollectorOutputContract`.
- Triage decision: `selected_agents`, `skipped_agents`, `signals`, `deferred_agents`
  byte-identical to the reference; validates against `TriageOutputContract`.

**Why it's deterministic:** the analysis path (collect → triage → assignment →
reality-check) is pure Python pattern matching and scoring — no LLM. The only
non-deterministic element is the orchestrator LLM deciding *which tool to call
next* (routing), which does not affect analysis results. Set `LLM_MODE=none`
(the default) to keep the optional LLM phases (schema-design, synthesis) off too.

---

## 4. Gotchas already discovered (do not re-learn these the hard way)

### 4.1 Two collection entry points — use the right one

There are **two** ways collection happens in this codebase:

- `scripts/run_assessment.py::phase_collect` — parses the offline file in-process
  and writes via `ArtifactStore`. **Storage-agnostic. This is the one the ATX
  path mirrors** (see `core.py::ingest_offline_collection`).
- `src/agents/collector/handler.py::run_collector` (the ECS `entrypoint.py` path) —
  reads the offline input via **boto3 S3 directly** (`offline_parser.fetch_offline_json`),
  not through `ArtifactStore`.

The ATX tools deliberately do **not** call `LocalOrchestrator.start_job()` for
collection, because that routes to the boto3 ECS handler. They call the
storage-agnostic ingest path instead. Keep it that way.

### 4.2 One agent per image

Each existing `AGENT_TYPE` must be its own deployable subagent image. An earlier
draft merged collector+triage into one image — that was wrong. It breaks the
collect/triage seam and diverges from the existing topology. The merged files
were removed; `subagent_base.py` makes adding a new single-purpose subagent cheap.

### 4.3 SDK / container requirements (from the toolkit steering)

- Images **must** be `linux/arm64` (Bedrock AgentCore runs on Graviton). x86 fails
  at runtime with `exec format error`.
- The Dockerfile **must** register the botocore service models and create the MCP
  shim at `/home/amazon/AgentBuilderAgenticMCP/bin/agent-builder-agentic-mcp`.
  Missing either → agent stuck in STARTING. Both Dockerfiles here already do this.
- Use `AgentRuntimeServer` with `delayed_timeout=3600`, **never**
  `StatelessAgentRuntimeServer` (hard 28s timeout kills long work).
- `mcp_clients` must be a **list** (or `None`), never singular `mcp_client=`.
- Subagent class must be defined **inside** `agent_factory()` (module-level subclasses
  hang in production containers). `subagent_base.py` handles this.
- Use the cross-region model id `us.anthropic.claude-sonnet-4-6`.

### 4.4 LocalOrchestrator vs Step Functions tradeoff

`LocalOrchestrator` runs all phases in-process with `ThreadPoolExecutor` fan-out.
Your existing cloud deployment uses Step Functions + per-agent ECS tasks (true
parallelism, retries, dedicated CPU/memory). The subagent architecture restores
true per-agent isolation. Step Functions was effectively doing the orchestrator
LLM's job deterministically — AWS Transform's orchestrator now takes that role,
with each phase as a subagent.

### 4.5 Python env

The repo uses **uv** with Python 3.12 (`.python-version`). The SDK requires 3.11+.
The container Dockerfiles use `python:3.11-slim` (satisfies the floor). Locally, use
`uv run python …`. If the venv breaks with `ModuleNotFoundError: encodings`, the
venv was created with a mismatched interpreter — recreate with `uv venv`.

---

## 5. Prerequisites for deployment

1. **AWS Transform account allowlisting** *(BLOCKING — currently pending)*.
   Registration with the AWS Transform registry and invocation from the WebApp
   require the AWS account to be allowlisted by the AWS Transform team. Without it,
   `register_agent` fails and the orchestrator is not invokable from AWS Transform.
   Account in use: `123456789012`. Check status with the AWS Transform team before
   attempting a full (registered) deploy.

2. **IAM roles** — already created via `pipeline/iam-roles.yaml`:
   - `AgentCoreExecutionRole` (trusts `bedrock-agentcore.amazonaws.com`) — runs the
     container; has Bedrock, `transform-agents:*`, ECR, CloudWatch Logs, X-Ray, and
     **S3 read/write on the artifact bucket** (added via the `ArtifactBucketName` param).
   - `AWSTransformAgentInvokeRole` (trusts `prod.us-east-1.compute.elastic-gumby.aws.internal`)
     — assumed by AWS Transform to invoke the runtime.

   Redeploy the stack with the artifact bucket name:

   ```bash
   aws cloudformation deploy \
     --template-file pipeline/iam-roles.yaml \
     --stack-name aws-transform-agent-iam-roles \
     --capabilities CAPABILITY_NAMED_IAM \
     --parameter-overrides ArtifactBucketName=<your-bucket> \
     --region us-east-1
   ```

3. **S3 bucket** holding the offline collection input + artifacts. Set `S3_BUCKET`
   on the runtimes so the whole pipeline uses S3 (the factory in
   `src/storage/__init__.py` switches automatically).

4. **Container runtime** — Docker or finch for local ARM64 builds (macOS/Linux);
   Windows must use CodeBuild (`use_codebuild=True`).

5. **AWS Transform Agent Toolkit power** active in Kiro (provides the
   `deploy_agent_full_pipeline` / `build_agent_image` / `register_agent` tools).

---

## 6. Deployment runbook

### 6.1 What's testable WITHOUT allowlisting

- Build + push images to ECR.
- Deploy to Bedrock AgentCore (runtime reaches READY).
- Invoke the AgentCore runtime **directly** (`aws bedrock-agentcore invoke-agent-runtime`),
  bypassing AWS Transform.

This proves the container half of the pipeline. It does **not** prove the
AWS Transform → orchestrator → subagent A2A path — that needs allowlisting.

To deploy without registering:

```python
deploy_agent_full_pipeline(
    agent_path="src/atx_orchestrator",
    agent_name="db-modernization-collector",
    skip_registry=True,
)
```

### 6.2 Full deploy (AFTER allowlisting)

> Note on Dockerfiles: the deploy tool defaults to a file named `Dockerfile` in
> `agent_path`. `src/atx_orchestrator/Dockerfile` is the **orchestrator**. The
> collector uses `Dockerfile.collector`. If the deploy tool can't target a
> non-default Dockerfile name, build+push the collector image manually and use
> `deploy_agent_to_agentcore(image_uri=…)`, OR split each agent into its own
> subdirectory each containing a plain `Dockerfile`. (Subdirectory split is the
> cleaner long-term layout once there are several subagents.)

```python
# 1) Collector subagent — RESTRICTED, invoked by the orchestrator
deploy_agent_full_pipeline(
    agent_path="src/atx_orchestrator",
    agent_name="db-modernization-collector",
    owner_contact_info="wwso-database-modernizer",
    agent_version="1.0.0",
    job_orchestrator=False,
)

# 2) Orchestrator — PUBLIC, user-facing in the WebApp
deploy_agent_full_pipeline(
    agent_path="src/atx_orchestrator",
    agent_name="db-modernization-orchestrator",
    owner_contact_info="wwso-database-modernizer",
    agent_version="1.0.0",
    job_orchestrator=True,
    chat_ui_label="DB Modernization Assessment",
)
```

Set these env vars on both runtimes (via the AgentCore runtime config):

```
S3_BUCKET=<your-bucket>
AWS_REGION=us-east-1
LLM_MODE=none
MODEL_ID=us.anthropic.claude-sonnet-4-6
```

### 6.3 Upload a test fixture

```bash
aws s3 cp artifacts/discourse/15e6403d/uploads/collector-output.json \
  s3://<your-bucket>/discourse/<job-id>/uploads/collector-output.json
```

Then invoke the orchestrator from AWS Transform with `job_id` + `database_name=discourse`
and confirm it writes `discourse/<job-id>/collector/output.json` and reports COMPLETED.

---

## 7. Immediate next task: A2A wiring (Phase 2)

**This is the most valuable un-blocked work and does not require AWS spend.**

Right now the orchestrator's `run_collect` tool calls the collector logic
**in-process** (via `core.run_collect_core`). For a true subagent architecture the
orchestrator must instead **invoke the deployed collector subagent over A2A**:

1. In `tools.py` (or a new `a2a.py`), implement the fire-and-forget + poll pattern
   from the toolkit's `orchestrator-patterns.md`:
   - `send_message(agent_instance_id, params={"message": {...}})`
   - expect A2A `SendMessage` to return error `-32603` after ~25s (normal — keep going)
   - poll `get_agent_instance` until `COMPLETED`
   - extract result from `agentOutput.serializedPayload`
2. Build the `stepLabel → stepId` mapping after `put_job_plan` (PutJobPlan assigns
   its own step ids — the label you send is not the id).
3. Use `execution_groups` (list of dicts) for fan-out phases later, not a flat dict.
4. Unit-test the message construction + payload parsing locally with a stubbed
   Agentic API before deploying.

The collector subagent already emits the correct COMPLETED payload
(`{"response": <summary>}` via `update_status(..., agent_output=…)`), so the
orchestrator just needs to send → poll → parse.

---

## 8. Future roadmap (beyond the PoC)

- Build the remaining subagents, one image each, mirroring existing `AGENT_TYPE`s:
  `triage` (core ready), `analysis-*`, `assignment-resolver`, `reality-check`,
  `schema-design`, `synthesis`.
- Orchestrator uses `execution_groups` for parallel fan-out (analysis × N engines,
  schema-design × N engines) — restoring the parallelism Step Functions provided.
- Human-in-the-loop review gates become natural AWS Transform chat turns (e.g.
  "analysis done, here are the selected engines — proceed?").
- Decide long-term: retire Step Functions in favor of the AWS Transform orchestrator,
  or keep both paths (cloud SFN for production throughput, AWS Transform for the
  conversational experience). They share the S3 artifact layout, so a job started
  by either is readable by the other.

---

## 9. Quick reference

| Thing | Value |
|---|---|
| Owner | `wwso-database-modernizer` |
| AWS account | `123456789012` |
| Region | `us-east-1` |
| Orchestrator image | `db-modernization-orchestrator` |
| Collector image | `db-modernization-collector` |
| Execution role | `AgentCoreExecutionRole` |
| Invoke role | `AWSTransformAgentInvokeRole` |
| IAM template | `pipeline/iam-roles.yaml` |
| Reference job | `artifacts/discourse/15e6403d/` |
| Registry endpoint (prod) | `https://iad.prod.agent-registry-external.elastic-gumby.ai.aws.dev` |
| Blocking prerequisite | AWS Transform account allowlisting |
