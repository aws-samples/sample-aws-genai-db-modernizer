# ADR-031: Query-First Routing Model and Engine Rule Catalog

**Status:** Accepted **Date:** 2026-10-11 **Deciders:** Database Modernizer Assessment Architecture Team **Related ADRs:** ADR-017 (Analysis Agent Scoring Framework), ADR-018 (Reality Check and Human Approval Gate), ADR-021 (Aurora Absorption Pass in Reality Check), ADR-026 (Fold Reality Check into the Assessment Core)

---

## Context

The routing model has been described across many issue threads rather than one document: how a query gets its engine, how the reality check consolidates engines, how co-dependency groups move, how cost factors into scoring, and how pattern-and-engine rules should be organized. Contributors and reviewers have had no single place to read the intended model or check a routing change against it.

An audit of the routing code against that intended model found that the model itself needed decisions, not just a write-up: whether cost is really a dimension of the score, how DynamoDB's transaction support should be scored, and how pattern and engine rules should be organized so one fact is not encoded in several places that can disagree. This ADR records those decisions. It does not record the audit's measurements, which belong to the tracking issues the audit filed.

Earlier ADRs described parts of this model before these decisions were made. ADR-017 introduced per-query scoring and per-target weights but predates engine-neutral detection and a declared cost dimension. ADR-018 introduced the reality check as engine consolidation, with its own capability and scoring logic separate from the resolver's. ADR-021 added an Aurora-specific absorption pass with its own fitness gate, inside the reality check. ADR-026 folded the reality check into the assessment core without changing how it scores a query. None of them described the query as the sole unit of assignment, a single engine- and query-aware cost dimension, or a pattern-first, engine-neutral rule catalog.

## Decisions

### 1. The query is the unit of assignment

Every routing decision is made per query. A table is an input to a query's score; it never decides a query's engine on its own. The engine recommended for a table in a table-level mapping is a summary, not ownership. No step may assign a query because of what other queries on the same table do: no table majority, no table owner, no table-wide classification. Counts, summaries and groups shown in deliverables are built from queries; a query group reported for a relational engine is a real group of queries, not a stand-in for a table.

### 2. A query's engine is decided in a fixed order

For each query and each candidate engine, the same five steps run in order:

1. **Candidates.** Every modeled target engine, minus any engine the source makes impossible. A homogeneous source always has its matching relational engine as a candidate. A heterogeneous source has exactly one relational engine as a candidate, chosen deterministically with the choice traced (see Decision 6).
2. **Capability gate.** An engine is excluded only if it is structurally unable to run the query at all. Poor fit is a score difference, not an exclusion, and every candidate is held to the same gate.
3. **Ownership gate.** An engine that is not a system of record cannot own a write or a locking read.
4. **Score.** A weighted average of four dimensions — pattern match, complexity, performance and cost — computed from the query's own tables and its own matched pattern signals. A table-level classification may inform a query only through that query's own matched patterns, never by inheritance from the table.
5. **Penalties.** A penalty applies only to an engine that actually lacks the capability a pattern needs (see Decision 3). An engine that runs a pattern natively is not penalized for it.

The highest total score wins. On an exact tie, the source-compatible relational engine wins; no other engine preference exists. In the reality check, where a query already has a current engine (the first assignment has none), a tie that includes that current engine stays there instead: a tie is not a strictly higher score for some other engine, so moving the query would add a migration cost for no score benefit at all. This stability rule only ever applies when exactly one tied engine is the query's current one -- two different, still-tied current engines (members of the same co-dependency group sitting on two different engines) is not "the" current engine either way, and is reported as unresolved, not decided by which one sorts first. When neither the source-compatible rule nor stability resolves a tie, Decision 12 governs the placeholder pending the customer's own choice.

Two second-level decisions apply after the per-query result: co-dependency grouping (Decision 4) and a small set of listed overrides for database administration statements and justified search pinning, which skip steps 3 to 5 under a stated condition only.

### 3. DynamoDB supports ACID transactions; the score reflects shape, not a missing capability

