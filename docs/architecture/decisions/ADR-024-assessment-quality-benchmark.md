# ADR-024: Assessment Quality Benchmark

**Status:** Proposed
**Date:** 2026-07-23
**Deciders:** Database Modernizer Assessment Architecture Team

## Context

The pipeline produces engine assignments, schema designs, risk assessments, and
load-test numbers with no ground truth. Today the output is trusted as-is. That
leaves three questions unanswerable:

1. Is a given run's output good or bad?
2. Did a change to agent code or prompts improve quality or regress it?
3. Is one approach measurably better than another?

Every change the project makes is currently unmeasured. Before investing in
larger initiatives (a new compute substrate, a new UI, scoping improvements),
each of which claims to make the tool "better", we need an instrument that turns
"better" from an assertion into a measurement.

This ADR is **Proposed**: a first working version (the assignment stage) is
built to learn from, but the approach is not ratified. Team input is being
gathered via a tracking issue. It may be accepted, revised, or superseded.

## Decision

Introduce a **staged assessment-quality benchmark** that scores each pipeline
stage's output against human-reviewed reference answers, mirroring the pipeline
structure (triage → assignment → schema → synthesis). It is additive: it reads
the same artifacts the pipeline already produces and invokes existing agent
handlers; no pipeline or agent code changes.

### Principles

1. **The dataset is the asset, the runner is disposable glue.** Following
   SWE-bench/MMLU, the durable value is the cases + answer keys, not the harness.
   Cases live under `benchmarks/cases/<stage>/<case>/` (input artifacts +
   `expected.json`), catalogued in a manifest. The runner is thin enough to
   rewrite if the tool's architecture changes; the dataset survives.

2. **Two modes, chosen per stage.**
   - *Deterministic-regression* for rule-based stages (engine assignment =
     `AssignmentResolver`, no LLM): same gold inputs → same output; the score
     should be ~100% and a drop is a code regression. Runs once, no AWS, CI-able.
   - *LLM-quality* for LLM-driven stages (triage, schema, synthesis): run the
     real agent (real Bedrock), validate against acceptable-sets, run N times,
     report variance. On-demand, costs money.
   Both share the case format, answer-key schema, scoring core, and
   failure-handling. They differ only in the stage adapter and run count.

3. **Acceptable-set scoring, not single answers.** Each query maps to a set of
   acceptable engines (a KV lookup legitimately fits DynamoDB or ElastiCache).
   An optional `ideal` field enables a secondary "best-match" rate. A `rationale`
   field documents the human reviewer's reasoning.

4. **Staged (gold-input) isolation, not end-to-end for v1.** Each stage is fed
   the reference (gold) output of upstream stages, never a live upstream run.
   This prevents error amplification (an early mistake making a later, correct
   agent look wrong) and isolates which agent regressed. An end-to-end
   (predicted-input) benchmark is valuable and deferred; the two coexist and
   answer different questions ("is this agent good" vs. "is the product good").

5. **Infrastructure failures are never scored as quality.** A Bedrock throttle
   or agent crash is quarantined (its own outcome category), retried where
   transient, and excluded from the score's denominator, never counted as a
   wrong answer. Any run with an unresolved throttle/error is flagged
   "INCOMPLETE — re-run before trusting these numbers." A partial run can never
   be mistaken for a clean one.

6. **Human-authored, human-reviewed cases only.** No synthetic generation:
   grading the tool against another model's output is circular. Cases are
   hand-authored clean workloads (each shaped to isolate an engine) plus trimmed,
   labeled real examples. An answer key counts only when a human sets
   `reviewed: true`.

### What v1 delivers

- Harness: case loader + manifest, acceptable-set scoring, an assignment stage
  adapter, outcome-aware reporting with the trust banner, and a CLI
  (`python -m benchmarks.runner.run --stage assignment`).
- One stage end-to-end: engine assignment (deterministic-regression mode), which
  also pre-wires the throttle/outcome machinery the LLM stages will reuse.
- A seed dataset (two reviewed cases) + `benchmarks/README.md`.

### Where it lives

In-repo (`benchmarks/`), calling agent handlers directly against synthetic
offline-collection JSONs. This needs no deployment for the deterministic stage.
The dataset/runner boundary preserves the option to move the dataset to an
external repo, or to drive the deployed API, later.

## Consequences

**Positive**

- A measurable quality signal and a regression guard for rule-based logic.
- The instrument that lets other initiatives prove they improve quality.
- The dataset outlives the current architecture.

**Negative / costs**

- Authoring acceptable-sets is subjective and labor-intensive; answer keys will
  iterate. This is the main ongoing effort.
- LLM-quality stages require real Bedrock (cost, non-determinism); those runs are
  on-demand, not CI.
- Gold-input isolation means dependent stages need both an expected output and a
  gold input authored (the schema/synthesis stages, when added).

**Risks / open questions**

- What constitutes a fair acceptable-set per workload type.
- How many cases, and what clean-vs-real mix, before an aggregate is trusted.
- For LLM stages: how many runs constitute a trustworthy measurement.
- Whether the dataset should eventually be its own repository.

## Alternatives considered

- **End-to-end-only benchmark.** Rejected as the *sole* approach: it can't
  localize which agent regressed and amplifies upstream error. Retained as a
  deferred, complementary benchmark.
- **Single expected engine per query.** Rejected: over-penalizes defensible
  alternative choices, producing noisy, unfair scores.
- **Synthetic/LLM-generated cases and answer keys.** Rejected for v1: grading the
  tool against a generator's output is circular and untrustworthy without human
  review.
- **External benchmark repo from day one.** Deferred: adds a stable-contract and
  versioning burden prematurely; the dataset/runner split keeps the option open.

## References

- Related: ADR-023 (Context Graph Layer) — its provenance data (`PRODUCED_BY`,
  decision rationale) could later power explainable scoring.
- Design detail and implementation plan tracked separately (working docs).
