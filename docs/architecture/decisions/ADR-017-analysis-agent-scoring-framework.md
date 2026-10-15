# ADR-017: Analysis Agent Scoring Framework

**Status:** Partially superseded: Decision 2 (per-engine pattern catalogs, by ADR-031 Decision 8); the Decision 4 weights table (the code uses other weights)
**Date:** 2026-03-13
**Deciders:** Database Modernizer Assessment Architecture Team
**Related ADRs:** ADR-006 (Analysis Agent Architecture)

> Superseded in part by [ADR-031](ADR-031-query-first-routing-and-engine-rule-catalog.md): per-engine pattern catalogs with independent detection code are replaced by one engine-neutral catalog and detector per pattern.

---

## Context

ADR-006 established that analysis agents are a category of specialized agents, each evaluating workloads for a specific target database. The Redis/ElastiCache agent was the first implementation, using a per-table scoring pipeline where queries were grouped by table and scored in aggregate.

As we began building the DynamoDB agent, the architecture review identified several limitations in the per-table approach:

1. A single table participates in multiple access patterns — per-table aggregation loses this nuance
2. Pattern confidence scores were computed by a generic formula rather than reflecting specialist domain knowledge
3. No mechanism existed to trace how a recommendation was derived, making calibration impossible
4. Score weights were hardcoded and identical across all targets, despite different targets having different strengths

The analysis agent is the core product value — the component where specialist team knowledge translates into migration recommendations. The scoring framework needs to support specialist-curated knowledge as a first-class input.

## Decisions

### 1. Queries are the atomic unit of analysis

**Decision:** Pattern matching happens per-query, not per-table. Table recommendations are a derived view aggregated from per-query matches.

**Alternatives considered:**

- Per-table scoring (Redis v1 approach): simpler but loses per-query pattern detail. A table with 10 key-value lookups and 2 complex aggregations gets a blended score that hides both signals.
- Per-table-cluster scoring (FK groups): premature — we don't have real-world data showing FK clusters matter more than individual table patterns. Deferred to Phase 1.

**Rationale:** The collector already provides per-query data (digest, frequency, latency, tables accessed). Matching at the query level preserves the full signal. Aggregation to tables is a presentation concern, not an analysis concern. The decision trace needs per-query granularity for specialist calibration.

### 2. Specialist-curated pattern catalogs drive scoring

**Decision:** Each target agent defines a pattern catalog with specialist-assigned base scores. The `pattern_match_score` dimension (highest weight) is driven by these catalog scores, not a generic formula.

**Alternatives considered:**

- Generic formula only (Redis v1): `pattern_count * 25, capped at 60` — treats all patterns equally and doesn't encode specialist knowledge.
- LLM-driven scoring: too expensive, non-deterministic, hard to calibrate. Deferred to Phase 1 for unmatched queries only.

**Rationale:** A DynamoDB specialist knows that "key-value lookup → DynamoDB" is a 95% confidence match, while "denormalizable relationship" is 70% (needs design work). This knowledge should be encoded as data in the catalog, not buried in if/else logic. The catalog is version-controlled and evolves with each assessment.

