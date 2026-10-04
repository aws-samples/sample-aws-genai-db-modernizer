
# /analyze-aurora-postgresql

Runs the Aurora PostgreSQL analysis phase. This phase is fully deterministic (no LLM needed).

> **Note:** Aurora PG analysis uses catalog-driven pattern detection with compiled regex
> for PG-specific features (CTEs, window functions, JSONB, arrays, LATERAL joins,
> tsvector, ON CONFLICT). Scoring uses a graduated relational need score (15-65)
> instead of a flat baseline, ensuring tables with no relational need score honestly low.

## Tool Use

Read files with the Read tool (use `offset`/`limit` for large files). Search file contents with `uv run python scripts/search_artifacts.py <regex> <path>` (or the Grep tool if this session has one). Use Bash only for the documented `uv run python scripts/…` commands; never use `cat`, `jq`, `python3 -c`, `sed`, `ls`, `cd` chains, heredocs or `grep`.

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
   Read `artifacts/{database_name}/{job_id}/analysis-aurora_postgresql/analysis.json` and show:
   - Table recommendations (with score breakdown: pattern_match, complexity, performance, cost)
   - PG-specific patterns detected (CTEs, window functions, JSONB, arrays, tsvector, upsert)
   - Common relational patterns (complex joins, aggregations, transactions, pagination)
   - Anti-patterns detected (no-relational-need, single-access-pattern, high-freq-pk-lookup)
   - Concerns and migration complexity per table

4. **Update state**
   Update `.modernizer-state.json`: set `phase_status.analysis_aurora_postgresql` = "complete"
