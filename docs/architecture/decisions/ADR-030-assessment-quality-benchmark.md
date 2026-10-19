# ADR-030: Assessment Quality Benchmark

**Status:** Accepted (first version: assignment stage only; revisit once it has been used on real changes)
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

## Amendment 1 (2026-10-19, #1074): a final-assignment stage

The first end-to-end stage is `final_assignment`. It runs the deterministic chain (collect, triage, analysis, assignment, reality check, no model) on a whole bundled sample and scores the assignment the reality check leaves behind, which is what the deliverables show. It coexists with the gold-input `assignment` stage and uses the same key format, scoring and gate rules: an unreviewed case is skipped and never gates. Its keys are written by a human from a blind review sheet (`benchmarks/runner/review_sheet.py`) that shows the facts routing reads and never the pipeline's engine or scores, per principle 6. That a human writes each key is a process rule: the guards check only that the committed keys stay `reviewed: false` and that the sheet shows no engine or score. Principle 4 (gold-input isolation) still holds for the `assignment` stage; this stage answers the other question it names, "is the product good".

## Amendment 2 (2026-10-19, #1084): an anti-pattern stage

The `anti_pattern` stage scores the pipeline's anti-pattern verdict: a case is one bundled sample and one penalised catalog anti-pattern (`src/catalog/patterns/`), and its key says per query whether the query shows that anti-pattern (`shows`) and, optionally, which engines are acceptable for it. The stage runs the same deterministic chain as `final_assignment`; a query is correct when the pipeline's verdict (some engine's analysis reports the pattern for it; for `substring-match`, the collector-based detector) matches the key's and, where the key gives an acceptable set, the final engine is in it. A key entry without a boolean verdict errors its case only. A case's queries are the busiest flagged queries (up to 12) whose final engine moves when the pattern's penalty takes a basis point from 0 to 40, plus two unflagged decoys per deciding query, of its kind and, while the sample has one, of its access-pattern class; the guards recompute this membership for every committed case, and the sweep restores the penalty table it patches. The blind sheet (`benchmarks/runner/anti_pattern_cases.py`) shows no engine, score or pattern id in its routing columns, leaves out the access-pattern class, reason, pattern facts and shape pointers, orders rows by a hash of the case and query ids, and the guards check that no low-cardinality column (statement type, tier, group membership, collector flags) has a value only flagged rows show. The tables a row reads are not matched: for a table-level pattern every query of a flagged table is flagged. As in Amendment 1, a human writes each key: the guards check that the committed keys stay `reviewed: false` and empty and that the builder, `--sheets-only` included, never rewrites a reviewed case.

## Enforcement

| Decision | Enforced by |
|---|---|
| Amendment 1 | `tests/unit/benchmarks/test_stage_final_assignment.py::test_the_stage_scores_the_reality_checks_version_not_the_first_pass`<br>`tests/unit/benchmarks/test_stage_final_assignment.py::test_run_unreviewed_runs_but_never_scores_or_gates`<br>`tests/unit/benchmarks/test_stage_final_assignment.py::test_the_committed_stage_lists_the_three_samples_unreviewed`<br>`tests/unit/benchmarks/test_gate.py::test_every_stage_gates_exactly_its_reviewed_cases`<br>`tests/unit/benchmarks/test_stage_final_assignment.py::test_the_stage_matches_the_e2e_pipelines_final_engines`<br>`tests/unit/benchmarks/test_stage_final_assignment.py::test_an_unreviewed_case_that_errors_stays_skipped_and_never_fails_the_gate`<br>`tests/unit/benchmarks/test_review_sheet.py::test_rows_never_show_an_engine_or_a_score`<br>`tests/unit/benchmarks/test_review_sheet.py::test_committed_sheet_is_blind_and_covers_every_query` |
| Amendment 2 | `tests/unit/benchmarks/test_stage_anti_pattern.py::test_the_stage_scores_a_reviewed_fixture_case`<br>`tests/unit/benchmarks/test_stage_anti_pattern.py::test_a_different_penalty_moves_the_scored_engines`<br>`tests/unit/benchmarks/test_stage_anti_pattern.py::test_a_malformed_key_or_case_errors_the_case_not_the_batch`<br>`tests/unit/benchmarks/test_stage_anti_pattern.py::test_penalty_set_to_restores_the_catalog_value_and_the_aliases`<br>`tests/unit/benchmarks/test_stage_anti_pattern.py::test_the_committed_cases_are_unreviewed_with_no_key`<br>`tests/unit/benchmarks/test_stage_anti_pattern.py::test_run_unreviewed_never_scores_or_gates`<br>`tests/unit/benchmarks/test_anti_pattern_cases.py::test_committed_case_membership_follows_the_sweep_rule`<br>`tests/unit/benchmarks/test_anti_pattern_cases.py::test_the_selection_reproduces_the_committed_cases`<br>`tests/unit/benchmarks/test_anti_pattern_cases.py::test_penalty_set_to_refuses_a_pattern_the_scorer_has_no_penalty_for`<br>`tests/unit/benchmarks/test_anti_pattern_cases.py::test_no_sheet_column_or_position_separates_flagged_rows`<br>`tests/unit/benchmarks/test_anti_pattern_cases.py::test_the_sheet_never_shows_an_engine_a_score_a_class_or_a_flag`<br>`tests/unit/benchmarks/test_anti_pattern_cases.py::test_committed_sheet_is_blind_and_covers_the_case`<br>`tests/unit/benchmarks/test_anti_pattern_cases.py::test_the_builder_never_rewrites_a_reviewed_case`<br>`tests/unit/benchmarks/test_anti_pattern_cases.py::test_sheets_only_never_rewrites_a_reviewed_cases_sheet`<br>`tests/unit/benchmarks/test_gate.py::test_every_stage_gates_exactly_its_reviewed_cases` |

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

This ADR introduces a new capability; it does not supersede or rewrite any
existing ADR. Distinctions from adjacent ADRs, to avoid confusion:

- **ADR-017 (Analysis Agent Scoring Framework)** — that is the *runtime* scoring
  the tool uses to **produce** recommendations (per-query engine scoring inside
  the agents). ADR-030 is the opposite direction: it **evaluates** whether those
  recommendations were good, against human ground truth. ADR-017 is the thing
  being measured; ADR-030 is the measuring stick. They are complementary.
- **ADR-009 (Testing Infrastructure)** — unit/integration testing of code
  correctness. ADR-030 measures *output quality*, a separate concern from
  code-correctness tests (though the deterministic assignment benchmark can run
  in CI like a regression test).
- **ADR-023 (Context Graph Layer)** — related: its provenance data
  (`PRODUCED_BY`, decision rationale) could later power explainable scoring.
- Design detail and implementation plan tracked separately (working docs).
