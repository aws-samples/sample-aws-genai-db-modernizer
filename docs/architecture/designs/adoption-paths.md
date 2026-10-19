# Design: adoption paths derived from the analysis

**Status:** Accepted (maintainer decision, 2026-10-19: the eight recommendations of section 16 are accepted). Design only: no code changes with it.
**Issue:** #472 (epic #322). **Implementation:** epic #1128, issues #1129 to #1136 (section 15). **Related:** #471 (version and feature gaps, facts since contract 1.35), #473 (one-step view), #992 (one source of truth per table, merged in #1032), #1028 (results page, merged in #1031), #1020 (executive report PDF, merged in #1030), ADR-031, ADR-032.

## 1. Summary

1. Today every deliverable shows one fixed path (relational move, then cache, then every purpose-built engine in one wave). This design turns it into a short menu of paths, each a deterministic view of the one assignment the pipeline already decided.
2. Four kinds of path: the quickest win (cache the hot reads, no data migration), the relational move only, the relational move plus one larger step (one path per purpose-built engine), and the full target. The full target can be taken in stages (the waves) or in one step (#473).
3. A path never re-routes a query. A query keeps its final engine when the path includes that engine, and otherwise stays on the relational engine wave 1 put it on. No Move is recorded and no score is recomputed (ADR-031 Decisions 1, 10 and 11 hold).
4. A path is offered only when every query it leaves on the relational engine passes that engine's capability and ownership gates, the same check the customer gate runs. Otherwise the path is replaced by the smallest larger path that serves every query, and the reason is stated.
5. Inside each purpose-built target, the work is split into **move units**: the tables one engine owns (their write groups from #1032), the queries on them, and the co-dependency units they belong to. Units never split a write group or a co-dependency unit.
6. Units are ordered into at most four **stages** per engine, each with its own gate: reads on a replica first (no system of record changes), then self-contained ownership moves, then ownership moves that need a reverse replica, then units held by an open critical or high risk. Within a stage, the smallest blast radius goes first, then the highest call share.
7. Wave 1 (the relational move) now says which tables stay on the relational engine for good, which leave later and in which stage, and which no query reads. A cross-engine source, or one with a blocking #471 gap, gets a staged relational move by table cluster (tables linked by a foreign key or a shared query).
8. Each path shows the same figures, all computed once in synthesis (ADR-032): engines at the end, queries and tables moved, monthly cost and its difference from the relational-only path, open critical and high risks, the gate before each step, and every replicated table with its CDC direction (#1032).
9. `report.json` gains `adoption_paths` (the data: units, stages, paths) and `facts.adoption` (the figures). All fields are optional, so the change is a MINOR contract bump (the next free one after 1.39); an older report shows today's waves unchanged.
10. Presentation is summary first everywhere: one table of paths with five columns, details one click (UI) or one section (engineering report) away. The executive PDF gets one page and names no tables.

## 2. Binding maintainer direction

- #472 (2026-10-16): a smarter wave report. Instead of only "move to relational", say, within the relational target too, which tables go first and in which waves. This may grow into its own epic.
- #473 (2026-10-16): a one-step alternative view is wanted. Draw a clear line on how much to show: summary first, details on demand.
- #471 (2026-10-16): version and feature gaps for the relational move are in scope, and heterogeneous migrations (SQL Server, Oracle, DB2 to Aurora) are in scope later; a heterogeneous-migration epic may follow.
- #322: every step is optional; the paths come from each workload's analysis, not from a fixed recipe.
- AGENTS.md and ADR-031: the query is the unit; engine assignment is deterministic; a model may explain, never decide; every engine change is a Move.
- ADR-032: every customer-facing figure is computed once in synthesis; renderers only format.
- #992 (maintainer decision 2026-10-17, merged in #1032): one source of truth per table; other engines read a table only as an eventually consistent CDC replica.

## 3. What the plan shows today

Measured on 2026-10-19 on origin/main `4912de7f` (after #1030, #1031 and #1032 merged) with the deterministic pipeline (`uv run python scripts/run_assessment.py --file docs/examples/<sample>/<sample>-collection.json --all -y --llm-mode none`), contract 1.39. A first draft measured on `06178851` (2026-10-17, contract 1.27) gave the same shape; the one-writer rule of #992 moved some tables and queries between engines, so the figures below are the later ones.

| Sample | Wave 1 (relational) | Wave 2 (cache) | Wave 3 (purpose-built) | Table groups in wave 3 |
|---|---|---|---|---|
| wordpress (MySQL 8.0) | Aurora MySQL, 50 tables, 107 queries at cutover, 68 stay | 20 hot reads, 83.4% of calls | DynamoDB: 39 queries, 18 tables served, 15 move, 3 kept in sync | 1 (18 tables, 39 queries) |
| discourse (PostgreSQL 16) | Aurora PostgreSQL, 311 tables and 1 view, 1,654 queries at cutover, 804 stay | 3 hot reads, 25.1% of calls | DynamoDB and DocumentDB: 850 queries, 224 tables served, 183 move (69 and 114), 41 kept in sync | 1 (133 tables, 665 queries) |
| trailgear (SQL Server 15) | Aurora PostgreSQL (cross-engine), 30 tables and 1 view, 95 queries at cutover, 47 stay | 16 hot reads, 73.1% of calls | DynamoDB: 48 queries, 29 tables served, 23 move, 6 kept in sync | 1 (29 tables, 48 queries) |

Findings that shape the design:

- **Every purpose-built wave is one table group.** `_dynamodb_table_groups` builds a group per co-dependency group with a DynamoDB member, and puts every other table in one "independent" bucket. On all three samples every co-dependency group (wordpress: one of 15 queries; discourse: 189, 4, 2 and 2; trailgear: 4, 2 and 2) sits on the relational engine, so the bucket holds everything. The gate "query parity confirmed per table group" therefore gates one group of 133 tables on discourse. DocumentDB gets no groups at all.
- **Wave 1 says the whole database moves, not which tables matter.** A reader cannot tell the tables that stay on Aurora for good from those that leave in wave 3, or from the 28 wordpress and 39 discourse tables no collected query reads.
- **The direct option is one sentence.** `src/report/roadmap.py` prints "One suggested adoption path, not the only one" and points at the target engines table. Nothing shows the alternatives' cost or risk.
- **Engine costs are per-engine estimates.** `tco_analysis.cost_breakdown` holds each engine's analysed monthly cost. The relational engine is sized for the whole collected throughput (wordpress: 222 calls per second on a db.r6g.xlarge), so it is not resized when queries leave. The current cost is unknown on all three samples (`current_cost_known` is false).
- **The figures a path needs already exist.** `facts.engines` (role, workload share, cost), `facts.critical_high_risks_by_engine`, `facts.waves[]`, `facts.no_migration` (the retained engine plus the cache, which is already the relational-only end state), `facts.moving_query_count` (#1020), `table_sources` and `facts.table_sources` (#992) and `facts.relational_gaps` (#471).

## 4. Terms

- **Path:** an end state (a set of engines) plus the ordered steps that reach it. Every path ends inside the full target: it is the target with some steps not taken.
- **Step:** one wave of the current plan, restricted to the engines the path includes (the relational move, the cache, one purpose-built engine's share of a wave, a search read model).
- **Move unit:** the smallest set of tables and queries that can move to one engine on its own without splitting a write group or a co-dependency unit.
- **Stage:** an ordered slice of one step: the move units of one engine that share a kind of risk, with one gate. Stages are numbered inside their wave (3a, 3b, ...), so today's wave numbers keep their meaning.
- **Gate:** the condition to verify before the next stage or step starts. Every gate is a fixed template filled from facts, never model text.
- **Relational engine (R):** wave 1's engine: the source-compatible Aurora engine, or the one ADR-031 Decision 6 chose for a heterogeneous source.
- **Purpose-built targets (T):** the final engines with role "Migration target" or "Search read model", minus R (trailgear's Aurora PostgreSQL is a "Migration target" by role, but it is R).
- **Cache (C):** the engine with role "Cache layer", when the cache overlay has at least one read.

## 5. Inputs

Every input is already deterministic and already read by synthesis. No new routing input, no model input.

| Input | Where it comes from | Used for |
|---|---|---|
| Final engine per query, its tables, in-scope flag | effective assignment `query_assignments` | units, path projection |
| Source of truth, write engines, replica engines per table | #992: `TableAssignment.source_of_truth`, `replica_engines`, `Assignment.write_groups` | which tables a unit owns, CDC direction |
| Placement units (co-dependency groups merged with write groups) | #992: `write_groups.placement_groups` over `Assignment.co_dependency_groups` | units never split them |
| Call rate and statement type per query | collector `calls_per_second`, `query_type` | call share, write counts |
| Foreign keys | collector `database_schema.tables[].foreign_keys` | relational clusters |
| Open risks with their queries and tables | `risk_assessment.risks[]` (`severity`, `query_ids`, `affected_tables`, `mitigation`) | risk-held stage, path risks |
| Cache overlay | `cache_overlay` (reads, call share, owners) | quickest win, cache step |
| Per-engine monthly cost | `tco_analysis.cost_breakdown` through `report_facts.engine_costs` | path cost |
| Wave 1 homogeneity and the Aurora choice | `migration_waves[0].homogeneity`, `Assignment.aurora_engine_choice` | relational step, staged relational move |
| Version and feature gaps | #471: `facts.relational_gaps` (contract 1.35), per gap its feature, impact (`blocks_move`, `needs_rewrite`, `behaves_differently`, `informational`), tables, queries and objects; the risk register raises one Wave 1 risk per impact (#1149) | relational step gate, staged relational move |
| Capability and ownership gates on R | the customer gate's checks (`src/agents/referee/assignment_overrides.py`), which reject an override onto an engine that cannot serve or own a query | whether a path can leave a query on R |

## 6. Move units (exact rule)

For each purpose-built target engine E, built in `adoption_paths.py` (new, under `src/agents/referee/`):

1. **Owned tables.** A table is E's when its `source_of_truth` is E. A table no query writes is E's when every in-scope query reading it runs on E (it moves with them; nothing else needs it).
2. **Seed units.** Each placement unit (#992) with a member on E seeds one unit with its E members. Each owned table not yet in a unit seeds one unit with its write group.
3. **Join.** Two units merge when one E query touches tables of both. Repeat to a fixed point (union-find over E's queries and owned tables).
4. **Replica reads.** An E query that touches no owned table reads only tables another engine owns. It joins the unit of the first owned table it shares a query with; otherwise its own unit is keyed by the tables it reads (a "reads on a replica" unit).
5. **Per unit, record:** owned tables; replicated-in tables (owned elsewhere, read here) with their source of truth; reverse replicas (other engines' queries that still read the unit's owned tables, and so read a CDC copy fed from E); query ids; write and locking-read ids; call share (the unit's calls per second over the in-scope total); open risk ids (risks whose `query_ids` or `affected_tables` meet the unit's queries or owned tables); and a stable unit key (engine plus the first table id, sorted).

Guarantees, each a test: every E query is in exactly one unit; a write group is never in two units; a placement unit is never in two units; the units' queries add up to E's query count; the result does not depend on input order.

## 7. Ordering within a target

### 7.1 Stages of a purpose-built step

Each unit gets exactly one stage, the first that applies:

| Stage | Rule | Why this position | Gate template |
|---|---|---|---|
| d. Held by a risk | the unit has an open critical or high risk | a known blocker goes last, after its mitigation | "Before this stage: {mitigation of each risk}." |
| a. Reads on a replica | the unit owns no table | no system of record changes; reversible by pointing the reads back | "CDC from {source of truth} to {E} is within the reads' staleness tolerance, and the {n} reads return the same rows on {E}." |
| b. Self-contained ownership | it owns tables, reads none owned elsewhere, and no other engine reads its tables | the writes move, but no replica in either direction | "Backfill complete and writes cut over for {n} tables; query parity confirmed; no other engine reads them." |
| c. Ownership with a reverse replica | it owns tables that another engine still reads, or reads tables owned elsewhere | the widest coupling: the source of truth moves and a CDC copy flows back | "Writes cut over to {E}; CDC from {E} to {engines} for the {n} reads that stay there, within tolerance." |

Stage d is tested first so a risk always holds its unit, whatever its shape. The stages run a, b, c, d; an empty stage is skipped. Within a stage the units sort by: blast radius ascending (the unit's queries plus its reverse-replica reads), then call share descending, then unit key. The first unit of the first stage is named the pilot.

When a wave holds two engines (the merged DynamoDB and DocumentDB wave), its stages run by stage kind first, then in the wave's engine order: 3a DynamoDB, 3a DocumentDB, 3b DynamoDB, and so on. A path that includes only one of the two keeps only that engine's stages.

A search read model (OpenSearch) owns no table, so its whole step is one stage of kind a, with today's sync gate.

### 7.2 The relational step

Wave 1 keeps moving the whole collected schema. It gains a table classification that answers "which tables matter" without changing its counts:

- **Stays on {R}:** tables whose source of truth is R in the full target (or, unwritten, read only by R's queries).
- **Leaves later:** tables owned by a purpose-built engine, each with the stage it leaves in (for example "3b").
- **Read on {R} and replicated out:** tables R owns that a purpose-built engine reads through CDC.
- **No collected query reads it:** carried for completeness (wordpress 28 tables, discourse 39).

A **staged relational move** replaces the single cutover only when one of these holds:

1. the source is cross-engine or heterogeneous (ADR-031 Decision 6), or
2. #471 reports a gap with impact "blocks the move" that touches only some tables.

The stages are **relational clusters**: the connected components of the graph whose nodes are tables and whose edges are foreign keys and in-scope queries that touch two tables (any final engine, since at wave 1 every query runs on R). Clusters sort by: no blocking gap first; then fewest #471 conversion items (for a heterogeneous source, #471's per-feature list); then fewest queries; then call share descending; then the first table id. Clusters with no query go in the first stage (copy only, no application change). The gate per cluster: "Schema and query translation validated for these {n} tables; no query reads both this cluster and one still on {source}."

Otherwise (a homogeneous source without a blocking gap) wave 1 stays one cutover: replicating the whole database once is less work and less risk than staging a move the engine family already supports.

## 8. Which paths exist

### 8.1 The projection rule

A path P is a set of engines: R, optionally C, and a subset S of T. Its assignment is the effective assignment projected onto P:

- a query whose final engine is in P keeps it;
- any other in-scope query stays on R;
- a table's source of truth in P is its final source of truth when that engine is in P, and R otherwise; replicas follow from it, with #1032's rule;
- a cached read whose owner is not in P is fronted on R.

This is a view, not an assignment version. Nothing calls `move_ledger.apply`, so ADR-031 Amendment 3's guard is untouched, and the routing snapshot cannot change. Each step of P is the same step the full target takes, so every path's steps are a subset of the waves, and the deliverables never show two different plans for the same table.

### 8.2 Path rules

| Path id | Engines | Offered when | Not offered (reason stated) |
|---|---|---|---|
| `cache_first` (quickest win) | C in front of the current source | C exists | no read clears the cache overlay's hot-read floor |
| `relational_only` | R (+ C) | wave 1 exists with homogeneity `homogeneous` or `cross_engine`, and every in-scope query passes R's capability and ownership gates | heterogeneous with no Aurora engine chosen; or n queries fail R's gates, listed with the engines that serve them |
| `relational_plus_<E>`, one per E in T | R + E (+ C) | every in-scope query whose final engine is in T minus E passes R's gates | replaced by its closure: E plus every engine some failing query needs |
| `full_target` | R + T (+ C) | T is not empty | never withheld: it is the assessment's own plan |

Rules applied after the table:

- **Closure.** When `relational_plus_<E>` fails its condition, the path offered instead is R + E + every engine the failing queries' final engines name, repeated until the condition holds. Its id joins the engine ids in catalog order (`relational_plus_dynamodb_documentdb`).
- **Dedupe.** A path whose engine set equals another's is shown once, under the earlier id in this order: `cache_first`, `relational_only`, `relational_plus_*`, `full_target`. When T has one engine, `relational_plus_<E>` is the full target and only `full_target` appears.
- **Combinations.** Pairs and larger subsets of T are offered only as closures. With three or more purpose-built targets this keeps the menu at most `2 + |T|` paths instead of `2^|T|` (decision 3, section 16).
- **Cache.** The cache is part of every path that includes it in the target, fronting R until the step that moves its reads (today's wave 2 rule). `cache_first` is the only path in which the cache fronts the current source; it moves no data and is reversible.
- **Blocked, not withheld.** A #471 gap with impact "blocks the move" does not remove a path. It becomes the first gate of every path that includes the relational step, and `cache_first` stays the one path it does not block.
- **The default.** `full_target`, staged, is the suggested path, as today. Nothing ranks the other paths: they are listed in the order above.

### 8.3 The one-step option (#473)

`full_target` has two forms: staged (the waves and their stages) and one step (every step's cutover at once, with the target schemas). Both have the same end state and so the same cost, engines, queries and tables. They differ in:

- cutovers: one against the number of steps and stages;
- gates: the one-step gate is every stage gate, verified together before the single cutover;
- risks: the same open risks, all faced at once instead of one stage at a time;
- replication: one step needs no temporary CDC streams, only the permanent replicas of #1032; staged needs, in addition, the reverse streams of each stage c until the later stages finish;
- reversibility: staged can stop after any stage; one step rolls back as a whole.

When it makes sense, as a fixed rule stated in the deliverables: one step suits a workload with no open critical or high risk, at most one purpose-built target, and no stage c (no reverse replica). Otherwise the deliverables state the staged form first. On the three samples none suits one step: wordpress has one target but a stage c (and, in a run with no model, one open high risk); discourse has two targets; trailgear has a stage c, a held stage and two open high risks.

## 9. What each path shows

| Figure | Definition | Source |
|---|---|---|
| End-state engines | R, the included part of T, and C when it is in the path | path rule |
| Queries moved | in-scope queries whose engine in P is not R | projection |
| Queries by engine | count per engine in P | projection |
| Tables migrate | tables whose source of truth in P is not R | projection, #1032 |
| Monthly cost | the sum of `cost_breakdown` for P's engines | `report_facts.engine_costs` |
| Cost difference | against `relational_only` (and against the current cost when `current_cost_known`) | facts |
| Open critical and high risks | the sum of `critical_high_risks_by_engine` over P's engines, plus #471 blocking gaps when P includes the relational step | `risk_facts` |
| Steps, stages and gates | the waves and stages P includes, with their gates in order | `adoption_paths` |
| Replicated tables | per table in P: source of truth, replica engines, CDC direction, the reads that may lag; at most 25 listed, the most replica reads first, the rest counted (#992's cap) | projection, #1032 |
| Temporary replication | the reverse streams stage c needs until the path ends | stages |
| Not offered | each withheld path with its one-line reason | path rule |

Two invariants keep the paths consistent with the rest of the report: `full_target`'s monthly cost equals `facts.projected_monthly_cost_usd`, and its queries moved equals `facts.moving_query_count` (#1020) for a homogeneous source, or that count less wave 1's queries for a cross-engine source, whose relational engine is itself a migration target (trailgear: 95 less 47); `relational_only`'s engines and workload equal `facts.no_migration`.

Cost is per engine, not prorated per stage (ADR-031 Decision 5 keeps the per-query cost dimension out of report pricing). Each path states the existing assumption that the relational engine is sized for the whole collected workload, so the cost of a path that moves queries off R is an upper bound for R.

## 10. Prototype numbers on the samples

Computed on 2026-10-19 with throwaway scripts outside the repository, on the deterministic runs above, reading each table's real `source_of_truth` (#992). One approximation: stage d counts no schema-design coverage gap, since in a run with no model every DynamoDB query has one (decided rule: such a gap holds a unit only when schema design ran, decision 5 in section 16).

Paths (cache included where the path includes it; current cost unknown on all three):

| Sample | `cache_first` | `relational_only` | `relational_plus_*` | `full_target` |
|---|---|---|---|---|
| wordpress | $165.55, 20 reads, 83.4% of calls | $484.35, 107 queries on Aurora MySQL | equals the full target | $582.76 (+$98.41), 39 queries and 15 tables to DynamoDB, 1 high risk |
| discourse | $165.10, 3 reads, 25.1% of calls | $286.16, 1,654 queries on Aurora PostgreSQL | + DynamoDB: $301.36 (+$15.20), 665 queries, 69 tables, 1 high risk; + DocumentDB: $557.96 (+$271.80), 185 queries, 114 tables, 0 high risks | $573.16 (+$287.00), 850 queries, 183 tables, 1 high risk |
| trailgear | $166.12, 16 reads in front of SQL Server | $514.46, 95 queries on Aurora PostgreSQL (cross-engine) | equals the full target | $620.93 (+$106.47), 48 queries and 23 tables to DynamoDB, 2 high risks |

Stages inside the purpose-built steps (units, queries, share of calls):

| Sample, engine | a. Reads on a replica | b. Self-contained | c. With a reverse replica | d. Held by a risk |
|---|---|---|---|---|
| wordpress, DynamoDB (18 units) | 3 units, 4 queries, 0.9% | 2, 2, 0.6% | 13, 33, 54.9% (64 Aurora reads on a reverse copy) | none |
| discourse, DynamoDB (122 units) | 54, 226, 21.3% | 9, 43, 6.1% | 58, 282, 13.9% (237 reverse reads) | 1, 114, 12.0% |
| discourse, DocumentDB (139 units) | 26, 32, 8.8% | 85, 100, 0.8% | 28, 53, 0.9% (30 reverse reads) | none |
| trailgear, DynamoDB (28 units) | 6, 8, 25.5% | 7, 10, 22.7% | 14, 26, 19.3% (40 reverse reads) | 1, 4, 18.2% |

Relational clusters (foreign keys plus shared queries): wordpress 41 (an 8-table core with 47.6% of calls; 28 tables no query reads, and no foreign keys collected), discourse 193 (a 103-table core with 1,113 queries and 57.0% of calls; 34 clusters with no query), trailgear 7 (a 24-table core with 66.3% of calls and six single tables). Only trailgear gets a staged relational move under section 7.2 (cross-engine; its #471 gaps are 5 that need a rewrite, 4 that behave differently and 2 informational, none blocking); its first stage would hold the six single tables.

What the numbers say: discourse can move 226 DynamoDB queries, 21.3% of its calls, before any system of record changes, which the current single group hides, while its one open high risk holds a single 114-query unit (12.0% of calls) instead of the whole wave; on trailgear one unit (4 queries, 18.2% of calls) lands in stage d, held by an open high risk on one of its reads; on wordpress most of the value (54.9% of calls) sits in stage c, so its gate on reverse replication is the one that matters.

## 11. Contract and facts

### 11.1 Data: `report.json`

`SynthesisOutputContract` gains (the next free MINOR after 1.39, main's version on 2026-10-19):

- `migration_waves[].stages: list[WaveStage] | None`. `WaveStage`: `label` ("3a"), `engine`, `kind` (`replica_reads`, `self_contained`, `with_reverse_replica`, `risk_held`, `relational_cluster`), `units: list[MoveUnit]`, `gate`, `gate_parts` (the #1028 shape, so the UI collapses table lists the same way).
- `migration_waves[0].table_classes: RelationalTableClasses | None`: `stays`, `leaves` (table and stage label), `replicated_out`, `unread`.
- `MoveUnit`: `key`, `engine`, `owned_tables`, `replicated_in` (table, source of truth), `reverse_replicas` (engine, read query ids), `query_ids`, `write_query_ids`, `call_share_percent`, `risk_ids`, `pilot: bool`.
- `adoption_paths: list[AdoptionPath] | None`. `AdoptionPath`: `id`, `title`, `engines`, `offered`, `not_offered_reason`, `steps` (wave numbers and stage labels, in order), `queries_by_engine`, `table_sources` (the projected #1032 rows, same `TableSource` model).

### 11.2 Figures: `facts.adoption`

`ReportFacts.adoption: AdoptionFacts | None`, computed in `src/shared/report_facts.py` from the fields above, so every renderer prints the same numbers:

- `paths: list[AdoptionPathFacts]`, offered paths only: `id`, `title`, `engines`, `queries_moved`, `tables_migrate`, `monthly_cost_usd`, `cost_delta_vs_relational_usd`, `cost_delta_vs_current_usd | None`, `critical_high_risks`, `step_count`, `stage_count`, `gate_count`, `replicated_table_count`, `temporary_stream_count`, `first_gate`.
- `not_offered: list[{id, reason}]`.
- `one_step: OneStepFacts`: `cutovers` (1), `staged_cutovers`, `gate_count`, `temporary_stream_count`, `suits_one_step: bool` and `suits_reasons` (the rule of section 8.3, as stated facts, never prose a renderer parses).
- `stages: list[StageFacts]`: per stage its label, engine, kind, unit count, query count, owned and replicated table counts, call share, pilot table names (at most 5) and `more_unit_count`.

Owned figures under ADR-032 Decision 2: every numeric and list field above joins the guard automatically (it is derived from the model), on the Python and the JavaScript side.

### 11.3 Version impact and older reports

- One MINOR bump of `synthesis_output`, with a Version History entry; every new field defaults to None. No change to the assignment, move ledger or analysis contracts.
- An older report has no assignment data in `report.json` to rebuild units from, so `report_facts()` leaves `facts.adoption` None and the `/results` API returns it as None. Every renderer then shows today's waves exactly as now (tested on the saved fixtures, as #1031 did for `gate_parts`).
- `src/report/waves.py` (the legacy wave builder) is unchanged; it never produces stages.

## 12. Presentation: summary first, details on demand

One rule for all three deliverables: the summary is the five-column path table (path, engines, queries moved, monthly cost and difference, open high and critical risks); everything else (stages, units, table names, replicas, gates) is detail.

### 12.1 Executive report PDF (#1020)

- One new page, "Ways to adopt", after "Migration waves": the path table (at most five rows), one line for the one-step form with its fixed "suits one step" sentence, and one line per path not offered.
- The "Migration waves" page gains the stage count per wave ("3 stages: reads first, then ownership, then reverse replicas"), no table names.
- No table names or SQL anywhere, so the page passes `without_table_names`; the pilot is described by its stage and share, never its table.

### 12.2 Engineering report

- New section "Adoption paths" just before "Migration roadmap": the path table, then one short block per path (engines, steps in order, first gate, replicated tables capped at 25) and the not-offered lines.
- "Migration roadmap" keeps its waves; each wave gains "Stages" with, per stage, its gate and its units (the first 10 tables of each, "+N more"), the pilot marked. Wave 1 gains its table classes (capped lists with counts).
- A "One step" subsection compares the two forms of the full target from `facts.adoption.one_step` and keeps today's pointer to "Target schemas by engine".
- The existing "One suggested adoption path, not the only one" sentence now points at the "Adoption paths" section.

### 12.3 Results page (after #1031)

- In the migration roadmap section, a path selector above the wave cards: one chip per offered path, `full_target` (staged) selected when the page opens, so the default view is today's.
- Selecting a path shows its four tiles (engines, queries moved, monthly cost and difference, open high and critical risks) and greys out the waves and stages the path leaves out. No new numbers are computed in the UI: each tile is a `facts.adoption.paths[]` field.
- Each wave card shows "N stages (…)" collapsed, using the `gate_parts` pattern from #1031; opening it lists the stages with their gates, and each stage's units expand the same way.
- "Show the one-step option" toggles a card with the one-step facts and a link to the target schemas.
- New i18n plurals use `_one` and `_other`; the new components join `GUARDED_UI_FILES`.

## 13. Interaction with #471 and #473

- **#471** produces the gaps (`facts.relational_gaps` since contract 1.35); this design consumes them. A gap names its feature, impact, tables, queries and objects. `blocks_move` becomes the first gate of every path with the relational step and can trigger the staged relational move (section 7.2); `needs_rewrite` and `behaves_differently` are listed in wave 1's gate; `informational` stays in the engineering report only. For a heterogeneous source, #471's per-feature conversion list orders the relational clusters. Until #1135 lands, the relational step keeps today's homogeneity note and the staged move triggers only for cross-engine sources.
- **#473** is section 8.3. It needs only `facts.adoption.one_step` and the path table; #1131 and #1132 close it, sharing the paths' wording.
- **Heterogeneous epic (later):** the staged relational move by cluster is the hook; conversion effort per cluster (procedures, triggers, dialect features) slots into the cluster sort key without changing the shape. On the samples the collector's `procedures[].referenced_tables` is empty, so procedure references cannot yet be counted per cluster.

## 14. Guards

Each becomes a test with the implementation (AGENTS.md "Before asking for review"):

- A path never writes an engine: the only-the-ledger guard stays green, and the routing snapshot is unchanged by the whole feature.
- Projection: every query is on an engine of its path; a query on an engine the path includes keeps it; `full_target`'s projection equals the effective assignment.
- Offering: a path is offered only if every query left on R passes R's gates (fixture with a gated query); its closure is offered instead, with the reason.
- Units: section 6's guarantees (partition, write groups whole, placement units whole, order independence).
- Stages: a risk always holds its unit; stage order a, b, c, d; the in-stage sort key; ties broken by the unit key.
- Consistency: `full_target` equals `projected_monthly_cost_usd` and, as section 9 states, `moving_query_count`; `relational_only` equals `no_migration`; every deliverable prints the same path figures (`tests/e2e/test_deliverable_figures.py` extended).
- Renderers only format: the Python and JavaScript guards own the new facts.
- Old reports: a saved report without `adoption_paths` renders exactly as before.
- No engine bias: swapping two purpose-built engines' ids in a fixture swaps their paths and nothing else.

## 15. Implementation issues (filed 2026-10-19 under epic #1128)

1. **#1129 Move units and ordered stages within each migration wave.** `adoption_paths.py`: units (section 6), stages (7.1), wave 1 table classes (7.2, without the staged move), `migration_waves[].stages` and `table_classes`; contract bump. First. No deliverable change yet beyond the stored fields.
2. **#1130 Adoption paths and their facts in report.json.** Projection, offering rules and closure (section 8), `adoption_paths`, `facts.adoption` including `one_step`, consistency invariants and guards. Depends on #1129.
3. **#1131 Engineering report: adoption paths, stages and the one-step option.** "Adoption paths" section, stages under each wave, wave 1 table classes, "One step" subsection (closes #473's engineering-report part). Depends on #1130.
4. **#1132 Results page: path selector, wave stages and the one-step option.** Path selector, tiles, collapsed stages, one-step card (closes #473's UI part). Depends on #1130; independent of #1131.
5. **#1133 Executive report: a Ways to adopt page.** The "Ways to adopt" page and stage counts on the waves page. Depends on #1130.
6. **#1134 Staged relational move by table cluster for cross-engine sources.** Relational clusters and their order (7.2 rule 1). Depends on #1129; can run in parallel with #1131 to #1133.
7. **#1135 Relational gaps gate the adoption paths and order the clusters.** Blocking gaps as first gates, the 7.2 rule 2 trigger and the conversion-item sort key, from `facts.relational_gaps`. Depends on #1134 and #471.
8. **#1136 e2e-llm rubric and judge cover the adoption paths.** Rubric lines and judge reading for the new page and section; one batched run once #1131 to #1133 land.

# 1129 and #1130 touch `synthesis_output.py`, the contract snapshots and `report_facts.py`; land them in order before fanning out #1131 to #1134 (AGENTS.md "Parallel worktrees")

## 16. Decisions (maintainer, 2026-10-19)

The maintainer accepted all eight recommendations below (recorded on #1036). Each question is kept with its answer so the reasoning stays traceable.

1. **Should a path re-route queries, or only project them?** Projection keeps every path inside the target and needs no new routing. Re-scoring with the path's engines only could give a different, possibly better, smaller architecture. **Decided:** projection now; a re-scored "smaller target" would be a reality-check variant and belongs in its own issue.
2. **Within a stage: smallest blast radius first, or highest call share first?** **Decided:** smallest blast radius first, call share as the tie-break, and the pilot named. The stage already groups by coupling, so this order makes the first move the cheapest to undo; a value-first order is one sort key away if customers ask.
3. **With three or more purpose-built targets, list every combination?** None of the samples has three today (OpenSearch is removed on all three). **Decided:** no; single steps, closures and the full target only, which keeps the menu at most `2 + |T|`.
4. **Does the quickest win front the current source, or come after the relational move as today?** Fronting the source is quicker and moves no data, but a source outside AWS adds network distance the collector cannot see (`deployment_type` and `rds_instance_metadata` are empty on the samples). **Decided:** front the current source in `cache_first`, with a fixed line naming the network assumption; keep today's order in every other path.
5. **Should a schema-design coverage gap hold a unit in stage d?** In a run with no model, every DynamoDB query has one, so every DynamoDB unit would be held. **Decided:** yes when schema design ran; when it did not, show the stages unheld with one "schema design pending" line, as `table_basis.design_coverage` already reports.
6. **Cost of a partial step.** Engine costs are not split per stage, and the relational engine is not resized when queries leave. **Decided:** keep per-engine costs and state the upper-bound assumption; resizing the relational engine per path needs a sizing fact per path and is a separate issue.
7. **Staged relational move for homogeneous sources.** **Decided:** only for a blocking #471 gap; otherwise one cutover, since the engine family already supports a whole-database replica.
8. **Is "smarter waves" its own epic, as #472's direction suggests?** **Decided:** yes; epic #1128 under #322 holds the implementation issues, #472 is closed by this design (#1036), and #473 is closed by #1131 and #1132.

## 17. Alternatives considered

- **More waves instead of paths.** Splitting the purpose-built wave per engine and per stage, with no menu. Rejected: it improves ordering but still implies one recommended sequence, which #322 rejects.
- **One assignment version per path.** Each path as a real assignment through the move ledger. Rejected for now: it multiplies versions, schema designs and approvals for alternatives the customer may never pick, and invites the paths to drift from the target (decision 1, section 16).
- **Table groups from co-dependency groups only (today's rule).** Rejected: on all three samples it yields one group, because every co-dependency group sits on the relational engine.
- **Ranking the paths.** A score that recommends one path. Rejected: the choice depends on the customer's appetite for change, which the analysis does not measure; the full target stays the default, staged, as today.
