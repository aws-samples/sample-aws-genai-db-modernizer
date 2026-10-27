# AWS Transform Integration Guide

A reader's overview of how the database modernization pipeline runs on AWS Transform. The code is in `src/atx_orchestrator/`. This guide explains the shape and the ideas; the fleet, the tools and the files are kept in one place, the module's [`README.md`](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/src/atx_orchestrator/README.md), so they are not listed twice.

**Audience:** developers and solutions architects picking up the AWS Transform integration.

## What this is

AWS Transform runs an LLM orchestrator that coordinates a workflow by invoking purpose-built subagents. This integration maps the existing pipeline onto that model without rewriting it: the orchestrator drives a small fleet of agents, and each agent calls the same deterministic Python the local pipeline (`LocalOrchestrator`) uses, through shared functions in `core.py`.

The hard constraint is unchanged from the rest of the project: the analysis stays deterministic and auditable. No model decides which engine a table or query goes to (ADR-031). The model-backed steps are the Reality Check consolidation review, the post-gate feasibility reviewer (it advises and can block, but never reroutes) and the synthesis executive summary. Only the orchestrator's own choice of the next tool is non-deterministic.

## The fleet

One container image (`Dockerfile.atx`) can run any agent. At startup `atx_entrypoint.py` reads `AGENT_TYPE` and serves the matching factory; the AWS Transform runtime provisions one instance per agent, and the orchestrator invokes the others over A2A (agent-to-agent). There are four agents:

| `AGENT_TYPE` | Does |
|---|---|
| `orchestrator` | The chat-facing LLM orchestrator. Owns the tools, the progress plan and the assignment-review gate |
| `assessment-core` | The whole front half in one agent: Collect, Triage, Analyze (every triage-selected engine), Assign and Reality Check (ADR-024, ADR-025, ADR-026) |
| `schema` | Schema design for any engine, one parametrized agent invoked once per target engine in parallel (ADR-027) |
| `referee-synthesis` | The synthesis report and the customer deliverables |

```text
AWS Transform WebApp
     |  MCP / A2A
     v
orchestrator
     |-> assessment-core      -> collector, triage, analysis-<engine>, assignment, reality check
     |-> assignment-review gate (orchestrator tools; the one required pause, ADR-028)
     |-> schema (per engine)  -> schema-<engine>/v<N>/schema_output.json
     '-> referee-synthesis    -> synthesis/v<N>/report.json and deliverables/
```

Every agent reads and writes through the `ArtifactStore` abstraction: a local directory (`ARTIFACT_DIR`) for testing, or an S3 bucket (`S3_BUCKET`) in the cloud. A job started by `LocalOrchestrator` or by the AWS Transform path has the same artifact layout ([contracts/artifacts.md](../contracts/artifacts.md)), so either can read the other's job.

## The flow, with its tools

The orchestrator has one tool per step. The names below are in `src/atx_orchestrator/tools.py`; the README has the full table.

