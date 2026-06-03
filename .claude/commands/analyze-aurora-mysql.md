
# /analyze-aurora-mysql

Runs the Aurora MySQL analysis phase. This phase is fully deterministic (no LLM needed).

> **Note:** Aurora MySQL analysis uses catalog-driven pattern detection with MySQL-specific
> features (ON DUPLICATE KEY, STRAIGHT_JOIN, GROUP_CONCAT). Scoring uses a graduated
> relational need score (15-65) instead of a flat baseline, ensuring tables with no
> relational need score honestly low.

## Prerequisites

- `.modernizer-state.json` exists with `selected_engines` containing "aurora_mysql"
- Collector phase is complete

## Steps

1. **Read state**
   Read `.modernizer-state.json` to get `job_id` and `database_name`.

2. **Run analysis**

   ```bash
   uv run python scripts/run_analysis.py --job-id {job_id} --db {database_name} --engine aurora_mysql --llm-mode none
   ```

3. **Present results**
   Read `.artifacts/{database_name}/{job_id}/analysis-aurora_mysql/analysis.json` and show:
   - Table recommendations (with score breakdown: pattern_match, complexity, performance, cost)
   - MySQL-specific patterns detected (ON DUPLICATE KEY, STRAIGHT_JOIN, GROUP_CONCAT)
   - Common relational patterns (complex joins, aggregations, transactions, pagination)
   - Anti-patterns detected (no-relational-need, single-access-pattern, high-freq-pk-lookup)
   - Concerns and migration complexity per table

4. **Update state**
   Update `.modernizer-state.json`: set `phase_status.analysis_aurora_mysql` = "complete"
