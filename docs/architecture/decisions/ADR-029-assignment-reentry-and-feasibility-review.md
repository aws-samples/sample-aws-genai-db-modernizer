# ADR-029: Assignment Re-Entry and Post-Gate Feasibility Review

**Status:** Proposed
**Date:** 2026-08-27
**Deciders:** Database Modernizer Assessment Architecture Team
**Related ADRs:** ADR-028 (Customer Assignment-Review Gate), ADR-027 (Consolidate the Schema-Design Fleet), ADR-026 (Reality Check in the Assessment Core), ADR-018 (Assignment Resolution)

---

## Context

ADR-028 shipped the customer assignment-review gate between Reality Check and
Schema Design, plus the provenance + staleness groundwork (one
`resolve_effective_assignment_version`, a `source` provenance stamp, and the
`stale_schema_versions` primitive). Its Future Work called out the capability
this ADR delivers:

> Re-entry into the assignment phase after schema design, using staleness-driven
> re-dispatch of only the affected engines.

Two gaps remain after ADR-028, both surfaced in live testing and customer
feedback:

1. **The gate is one-shot.** Once the customer approves and schema design runs,
   there is no supported way to change routing and have only the affected work
   re-run. The `stale_schema_versions` primitive exists but nothing consumes it,
   and there is no mechanism to copy an unaffected engine's schema output forward
   to a new assignment version, so any re-route today would mean re-running the
   whole expensive fleet (ADR-027) or leaving stale schema/synthesis artifacts
   behind. Separately, `get_synthesis_report` reads an unversioned
   `synthesis/report.json` that no writer produces (the writer emits
   `synthesis/v{N}/report.json`), so the reader is always empty.

2. **The gate accepts routings that cannot work, silently.** When a customer
   edits routing at the gate, `apply_assignment_overrides` carries
   `co_dependency_groups` and `table_assignments` forward verbatim (a
   `model_copy` that only updates the query assignments), so those derived views
   go stale against the new routing. The validator *does* recompute co-dependency
   splits fresh and emit `WARNING [HIGH]` strings, but they land in
   `validation_warnings` on the artifact and are never surfaced to the customer.
   And there is no check at all for the failure mode a customer raised directly:
   moving a table's **reads** to one engine while its **writes** stay on another.
   Without a replication pattern (CQRS / materialized view / zero-ETL), the read
   engine serves stale or absent data. Nothing detects or explains that.

The data needed to close gap 2 already exists: `QueryPattern.query_type`
(`SELECT` / `INSERT` / `UPDATE` / `DELETE` / `MERGE` / `OTHER`) classifies each
query as read or write, `tables_accessed` lists the tables it touches, and
`ENGINE_CAPABILITIES` (in `reality_check.py`) records that only `aurora_*` serve
`complex_joins`. `reality_check.py` also already defines an
`ARCHITECTURAL_PATTERNS` catalog (CQRS, materialized view, event-driven sync)
with DynamoDB->OpenSearch zero-ETL examples we can quote back to the customer.

**How co-dependency is enforced today (and where it breaks).** `co_dependency_groups`
(JOIN-based, union-find over significant JOINs) is consumed at **assignment
time**: `assignment_resolver` assigns each group atomically to one engine, so
co-dependent queries are co-located. Schema design does **not** read
`co_dependency_groups` — it consumes only the per-engine query set and re-derives
its own table clustering from FKs, aggregates, and co-access patterns
(`group_splitter.py`). So in the auto pipeline, related queries are modeled
together by co-location, and this is not silently broken. It breaks precisely
when a **customer override at the gate splits a group across engines**: schema
design then designs each engine's slice independently, a cross-engine JOIN
becomes infeasible (on a non-`complex_joins` engine) or an implicit distributed
join, and today only a buried `validation_warnings` string records it — while
`co_dependency_groups` itself is carried forward stale. Closing that is the point
of Layers B and C.

## Decision

Deliver re-entry and a feasibility reviewer in three layers on one branch. Layer
A makes re-entry mechanically correct and cheap; Layer B stops the gate from
recording routings that are internally inconsistent; Layer C tells the customer
when a routing will not work and makes them fix it or knowingly accept it.

### A. Staleness-driven re-entry (mechanics)

Re-entry reuses the existing gate rather than adding a parallel flow.

- **Reopen.** A re-entry entry point flips the `.meta` `ASSIGNMENT_REVIEW` phase
  from `COMPLETED` back to `AWAITING_REVIEW` and re-presents the effective
  assignment (the existing `present_assignment_review` /
  `open_detailed_routing_review` tools already read the effective version and
  work post-schema). Customer edits flow through the existing
  `finalize_assignment_review` -> `apply_assignment_overrides` path, appending a
  new `customer_gate` version `vN+1` exactly as a first-pass edit does.

- **Diff.** `assignment_engine_diff(previous, effective)` compares the set of
  in-scope queries routed to each engine between the version schema was last
  built at and the new effective version. An engine is **affected** if its
  in-scope query set changed (a query arrived, left, or its scope flipped);
  otherwise it is **unaffected**.

