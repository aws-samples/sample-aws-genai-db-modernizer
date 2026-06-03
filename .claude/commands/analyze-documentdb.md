
# /analyze-documentdb

Runs the DocumentDB analysis phase. This phase is fully deterministic (no LLM needed).

> **Note:** LLM-powered analysis was removed in favor of deterministic results. The LLM seam
> infrastructure remains available via `--llm-mode bedrock` for future use, but the default
> pipeline relies on deterministic pattern detection, embedding candidate scoring, and
> co-access analysis. Embed-vs-reference decisions are deferred to the schema design phase.

## Prerequisites

- `.modernizer-state.json` exists with `selected_engines` containing "documentdb"
- Collector phase is complete

## Steps

1. **Read state**
   Read `.modernizer-state.json` to get `job_id` and `database_name`.

2. **Run analysis**

   ```bash
   uv run python scripts/run_analysis.py --job-id {job_id} --db {database_name} --engine documentdb --llm-mode none
   ```

3. **Present results**
   Read `.artifacts/{database_name}/{job_id}/analysis-documentdb/analysis.json` and show:
   - Table recommendations
   - Embedding candidates
   - Patterns detected
   - Cost estimate

4. **Update state**
   Update `.modernizer-state.json`: set `phase_status.analysis_documentdb` = "complete"