1. **Plan and upload.** `declare_plan_and_request_upload` runs at job start, before any customer turn: it registers the plan with the WebApp progress panel and raises the collection-upload panel. Once the customer has uploaded and named the database, `finalize_collection_upload` records the collection as the assessment's input.
2. **Front half.** `run_assessment_core_via_a2a` runs `assessment-core`.
3. **Assignment review (the one gate).** `present_assignment_review` shows the plan, `open_detailed_routing_review` opens the detailed routing table on request, and `finalize_assignment_review` records the decision and runs the feasibility reviewer. A BLOCKING finding loops the gate (status `infeasible`) until it is fixed or explicitly accepted (ADR-028, ADR-029).
4. **Schema design.** `run_schema_design_<engine>_via_a2a` invokes `schema` once per engine, concurrently.
5. **Synthesis.** `run_synthesis_via_a2a` produces the report and renders the executive report PDF, the engineering report and the assessment data, publishes them to the WebApp Artifacts panel and keeps a durable copy as the job's `deliverables/` folder. Each file names the code commit it was rendered from; a deployment with no git checkout sets it with `MODERNIZER_BUILD_COMMIT` (otherwise the files say `unknown`).
6. **Re-entry (ADR-029).** After a report exists, `reopen_assignment_review`, then `finalize_assignment_review` on the edit, then `redispatch_after_reroute` re-runs only the engines whose routing changed (unchanged engines' schema is copied forward from `v<N>` to `v<N+1>`), and synthesis runs again.

`get_job_status` reports per-phase progression and `get_synthesis_report` returns the finished report.

## How the orchestrator drives a subagent (A2A)

All phase tools go through one primitive, `invoke_and_wait(agent_id, message)` in `a2a.py`. Given a registered agent name, it:

1. **Discovers or spawns** the instance: it looks for a pre-provisioned one by name with `list_agent_instances`, and otherwise spawns one with `invoke_agent` and waits for it to be running.
2. **Sends** the work with `send_message`. A `-32603` ("Internal error") after about 25 seconds is normal for a long-running subagent; the container is still processing, so the primitive keeps polling.
3. **Polls** `get_agent_instance` until the status is `COMPLETED` or `FAILED`, then parses the result payload into a dict.

The tools are thin: each builds the message and delegates the invoke, progress marking and error handling to a shared helper. A failure comes back as a JSON error dict, not an exception, so the orchestrator model can read and relay it. The model only ever knows subagent names; instance resolution happens inside `invoke_and_wait`.

## Determinism and the reference job

The integration is checked against a reference job: a Discourse run (311 tables, 1654 queries), `artifacts/discourse/15e6403d/`. The contract and tool scripts feed its offline collection through the AWS Transform code path and diff the result against the reference's collector and triage output, ignoring only volatile fields (timestamps and the job id). A change to this integration must keep that reproduction identical.

`artifacts/` is gitignored, so the reference job is not in the repository. The scripts read it from a local checkout and stop with "Missing reference fixture" when it is absent (`scripts/atx_contract_test.py`). A maintainer holds the reference; without it, `atx_smoke_test.py` still checks imports and tool wiring.

## Running locally (no AWS, no Docker)

```bash
uv run python scripts/atx_smoke_test.py       # imports + tool wiring
uv run python scripts/atx_contract_test.py    # raw handlers reproduce the reference
uv run python scripts/atx_tool_test.py        # orchestrator tool path reproduces the reference
uv run python scripts/atx_subagent_test.py    # assessment-core reproduces the reference
```

The unit and contract suites run in an environment without the AWS Transform SDK, as CI does:

```bash
uv run --no-sync pytest tests/unit/ tests/contract/ --cov=src
```

## Extending: adding a new subagent

Adding a phase touches four places, mirroring the existing subagents:

1. A subagent module exposing `SYSTEM_PROMPT`, a `_work(params)` function that calls into `core.py`, and `agent_factory = make_subagent_factory(...)`.
2. A row in `_AGENTS` and a dispatch branch in `atx_entrypoint.py`.
3. A storage directory created and owned in `Dockerfile.atx` (the entrypoint row and the Dockerfile directory have no compile-time guard; keep them in sync).
4. An orchestrator tool in `tools.py` that calls `invoke_and_wait`, plus its registration in `orchestrator.py`.

Prefer a parametrized agent over copy-paste when a phase has per-engine variants: `schema` takes `target_type` from the invocation payload (ADR-027), and `assessment-core` drives every analysis from one table-driven core.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `AGENT_TYPE` | (required) | Which agent this container serves; no default |
| `ARTIFACT_DIR` | `/app/artifacts` | Local artifact directory (used when `S3_BUCKET` is unset) |
| `S3_BUCKET` | (unset) | Set to use S3 instead of the local filesystem |
| `MODEL_ID` | the `ATX_AGENT` role default in `src/shared/models.py` | Orchestrator and subagent model (a cross-region inference profile) |
| `AWS_REGION` | `us-east-1` | AWS region |

## Build and deploy

The image must be `linux/arm64` (Bedrock AgentCore runs on Graviton); an x86 image fails at runtime with `exec format error`.

```bash
finch build --platform linux/arm64 \
  -t db-modernization-atx:latest \
  -f src/atx_orchestrator/Dockerfile.atx .
```

`AGENT_TYPE` is set per AgentCore runtime, so this one image backs every agent. Deployment is driven by `pipeline/atx_deploy.py` (verbs `build`, `apply`, `destroy`, `status`; run it with `--help`); the module README describes a personal fleet and the AgentCore requirements.

## Related documentation

- Module reference (the fleet, the files, the tools): [`src/atx_orchestrator/README.md`](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/src/atx_orchestrator/README.md)
- The decisions: ADR-024 to ADR-029 in the [ADR index](../architecture/decisions/README.md)
- Artifact layout: [contracts/artifacts.md](../contracts/artifacts.md)
- Agent contracts: [../contracts/agent-contracts-spec.md](../contracts/agent-contracts-spec.md)
