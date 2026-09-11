# ADR-028: Customer Assignment-Review Gate Before Schema Design

**Status:** Proposed
**Date:** 2026-09-11
**Deciders:** Database Modernizer Assessment Architecture Team
**Related ADRs:** ADR-026 (Reality Check in the Assessment Core), ADR-027 (Consolidate the Schema-Design Fleet), ADR-018 (Assignment Resolution), ADR-016 (Compute and Orchestration Strategy)

---

## Context

The modernizer routes every query / access pattern to a target engine in the
**assignment artifact** (`<db>/<job>/assignment/v<N>/assignment.json`). Each
`QueryAssignment` already carries what a human needs to review a routing
decision: `assigned_engine`, `in_scope`, `confidence`, `assignment_reason` (the
"why"), `source_tables`, and `warnings`. Reality Check may consolidate engines
and, when it does, writes `assignment/v2` (ADR-026).

The **web** product gates before schema design: it shows the assignment for
review, lets the customer override routing (reassign an engine, drop a query
from scope, narrow tables), writes a new `CUSTOMER_MODIFIED` version, and only
then runs schema design. That gate is enforced by an explicit approval step
(`WaitForAssignmentApproval`) plus an approval marker (`.meta/<job>.json`), and
the per-query override write path already exists in
`src/api/routes/assignments.py` (applies all overrides, stamps
`customer_override=True`, writes `assignment/v{N+1}` with `previous_version`).

The **AWS Transform (ATX)** modernizer has **no such gate**. After Reality Check
the orchestrator dispatches schema design immediately, so a customer cannot see
or adjust routing before the long, LLM-heavy design phase runs. Bringing the
web gate to ATX is the primary goal of this ADR.

A second, long-standing problem surfaces the moment a customer edit can create a
new assignment version: **"which assignment version is authoritative" is
resolved as a blind `max(version)` in at least four independent places**:

- `core._resolve_assignment_version` (ATX path),
- `local_orchestrator._get_assignment_version`,
- `api/services/local_s3` (`max(versions)`),
- `graph/populators` (scans `v10 -> v1`).

Every downstream reader (schema design, synthesis, the pre-dispatch skip, the
graph) trusts "latest = highest number." That is correct only because the
pipeline is strictly linear today (v1 assign -> v2 reality-check). Adding a
customer-edit version, and later re-entry into the assignment phase, multiplies
the fragility: nothing records **which stage produced a version** or **which
version a downstream artifact was built from**, so "latest" is an assumption,
not a fact the system can verify.

## Decision

Add a **customer assignment-review gate** between Reality Check and Schema
Design in the ATX orchestrator, and consolidate assignment-version resolution
behind **one provenance-aware resolver**. Three parts.

### A. The gate (hard interrupt)

After Reality Check, the orchestrator renders the effective assignment to a
review markdown, presents it, and **stops**. It must not call any schema-design
tool until an approval artifact exists.

- Enforced by **artifact state, not prompt discipline**: a gate tool returns
  "awaiting approval" and refuses to advance the pipeline until approval is
  recorded, mirroring the web's `WaitForAssignmentApproval` +
  `.meta/<job>.json` marker. The system prompt describes the gate, but the block
  is real regardless of what the LLM decides.
- Two ways to clear the gate:
  1. **Approve / light chat edits** — the customer replies "looks good", or asks
     for a small change in chat ("move the full-text queries to OpenSearch"),
     which is applied and re-presented.
  2. **Hand-edited markdown** — the customer edits the review document and
     re-submits it. This is the path for bulk changes, where chat is impractical.

### B. The review document: free-form markdown with one embedded parseable table

The document is **free-form markdown** for readability (grouped by target
engine, showing the access pattern, the "why" from `assignment_reason`,
`confidence`, and any `warnings`). Free-form is non-negotiable: it is what the
customer actually reads and edits.

Inside that document is exactly **one fenced, strictly-formatted editable
table**, keyed by `query_id` as a stable anchor, with a fixed column schema:

```
| query_id | access pattern | current engine | new engine | in scope |
```

- On re-submit, **only this table is parsed**; surrounding prose is ignored and
  preserved. `query_id` is the anchor, so reordering or reformatting the prose
  never changes routing.
- The parse is **strict and fail-loud**: an unknown or duplicated `query_id`, an
  engine outside the known set, or a malformed row is rejected with a clear
  message rather than silently dropped. A row left unchanged means no change for
  that query.
- This mirrors the AWS Transform Helix plan-editor pattern (a single markdown
  document with well-defined editable surfaces and a capability catalog that
  bounds what may change), adapted to per-query routing.

### C. Versioning: one resolver + provenance + staleness detection

- **One new version per gate pass**, capturing all edits together
  (`status = customer_modified`, `previous_version` set). If the customer
  changes nothing, **no version is written** and the pipeline passes through.
  This reuses the override -> new-version logic already in
  `api/routes/assignments.py`, factored into a shared helper so REST and the ATX
  gate share one write path.
