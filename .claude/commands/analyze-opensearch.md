
# /analyze-opensearch

Runs the OpenSearch analysis phase. This phase is fully deterministic (no LLM needed).

## Steps

1. **Read state**
   Read `.modernizer-state.json` to get `job_id`, `database_name`.

2. **Run analysis**

   ```bash
   uv run python scripts/run_analysis.py --job-id {job_id} --db {database_name} --engine opensearch --llm-mode none
   ```

3. **Present results**
   Read `.artifacts/{database_name}/{job_id}/analysis-opensearch/analysis.json` and show:
   - Text search patterns detected
   - Time-series / log patterns
   - Cost estimate

4. **Update state**
   Set `phase_status.analysis_opensearch` = "complete"
