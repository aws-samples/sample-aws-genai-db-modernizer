# Adding or Changing an Analysis Engine

**Audience:** Engineers and AI assistants adding an engine to the pipeline or changing how one is analysed
**Reference implementations:** DynamoDB (the most complete), OpenSearch (a search read model, the smallest), ElastiCache (a cache that owns no query)
**Routing model:** [ADR-031](../architecture/decisions/ADR-031-query-first-routing-and-engine-rule-catalog.md). Read it before touching anything that scores or places a query; AGENTS.md "Routing changes" lists the rules a change must keep.

---

## Overview

An analysis agent reads the collector's output and reports how well one target engine fits the workload: patterns found, anti-patterns, a per-table recommendation, a cost estimate and a decision trace. It is deterministic. An optional model advisor (`--llm-mode bedrock`, `src/tools/analysis/llm_advisor_base.py`) can comment on queries the patterns did not match, but it never changes a score.

The analysis does not decide where a query goes. Routing does, in the Assignment phase, with one scorer (`query_scoring.score_query_for_engine`) that reads the **rule catalog**, and the move ledger records every engine change (ADR-031). So adding an engine is mostly catalog data plus the engine's own detection code:

> **Adding an engine** means one engine file, one entry per pattern file (the completeness check lists exactly which), and the engine's own detection code where a pattern has no existing detector. (ADR-031 Decision 8)

### Core principles

