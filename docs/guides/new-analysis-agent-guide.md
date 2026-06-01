# Building a New Analysis Agent: Practical Walkthrough

**Audience:** Engineers and LLMs implementing new analysis agents
**Reference implementation:** DynamoDB agent (primary), ElastiCache/Redis agent (legacy)
**Last updated:** March 2026

---

## Overview

An analysis agent evaluates a source database workload (collected by a Collector agent) and recommends whether queries and tables are suitable for migration to a specific AWS target database. This guide walks through the architecture decisions, data flow, and implementation steps.

### Core Principles

1. **Queries are the atomic unit.** Pattern matching happens per-query, not per-table. Tables are a derived view.
2. **Specialist-curated catalogs drive scoring.** Pattern confidence comes from domain experts.
3. **Decision traces are required.** Every agent writes a `decision-trace.json` alongside `analysis.json` for calibration and transparency.
4. **Deterministic detection first.** Regex, keywords, query structure. LLM is optional Phase 1 enhancement for unmatched queries.

### What you'll build

```
src/tools/analysis/<target>_pattern_catalog.py    # Specialist-curated patterns
src/tools/analysis/<target>_analysis_tools.py     # Detection + scoring + costs
src/agents/analysis/<target>_analysis_agent.py    # Entry point
tests/fixtures/<target>_pattern_fixtures.py       # Per-pattern test fixtures
tests/unit/agents/analysis/test_<target>_analysis_agent.py
```

### Data flow

```
CollectorOutputContract (dict)
        │
        ▼
AnalysisInput(collector_output=..., target_database=...)
        │
        ▼
analyze_for_<target>(analysis_input)
        │
        ├── 1. Per-query pattern matching against catalog
        │       → list[QueryMatch] (query_id, pattern_id, signals, score)
        │
        ├── 2. (Optional) Target-specific structural detection
        │       → Varies by target. Examples:
        │         DynamoDB: PK classification, GSI candidates, denorm sub-types,
        │                   secondary index dominance
        │         DocumentDB: schema complexity, embedding candidates
        │         OpenSearch: index mapping analysis, field type detection
        │
        ├── 3. Aggregate identification (shared, engine-agnostic)
        │       → Groups related tables by FK + co-access patterns
        │       → list[AggregateRecommendation] (optional, in output contract v2.1)
        │
        ├── 4. Aggregate into workload segments (per-pattern grouping)
        │       → list[WorkloadSegment] (pattern, queries, tables, score)
        │
        ├── 5. Derive per-table recommendations from segments
        │       → list[TableRecommendation]
        │
        ├── 6. Estimate costs
        │       → CostEstimate
        │
        ├── 7. (Optional) LLM advisor second pass
        │       → Target-specific recommendations (Phase 1, DynamoDB first)
        │
        └── 8. Build decision trace
                → decision-trace.json (all detection internals recorded here)
        │
        ▼
AnalysisOutputContract v2.1 + decision-trace.json
```

---

## Step 1: Define your pattern catalog

Start here — before writing any detection code. The catalog is the specialist's knowledge encoded as data.

**Reference:** `src/tools/analysis/dynamodb_pattern_catalog.py`

Each catalog pattern includes:

| Field | Purpose |
|-------|---------|
| `pattern_id` | Unique ID (e.g., `dynamodb-01`) |
| `pattern_type` | Human-readable type (e.g., `key-value-lookup`) |
| `description` | Specialist-written explanation of the pattern |
| `base_score` | Specialist-assigned confidence (0-100) — this is the expert prior |
| `detection_signals` | List of signals that indicate this pattern |
| `concerns` | Known caveats or migration considerations |

### Pattern examples by target

| Target | Patterns |
|--------|----------|
| DynamoDB | key-value-lookup (100), range-query (90), write-heavy-ingestion (85), time-series-event-log (85), metadata-config-store (90), session-store (90), bounded-parent-child (75), many-to-many-junction (65), co-accessed-tables (70), adjacency-list (60) |
| Redis | caching-layer (95), session-store (90), rate-limiter (85), leaderboard (85), pub-sub (80) |
| DocumentDB | content-management (90), product-catalog (85), polymorphic-data (80), nested-document (85) |
| OpenSearch | full-text-search (95), log-analytics (90), faceted-navigation (85) |
| Neptune | social-graph (95), fraud-detection (90), knowledge-graph (85), recommendation-engine (80) |

### Anti-patterns

Anti-patterns are concerns, not blockers. Each has a severity and guidance:

