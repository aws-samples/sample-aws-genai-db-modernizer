
# /analyze-elasticache

Runs the ElastiCache/Redis analysis phase. This phase is fully deterministic (no LLM needed).

## Tool Use

Read files with the Read tool (use `offset`/`limit` for large files). Search file contents with `uv run python scripts/search_artifacts.py <regex> <path>` (or the Grep tool if this session has one). Use Bash only for the documented `uv run python scripts/…` commands; never use `cat`, `jq`, `python3 -c`, `sed`, `ls`, `cd` chains, heredocs or `grep`.

## Steps

1. **Read state**
   Read `.modernizer-state.json` to get `job_id`, `database_name`.

2. **Run analysis**

   ```bash
   uv run python scripts/run_analysis.py --job-id {job_id} --db {database_name} --engine elasticache --llm-mode none
   ```

3. **Present results**
   Read `artifacts/{database_name}/{job_id}/analysis-elasticache/analysis.json` and show:
   - Caching patterns detected
   - Session / leaderboard patterns
   - Cost estimate

4. **Update state**
   Set `phase_status.analysis_elasticache` = "complete"