DynamoDB supports ACID transactions: `TransactWriteItems` and `TransactGetItems` give atomic, consistent behavior across up to 100 items, and a conditional write is itself ACID on a single item. A transaction is therefore not treated as a capability DynamoDB lacks. The score instead reflects how far the transaction's shape is from that native support: a transaction touching one or a few entities scores well, a wide multi-table transaction on a normalized model scores poorly because it needs a redesign. The decision trace states which shape a query matched and why.

This supersedes any treatment that scores DynamoDB transactions as a missing capability requiring a saga or workaround, in favor of the shape-based score above.

### 4. Co-dependency groups move as one unit

Queries that must move together — statements in one transaction, or reads that must stay consistent with a write — are placed as one unit once the group's engine is decided. Every member goes to that engine, even if a member alone would score higher elsewhere; each member's own best engine is still recorded in the trace. How the group's engine is chosen — for example, from the members' combined scores — is not decided by this ADR and remains open, to be settled with options when the grouping code next changes.

### 5. Cost is one of four weighted score dimensions, and is being made engine- and query-aware

Cost was already one of the four weighted dimensions of a query's score (pattern match, complexity, performance, cost), at a 40/30/20/10 default split, with some engines weighting cost differently. That weighting is unchanged by this ADR. What changes is how the cost dimension's value is computed: it is to be engine- and query-aware, factoring in the engine's standing cost shared across the queries it would serve, so that a low-traffic engine with high standing cost scores low on cost for a query it barely serves. The pricing shown in deliverable reports is a separate, unchanged presentation and is not derived from this scoring dimension.

### 6. Heterogeneous sources pick exactly one Aurora engine, deterministically

For a heterogeneous source (SQL Server, Oracle, DB2), exactly one of Aurora PostgreSQL or Aurora MySQL is the relational candidate for every query from that source. The choice is made deterministically, with the reasons traced, and feature support for a given query is checked against that chosen engine rather than assumed.

### 7. The reality check is the CTO consolidation review, routed by the same per-query scores

The first assignment can spread a workload across several engines because each fits some queries best. The reality check is the second-level review that asks, per engine already assigned: is this engine worth running for the queries it would serve, given its standing cost, its operational weight, and whether another engine already in the architecture serves those queries well enough? It may remove an engine or move its queries onto an engine already in the architecture. A moved query goes to its highest-scoring engine among the engines that remain, using the same per-query scores the first assignment used — the reality check does not keep a second, independent fit model. It reports every removed engine and every moved query with before and after scores, and claims a saving only for an engine the first assignment actually justified.

This supersedes the reality check's own independent capability and scoring logic (ADR-018) and the Aurora-specific absorption pass with its own fitness gate (ADR-021), replacing both with one scoring path shared with the first assignment. The reality check's role as engine consolidation and the human approval gate it feeds (ADR-018, ADR-026) are unchanged by this ADR.

