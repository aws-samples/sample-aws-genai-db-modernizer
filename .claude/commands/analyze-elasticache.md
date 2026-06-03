
# /analyze-elasticache

Runs the ElastiCache/Redis analysis phase. This phase is fully deterministic (no LLM needed).

## Steps

1. **Read state**
   Read `.modernizer-state.json` to get `job_id`, `database_name`.

2. **Run analysis**

   ```bash
   uv run python scripts/run_analysis.py --job-id {job_id} --db {database_name} --engine elasticache --llm-mode none
   ```

3. **Present results**
   Read `.artifacts/{database_name}/{job_id}/analysis-elasticache/analysis.json` and show:
   - Caching patterns detected
   - Session / leaderboard patterns
   - Cost estimate

4. **Update state**
   Set `phase_status.analysis_elasticache` = "complete"