1. **The query is the unit.** Pattern matching, scoring and routing work per query. Table-level recommendations are a derived summary.
2. **The catalog is the source of rules.** Scores, supports, penalties, weights, prices and thresholds are TOML under `src/catalog/`. The Python catalog modules are thin loaders ("Edit the TOML file, not this module"). Never add a private fit function or an engine literal to routing code; `tests/unit/shared/test_rule_catalog_complete.py` fails on one.
3. **Detection is engine-neutral and stays in code.** A detector reports a fact about a query (a wildcard position, a transaction's entity count). The catalog turns the fact into each engine's support and score, so one fact reads the same whichever engine is scored. SQL facts come from the declared fact modules (`src/tools/sql/query_facts.py`, `facade.py`, `regex_facts.py`), read in the source engine's dialect.
4. **A model may explain routing, never decide it.** Assignment is deterministic.
5. **A decision trace is required.** Every engine writes `decision-trace.json` next to `analysis.json`, for calibration and review.

### Engine kinds

The engine file's `status` and `role` say what the engine is for:

- `status = "active"` is a routing target. `future` is a named placeholder (Neptune, Keyspaces) that is never analysed, designed or routed (ADR-031 Decision 9). `legacy` is an id only older reports carry (the generic `aurora`).
- `system_of_record` says whether the engine can durably own a table. DynamoDB, DocumentDB and the Aurora engines do. OpenSearch is a search read model and ElastiCache is a cache; neither owns a query or a write.

### Data flow

```text
CollectorOutputContract (dict)
        |
        v
AnalysisInput(collector_output=..., target_database=...)
        |
        v
analyze_for_<engine>(analysis_input)
        |-- per-query pattern matching against the catalog     -> WorkloadAnalysis
        |-- table profiles and scoring (scoring.py + adjustments) -> TableRecommendation[]
        |-- engine-specific structural detection (optional)    -> decision trace
        |-- aggregate identification (optional, shared)        -> aggregate_recommendations
        |-- cost estimate from the price table                 -> CostEstimate
        '-- decision trace                                     -> decision-trace.json
        |
        v
AnalysisOutputContract + decision trace   (analysis-<engine>/ in the artifact store)
        |
        v
Assignment: the one scorer, write groups, tie-break, move ledger (ADR-031)
```

---

## Step 1: Add the engine file

Create `src/catalog/engines/<engine>.toml`. The fields and what each means are documented at the top of `src/shared/catalog/engines.py` (a missing, unknown or mistyped field fails at import). The essentials:

| Field | Purpose |
|---|---|
| `id` | The engine id the pipeline emits; must equal the file name |
| `display_name`, `display_order`, `ui_picker_order` | The AWS-branded name and the order in the display table and the review gate's picker |
| `status`, `role`, `system_of_record` | What the engine is (see above) |
| `[colors] ui` | The engine's one colour in every view |
| `[pricing]` | `base_monthly`, burden fields and `list_prices = "<engine>.<configuration>"`, the price-table configuration the engine is priced on |
| `[score_weights.<profile>]` | Pattern match, complexity, performance and cost weights, summing to 1; the first profile is the default |
| `[justification_floor]`, `[thresholds.*]` | What a standing engine must earn, each with its reason (optional) |
| `source_engines` | For a relational target, the source dialects it is native to |

Cost carries the same 10% weight for every engine (ADR-031 Decision 5, #1002); the loader rejects a file that differs.

Never write a price in an engine file or an estimator. List prices come from the one dated price table, `src/catalog/prices.toml` (us-east-1, on-demand), written by `uv run python scripts/refresh_prices.py` from the public AWS Price List files. A new engine adds its section to that script's `SPECS` and runs it.

## Step 2: Add the pattern entries and the gates

1. **Pattern files.** `src/catalog/patterns/<pattern_type>.toml` has one `[engines.<id>]` entry per engine: whether it supports the pattern, its `kind` (a `fit` with a `score`, an `anti_pattern`, `native`, `scan`, `redesign`, `penalized`, `unscored` or `future`), and a `why` that feeds the decision trace. The completeness check fails with the exact list of missing entries, so run it and fill what it names:

   ```bash
   uv run pytest tests/unit/shared/test_rule_catalog_complete.py tests/unit/shared/test_pattern_catalog.py tests/unit/shared/test_engine_catalog.py -q
   ```

   A penalty names a `penalty_basis` and the points are fixed by the basis, the same for every engine (ADR-031 Amendment 6); the loader rejects any other number. A fit pattern's detector must test the access pattern its reason describes.
2. **Hard gates.** `src/catalog/gates.toml` lists, per capability, the engines that have it, and the exclusions that bar an engine from a query. A capability the new engine lacks or has changes which queries it can serve at all. The gate and the pattern files' `supported` flags must agree (the loader checks).
3. **Triage signals.** `src/catalog/triage_signals.toml` names, per signal, the engines the signal is evidence for. Triage selects every active, query-owning engine whatever the signals say; only a cache overlay engine depends on a signal (`src/agents/referee/triage.py`). The list of analysed engines comes from the catalog (`catalog.engine_ids(status="active")`), so no triage code change is needed.
4. **Access classes.** `src/catalog/access_classes.toml` says which pattern files describe each access-pattern class. A class's score on an engine is the highest rating the listed pattern files give it, so the class needs no engine-specific entry. A write is rated only on a system of record.
5. **Regenerate the generated files.** `uv run python scripts/render_catalog.py` rewrites `docs/architecture/rule-catalog.md` and the UI's `engineCatalog.generated.js`; `--check` runs in lint. Never hand-edit them.

## Step 3: Write the detection

Create the engine's analysis tools in `src/tools/analysis/<engine>_analysis_tools.py` and a thin catalog loader `<engine>_pattern_catalog.py`, modelled on the OpenSearch pair:

```python
# src/tools/analysis/<engine>_pattern_catalog.py
from src.shared.catalog import score_weights
from src.tools.analysis.pattern_catalog_base import catalog_anti_patterns, catalog_patterns

<ENGINE>_PATTERNS = catalog_patterns("<engine>")
<ENGINE>_ANTI_PATTERNS = catalog_anti_patterns("<engine>")
<ENGINE>_WEIGHTS = score_weights("<engine>")
```

`analyze_<engine>_use_cases(collector_output)` walks the queries and tables and returns a `WorkloadAnalysis`. Rules of thumb that hold for every engine:

- A query can match several patterns: use independent `if`s, not `elif`.
- Prefer structural signals (query type, key usage, join count, frequency) over text matching, and combine them for confidence. Text-only matches are `MEDIUM` at best.
- A scan once an hour is fine; one a second is not. Frequency matters for an anti-pattern.
- Use the `Confidence` enum to say how certain a match is, and validate each rule against real collector output: track false positives (`"user_id" in text` matches any user-table query).
- A new SQL fact belongs in the declared fact modules, not in the engine's tools, so every engine reads it the same way. The guard `tests/unit/shared/test_rule_catalog_complete.py` flags SQL keywords and engine literals outside them.
- Engine-specific structural detection (DynamoDB's key classification, GSI candidates and denormalisation sub-types; DocumentDB's embedding candidates) goes to the decision trace, not the output contract.

## Step 4: Score tables with the shared layer

`src/tools/analysis/scoring.py` is the engine-agnostic scoring layer for the per-table recommendations the deliverables summarise. Reuse it; do not fork it.

```python
from src.shared.catalog import patterns as catalog_patterns
from src.tools.analysis.scoring import (
    build_table_profiles,   # aggregate per-table schema, query and pattern stats
    compute_base_scores,    # evidence-based 0..100 scores, four dimensions
    compute_confidence,     # weighted confidence, accepts the engine's weights
)

profiles = build_table_profiles(collector_output, workload_analysis)
for table_id, profile in profiles.items():
    scores = compute_base_scores(profile)
    scores = _apply_<engine>_adjustments(scores, profile)
    confidence = compute_confidence(scores, weights=<ENGINE>_WEIGHTS)
```

All four dimensions (`pattern_match`, `complexity`, `performance`, `cost`) start at 0 and earn points from evidence, so a table with no queries and no patterns scores near zero. The adjustment function adds only the structural bonuses and penalties no single pattern explains. A bonus for a pattern the engine excels at is catalog data: the pattern file's fit entry carries `table_bonus` (and `complexity_bonus`, `bonus_group`, `complexity_feature`), read with `catalog_patterns.table_bonuses("<engine>", profile.pattern_types)`.

| Confidence | Recommendation |
|------------|---------------|
| >= 80 | HIGHLY_SUITABLE |
| >= 60 | SUITABLE |
| >= 40 | MARGINAL |
| < 40 | NOT_SUITABLE |

**These table scores do not route queries.** Routing scores each query with `query_scoring.score_query_for_engine`: the query's pattern dimension is its own access-pattern class's score on the engine (ADR-031 Decision 14, Amendments 5 and 7, `query_pattern.QueryPatternModel`), its cost dimension is its own estimated monthly cost from the engine file's `[pricing]`, and only complexity and performance come from the query's tables. A change to routing behaviour is a change to the catalog or to that scorer, reviewed against ADR-031.

For engines where related tables should migrate together, `scoring.detect_co_access()` and `scoring.identify_aggregates()` group tables by foreign keys and query co-access into `aggregate_recommendations`. They are optional: leave the field `None` for an engine whose tables migrate independently.

## Step 5: Write the decision trace

Every engine's trace is a versioned contract in `src/contracts/`: `DynamoDBDecisionTrace`, `OpenSearchDecisionTrace`, `AuroraDecisionTrace` and so on. A trace that shares the core sections (`trace_version`, `agent`, `summary`, `query_matches`, `pattern_summaries`, `recommendation_derivations`) extends `DecisionTraceBase` (`src/contracts/decision_trace_base.py`); read the OpenSearch one for a small example.

1. Add `src/contracts/<engine>_decision_trace.py`.
2. Register its path in `src/shared/contract_versioning.py` (`analysis-<engine>/decision-trace\.json`). `write_decision_trace` (`src/agents/analysis/decision_trace.py`) refuses a trace with no registered contract, and `tests/contract/test_declared_artifacts.py` fails on an unregistered written file.
3. A new contract starts at its own version; `docs/contracts/agent-contracts-spec.md` and CONTRIBUTING.md "Contract Changes" cover bumps to existing ones.

The trace records, per query, which patterns matched and the signals behind each match; per pattern, the catalog base score, the adjusted score and the reasons; and per table, how the segments combined into the recommendation, with the weights used. Specialists read it to calibrate: run the agent on real collector outputs, classify each pattern's matches as true positive, false positive or false negative, adjust the catalog scores and detection, and repeat.

## Step 6: Write the entry point and register it

The agent is a thin orchestrator. Model it on `src/agents/analysis/opensearch_analysis_agent.py`:

```python
def analyze_for_<engine>(analysis_input: AnalysisInput) -> tuple[AnalysisOutputContract, dict, str]:
    workload = analyze_<engine>_use_cases(collector_output)
    recommendations, classifications, weights = analyze_<engine>_patterns(collector_output, workload)
    cost = estimate_<engine>_costs(collector_output, ...)
    trace = build_<engine>_decision_trace(...)
    return AnalysisOutputContract(..., agent_metadata=AgentMetadata(
        target_database=TargetDatabase.<ENGINE>, ...)), trace, ""   # "" = no ER diagram
```

`AnalysisOutputContract` takes its `contract_version` from the model's default (`tests/unit/agents/analysis/test_analysis_writes_the_model_version.py`); never write a literal.

Registration points, found by following an existing engine (`git grep -l opensearch -- src scripts .claude` lists every site for the smallest one):

| Where | What |
|---|---|
| `src/contracts/analysis_output.py` | Add the id to `TargetDatabase`. A new enum value is a MINOR bump, declared with `scripts/bump_contract.py` (CONTRIBUTING.md) |
| `src/agents/analysis/handler.py` | A `case "<engine>":` that runs the agent and writes `analysis.json`, `decision-trace.json` and the ER diagram if any |
| `scripts/run_analysis.py` | The engine list, the `TargetDatabase` map and the dispatch |
| `src/agents/entrypoint.py` | `ANALYSIS_AGENTS`. It serves the `agent-load-test` image's dispatch only, not the local pipeline |
| `src/orchestrator/local_orchestrator.py` | The artifact path list `_infer_progression` uses to tell that analysis ran |
| `.claude/commands/analyze-<engine>.md` | The analysis command (carries the shared tool-use paragraph; see `/add-modernizer-skill`) |

## Step 7: Cost estimation

Each engine prices differently. The estimator should use the simplest pricing model as the default, derive throughput from the collector's query patterns (`calls_per_second`, read/write ratio) and storage from table sizes, list every assumption in `pricing_assumptions`, and leave optional features (DAX, global tables, backups) out unless asked. It reads figures through `catalog.pricing(engine)`; bill a month as `HOURS_PER_MONTH` (730) and request volumes over `SECONDS_PER_MONTH` (`src.shared.catalog.prices`), and record the table's date in the estimate (`cost_components["price_list"] = price_basis()`) so the deliverables can state it.

| Engine | Default pricing (price table section) | Key inputs |
|--------|----------------|------------|
| DynamoDB | On-demand request units and storage (`dynamodb.on_demand`) | calls_per_second, read/write ratio, total size |
| ElastiCache | Node-based Valkey (`elasticache.valkey`) | working set size, connections |
| DocumentDB | Instance plus Standard storage I/O (`documentdb.standard`) | storage, read/write IOPS |
| OpenSearch | Managed domain on gp3 EBS (`opensearch.managed`) | index size, query rate |
| Aurora | I/O-Optimized instance, ACU and storage (`aurora_<engine>.io_optimized`) | calls_per_second, total size |

## Step 8: Error handling

Handle partial or missing collector output without crashing:

- **Zero queries:** recommend from schema structure only; set `pattern_match_score = 0` for all tables and say so in the trace.
- **Zero tables:** return an empty recommendation list and log a warning.
- **Missing metrics:** skip performance scoring and set `performance_score = 0`.
- **Partial output:** process what is there; never fail on a missing optional field.

The minimum viable input is one table with one column.

## Step 9: The rest of the engine

The analysis is the first half. An engine that routes queries also needs:

- **A schema designer** (`src/tools/schema/<engine>_schema_agent.py`, built on the shared `base_schema_agent.py`; read `opensearch_schema_agent.py` for the smallest), its model-output contract (`src/contracts/<engine>_model_output.py`), a `case` in `src/agents/schema_design/handler.py`, the engine in `scripts/run_schema_design.py` (engine list, skill path, contract class), a data-modelling prompt in `src/skills/`, and `.claude/commands/design-schema-<engine>.md` with its `Skill(design-schema-<engine>)` rule in `.claude/settings.ci.json`. See [strands-agent-development-guide.md](strands-agent-development-guide.md) for the seam pattern and the PE-review loop.
- **The AWS Transform fleet**, if the engine should be designed there (`src/atx_orchestrator/subagents/schema.py` and `tools.py`). See [aws-transform-integration.md](aws-transform-integration.md).
- **A load test**, optionally: [load-testing-new-engine.md](load-testing-new-engine.md).
- **The library contract:** a designed engine appears in `assess()`'s design block, which is a MINOR change to that contract ([assess-design-block.md](../contracts/assess-design-block.md)).

## Step 10: Tests and fixtures

Write tests first. Three fixture layers:

1. **Per-pattern fixtures (engine-specific):** small dicts that trigger exactly one pattern, for isolation tests. Reference: `tests/fixtures/dynamodb_pattern_fixtures.py`, `documentdb_pattern_fixtures.py`, `opensearch_pattern_fixtures.py`.
2. **Vertical fixtures (shared):** realistic collector outputs several engines analyse. Reference: `tests/fixtures/ecommerce_collector_output.py`, `gaming_collector_output.py`.
3. **Synthetic generator (shared):** for benchmarks, `tests/fixtures/generate_synthetic_collector_output.py`.

Then: per-pattern isolation tests and a vertical integration test under `tests/unit/agents/analysis/test_<engine>_analysis_agent.py`; a contract test that the output and the trace validate against their models; the catalog completeness tests above; and the routing snapshot. A new engine changes routing results, so regenerate `tests/e2e/routing_snapshot.py`'s baseline on top of main and explain each query that moved (AGENTS.md "Routing changes"); never bend a fixture to keep a result.

## Checklist for a new engine

- [ ] `src/catalog/engines/<engine>.toml`, with weights, pricing and `list_prices`; prices added via `scripts/refresh_prices.py`
- [ ] An `[engines.<engine>]` entry in every pattern file; completeness tests green
- [ ] Gates, triage signals and access classes reviewed; `scripts/render_catalog.py` run
- [ ] Detection in `<engine>_analysis_tools.py`; new SQL facts in the fact modules
- [ ] Scoring adjustments, with pattern bonuses as `table_bonus` data
- [ ] Cost estimate from the price table, with the price-list date recorded
- [ ] Decision trace contract, registered in `contract_versioning.py`
- [ ] Entry point, `TargetDatabase` value (contract bump declared), handler case, `run_analysis.py`
- [ ] Commands, skills and `.claude/settings.ci.json` rules
- [ ] Schema designer and its contract, if the engine routes queries
- [ ] Fixtures and tests; routing snapshot regenerated and every moved query explained
- [ ] Checked against ADR-031 and every decision it touches (AGENTS.md "Before asking for review", item 7)
- [ ] `make lint` and `make test`; `./ci/pre_merge.sh` before pushing

## File reference

| File | Purpose |
|---|---|
| `src/catalog/engines/`, `patterns/`, `gates.toml`, `triage_signals.toml`, `access_classes.toml`, `prices.toml` | The rule catalog: the source of every engine fact and score |
| `src/shared/catalog/` | Typed loaders for the catalog |
| `docs/architecture/rule-catalog.md` | Generated pattern-by-engine matrix |
| `src/contracts/collector_output.py`, `analysis_input.py`, `analysis_output.py` | The input and output contracts (read-only for an engine author, except the `TargetDatabase` value) |
| `src/tools/analysis/scoring.py` | Shared table scoring and aggregate identification |
| `src/agents/referee/query_scoring.py` | The one query scorer routing uses |
| `src/tools/analysis/dynamodb_*`, `src/agents/analysis/dynamodb_analysis_agent.py` | DynamoDB reference implementation |
| `src/tools/analysis/opensearch_*`, `src/agents/analysis/opensearch_analysis_agent.py` | OpenSearch (search read model) |
| `src/tools/analysis/redis_analysis_tools.py`, `src/agents/analysis/elasticache_analysis_agent.py` | ElastiCache (cache overlay) |