- `CRITICAL` — likely not suitable for this target without significant redesign
- `HIGH` — needs attention, may reduce confidence significantly
- `MEDIUM` — manageable with design changes
- `LOW` — minor concern, document and move on

**Important:** A full table scan is not automatically an anti-pattern. A scan at 1/hour is fine. A scan at 1/second is a problem. Frequency matters. Similarly, JOINs are not anti-patterns for DynamoDB — they're signals for denormalization design.

### Score weights (per-target configurable)

Each catalog defines its own score dimension weights. Defaults:

| Dimension | Default | DynamoDB | Redis |
|-----------|---------|----------|-------|
| pattern_match_score | 40% | 50% | 40% |
| complexity_score | 30% | 25% | 20% |
| performance_score | 20% | 15% | 30% |
| cost_score | 10% | 10% | 10% |

Weights are defined in the catalog module and passed to the scoring layer. They are not hardcoded in `scoring.py`.

---

## Step 2: Implement per-query pattern detection

This is the core of your agent. For each query in the collector output, match it against your catalog patterns.

**Reference:** `src/tools/analysis/dynamodb_analysis_tools.py` → `analyze_dynamodb_use_cases()`

### Per-query matching

```python
def analyze_<target>_use_cases(collector_output: dict) -> WorkloadAnalysis:
    queries = collector_output.get("queries", {}).get("query_patterns", [])
    tables = collector_output.get("database_schema", {}).get("tables", [])

    # Build lookup structures (PK columns, table sizes, etc.)
    # ...

    # Per-pattern accumulators
    pattern_a_queries: list[dict] = []
    pattern_b_queries: list[dict] = []

    for q in queries:
        text_lower = q.get("query_text", "").lower()

        # Use if (not elif) — a query can match multiple patterns
        if <condition_for_pattern_a>:
            pattern_a_queries.append(q)
        if <condition_for_pattern_b>:
            pattern_b_queries.append(q)

    # Build Pattern objects from non-empty accumulators
    patterns = _build_patterns_from_matches([
        (CATALOG_PATTERNS[0], pattern_a_queries),
        (CATALOG_PATTERNS[1], pattern_b_queries),
    ])

    return WorkloadAnalysis(patterns_detected=patterns, anti_patterns_detected=anti_patterns)
```

### Detection techniques (ordered by preference)

1. **Structural signals** — query type, row counts, PK usage, join count, frequency. Most reliable.
2. **Text matching** — keywords in query text (`session`, `created_at`, `ORDER BY ... LIMIT`). Fast but prone to false positives.
3. **Combined** — structural + text together for higher confidence.
4. **LLM (Phase 1)** — for unmatched queries only. Not implemented in Phase 0.

### Pattern validation guidance

Heuristics are a starting point. Every detection rule needs validation against real collector outputs:

- Track false positives: `"user_id" in text` matches any user-table query, not just session queries. Combine with structural signals (TTL columns, high frequency) to improve precision.
- Use the `Confidence` enum to signal certainty: `HIGH` for structural matches, `MEDIUM` for text-only, `LOW` for weak signals.
- The decision trace captures every match decision — use it for calibration.

---

## Step 3: Aggregate into workload segments

A workload segment = all queries matching a single catalog pattern. This is the unit customers think about: "my session management workload" not "query q-014."

**Decision:** Workload segments are per-pattern groupings. One pattern = one segment. A segment contains all queries that matched that pattern, the tables they touch, and the catalog base score.

```
Segment: "session-store"
  Pattern: dynamodb-06 (base_score=90)
  Queries: [q-003, q-017, q-042] (12 calls/sec total)
  Tables: [mydb.sessions, mydb.user_tokens]
  Adjusted score: 88 (minor penalty: no TTL column detected)
```

### From segments to table recommendations

Tables are a derived view from segments. A table's recommendation is driven by the segments it participates in:

1. For each table, collect all segments that reference it
2. The table's `pattern_match_score` = weighted average of segment scores (weighted by query frequency)
3. Complexity, performance, and cost scores come from the shared scoring layer (`scoring.py`)

### Table grouping and Aggregate identification

Tables connected by foreign keys and co-access patterns can be grouped into **aggregates** — units that should migrate together. This is useful for any target where related tables need coordinated migration (DynamoDB, DocumentDB, etc.).

The shared module `scoring.py` provides engine-agnostic aggregate identification:

- `detect_co_access()` — finds pairs of tables co-accessed by the same query
- `identify_aggregates()` — combines FK adjacency graphs with co-access patterns to find connected components

When a table is part of an aggregate:

- The output contract includes `aggregate_recommendations` with the root table, member tables, co-access confidence, and combined migration complexity
- The decision trace includes the full aggregate membership and co-access evidence
- Downstream consumers (Referee-Synthesis) use aggregates to plan migrations around domain boundaries

Not all agents need aggregates. For targets like OpenSearch or ElastiCache where tables typically migrate independently, you can skip aggregate identification and leave `aggregate_recommendations` as `None`.

### Target-specific structural detection (optional)

Beyond pattern matching and aggregates, some targets benefit from additional structural analysis. This is entirely target-specific — implement what makes sense for your target's data model.

**DynamoDB examples** (in `dynamodb_analysis_tools.py`):

- PK classification — maps relational PKs to partition/sort key candidates
- GSI candidate detection — identifies columns needing Global Secondary Indexes
- Denormalization sub-type classification — bounded-parent-child, junction tables, co-accessed tables, adjacency lists
- Secondary index dominance — flags tables where queries mostly use a secondary index

**DocumentDB might add:**

- Embedding candidate detection — parent-child relationships suitable for nested documents
- Schema polymorphism detection — tables with sparse columns suggesting flexible schemas

**OpenSearch might add:**

- Full-text field detection — text columns with search-like query patterns
- Aggregation pipeline analysis — GROUP BY patterns mapping to OpenSearch aggregations

These detection results go into the decision trace, not the output contract (except `aggregate_recommendations` which is shared).

- The specialist can evaluate the aggregate as a migration unit
- Downstream consumers (Referee-Synthesis) use aggregates to plan migrations around domain boundaries

Aggregate identification is implemented in `scoring.py` (engine-agnostic) and available for other agents to reuse. The DynamoDB-specific detection functions (PK classification, GSI candidates, denormalization sub-types, secondary index dominance) live in `dynamodb_analysis_tools.py`.

---

## Step 4: Use the shared scoring layer

All agents share generic scoring in `src/tools/analysis/scoring.py`. You reuse everything except the target-specific adjustment function.

**Reference:** `src/tools/analysis/scoring.py`

### Pipeline

```python
from src.tools.analysis.scoring import (
    build_table_profiles,    # Generic — reuse as-is
    compute_base_scores,     # Generic — reuse as-is
    compute_confidence,      # Generic — accepts custom weights
)

def analyze_<target>_patterns(collector_output, workload_analysis):
    profiles = build_table_profiles(collector_output, workload_analysis)

    recommendations = []
    for table_id, profile in profiles.items():
        scores = compute_base_scores(profile)
        scores = _apply_<target>_adjustments(scores, profile)
        confidence = compute_confidence(scores, weights=TARGET_WEIGHTS)
        # ... build TableRecommendation with confidence_score=confidence
```

### Writing your adjustment function

Pattern match bonuses come from the catalog base scores. The adjustment function adds target-specific structural bonuses/penalties:

```python
def _apply_<target>_adjustments(scores, profile):
    # Bonuses for patterns this target excels at
    if "key-value-lookup" in profile.pattern_types:
        scores.pattern_match_score += 15

    # Structural adjustments
    if profile.foreign_key_count > 3:
        scores.complexity_score -= 15

    # Clamp all to [0, 100]
    return ScoreBreakdown(...)
```

### Recommendation thresholds

| Confidence | Recommendation |
|------------|---------------|
| >= 80 | HIGHLY_SUITABLE |
| >= 60 | SUITABLE |
| >= 40 | MARGINAL |
| < 40 | NOT_SUITABLE |

---

## Step 5: Write the decision trace

**Required.** Every analysis agent must produce a `decision-trace.json` as a separate S3 artifact alongside `analysis.json`.

```
<database-name>/<ksuid>/analysis-<target>/analysis.json        ← contract output (agent-to-agent)
<database-name>/<ksuid>/analysis-<target>/decision-trace.json  ← feedback artifact (human review)
```

The agent writes both. Referee-Synthesis only reads `analysis.json`. Specialists read `decision-trace.json` for calibration. No contract change needed.

### Trace structure

