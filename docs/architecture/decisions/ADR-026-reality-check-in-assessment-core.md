# ADR-026: Fold Reality Check into the Assessment Core

**Status:** Accepted
**Date:** 2026-08-27
**Deciders:** Database Modernizer Assessment Architecture Team
**Related ADRs:** ADR-025 (Consolidate the Deterministic Core), ADR-024 (Consolidate the Analysis Fleet), ADR-018 (Reality Check and Human Approval Gate), ADR-021 (Aurora Absorption Pass)

---

## Context

Reality Check was the last pipeline phase with no deployed ATX subagent. The
orchestrator prompt literally told the model it was "NOT AVAILABLE". ADR-018
introduced two separate things that are easy to conflate:

- The Reality Check **agent**: a CTO-level engine-consolidation step that runs
  after assignment and before schema design. For each assigned engine it asks
  whether that engine earns its place or whether a surviving engine can absorb
  its queries, then reassigns and reports. It writes `reality-check/output.json`
  always, and a new `assignment/v2/assignment.json` only when it consolidates.
- The **human approval gate**: a separate phase (`ASSIGNMENT_REVIEW`) that pauses
  the pipeline for human review before schema design. This gate lives in the
  orchestrator/state layer, not in the reality-check handler.

Investigation of `src/agents/referee/reality_check_handler.py` established three
facts that shape this decision:

1. The reality-check agent runs start to finish and returns. It has no in-process
   human wait. The "awaiting_llm" handshake exists only in `llm_mode="external"`
   and is resolved by a separate invocation, not a mid-run pause.
2. `llm_mode` selects the enrichment: `none` is fully deterministic; `bedrock`
   adds an LLM pass that validates the consolidations and writes a CTO executive
   summary; `external` is the out-of-band handshake.
3. The human gate is a distinct phase the reality-check agent neither triggers
   nor depends on.

So the reality-check agent is fully compatible with the fire-and-forget A2A
model, and porting it does not require solving the human gate.

Two problems also surfaced with the current ATX flow:

- ADR-025's `deterministic-core` agent stops at assignment, so the consolidation
  step that ADR-018 designed to run before the expensive schema-design phase was
  simply missing from ATX.
- The ATX orchestrator hardcoded `assignment_version=1` in its prompt and passed
  it to the schema-design and synthesis tools. That is fragile and, worse, it
  means a consolidated `v2` assignment would be ignored. The REST API does not do
  this: `src/api/routes/assignments.py::_latest_assignment_version` resolves the
  latest version by listing the `assignment/` prefix.

## Decision

### 1. Fold Reality Check into the core agent

Add Reality Check as the final phase of the consolidated core agent, which now
runs Collect -> Triage -> Analyze -> Assign -> **Reality Check** in one process.
It sits exactly where the DAG puts it (after assignment, before schema design),
and running it in-process keeps the whole front-half a single A2A round-trip.

Reality Check runs with `llm_mode="bedrock"`: the deterministic consolidation is
always applied, and the Bedrock pass validates the consolidations and produces
the CTO executive summary. This is precedented by Synthesis, which already makes
a Bedrock call inside an ATX subagent.

### 2. Rename the agent to `assessment-core`

Because the core now makes a Bedrock call, "deterministic-core" is no longer
accurate. Rename it to `assessment-core`: the core assessment stage that produces
the consolidated per-engine assignment, ahead of design and reporting. The
pipeline now reads as **assess (assessment-core) -> design (schema) -> report
(synthesis)**. The rename covers the AGENT_TYPE, the subagent module, the fleet
entry, the A2A tool, the container directory, and the orchestrator prompt.

### 3. Resolve the assignment version in Python, not the prompt

Remove `assignment_version` from the prompt and from the schema-design and
synthesis tool signatures. Add `core._resolve_assignment_version(store, job_id,
database_name)` mirroring the REST API's `_latest_assignment_version`, and have
the schema-design and synthesis tools resolve the latest version themselves.
The LLM no longer supplies or reasons about the version.

This is what makes Reality Check effective rather than cosmetic: when it writes
`assignment/v2/`, schema design and synthesis automatically operate on the
consolidated engine set, delivering the ADR-018 cost saving (fewer engines
designed). When Reality Check does not consolidate, the latest version is still
`v1` and nothing changes.

### 4. Defer the human gate and the external LLM mode

The `ASSIGNMENT_REVIEW` human approval gate and `llm_mode="external"` both need
mid-run human or LLM input, which the current A2A model (submit, poll, terminal;
no streaming, no mid-run input) does not support. Both are deferred. Reality
Check runs to completion without either.

## Rationale

- **Value.** Reality Check is the step that trims engine count before the most
  expensive phase. Folding it in, with the version fix, means ATX finally
  designs schemas for the consolidated set rather than the raw assignment.
- **No new runtime.** Folding into the core keeps the fleet at nine runtimes and
  the front-half at one A2A round-trip, consistent with ADR-025. Reality Check is
  sequential after assignment anyway, so a separate runtime would only add a
  cold start and a round-trip.
- **Correctness.** Resolving the version in Python removes an entire class of
  prompt-driven fragility and matches how the REST API already behaves.
- **Precedent.** Bedrock-in-subagent is already how Synthesis works, so the LLM
  pass needs no new infrastructure.

## Consequences

Positive:

- Reality Check ships in ATX; consolidation actually reduces the engines that
  get designed and reported; version handling is deterministic and API-aligned.

Tradeoffs:

- The core is no longer purely deterministic: it makes one Bedrock call at the
  end. Cost and latency rise by that one call (bounded, like Synthesis). Analysis
  stays hardcoded to `llm_mode="none"`.
- Renaming orphans the deployed `dbmod-<env>-deterministic-core` runtime; it must
  be reaped, like prior renames. The new `dbmod-<env>-assessment-core` deploys
  fresh. The runtime name fits the 48-character cap.
- Retry granularity is unchanged from ADR-025: a failure re-runs the core chain,
  which is safe and idempotent (Reality Check included).

## Future Work

- Port the `ASSIGNMENT_REVIEW` human approval gate once the A2A model supports
  mid-run input, or model it as a distinct WebApp interaction outside the agent.
- Revisit `llm_mode="external"` only if an out-of-band review workflow is needed
  in ATX.

This ADR supersedes ADR-025's "deterministic-core" naming and its "stop before
Reality Check" boundary: the boundary now falls after Reality Check, and the
agent is named for what it produces rather than for being purely deterministic.