When the mandatory-engine justification floor moves a query off an engine that failed it, a candidate already mandatory for a different engine (data already flowing to it via a signal override, such as a text-search sync) ranks ahead of an equally-scored, non-mandatory candidate (`reality_check._find_best_absorber_for_query`'s `is_mandatory_secondary` bonus) -- a real operational reason (the data is already synced there) rather than a preference of convenience. It applies only after the score itself ties; an exact tie that survives it too still goes through Decision 2's tie-break, never decided by this bonus a second way.

### 8. Rules are a pattern-first, engine-neutral catalog in one place

Routing rules move out of per-engine code and into a declarative catalog read by every step: analysis, gates, penalties, scoring, cost, reality-check floors, reports and the UI.

- **One file per access-pattern type** (for example key-value lookup, full text search, transactions), each naming the pattern, the detector that identifies it, and one entry per engine with whether the engine supports the pattern, its score, and a short reason that feeds the decision trace.
- **One file per engine** for properties that do not depend on the pattern: cost and pricing inputs, score dimension weights, justification floors, whether the engine can own writes, and its display name and color.
- **Detection stays in code and is engine-neutral.** A detector runs once per query and reports facts (for example a transaction's entity count, or a wildcard's position), not scores. The catalog turns those facts into each engine's support and score, so a given fact is classified the same way regardless of which engine is being scored, and engines differ only in their catalog entries. This removes the previous pattern, where each engine's analysis detected and scored its own patterns independently, which let the same fact be encoded in more than one place with disagreeing answers.
- **Validation.** The catalog loads into typed models at start-up, so a missing or mistyped field fails loudly. A completeness check requires every pattern file to have an entry for every engine, so adding an engine or a pattern fails with the exact list of missing decisions instead of silently defaulting to "unsupported."
- **Comparison.** A rendered pattern-by-engine matrix, with support, score and reason, plus the engine properties side by side, is the one place that answers "why does this engine's score differ from that one's for this pattern?" Display names and colors used elsewhere are generated from the engine files rather than kept as separate copies.
- **Adding an engine** means one engine file, one entry per pattern file (the completeness check lists exactly which), and the engine's own detection code where a pattern has no existing detector. No change to the resolver, the reality check, the reports or the UI.

### 9. Neptune and Keyspaces are future placeholders

Neptune and Keyspaces are added to the catalog as placeholders only, each marked as a future enhancement with an explicit "not modeled yet" entry in every pattern file so the completeness check passes on a recorded decision rather than a default. A placeholder engine is never a routing candidate and routing output is unchanged by its presence. Modeling either engine for real — giving it real scores, detection and schema-design support — is separate, later work.

### 10. Every routing change reports its per-query effect

A routing change reports, before and after against the prior behavior, the per-query effect on every available sample workload, for both the first assignment and the final (post-reality-check) assignment. Engine totals alone are not sufficient evidence. If a score-based result looks wrong, it is reported as a finding, not silently corrected with a bias toward a preferred engine.

### 11. Models explain; they do not decide

Assignment and reality-check routing remain deterministic. A model may explain an assignment in natural language; it never decides or changes one. Running without a model gives the same assignment and reality-check result as running with one.

### 12. A still-unresolved tie gets a placeholder, decided by settled workload share then a stable hash (maintainer decision, 2026-10-12)

Decision 2 leaves some ties with no rule at all: no tied engine is source-compatible, and (in the reality check) no tied engine is the query's current one either, or two different tied engines are each current for different members of the same co-dependency group. The query still needs an engine to sit under until the customer picks one, so a placeholder is used -- but the placeholder is not itself a new ADR-031 preference, and the structured, customer-visible fact is the tie itself (`QueryAssignment.tied_engines`), never silently hidden behind the placeholder's pick.

The placeholder, in order: first, whichever tied engine already carries more of the SETTLED workload for this run -- the number of queries this run decided without a tie (never another placeholder pick, so one tie's placeholder can never feed another tie's count, which would make the result depend on evaluation order); second, when that count is also tied (including the common case where neither engine has any settled workload yet), a stable hash of the query id and the candidate engine name. Favoring the engine with more settled workload is consolidation-consistent with Decision 7's CTO review, which already prefers fewer, larger engines over more, smaller ones -- it is not a preference for one engine's name or brand. The hash fallback exists only so the result is deterministic across runs on the same input; it is a hash OF the engine's name together with the query id, not a comparison BY the name -- an engine's name, traffic or query count is never read to decide which one wins by being who it is, only hashed alongside the query id to pick a stable, unpredictable split when the settled-workload count cannot.

A tied query is evidence for exactly the ONE engine the placeholder picks -- never for every tied engine, and never for none. Phase 1 counts it as that one engine's native workload when deciding whether the engine earns its place; excluding it from every engine's evidence could leave an engine whose only queries were tied uncommitted with no fallback (crashing the placement invariant when nothing else was eligible either) or let a tied workload silently fall to a far weaker committed engine because Phase 1 never got to evaluate either tied engine at all. The query still carries `tied_engines` and stays visible to the customer either way. If the picked engine is later removed anyway, Phase 2 re-resolves the tie among whichever of the originally tied engines are still committed, never opening the choice to the wider committed set while at least one of them survives; only once none of the tied engines survive does the query fall back to its best-scoring surviving engine among the rest, with an honest reason, and `tied_engines` clears -- the tie is genuinely gone, not re-placeholdered.

Eligibility gates that key off "the query's current engine" (the specialist-absorb exemption, the join-consolidation block) treat every one of a tied query's `tied_engines` as current, not just the one placeholder happens to be recorded as -- otherwise an arbitrary placeholder pick could silently decide a gate's answer instead of just sitting the query somewhere pending the customer's choice. The same stable key (sorted member ids for a co-dependency group, never input order) feeds the placeholder's hash fallback everywhere it is used -- the first assignment, the reality check's placement step, and its mandatory-engine-floor absorber -- so the same tie cannot re-pick a different engine purely because of evaluation order.

This is implemented once, in `tie_break.pick_unresolved_tie_placeholder`, so the resolver, the reality check's placement step, its mandatory-engine-floor absorber and its eligibility gates cannot drift from each other on it; the settled-workload snapshot itself is computed once, by the resolver, and persisted for the reality check to reuse (`RoutingTrace.settled_counts`) rather than each stage measuring "settled" differently. A future ADR-031 amendment may replace this placeholder; until then, this is the one rule governing it.

## What this ADR supersedes

- **ADR-017** — the per-query scoring unit (section 1) and configurable weights per target (section 4) stand; its model of specialist-curated, per-engine pattern catalogs with independent detection code is superseded by Decision 8's single, engine-neutral catalog and detector.
- **ADR-018** — the reality check as a consolidation step, and the human approval gate it feeds, stand; its description of the reality check computing engine fit through its own capability table and scoring logic is superseded by Decision 7.
- **ADR-021** — the general principle that a committed engine can absorb orphan queries from an engine that does not justify its cost stands as part of the reality check's consolidation role; its Aurora-specific absorption pass, with its own fitness gate and thresholds separate from the shared score, is superseded by Decision 7's single scoring path.
- **ADR-026** — folding the reality check into the assessment core, and the assignment-version resolution it introduced, stand unchanged; any description of the reality check's internal scoring logic as distinct from the resolver's is superseded by Decision 7.

## Consequences

### Positive

- One document that contributors and reviewers can read and check a routing change against, replacing scattered issue-comment decisions.
- A single scoring path for the first assignment and the reality check removes a class of bugs where the two disagreed for reasons that were not genuine consolidation.
- The pattern-first catalog makes every routing rule, for every engine, visible and comparable in one place, and makes adding an engine a configuration change rather than a code change to shared logic.
- DynamoDB's transaction support, and heterogeneous-source engine choice, are now stated decisions rather than implicit code behavior.

### Negative

- Building the catalog, unifying the two scoring paths, and making cost engine- and query-aware are each nontrivial changes to code that is already heavily depended on; they are sequenced as separate, independently reviewed and verified changes, not a single rewrite.
- How a co-dependency group's engine is chosen is explicitly left open by this ADR, so group routing can still disagree with what Decision 2 would pick for each member alone until that follow-up decision is made.

### Risks

- Migrating existing per-engine rules into the catalog risks silently changing a score if a rule is transcribed incorrectly; the per-query before/after reporting in Decision 10 is the mitigation.
- Neptune and Keyspaces placeholders could be mistaken for real support if a future contributor skips the "future enhancement" marking; the completeness check and the explicit `status` marking exist to prevent that.

## Alternatives Considered

- **Leave the model in issue comments.** Rejected: hard to find, easy to drift from, and no single place to check a routing PR against.
- **Rewrite the superseded ADRs in place.** Rejected: ADRs are a record of decisions as they were made. A new ADR that states what it supersedes keeps that history intact instead of erasing it.
- **Keep two independent scoring paths (resolver and reality check).** Rejected: the two paths drifted and disagreed on a large share of queries for reasons that were not intentional review logic, which is a correctness risk, not a feature.
- **Model Neptune and Keyspaces now instead of as placeholders.** Rejected for this round: in-flight data-movement work takes priority, and a placeholder still proves the catalog can hold a new engine.