- **Provenance stamp.** Add a `source` field to the `Assignment` model
  (`assignment_resolution` | `reality_check` | `customer_gate`). `status` keeps
  tracking approval state; `source` records which stage produced the version.
  Together with `previous_version` and `timestamp`, any consumer can reason
  about the lineage instead of trusting the number.
- **One resolver.** Replace the four ad-hoc `max(version)` computations with a
  single `resolve_effective_assignment_version()`. Its rule is stated once:
  "the highest committed version in the lineage." Today that equals the max, but
  the rule is now centralized and provenance-aware rather than re-derived four
  different ways.
- **Staleness detection (the robustness win).** Every downstream artifact
  records the `assignment_version` it consumed. Schema outputs already do
  (`schema_output.json` carries `assignment_version`); extend the same stamp to
  synthesis. Rule: any downstream artifact whose recorded version is behind the
  effective version is **stale** and must be re-run. This is what makes a future
  "re-enter the assignment phase" flow safe: it appends a new highest version,
  and stale artifacts are re-dispatched, with no stage pinned to a fixed number.

## Implementation surface

- **`src/contracts/assignment_models.py`** — add a `source` enum field to
  `Assignment`; the assignment resolver and Reality Check stamp it on write.
- **Assignment version resolution** — introduce one
  `resolve_effective_assignment_version()` and route
  `core._resolve_assignment_version`, `local_orchestrator._get_assignment_version`,
  `local_s3`, and `graph/populators` through it.
- **Shared override write path** — factor the override -> `v{N+1}` logic out of
  `api/routes/assignments.py` into a helper both REST and the ATX gate call.
- **Render** — assignment -> review markdown (group by engine, embed the
  parseable table).
- **Parse** — edited markdown table -> per-query overrides, strict validation.
- **ATX orchestrator tools** — `present_assignment_review` (render + write the
  review doc, mark awaiting approval), `apply_assignment_edits` (parse edit,
  write `v{N+1}`), and a gate that blocks schema tools until an approval artifact
  exists. Orchestrator prompt runs the gate after Reality Check and before any
  schema-design tool.
- **Downstream provenance** — stamp the consumed `assignment_version` on the
  synthesis output; add the staleness check.

The durable contract (the `assignment/v<N>/assignment.json` shape plus the new
`source` field) stays stable, so schema design, synthesis, and the graph read
the same artifacts they always have.

## Rationale

- **Parity with the web gate**, on the same artifact and the same override
  semantics, so behaviour matches across products and the write path is shared.
- **Customers steer routing before the expensive phase.** Schema design is the
  long, LLM-heavy phase (ADR-027); reviewing routing first avoids designing for
  an engine the customer would have removed.
- **Kills the blind-`max` fragility.** One resolver with a stated rule plus
  provenance replaces four independent "latest" guesses.
- **Safe future re-entry.** Provenance + staleness detection lets a later
  assignment change re-dispatch only what is stale, without pinning stages to
  fixed versions.

## Alternatives considered

- **Chat-only editing (no file) — rejected as the sole path, kept as the light
  path.** Fine for approve and small tweaks; impractical when a customer wants to
  move many access patterns at once.
- **Fully structured input (JSON / form) instead of markdown — rejected.** More
  reliable to parse, but not what the customer edits in the Transform chat UX.
  The embedded, delimited table inside the free-form document is the compromise:
  human-editable, machine-parseable.
- **Per-stage version pinning ("each stage wires to its version") — rejected.**
  It spreads version-selection logic across every downstream stage and creates a
  coordination burden on every future change. One resolver + provenance +
  staleness gives the same safety with a single point of truth.
- **Mutate the assignment in place on edit — rejected.** Loses lineage and
  reproducibility; append-only versions preserve both and enable staleness
  detection.

## Consequences

Positive:

- Routing review/approval parity with the web product in ATX.
- A single authoritative version resolver; provenance and staleness make version
  handling verifiable rather than assumed.
- The customer's edits flow downstream through the existing effective-version
  machinery with no per-tool wiring.

Tradeoffs:

- **New parsing surface.** Round-tripping a hand-edited markdown table is new and
  must be defended by a strict schema, `query_id` anchoring, and fail-loud
  validation.
- **Model and writer changes.** Adding `source` touches the assignment resolver
  and Reality Check so they stamp it; the four resolvers must be migrated to the
  shared one in the same change to avoid a split brain.
- **The pipeline no longer runs unattended end-to-end.** The hard interrupt is
  the point of the gate, but it means ATX schema design now waits on a human
  approval artifact, which callers and tests must account for.

## Future Work

- **Re-entry into the assignment phase** after schema design, using
  staleness-driven re-dispatch of only the affected engines.
- **Consolidate the web gate onto the shared write path** so both products share
  one override -> version implementation end to end.
- Optional: surface the parseable table's schema in a capability-catalog style
  helper (as Helix does) so the editable surface is self-describing.