*Superseded by ADR-031 Decision 8 (#707): per-engine pattern catalogs with independent detection code are replaced by one engine-neutral catalog, one file per access-pattern type. Scores are still catalog data, not a generic formula.*

### 3. Decision trace is a required output

**Decision:** Every analysis agent must produce a `decision-trace.json` as a separate S3 artifact alongside `analysis.json`. The trace is not part of the agent-to-agent contract — Referee-Synthesis only reads `analysis.json`.

**S3 path convention:**

```
<database-name>/<ksuid>/analysis-<target>/analysis.json        ← contract output
<database-name>/<ksuid>/analysis-<target>/decision-trace.json  ← feedback artifact
```

**Alternatives considered:**

- No trace (Redis v1): impossible to calibrate. When a specialist disagrees with a recommendation, there's no way to understand why the agent made that call.
- Trace embedded in analysis.json: couples the feedback loop to the agent contract. Referee-Synthesis would need to ignore trace fields. Separate artifact is cleaner.
- Trace in DynamoDB/database: over-engineered for Phase 0. S3 JSON files are sufficient for artisanal calibration.

**Rationale:** The calibration loop is: specialist reviews trace → classifies deltas (true/false positive/negative) → adjusts catalog scores → re-run. Without the trace, this loop doesn't work. The trace format can evolve independently of the agent contract.

### 4. Score weights are per-target configurable

**Decision:** Each target catalog defines its own score dimension weights. `compute_confidence()` accepts an optional weights dict. Default remains 40/30/20/10 for backward compatibility.

| Target   | pattern_match | complexity | performance | cost |
| -------- | ------------- | ---------- | ----------- | ---- |
| Default  | 40%           | 30%        | 20%         | 10%  |
| DynamoDB | 50%           | 25%        | 15%         | 10%  |
| Redis    | 40%           | 20%        | 30%         | 10%  |

*Superseded (#701): `DYNAMODB_SCORE_WEIGHTS` in `src/tools/analysis/dynamodb_pattern_catalog.py` is 60/20/5/15, and `src/tools/analysis/redis_analysis_tools.py` passes no weights, so Redis uses the 40/30/20/10 default.*

**Alternatives considered:**

- Universal weights: simpler but wrong. DynamoDB's value proposition is access pattern fit (pattern_match), while Redis's is latency improvement (performance). Same weights for both misrepresents the analysis.
- Fully dynamic weights (learned from data): premature. Need 2-3 agents producing results on real data before we can learn optimal weights.

**Rationale:** Specialist knowledge extends to knowing which scoring dimensions matter most for their target. DynamoDB specialists weight pattern match higher because DynamoDB's suitability is almost entirely determined by access pattern fit. Redis specialists weight performance higher because the value proposition is latency reduction.

### 5. Workload segments are per-pattern groupings

**Decision:** A workload segment = all queries matching a single catalog pattern. One pattern = one segment.

**Alternatives considered:**

- Per-table clusters: doesn't capture cross-table patterns (e.g., "session management" spans sessions + user_tokens tables).
- Human-defined segment templates: too much upfront work for Phase 0. Can be added to the catalog later if per-pattern grouping proves insufficient.
- ML-based clustering: premature. No training data.

**Rationale:** Simplest model that captures the customer's mental model. "Your session management workload (12 queries, 3 tables) maps to DynamoDB at 92% confidence" is actionable. If we need finer segmentation, the decision trace has the per-query data to support it.

## Enforcement

Each decision names the guard test that fails when the code stops following it. `tests/unit/docs/test_adr_enforcement.py` fails when a decision has no row here, or when a row names a test file, class or function that does not exist. A decision with no guard yet says `none yet` and the issue that will add one; it is never left blank.

| Decision | Enforced by |
|---|---|
| 1. Queries are the atomic unit of analysis | `tests/unit/test_assignment_resolver.py::TestOpenSearchScoresTheQueryNotTheTable`<br>`tests/unit/test_assignment_resolver.py::TestComputeQueryConfidence::test_pattern_match_on_a_table_outside_the_querys_own_tables_has_no_effect` |
| 2. Specialist-curated pattern catalogs drive scoring | superseded -- #707 (ADR-031 Decision 8 replaces per-engine catalogs with one engine-neutral catalog; its guard is #639) |
| 3. Decision trace is a required output | `tests/unit/agents/analysis/test_decision_trace_completeness_properties.py`<br>`tests/unit/agents/analysis/test_decision_trace_artifact.py` |
| 4. Score weights are per-target configurable | `tests/unit/agents/referee/test_cost_dimension.py::test_an_engine_may_weight_cost_differently_but_keeps_the_same_four_dimensions`<br>`tests/unit/agents/analysis/test_score_weights_per_target.py`<br>superseded -- #701 (the weights table does not match the code, `src/tools/analysis/dynamodb_pattern_catalog.py`) |
| 5. Workload segments are per-pattern groupings | `tests/unit/agents/analysis/test_workload_segment_per_pattern.py`<br>none yet -- #720 (Aurora emits `aurora-common-10` once per table) |

## Consequences

### Positive

- Specialist knowledge is encoded as version-controlled data, not code
- Per-query granularity enables precise calibration and debugging
- Decision trace creates a feedback loop for continuous improvement
- Per-target weights allow each agent to express its target's strengths
- Framework scales to new targets without modifying shared scoring code

### Negative

- More complex than per-table scoring — each agent needs a catalog, detection logic, and trace builder
- Catalog maintenance is a human process — specialists must review and update scores
- Per-query matching is more expensive computationally (though still sub-second for typical workloads)

### Risks

- Catalog scores are expert priors that may be wrong initially — the calibration loop mitigates this
- Per-pattern segmentation may be too coarse for complex workloads — decision trace data will reveal if finer segmentation is needed

## Implementation

- `src/catalog/patterns/<pattern_type>.toml` — specialist-curated patterns with base scores (ADR-031 Decision 8); `src/tools/analysis/<target>_pattern_catalog.py` builds each engine's lists from them and still holds the weights
- `src/tools/analysis/<target>_analysis_tools.py` — per-query detection, scoring adjustments, decision trace builder
- `src/tools/analysis/scoring.py` — shared scoring with configurable weights (`compute_confidence(scores, weights=...)`)
- Reference implementation: DynamoDB agent (`src/agents/analysis/dynamodb_analysis_agent.py`)
- Implementation guide: `do../guides/new-analysis-agent-guide.md`