```json
{
  "trace_version": "1.0",
  "agent": "dynamodb-analysis-agent",
  "summary": {
    "queries_analyzed": 142,
    "queries_matched": 87,
    "queries_unmatched": 55,
    "patterns_detected": 5,
    "anti_patterns_detected": 2
  },
  "query_matches": [
    {
      "query_id": "q-001",
      "query_text_preview": "SELECT * FROM users WHERE id = ?",
      "matched_patterns": ["dynamodb-01"],
      "matched_anti_patterns": [],
      "signals": ["single_row_select", "primary_key_equality", "high_frequency"]
    }
  ],
  "pattern_summaries": [
    {
      "pattern_id": "dynamodb-01",
      "catalog_base_score": 95,
      "queries_matched_count": 23,
      "tables_involved": ["mydb.users", "mydb.sessions"],
      "total_calls_per_second": 45.2,
      "adjusted_score": 92,
      "adjustment_reasons": ["minor penalty: nullable columns in result set"]
    }
  ],
  "recommendation_derivations": [
    {
      "table_id": "mydb.users",
      "segments_contributing": ["dynamodb-01", "dynamodb-06"],
      "score_breakdown": {
        "pattern_match": 88,
        "complexity": 72,
        "performance": 65,
        "cost": 80
      },
      "weights_used": {"pattern_match": 0.5, "complexity": 0.25, "performance": 0.15, "cost": 0.1},
      "weighted_confidence": 79,
      "final_recommendation": "SUITABLE"
    }
  ]
}
```

### Calibration loop (Phase 0)

1. Specialist assigns initial catalog scores
2. Run agent against real collector outputs
3. Specialist reviews `decision-trace.json`, writes assessment (can be a `.md` file)
4. Classify deltas: true positive, false positive, false negative per pattern
5. Adjust catalog scores and detection rules
6. Repeat

Track per agent: pattern precision, pattern recall, recommendation agreement rate, average confidence delta.

---

## Step 6: Write the agent entry point

The agent is a thin orchestrator.

**Reference:** `src/agents/analysis/dynamodb_analysis_agent.py`

```python
def analyze_for_<target>(analysis_input: AnalysisInput) -> AnalysisOutputContract:
    collector_output = analysis_input.collector_output

    workload_analysis = analyze_<target>_use_cases(collector_output)
    table_recommendations = analyze_<target>_patterns(collector_output, workload_analysis)
    cost_estimate = estimate_<target>_costs(collector_output, ...)

    # Optional: aggregate identification (shared, engine-agnostic)
    # Skip if your target doesn't benefit from table grouping.
    # aggregates = identify_aggregates(collector_output, workload_analysis)

    # Optional: target-specific structural detection
    # Add detection functions relevant to your target's data model.
    # Results go to the decision trace, not the output contract.

    decision_trace = build_decision_trace(collector_output, workload_analysis, table_recommendations)

    return AnalysisOutputContract(
        contract_version="2.1",
        agent_metadata=AgentMetadata(
            agent_name="<target>-analysis-agent",
            agent_version="1.0.0",
            target_database=TargetDatabase.<TARGET>,
            analysis_timestamp=datetime.now(),
        ),
        table_recommendations=table_recommendations,
        workload_analysis=workload_analysis,
        cost_estimate=cost_estimate,
        aggregate_recommendations=None,  # Populate from identify_aggregates() if applicable
    ), decision_trace  # Agent handler writes both to S3
```         agent_version="1.0.0",
            target_database=TargetDatabase.<TARGET>,
            analysis_timestamp=datetime.now(),
        ),
        table_recommendations=table_recommendations,
        workload_analysis=workload_analysis,
        cost_estimate=cost_estimate,
        aggregate_recommendations=None,  # Optional — populate if aggregates are detected
    ), decision_trace  # Agent handler writes both to S3