- **Re-dispatch (deterministic, not LLM-driven).** A new
  `redispatch_after_reroute` tool:
  1. **Copies forward** each unaffected engine's schema output from
     `schema-{engine}/v{prev}/schema_output.json` to
     `schema-{engine}/v{new}/schema_output.json`, restamping the embedded
     `assignment_version` to `new`. An engine whose in-scope query set is
     unchanged produces an identical schema, so re-running it would waste an
     LLM-heavy pass for a byte-identical result.
  2. **Re-runs** schema design only for affected engines, through the same A2A
     invoke path the six per-engine tools already use.
  3. **Re-runs synthesis**, which reads every engine's schema output at the
     effective version and writes `synthesis/v{new}/report.json`.

  This is deterministic and owned by the tool, not by the LLM choosing which
  per-engine tools to call, so re-entry re-dispatch is reproducible.

- **Correctness.** `co_dependency_groups` is consumed at **assignment time** —
  `assignment_resolver` assigns each group atomically to a single engine, which
  is how co-dependent queries are kept together — **not** at schema design.
  Schema design consumes only the queries routed to an engine (via
  `filter_collector_for_assignment`, plus collector/analysis) and re-derives its
  own table clustering from FKs, aggregate recommendations, and co-access
  patterns (`group_splitter.py`), computed fresh each run. So an engine whose
  in-scope query set is unchanged produces a byte-identical schema, which makes
  copy-forward sound; only affected engines plus synthesis re-run. This
  **per-engine-query-set invariant** — not any claim that `co_dependency_groups`
  is unused — is the load-bearing assumption behind staleness-driven re-dispatch.

- **Fix `get_synthesis_report`.** Resolve the effective version and read
  `synthesis/v{N}/report.json` (falling back to the legacy
  `referee-synthesis/report.json`), matching what the writer produces.

### B. Keep the gate honest (cheap, high value)

- **Recompute derived views on override.** In `apply_assignment_overrides`,
  recompute `co_dependency_groups` (via `build_co_dependency_groups` over the
  collector output) and `table_assignments` (via `derive_table_assignments` over
  the new query assignments) instead of carrying the previous version's forward.
  The same stale carry-forward exists in `reality_check_handler`'s revised-write
  path and is corrected there too.

- **Surface the buried warnings.** `finalize_assignment_review` returns the
  `validation_warnings` produced for the new version (the co-dependency-split
  `WARNING [HIGH]` strings the validator already computes) so the orchestrator
  presents them to the customer instead of leaving them only on the artifact.

### C. Post-gate feasibility reviewer

A reviewer runs on the customer-edited assignment **after** `finalize` applies it
(on both first-pass and re-entry), producing structured, human-facing findings.
It is the "reviewer once the customer finishes the HITL gate" the customer asked
for.

**The reviewer pushes back out loud.** A silent pipeline that runs to completion
and then fails in production is worse than one that stops and says so. Customers
expect real expert pushback from this tool: when a routing will not work, the
reviewer states plainly that the customer's choice is not feasible, names the
specific table/queries and engines involved, and explains why — rather than
proceeding quietly. It does not auto-fix (that would override the customer's
explicit decision), but it will not let an infeasible routing pass unremarked.

- **Read/write split.** For each table, classify the queries touching it into
  writes (`INSERT` / `UPDATE` / `DELETE` / `MERGE`) and reads (`SELECT`) via
  `query_type`, and record the engine each is routed to. If the table has writes
  on engine set `W` and a read routed to an engine **not** in `W` (its reads are
  served somewhere its writes never land), and no replication is acknowledged,
  emit a **blocking** finding: reads on B depend on writes on A, which requires a
  replication / CQRS / materialized-view pattern (quoting `ARCHITECTURAL_PATTERNS`,
  e.g. DynamoDB->OpenSearch zero-ETL) or moving both to one engine.
  (`query_type` `OTHER` / null is unclassifiable and skipped.)

- **Capability-aware co-dependency split.** For each co-dependency group split
  across engines, if at least one destination engine lacks `complex_joins`
  (`ENGINE_CAPABILITIES` — only `aurora_*` have it), escalate the existing
  advisory warning to a **blocking** finding: the JOIN cannot be served where it
  landed. A split where every side is join-capable stays **advisory**.

- **Gate behavior.** Blocking findings **loop the gate**: `finalize` does not
  mark `ASSIGNMENT_REVIEW` `COMPLETED`; it re-presents the findings and keeps the
  phase `AWAITING_REVIEW`, so the customer must either fix the routing or
  **explicitly accept** the risk (an acceptance flag passed to `finalize`, which
  records the accepted findings on the artifact for audit). Advisory findings
  inform and proceed. The customer always decides; the gate never silently ships
  a broken split, and never overrides the customer's explicit choice.

- **Inform, do not auto-fix.** The reviewer does not re-score or pull a whole
  co-dependency group along to follow a moved query. That would fight the
  customer's explicit routing decision. It informs precisely and lets the
  customer decide.

## Implementation surface

