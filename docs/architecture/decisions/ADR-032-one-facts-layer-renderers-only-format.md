# ADR-032: One Facts Layer in Synthesis; Renderers Only Format

**Status:** Accepted
**Date:** 2026-10-14
**Deciders:** Database Modernizer Assessment Architecture Team

## Context

Every deliverable (the decision report, the engineering report, the executive deck and its PDF, and the interactive analysis report the UI exports) renders one synthesis `report.json`. AGENTS.md's "Deliverables agree" invariant says a fact changed in one must change in all. In practice each deliverable computed its own figures from the raw fields, and the copies drifted:

- the merged DynamoDB/DocumentDB wave headline subtracted the deduped `table_owners` list from `table_count`, while the "Tables migrate" tile summed `owned_counts_by_engine`, so the two disagreed whenever one engine of the wave served a table the other owned (#581);
- the deck's "Basis of the analysis" bullet took a partial `table_mappings` as the complete, fit-scored table count, while the tile added the migration-wave fallback of the engines with no schema design yet (#533);
- wave 1 stated 312 "source tables and views" next to "Analyzed 311 source tables", with nothing saying the extra one is a real view (#578);
- earlier, the deck truncation and the "400 tables migrate" out of 311 double count (#568), and three copies of the eliminated-engine rule (#235).

The deliverable-consistency e2e test (#573) catches such a disagreement after it happens; it does not remove the reason the copies exist.

## Decision

### 1. Synthesis computes every customer-facing figure once, into `report.json`

`src/shared/report_facts.py` (`compute_report_facts`) computes the figures the deliverables print: each engine's role, workload, cached reads, source tables, tables that migrate and monthly cost; the totals (engines, tables migrate, workload, cost, the projected monthly cost); the table basis behind "N tables mapped" with its design coverage; each wave's served, moving and kept-in-sync tables and wave 1's tables and views; the no-migration step's figures; the open and resolved risk counts by severity, type and engine; the tied-query count. Synthesis writes the result to `report.json` as `facts` (`SynthesisOutputContract.facts`, `ReportFacts`, contract 1.14). A new figure a deliverable needs is added here first.

### 2. Renderers only format

The modules under `src/report/` read figures through `src.report.facts.report_facts` and format them: they never count, total, add, subtract or filter a figure the facts layer owns. The owned figures are every numeric, record and record-list field of the `ReportFacts` contract (derived from the model, so a new fact is owned when it is added) plus the `report.json` inputs they are computed from. A sum of percentages is stored unrounded, so each renderer rounds it once, to the places it prints. Counting what a list shows ("+N more" after the first 20 names) is formatting; a line that computes on a fact for presentation only (chart geometry, verb agreement) says so with `# facts: presentation (<reason>)`, and the exemptions in use are pinned in the guard test. The legacy wave builder (`src/report/waves.py`, for a report synthesized before `migration_waves` existed) produces data, not output.

### 3. Layering

`src/report/` imports nothing from `src/agents/`; the facts layer imports neither `src/agents/` nor `src/report/`. The layer lives in `src/shared/` so synthesis can write it and the renderers can compute the same block for an old report, from one implementation.

### 4. One reader, one answer for an old report

`report_facts(report)` returns the stored `facts`, and for a report synthesized before they existed computes them with the same `compute_report_facts`, over the waves `resolve_migration_waves` resolves for it. The interactive analysis report's export data (`build_export_data`) and the `/results` API (`src/api/routes/results.py`) always carry `facts`, filling them in with the same function for an older report; the UI's own exports read them through `src/ui/src/utils/reportFacts.js` and only format them.

### 5. The consistency test stays underneath

`tests/e2e/test_deliverable_figures.py` keeps reading every deliverable's figures back and comparing them with `report.json`, and also checks that the stored `facts` are exactly what the layer computes from the report's own fields.

### 6. Labels and reasons are facts too

A deliverable never reads a decision back out of prose. The triage signals that overrode queries onto an engine are `facts.engines[].signal_overrides`, from the assignment's own `signal_override` fields; the reason text itself names the signal and the engine by their readable forms (#584). The engines the reality check removed are `facts.removed_engines`, judged against the final architecture of the effective assignment, so an engine a customer edit moved queries back onto is never listed; a reality-check consolidation line that edit reversed is `reversed` in `facts.reality_check_trade_offs` and no deliverable states it as current (#546). The reality check's own consolidation lines are parsed once, in the facts layer, never by a renderer.

### 7. Every deliverable shows the reality-check decisions

The reality check's own trade-offs that still apply (the engines it consolidated, the engines its sanity sweep kept, the patterns it recommends) are `facts.reality_check_decisions`, each with its kind, engine, title and detail. The decision report lists them under "Reality check decisions" (a consolidation of an engine "Engines not used" already lists is stated there once), the deck gives them a "Reality Check Decisions" slide when there are any, the engineering report keeps its "Migration trade-offs" section, and the interactive analysis report and the UI's results page list them in a "Reality check" trade-offs tab (#595).

## Enforcement

| Decision | Enforced by |
|---|---|
| 1. Synthesis computes every customer-facing figure once | `tests/unit/agents/referee/test_synthesis_migration_waves.py::TestMigrationWaves::test_written_report_carries_the_facts_block`<br>`tests/e2e/test_deliverable_figures.py::test_report_json_facts_are_the_facts_layer_output` |
| 2. Renderers only format | `tests/unit/report/test_renderers_only_format.py::test_no_renderer_does_arithmetic_on_a_fact`<br>`tests/unit/report/test_renderers_only_format.py::test_the_guard_flags_every_counting_shape`<br>`tests/unit/report/test_renderers_only_format.py::test_every_figure_field_of_the_facts_contract_is_owned`<br>`tests/unit/report/test_renderers_only_format.py::test_presentation_exemptions_carry_a_reason_and_are_pinned`<br>`tests/unit/report/test_report_facts.py::test_renderers_read_the_stored_facts_not_their_own_copy` |
| 3. Layering | `tests/unit/report/test_renderers_only_format.py::test_report_imports_nothing_from_agents`<br>`tests/unit/report/test_renderers_only_format.py::test_the_facts_layer_imports_neither_agents_nor_report` |
| 4. One reader, one answer for an old report | `tests/unit/report/test_report_facts.py::test_a_report_without_facts_gets_the_same_block_synthesis_writes`<br>`tests/unit/api/test_results.py::TestGetResults::test_backfills_facts_for_a_report_written_before_615` |
| 5. The consistency test stays underneath | `tests/e2e/test_deliverable_figures.py` |
| 6. Labels and reasons are facts too | `tests/unit/report/test_report_facts.py::test_584_the_stored_fact_decides_not_the_reason_text`<br>`tests/unit/report/test_report_facts.py::test_546_no_deliverable_claims_the_removal_or_its_saving`<br>`tests/unit/test_assignment_resolver.py::TestAssignmentReasons::test_signal_override_reason` |
| 7. Every deliverable shows the reality-check decisions | `tests/e2e/test_deliverable_figures.py::test_every_deliverable_shows_the_reality_check_decisions`<br>`tests/unit/report/test_report_facts.py::test_595_the_decision_report_deck_and_engineering_report_show_them` |

## Consequences

- A deliverable can no longer disagree with another on a figure the facts layer owns: both print the same field. The renderers lose their own arithmetic (`architecture._architecture_engines` and the wave headline are formatters over facts now).
- `report.json` grows by the facts block (a few kilobytes on the discourse sample).
- A new deliverable, or a new figure in an existing one, starts by adding the figure to `compute_report_facts` and its contract model; the guard test fails if a renderer computes it instead.

## Related

Issues #615 (this change), #533, #578, #581, #584, #546, #595, #568, #573 and #235; ADR-031.
