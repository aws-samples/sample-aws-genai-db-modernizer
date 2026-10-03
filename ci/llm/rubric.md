---
pass_mean: 3.5
min_score: 2
---

# Deliverable quality rubric

Scored by `ci/llm/judge.py` against the rendered deliverables of one synthesis
job: `report.json`, the decision report (HTML), the engineering report
(Markdown), and the executive summary PDF. Each of the six criteria below is
scored 1-5. Pass rule: **mean >= `pass_mean`** (front-matter, above) **and no
criterion below `min_score`** (also front-matter). Thresholds are tuned over
the first live runs and live here, not in code, so adjusting them never
requires touching `judge.py`.

## 1. grounded

Every claim in the executive summary and decision report is supported by
`report.json` (engines, costs, table counts); no invented numbers.

- **1** — Numbers in the report (engine names, costs, table/query counts) do
  not appear anywhere in `report.json`, or contradict it outright.
- **3** — Core facts (selected engines, overall cost figure) are grounded,
  but at least one supporting detail (a table count, a secondary cost
  component) is unsupported or not traceable to `report.json`.
- **5** — Every number and claim traces cleanly back to `report.json`; no
  invented figures anywhere.

## 2. justified_engines

Each selected engine has a stated workload reason tied to query patterns.

- **1** — Engines are named with no reasoning, or the reasoning is generic
  boilerplate unrelated to the actual query patterns.
- **3** — Most engines have a workload-specific reason; at least one engine
  is asserted without one.
- **5** — Every selected engine states a specific workload reason (access
  pattern, query shape, consistency/latency need) tied to the data.

## 3. cost

Monthly cost/TCO is present and consistent across the decision report, the
engineering report, and the PDF.

- **1** — Cost is missing from one or more deliverables, or the figures
  across deliverables disagree.
- **3** — Cost is present everywhere but rounding/presentation differs
  enough to look inconsistent (e.g. one deliverable omits a cost
  component the others include).
- **5** — The same monthly cost/TCO figure (or a clearly reconciled
  breakdown of it) appears consistently across all three deliverables.

## 4. risks

Risks are specific (named tables/patterns/engines), each with a mitigation.

- **1** — Risks are generic ("migration risk exists") with no named
  table/pattern/engine, or no mitigation is given.
- **3** — Most risks name a specific table/pattern/engine and a mitigation;
  at least one risk is generic or lacks a mitigation.
- **5** — Every risk names a specific table, access pattern, or engine, and
  pairs it with a concrete mitigation.

## 5. roadmap

Migration sequencing is coherent with the assignment: no wave migrates a
table assigned to an engine that was eliminated.

- **1** — The roadmap sequences a table into or via an engine that the
  assignment does not select (or that reality-check eliminated).
- **3** — The roadmap is coherent with the assignment but the ordering
  rationale for at least one wave is unclear or unstated.
- **5** — Every wave's tables map cleanly onto the final assignment, in a
  clearly justified order (dependencies, risk, or business priority).

## 6. tone

No hedging, no filler, no contradictions between sections.

- **1** — Hedging language ("might", "could potentially", "it's possible
  that") or filler padding is pervasive, or sections contradict each other.
- **3** — Mostly direct, but at least one hedge, filler phrase, or minor
  cross-section contradiction survives.
- **5** — Direct and confident throughout; no hedging, no filler, no
  contradictions between sections.
