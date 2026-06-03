
# /analyze-dynamodb

Runs the DynamoDB analysis phase. This phase is fully deterministic (no LLM needed).

> **Note:** LLM-powered analysis was removed in favor of deterministic results. The LLM seam
> infrastructure remains available via `--llm-mode bedrock` for future use, but the default
> pipeline relies on deterministic pattern detection, scoring, and aggregate identification.
> Key design decisions are deferred to the schema design phase where full context is available.

## Prerequisites

- `.modernizer-state.json` exists with `selected_engines` containing "dynamodb"
- Collector phase is complete

## Steps

1. **Read state**
   Read `.modernizer-state.json` to get `job_id` and `database_name`.

2. **Run analysis**

   ```bash
   uv run python scripts/run_analysis.py --job-id {job_id} --db {database_name} --engine dynamodb --llm-mode none
   ```

3. **Present results**
   Read `.artifacts/{database_name}/{job_id}/analysis-dynamodb/analysis.json` and show:
   - Table recommendations
   - Patterns detected
   - Anti-patterns
   - Cost estimate
   - Aggregate recommendations (co-access groups)

4. **Update state**
   Update `.modernizer-state.json`: set `phase_status.analysis_dynamodb` = "complete"
