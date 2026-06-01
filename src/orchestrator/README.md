# Orchestrator

Step Functions state machine that coordinates the Database Modernizer job workflow. All agents run as ECS Fargate tasks; Step Functions manages sequencing, parallelism, retries, and the deeper-analysis loop.

## Workflow

```
RunCollector
    ↓
RunRefereeTriage          ← reads collector output, selects target engines
    ↓
RunEnginePipelines        ← Map state (MaxConcurrency=7), one iteration per engine:
  ├─ RunAnalysis           (AGENT_TYPE = engine name, e.g. "dynamodb")
  └─ RunSchemaDesign       (AGENT_TYPE = "schema-design", TARGET_TYPE = engine name)
       └─ internal PE review loop (designer + PE reviewer, up to 3 iterations)
    ↓
RunLoadTestPipelines      ← Map state (MaxConcurrency=3), per-engine load test
  └─ RunLoadTest           (AGENT_TYPE = "load-test", TARGET_TYPE = engine name)
       └─ provision → seed → k6 run → parse → teardown (15 min default)
    ↓
RunRefereeSynthesis       ← reads all analysis + schema + load test outputs
    ↓
CheckDeeperAnalysis       ← if synthesis requests deeper analysis (max 2 iterations),
    │                        loops back to RunSchemaDesignPipelines
    ↓
JobComplete
```

## S3 Artifact Paths

All artifacts live under `{database_name}/{job_id}/`:

| Agent | Path | Key Files |
|-------|------|-----------|
| Collector | `collector/` | `output.json` |
| Referee Triage | `referee-triage/` | `triage.json` |
| Analysis | `analysis-{engine}/` | `analysis.json`, `decision-trace.json`, `er-diagram.mmd` |
| Schema Design | `schema-{engine}/v{N}/` | `schema_output.json`, `design_trace.json` |
| Load Test | `load-test/v{N}/` | `results/summary.json`, `results/comparison.json`, `scripts/` |
| Referee Synthesis | `referee-synthesis/` | `report.json` |

## ECS Task Definitions

| Task Definition | vCPU | Memory | Image |
|----------------|------|--------|-------|
| Collector | 4 | 8 GB | agent |
| Analysis | 2 | 4 GB | agent |
| Schema Design | 2 | 4 GB | agent |
| Referee Triage | 2 | 4 GB | agent |
| Referee Synthesis | 2 | 4 GB | agent |
| Load Test | 4 | 8 GB | agent-load-test (includes k6) |

## EventBridge

EventBridge is used only for progress notifications (agent started, completed, errors). It does not coordinate the workflow — Step Functions handles all sequencing.

## Agent Dispatch

All ECS tasks share a single container image except load test (which has k6 installed). The entrypoint (`src/agents/entrypoint.py`) routes based on `AGENT_TYPE`:

- `collector` → `src/agents/collector/handler.py`
- `referee-triage` → `src/agents/referee/triage_handler.py`
- `{engine}` (dynamodb, documentdb, etc.) → `src/agents/analysis/handler.py`
- `schema-design` (+ `TARGET_TYPE`) → `src/agents/schema_design/handler.py`
- `load-test` (+ `TARGET_TYPE`) → `src/agents/load_test/handler.py`
- `referee-synthesis` → `src/agents/referee/synthesis_handler.py`

Exit code 0 = success (Step Functions advances). Non-zero = failure (retries or catches).

## Local Execution

### Full Pipeline

The phased test script (`scripts/test_local_phased.py`) runs the complete pipeline. Load test executes after schema design as part of the normal flow when called via the local orchestrator.

### Standalone Load Test (single engine)

```bash
# Prerequisites: k6 installed (brew install k6), AWS credentials configured
uv run python scripts/run_load_test.py <database_name> <job_id> [options]

# Examples:
uv run python scripts/run_load_test.py wordpress b70de5dc --engine dynamodb --duration 15
uv run python scripts/run_load_test.py wordpress b70de5dc --dry-run-only  # validate scripts without running
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--engine` | dynamodb | Target engine |
| `--schema-version` | latest | Schema version to test |
| `--duration` | 15 | Test duration in minutes |
| `--warmup` | 30 | Warmup seconds (excluded from metrics) |
| `--region` | us-east-1 | AWS region for DynamoDB |
| `--dry-run-only` | false | Only validate k6 scripts, skip full test |

### Load Test Lifecycle

1. **Read** — collector output + schema design from `./artifacts/`
2. **Adapt** — transform real schema output to handler format (input_adapter.py)
3. **Provision** — create DynamoDB table with GSIs (on-demand billing)
4. **Seed** — write synthetic data (entity relationships, capped at 10K items/entity)
5. **Generate** — k6 JavaScript scenarios per access pattern
6. **Dry-run** — `k6 inspect` validates all scripts parse correctly
7. **Execute** — `k6 run main.js` with constant-arrival-rate executors (15 min)
8. **Parse** — per-scenario latency, cost, error rates from custom k6 metrics
9. **Write** — results to `./artifacts/{db}/{job}/load-test/v{N}/`
10. **Teardown** — delete DynamoDB table
