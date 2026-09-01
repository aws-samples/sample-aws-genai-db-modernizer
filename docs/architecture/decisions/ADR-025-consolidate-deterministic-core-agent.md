# ADR-025: Consolidate the Deterministic Core into One Agent

**Status:** Accepted
**Date:** 2026-08-27
**Deciders:** Database Modernizer Assessment Architecture Team
**Related ADRs:** ADR-024 (Consolidate the Deterministic Analysis Fleet), ADR-016 (Compute and Orchestration Strategy), ADR-007 (Referee Orchestration), ADR-018 (Reality Check and Human Gate)

---

## Context

ADR-024 consolidated the six per-engine analysis agents into a single
in-process analysis agent and proved the pattern in a deploy: fewer runtimes,
one A2A round-trip, and end-to-end latency no worse (faster, in practice)
because fan-out overhead dwarfs the millisecond-scale deterministic work. Its
Future Work section named the next step: fold the offline collector, triage,
and assignment into that agent to form a single deterministic-core agent
covering Collect, Triage, Analyze, and Assign.

After ADR-024 the ATX fleet still runs four separate deterministic runtimes for
the front-half of the pipeline: `collector`, `triage`, `analysis` (already
consolidated), and `assignment`. All four share the properties ADR-024 relied
on:

- **Deterministic.** None invoke Bedrock or an LLM in the ATX path.
  `run_collect_core`, `run_triage_core`, `run_analysis_core` (hardcoded
  `llm_mode="none"`), and `run_assignment_core` are all rule-based.
- **Strictly sequential and artifact-chained.** Each reads its predecessor's
  `<db>/<job>/...` artifacts from the ArtifactStore and writes its own. There is
  no in-memory fan-in.
- **Fast.** The whole front-half is milliseconds to low seconds of CPU work,
  well under the per-runtime cold-start and A2A poll overhead measured in
  seconds.

The single-source-of-truth core functions already exist in
`src/atx_orchestrator/core.py` and operate on the ArtifactStore, so the durable
contract is the artifact set rather than any agent boundary.

### The ordering constraint that shapes the boundary

Assignment reads every `analysis-<engine>/analysis.json`, so there is a hard
`analysis -> assignment` dependency. Analysis already sits between triage and
assignment. That single fact rules out merging collector, triage, and
assignment while leaving analysis as a separate agent: assignment must run after
the analysis agent has produced its artifacts, and a fire-and-forget subagent
cannot block mid-run waiting on another agent (only the orchestrator drives A2A
round-trips).

The only two consolidations the DAG permits are therefore:

- **Option A (intake only):** merge `collector` + `triage` into one agent, using
  the existing `run_collect_triage_core`. Assignment stays a separate agent
  after analysis. Fleet drops by one runtime.
- **Option B (full deterministic core):** absorb `collector`, `triage`, and
  `assignment` into the analysis agent, producing one agent that runs Collect,
  Triage, Analyze, Assign in-process and in order. This works precisely because
  analysis is in the same process, so the `analysis -> assignment` dependency is
  satisfied sequentially without a cross-agent wait.

## Decision

Adopt **Option B**: a single **deterministic-core agent** that runs Collect,
Triage, Analyze, and Assign in one process, in order, via the existing core
functions. This is the consolidation ADR-024 pointed at.

- A new `core.run_deterministic_core(job_id, database_name, input_key="",
  store=None, ...)` composes the existing cores in sequence:
  `run_collect_core` (or the collect step of `run_collect_triage_core`),
  `run_triage_core`, `run_all_analyses`, `run_assignment_core`. It reuses those
  functions unchanged rather than reimplementing any phase.
- Each phase writes the same artifacts it writes today
  (`collector/output.json`, `referee-triage/triage.json`,
  `analysis-<engine>/analysis.json` and its trace/diagram, and
  `assignment/v1/assignment.json`). Downstream (Schema Design, Synthesis) and
  every artifact-key test fixture receive byte-identical inputs.
- The agent reports progress by resolving the orchestrator-declared plan steps
  from the server (`register_steps_from_server`) and ticking `collector`,
  `triage`, the nested `analysis_<engine>` sub-steps, and `assignment` as it
  moves through them. This is the same cross-process progress pattern ADR-024
  proved, so the WebApp panel keeps its per-phase and per-engine granularity
  even though one runtime now drives all four boxes.