- **`src/storage/assignment_versioning.py`** — `assignment_engine_diff(prev,
  effective)` returning affected/unaffected engine sets; consume
  `stale_schema_versions` from the re-dispatch path.
- **`src/agents/referee/assignment_overrides.py`** — recompute
  `co_dependency_groups` + `table_assignments` in `apply_assignment_overrides`
  (Layer B).
- **`src/agents/referee/reality_check_handler.py`** — recompute the same two
  derived views in the revised-assignment write (Layer B).
- **New `src/agents/referee/feasibility_review.py`** — `review_assignment_feasibility(assignment, collector_output)`
  returning structured findings (read/write split + capability-aware co-dependency
  split), each tagged blocking/advisory with remediation text (Layer C).
- **`src/atx_orchestrator/tools.py`** — a re-entry entry point (reopen the gate);
  `redispatch_after_reroute` (copy-forward unaffected engines, re-run affected +
  synthesis); run the feasibility reviewer inside `finalize_assignment_review`
  and loop the gate on blocking findings; return `validation_warnings`; add the
  acceptance flag; fix `get_synthesis_report` to read the versioned path.
- **Schema output copy-forward** — read `schema-{engine}/v{prev}`, restamp
  `assignment_version`, write `schema-{engine}/v{new}` (no agent invoke).

## Contract impact

- **`Assignment` contract** — record accepted feasibility findings on the
  artifact (a small list with the finding id/severity and an accepted flag) so an
  approved-with-known-risk routing is self-describing. Optional and defaulted for
  backward compatibility; bump the model contract version and add round-trip
  tests.
- **Feasibility findings** — a new small typed structure (finding kind, severity,
  table/engines involved, message). Pin it with a contract test.
- **Downstream provenance** — synthesis already stamps `assignment_version`
  (ADR-028); copy-forward must preserve/restamp `assignment_version` on schema
  outputs so staleness stays checkable.
- **Contract tests** — add feasibility-reviewer tests (read/write split found;
  reads-follow-writes clean; capability-aware co-dep blocking vs advisory);
  assignment round-trip with accepted findings; re-entry diff/copy-forward unit
  tests.

## Rationale

- **Cheap re-entry.** Copy-forward + affected-only re-dispatch reuses the
  provenance/staleness groundwork ADR-028 built and avoids re-running the whole
  fleet for a one-query re-route.
- **The gate stops lying.** Recomputing derived views and surfacing the existing
  warnings closes the gap between what the artifact claims and what the routing
  is, for near-zero cost.
- **Catches the failure the customer named.** Read/write split detection is the
  concrete "you moved reads to B but writes stay on A, that will not work"
  reviewer, backed by data already collected (`query_type` + `tables_accessed`)
  and capabilities already modeled.
- **Vocal pushback, not silent failure.** The tool is expected to tell the
  customer when they are wrong. Stopping the gate with a plain "this is not
  feasible, here is why" is the product behavior; running to completion and
  failing in production later is the anti-goal.
- **Respects customer intent.** Blocking findings inform and require a decision;
  they never auto-rewrite the customer's routing. Fix or explicitly accept — the
  customer decides, with the decision recorded.

## Alternatives considered

- **Re-run the whole pipeline on any re-route — rejected.** Correct but wastes
  the expensive schema fleet for engines whose routing did not change; the
  per-engine invariant makes copy-forward safe.
- **LLM chooses which engines to re-dispatch — rejected.** Non-deterministic and
  hard to test; `redispatch_after_reroute` computes the affected set from the
  assignment diff.
- **Auto-fix broken splits (pull the co-dependency group, or re-score to follow
  a moved query) — rejected.** Fights the customer's explicit choice. Inform and
  let the customer decide (fix or accept).
- **Hard-block broken routings with no override — rejected.** A customer may
  genuinely intend a CQRS/replication topology; blocking findings are acceptable
  with an explicit, recorded acceptance.
- **First-class replication declaration in the model — deferred.** For now the
  explicit-accept path records that the customer acknowledged the replication
  requirement; a structured replication field is future work.

## Consequences

Positive:

- A customer can change routing after seeing schema/synthesis results and only
  the affected engines plus synthesis re-run.
- The assignment artifact's derived views are consistent with its routing.
- Infeasible routings are caught and explained before the expensive phase re-runs.
- `get_synthesis_report` works.

Tradeoffs:

- **More gate states.** `finalize` can now end `AWAITING_REVIEW` (blocking
  findings) instead of always `COMPLETED`; callers and tests must handle the loop.
- **New reviewer surface.** Read/write and capability logic must be defended by
  tests, including the unclassifiable-`query_type` and read-only-table edge cases.
- **Copy-forward correctness rests on the per-engine invariant.** If a future
  change makes schema design consume cross-engine inputs (e.g.
  `co_dependency_groups`), copy-forward would need revisiting; documented here as
  the load-bearing assumption.

## Future Work

- First-class replication/CQRS declaration on the assignment so an intended
  read/write split is modeled rather than accepted as a risk.
- Extend the feasibility reviewer with transaction-boundary and
  referential-integrity splits (Aurora-only capabilities already in
  `ENGINE_CAPABILITIES`).
