# ADR-028: Customer Assignment-Review Gate Before Schema Design

**Status:** Accepted
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
The gate is not new to the domain model either: `phase_models.py` already
defines an `ASSIGNMENT_REVIEW` phase that `REALITY_CHECK` precedes and
`SCHEMA_DESIGN` depends on, the orchestrator progression file
(`.meta/<job>.json`) records its completion, and
`AssignmentStatus.CUSTOMER_APPROVED` exists for the approved artifact. So the
approval signal already exists; what is missing is a consumer that honors it in
ATX.

The **AWS Transform (ATX)** modernizer does **not honor that gate**. Its
orchestrator prompt currently tells it not to pause between phases for approval
(`orchestrator.py`), so after Reality Check it dispatches schema design
immediately and a customer cannot see or adjust routing before the long,
LLM-heavy design phase runs. Bringing the web gate to ATX is the primary goal of
this ADR.

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
tool until the review is approved.

- Enforced by **artifact state, not prompt discipline**, reusing the mechanism
  that already exists rather than adding a new artifact: the orchestrator marks
  the `ASSIGNMENT_REVIEW` phase running, and the gate tool refuses to advance to
  schema design until that phase is recorded completed in the progression file
  (`.meta/<job>.json`) and the effective assignment version carries
  `status = customer_approved` (unedited) or `customer_modified` (after an edit).
  ATX simply starts honoring the `ASSIGNMENT_REVIEW` phase the domain model and
  the web already define; its prompt changes from "do not pause" to "run the
  gate". The system prompt describes the gate, but the block is real regardless
  of what the LLM decides.
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
- **Self-describing editable surface (in scope).** The table's column schema and
  the set of valid target engines come from one small catalog helper (the
  equivalent of Helix's `get_edit_rules`), not hard-coded separately in the
  renderer and the parser. The renderer, the parser's validation, and the
  customer-facing guidance all read that single catalog, so the editable surface
  cannot drift between what we show, what we accept, and what we document.

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
- **Editable-surface catalog** — one helper returns the table column schema and
  the valid engine set; the renderer and the parser validation both read it.
- **Render** — assignment -> review markdown (group by engine, embed the
  parseable table from the catalog).
- **Parse** — edited markdown table -> per-query overrides, strict validation
  against the catalog.
- **ATX orchestrator tools** — `present_assignment_review` (render + write the
  review doc, mark `ASSIGNMENT_REVIEW` running), `apply_assignment_edits` (parse
  edit, write `v{N+1}`), and a gate that blocks schema tools until
  `ASSIGNMENT_REVIEW` is completed. The orchestrator prompt changes from its
  current "do not pause between phases" instruction to "run the gate after
  Reality Check and before any schema-design tool".
- **Downstream provenance** — stamp the consumed `assignment_version` on the
  synthesis output; add the staleness check.

The artifact shape (`assignment/v<N>/assignment.json`) stays backward-compatible
by keeping `source` optional with a default, so old artifacts and readers do not
break; see Contract impact below.

## Contract impact

**This is a contract modification, not just new code.** It changes shared data
contracts, so the blast radius reaches every producer and consumer of the
assignment, plus contract tests:

- **`Assignment` contract** — new `source` field (optional, defaulted for
  backward compatibility) and first real use of `AssignmentStatus.CUSTOMER_APPROVED`.
  Bump the model's contract version and add round-trip tests. Both writers (the
  assignment resolver and Reality Check) must stamp `source`.
- **Downstream artifact contracts** — the consumed `assignment_version` (and
  ideally its `source`) must be recorded on synthesis output
  (`AssignmentSummary` in `synthesis_output.py`) as it already is on schema
  output, so staleness is checkable. `reality-check/output.json` already carries
  `source_assignment_version` (`contract_version "1.1"`) and should align with
  the new provenance vocabulary.
- **Phase contract** — `ASSIGNMENT_REVIEW` already exists in `phase_models.py`;
  ATX must honor the `REALITY_CHECK -> ASSIGNMENT_REVIEW -> SCHEMA_DESIGN`
  ordering the contract declares.
- **Contract tests to update/add** — `tests/contract/test_reality_check_output.py`,
  `tests/contract/test_synthesis_output.py`, and a new assignment-model contract
  test covering the `source` field, defaulting, and version round-trip. The
  review-markdown render/parse round-trip also needs a golden-file contract test
  (render -> edit -> parse -> overrides) so the editable-surface format is pinned.

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
  staleness-driven re-dispatch of only the affected engines. This is a
  customer-requested capability (assessment feedback) and is tracked as its own
  issue; the provenance + staleness groundwork in this ADR is the prerequisite
  for it.
