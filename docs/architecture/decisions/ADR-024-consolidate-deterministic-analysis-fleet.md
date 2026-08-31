# ADR-024: Consolidate the Deterministic Analysis Fleet into One Agent

**Status:** Accepted
**Date:** 2026-08-31
**Deciders:** Database Modernizer Assessment Architecture Team
**Related ADRs:** ADR-016 (Compute and Orchestration Strategy), ADR-006 (Analysis Agent Patterns), ADR-017 (Analysis Agent Scoring Framework), ADR-007 (Referee Orchestration)

---

## Context

In the AWS Transform (ATX) deployment, every agent maps to its own AgentCore
runtime, and the orchestrator reaches each one over A2A (invoke + poll until
terminal). The analysis phase is modeled as six per-engine agents, one per
target engine (DynamoDB, DocumentDB, ElastiCache, OpenSearch, Aurora
PostgreSQL, Aurora MySQL), so it deploys as six runtimes and the orchestrator
makes up to six A2A round-trips.

ADR-016 established "agents own their parallelism" and workload-aware analysis
selection to avoid wasting compute, with concurrency intended to shorten
wall-clock time. For the analysis phase specifically, that goal has inverted:

- Analysis is **fully deterministic** in the ATX path. `core.run_analysis_core`
  hardcodes `llm_mode="none"`; no Bedrock/LLM is invoked. All six engines share
  one table-driven function (`_ANALYSIS_ENGINES`).
- The per-engine work is **millisecond-scale** CPU scoring.
- Hand-off is via **S3 artifacts** (`<db>/<job>/analysis-<engine>/analysis.json`),
  which the Assign phase reads independently. There is no in-memory fan-in.

Measured on the WordPress sample (triage selected 5 engines), all engines run
in a single process:

| Run mode | Total (all engines) |
|---|---|
| Parallel (ThreadPoolExecutor) | 0.069 s |
| Sequential (one after another) | 0.050 s |

Per-engine: 6–13 ms. Two observations: the total analysis work is ~50 ms, and
the parallel version is *slower* than sequential because thread overhead
dominates millisecond-scale CPU work (and the GIL prevents true multi-core
speedup regardless).

We are therefore paying for six AgentCore runtimes, each with a cold start and
an A2A poll loop measured in seconds, to parallelize ~50 ms of deterministic
work. The parallelism optimizes milliseconds while the fan-out costs seconds.

## Decision

Consolidate the six per-engine analysis agents into a **single analysis agent**
that runs all triage-selected engines in-process, sequentially, via the
existing per-engine `run_analysis_core`.

- The agent reads the triage output to decide which engines to run (triage
  already encodes the source-engine constraints, e.g. a MySQL source selects
  `aurora_mysql` and not `aurora_pg`), with an optional explicit engine list for
  override.
- Each engine still writes the same `analysis-<engine>/analysis.json` (plus
  decision trace, plus ER diagram where applicable), so Assign, Reality Check,
  and Schema Design receive byte-identical inputs.
- No ThreadPool: a plain sequential loop is simpler and, at this scale, faster.

The per-engine analyze modules, the `AnalysisInput` contract, the artifact keys,
and every downstream phase are unchanged.

**Boundary — where consolidation stops (the deterministic/LLM line):**

- **Keep separate:** Schema Design (six agents) and Synthesis are real LLM work
  where parallelism and isolation genuinely pay off. Reality Check has an LLM
  seam and, in `external` mode, a human-in-the-loop pause (`awaiting_llm`) that
  does not fit a fire-and-forget deterministic agent.
- **Consolidate:** only the deterministic, sequential, millisecond-scale,
  artifact-chained phases.

## Rationale

- **Compute.** Analysis runtimes drop from 6 to 1: fewer cold starts, fewer
  invocations, and fewer entries against the `AtxAgentRegistry` 50-agents-per-
  account quota.
- **Latency.** The orchestrator makes one A2A round-trip instead of six. Because
  the fan-out overhead (seconds) dwarfs the work (~50 ms), the consolidated path
  is expected to be *faster* end-to-end, not merely cheaper.
- **Simplicity.** The orchestrator prompt no longer coordinates a six-way
  parallel dispatch with per-engine source constraints; it calls analysis once.
  Fewer runtimes to build, register, and reason about.
- **Safety.** The durable contract is the S3 artifact set, not an in-memory
  fan-in. Preserving the per-engine artifact keys makes the change invisible to
  everything downstream.

This refines ADR-016 principle 4 ("agents own their parallelism") for the
analysis phase: the analysis agent owns its per-engine iteration internally
rather than being represented as N separate agents/runtimes.

## Consequences

Positive:

- Lower compute and registry footprint; a simpler and faster deterministic
  front-half of the pipeline.

Tradeoffs:

- **Progress granularity.** The orchestrator today ticks off per-engine job-plan
  steps as each agent completes. A single agent runs them opaquely. Mitigation:
  the consolidated agent emits per-engine `UpdateJobPlanStep` calls so the WebApp
  progress panel stays granular; otherwise progress collapses to a single
  coarser "Analysis" step.
- **Retry / observability granularity.** A failure re-runs the analysis chain
  from the top rather than a single engine. Acceptable: analysis is
  deterministic, idempotent, and artifact-checkpointed, so re-running is cheap
  and safe.

## Future Work (staged)

The same four properties — deterministic, strictly sequential, millisecond-
scale, artifact-chained — hold for the rest of the deterministic front-half.
Once this consolidation is proven in a deploy, extend the pattern:

- Fold the **offline collector + triage + assignment** into the analysis agent,
  forming a single **deterministic-core agent** covering Collect -> Triage ->
  Analyze -> Assign. This would take the ATX fleet from ~17 runtimes to ~9 and
  reduce the orchestrator's deterministic front-half to one A2A call.
- **Stop before Reality Check** (LLM seam + human-in-the-loop pause) and keep
  Schema Design (x6) and Synthesis as separate LLM agents.
- When unifying code paths, resolve the known artifact divergence: the local
  `handler.run_analysis` writes `er-diagram.json` while the ATX
  `core.run_analysis_core` writes `er-diagram.mmd`.

This ADR covers the analysis consolidation only; the deterministic-core merge
will be recorded as its own follow-up ADR when undertaken.
