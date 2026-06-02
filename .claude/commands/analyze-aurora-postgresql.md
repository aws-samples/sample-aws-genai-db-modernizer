
# /analyze-aurora-postgresql

Runs the Aurora PostgreSQL analysis phase. This phase is fully deterministic (no LLM needed).

> **Note:** Aurora PG analysis uses catalog-driven pattern detection with compiled regex
> for PG-specific features (CTEs, window functions, JSONB, arrays, LATERAL joins,
> tsvector, ON CONFLICT). Scoring uses a graduated relational need score (15-65)
> instead of a flat baseline, ensuring tables with no relational need score honestly low.

## Prerequisites

- `.modernizer-state.json` exists with `selected_engines` containing "aurora_postgresql"
- Collector phase is complete

## Steps

1. **Read state**
   Read `.modernizer-state.json` to get `job_id` and `database_name`.

2. **Run analysis**

   ```bash
   uv run python scripts/run_analysis.py --job-id {job_id} --db {database_name} --engine aurora_postgresql --llm-mode none
   ```

3. **Present results**
   Read `.artifacts/{database_name}/{job_id}/analysis-aurora_postgresql/analysis.json` and show:
   - Table recommendations (with score breakdown: pattern_match, complexity, performance, cost)
   - PG-specific patterns detected (CTEs, window functions, JSONB, arrays, tsvector, upsert)
   - Common relational patterns (complex joins, aggregations, transactions, pagination)
   - Anti-patterns detected (no-relational-need, single-access-pattern, high-freq-pk-lookup)
   - Concerns and migration complexity per table

4. **Update state**
   Update `.modernizer-state.json`: set `phase_status.analysis_aurora_postgresql` = "complete"
