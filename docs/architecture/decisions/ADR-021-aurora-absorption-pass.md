# ADR-021: Aurora Absorption Pass in Reality Check

**Status:** Accepted
**Date:** 2026-05-29
**Deciders:** Database Modernizer Assessment Architecture Team

## Context

With Aurora PostgreSQL and Aurora MySQL added as target engines (ADR-018 established the reality check flow), the pipeline now has a "jack-of-all-trades" engine available. Aurora can serve virtually any relational workload pattern: joins, aggregations, transactions, text search (basic), pagination, and CRUD.

The current reality check evaluates each engine's unique value and consolidates redundant engines into more general-purpose alternatives already committed in the architecture. However, the consolidation logic is **unidirectional toward NoSQL engines**. It asks "can DynamoDB absorb this?" but never asks "can Aurora absorb orphan queries from engines that don't justify their operational cost?"

### Observed Problem

In real workloads (WordPress, Discourse), we see engines surviving the reality check with very few queries:

- **WordPress**: DocumentDB retained 3 queries after partial consolidation. The customer would pay $200/mo for a DocumentDB cluster serving 3 access patterns, while Aurora MySQL (already committed) could serve them at 50-70% confidence.
- **Discourse**: Similar patterns where low-count engines survive because the current logic only considers NoSQL-to-NoSQL consolidation.

The CTO question is clear: "Am I really keeping a $200/mo database cluster for 3 queries when I already have Aurora in the stack?"

### Key Insight

The customer is migrating FROM RDS (single-instance relational). Aurora is a NEW engine in the target architecture, not free. However, once Aurora is committed (selected during triage and surviving the unique value assessment), the marginal cost of absorbing additional queries into it is near-zero. Aurora becomes a **gravitational absorber** for orphan queries from engines that don't justify their operational overhead.

## Decision

Add an **Aurora Absorption Pass** to the reality check, executed after the existing unique value assessment (Pass 0) and before consolidation (Pass 1). This pass runs only when an Aurora engine is already committed in the architecture.

### Rules

1. **Activation condition**: An Aurora engine (aurora_postgresql or aurora_mysql) must already be in the committed engine set after Pass 0. This pass never introduces Aurora; it only leverages an Aurora already committed.

2. **Candidate identification**: Any non-Aurora engine with fewer than 10 queries after initial assignment is a candidate for Aurora absorption. This threshold reflects the operational reality: a dedicated database cluster for < 10 access patterns rarely justifies its cost.

3. **Absorption fitness gate (dual condition)**: A query is absorbable into Aurora only if BOTH conditions are met:
   - Aurora fit score >= 50 (Aurora can adequately serve the query)
   - Specialist delta < 30 (the current engine isn't dramatically better than Aurora)

   The specialist delta is: `specialist_fit_score - aurora_fit_score`. This is the core protection mechanism and it is **engine-agnostic**. It handles all cases universally without engine-specific rules:
   - DynamoDB scores 95 on a high-frequency PK lookup, Aurora scores 45: delta = 50, **protected**
   - OpenSearch scores 90 on full-text search with BM25 relevance, Aurora scores 30: delta = 60, **protected**
   - ElastiCache scores 85 on sub-ms session cache, Aurora scores 20: delta = 65, **protected**
   - DocumentDB scores 55 on basic CRUD, Aurora scores 52: delta = 3, **absorbable**

   This captures the fundamental insight: a specialist engine with only 3 queries still earns its place if those queries are purpose-built for it. Three DynamoDB PK lookups doing 10,000 reads/sec at sub-millisecond latency provide genuine operational relief to Aurora (no connection pool pressure, no read replica scaling, zero marginal cost on serverless). The fit score already encodes "is this engine purpose-built for this pattern," so the delta naturally protects high-value specialist workloads regardless of which engine they run on.

4. **Protected engines**: Engines with mandatory signal overrides (e.g., OpenSearch committed via `text_search` signal) are never candidates for absorption, regardless of query count.

5. **Cost justification**: The absorption only proceeds if eliminating the engine saves more than the degradation cost. Formula: `engine_base_cost > (queries_below_threshold * penalty_per_degraded_query)`. With current values ($200 DocumentDB, $10 penalty per query), a 3-query DocumentDB is always absorbed ($200 > $30).

6. **Consolidation direction**: Aurora absorbs FROM other engines. Other engines never absorb FROM Aurora in this pass (Aurora's queries were already evaluated in Pass 0).

### Updated Reality Check Flow

```
Pass 0: Unique Value Assessment (existing, iterative elimination)
Pass 1: Aurora Absorption (NEW, absorb orphans into committed Aurora)
Pass 2: Consolidation (existing, distribute queries from redundant engines)
Pass 3: Architectural Pattern Detection (existing)
Pass 4: Integration Topology (existing)
```

### Thresholds

| Parameter                           | Value | Rationale                                                           |
| ----------------------------------- | ----- | ------------------------------------------------------------------- |
| `AURORA_ABSORPTION_QUERY_THRESHOLD` | 10    | A CTO would question keeping a cluster for < 10 patterns            |
| `AURORA_ABSORPTION_MIN_FIT`         | 50    | Aurora must be "adequate", not just technically capable             |
| `SPECIALIST_DELTA_THRESHOLD`        | 30    | If the specialist is 30+ points better than Aurora, the query stays |
| `DEGRADATION_PENALTY_PER_QUERY`     | 10    | Dollar-equivalent cost of serving a query suboptimally              |

## Consequences

### Positive

- Eliminates low-value engine clusters that survive current logic due to unidirectional consolidation
- Reduces operational overhead; fewer databases to monitor, patch, backup, and maintain expertise for
- Makes Aurora's role explicit: the relational safety net that catches orphan workloads
- Aligns with CTO decision-making: "is this engine earning its $200/mo for 3 queries?"

### Negative

- Queries absorbed by Aurora may be served at lower quality than the original specialist engine (mitigated by the >= 50 fit score gate)
- Customers migrating from RDS might see Aurora absorbing too much, reducing the "modernization" value proposition (mitigated by the threshold, engines with 10+ queries still survive)

### Risks

- Threshold tuning: 10 queries and 50 fit score are initial values based on WordPress/Discourse runs. May need adjustment after broader testing.
- Mitigated by: human approval gate after reality check allows customers to override any absorption decision.

## Alternatives Considered

1. **Option B, Introduce Aurora during reality check**: Rejected. If triage didn't select Aurora, introducing it at reality check re-opens engine selection decisions that were already made. Creates circular logic.

2. **Lower MIN_QUERIES_THRESHOLD globally**: Rejected. The existing threshold (5) only triggers "trivial consolidation" which moves queries to other NoSQL engines. The problem is consolidation direction, not threshold value.

3. **Make absorption LLM-driven**: Rejected. The decision is mechanical (query count + fit score). Adding LLM reasoning here introduces non-determinism without proportional value.