- The orchestrator exposes one tool, `run_deterministic_core_via_a2a`, replacing
  `run_collect_via_a2a`, `run_triage_via_a2a`, `run_analysis_via_a2a`, and
  `run_assignment_via_a2a`. The customer-upload discovery
  (`_discover_uploaded_input`) stays in the orchestrator process (which holds
  the Transform job context) and is passed to the consolidated tool as
  `input_key`, exactly as it is passed to the collector today.

**Boundary (the deterministic/LLM line):** stop after Assign. Schema Design (six
agents) and Synthesis stay separate LLM agents where parallelism and isolation
pay off. Reality Check keeps its LLM seam and human-in-the-loop pause (ADR-018)
and is not folded in. This is the same line ADR-024 drew, now applied to the
whole front-half.

## Rationale

- **Compute and registry footprint.** The deterministic front-half drops from
  four runtimes to one. Combined with ADR-024, the ATX fleet goes from roughly
  nine runtimes to six, with fewer cold starts, fewer invocations, and fewer
  entries against the AtxAgentRegistry per-account agent quota.
- **Latency.** The orchestrator makes one A2A round-trip for Collect through
  Assign instead of four. Because the per-runtime overhead (seconds) dwarfs the
  work (milliseconds to low seconds), the consolidated path is expected to be
  faster end-to-end, not merely cheaper.
- **Simplicity.** The orchestrator prompt and tool list describe one
  deterministic call rather than sequencing four. Fewer runtimes to build,
  register, and reason about.
- **Safety.** The durable contract is the artifact set, not an agent boundary.
  Preserving every artifact key makes the change invisible to everything
  downstream, which is what let ADR-024 ship without touching Schema Design,
  Synthesis, or their fixtures.

This continues to refine ADR-016 principle 4 ("agents own their parallelism"):
the deterministic core owns its internal phase sequence rather than being
represented as four separate runtimes.

## Alternatives considered

- **Option A (intake only), rejected as the primary step.** Merging only
  collector and triage saves a single runtime and leaves assignment as its own
  runtime after analysis. It is a strictly smaller version of Option B that does
  not complete the deterministic-core vision, and it would require a second
  consolidation later to finish the job. Option A remains the natural fallback
  if we ever need analysis and assignment to stay independently retriable as
  separate agents; in that case intake still captures the easy win.
- **Merge collector + triage + assignment (skipping analysis), rejected as
  impossible.** The `analysis -> assignment` dependency and the fire-and-forget
  subagent model make this invalid: assignment cannot run before the analysis
  agent, and a subagent cannot wait on another agent mid-run.

## Consequences

Positive:

- A single, faster, cheaper deterministic front-half; a smaller fleet; a simpler
  orchestrator contract.

Tradeoffs:

- **Retry and observability granularity.** A failure in any of the four phases
  surfaces as the deterministic-core agent failing, and a retry re-runs the
  chain from Collect. This is acceptable because every phase is deterministic,
  idempotent, and artifact-checkpointed, so re-running is cheap and safe. The
  per-phase progress ticks and clear per-phase error mapping (which phase
  failed, relayed in the payload) preserve the diagnosis path.
- **One larger agent image.** The deterministic-core agent imports the collector,
  triage, analysis, and assignment code paths. These are already in the shared
  container, so the runtime footprint is unchanged; only the logical grouping
  moves.
- **Runtime name length.** AgentCore runtime names are capped at 48 characters
  with hyphens converted to underscores. The suffix `deterministic-core` fits
  under the current environment prefixes, but the deploy path already validates
  this and will fail loudly if a longer prefix pushes it over; a shorter alias
  can be chosen at implementation time if needed.

## Future Work

- With the deterministic core consolidated, the remaining separate agents are
  the genuine LLM phases (Schema Design, Synthesis) and Reality Check. No
  further deterministic consolidation is available or intended; the LLM line is
  the stopping point.
- If independent retriability of analysis or assignment becomes a requirement,
  revisit by splitting the deterministic core back toward Option A rather than
  re-expanding to per-phase agents.
- The ER-diagram artifact divergence noted in ADR-024 (the local
  `handler.run_analysis` writes `er-diagram.json` while the ATX
  `core.run_analysis_core` writes `er-diagram.mmd`) is unaffected here: this
  change composes the existing ATX core functions and introduces no new artifact
  path. It remains a separate cleanup.