- **Consolidate the web gate onto the shared write path** so both products share
  one override -> version implementation end to end.

The self-describing editable-surface catalog, previously listed here as
optional, is now **in scope** for this ADR (see Decision B and Implementation).

---

## Amendment (2026-08-27): platform-native HITL editable-table transport + two-step gate

The original decision (parts A-C above) shipped and the gate worked, but the
**transport** proved wrong in a live run. The review document was published as a
`CUSTOMER_OUTPUT` markdown artifact and the customer was asked to paste an edited
marker-bounded table back into chat. Two failures surfaced:

1. **The table does not round-trip through chat.** A real assignment was 1,654
   rows; that cannot be pasted back, and the free-text "move query X" path the
   LLM offered was never wired to `apply_assignment_edits`.
2. **`HITL_FROM_AGENT` never surfaced in the Artifacts panel.** An earlier attempt
   to publish the review as `HITL_FROM_AGENT` uploaded successfully but did not
   render in the panel — that category is for the HITL-task UI, not the panel — so
   the customer could not find the table at all.

This amendment keeps the gate, the approval signal (`.meta` `ASSIGNMENT_REVIEW`
phase), and all of parts C (one resolver + provenance + staleness). It changes
**only how the routing is presented and how edits come back**, and adds a
recommendation step in front of the detailed table.

### D. Two-step gate

`present_assignment_review` no longer renders the full per-query table. It renders
an **engine-level recommendation** (per engine: in-scope/total query counts and
the main rationale, aggregated from `assignment_reason`) and asks the customer to
either **continue with the recommendation** or **review the full routing in
detail**. The large per-query table is generated only if they choose detail. Most
customers accept the recommendation, so the expensive table is usually never
built.

- Continue -> `finalize_assignment_review` (no edits) approves as-is.
- Review in detail -> `open_detailed_routing_review` raises the editable table.

### E. HITL editable `TableComponent` transport (raise-and-resume)

`open_detailed_routing_review` raises a **BLOCKING** platform HITL task rendered as
an editable `TableComponent` (`src/atx_orchestrator/runtime/hitl.py`), over the
same `get_agentic_api_client()` seam `runtime/artifacts.py` already uploads
through (`create_artifact_upload_url` -> `complete_artifact_upload` ->
`create_hitl_task` -> `start_hitl_task`). Columns: `query_id`, `access pattern`,
`current engine` (read-only), **`new engine`** and **`in scope`** (editable, each
with an `editConfig` validation regex the WebApp enforces inline), and
**`rationale`** (read-only, the per-query "why"). The customer edits cells in place
and submits.

The blocking model is **raise-and-resume**: the tool records the `hitlTaskId` in a
small transport-state pointer (`<db>/<job>/assignment/review/pending_hitl.json` —
not an approval artifact) and the orchestrator ends its turn. When the customer
submits, the platform re-invokes the orchestrator, which calls
`finalize_assignment_review`; that reads the submission back
(`get_hitl_task` -> `humanArtifact` inline `content` or downloaded `artifactId`),
converts the edited rows to overrides via `diff_review_items`, and applies them
through the same `apply_assignment_overrides` path (`source = customer_gate`). This
avoids parking a multi-hour human wait inside a single AgentCore turn.

### F. Chat fallback retained

When the HITL transport is unavailable (outside the WebApp runtime, or any client
failure), `open_detailed_routing_review` degrades to the original transport:
publish the full editable markdown table as `CUSTOMER_OUTPUT` and accept the
edited marker-bounded table back through `finalize_assignment_review(edited_markdown=...)`.
The markdown render/parse/diff functions (Decision B) are retained for this path.

### Transport surface after the amendment

- `present_assignment_review` — engine-level recommendation + choice (`awaiting_choice`).
- `open_detailed_routing_review` — raise the editable HITL table (BLOCKING), or
  fall back to `CUSTOMER_OUTPUT` markdown + chat.
- `finalize_assignment_review(job_id, database_name, edited_markdown="")` — read
  HITL submission, or parse fallback markdown, or approve-as-is; apply overrides;
  mark `ASSIGNMENT_REVIEW` completed. Replaces `apply_assignment_edits`.

`present_assignment_review` remains the entry point; `apply_assignment_edits` is
superseded by `finalize_assignment_review`. Structured additions live in
`assignment_review.py` (`render_assignment_summary`, `build_review_table`,
`diff_review_items`) alongside the retained markdown helpers.
