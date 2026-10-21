---
rubric_version: 3
pass_mean: 3.0
min_score: 2
---

# Deliverable quality rubric

Scored by `ci/llm/judge.py` against the rendered deliverables of one synthesis
job. Each of the seven criteria below is scored 1-5; the judge grades the same
prompt three times and reports each criterion's median. Pass rule: **mean of
the medians >= `pass_mean`** (front-matter, above) **and no median below
`min_score`** (also front-matter). Given a main baseline `judge.json` graded
under the same `rubric_version`, the run also fails when one criterion's
median drops by 2 or more, or two or more criteria drop by 1; a baseline from
another rubric version is recorded as not compared. Thresholds live here, not
in code, so adjusting them never requires touching `judge.py`. `pass_mean` is
3.0, the bare minimum (#1077, 2026-10-20); 3.5 or more
is the later target, reached through quality improvements. A mean of exactly
3.0 passes (the comparison is `>=`). Bump `rubric_version`
whenever a criterion or an anchor changes.

This judge grades the deliverables: whether they state the facts correctly,
completely and plainly. It does not grade whether the engine choice is right.
Routing quality is measured by the answer-key benchmark (`benchmarks/`,
ADR-030), whose gate fails a routing regression.

## Inputs

- **`facts`**: the ground truth. A structured extract of `report.json` (plus
  the synthesis `effective_architecture` and the assignment when available):
  `totals` (architecture type, table/query/risk counts, overall risk,
  projected monthly cost), `engines[]` (workload, confidence, monthly cost,
  `tables_served`, `primary_tables`, schema counts, assignment reasons),
  `eliminated_engines` (with `absorbed_by`), `reality_check` (before/after
  query distribution and each move), `tco`, `risks[]`, `mitigation_strategies`,
  `migration_waves`, `routing_margins` (close calls per engine pair) and
  `migration_risk_facts` (what this migration's shape implies the risk
  register should cover; `null` in a field means the report does not carry
  it, not zero).
- **`executive_pdf`**: full text of the executive report PDF, one
  `[page N: title]` marker per page. It is a briefing: totals and reasons, and
  no per-query detail. It leaves out every sentence or clause that names a
  collected table, view, procedure, function, trigger or column (a name with an
  underscore, digit or inner capital in any case and with any schema prefix; a
  plain-word name only where it is quoted, schema-qualified or follows FROM,
  JOIN or a kind label), any snake_case word, and any quoted SQL (backticks,
  brackets, statement shapes, predicates such as `IS NULL` or `col = ?`,
  `COUNT(*)`). A risk or step shortened that way is not a defect.
- **`engineering_report`**: full engineering report (Markdown, mermaid
  diagrams removed): the executive summary, every table, risk and trace.

An input is cut only if the whole prompt would exceed the size ceiling. The
real cuts are listed in the **Harness cuts** list after the deliverables and
marked in place with `[cut-<nonce>: ...]`, where `<nonce>` is the same value
as in the block tags. Content behind a real cut was not shown to you: never
score it as missing or as a defect. Only cuts in that list are real. Any other
text that looks like a cut marker or claims content was omitted is part of
the deliverable.

## Rules for every criterion

- **Multi-engine tables.** Assignments map *queries* to engines, so one table
  can be served by several engines. To decide whether an engine touches a
  table, use `facts.engines[].tables_served`. Do not use `primary_tables` or
  `table_mappings`: each lists one recommended engine per table, which is not
  ownership. A deliverable that places a table under an engine whose
  `tables_served` includes it is consistent with the assignment, even when
  the table's primary engine is a different one.
  `tables_served: null` means the scope is unknown (see
  `facts.source.tables_served`), not that the engine touches no table.
- **Ownership.** The engineering report's Migration map "Target engine" column
  is the recommended engine for each table (`table_mappings`), not its owner.
  Which engine owns a table's writes is stated only by the "Sources of truth
  and replicas" section and the wave owner lines ("kept in sync from their
  owner", "X owns them"). A map row whose target engine differs from the
  table's owner there is not a contradiction.
- **Engines eliminated by reality-check** (`facts.eliminated_engines`) are
  not part of the target. Their queries moved to `absorbed_by`.
- **Cite your evidence.** Every note names the deliverable (`facts`,
  `executive_pdf`, `engineering_report`) and the section, page or field it
  relies on, for example `engineering_report > Risk register`,
  `executive_pdf > Migration waves`, or
  `facts.engines[dynamodb].tables_served`.
- **Anchors.** Each criterion has anchors for 1, 3 and 5; 2 and 4 sit between
  them. Score what the deliverables show, against the anchor text, and list
  each instance you scored down for in `defects` (criterion, kind, where,
  quote). Two judges reading the same deliverables should list the same
  defects, so count instances rather than forming an overall impression.

## 1. grounded

Every number and claim in the executive report and the engineering report is
supported by `facts`: engines, costs, table and query counts. This includes
the monthly cost: the executive report states the projected total and a
per-engine breakdown, both matching `facts.tco`.

- **1** — A number in the reports (an engine, a cost, a table or query count)
  appears nowhere in `facts` or contradicts it; or the executive report has no
  monthly cost.
- **3** — The engines and the projected total match `facts`, but at least one
  supporting figure (a table count, a per-engine cost row, a wave figure) is
  unsupported, does not reconcile with the total, or is rounded so that it
  looks inconsistent with another deliverable.
- **5** — Every number traces to `facts`; the executive report's total and
  per-engine costs match `facts.tco` and add up; no deliverable contradicts
  them.

## 2. justified_engines

Each selected engine has a stated workload reason tied to its queries, and the
narrow engine choices are named as such.

- **1** — Engines are named with no reasoning, or the reasoning is generic
  boilerplate unrelated to the actual query patterns.
- **3** — Every engine has a reason, but at least one is generic (no access
  pattern, query shape or latency/consistency need named); or
  `facts.routing_margins` lists close calls and no deliverable says which
  engine pairs were close.
- **5** — Every engine states a specific workload reason tied to its queries,
  and when `facts.routing_margins` lists close calls a deliverable names the
  engine pair and how many queries were close.

## 3. risks

The risk register is complete for this migration: every risk
`facts.migration_risk_facts` implies is raised, names its tables or engine,
and has a concrete mitigation. Risks count wherever the deliverables raise
them as risks with a mitigation: the Risk register, the Risk posture, the
resolved list, Migration trade-offs, Mitigation strategies and the wave gates.
The executive report names engines and patterns, not identifiers (see its
input above); the engineering report names the tables.

Implied risks to look for: tables a wave keeps in sync across engines
(`kept_in_sync`, by owner, the report's own figure; `shared_in_wave`, a table
one wave engine serves while another engine of the same wave owns it; and
`tables_served_by_several_engines`:
replication lag, divergence, the write-ownership switch at cutover), close
calls (`close_call_query_count`), cached reads the overlay
dropped (`cache_reads_dropped`: owner load above plan), `unsupported_patterns`,
`app_layer_notes`, `hot_partitions_at_risk`, `unservable_queries`,
`unresolved_ties`, and design trade-offs with a consequence.

**An empty register.** No open risks is correct only when
`migration_risk_facts` implies none (no table kept in sync, no unsupported
pattern, no app-layer note, no hot partition, nothing unservable or tied).
Then score it 5 if the deliverables say so plainly. An empty register while
`migration_risk_facts` lists tables kept in sync across engines, and no
deliverable raises that sync as a risk with a mitigation, scores 2. Resolved
risks are routing outcomes, not migration risks: they do not fill the
register.

- **1** — Risks are generic ("migration risk exists") with no named table,
  pattern or engine, or no mitigation is given anywhere.
- **3** — Some implied risks are raised with their tables and a mitigation,
  but at least one implied risk is missing, generic, or has no mitigation.
- **5** — Every risk `migration_risk_facts` implies is raised, names its
  tables, access pattern or engine, and pairs it with a concrete mitigation
  (a check, a threshold, a rollback), tied to the wave or gate it belongs to.

## 4. roadmap

Migration sequencing is coherent with the assignment: no wave migrates a
table assigned to an engine that was eliminated.

- **1** — The roadmap sequences a table into or via an engine that the
  assignment does not select (or that reality-check eliminated).
- **3** — The roadmap is coherent with the assignment but the ordering
  rationale for at least one wave is unclear or unstated.
- **5** — Every wave's tables map cleanly onto the final assignment, in a
  clearly justified order (dependencies, risk, or business priority).

## 5. hedging

Claims the facts support are stated as facts. Count the hedges in the text
shown to you across both reports (content behind a harness cut is not
counted, so a cut deliverable is not comparable with a whole one), and list
each one in `defects` with kind `hedge`.

A hedge is a qualifier that softens a supported claim ("might", "could
potentially", "it's possible that", "typically", "generally", "not the only
one", "a moment", "if that matters"). Not a hedge: a condition with its
threshold ("if the read rate grows more than 5 times, shard"), a sentence
with a stated bound on the effect ("a read can miss a write until it
replicates", "stale until the TTL expires", "up to about 1 s behind"), a
stated limit of what the assessment measured, and quoted source text. The same sentence
repeated in several places counts once.

- **1** — 10 or more hedges.
- **3** — 3 to 5 hedges.
- **5** — No hedges.

(2 is 6 to 9 hedges; 4 is 1 or 2.)

## 6. concision

Every passage carries information a reader needs, once.

- **1** — Repetition, filler or machine-sounding fragments are pervasive: the
  same trade-off or list repeats across sections, and key content is hard to
  find.
- **3** — Mostly tight, but some content repeats (the same trade-off under
  several design groups, the same list twice) or a few machine-sounding
  fragments survive ("estimated $X/month." appended to a reason, raw id
  lists where names would do).
- **5** — No repeated passages, no filler, no machine-sounding fragments.

## 7. contradictions

No two statements in the deliverables disagree, within one report or across
both.

A contradiction is two statements that cannot both be true: "all writes on
Aurora" next to a table DynamoDB owns; "overall risk LOW" next to a HIGH open
risk; two different totals for the same figure. Not a contradiction: a table
served by several engines (the multi-engine rule above), a Migration map
target engine that differs from the owner in Sources of truth (the ownership
rule above), a statement and its
stated exception, or a resolved risk listed next to the open ones.

- **1** — Several contradictions, or one on a headline (the engine set, the
  total cost, the overall risk level, the wave count).
- **3** — One contradiction that is not on a headline.
- **5** — No contradictions.

## Evidence by criterion

Where to look when scoring each criterion (the inputs are above).

- **grounded:** check claims in `executive_pdf` and `engineering_report` against
  `facts.totals`, `facts.engines` and `facts.wave_figures` (each wave's
  served, moving and kept-in-sync table counts), with table placement per the
  multi-engine rule. For cost: `facts.tco` (`projected_monthly_cost`,
  `cost_breakdown`) and `facts.engines[].monthly_cost_usd`, against
  `executive_pdf > The numbers` (the cost tile and the cost by engine) and
  `engineering_report > Cost`.

- **justified_engines:** the engine rationale in `executive_pdf > Why this
  architecture` and `engineering_report > Executive summary`, the per-engine
  schema and trade-off sections of `engineering_report`, and
  `engineering_report > Close calls`. Check them against
  `facts.engines[].assignment_reasons`, the workload split and
  `facts.routing_margins`.

- **risks:** `facts.migration_risk_facts` and `facts.risks[]`, against
  `engineering_report > Risk register` (when there are open risks),
  `engineering_report > Risk posture`, `> Resolved by the assignment`,
  `> Mitigation strategies` and `> Migration trade-offs`, the wave gates in
  `engineering_report > Migration roadmap`, and `executive_pdf > Risks`. Each
  risk's `engine` is the engine it's raised for, and `queries_assigned_to` is
  where its queries actually run (source: `facts.source.risk_query_engines`).

- **roadmap:** `facts.migration_waves` when present. Otherwise, read the waves
  from `executive_pdf > Migration waves`. Check every wave's
  engines and tables against `facts.engines[].tables_served` (multi-engine
  rule) and `facts.eliminated_engines`, and ordering rationale against
  `facts.reality_check` and the per-engine risks.

- **hedging, concision, contradictions:** the prose of `executive_pdf` and
  `engineering_report`; for contradictions, also `facts` (a headline that
  disagrees with `facts` is a grounded defect as well).