```

---

## Step 7: Cost estimation

Each target has different pricing models. The cost estimation function should:

- Use the simplest pricing model as default (e.g., DynamoDB on-demand, Aurora I/O-optimized)
- Derive throughput from collector query patterns (calls_per_second, read/write ratio)
- Derive storage from table sizes
- Document all assumptions in `pricing_assumptions`
- Not include optional features (DAX, global tables, backups) unless explicitly requested

### Target-specific guidance

| Target | Default pricing | Key inputs |
|--------|----------------|------------|
| DynamoDB | On-demand ($0.125/M RRU, $0.625/M WRU, $0.25/GB) | calls_per_second, read/write ratio, total size |
| ElastiCache | cache.r6g.large ($0.166/hr) | working set size, connections |
| DocumentDB | db.r6g.large ($0.348/hr) + I/O ($0.20/M) | storage, read/write IOPS |
| OpenSearch | m6g.large.search ($0.167/hr) | index size, query rate |
| Neptune | db.r6g.large ($0.348/hr) | vertex/edge count, traversal rate |

---

## Step 8: Error handling and graceful degradation

Agents must handle partial or missing collector output gracefully:

- **Zero queries:** Produce recommendations based on schema structure only (complexity, size). Set `pattern_match_score = 0` for all tables. Note in decision trace.
- **Zero tables:** Return empty recommendations list. Log warning.
- **Missing metrics:** Skip performance scoring. Set `performance_score = 0`.
- **Partial collector output:** Process what's available. Never crash on missing optional fields.

Minimum viable input: at least one table with at least one column. Everything else is optional enrichment.

---

## Step 9: Build test fixtures

Three layers, same as before:

### Layer 1: Per-pattern fixtures (agent-specific)

Small dicts triggering exactly ONE pattern. Used for isolation tests.

**Reference:** `tests/fixtures/dynamodb_pattern_fixtures.py`

### Layer 2: Vertical fixtures (shared across agents)

Realistic production-like fixtures. Shared — the same e-commerce fixture can be analyzed by DynamoDB, Redis, and DocumentDB agents.

**Reference:** `tests/fixtures/ecommerce_collector_output.py`

### Layer 3: Synthetic generator (shared)

For benchmarking. Already exists at `tests/fixtures/generate_synthetic_collector_output.py`.

---

## Step 10: Register with Referee-Triage

After your agent is working:

1. Add the target to the `ANALYSIS_AGENTS` set in `src/agents/entrypoint.py`
2. Add the agent type to the Referee-Triage selection logic
3. The Step Functions Map state already handles dynamic agent lists — no orchestration changes needed

---

## Checklist for a new agent

- [ ] Define pattern catalog with specialist-assigned base scores
- [ ] Define anti-pattern catalog with severity and guidance
- [ ] Define target-specific score weights
- [ ] Implement per-query pattern detection (`analyze_<target>_use_cases()`)
- [ ] Implement scoring adjustments (`_apply_<target>_adjustments()`)
- [ ] Implement cost estimation (`estimate_<target>_costs()`)
- [ ] Implement decision trace builder
- [ ] Wire up agent entry point
- [ ] Wire into `handler.py` dispatch
- [ ] Create per-pattern isolation fixtures
- [ ] Reuse or create vertical fixture
- [ ] Add fixture validation tests
- [ ] Add per-pattern isolation tests + vertical integration test
- [ ] Verify: `pytest tests/ -v` — all green
- [ ] Run against real collector output, review decision trace with specialist

---

## Decisions log

Decisions made for the analysis agent framework (March 2026):

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Workload segment definition | Per-pattern grouping | Simple, deterministic. One pattern = one segment. Can add human-defined templates later. |
| Table grouping (aggregates) | Implemented in Phase 1 | `identify_aggregates()` in `scoring.py` is engine-agnostic (FK + co-access). Optional per agent — skip for targets where tables migrate independently. |
| Score weights | Per-target configurable | Each catalog defines its own weights. DynamoDB: 50/25/15/10. Redis: 40/20/30/10. |
| Fixture reuse | Vertical fixtures shared, per-pattern fixtures agent-specific | Reduces duplication. Synthetic generator for initial development. |
| LLM detection | Phase 1 (in progress) | Deterministic first. LLM advisor as optional second pass via Strands SDK. DynamoDB is the first target; pattern is reusable. |
| Decision trace | Required for all agents | Separate S3 artifact, not part of agent contract. Target-specific detection internals recorded here. |
| Pattern composition | Deferred to Phase 1 | Start with independent pattern matching. Composite rules add complexity without proven value yet. |

---

## File reference

### Contracts (read-only — do not modify)

| File | Purpose |
|------|---------|
| `src/contracts/collector_output.py` | CollectorOutputContract v3.0 — your input |
| `src/contracts/analysis_input.py` | AnalysisInput — wraps collector output |
| `src/contracts/analysis_output.py` | AnalysisOutputContract v2.1 — your output (includes optional `aggregate_recommendations`) |

### Shared scoring (reuse, do not fork)

| File | Purpose |
|------|---------|
| `src/tools/analysis/scoring.py` | `build_table_profiles()`, `compute_base_scores()`, `compute_confidence()` |

### DynamoDB reference implementation

| File | Purpose |
|------|---------|
| `src/tools/analysis/dynamodb_pattern_catalog.py` | Specialist-curated pattern catalog |
| `src/tools/analysis/dynamodb_analysis_tools.py` | Detection, scoring, cost estimation |
| `src/agents/analysis/dynamodb_analysis_agent.py` | Agent entry point |

### Redis implementation (legacy — ships as-is)

| File | Purpose |
|------|---------|
| `src/agents/analysis/redis_analysis_agent.py` | Agent entry point |
| `src/tools/analysis/redis_analysis_tools.py` | Detection, scoring, cost estimation |
